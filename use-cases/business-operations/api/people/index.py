# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The people target: pack and SOP tools for a person in a remote MCP client.

The platform's people Gateway admits only its remote clients (gate 1), and its
interceptor verifies the person's token and sets `caller` from it on every call
(gate 2), so no token reaches this function. This function is gate 3:

* **A fixed tool set.** :data:`TOOLS` is every tool and the one console route each
  runs, by tier (`PEOPLE_TIERS`, from `people_tools`). Any other tool, or a tool of
  another target, is refused; no route outside the set has a code path here, so limits,
  the trigger mode, the stop, structure and runs are unreachable whatever a call says.
* **A caller, and only the arguments the tool takes.** A call without `caller`
  (`sub`, `client_id` and a group list) was not made through the people Gateway and is
  refused. So is an argument the tool does not take or a missing one, a write without
  an integer `based_on`, a write limit in `set_rule_values`, and more than
  :data:`WRITES_PER_MINUTE` writes by one person in this instance.

Then gate 4 is the console's own code: `business-operations-remote-authors` is required
for every tool, the groups that change structure, limits or runs are removed, and the
route's handler (`api/packs`, `api/sops`, `api/config`) decides with `admit`, the authors
floor, the pack's grants and `pack_tiers.edit_refusal`, from an event shaped as API
Gateway would send it with the caller's claims and `requestContext.door`.

Trusting `caller` rests on two facts: the people Gateway's interceptor replaces it on
every call, and only the people Gateway's role may invoke this function (its resource
policy; IAM is the outer boundary, as for every function here).

