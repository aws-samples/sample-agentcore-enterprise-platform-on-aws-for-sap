# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Whether an emergency stop stands, as every path that starts a run asks it.

`POST /deployment/stop` (`api/stop`) writes one SSM parameter, beside the deployment's
trigger mode, and only `POST /deployment/stop/clear` ends it. While it stands nothing new
starts: `POST /runs`, the enqueue, the decision routes, the producers' gate, the
operator re-run and the invoker all refuse, naming the stop.

Read uncached, so a stop holds from the next call. An absent parameter is no stop; one
that cannot be read or parsed stands, so a broken read never lets a run start through
a stop someone pulled.
"""

from __future__ import annotations

import json
import os

PARAMETER_ENV = "EMERGENCY_STOP_PARAMETER"
#: What a person reads back: who pulled it, when, why, and the clear once there is one.
PUBLIC_KEYS = (
    "stands",
    "stopped_at",
    "stopped_by",
    "reason",
    "cleared_at",
    "cleared_by",
    "clear_reason",
)
UNREADABLE = {
    "stands": True,
    "reason": "the emergency stop could not be read",
    "unreadable": True,
}

_ssm = None


def _client():
    global _ssm
    if _ssm is None:
        import boto3

        _ssm = boto3.client("ssm")
    return _ssm


def read(client=None) -> dict | None:
    """The stop's state, None when there has never been one or the parameter is unset."""
    name = os.environ.get(PARAMETER_ENV, "")
    if not name:
        return None
    try:
        raw = (client or _client()).get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as error:  # noqa: BLE001 - classified below
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        return None if code == "ParameterNotFound" else dict(UNREADABLE)
    try:
        state = json.loads(raw)
    except ValueError:
        return dict(UNREADABLE)
    return state if isinstance(state, dict) else dict(UNREADABLE)


def standing(client=None) -> dict | None:
    """The stop when it stands, else None. Only an explicit `stands: false` clears."""
    state = read(client)
    return state if state is not None and state.get("stands") is not False else None


def public(state: dict) -> dict:
    return {k: state[k] for k in PUBLIC_KEYS if k in state}


def refusal(state: dict) -> str:
    """The sentence every refusal carries."""
    who = ", ".join(str(state[k]) for k in ("stopped_by", "stopped_at") if state.get(k))
    because = str(state.get("reason") or "no reason recorded")
    return (
        f"An emergency stop stands: {because}"
        + (f" ({who})" if who else "")
        + ". Nothing starts until an admin clears it."
    )


def refusal_body(state: dict) -> dict:
    """The `409` body of an HTTP route: the sentence and the stop."""
    return {"error": refusal(state), "emergency_stop": public(state)}
