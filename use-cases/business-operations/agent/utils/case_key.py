# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Case identity: the one place a `case_id` is built or read in the agent image.

A case is keyed in DynamoDB by ``case_id``, its sole partition key, and identified
the same way everywhere else: SQS message bodies and `MessageGroupId`, ticket
correlation, agent prompts, URLs, trace records, and AgentCore session ids::

    {short}-{base32(sha256(pack_id, natural key))[:26]}    e.g. ap-k7m2q9x4rt6bw8ncf3jhd5gzpv

``short`` is the pack's two-to-eight character code (``short_id`` in the ``source``
block of ``packs/*/pack.json``) so an id read off a screen still says which process
it belongs to. Everything after it is a digest of the case's natural key, which
buys the two properties every consumer depends on:

  * **Any key shape.** The natural key is a map of SAP field to value of *any*
    arity, and the values may hold characters (``-``, ``/``, spaces) that no id
    charset would accept. A digest flattens all of it.
  * **Escaping-free, and bounded.** The id is safe as-is in a URL path or query
    string, in an SQS ``MessageGroupId``, in an AgentCore Memory
    ``actor_id``/``session_id``, inside a JSON string literal, and as an AppSync
    Events channel segment. That last consumer is the narrowest and it is the
    service's grammar, not ours: ``[A-Za-z0-9-]{1,50}`` (``utils/run_channel.py``). A
    minted id is at most 35 characters of ``[a-z0-9-]``, so it clears all five by
    construction rather than by a validation the caller might skip.

The digest covers ``pack_id`` as well as the key, so two packs that happen to key on
the same values can never collide even if their ``short`` codes clash. Renaming a
pack's ``pack_id`` therefore re-mints its ids; treat both fields as identity.

What the digest costs is readability, which three things pay back: the record
carries ``key`` (the natural key it was derived from) and ``title``; log lines print
both, via :func:`describe_case`; and ``GET /cases?key=<pack_id>:<json>`` resolves a
natural key back to a case.

One file, two artifacts: the API layer's ``case_key.py`` is a symlink to this one.
``frontend/src/lib/caseKey.ts`` reads and validates ids but never mints one; both are
held to ``types/case-id-vectors.json``. The patterns come from the schemas, through the
generated ``contracts.py``.
"""

import hashlib
import re
from base64 import b32encode
from collections.abc import Mapping

try:  # a package in the agent image, a top-level module in the Lambda layer
    from .contracts import CASE_ID_PATTERN, SHORT_ID_PATTERN
except ImportError:
    from contracts import CASE_ID_PATTERN, SHORT_ID_PATTERN

#: Between the short id and the digest.
SEPARATOR = "-"

#: Digest characters kept in a minted id: 26 base32 chars is 130 bits, which puts a
#: collision beyond reach while leaving the whole id inside every consumer's limit.
DIGEST_LENGTH = 26

#: Anchored pattern for a ``case_id``: what :func:`derive_case_id` mints, and the only
#: form any reader accepts.
DERIVED_CASE_ID_PATTERN = CASE_ID_PATTERN

_SHORT_ID_RE = re.compile(SHORT_ID_PATTERN)
_DERIVED_CASE_ID_RE = re.compile(DERIVED_CASE_ID_PATTERN)

#: Field name charset for a natural key. SAP field names are already this shape, and
#: keeping the ``=`` and the delimiter below out of a name is what makes the digest
#: input unambiguous.
_KEY_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: Delimiter between the parts of the digest input. ASCII unit separator: it cannot
#: appear in an SAP field value, so no pair of distinct keys can serialize alike.
_KEY_DELIMITER = "\x1f"

#: AgentCore Runtime rejects a session id shorter than this.
RUNTIME_SESSION_MIN_LENGTH = 33

#: Namespace every case-derived session id carries. Public because recognizing one
#: matters as much as minting one: a route that accepts a caller-supplied session id
#: has to refuse anything in this reserved namespace.
RUNTIME_SESSION_PREFIX = "erp-case-"

_SESSION_PAD = "0"


class CaseKeyError(ValueError):
    """A case identity could not be built or parsed."""


def canonical_key(pack_id: str, key: Mapping[str, str]) -> str:
    """Serialize a natural key to the exact string :func:`derive_case_id` digests.

    Sorted by field name, so a key rebuilt in a different order is still the same
    case: the config's field order is a display concern, and letting it reach the
    digest would make identity depend on how a mapper happened to iterate.

    Raises:
        CaseKeyError: If ``pack_id`` is empty, the key is empty, or a field name or
            value is empty or holds a character that would make the serialization
            ambiguous.
    """
    pack = "" if pack_id is None else str(pack_id).strip()
    if not pack:
        raise CaseKeyError("pack_id is required to derive a case_id")
    if not isinstance(key, Mapping) or not key:
        raise CaseKeyError(f"{pack}: a natural key with at least one field is required")

    parts = [pack]
    for name in sorted(key):
        field = str(name).strip()
        if not _KEY_FIELD_RE.match(field):
            raise CaseKeyError(
                f"{pack}: key field {name!r} is not a usable field name; "
                f"expected characters in [A-Za-z0-9_]"
            )
        value = "" if key[name] is None else str(key[name]).strip()
        if not value:
            raise CaseKeyError(
                f"{pack}: key field {field} is empty, so the case has no identity"
            )
        if _KEY_DELIMITER in value:
            raise CaseKeyError(f"{pack}: key field {field} holds a control character")
        parts.append(f"{field}={value}")
    return _KEY_DELIMITER.join(parts)


def derive_case_id(pack_id: str, short_id: str, key: Mapping[str, str]) -> str:
    """Mint the ``case_id`` for a case from its natural key. The only way to mint one.

    Deterministic: the same SAP object polled twice, or pushed once and polled once,
    derives the same id, which is what makes ``case_exists`` a duplicate check rather
    than a race. See the module docstring for the form and what it costs.

    Args:
        pack_id: The domain's identity, e.g. ``finance_ops``. Digested, not displayed.
        short_id: The domain's readable prefix, e.g. ``ap``.
        key: The natural key — SAP field name to value, any arity.

    Raises:
        CaseKeyError: If ``short_id`` is not a usable prefix, or the key cannot be
            serialized (see :func:`canonical_key`).
    """
    short = "" if short_id is None else str(short_id).strip()
    if not _SHORT_ID_RE.match(short):
        raise CaseKeyError(
            f"short_id={short_id!r} is not a usable case_id prefix; expected 2-8 "
            f"characters matching {SHORT_ID_PATTERN}"
        )
    digest = hashlib.sha256(canonical_key(pack_id, key).encode("utf-8")).digest()
    return f"{short}{SEPARATOR}{b32encode(digest).decode().lower()[:DIGEST_LENGTH]}"


def describe_case(case_id: str, key: Mapping[str, str] | None = None) -> str:
    """Render a case for a log line: the id, then the natural key that produced it.

    ``ap-k7m2q9x4rt6bw8ncf3jhd5gzpv (SupplierInvoice=5100001976, FiscalYear=2026)``

    A minted id is unreadable on its own, so every log line that used to be
    self-describing has to carry the key alongside it. This is that, in one place,
    so an operator grepping logs sees the same rendering everywhere.
    """
    if not key:
        return str(case_id)
    pairs = ", ".join(f"{name}={key[name]}" for name in key)
    return f"{case_id} ({pairs})"


def to_case_key(case_id: str) -> dict[str, str]:
    """Return the DynamoDB ``Key`` dict for a ``case_id``.

    ``case_id`` is the cases table's sole partition key, so this is a one-attribute
    key. It stays a function rather than an inline dict so the attribute name lives
    in one place, and so a malformed id is refused on the way in.

    Raises:
        CaseKeyError: If the value is not a well-formed case identity.
    """
    return {"case_id": normalize_case_id(case_id)}


def normalize_case_id(case_id: str) -> str:
    """The id, stripped, if it is a case id.

    Raises:
        CaseKeyError: If the value is not one.
    """
    value = "" if case_id is None else str(case_id).strip()
    if not _DERIVED_CASE_ID_RE.match(value):
        raise CaseKeyError(f"case_id={value!r} is not a case id")
    return value


def try_normalize_case_id(case_id) -> str | None:
    """Best-effort :func:`normalize_case_id` — ``None`` instead of raising.

    For untrusted edges (message bodies, free-text prompts) where an absent or
    malformed id is an expected outcome that downstream code resolves another way.
    """
    try:
        return normalize_case_id(case_id)
    except CaseKeyError:
        return None


def to_runtime_session_id(case_id: str) -> str:
    """Derive a stable AgentCore session id for a case.

    Deterministic, so every turn and every background resume for one case lands in
    the same AgentCore Memory session instead of a fresh random one. Right-padded
    because AgentCore Runtime rejects ids shorter than
    :data:`RUNTIME_SESSION_MIN_LENGTH`.

    Raises:
        CaseKeyError: If ``case_id`` is not a well-formed case identity.
    """
    base = f"{RUNTIME_SESSION_PREFIX}{normalize_case_id(case_id)}"
    if len(base) < RUNTIME_SESSION_MIN_LENGTH:
        base += SEPARATOR + _SESSION_PAD * (RUNTIME_SESSION_MIN_LENGTH - len(base) - 1)
    return base
