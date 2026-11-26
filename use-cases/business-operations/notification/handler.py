# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""send_notification: the agent's path to a person, over one channel per deployment.

`NOTIFICATION_CHANNEL` is `ses` (the default) or `tickets` (the sample ticketing
table).

Every message is screened before any channel sees it: `ApplyGuardrail`, on this
function's own role, refuses one that carries a credential, an SSN or a card number.
It runs in every Cedar mode, so no Gateway setting turns it off.

Two kinds of failure, kept apart on purpose:

- A `ValueError` is about the request (a malformed or disallowed recipient, or what
  the guardrail refuses): the agent can correct it and send once more, so it is named
  back and raises no alarm.
- Anything else is the channel (SES refusing, throttling, a sender never
  configured): nothing the agent does next reaches a person, so it emits the
  `NotificationFailure` metric an alarm watches, and the agent parks the case.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from case_key import try_normalize_case_id
from persist_mask import WITHHELD, mask
from ticket_record import new_ticket

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CHANNEL = os.environ.get("NOTIFICATION_CHANNEL", "ses")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
NAMESPACE = os.environ.get("METRICS_NAMESPACE", "business-operations")
PRIORITIES = ("low", "medium", "high", "critical")
_EMAIL = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

_clients: dict = {}


def _client(service: str):
    if service not in _clients:
        _clients[service] = (
            boto3.resource(service) if service == "dynamodb" else boto3.client(service)
        )
    return _clients[service]


class NotConfigured(Exception):
    """The deployment, not the request, is missing something: a channel failure."""


def emit_failure_metric(kind: str) -> None:
    """NotificationFailure as Embedded Metric Format: no PutMetricData grant needed.

    Two dimension sets, the second empty: CloudWatch does not roll a dimensioned
    metric up into the dimensionless one the alarm watches.
    """
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": NAMESPACE,
                            "Dimensions": [["Channel"], []],
                            "Metrics": [
                                {"Name": "NotificationFailure", "Unit": "Count"}
                            ],
                        }
                    ],
                },
                "Channel": CHANNEL,
                "NotificationFailure": 1,
                "FailureKind": kind,
            }
        )
    )


def screen(*texts: str) -> None:
    """Raise ValueError when the guardrail refuses what the message carries.

    The texts are the recipient, subject and body as the agent wrote them, before the
    case reference is appended: the guardrail reads a derived case id as an AWS key.
    A guardrail that cannot be asked is a channel failure, so nothing is sent unscreened.
    """
    if not GUARDRAIL_ID:
        raise NotConfigured("this deployment has no notification guardrail")
    content = [{"text": {"text": t}} for t in texts if t]
    if not content:
        return
    resp = _client("bedrock-runtime").apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VERSION,
        source="OUTPUT",
        content=content,
    )
    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return
    found = sorted(
        {
            str(e.get("type"))
            for a in resp.get("assessments") or []
            for e in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities")
            or []
            if e.get("action") == "BLOCKED"
        }
    )
    raise ValueError(
        f"the message carries {', '.join(found) or 'content'} that may never leave in "
        "a notification; remove it and send again"
    )


def with_case_reference(body: str, case_id) -> str:
    """Append `Case ID: <id>` once, after the guardrail has scored the body.

    The guardrail reads a derived case id as an AWS access key, so the agent keeps
    it out of the body and passes it in `case_id`. That argument is never scored,
    so anything that is not a real case id is dropped here rather than delivered.
    """
    if not case_id:
        return body
    canonical = try_normalize_case_id(case_id)
    if not canonical:
        logger.warning("case_id is not a case id; reference line omitted")
        return body
    line = f"Case ID: {canonical}"
    if line in body.splitlines():
        return body
    return f"{body}\n\n---\n{line}\n"


# ── SES ──


def _sender() -> str:
    sender = os.environ.get("NOTIFICATION_SENDER", "").strip()
    if not sender:
        raise NotConfigured(
            "this deployment has no SES sender: set use_cases.business-operations."
            "notification.ses_sender_email in platform.yaml, verify that address in "
            "SES, and redeploy"
        )
    return sender


