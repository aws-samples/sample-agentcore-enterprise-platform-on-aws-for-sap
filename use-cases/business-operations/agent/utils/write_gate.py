# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The write guards and the write verifier, around every SAP write tool.

The Gateway run checks are the gate for what a run may write: the run grant, the case's own
document and the amount a human must approve above all hold there, in every Cedar mode
(`business_operations_check/run_checks.py`). This wrapper adds only what needs SAP reads or
a model, and so cannot run at the Gateway.

**The write guards.** On a payment-releasing write, when the case's pack declares
`write_guards`: deterministic re-reads of SAP for the SOP rules that protect money.
`write_guards.py` documents them; this module only supplies the case, the pack's
constants and approvers, and the two tools the checks read with.

**The write verifier.** When the case's pack declares `verifier.enabled`, the same
writes, and a `Cancel` on the invoice service, which no guard grades, are put to an
independent model after every guard has passed, with the rows the guards just read.
Its disagree refuses the write exactly as a guard does. See `write_verifier.py`.
"""

import os
import uuid

import boto3
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

from . import write_guards, write_verifier
from .case_key import to_case_key, try_normalize_case_id
from .config_overrides import constant_overrides, resolved_constants
from .evidence import result_text, tool_failed
from .mcp_topology import SAP_WRITE_TOOLS
from .registry import constants, entitlement_map, packs


def guards_by_pack() -> dict[str, list[str]]:
    """`{pack_id: write_guards}` for every published pack that declares any."""
    return {
        pack_id: list(entry["pack"].get("write_guards") or [])
        for pack_id, entry in packs().items()
        if entry["pack"].get("write_guards")
    }


def verifiers_by_pack() -> dict[str, dict]:
    """`{pack_id: verifier}` for every published pack whose verifier is enabled."""
    return {
        pack_id: dict(entry["pack"]["verifier"])
        for pack_id, entry in packs().items()
        if (entry["pack"].get("verifier") or {}).get("enabled")
    }


def _pack_constants(pack_id: str) -> dict:
    """Declared constants with this stack's limit overrides applied."""
    return resolved_constants(
        dict(constants().get(pack_id) or {}), constant_overrides(pack_id)
    )


def _case_identity(case_id: str) -> tuple[str, dict] | None:
    """`(pack_id, key)` as stored, or None if it cannot be read."""
    table_name = os.environ.get("CASES_TABLE")
    if not table_name:
        return None
    try:
        item = (
            boto3.resource("dynamodb")
            .Table(table_name)
            .get_item(
                Key=to_case_key(case_id),
                ProjectionExpression="pack_id, #k",
                ExpressionAttributeNames={"#k": "key"},
            )
            .get("Item", {})
        )
    except Exception as e:  # noqa: BLE001 - refuse on any read failure
        print(f"[WRITE-GUARD] Case lookup failed for {case_id}: {e}")
        return None
    pack = item.get("pack_id")
    if not pack:
        return None
    key = item.get("key") if isinstance(item.get("key"), dict) else {}
    return str(pack), {
        "SupplierInvoice": key.get("SupplierInvoice"),
        "FiscalYear": key.get("FiscalYear"),
    }


class VerifierRefusal(write_guards.GuardRefusal):
    """The verifier did not agree. Refused, logged and traced exactly as a guard is."""

    kind = "verifier_refused"
    source = "an independent review of this write against the SOP"

    def __init__(self, detail: str):
        super().__init__("verifier", detail, write_guards._MANUAL_REVIEW)

    @property
    def reason(self) -> str:
        return f"{self.kind}: {self.detail}"


def _trace_refusal(refusal: "write_guards.GuardRefusal") -> None:
    """Record the refusal on the current span, so a trace shows which guard fired."""
    try:
        from opentelemetry import trace

        trace.get_current_span().add_event(
            refusal.kind,
            {"predicate": refusal.predicate, "detail": refusal.detail[:500]},
        )
    except Exception:  # noqa: BLE001, S110
        pass


