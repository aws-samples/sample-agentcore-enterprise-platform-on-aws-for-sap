---
name: sec-production-check
description: Use when asked "is this ready for production", "harden this deployment", "what blocks go-live" or "before we go live" for a business-operations stack. Runs the read-only `ops.py production-check` (platform.yaml plus EventBridge) and `deploy.sh verify`, then reports every row with its fix and effort. Changes nothing.
---

# Check a deployment against production

This skill only reads. Paths are relative to the repository root, and `UC` is
`use-cases/business-operations`. "You" in this skill is the coding agent that runs it.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
PLATFORM_CONFIG=<the stack's platform.yaml> ops production-check   # --offline skips AWS
```

It reads the file that `PLATFORM_CONFIG` names (`platform.yaml` at the root when unset), and
lists the Amazon EventBridge rules in the Region. It prints one line per row, `PASS`, `WARN` or
`FAIL`, with the fix under each row that does not pass. It exits 1 while any row fails. Ids in
brackets are rows of the threat model (`UC/docs/reference/threat-model.md`, and the SAP layer's
threat model for `SAP-`).

| Row | FAIL when | Fix |
|---|---|---|
| `deployment.mode` | not `production`. The row lists every platform control that the platform's own validator would still refuse (identity provider (IdP), networking, egress filter, guardrails, CloudTrail alerting, alarms and `alarm_email`, ...) | Set `production` and the listed controls. An enterprise IdP takes the longest |
| `security.egress_filter` | off: no guardrail scans tool calls and responses on the shared Gateway | `security.egress_filter: true` |
| `security.egress_request_action` | not `block`: a tool call that the guardrail would rewrite goes through masked. So a write can post a value that the business process agent did not send | `security.egress_request_action: block` |
| `security.cedar` | Cedar off, or `LOG_ONLY`. In both, Cedar refuses nothing, so every app client the Gateway admits can reach tools that only Cedar would refuse. Which controls apply in each Cedar mode: [SAP access limits](../../docs/architecture.md#sap-access-limits) | `enabled: true`, `mode: ENFORCE`, then `deploy.sh verify` |
| `promotion.role` | `development` under `deployment.mode: production` | Unset it. A role other than `production` there is a WARN, and is correct for QAS only |
| bundle signing trust | the stack's role is `production` | **Not available yet.** Pack bundles have a `sha256` and no signature. So anyone who may invoke the pack composer can import a bundle edited by hand. Allow `lambda:InvokeFunction` on it only to the release pipeline, and require the change reference. A WARN in other roles |
| `identity.self_sign_up` | on | `false`; add users with `ops add-user` |
| `api.sample_ticketing` | on | Connect a real IT service management (ITSM) system, then `false` |
| `test_data.enabled` | on | `false`: the seeder writes SAP outside the Gateway, Cedar and the run checks |
| `observability.log_retention_days` | under 90 (WARN under 365) | 365, or what your retention policy names |
| `contacts` | a contact that the standard operating procedures (SOPs, or procedures) cite is unset (WARN: one points at `example.com`) | Set each under `use_cases.business-operations.contacts` |
| AWS for SAP MCP (Model Context Protocol) Server TLS | never: always a WARN | The SAP layer turns on certificate verification with `MCP_SERVER_SESSION_VERIFY`. `deploy.sh verify` fails if an SAP runtime's start banner says `Session Verify: False`. Run it after each image change |
| audit-change alarms | never: a WARN while no EventBridge rule in the Region watches the deployment's trigger-mode parameter, so a trigger-mode change, a registry write or a procedure write goes unnoticed | Deploy this use case's main stack, which alerts the platform's ops topic on all three. The trigger-mode rule fires only while a trail logs management events in the Region |

## Deployed state

`production-check` reads the configuration, which is what the next deploy would build.
`deploy.sh verify` proves what is deployed now:

```bash
./scripts/deploy.sh verify
```

Its use-case line must say that Cedar `ENFORCE` refused the reader's write, and that the
runtime refused the reader's token. A `FAIL:` from `verify` outweighs a `PASS` from
`production-check`.

## Gaps in both checks

Report these as open items the deployer answers, each with who owns it:

- Amazon Simple Email Service (SES) out of the sandbox (`aws sesv2 get-account`), and
  `notification.recipient_domains` limited to the domains that mail may go to.
- The pack's `AUTO_WRITE_LIMIT_USD` (or `write_limit_waiver`), its `write_guards` and verifier
  on writes that move a payment, and its `write_amount_field`. Write guards are fixed checks
  against SAP before a payment write; the verifier is a second model that must agree with it.
- What the Gateway does when the interceptor Lambda fails. With reserved concurrency 0, it
  refused every call (BO-61), so an outage stops every use case. A timeout or a function error
  is not verified. Test it on a development stack, and have an alarm on the interceptor's errors
  and throttles.
- `layers.sap.systems.<s>.writes` no wider than the packs need, and the SAP service user's
  roles (an SAP administrator's review).
- `ops trigger-mode`: `auto` means every new exception is a paid run with no person.
- Cost-allocation tags activated, for `ops cost-report`.

## Self-check

Run it twice. The second run must print the same rows. Then print one line:

- `OK: no row blocks production, <n> warning(s)` (the verb's own last line), or
- `FAIL: <n> row(s) block production, <n> warning(s)` (the verb's own last line), followed by
  each failing row and its fix.
