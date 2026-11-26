# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trigger mode API: the deployment's trigger mode for unattended SAP action, and each pack's own.

    GET PUT /deployment/trigger-mode        the deployment's trigger mode, and every visible
                                        pack's mode
    GET PUT /packs/{pack_id}/trigger-mode   one pack's mode

An unattended producer queues a case only when the deployment's trigger mode and the case's pack
both say `auto` (`pipeline/case_store.Gate`); an absent mode is `manual`, so a pack
nobody has set, a new one included, never runs alone. Containment and break glass set
the deployment's trigger mode, so either stops every pack at once.

While an emergency stop stands, neither `PUT` turns a mode to `auto` (a `409` naming the
stop); `manual` stays allowed, so a person can still turn a pack off.

Either `PUT` needs the admins group (enforced by `entitlement.admit`); `GET` stays open to
every user, so every operator can see whether the agent acts alone. A pack's `GET` needs
sight of the pack.

Each flip stamps the actor on the mode parameter's own version history (SSM keeps
a description per version) and mirrors the latest one into a sibling parameter that
`GET` returns. CloudTrail names only this Lambda's role as the caller.

An absent mode parameter reads as None; every reader of it treats that as `manual`.
"""

import logging
import os
import re
from datetime import UTC, datetime

import boto3
import emergency_stop
import registry
from api_http import HttpError, field, json_body, respond, route
from entitlement import CallerEntitlement, caller_identity

logger = logging.getLogger()

ssm = boto3.client("ssm")
#: `/<project>/<env>/use-cases/business-operations/autonomy`.
PREFIX = os.environ["TRIGGER_MODE_PREFIX"]
#: One pack's mode is `<PACK_PREFIX>/<pack_id>`, and who last set it
#: `<ACTOR_PREFIX>/<pack_id>` (names.trigger_mode_parameters).
PACK_PREFIX = os.environ.get("PACK_MODE_PREFIX", f"{PREFIX}/packs")
ACTOR_PREFIX = os.environ.get("PACK_ACTOR_PREFIX", f"{PREFIX}/pack-actors")
_PACK_ID = re.compile(r"[a-z][a-z0-9_]*")
#: GetParameters takes at most ten names a call.
_BATCH = 10

#: The one switch, and the values it takes.
MODE = "trigger-mode"
MODES = {"auto", "manual"}

#: Suffix of the sibling parameter that records who last flipped the mode, and when.
#: Suffixed rather than nested so a reader globbing `/<stack>/autonomy/*` for modes
#: cannot mistake it for one.
ACTOR_SUFFIX = "-last-changed-by"

#: Whether anything unattended reads the mode: false until the agent queue is
#: deployed, so a stored `auto` is reported as inert rather than as live writes.
AUTONOMOUS_CAPABLE = os.environ.get("AUTONOMOUS_CAPABLE") == "true"


@route(refresh=False)
def handler(event):
    method = event.get("httpMethod", "GET")
    resource = str(event.get("resource") or "")
    if resource.endswith("/packs/{pack_id}/trigger-mode"):
        pack_id = str((event.get("pathParameters") or {}).get("pack_id") or "")
        if method == "GET":
            return _get_pack(event, pack_id)
        if method == "PUT":
            val = _mode(json_body(event))
            _stop_gate(val)
            return _put_pack(event, pack_id, val)
        return respond(405, {"error": "Method not allowed"}, event)

    if method == "GET":
        # The actor rides with the mode, for the same reason `autonomous-capable` does:
        # a stored `auto` and who put it there are one fact to an operator. None on a
        # stack whose last flip predates the field, which is not the same as nobody.
        switch = _read(MODE)
        return respond(
            200,
            {
                MODE: switch,
                f"{MODE}{ACTOR_SUFFIX}": _read(f"{MODE}{ACTOR_SUFFIX}"),
                "autonomous-capable": AUTONOMOUS_CAPABLE,
                "packs": _pack_modes(event, switch),
            },
            event,
        )

    if method == "PUT":
        val = _mode(json_body(event))
        _stop_gate(val)
        actor = _actor(event)
        ssm.put_parameter(
            Name=f"{PREFIX}/{MODE}",
            Value=val,
            Type="String",
            Overwrite=True,
            # The audit record. SSM keeps a description per version and timestamps each
            # one, so parameter history *is* the log of who flipped unattended SAP action
            # on and when. An overwrite that omits a description *retains* the previous
            # version's (measured against SSM), which is why every writer of this
            # parameter has to pass one.
            Description=actor,
        )
        # After the mode, not before: a failed flip must not leave an actor recorded
        # against a change that never happened.
        _attribute(MODE, val, actor)
        return respond(200, {MODE: val}, event)

    return respond(405, {"error": "Method not allowed"}, event)


def _stop_gate(val: str) -> None:
    """No mode turns to `auto` while an emergency stop stands; `manual` always may."""
    if val == "auto" and (stop := emergency_stop.standing(ssm)):
        raise HttpError(
            409,
            emergency_stop.refusal(stop),
            emergency_stop=emergency_stop.public(stop),
        )


def _mode(body: dict) -> str:
    val = field(body, MODE)
    if val not in MODES:
        raise HttpError(400, f"{MODE} must be one of {sorted(MODES)}")
    return val


def _get_pack(event: dict, pack_id: str) -> dict:
    """One pack's mode, who set it, and what it means with the deployment's trigger mode."""
    if not _PACK_ID.fullmatch(pack_id):
        raise HttpError(400, "`pack_id` is not a pack id")
    registry.refresh_for_request()
    if pack_id not in registry.packs():
        raise HttpError(404, f"No published pack {pack_id!r}")
    if not CallerEntitlement.resolve(event).may_see(pack_id):
        raise HttpError(403, "Not entitled to this pack")
    switch = _read(MODE)
    found = _read_many([f"{PACK_PREFIX}/{pack_id}", f"{ACTOR_PREFIX}/{pack_id}"])
    mode = found.get(f"{PACK_PREFIX}/{pack_id}")
    on = AUTONOMOUS_CAPABLE and switch == "auto"
    return respond(
        200,
        {
            "pack_id": pack_id,
            MODE: mode,
            f"{MODE}{ACTOR_SUFFIX}": found.get(f"{ACTOR_PREFIX}/{pack_id}"),
            "effective": "auto" if on and mode == "auto" else "manual",
            "deployment": switch,
        },
        event,
    )


def _pack_modes(event: dict, switch: str | None) -> dict | None:
    """`{pack_id: {trigger-mode, trigger-mode-last-changed-by, effective}}` for every
    published pack the caller may see, or None when the registry or the parameters
    cannot be read: GET answers with the switch whatever happens to the packs."""
    try:
        registry.refresh_for_request()
        caller = CallerEntitlement.resolve(event)
        ids = sorted(p for p in registry.packs() if caller.may_see(p))
        modes = _read_many([f"{PACK_PREFIX}/{p}" for p in ids])
        actors = _read_many([f"{ACTOR_PREFIX}/{p}" for p in ids])
    except Exception:
        logger.exception("pack trigger modes unreadable")
        return None
    on = AUTONOMOUS_CAPABLE and switch == "auto"
    return {
        p: {
            MODE: modes.get(f"{PACK_PREFIX}/{p}"),
            f"{MODE}{ACTOR_SUFFIX}": actors.get(f"{ACTOR_PREFIX}/{p}"),
            "effective": "auto"
            if on and modes.get(f"{PACK_PREFIX}/{p}") == "auto"
            else "manual",
        }
        for p in ids
    }


def _read_many(names: list[str]) -> dict[str, str]:
    """`{name: value}` for the names that exist; an absent one is simply left out."""
    found: dict[str, str] = {}
    for i in range(0, len(names), _BATCH):
        reply = ssm.get_parameters(Names=names[i : i + _BATCH])
        found.update({p["Name"]: p["Value"] for p in reply.get("Parameters", [])})
    return found


def _put_pack(event: dict, pack_id: str, val: str) -> dict:
    """Set one pack's mode. Only a published pack: the route never writes a parameter
    for a name nothing routes to."""
    if not _PACK_ID.fullmatch(pack_id):
        raise HttpError(400, "`pack_id` is not a pack id")
    registry.refresh_for_request()
    if pack_id not in registry.packs():
        raise HttpError(404, f"No published pack {pack_id!r}")
    actor = _actor(event)
    ssm.put_parameter(
        Name=f"{PACK_PREFIX}/{pack_id}",
        Value=val,
        Type="String",
        Overwrite=True,
        # Each pack's own history is the log of who flipped it, as for the switch.
        Description=actor,
    )
    _record(f"{ACTOR_PREFIX}/{pack_id}", f"{pack_id} {MODE}", val, actor)
    return respond(200, {"pack_id": pack_id, MODE: val}, event)


def _read(param: str) -> str | None:
    """One `/<stack>/autonomy/` parameter, or None if it is absent or unreadable."""
    try:
        return ssm.get_parameter(Name=f"{PREFIX}/{param}")["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - absent or unreadable reads as None
        return None


def _actor(event) -> str:
    """Who flipped it, from the trusted authorizer context and never from the body.

    A caller-supplied actor would let anyone sign someone else's name to the switch that
    starts unattended SAP writes, so this reads the authorizer's claims and nothing else.

    Both identities, because each alone is a worse record: `display` is email-first so a
    person can read it back, and an email address in Cognito is mutable; `subject` is the
    immutable `sub`, which is what still resolves after a rename. The shared resolver
    rather than a local claims ladder — a case approval and a settings override stamp
    themselves with the same one, so the operator named here is the operator named there.
    """
    identity = caller_identity(event)
    display = identity.display or "unknown"
    return f"{display} ({identity.subject or 'no sub'})"


def _attribute(param: str, val: str, actor: str) -> None:
    """Record who set `param` to `val`, and when, where `GET /deployment/trigger-mode` can
    read it back.

    Two records of one fact, and neither is redundant. `Description=actor` on the mode
    parameter itself is the durable one: SSM versions and timestamps every write, so
    parameter history *is* the log of who flipped unattended SAP action on, for free and
    for every flip. But `get_parameter` does not return a description, and reading it
    back would need `DescribeParameters` — a new IAM action, list-scoped and paginated.
    So the latest flip is also mirrored into a sibling parameter under the same
    `/<stack>/autonomy/` prefix the route's policy already scopes: no new grant, and the
    GET serves it with a read it was making anyway.

    Best-effort by design: the mode is already stored by the time this runs, and failing
    the request here would report a flip that did happen as one that did not. An
    unwritten actor is logged, and an unattributed flip is this route's pre-existing
    behaviour.
    """
    _record(f"{PREFIX}/{param}{ACTOR_SUFFIX}", param, val, actor)


def _record(name: str, what: str, val: str, actor: str) -> None:
    """Write `<val> set by <actor> at <time>` to `name`; best-effort (`_attribute`)."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    record = f"{val} set by {actor} at {stamp}"
    # Logged whether or not the parameter write lands, so the Lambda's own log group
    # carries the attribution even when SSM refuses it.
    print(f"[TRIGGER MODE] {what}: {record}")
    try:
        ssm.put_parameter(Name=name, Value=record, Type="String", Overwrite=True)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - best-effort attribution
        print(f"[TRIGGER MODE] could not record the actor for {what}: {e}")
