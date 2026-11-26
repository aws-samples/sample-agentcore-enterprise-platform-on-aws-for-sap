# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The seeder: manufacture one pack's exception in SAP. `ops.py seed <pack>` invokes it.

SAP only: nothing here writes a case. The poller finds the blocked document on its
next run, the same way it finds a real one (`ops.py poll <pack>` runs it at once).

**No code per pack.** The documents to post are the `seed.steps` of the pack as it is
published here, read from the registry, so a pack that arrives by import seeds with no
deploy; `seed_steps` renders and posts them through the SAP layer's write client. A
pack with no `seed.steps` is refused.

No HTTP route: it is invoked over IAM, so `lambda:InvokeFunction` on it is the gate. An
invoke carries no verified caller identity, and CloudTrail names the caller only on a
trail that logs this function's data events; the platform's trail logs management
events only. A route could not finish a chain inside API Gateway's 29 s,
and a browser's retry posted a second one. Every seed is logged with what SAP created,
since it does not pass the Gateway's run checks (business_operations/test_data.py says why).
`requested_by` is the caller's own account of who it is, logged beside that and never
trusted.

`{"pack_id", "read": {"key", "fields"}}` reads SAP's record of a seeded case back, for
the eval runner to grade SAP's end state: a GET, never a write. It reads `seed.sap_state`
when the pack declares one, the record a write would change (a Release clears the
supplier line's block, never the invoice header's), and otherwise the `source`
document by its key.

A blank `posting_date` is the latest date SAP has open for `seed.company_code`, read
through the same SAP connection (`sap_odata.open_posting_date`). That read goes through
ADT, which takes Basic only, so on a system whose `flow` is a token flow a blank
`posting_date` is refused before any SAP call and the operator sets one; `describe`
reports the `flow` so `ops.py seed --list` can say so first.

**Only a sandbox.** Before any SAP call, a pack whose system's `system_role` is not
`sandbox` is refused, whatever the event asks: the synth checked the checkout's packs,
and this catches one imported later that names another system. The stack grants no
other system's secret either. `{"pack_id", "describe": true}` reports the published
seed, the system and its system role after one GET of the first step's OData service
document, so the eval runner learns that SAP answers before it seeds anything.

**What it posted, recorded.** Every seed is written to the test data table before its
first SAP call and settled after its last: the inputs, the preset it started from, who
asked, what the pack's checker and this stack's limits say the case should end as
(`test_data_expectation.py`), the documents SAP created and the case id. This function
is the table's only writer, so the record, and the `test_data` mark the poller copies
from it onto the case it opens (`pipeline/case_store.py`), say what was really posted.
A seed whose record cannot be written posts nothing. `post_id` comes from the console's
route, which invokes this asynchronously and without retry; `ops.py seed` sends none and
one is minted here.
"""

import json
import logging
import os
import re
import urllib.parse
from decimal import Decimal

import boto3
import config_overrides
import registry
import sap_http
import sap_odata
import seed_steps
import test_data_expectation
import test_data_records
from botocore.exceptions import ClientError
from case_key import CaseKeyError, derive_case_id, to_case_key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: name -> {base_url, secret, client, system_role}: the systems this use case calls
#: directly, every one of them, so a pack resolves to its system by the poller's rule.
SAP_SYSTEMS = json.loads(os.environ.get("SAP_SYSTEMS", "{}"))
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
#: Where each seed is recorded; this function is its only writer.
TEST_DATA_TABLE = os.environ.get("TEST_DATA_TABLE", "")
#: The cases table, for the one write this function makes there: the `test_data` mark on
#: a case the poller or SAP's event opened before the seed was settled.
CASES_TABLE = os.environ.get("CASES_TABLE", "")
_PRESET_MAX = 200
_dynamodb = None


def _table(name: str | None = None):
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(name or TEST_DATA_TABLE)


def _stored(value):
    """A JSON value as DynamoDB takes it: floats as Decimal."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def _documents(result: dict) -> dict:
    """The document numbers SAP created, as the steps' `returns` name them."""
    return {
        k: v
        for k, v in result.items()
        if k not in ("inputs", "case", "case_id") and not k.endswith("_error")
    }