One log line per call: `sub`, client, tool, pack, process type, base and written
version, status. Never a token or a SOP's text.
"""

import difflib
import importlib.util
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HERE = Path(__file__).resolve().parent
#: The target's name on the people Gateway; a tool name is `<target>___<tool>`.
TARGET = os.environ.get("PEOPLE_TARGET", "business-operations-people")
REMOTE_AUTHORS_ENV = "REMOTE_AUTHORS_GROUP"
#: Groups a people call never holds, whatever the person's token says: structure,
#: limits, the trigger mode and the stop, and starting runs.
STRIPPED_GROUPS = frozenset(
    {
        "business-operations-admins",
        "business-operations-pack-admins",
        "business-operations-limit-owners",
        "business-operations-case-workers",
    }
)
LIMIT_SYMBOLS = frozenset({"AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"})
WRITES_PER_MINUTE = 30
DIFF_MAX = 20_000
READ, AUTHOR = "read", "author"

_STR = {"type": "string"}
_INT = {"type": "integer"}
_PACK_ID = {"type": "string", "description": "A pack id, as list_packs reports it."}
_PROCESS_TYPE = {
    "type": "string",
    "description": "A process type the pack maps, as list_sops reports it.",
}
_BASED_ON = {
    "type": "integer",
    "description": (
        "The pack version you read (`version` from get_pack, get_sop, list_sops or "
        "get_base_prompt). If the pack moved on since, nothing is written and the "
        "answer names the version it is at: read again and reapply."
    ),
}
#: Declared on every tool so it validates whether the Gateway checks a tool's
#: arguments before or after its interceptor sets it (design, open question 2).
CALLER = {
    "type": "object",
    "description": (
        "Set by the people Gateway from your sign-in. Do not send it: anything you "
        "send is replaced."
    ),
}


@dataclass(frozen=True)
class Tool:
    name: str
    tier: str
    method: str
    resource: str
    description: str
    required: dict = field(default_factory=dict)
    optional: dict = field(default_factory=dict)

    @property
    def write(self) -> bool:
        return self.tier == AUTHOR

    @property
    def handler(self) -> str:
        if self.resource in ("/registry", "/packs/{pack_id}/rules"):
            return "config"
        return "sops" if "/sops" in self.resource else "packs"

    def schema(self) -> dict:
        """The tool as the Gateway target lists it."""
        properties = {
            **{k: v for k, v in self.required.items()},
            **{k: v for k, v in self.optional.items()},
            "caller": CALLER,
        }
        hint = (
            "Writes a new pack version in a development stack, as you."
            if self.write
            else "Reads only."
        )
        return {
            "Name": self.name,
            "Description": f"{self.description} {hint}",
            "InputSchema": {
                "Type": "object",
                "Properties": {k: _cap(v) for k, v in properties.items()},
                "Required": sorted(self.required),
            },
        }


def _cap(schema: dict) -> dict:
    """A JSON schema in the Gateway's tool-schema key case, nested schemas included
    (CloudFormation refuses a lowercase `type` under `Items`). Property names under
    `properties` are the tool's own and keep their case."""
    out = {}
    for key, value in schema.items():
        cased = key[:1].upper() + key[1:]
        if key == "properties" and isinstance(value, dict):
            out[cased] = {name: _cap(sub) for name, sub in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[cased] = _cap(value)
        else:
            out[cased] = value
    return out


_CREATE = {
    "pack_id": _PACK_ID,
    "display_name": _STR,
    "description": _STR,
    "short_id": _STR,
    "kind": {"type": "string", "description": "filter, absence or aging."},
    "definition": _STR,
    "process_types": {"type": "array", "items": _STR},
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_packs",
        READ,
        "GET",
        "/registry",
        "Every pack you may see, with its SAP system and process types.",
    ),
    Tool(
        "get_pack",
        READ,
        "GET",
        "/packs/{pack_id}",
        "One pack's document by part, and its version.",
        {"pack_id": _PACK_ID},
    ),
    Tool(
        "list_sops",
        READ,
        "GET",
        "/packs/{pack_id}/sops",
        "One pack's SOPs, one row per process type.",
        {"pack_id": _PACK_ID},
    ),
    Tool(
        "get_sop",
        READ,
        "GET",
        "/packs/{pack_id}/sops/{process_type}",
        "One SOP's text, with the pack version you read it at.",
        {"pack_id": _PACK_ID, "process_type": _PROCESS_TYPE},
    ),
    Tool(
        "check_sop",
        READ,
        "POST",
        "/packs/{pack_id}/sops/{process_type}/check",
        "Lint a SOP's text without saving it: the findings put_sop refuses on.",
        {"pack_id": _PACK_ID, "process_type": _PROCESS_TYPE, "content": _STR},
    ),
    Tool(
        "get_base_prompt",
        READ,
        "GET",
        "/packs/{pack_id}/base_prompt",
        "One pack's agent instructions, which put_base_prompt replaces.",
        {"pack_id": _PACK_ID},
    ),
    Tool(
        "get_rules",
        READ,
        "GET",
        "/packs/{pack_id}/rules",
        "One pack's rule values with their bounds and the SOPs that cite them.",
        {"pack_id": _PACK_ID},
    ),
    Tool(
        "list_versions",
        READ,
        "GET",
        "/packs/{pack_id}/versions",
        "One pack's stored versions, newest first, with who made each and how.",
        {"pack_id": _PACK_ID},
        {"limit": _INT, "next_token": _STR},
    ),
    Tool(
        "get_version",
        READ,
        "GET",
        "/packs/{pack_id}/versions/{version}",
        "One stored version's document, to compare with another.",
        {"pack_id": _PACK_ID, "version": _INT},
    ),
    Tool(
        "put_sop",
        AUTHOR,
        "PUT",
        "/packs/{pack_id}/sops/{process_type}",
        "Replace the text of the SOP a process type maps. Refused, with the "
        "findings, if the lint finds anything; the answer carries the diff.",
        {
            "pack_id": _PACK_ID,
            "process_type": _PROCESS_TYPE,
            "content": _STR,
            "based_on": _BASED_ON,
        },
    ),
    Tool(
        "put_base_prompt",
        AUTHOR,
        "PATCH",
        "/packs/{pack_id}",
        "Replace a pack's agent instructions, display name or description.",
        {"pack_id": _PACK_ID, "based_on": _BASED_ON},
        {"base_prompt": _STR, "display_name": _STR, "description": _STR},
    ),
    Tool(
        "set_rule_values",
        AUTHOR,
        "PATCH",
        "/packs/{pack_id}",
        "Set rule values the pack declares, within their bounds. Never a write limit.",
        {
            "pack_id": _PACK_ID,
            "based_on": _BASED_ON,
            "values": {"type": "object", "description": "{SYMBOL: number}"},
        },
    ),
    Tool(
        "create_pack",
        AUTHOR,
        "POST",
        "/packs",
        "Create a pack from a spec. It is born inert: nothing polls it.",
        _CREATE,
        {
            "attributes": {"type": "array", "items": {"type": "object"}},
            "constants": {"type": "object"},
            "older_than_days": _INT,
        },
    ),
)
BY_NAME = {t.name: t for t in TOOLS}

_TYPES = {"string": str, "integer": int, "object": dict, "array": list}


