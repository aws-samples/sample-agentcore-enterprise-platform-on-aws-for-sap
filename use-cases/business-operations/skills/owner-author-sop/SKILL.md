---
name: owner-author-sop
description: Use when writing or revising a standard operating procedure (SOP) for a business-operations pack (`use-cases/business-operations/packs/<pack_id>/sops/`), or when asked to "write the SOP for <process type>", "add a step to the procedure" or "change what the agent does for <exception>". Enforces RFC 2119 language, the section structure, and {{SYMBOL}} / {{CONTACT_*}} placeholders instead of literal values, lints the SOP before anything is written, then writes it through the checkout, the console, the REST API or the authoring Model Context Protocol (MCP) server.
---

# Author an SOP

A standard operating procedure (SOP) is the procedure that the business process agent follows
for one or more process types. This page says "procedure" after this paragraph. The router puts
a procedure's text into the pack's `base_prompt.txt` at `{SOP_CONTENT}`, so every sentence is an
instruction that the model reads. Paths here are relative to the repository root, and `UC` is
`use-cases/business-operations`. "You" in this skill is the coding agent that runs it.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
```

**Two ways to work.**

- In a checkout, every step below works.
- **Without a checkout**, the authoring Model Context Protocol (MCP) server
  (`UC/tools/README.md`) is the only transport. Read with `list_packs`, `read_rules`,
  `list_sops` and `get_sop`, and write with `put_sop`. Skip anything that names `ops`,
  `UC/packs/` or a deploy. The server signs in as the person in the browser, so every write is
  theirs.

Someone with neither edits the procedure on the console's Packs screen. The person is usually
the process owner, who knows the rule and its exceptions but not the file format. Ask about the
business, and handle the format yourself.

## SOP location

- On disk: `UC/packs/<pack_id>/sops/<file>.txt` (or `.md`; nothing else is accepted).
- In the deployed stack: the SOP bucket, key `<pack_id>/<file>.txt`. The pack's
  `process_type_to_sop` maps a process type to that key. `get_sop`, `put_sop` and
  `PUT /packs/{pack_id}/sops/{process_type}` find the procedure by that key.
- A pack serves only its own procedures: the key's first segment is the `pack_id`.
- Which procedure files a pack has, and which process type maps to which, is the pack's
  **structure** (`dev-author-pack`). The text of a mapped procedure is **content**. The pack's
  authors can edit it in a development stack. A quality or production stack receives it only by
  import (`ops-promote-pack`).

## Rules

1. **RFC 2119 keywords, in capitals.**
   - MUST / MUST NOT for a step that, if left out, would corrupt data, move money wrongly, or
     break an audit.
   - SHOULD for a strong default with a stated exception.
   - MAY for a judgement call.
   - MUST NOT for anything harmful or that cannot be undone (posting without approval,
     inventing data).

   Question every MUST: is it really absolute?
2. **Every clause is one complete sentence that can be read alone.** The business process agent
   cites the rule it acted on by quoting the sentence. The platform checks the quote against the
   procedure text the run was given. A rule split across a bullet fragment and its parent line
   cannot be quoted. Number clauses `N.M`, followed by two spaces and a capital letter
   (`3.2  IF the variance…`), so that the number is recorded with the quote.
3. **Third person**, "The agent MUST…". The base prompt is in the second person; the procedure
   is not.
4. **Name the entity set or function** for every SAP call (`A_SupplierInvoice`,
   `/Release`). Never write "call the invoice API". Read the names from the pack's `sap_service`.
5. **No literal thresholds, tolerances, windows or amounts**, in steps or in the tolerances
   table. Write `{{SYMBOL}}`, declared in the pack's `pack.json` `constants`:
   - the name matches `[A-Z][A-Z0-9_]*`. The unit goes in the prose
     ("within `{{MISSING_GR_ESCALATION_DAYS}}` days"), not in the value;
   - a `_PCT` symbol must be 0 to 100, a `_DAYS` symbol 0 to 365, and any other symbol 0 to
     1,000,000,000;
   - authors change a declared symbol's value within those bounds without changing the
     procedure. The value is content, and needs the pack's `constant_groups`. The value moves
     with the pack: no stack overrides it;
   - declaring a new symbol is the pack's structure. So is the value of `AUTO_WRITE_LIMIT_USD`
     or `DUAL_APPROVAL_LIMIT_USD`, which a stack may only lower;
   - an undeclared symbol reaches the model unchanged, as `{{SYMBOL}}`. So the synth, the
     procedure routes and `bundle import` all refuse a procedure that cites one.
6. **People are roles, never addresses.** Write `{{CONTACT_<ROLE>}}` (`{{CONTACT_AP_TEAM}}`).
   - Its value comes from `use_cases.business-operations.contacts.<role>` in `platform.yaml`,
     with the key in lower case (`ap_team`). `PUT /deployment/settings` can change it at once.
   - A new role means a new key there. The synth warns while one is unset, and an import into a
     stack that does not have it is refused.
   - Never use the `CONTACT_` prefix for a constant.
7. **Stateless.** The business process agent remembers nothing from earlier runs. Say where
   each fact is read.
8. **Never invent data.** If data is missing, the procedure says to set the case to `error` (or
   escalate) and stop.
9. **Notifications** name the recipients, the subject template and the body fields.
10. **IF / ELSE blocks** are indented under the clause that owns them.

## Structure

The headings are fixed, in this order. The router reads the `Version` line and records it on
every run's trace.

```
<TITLE IN CAPITALS>
STANDARD OPERATING PROCEDURE (SOP)
Version 1.0

