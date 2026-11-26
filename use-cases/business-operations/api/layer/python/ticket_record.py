# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The one shape of a SAMPLE ticketing ticket, whoever files it.

Three writers put a ticket in the table: `POST /tickets` (a person), the agent's
`demo_create_ticket`, and `send_notification` on the `tickets` channel. Each built the
same 16-field record by hand, so they drifted. They all build it here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from case_key import try_normalize_case_id

# types/tickets.schema.json (TicketPriority, response_type); a drift test pins them.
VALID_PRIORITIES = {"high", "medium", "low"}
VALID_RESPONSE_TYPES = {"approval", "free_text"}


def new_ticket(
    title: object,
    *,
    created_by: str,
    description: object = "",
    priority: object = None,
    assigned_to: object = "",
    case_id: object = None,
    category: object = None,
    response_type: object = None,
) -> dict:
    """An open ticket, ready to put. Raises ValueError for what a caller must fix.

    An unknown priority becomes `medium`. A case id that does not parse is stored as
    "" rather than in a shape nothing else understands, so the resume path can always
    route by the stored form.
    """
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        raise ValueError("title is required")
    response_type = response_type or "approval"
    if not isinstance(response_type, str) or response_type not in VALID_RESPONSE_TYPES:
        raise ValueError(
            f"response_type must be one of: {sorted(VALID_RESPONSE_TYPES)}"
        )
    now = datetime.now(timezone.utc).isoformat()
    return {
        "ticket_id": f"TKT-{uuid.uuid4().hex[:8].upper()}",
        "title": title,
        "description": description or "",
        "status": "open",
        "priority": priority
        if isinstance(priority, str) and priority in VALID_PRIORITIES
        else "medium",
        "created_by": created_by,
        "assigned_to": assigned_to or "",
        "case_id": try_normalize_case_id(case_id) or "",
        "category": category or "general",
        "response_type": response_type,
        "resolution": "",
        "comments": [],
        "created_at": now,
        "updated_at": now,
    }