def allowed_domains(sender: str) -> tuple[str, ...]:
    """The configured recipient domains, or the sender's own when none are.

    Fail closed: the agent picks the recipient out of case data, which is
    deliberately unsanitized, so an open recipient makes every notification an
    exfiltration channel.
    """
    configured = [
        d.strip().lower().lstrip("@")
        for d in os.environ.get("NOTIFICATION_RECIPIENT_DOMAINS", "").split(",")
    ]
    return tuple(d for d in configured if d) or (sender.rpartition("@")[2].lower(),)


def send_ses(*, recipient: str, subject: str, body: str, **_) -> dict:
    # Sender first: a missing one fails every call, whatever the recipient.
    sender = _sender()
    if not recipient or not _EMAIL.match(recipient):
        raise ValueError(f"Invalid email: {recipient}")
    # A subdomain of a listed domain passes; a look-alike suffix does not.
    domain = recipient.rpartition("@")[2].lower()
    allowed = allowed_domains(sender)
    if not any(domain == d or domain.endswith(f".{d}") for d in allowed):
        raise ValueError(
            f"Recipient domain not allowed: {domain}. This deployment may only mail "
            f"{', '.join(allowed)}; an operator adds a domain to "
            "notification.recipient_domains in platform.yaml."
        )
    resp = _client("ses").send_email(
        Source=f"SAP Agent <{sender}>",
        Destination={"ToAddresses": [recipient]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )
    return {
        "channel": "ses",
        "message_id": resp["MessageId"],
        "recipient": recipient,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── The sample ticketing table ──


def send_ticket(
    *, recipient: str, subject: str, body: str, case_id, priority: str
) -> dict:
    ticket = new_ticket(
        subject or "Agent notification",
        created_by="agent",
        description=body,
        priority=priority,
        assigned_to=recipient,
        case_id=case_id,
        category="agent_notification",
        # A notification asks for no decision; unset, the console reads it as an
        # approval and offers Approve and Deny.
        response_type="free_text",
    )
    _client("dynamodb").Table(os.environ["TICKETS_TABLE"]).put_item(Item=ticket)
    return {
        "channel": "tickets",
        "ticket_id": ticket["ticket_id"],
        "assigned_to": recipient,
        "timestamp": ticket["created_at"],
    }


CHANNELS = {"ses": send_ses, "tickets": send_ticket}


def handler(event, context):
    # The Gateway sets the tool name; a plain invoke has none. The Gateway role's
    # invoke grant is the access control, not this.
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except (AttributeError, KeyError, TypeError):
        return {"error": "No tool name: call this through the AgentCore Gateway"}
    if full.split("___", 1)[-1] != "send_notification":
        return {"error": f"Unknown tool: {full}"}
    priority = event.get("priority") or "medium"
    try:
        send = CHANNELS.get(CHANNEL)
        if send is None:
            raise NotConfigured(f"unknown notification channel {CHANNEL!r}")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
        recipient = event.get("recipient") or ""
        subject = event.get("subject") or ""
        body = event.get("body") or ""
        screen(recipient, subject, body)
        subject, body = mask([subject, body])
        if WITHHELD in (subject, body):
            raise NotConfigured("the persistence guardrail could not be reached")
        result = send(
            recipient=recipient,
            subject=subject,
            body=with_case_reference(body, event.get("case_id")),
            case_id=event.get("case_id"),
            priority=priority,
        )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    except ValueError as e:
        logger.warning("Rejected: %s", e)
        return {"error": f"Notification failed: {e}"}
    except NotConfigured as e:
        # Authored text about this deployment's own settings, so it is named back.
        logger.error("Channel not configured: %s", e)
        emit_failure_metric("NotConfigured")
        return {"error": f"Notification channel unavailable: {e}"}
    except Exception as e:
        logger.exception("Channel failure")
        emit_failure_metric(type(e).__name__)
        return {"error": f"Notification channel unavailable: {type(e).__name__}"}
