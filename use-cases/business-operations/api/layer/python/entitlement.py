# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Who is calling, may they use this API at all, and which packs may they act on.

Every route behind the API's
Cognito authorizer calls :func:`admit` first, then resolves a
:class:`CallerEntitlement` for the pack-level questions.

**Declare to allow.** An earlier version opened a pack that declared no groups to every
signed-in user. On a shared user pool that is every user any tenant, the platform or
a federated IdP creates, so here nothing is open by default:

* :func:`admit` refuses a token from any client but this API's own, and any caller
  not in the base users group (``USERS_GROUP``). A route in :data:`ADMIN_ROUTES`
  also needs the admins group (``ADMINS_GROUP``). An unset group refuses everyone.
* A pack's ``entitlement`` keys (``groups``, ``approve_groups``, ``author_groups``,
  ``constant_groups``, ``limit_groups``, ``process_groups``) restrict on top of the base group, each on its
  own key. A pack
  the published registry does not know is refused: "I cannot prove you may" never
  resolves to "go ahead".

The map comes from ``registry.json`` in the registry bucket (``registry.py``). An
unreadable registry raises :class:`registry.RegistryUnavailable`, which `api_http.route`
turns into a CORS-bearing 503, never into an open floor.
"""

from __future__ import annotations

import json
import os
import re
from typing import NamedTuple

import registry

#: Explicit override, read before the registry: a test or a break-glass pins a map here.
ENV_VAR = "ENTITLEMENT_MAP"
#: The groups the stack creates, passed in by name.
USERS_GROUP_ENV = "USERS_GROUP"
ADMINS_GROUP_ENV = "ADMINS_GROUP"
#: The deployment-wide authoring floor: closed when empty.
AUTHOR_FLOOR_ENV = "SOP_AUTHOR_GROUPS"
#: Who may lower a pack's write limits in this stack: closed when empty. Its own group,
#: not the authors', so a customer can map it apart (`identity.group_map`).
LIMIT_OWNERS_ENV = "LIMIT_OWNER_GROUPS"
#: Who may start the agent on a case to work it: closed when empty. Apart from the
#: approvers, so a customer maps case workers and approvers to different IdP groups.
CASE_WORKERS_ENV = "CASE_WORKER_GROUPS"
#: The claims carrying the caller's groups, comma-separated. Unset: Cognito's (and the
#: run channel's lifted `groups`). Under the platform's direct mode the stack sets
#: `roles`, Entra's app-role claim, and only that.
GROUP_CLAIMS_ENV = "GROUP_CLAIMS"
_DEFAULT_GROUP_CLAIMS = ("groups", "cognito:groups")
#: This API's own OAuth clients. The Cognito authorizer accepts a token from ANY
#: client on the shared pool, so this is the only client check there is.
API_CLIENTS_ENV = "API_ALLOWED_CLIENTS"

#: (method, API Gateway resource path) -> needs the admins group as well.
ADMIN_ROUTES = frozenset(
    {
        # Turns unattended SAP action on or off for every pack, or for one.
        ("PUT", "/deployment/trigger-mode"),
        ("PUT", "/packs/{pack_id}/trigger-mode"),
        # Turns it off and stops every run under way (the emergency stop).
        ("POST", "/deployment/stop"),
        # Lets runs start again after one.
        ("POST", "/deployment/stop/clear"),
    }
)

#: A JSON array, Cognito's bracketed `[a b]` rendering of `cognito:groups`, or CSV.
#: Cognito accepts a comma in a group name, so never name a group `AP,EMEA`: a
#: fragment matching an entitled group would be an escalation.
_SPLIT = re.compile(r"[,\s]+")
_STRIP = "\"' "


def _parse_groups(raw: object) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(g).strip() for g in raw if str(g).strip())
    text = str(raw).strip()
    if not text:
        return frozenset()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            return frozenset(str(g).strip() for g in decoded if str(g).strip())
        text = text[1:-1] if text.endswith("]") else text[1:]
    return frozenset(
        stripped for part in _SPLIT.split(text) if (stripped := part.strip(_STRIP))
    )


def author_floor() -> frozenset[str]:
    """Groups this deployment lets author anything at all. Empty refuses everyone."""
    return _parse_groups(os.environ.get(AUTHOR_FLOOR_ENV, ""))


def limit_owners() -> frozenset[str]:
    """Groups this deployment lets lower a pack's write limits. Empty refuses everyone."""
    return _parse_groups(os.environ.get(LIMIT_OWNERS_ENV, ""))


