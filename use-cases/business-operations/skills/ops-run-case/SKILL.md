---
name: ops-run-case
description: Use when asked to "run a case", "test the pack", "seed and run", "try it end to end" or "did the agent release the invoice" on a deployed business-operations stack. Seeds one SAP exception with `ops.py seed`, makes it a case with `ops.py poll`, starts and follows the run, takes it through a human approval in the console or a ticket, checks the SAP data afterwards, re-runs a case with `ops.py rerun`, and reads the evidence the run left.
---

# Run one case end to end

This skill takes one exception from a document posted in SAP to a case with a final status.
Paths are relative to the repository root, and `UC` is `use-cases/business-operations`. "You" in
this skill is the coding agent that runs it; "the business process agent" is the deployed agent
that works the case.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
PROJECT=<project> ENV=<environment>   # from platform.yaml
```

Every step except the last two writes something real: SAP documents, a case, and a run that
Amazon Bedrock bills (`cost-report` shows what your runs cost). Run it on a demo or sandbox SAP
client only.

## 1. Seed one exception

This step needs `use_cases.business-operations.test_data.enabled: true`, and a pack whose
`seed` block has `steps`. The steps are the documents to post, declared in `pack.json`; there is
no code per pack.

- **The SAP client needs the data the steps name.** `finance_ops` posts on SAP's S/4HANA sample
  data: supplier `USSU-VSF04`, material `MZ-RM-R300-01`, plant, purchasing organization and
  company code `1710`, purchasing group `002`. On an SAP client without that data, SAP refuses
  the purchase order. Ask before you seed. Without the data, ask the SAP team to post one
  blocked supplier invoice (MIRO), and start at step 2.

```bash
ops seed finance_ops --list                                          # inputs and presets
ops seed finance_ops --preset 7 posting_date=2026-08-31 --yes        # over the auto-write limit
```

- **Posting periods decide whether the documents are posted.** For `finance_ops`, the steps
  post a purchase order, a goods receipt and a blocked invoice with `posting_date`.
  - SAP refuses a goods receipt or invoice in a closed period. The purchase order is posted
    anyway, and the verb exits 1 and names the step that failed (`gr_error`, `invoice_error`).
  - Ask whoever runs the SAP client which Materials Management (MM) period (MMPV) and Financial
    Accounting (FI) period (OB52) are open for company code 1710. Pass a date inside both.
  - Opening a period is an SAP change. Never do it from here.
- Choose the preset for the path you want. An exact match under `AUTO_WRITE_LIMIT_USD` is
  released with no person. Preset 7 (exact match, over the limit) and the variance presets stop
  and wait for an approval.
- Keep the printed `case.case_id`. It is the id that the poller gives the exception, derived
  from `case.key` (for `finance_ops`, `SupplierInvoice` and `FiscalYear`).

## 2. Make it a case

```bash
ops poll finance_ops     # {"packs": {"finance_ops": {"system": "s4", "created": 1, ...}},
                         #  "cases_created": 1, "cases_skipped": N, "cases_rejected": 0}
```

`created: 0` means one of two things. Either the document already has a case, or the pack's
`source.filter` does not match it. In the second case the invoice did not post, so go back to
step 1.

```bash
TABLE=$(aws ssm get-parameter --name "/$PROJECT/$ENV/use-cases/business-operations/cases-table" \
  --query Parameter.Value --output text)
```

## 3. Start the run and follow it

- `ops trigger-mode` reads the deployment's trigger mode and each pack's mode. Each one is
  `manual` by default. Unless both read `auto`, the case waits in `detected`. Start it with
  **Process** on the case page in the console, or with `ops rerun <case_id>`. When both are
  `auto`, the poll already queued it.
- Follow it on the console's case page, which shows the run as it happens. Or read the record
  every 20 seconds until the status is no longer `processing`:

```bash
aws dynamodb get-item --table-name "$TABLE" --key '{"case_id":{"S":"<case_id>"}}' \
  --projection-expression "#s,status_reason,ticket_id" --expression-attribute-names '{"#s":"status"}'
