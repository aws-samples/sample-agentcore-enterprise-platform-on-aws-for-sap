# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Test data from the console: a pack's seed recipe, its expected outcomes, and posts.

    GET  /packs/{pack_id}/test-data              the recipe, each preset's expectation,
                                                 and what was posted, newest first
    POST /packs/{pack_id}/test-data/expectation  the expectation for typed values
    POST /packs/{pack_id}/test-data              post the chain, or copies of it

Mounted only with `test_data.enabled`, which is refused under `deployment.mode:
production`. Every call needs an operator (`business-operations-admins`) who may see the
pack. A post also needs the pack's SAP system to be a `sandbox` the seeder reaches; the
seeder checks that again before its first SAP call, and holds IAM on no other system.

**A post makes real SAP documents**, outside the Gateway and the run checks (the seeder's
module says why), so this route never posts itself. It checks the inputs against the
seed's declaration, then hands each copy to the seeder asynchronously, because a chain
outlasts an API request, and with no retry, because a retried chain is a second set of
documents. The seeder records each post before it posts (`test_data_records.py`); this
function only reads those records and the cases they made.
"""

import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import config_overrides
import entitlement
import registry
import seed_steps
import test_data_expectation
import test_data_records
from api_http import HttpError, field, json_body, respond, route
from case_key import CaseKeyError, to_case_key
from entitlement import CallerEntitlement, caller_groups, caller_identity

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SEEDER_FUNCTION_NAME = os.environ.get("SEEDER_FUNCTION_NAME", "")
TEST_DATA_TABLE = os.environ.get("TEST_DATA_TABLE", "")
CASES_TABLE = os.environ.get("CASES_TABLE", "")
#: `{system: system_role}` for the systems the seeder reaches, by the poller's rule.
SEED_SYSTEMS = json.loads(os.environ.get("SEED_SYSTEMS") or "{}")
MAX_COPIES = 5
_PRESET_MAX = 200

lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")


def _pack_system(document: dict) -> str | None:
    named = document.get("sap_system")
    if named:
        return named if named in SEED_SYSTEMS else None
    return next(iter(SEED_SYSTEMS)) if len(SEED_SYSTEMS) == 1 else None


def _post_refusal(document: dict) -> str | None:
    """Why this pack may not be posted to here, or None."""
    name = _pack_system(document)
    if name is None:
        return "the pack resolves to no SAP system the seeder reaches"
    if SEED_SYSTEMS[name] != "sandbox":
        return f"its SAP system {name} is {SEED_SYSTEMS[name]}, not a sandbox"
    return None


def _values(seed: dict, given: object) -> dict:
    """The seed's inputs with `given` laid over them, or the 400. A blank posting date is
    left for the seeder, which reads SAP's open period; here it is only a placeholder."""
    if not isinstance(given, dict):
        raise HttpError(400, "inputs must be an object")
    today = datetime.now(UTC).date().isoformat()
    try:
        return seed_steps.inputs(seed, given, lambda: today)
    except ValueError as error:
        raise HttpError(400, "Invalid inputs", details=[str(error)]) from None


def _expected(pack_id: str, entry: dict, values: dict) -> dict | None:
    return test_data_expectation.expect(
        pack_id, entry, values, config_overrides.constant_overrides(pack_id)
    )