def case_workers() -> frozenset[str]:
    """Groups this deployment lets start the agent on a case. Empty refuses everyone."""
    return _parse_groups(os.environ.get(CASE_WORKERS_ENV, ""))


def caller_claims(event: dict) -> dict:
    """The authorizer's validated claims. Never a header or a body."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = authorizer.get("claims") or authorizer
    return claims if isinstance(claims, dict) else {}


def caller_groups(event: dict) -> frozenset[str]:
    claims = caller_claims(event)
    names = [c for c in os.environ.get(GROUP_CLAIMS_ENV, "").split(",") if c]
    out: frozenset[str] = frozenset()
    for name in names or _DEFAULT_GROUP_CLAIMS:
        out |= _parse_groups(claims.get(name))
    return out


#: An id token names its client in `aud`, a Cognito access token in `client_id`, an
#: Entra access token in `azp` (its `aud` is the platform app, which every client shares).
_CLIENT_CLAIMS = ("aud", "client_id", "azp")


def caller_client_allowed(event: dict) -> bool:
    """Whether the token was minted by one of this API's own clients.

    Fails closed: no client claim, or no allowlist, is a refusal. (An earlier version let a
    claim-less token through for its external-IdP authorizer, which this one does not
    have.)
    """
    claims = caller_claims(event)
    presented: set[str] = set()
    for key in _CLIENT_CLAIMS:
        presented |= _parse_groups(claims.get(key))
    allowed = _parse_groups(os.environ.get(API_CLIENTS_ENV, ""))
    return bool(presented and allowed and presented & allowed)


FORBIDDEN_CLIENT = {"error": "Token was not issued for this application"}
NOT_A_USER = {"error": "Not a Business Operations user"}
NOT_AN_ADMIN = {"error": "This needs a Business Operations administrator"}


def admit(event: dict) -> dict | None:
    """None if this caller may reach this route, else the 403 body to return.

    The first call in every handler. One table, so a route's group requirement is
    stated once and tested once (:data:`ADMIN_ROUTES`).
    """
    if not caller_client_allowed(event):
        return FORBIDDEN_CLIENT
    groups = caller_groups(event)
    users = os.environ.get(USERS_GROUP_ENV, "")
    if not users or users not in groups:
        return NOT_A_USER
    route = (event.get("httpMethod", ""), event.get("resource", ""))
    if route in ADMIN_ROUTES and not is_admin(groups):
        return NOT_AN_ADMIN
    return None


def is_admin(groups) -> bool:
    """In the admins group (``ADMINS_GROUP``), which an :data:`ADMIN_ROUTES` route needs.
    An unset group admits no one."""
    admins = os.environ.get(ADMINS_GROUP_ENV, "")
    return bool(admins and admins in groups)


_DISPLAY_CLAIMS = ("email", "preferred_username", "cognito:username", "username", "sub")


class CallerIdentity(NamedTuple):
    """`display` is for a person to read (email first); `subject` is the immutable
    OIDC `sub`, the identity of record. `subject` is "" when absent."""

    display: str | None
    subject: str


#: The one door besides the console: the platform's people Gateway, through this use
#: case's people target (`api/people/index.py`).
PEOPLE_DOOR = "people-gateway"
_DOOR_KEYS = ("via", "client", "by", "display")


def caller_door(event: dict) -> dict | None:
    """`{via, client, by, display}` when the people target made this request, else None.

    Read from `requestContext.door`, which API Gateway never sets and a browser cannot
    reach (API Gateway builds `requestContext` itself); only the people target builds an
    event carrying it. Anything but the exact shape is no door.
    """
    door = (event.get("requestContext") or {}).get("door")
    if not isinstance(door, dict) or door.get("via") != PEOPLE_DOOR:
        return None
    if set(door) != set(_DOOR_KEYS):
        return None
    if not all(
        isinstance(door[k], str) and 0 < len(door[k]) <= 256 for k in _DOOR_KEYS
    ):
        return None
    return {k: door[k] for k in _DOOR_KEYS}


def caller_identity(event: dict) -> CallerIdentity:
    claims = caller_claims(event)
    return CallerIdentity(
        display=next(
            (
                str(value).strip()[:256]
                for key in _DISPLAY_CLAIMS
                if (value := claims.get(key))
            ),
            None,
        ),
        subject=str(claims.get("sub") or ""),
    )


def load_map(raw: str | None = None) -> dict[str, dict]:
    """The entitlement map: ``ENTITLEMENT_MAP`` if set, else the published registry.

    A malformed explicit map raises rather than degrading to an empty one.
    """
    text = os.environ.get(ENV_VAR, "") if raw is None else raw
    if not text:
        return registry.entitlement_map()
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise registry.RegistryUnavailable(
            f"{ENV_VAR} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(v, dict) for v in parsed.values()
    ):
        raise registry.RegistryUnavailable(f"{ENV_VAR} must map pack ids to objects")
    return {str(pack_id): entry for pack_id, entry in parsed.items()}


class CallerEntitlement:
    """One caller's answers to "may I see / approve / author / tune this pack".

    Each gate needs the pack to be published and, where the pack declares the gate's
    key, a group in it. Visibility comes first for every write gate: a caller who may
    not see a pack's cases never approves or authors in it.
    """

    __slots__ = ("_groups", "_map")

    def __init__(self, groups: frozenset[str], entitlement_map: dict[str, dict]):
        self._groups = groups
        self._map = entitlement_map

    @classmethod
    def resolve(cls, event: dict) -> CallerEntitlement:
        return cls(caller_groups(event), load_map())

    @property
    def visible_packs(self) -> frozenset[str]:
        """Every published pack this caller may read. Empty is a real answer: callers
        turn it into a 403, never into an empty list that reads as a quiet day."""
        return frozenset(p for p in self._map if self.may_see(p))

    def may_see(self, pack_id: object) -> bool:
        return self._permits(pack_id, "groups")

    def may_approve(self, pack_id: object) -> bool:
        return self.may_see(pack_id) and self._permits(pack_id, "approve_groups")

    def may_author(self, pack_id: object) -> bool:
        return self.may_see(pack_id) and self._permits(pack_id, "author_groups")

    def may_set_constants(self, pack_id: object) -> bool:
        return self.may_see(pack_id) and self._permits(pack_id, "constant_groups")

    def may_lower_limits(self, pack_id: object) -> bool:
        """`PUT /packs/{pack_id}/controls`: a limit owner, then the pack's
        ``limit_groups`` where it declares them. Closed when the group is unset."""
        owners = limit_owners()
        return (
            bool(owners and self._groups & owners)
            and self.may_see(pack_id)
            and self._permits(pack_id, "limit_groups")
        )

    def may_process(self, pack_id: object) -> bool:
        """Process or Start runs on a case: a case worker, then the pack's
        ``process_groups`` where it declares them. Closed when the group is unset.
        A run passes a SAP write under the pack's limit for whoever started it."""
        workers = case_workers()
        return (
            bool(workers and self._groups & workers)
            and self.may_see(pack_id)
            and self._permits(pack_id, "process_groups")
        )

    def clears_author_floor(self) -> bool:
        floor = author_floor()
        return bool(floor and self._groups & floor)

    def _permits(self, pack_id: object, key: str) -> bool:
        entry = self._map.get(str(pack_id or ""))
        if entry is None:
            return False  # not published: refused
        required = entry.get(key) or []
        return not required or bool(self._groups.intersection(str(g) for g in required))


CASE_ALLOWED = "allowed"
CASE_MISSING = "missing"
CASE_FORBIDDEN = "forbidden"
CASE_NOT_A_CASE_WORKER = "not_a_case_worker"


def case_pack_verdict(
    table, case_key: dict, caller: CallerEntitlement, *, drive: bool = False
) -> str:
    """May ``caller`` act on one stored case? Decided by the case's own pack.

    ``drive`` asks whether the caller may start the agent on it to work it. The run checks
    pass an unapproved SAP write at or under the pack's limit for whoever started the
    run, so starting one takes a case worker (:meth:`CallerEntitlement.may_process`):
    seeing a pack is read-only.

    Missing before forbidden: an unentitled caller who guesses a real case id learns
    it exists. Accepted: case ids are document-derived, and a 404
    for a case that exists is a lie the collection routes may not tell either.
    """
    item = table.get_item(
        Key=case_key, ProjectionExpression="pack_id", ConsistentRead=True
    ).get("Item")
    if not item:
        return CASE_MISSING
    pack_id = item.get("pack_id")
    if not caller.may_see(pack_id):
        return CASE_FORBIDDEN
    if drive and not caller.may_process(pack_id):
        return CASE_NOT_A_CASE_WORKER
    return CASE_ALLOWED