class WriteGateTool(AgentTool):
    """Wraps a SAP write tool; refuses one its pack's guards or verifier reject.

    A real `AgentTool` subclass, not a duck-typed stand-in: `ToolRegistry.process_tools`
    drops anything failing `isinstance(tool, AgentTool)` with only a warning, and the
    adapter re-registers the snapshotted registry on the per-thread clone that serves
    the turn. A wrapper that fails that check deletes the tool from the model's config,
    which presents as "the agent never writes to SAP", indistinguishable from a guard
    working.
    """

    def __init__(
        self,
        inner,
        case_id: str | None,
        payload: dict | None = None,
        read_tool=None,
        ticket_tool=None,
        model_tiers: dict | None = None,
        verifier_log: list | None = None,
    ):
        super().__init__()
        self._inner = inner
        self._case_id = case_id
        self._payload = payload or {}
        # The unwrapped-by-scope read tools, captured at wrap time: the guards re-read
        # SAP through exactly the client and Gateway policy the agent's own reads use.
        self._read_tool = read_tool
        self._ticket_tool = ticket_tool
        # Tier → model id, handed in by basic_agent; and where each verifier call's
        # record goes, which MetricsHook prices into the run and writes to the trace.
        self._model_tiers = model_tiers or {}
        self._verifier_log = verifier_log if verifier_log is not None else []

    @property
    def tool_name(self) -> str:
        return self._inner.tool_name

    @property
    def tool_spec(self):
        return self._inner.tool_spec

    @property
    def tool_type(self) -> str:
        return self._inner.tool_type

    async def _call(self, tool, name: str, args: dict, invocation_state) -> str:
        """Run one read tool to completion; its text, or GuardRefusal if it failed."""
        if tool is None:
            raise write_guards.GuardRefusal(
                "sap_reread",
                f"{name} is not loaded on this run, so nothing can be re-read",
            )
        use = {
            "toolUseId": f"write-guard-{uuid.uuid4().hex[:12]}",
            "name": name,
            "input": args,
        }
        result = None
        try:
            async for event in tool.stream(use, invocation_state):
                if isinstance(event, ToolResultEvent):
                    result = event.tool_result
        except Exception as e:  # noqa: BLE001 - any read failure refuses the write
            raise write_guards.GuardRefusal(
                "sap_reread", f"{name} raised {type(e).__name__}: {e}"
            )
        text = result_text(result)
        # A SAP refusal (`success: false`, a 401 say) and a Lambda's `{error}` arrive
        # as successful calls; neither is a read the guards may grade.
        if tool_failed(result):
            raise write_guards.GuardRefusal(
                "sap_reread", f"{name} failed: {text[:200]}"
            )
        return text

    async def _guard_refusal(self, tool_use, invocation_state):
        """The GuardRefusal for a payment release the pack's guards reject, or None."""
        try:
            declared = guards_by_pack()
            verifiers = verifiers_by_pack()
            if not (declared or verifiers):
                return None
            # The case's pack decides first, so a pack that declares no guards is never
            # refused by this module's fail-closed classification below.
            identity = _case_identity(self._case_id) if self._case_id else None
            if identity is not None and not (
                declared.get(identity[0]) or verifiers.get(identity[0])
            ):
                return None
            tool_input = tool_use.get("input")
            # A Cancel is classified only where this case's own pack has a verifier, so
            # enabling one pack never changes how another pack's Cancel is treated.
            release = write_guards.releasing_invoice(
                self.tool_name,
                tool_input,
                cancel_too=bool(identity and verifiers.get(identity[0])),
            )
            if release is None:
                return None
            if identity is None:
                raise write_guards.GuardRefusal(
                    "case_binding",
                    f"case '{self._case_id}' could not be read"
                    if self._case_id
                    else "this run names no case, so no pack's guards can be chosen",
                )
            pack, key = identity
            spec = verifiers.get(pack)
            # Only a Cancel differs between the two readings, and no guard grades one.
            releases = (
                write_guards.releasing_invoice(self.tool_name, tool_input) is not None
            )
            if not (releases or spec):
                return None
            guards = declared.get(pack, []) if releases else []
            invoice, year = release
            if not (
                write_guards.same_number(key["SupplierInvoice"], invoice)
                and write_guards.same_number(key["FiscalYear"], year)
            ):
                raise write_guards.GuardRefusal(
                    "case_binding",
                    f"the write releases invoice {invoice}/{year}, but case "
                    f"'{self._case_id}' is invoice {key['SupplierInvoice']}/{key['FiscalYear']}",
                )

            async def read(service, entity_set, filter_, select):
                args = {
                    "service_name": service,
                    "entity_set": entity_set,
                    "filter": filter_,
                    "select": select,
                    # Stated, so a server-side default page cannot drop the reversal
                    # rows a goods-receipt total nets out.
                    "top": write_guards.READ_TOP,
                }
                rows = write_guards.records(
                    await self._call(
                        self._read_tool, "odata_read", args, invocation_state
                    )
                )
                reads.append(
                    {"entity_set": entity_set, "filter": filter_, "rows": rows}
                )
                return rows

            async def get_ticket(ticket_id):
                if ticket_id not in tickets:
                    text = await self._call(
                        self._ticket_tool,
                        "demo_get_ticket",
                        {"ticket_id": ticket_id},
                        invocation_state,
                    )
                    tickets[ticket_id] = write_guards.ticket_from(text)
                return tickets[ticket_id]

            # What the guards read, kept for the verifier so it never re-reads SAP. With no
            # guards (a Cancel, or a pack with only a verifier) `check` reads the header.
            reads: list[dict] = []
            tickets: dict = {}

            await write_guards.check(
                guards,
                invoice=invoice,
                fiscal_year=year,
                case_id=self._case_id,
                payload=self._payload,
                constants=_pack_constants(pack),
                approve_groups=list(
                    (entitlement_map().get(pack) or {}).get("approve_groups") or []
                ),
                read=read,
                get_ticket=get_ticket,
            )
            if guards:
                print(
                    f"[WRITE-GUARD] {self.tool_name} on {invoice}/{year} passed {guards}"
                )
            if spec:
                await self._verify(
                    spec,
                    tool_input,
                    releases,
                    invocation_state,
                    get_ticket,
                    reads,
                    guards,
                )
            return None
        except write_guards.GuardRefusal as refusal:
            return refusal

    async def _verify(
        self, spec, tool_input, releases, invocation_state, get_ticket, reads, guards=()
    ):
        """Raise VerifierRefusal unless the verifier model agrees with this write.

        Any failure building the question is a refusal too, down the same path, so an odd
        message shape reaches manual review rather than surfacing as a bare tool error.
        """
        # What the write does, by the guards' classification: an odata_update there
        # clears the payment block, so it is graded as the Release it amounts to.
        function = str((tool_input or {}).get("function_name") or "").strip().upper()
        write = function if self.tool_name == "odata_function_import" else "RELEASE"
        if not releases:
            write = "CANCEL"
        try:
            record = await self._ask(
                spec, tool_input, invocation_state, get_ticket, reads, guards
            )
        except Exception as e:  # noqa: BLE001 - a question that cannot be asked refuses
            record = {
                "tier": spec.get("model_tier", ""),
                "model_id": "",
                "verdict": "disagree",
                "reason": f"the verifier could not be asked ({type(e).__name__}: {str(e)[:200]})",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms": 0.0,
            }
        record["tool"] = self.tool_name
        record["function_name"] = function
        record["write"] = write
        self._verifier_log.append(record)
        print(
            f"[COST] verifier model_id={record['model_id']}, "
            f"input_tokens={record['input_tokens']}, output_tokens={record['output_tokens']}, "
            f"cost_usd={record['cost_usd']:.6f}, latency_ms={record['latency_ms']}"
            + (
                ", timed_out=true (cost is a lower bound)"
                if record.get("timed_out")
                else ""
            )
        )
        print(
            f"[WRITE-VERIFIER] {self.tool_name}: {record['verdict']} — {record['reason']}"
        )
        if record["verdict"] != "agree":
            raise VerifierRefusal(record["reason"])

    async def _ask(
        self, spec, tool_input, invocation_state, get_ticket, reads, guards=()
    ) -> dict:
        """Build the checker's question and put it; the call's record."""
        # Whatever the decision was: a denial or a reply is what a wrong write ignores.
        resumed = (
            (self._payload.get("payload") or {}).get("ticket_id")
            if self._payload.get("trigger") == "ticket-action"
            else None
        )
        ticket = None
        if resumed:
            try:
                ticket = await get_ticket(str(resumed))
            except write_guards.GuardRefusal as e:
                print(f"[WRITE-VERIFIER] ticket {resumed} not read: {e.detail}")
        agent = (invocation_state or {}).get("agent")
        said = write_verifier.rationale(agent)
        prompt = write_verifier.build_prompt(
            tool_name=self.tool_name,
            tool_input=tool_input,
            process_type=str(self._payload.get("process_type") or ""),
            sop=write_verifier.cited_sections(
                agent, said, function=str((tool_input or {}).get("function_name") or "")
            ),
            said=said,
            reads=reads,
            ticket=write_verifier.ticket_facts(ticket, self._payload),
            checks_passed=list(guards or []),
            guidance=str(spec.get("guidance") or ""),
        )
        return await write_verifier.verify(spec, self._model_tiers, prompt)

    async def stream(self, tool_use, invocation_state, **kwargs):
        guard = await self._guard_refusal(tool_use, invocation_state)
        if guard is None:
            async for event in self._inner.stream(tool_use, invocation_state, **kwargs):
                yield event
            return
        print(f"[WRITE-GUARD] Refused {self.tool_name}: {guard.reason}")
        _trace_refusal(guard)
        next_step = guard.next_step or write_guards._MANUAL_REVIEW
        yield ToolResultEvent(
            {
                "toolUseId": tool_use.get("toolUseId", ""),
                "status": "error",
                "content": [
                    {
                        "text": (
                            f"Write refused — {guard.reason}. This is {guard.source}, "
                            f"not an error — do not retry, and do not try another "
                            f"tool that writes to SAP. {next_step}"
                        )
                    }
                ],
            }
        )


def wrap_write_gate_tools(
    agent,
    payload: dict,
    model_tiers: dict | None = None,
    verifier_log: list | None = None,
) -> int:
    """Replace the SAP write tools in the agent's registry with guarded wrappers.

    Call AFTER the Gateway `target___` rename. Returns the number of tools wrapped,
    which is 0 when no pack declares a write guard or an enabled verifier.
    """
    if not (guards_by_pack() or verifiers_by_pack()):
        return 0
    registry = agent.tool_registry.registry
    case_id = try_normalize_case_id(payload.get("case_id"))
    read_tool, ticket_tool = registry.get("odata_read"), registry.get("demo_get_ticket")
    wrapped = 0
    for name in list(registry):
        # By its bare name, so a write that kept its target (two SAP systems on one
        # run) is guarded too; its re-read then finds no bare `odata_read` and refuses.
        if name.split("___")[-1] in SAP_WRITE_TOOLS:
            registry[name] = WriteGateTool(
                registry[name],
                case_id,
                payload,
                read_tool,
                ticket_tool,
                model_tiers,
                verifier_log,
            )
            wrapped += 1
    return wrapped