def _number(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def _plain(value):
    """A DynamoDB item as JSON: Decimals as the numbers they were written as."""
    return json.loads(json.dumps(value, default=_number))


def _cases(case_ids: list[str]) -> dict[str, dict]:
    """`{case_id: {status, key}}` for the cases the posts made, as far as they exist."""
    keys = []
    for case_id in dict.fromkeys(case_ids):
        try:
            keys.append(to_case_key(case_id))
        except CaseKeyError:
            continue
    if not keys:
        return {}
    response = dynamodb.batch_get_item(
        RequestItems={
            CASES_TABLE: {
                "Keys": keys,
                "ProjectionExpression": "case_id, #s, #k",
                "ExpressionAttributeNames": {"#s": "status", "#k": "key"},
            }
        }
    )
    # An unprocessed key reads as a case not open yet; the next refresh shows it.
    return {
        item["case_id"]: {"status": item.get("status"), "key": item.get("key") or {}}
        for item in response.get("Responses", {}).get(CASES_TABLE, [])
    }


def _posts(pack_id: str) -> list[dict]:
    records = test_data_records.for_pack(dynamodb.Table(TEST_DATA_TABLE), pack_id)
    cases = _cases([r["case_id"] for r in records if r.get("case_id")])
    rows = []
    for record in records:
        case = cases.get(record.get("case_id", ""))
        expected = record.get("expected")
        rows.append(
            {
                **{
                    k: record.get(k)
                    for k in (
                        "post_id",
                        "posted_at",
                        "posted_by",
                        "preset",
                        "inputs",
                        "expected",
                        "status",
                        "error",
                        "documents",
                        "case_id",
                    )
                },
                "case": case,
                "result": test_data_expectation.result(
                    expected, (case or {}).get("status")
                ),
            }
        )
    return _plain(rows)


def _page(pack_id: str, entry: dict) -> dict:
    document = entry.get("pack") or {}
    seed = document.get("seed") or {}
    name = _pack_system(document)
    refusal = _post_refusal(document)
    presets = []
    for preset in seed.get("presets") or []:
        try:
            values = _values(seed, preset.get("values") or {})
        except HttpError:
            # A preset the seed's own declaration refuses: shown, with no expectation.
            presets.append({**preset, "expected": None})
            continue
        presets.append({**preset, "expected": _expected(pack_id, entry, values)})
    return {
        "pack_id": pack_id,
        "sap_system": {"name": name, "system_role": SEED_SYSTEMS[name]}
        if name
        else None,
        "may_post": refusal is None,
        "refusal": refusal,
        "label": seed.get("label"),
        "summary": seed.get("summary"),
        "company_code": seed.get("company_code"),
        "steps": [
            {
                k: step[k]
                for k in ("id", "label", "service", "entity", "when")
                if k in step
            }
            for step in seed.get("steps") or []
        ],
        "inputs": seed.get("inputs") or [],
        "presets": presets,
        "posts": _posts(pack_id),
    }


def _post(event: dict, pack_id: str, document: dict) -> dict:
    body = json_body(event)
    seed = document.get("seed") or {}
    given = body.get("inputs") if "inputs" in body else {}
    _values(seed, given)
    copies = body.get("copies", 1)
    if isinstance(copies, bool) or not isinstance(copies, int):
        raise HttpError(400, "copies must be a whole number")
    if not 1 <= copies <= MAX_COPIES:
        raise HttpError(400, f"copies must be from 1 to {MAX_COPIES}")
    preset = field(body, "preset")
    if refusal := _post_refusal(document):
        raise HttpError(409, f"Test data posts only to a sandbox: {refusal}")
    if not SEEDER_FUNCTION_NAME:
        raise HttpError(503, "The seeder is not deployed")
    who = caller_identity(event).display or "unknown"
    posts = []
    for _ in range(copies):
        post_id = test_data_records.new_post_id()
        lambda_client.invoke(
            FunctionName=SEEDER_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "pack_id": pack_id,
                    "inputs": given,
                    "post_id": post_id,
                    # From the verified token, never from the body.
                    "requested_by": who,
                    **({"preset": preset[:_PRESET_MAX]} if preset else {}),
                }
            ).encode(),
        )
        logger.info(f"[TEST DATA] {who} asked for {pack_id} post {post_id}")
        posts.append({"post_id": post_id, "status": "posting"})
    return {"posts": posts}


@route
def handler(event):
    resource = str(event.get("resource") or "")
    method = event.get("httpMethod", "GET")
    pack_id = str((event.get("pathParameters") or {}).get("pack_id") or "")
    caller = CallerEntitlement.resolve(event)
    if not entitlement.is_admin(caller_groups(event)):
        raise HttpError(403, "Only an operator may use test data")
    if not caller.may_see(pack_id):
        raise HttpError(403, "Not entitled to this pack")
    entry = registry.packs().get(pack_id) or {}
    document = entry.get("pack") or {}
    if not (document.get("seed") or {}).get("steps"):
        raise HttpError(404, f"{pack_id} has no test data recipe (seed.steps)")

    if resource.endswith("/expectation") and method == "POST":
        values = _values(document["seed"], json_body(event).get("inputs") or {})
        return respond(200, {"expected": _expected(pack_id, entry, values)}, event)
    if resource.endswith("/test-data") and method == "GET":
        return respond(200, _page(pack_id, entry), event)
    if resource.endswith("/test-data") and method == "POST":
        return respond(202, _post(event, pack_id, document), event)
    return respond(405, {"error": "Method not allowed"}, event)