```

A run takes 45 to 90 seconds.

- `sap_updated` and `complete` are final: the agent cannot update the case again, and
  **Process** on it answers 409.
- `awaiting_human_input` waits for a person; `ticket_id` names the ticket.
- `manual_review_required` and `error` give their cause in `status_reason`.

## 4. Approve

A write over the pack's limit, or a variance that the standard operating procedure (SOP, or
procedure) sends to a person, stops the case with an approval ticket. Only a member of the
pack's `entitlement.approve_groups` can approve:

```bash
ops add-user <approver's email> --create --groups business-operations-ap-manager
```

Use a mailbox that the approver reads. `--create` sends the temporary password there. The
approver sets their own password at the first sign-in on the console.

- **Console:** the approver signs in, opens the case, reads the proposed write (the card
  shows it field by field) and approves it, or answers on the ticket.
- **Ticket:** the Tickets page, or `POST <ApiUrl>tickets/{id}/action` with
  `{"action": "approved", "comment": "..."}` and the approver's own token. That is the ID token
  the console keeps after they sign in, sent as the `Authorization` header.
  - `ApiUrl` is an output of the `<project>-<env>-uc-business-operations` stack.
  - `GET <ApiUrl>tickets/{id}` first shows them the write they are approving.
  - The console's app client allows only SRP and the browser's code flow, so there is no token
    without a sign-in.

Either way, the decision is recorded against the approver's token, with the digest of the
write they approved. The case is queued again, whatever the trigger mode says. Never approve by
invoking a route's Lambda with invented token (JWT) claims, or with anyone else's token: the
approver's identity on the action log is the audit record.

## 5. Check the SAP data

A run's `success: true` and the case's `sap_updated` are what the business process agent
reports. Read SAP itself. For `finance_ops`, **do not judge a release by the invoice header's
`PaymentBlockingReason`**. On S/4HANA, a `Release` removes the block on the supplier line of the
invoice's accounting document. The header (`RBKP-ZLSPR`, which `A_SupplierInvoice` shows) can
keep its posted value. The poller's filter then still matches the released invoice, and only its
stable case id prevents a second case.

- The accounting document: `BKPF` where `AWTYP = 'RMRP'` and `AWKEY` is the invoice number
  followed by its fiscal year.
- Its supplier line: `BSEG` with `KOART = 'K'`. `ZLSPR` blank means released; a value means
  still blocked.
- In the SAP GUI: FB03 on that document, or FBL1N for the supplier (payment block column).
  Over ABAP Development Tools (ADT), the SQL console reads both tables. Use two short `SELECT`s,
  because it cuts a statement at 255 characters.

## 6. Re-run

```bash
ops rerun <case_id>            # a stuck or failed run; refuses complete and sap_updated
ops rerun <case_id> --force    # also a case that waits for a person
ops rerun --dlq                # re-queue each dead letter's case
```

A re-run includes no person's decision, so it never counts as approved. A case that waits for
an approval waits for it again. If a case's description contains an old error, a re-run can
keep that error. To prove a fix, seed a new document.

## 7. Read the evidence

```bash
aws dynamodb get-item --table-name "$TABLE" --key '{"case_id":{"S":"<case_id>"}}' > case.json
python3 - <<'EOF'
import json
from boto3.dynamodb.types import TypeDeserializer
d = TypeDeserializer()
case = {k: d.deserialize(v) for k, v in json.load(open("case.json"))["Item"].items()}
print(case["status"], case.get("status_reason"), case.get("cost_summary"))
for a in case.get("action_log") or []:
    print("action", a.get("action"), a.get("actor"), a.get("source"), bool(a.get("approved_write")))
for t in case.get("agent_traces") or []:
    print("run", t.get("trigger"), t.get("outcome"), t.get("sop_key"), t.get("sop_version_id"),
          t.get("estimated_cost_usd"), len(t.get("verifier_calls") or []))
    for s in t.get("segments") or []:
        if s.get("type") == "tool":
            e = s.get("evidence") or {}
            print("  ", s.get("tool_name"), s.get("status"), e.get("kind"), e.get("authz"))
EOF
```

- `action_log`: what the business process agent recorded, and every `human_*` decision, with
  its actor, its source (`console` or `ticket`) and the approved write. `consumed_by_run` names
  the run that used the approval.
- `agent_traces[]`: one per run, with the trigger, the procedure key and version it read, the
  outcome, tokens and estimated cost, and each verifier call. The verifier is a second model that
  must agree with a payment write before it runs.
- `segments[].evidence`: per tool call, its kind (`sap_read`, `sap_write`, ...), the Cedar mode
  and outcome, the fields read or written, and the procedure clause quoted as its reason.

## Self-check

The skill is done when it prints one line:

- `OK: <case_id> <status>; SAP <document> supplier line block '<ZLSPR>'; USD <cost>` when the
  case reached the final status the preset predicts and SAP agrees, or
- `FAIL: <cause>`: the seed was not posted, the case never left `processing`, it reached a
  different final status, or SAP disagrees with the case.