def system_of(pack: dict) -> str | None:
    """The poller's rule: the pack's `sap_system`, or the only system there is."""
    named = pack.get("sap_system")
    if named:
        return named if named in SAP_SYSTEMS else None
    return next(iter(SAP_SYSTEMS)) if len(SAP_SYSTEMS) == 1 else None


def _quoted(value) -> str:
    """An OData string literal's body: a quote doubled, never a way out of it."""
    return str(value).replace("'", "''")


def _read(sap: sap_http.SapSystem, entry: dict, read: dict) -> dict:
    """SAP's record of a seeded case, `fields` only."""
    source = entry.get("source") or {}
    key, fields = read.get("key"), read.get("fields")
    # The case's key names a field `name` when it sets one; SAP's predicate wants `field`.
    keyed = {k.get("name", k["field"]): k["field"] for k in source.get("key") or []}
    if not isinstance(key, dict) or sorted(key) != sorted(keyed):
        raise ValueError(f"read.key must name {sorted(keyed)}")
    if not isinstance(fields, list) or not all(
        isinstance(f, str) and _FIELD_RE.fullmatch(f) for f in fields
    ):
        raise ValueError("read.fields must be a list of field names")
    state = ((entry.get("pack") or {}).get("seed") or {}).get("sap_state")
    # The same names a step may post to, because each lands in the URL path.
    target = state or source
    if not (
        seed_steps.SERVICE_RE.fullmatch(str(target.get("service")))
        and seed_steps.ENTITY_RE.fullmatch(str(target.get("entity")))
    ):
        raise ValueError("the read-back service and entity must be OData names")
    if state:
        url = (
            f"{sap.base_url}{sap_odata.ODATA_ROOT}/{state['service']}/{state['entity']}"
        )
        where = seed_steps.render(
            state["filter"], {k: _quoted(v) for k, v in key.items()}
        )
        rows = (
            sap.get_json(
                url, {"$filter": where, "$select": ",".join(fields), "$top": "1"}
            )
            .get("d", {})
            .get("results")
            or []
        )
        if not rows:
            raise ValueError(f"SAP has no {state['entity']} where {where}")
        record = rows[0]
    else:
        predicate = ",".join(
            f"{field}='{_quoted(key[name])}'" for name, field in keyed.items()
        )
        url = (
            f"{sap.base_url}{sap_odata.ODATA_ROOT}/{source['service']}/"
            f"{source['entity']}({urllib.parse.quote(predicate, safe='=,')})"
        )
        record = sap.get_json(url, {"$select": ",".join(fields)}).get("d") or {}
    return {f: record.get(f) for f in fields}