class Refused(Exception):
    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.answer = {"status": status, "error": error, **extra}


def tiers() -> frozenset[str]:
    return frozenset(t for t in os.environ.get("PEOPLE_TIERS", "").split(",") if t)


def offered() -> tuple[Tool, ...]:
    """The tools this deployment attaches: those of its tiers."""
    return tuple(t for t in TOOLS if t.tier in tiers())


# ── gate 3 ──


def _tool(context) -> Tool:
    try:
        full = str(context.client_context.custom["bedrockAgentCoreToolName"])
    except (AttributeError, KeyError, TypeError):
        raise Refused(
            400, "No tool name: call this through the people Gateway"
        ) from None
    target, sep, name = full.partition("___")
    tool = BY_NAME.get(name)
    if not sep or target != TARGET or tool is None or tool.tier not in tiers():
        raise Refused(404, f"No such tool: {full[:120]}")
    return tool


def _caller(arguments: dict) -> dict:
    caller = arguments.get("caller")
    if not isinstance(caller, dict):
        raise Refused(401, "No caller: call this through the people Gateway")
    groups = caller.get("groups")
    for key in ("sub", "client_id"):
        if not isinstance(caller.get(key), str) or not caller[key]:
            raise Refused(401, "No caller: call this through the people Gateway")
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise Refused(401, "No caller: call this through the people Gateway")
    return caller


def _arguments(tool: Tool, arguments: dict) -> dict:
    given = {k: v for k, v in arguments.items() if k != "caller"}
    unknown = sorted(set(given) - set(tool.required) - set(tool.optional))
    if unknown:
        raise Refused(400, f"{tool.name} takes no {', '.join(unknown)}")
    missing = sorted(set(tool.required) - set(given))
    if missing:
        raise Refused(400, f"{tool.name} needs {', '.join(missing)}")
    for name, value in given.items():
        kind = _TYPES[(tool.required.get(name) or tool.optional[name])["type"]]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise Refused(400, f"`{name}` must be {kind.__name__}")
    limits = sorted(set(given.get("values") or {}) & LIMIT_SYMBOLS)
    if tool.name == "set_rule_values" and limits:
        raise Refused(
            400,
            f"{', '.join(limits)} is a write limit, which is not offered here",
            detail="Lower a limit in the console, as a limit owner.",
        )
    if tool.name == "put_base_prompt" and not (
        set(given) & {"base_prompt", "display_name", "description"}
    ):
        raise Refused(
            400, "put_base_prompt needs base_prompt, display_name or description"
        )
    return given


_writes: dict[str, deque] = defaultdict(deque)


