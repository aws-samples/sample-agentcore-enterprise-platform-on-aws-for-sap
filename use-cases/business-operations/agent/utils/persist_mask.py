# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Mask what a run stores or sends on; leave what the model reads alone.

A bank-change or duplicate-payment check needs the supplier's bank account, so
nothing on the way to the model masks it. What outlives the run does not need it:
the case record (`agent_traces`, the case tool's title and description), tickets
and notifications. Under `security.require_guardrails` the use case creates a
persistence guardrail (`PERSIST_GUARDRAIL_ID`) that anonymizes bank, tax, social
security and card numbers, and each of those writes passes its text through
`mask` first. Without that setting there is no guardrail and nothing is masked.

Fails closed: when the guardrail cannot be asked, every text becomes `WITHHELD`.
"""

import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

WITHHELD = "[withheld: the persistence guardrail could not be reached]"
# Characters per ApplyGuardrail call, as the Gateway's egress interceptor: 50 text
# units, inside the 1,000 a sensitive-information policy accepts in one request.
_CHUNK_CHARS = 50_000
# Free text in a trace. Everything else on it is the platform's own values.
TRACE_TEXT_KEYS = frozenset(
    {"prompt", "content", "tool_input", "tool_result", "evidence"}
)

_client = None


def _bedrock():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get(
                "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION")
            ),
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _client


def enabled() -> bool:
    return bool(os.environ.get("PERSIST_GUARDRAIL_ID"))


def _apply(texts: list[str]) -> list[str]:
    """One call over `texts`, sent as one block, one per line. The masks are applied
    here from the reported matches, because the response says nothing about which
    text a match came from; a match found in no single text raises."""
    resp = _bedrock().apply_guardrail(
        guardrailIdentifier=os.environ["PERSIST_GUARDRAIL_ID"],
        guardrailVersion=os.environ.get("PERSIST_GUARDRAIL_VERSION", "DRAFT"),
        source="OUTPUT",
        content=[{"text": {"text": "\n".join(texts)}}],
    )
    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return texts
    masks = [
        (e["match"], "{" + str(e.get("type")) + "}")
        for a in resp.get("assessments") or []
        for e in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities") or []
        if e.get("match") and e.get("action") in ("ANONYMIZED", "BLOCKED")
    ]
    if any(not any(m in t for t in texts) for m, _ in masks):
        raise ValueError("a masked value spans two texts")
    out = []
    for t in texts:
        for match, tag in masks:
            t = t.replace(match, tag)
        out.append(t)
    return out


def mask(texts: list[str]) -> list[str]:
    """`texts`, masked; unchanged when this stack has no persistence guardrail."""
    texts = list(texts)
    if not enabled():
        return texts
    todo = [i for i, t in enumerate(texts) if isinstance(t, str) and t.strip()]
    try:
        out = list(texts)
        batch: list[int] = []
        size = 0
        for i in [*todo, None]:
            if batch and (i is None or size + len(texts[i]) + 1 > _CHUNK_CHARS):
                for j, masked in zip(batch, _apply([texts[j] for j in batch])):
                    out[j] = masked
                batch, size = [], 0
            if i is not None:
                batch.append(i)
                size += len(texts[i]) + 1
        return out
    except Exception:  # fail closed: nothing is stored unmasked
        logger.exception(
            "persistence guardrail failed; withholding %d text(s)", len(todo)
        )
        withheld = set(todo)
        return [WITHHELD if i in withheld else t for i, t in enumerate(texts)]


def mask_record(record, keys=TRACE_TEXT_KEYS, cache: dict | None = None):
    """`record` with every string under one of `keys`, at any depth, masked in place.

    `cache` maps a text already masked to its masked form, so a caller that masks the
    same growing record again (the live snapshot, every few seconds) asks the guardrail
    only about text it has not seen. A withheld text is never cached: the next call
    asks again.
    """
    slots: list[tuple] = []

    def walk(node, take: bool) -> None:
        items = node.items() if isinstance(node, dict) else enumerate(node)
        for k, v in items:
            inside = take or (isinstance(node, dict) and k in keys)
            if isinstance(v, str):
                if inside:
                    slots.append((node, k))
            elif isinstance(v, (dict, list)):
                walk(v, inside)

    if enabled() and isinstance(record, (dict, list)):
        walk(record, False)
        cache = {} if cache is None else cache
        todo = list(dict.fromkeys(node[k] for node, k in slots if node[k] not in cache))
        # This call's answers; only a real mask is kept for the next call.
        known = {text: cache[text] for node, k in slots if (text := node[k]) in cache}
        for text, masked in zip(todo, mask(todo)):
            known[text] = masked
            if masked != WITHHELD:
                cache[text] = masked
        for node, k in slots:
            node[k] = known.get(node[k], node[k])
    return record