def handler(event: object, context: object) -> dict:
    """`{pack_id, inputs, requested_by}` -> `{ok, ...what landed}`, or `{ok: false, error}`."""
    event = event if isinstance(event, dict) else {}
    pack_id = event.get("pack_id")
    registry.refresh_for_request()
    entry = registry.packs().get(pack_id) if isinstance(pack_id, str) else None
    pack = (entry or {}).get("pack") or {}
    seed = pack.get("seed") or {}
    if not seed.get("steps"):
        return {
            "ok": False,
            "error": f"Pack {pack_id!r} is not published with seed.steps",
        }
    name = system_of(pack)
    if name is None:
        return {
            "ok": False,
            "error": f"No SAP system this seeder reaches for {pack_id}",
        }
    where = {"system": name, "system_role": SAP_SYSTEMS[name].get("system_role")}
    if where["system_role"] != "sandbox":
        return {
            "ok": False,
            **where,
            "error": f"{pack_id} resolves to SAP system {name}, whose system role is "
            f"{where['system_role']}; the seeder seeds and reads only a sandbox",
        }
    try:
        sap = sap_http.system(name, SAP_SYSTEMS[name])
    except Exception:
        logger.exception("No SAP system for %s", pack_id)
        return {"ok": False, "error": f"Could not read the SAP system {name}"}

    if event.get("describe"):
        # What a seed of the published pack would post, for the operator to confirm:
        # never the checkout's copy, which may be another version. One GET first, so
        # a SAP that does not answer fails here and not halfway through a chain.
        service = str(seed["steps"][0].get("service"))
        if not seed_steps.SERVICE_RE.fullmatch(service):
            return {
                "ok": False,
                **where,
                "error": "the first step's service is not an OData name",
            }
        try:
            sap.get_json(f"{sap.base_url}{sap_odata.ODATA_ROOT}/{service}/")
        except sap_http.SapError as e:
            return {"ok": False, **where, "error": f"SAP did not answer: {e}"}
        flow = SAP_SYSTEMS[name].get("flow", "basic")
        return {"ok": True, "pack_id": pack_id, **where, "flow": flow, "seed": seed}

    if "read" in event:
        try:
            return {"ok": True, "record": _read(sap, entry, event["read"])}
        except (ValueError, sap_http.SapError) as e:
            return {"ok": False, "error": str(e)}

    given = {} if event.get("inputs") is None else event["inputs"]
    if not isinstance(given, dict):
        return {"ok": False, "error": "inputs must be an object"}
    post_id = event.get("post_id") or test_data_records.new_post_id()
    if not isinstance(post_id, str) or not test_data_records.POST_ID_RE.fullmatch(
        post_id
    ):
        return {"ok": False, "error": "post_id is not one this seeder mints"}
    try:
        values = seed_steps.inputs(
            seed,
            given,
            lambda: sap_odata.open_posting_date(sap, str(seed.get("company_code"))),
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    who = str(event.get("requested_by") or "(not given)")[:256]
    preset = event.get("preset")
    record = {
        "pack_id": pack_id,
        "post_id": post_id,
        "posted_at": post_id.split("#", 1)[0],
        "posted_by": who,
        "inputs": values,
        "expected": test_data_expectation.expect(
            pack_id, entry, values, config_overrides.constant_overrides(pack_id)
        ),
        "system": name,
        "status": "posting",
        **({"preset": preset[:_PRESET_MAX]} if isinstance(preset, str) else {}),
    }
    if TEST_DATA_TABLE:
        try:
            _table().put_item(
                Item=_stored(record),
                ConditionExpression="attribute_not_exists(post_id)",
            )
        except Exception:
            # Nothing is posted that the page and the case mark could not account for.
            logger.exception("Could not record the seed of %s as %s", pack_id, post_id)
            return {
                "ok": False,
                "post_id": post_id,
                "error": "Could not record the seed",
            }
    result = seed_steps.run(
        seed,
        values,
        lambda service, entity, body: sap_odata.post(sap, service, entity, body),
    )
    if case := result.get("case"):
        try:
            short_id = (entry.get("source") or {}).get("short_id")
            case["case_id"] = derive_case_id(pack_id, short_id, case["key"])
        except CaseKeyError as e:
            result["case_error"] = str(e)
    failed = sorted(k for k in result if k.endswith("_error"))
    logger.info(
        "Seeded %s on %s for %s as %s: %s",
        pack_id,
        name,
        who,
        post_id,
        json.dumps(result, default=str),
    )
    if TEST_DATA_TABLE:
        _settle(record, result, failed)
    return {
        "ok": not failed,
        **({"error": result[failed[0]]} if failed else {}),
        "pack_id": pack_id,
        "post_id": post_id,
        "system": name,
        **result,
    }


def _settle(record: dict, result: dict, failed: list[str]) -> None:
    """Write what SAP created onto the seed's record. A failure is logged and leaves it
    `posting`: the documents are already in SAP, and the log line above names them."""
    case_id = (result.get("case") or {}).get("case_id")
    settled = {
        **record,
        "status": "failed" if failed else "posted",
        "documents": _documents(result),
        **({"error": str(result[failed[0]])[:500]} if failed else {}),
        **({"case_id": case_id} if case_id else {}),
    }
    try:
        _table().put_item(Item=_stored(settled))
    except Exception:
        logger.exception("Could not settle the seed record %s", record["post_id"])
        return
    if case_id and CASES_TABLE:
        _mark_case(case_id, settled)


def _mark_case(case_id: str, record: dict) -> None:
    """Copy the mark onto the case, if it is already open and carries none.

    SAP's event can open the case before the chain is settled here, and then
    `put_case` found no record to copy. A case not open yet is left to `put_case`.
    """
    try:
        _table(CASES_TABLE).update_item(
            Key=to_case_key(case_id),
            UpdateExpression="SET test_data = :m",
            ConditionExpression="attribute_exists(case_id) AND attribute_not_exists(test_data)",
            ExpressionAttributeValues={":m": _stored(test_data_records.mark(record))},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.exception("Could not mark case %s as test data", case_id)
    except Exception:
        logger.exception("Could not mark case %s as test data", case_id)