def _rate(sub: str, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    recent = _writes[sub]
    while recent and now - recent[0] >= 60:
        recent.popleft()
    if len(recent) >= WRITES_PER_MINUTE:
        raise Refused(429, f"More than {WRITES_PER_MINUTE} writes in a minute; wait")
    recent.append(now)


# ── gate 4: the console's code ──


def _groups(caller: dict) -> list[str]:
    remote_authors = os.environ.get(REMOTE_AUTHORS_ENV, "")
    groups = [g for g in caller["groups"] if g not in STRIPPED_GROUPS]
    if not remote_authors or remote_authors not in groups:
        raise Refused(
            403,
            "Not a remote author",
            detail=f"Authoring from an external AI needs {remote_authors or 'a group'}"
            " on top of the console's own groups.",
        )
    return groups


_routes: dict[str, object] = {}


def _route(name: str):
    """The console's handler module for a route group, loaded once."""
    if name not in _routes:
        layer = HERE.parent / "layer" / "python"
        if layer.is_dir() and str(layer) not in sys.path:
            sys.path.append(str(layer))
        spec = importlib.util.spec_from_file_location(
            f"people_route_{name}", HERE.parent / name / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _routes[name] = mod
    return _routes[name]


def _event(tool: Tool, given: dict, caller: dict, groups: list[str]) -> dict:
    path = {
        k: str(given[k])
        for k in ("pack_id", "process_type", "version")
        if k in given and "{" + k + "}" in tool.resource
    }
    query = {k: str(given[k]) for k in ("limit", "next_token") if k in given}
    body = _body(tool, given)
    claims = {
        "sub": caller["sub"],
        "username": str(caller.get("username") or caller["sub"]),
        "client_id": caller["client_id"],
        "token_use": "access",
        "cognito:groups": groups,
    }
    resolved = tool.resource
    for k, v in path.items():
        resolved = resolved.replace("{" + k + "}", v)
    return {
        "httpMethod": tool.method,
        "resource": tool.resource,
        "path": resolved,
        "pathParameters": path or None,
        "queryStringParameters": query or None,
        "headers": {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {"claims": claims},
            "door": {
                "via": "people-gateway",
                "client": str(caller.get("client") or "unknown")[:256],
                "by": caller["sub"][:256],
                "display": claims["username"][:256],
            },
        },
    }


def _body(tool: Tool, given: dict) -> dict | None:
    if tool.name == "check_sop":
        return {"content": given["content"]}
    if tool.name == "put_sop":
        return {"content": given["content"], "expected_version": given["based_on"]}
    if tool.name == "put_base_prompt":
        pack = {k: given[k] for k in ("display_name", "description") if k in given}
        body = {"expected_version": given["based_on"]}
        if pack:
            body["pack"] = pack
        if "base_prompt" in given:
            body["base_prompt"] = given["base_prompt"]
        return body
    if tool.name == "set_rule_values":
        return {
            "pack": {"constants": given["values"]},
            "expected_version": given["based_on"],
        }
    if tool.name == "create_pack":
        return {
            "pack_id": given["pack_id"],
            "create": {k: v for k, v in given.items() if k != "pack_id"},
        }
    return None


def _run(tool: Tool, given: dict, caller: dict, groups: list[str]) -> dict:
    resp = _route(tool.handler).handler(_event(tool, given, caller, groups), None)
    try:
        body = json.loads(resp.get("body") or "null")
    except ValueError:
        body = None
    return {"status": int(resp.get("statusCode") or 500), "body": body}


def _put_sop(tool: Tool, given: dict, caller: dict, groups: list[str]) -> dict:
    """Lint, then write, then the diff against the text it replaced."""
    check = BY_NAME["check_sop"]
    linted = _run(check, {k: given[k] for k in check.required}, caller, groups)
    if linted["status"] >= 400:
        return linted
    findings = (linted["body"] or {}).get("findings") or []
    if findings:
        return {
            "status": 422,
            "body": {
                "error": "The SOP has findings; nothing was written",
                "findings": findings,
            },
        }
    read = BY_NAME["get_sop"]
    before = _run(read, {k: given[k] for k in read.required}, caller, groups)
    written = _run(tool, given, caller, groups)
    if written["status"] < 400 and isinstance(written["body"], dict):
        old = (
            ((before["body"] or {}).get("content") or "")
            if before["status"] < 400
            else ""
        )
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                given["content"].splitlines(keepends=True),
                f"v{given['based_on']}",
                "this write",
            )
        )
        written["body"]["diff"] = diff[:DIFF_MAX]
    return written


def _log(event: str, tool: str | None, caller: dict | None, given: dict, **extra):
    line = {
        "event": event,
        "sub": (caller or {}).get("sub"),
        "client": (caller or {}).get("client"),
        "tool": tool,
        "pack_id": given.get("pack_id")
        if isinstance(given.get("pack_id"), str)
        else None,
        "process_type": given.get("process_type")
        if isinstance(given.get("process_type"), str)
        else None,
        "based_on": given.get("based_on")
        if isinstance(given.get("based_on"), int)
        else None,
        **extra,
    }
    level = logging.INFO if event == "people_call" else logging.WARNING
    logger.log(level, json.dumps(line, default=str))


def handler(event, context):
    arguments = event if isinstance(event, dict) else {}
    tool = caller = None
    given: dict = {}
    try:
        tool = _tool(context)
        caller = _caller(arguments)
        given = _arguments(tool, arguments)
        if tool.write:
            _rate(caller["sub"])
        groups = _groups(caller)
    except Refused as refused:
        _log(
            "people_refused",
            tool.name if tool else None,
            caller,
            given,
            status=refused.answer["status"],
            reason=refused.answer["error"],
        )
        return refused.answer
    try:
        if tool.name == "put_sop":
            answer = _put_sop(tool, given, caller, groups)
        else:
            answer = _run(tool, given, caller, groups)
    except Exception as exc:  # noqa: BLE001 - a tool error, not a Lambda failure
        logger.error("people call failed: %s", type(exc).__name__)
        answer = {"status": 500, "body": {"error": "Internal server error"}}
    body = answer.get("body") if isinstance(answer.get("body"), dict) else {}
    _log(
        "people_call",
        tool.name,
        caller,
        given,
        version=body.get("version"),
        status=answer["status"],
    )
    return answer
