# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Case status sets that more than one Lambda has to agree on.

Here rather than in a handler directory because each handler ships as its own package:
a constant defined in one is not importable from another, and the only alternative is a
copy. Two copies of a business invariant is how the two ends of a decision path come to
disagree about what "settled" means.

The two sets answer different questions and are deliberately not interchangeable:

* ``SETTLED_STATUSES`` — the case's business decision is already recorded. A *new*
  decision here is a duplicate, and acting on it risks applying a recorded ERP side
  effect twice.
* ``RUN_TERMINAL_STATUSES`` — an agent run that ended with the case in one of these
  reached a defined outcome, so the message is safe to acknowledge. A superset: a case
  parked on a human is a legitimate place for a run to stop, but not a settled decision.
"""

#: A decision has been recorded and the work is finished. ``sap_updated`` is here for the
#: same reason ``agent_invoker`` will not retry it — the ERP side effect is already
#: written, and applying it twice is the failure this set exists to prevent.
SETTLED_STATUSES = frozenset({"complete", "sap_updated"})

#: A normally ended agent stream is safe to acknowledge only when the case reached one of
#: these schema-defined outcomes. ``sap_updated`` is included because retrying after a
#: recorded ERP side effect risks applying it twice.
#:
#: Wider than ``SETTLED_STATUSES`` by the two statuses that park a case on a person.
#: Those still accept an operator's reply — answering them is the entire supervised
#: path — so they are a fine place for a run to end and a wrong place to refuse a
#: decision.
RUN_TERMINAL_STATUSES = SETTLED_STATUSES | {
    "awaiting_human_input",
    "manual_review_required",
}

#: A case parked on a person, not a settled decision. `agent_invoker` uses this to
#: refuse a *redelivery* here (SQS ``ApproximateReceiveCount`` > 1) while still
#: accepting a human's fresh reply to the same case — the entire supervised path
#: depends on the second one working. Do not widen ``SETTLED_STATUSES`` instead:
#: these are exactly where a reply is expected to land next.
PARKED_ON_PERSON_STATUSES = RUN_TERMINAL_STATUSES - SETTLED_STATUSES

#: The two statuses only the platform sets, and the one the agent writes when a tool call
#: fails irrecoverably (`types/cases.schema.json`).
NOT_STARTED_STATUSES = frozenset({"detected"})
RUNNING_STATUSES = frozenset({"processing"})
FAILED_STATUSES = frozenset({"error"})

#: The console's views of the case list (`GET /cases?view=`), in the order it shows
#: them, each with the order its list takes by default. Every status is in exactly one
#: view besides `all` and `team`, which a test holds to the schema's statuses; `all` is
#: filled in by the handler from that list, so a new status cannot fall out of it.
#:
#: `needs_you` and `team` read the same statuses, every case waiting on a person, and
#: differ by pack: `team` reads every pack the caller may see, whoever may decide its
#: cases; `needs_you`, being in :data:`DECIDER_VIEWS`, only the packs whose cases the
#: caller may decide (`decisions.authorize`: the pack's `approve_groups`).
VIEWS: dict[str, tuple[frozenset[str] | None, str]] = {
    "needs_you": (PARKED_ON_PERSON_STATUSES, "asc"),
    "team": (PARKED_ON_PERSON_STATUSES, "asc"),
    "new": (NOT_STARTED_STATUSES, "asc"),
    "running": (RUNNING_STATUSES, "asc"),
    "failed": (FAILED_STATUSES, "asc"),
    "done": (SETTLED_STATUSES, "desc"),
    "all": (None, "desc"),
}

#: The views that read only the packs the caller may decide in (`may_approve`); every
#: other view reads every pack the caller may see.
DECIDER_VIEWS = frozenset({"needs_you"})