================================================================================
PURPOSE
================================================================================

<What this SOP covers and when it applies.>

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT",
"SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this
document are to be interpreted as described in RFC 2119.

================================================================================
SCOPE
================================================================================
<The process types that route here; the SAP service and entities in scope.>

================================================================================
PRECONDITIONS
================================================================================
================================================================================
STEP 1: <TITLE>
================================================================================
1.1  The agent MUST …
================================================================================
ESCALATION RULES
================================================================================
================================================================================
CONTACTS
================================================================================
<Each role as {{CONTACT_<ROLE>}}.>
================================================================================
TOLERANCES (CONFIGURABLE — DECLARED IN packs/<pack_id>/pack.json)
================================================================================
| Parameter | Symbol |      <- symbols only, never values
================================================================================
REVISION HISTORY
================================================================================
| Version | Date | Author | Changes |
```

Read one shipped procedure before you write a new one. `UC/packs/finance_ops/sops/short_pay.txt`
is short. It shows how a procedure states what the service **cannot** do, which stops the
business process agent from reporting a write that does not exist. Increase `Version` on every
change, and add a history row.

## Steps

1. **Ask** for the process types, the SAP entities, the business rules, every threshold
   (name, default, unit) and every role to notify. Never invent a number or a contact.
2. **Read the pack**: `UC/packs/<pack_id>/pack.json` (`constants`, `sap_service`,
   `process_type_to_sop`), or the deployed pack with
   `ops bundle export <pack_id> -o /tmp/<pack_id>.json`.
   - Without a checkout: `list_packs` gives `process_type_to_sop`, `read_rules` gives the
     constants, and `get_sop` gives the current text. Save `{"pack_id", "constants",
     "process_type_to_sop"}` from them as `/tmp/<pack_id>.json` for the lint.
   - A symbol you need that is not in `constants` is the pack's structure. Write the developer a
     one-line request (the name, default and unit) for `dev-author-pack`. Stop until the symbol
     is added.
3. **Draft** to the structure, then read each clause again against the rules above.
4. **Lint** (step "Self-check" below). Fix every `FAIL` before you write anything.
5. **Show the diff** against the current text, and ask before you write. Shipped procedures are
   revised deliberately.
6. **Write it through one transport:**

   | Change | Transport | Active when |
   |---|---|---|
   | New procedure file, or a new mapping | Checkout, then `ops bundle import packs/<pack_id>` (dry run), then the same with `--apply`. Or, in the deployed stack, the authoring MCP server's `create_sop {pack_id, process_type, content}` or `POST /packs/{pack_id}/sops`. That writes the file and its mapping in one version, and also needs `business-operations-pack-admins`. Development stacks only | At once |
   | Text of a mapped procedure, reviewed in git | Same as above | At once |
   | Text of a mapped procedure, edited in the deployed stack | Console `/packs`, procedure editor; or the authoring MCP server's `put_sop {pack_id, process_type, content}`; or `PUT /packs/{pack_id}/sops/{process_type} {content}` with a console id token | Next run |

   The authoring MCP server (`UC/tools/mcp-authoring/`, host config from `ops mcp-config`)
   signs in as you. So the write is recorded against you, and passes the authors check. It
   cannot delete. Only a development stack accepts a write. A quality or production stack
   refuses every procedure write and delete, and receives the procedure through
   `ops-promote-pack` with a change reference.
7. **Index it.** A write in the deployed stack starts a knowledge-base ingestion job. If a job
   is already running, the write answers `QUEUED`, and another job starts by itself when the
   first ends. You need to do nothing in either case. `NOT_DEPLOYED` means procedure search is
   off and nothing needs indexing.
8. **Keep the checkout current** (in a checkout only; without one, tell the developer that the
   new version exists). In a development stack, a write in the deployed stack is already the
   pack's next version (`ops bundle versions <pack_id>` names you). `ops bundle export <pack_id>`
   makes it a published version, so it can be promoted. Copy the text back into the checkout
   to keep the checkout current.

## Self-check

`lint_sop.py`, beside this file, needs no AWS and nothing except Python 3. Run it on the
draft, with the pack (from the checkout, or the JSON saved in step 2), so that it also checks
the symbols and the mapping:

```bash
python3 <this skill's directory>/lint_sop.py /tmp/<file>.txt --pack-json UC/packs/<pack_id>/pack.json
```

Save the draft under the procedure's own file name (`<file>.txt`), because the lint builds the
key from it. It prints `OK:` or one `FAIL:` per problem. The write path checks the symbols again,
and refuses what the lint missed.

With no checkout, the same lint runs on the server:

- the authoring MCP server's `validate_sop {pack_id, process_type, content}`;
- the console's procedure editor, which runs the check as you type;
- `POST /packs/{pack_id}/sops/{process_type}/check`.

Each one returns the refusals that a save would give, then the findings, and writes nothing.

After a write in the deployed stack, also check the read-back. `get_sop {pack_id, process_type}`
returns exactly the text you sent, and the response's `version_id` is new. Report
`OK: <key> v<Version> written via <transport>` or `FAIL: <cause>`.
