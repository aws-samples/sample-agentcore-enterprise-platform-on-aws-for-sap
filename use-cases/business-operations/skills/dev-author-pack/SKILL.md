---
name: dev-author-pack
description: Use when writing or changing a business-operations pack's `pack.json` or `base_prompt.txt` (`use-cases/business-operations/packs/<pack_id>/`), or when asked to "change the model tier", "add a constant", "give the pack a write", "edit the base prompt", "add a process type" or "create a pack without a checkout". Decides first whether each change is structure, content or a current setting, because that decides who may make it and how it reaches a stack, then writes it and validates it.
---

# Author a pack

A pack is one SAP process. `use-cases/business-operations/packs/<pack_id>/` contains:

- `pack.json`: what the business process agent may do, and where its cases come from;
- `base_prompt.txt`: who the business process agent is;
- `sops/`: what it does, by process type. These are its standard operating procedures (SOPs,
  or procedures); the `owner-author-sop` skill writes them;
- optionally, `evals.json`.

`packs/pack.schema.json` is authoritative for `pack.json`. Point `$schema` at
`../pack.schema.json`, so that an editor validates the file as you type. Paths here are relative
to the repository root. "You" in this skill is the coding agent that runs it.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
```

## Change classification

| Class | Keys | Who | How it reaches a stack |
|---|---|---|---|
| **Content** | `display_name`, `description`, `base_prompt.txt`, the text of a mapped procedure | The pack's authors (`business-operations-authors`, narrowed by `entitlement.author_groups`) | A development stack: console `/packs`, the authoring Model Context Protocol (MCP) server's `publish_pack_content`, or `PATCH /packs/{pack_id}`. Active at once. In other stacks, only by importing a bundle (`ops-promote-pack`) |
| **Content, with `constant_groups`** | The **value** of a rule constant the pack already declares (every constant except the two write limits) | The pack's authors, narrowed by `entitlement.constant_groups` | `constants` on `publish_pack_content`, `PATCH /packs/{pack_id}` or **Edit values** on the pack's **Procedures** tab in the console, in a development stack. This is the only place to set it: no stack overrides a rule value, and the pack includes it in every stack it is imported into |
| **Structure** | Every other `pack.json` key, declaring or removing a constant **name**, the value of `AUTO_WRITE_LIMIT_USD` or `DUAL_APPROVAL_LIMIT_USD`, and which procedure files exist | `business-operations-pack-admins`, in a `promotion.role: development` stack only | The checkout, then `ops bundle import packs/<pack_id>` (dry run, then `--apply`), or a deploy of the development stack, which imports a changed checkout pack. Elsewhere only by importing a bundle exported from development (`ops-promote-pack`) |
| **Current setting** | A contact, or a write limit **lowered** below the pack's value (never raised) | Authors for a contact; `business-operations-limit-owners` for a limit, narrowed by `entitlement.limit_groups` | `PUT /deployment/settings` for a contact, `PUT /packs/{pack_id}/controls` for a limit: in the console, or with the MCP server's `lower_pack_limits`. Never transported between stacks. `PUT /packs/{pack_id}/controls` refuses a rule value |

A constant's name is structure because procedures cite it (`{{SYMBOL}}`). Every procedure
write path refuses a procedure that cites a symbol no pack declares. The two write limits are
structure even as values. The run checks read them on every SAP write, and a stack may only
lower either one. So an author who could set the value could raise the limit on writes that
nobody approved. `ops pack status <pack_id>` lists each symbol's value in a stack and its source
(`pack v7`, `stack override (lowered from 100000)`, `platform.yaml`).

A patch that names a key its caller may not write is refused completely. If the refusal says
"structure", make the change in the checkout.

**Without a checkout**, the authoring MCP server's `create_pack` (or **New pack** on `/packs`)
builds a development draft from a spec. The draft has the read tools only, no SAP writes and no
routing. It starts **incomplete**: published and editable, but the poller and the router ignore
it until a pack admin completes its `source`, `fields` and tools. It starts incomplete because a
create form that accepted a tool list would let a browser grant SAP writes. To complete it, the
pack admin:

1. writes the draft into a checkout with
   `ops bundle export <pack_id> --to-dir use-cases/business-operations/packs` (an incomplete
   pack exports with no version, and creates no published version);
2. finishes its structure there (`dev-new-sap-process` step 3);
3. imports the directory.

## `pack.json` keys

| Key | Rule |
|---|---|
| `pack_id` | `snake_case`, equal to the directory name. It prefixes every case id |
| `display_name`, `description` | What an operator reads. The business process agent never sees them |
| `model_tier` | `haiku` (start here), `sonnet`, or `opus` (only where the deployment allows an Opus model). A tier the stack does not serve fails the synth |
| `max_turns` | 1 to 15, default 15. A run needs 5 to 7. Lower it only after you measure runs |
| `run_budget` | Omit for the default (`max_tokens` 2,000,000, `max_cost_usd` 0.50). Set it only from a measured run |
| `model_effort`, `context_editing` | Omit unless a benchmark says otherwise (`dev-tune-cost`) |
| `sap_system` | A key of `layers.sap.systems`. Omit when the deployment has exactly one |
| `sap_service` | The services and entity sets the business process agent is told about (`services[].service`; `entities.<alias>.name` is the **entity set**, such as `A_SupplierInvoice`, not the type). It is documentation for the model, and grants nothing |
| `gateway_tools` | The allowlist of tools, as full names `<target>___<tool>`. Case tools: `business-operations-case-management___get_case_state`, `___update_case_state`; `business-operations-sops___load_sop`; `business-operations-knowledge___search_sap_sops`; `business-operations-notification___send_notification`; with sample ticketing, `business-operations-tickets___demo_create_ticket`, `___demo_get_ticket`. SAP reads: `sap-<system>___odata_read`, `___odata_count`, `___get_metadata`. SAP writes: `sap-<system>___odata_update`, `___odata_create`, `___odata_function_import`. An unknown name fails the synth |
| `case_document_check` | **Required when `gateway_tools` includes an SAP write and the pack has a `source`.** `{"<write parameter>": "<source.key field>"}`. The run checks refuse any SAP write whose checked parameter is missing or differs from the case's own key, in every Cedar mode. So a run can write only its own document. Without this key, every write is refused |
| `write_amount_field` | The write field that contains the amount an SAP write posts (`InvoiceGrossAmount` for the supplier invoice service). Optional; letters, digits and `_`. When it is set, the run checks read that amount from the call's body, parameters or key fields. They compare it with the case's amount and with `AUTO_WRITE_LIMIT_USD`. A create or update that omits the field, or whose value is not a finite number, is refused. A function import (Release, Cancel) is checked only when it includes the field; otherwise its amount is still the case row's amount. A write that a person approved field by field is not checked again. Structure |
| `write_limit_waiver` | Why a pack that grants SAP writes has no `AUTO_WRITE_LIMIT_USD`, at least 20 characters. Required in that case: the synth refuses a pack that has an SAP write and has neither key. Not read at run time. Structure |
| `constants` | `{"UPPER_SNAKE": number}`: every threshold a procedure cites as `{{SYMBOL}}`. Numbers only. Bounds by suffix: `_PCT` 0 to 100, `_DAYS` 0 to 365, anything else 0 to 1,000,000,000. Two constants are special, and both stay structure, where the other values are content. `AUTO_WRITE_LIMIT_USD` is required in a pack that grants an SAP write (or give `write_limit_waiver`), and an SAP write above it needs a person's approval. `DUAL_APPROVAL_LIMIT_USD` is the second. Never start a constant's name with `CONTACT_` |
| `process_type_to_sop` | `{"<process_type>": "<pack_id>/<file>.txt"}`, each file under this pack's `sops/`. A process type that two packs both name fails the synth |
| `shared_sops` | The process types whose procedure another procedure loads, and that triage never chooses, such as `short_pay` after an approver chooses a short payment. Each must be a `process_type_to_sop` key. It must not be the triage type, a `source.process_type.rules` target or an alias. Omit the key when the pack has none. Triage, exception and alias procedures need no marking, because they follow from the files ([Kinds of SOP](../../docs/packs.md#kinds-of-sop)) |
| `dispositions` | The `snake_case` labels a closed case may have, one per outcome. If they are declared, the case tool refuses any other label, and the prompt names them. If they are omitted, any label is accepted. Include every label a procedure names (`disposition "..."`) |
| `entitlement` | `groups` (who may read cases), `approve_groups` (a subset of `groups`), `author_groups`, `constant_groups` (rule values), `process_groups` (starting the business process agent on a case, within `business-operations-case-workers`), `limit_groups` (lowering a write limit in a stack, within `business-operations-limit-owners`). Every group name starts `business-operations-`. The stack creates the groups a checkout pack names. An import into another stack needs them to exist there (`ops add-group <name>`) |
| `write_guards`, `verifier` | Payment-release checks for `API_SUPPLIERINVOICE_PROCESS_SRV` only. A **write guard** is a fixed check that the agent runtime makes by reading SAP again before it allows a write that releases a payment: `po_reference`, `no_duplicate`, `gr_coverage`, `within_tolerance_or_approved`. The **verifier** is a second, independent model that must agree with each payment write after the guards pass; anything but a clear agree refuses the write. A pack that grants an SAP write must declare `write_guards`, or `write_guards: []` to say it chose none. The synth refuses a pack that declares neither. Other services have no guards, so `[]` there means only the run checks apply to the write |
| `source`, `fields` | Where cases come from, and how their attributes are shown: `dev-new-sap-process` |
| `seed` | How `ops.py seed` posts one exception into SAP: `inputs`, `presets`, the documents to post as `steps` (templates; the schema says what they may read) and the `case` the poller opens for them. No code. Omit it when there is no chain of sandbox documents to post |

## `base_prompt.txt`

- Write in the second person ("You are …"). The prompt gives the business process agent's
  identity and domain rules, not the procedure.
- Include `{PLATFORM_MECHANICS}` exactly once, after the persona line. Include `{SOP_CONTENT}`
  exactly once, at the end, under a `STANDARD OPERATING PROCEDURE` heading. If either one is
  missing, the runtime drops that section with no error, and every write path refuses the prompt.
- Never repeat `packs/_platform_prompt.txt`. Every pack already gets its tool usage, OData (Open
  Data Protocol) scoping, write semantics, the ticket protocol and the citation rules. A
  variation for the domain is fine; a copy is not.
- Leave no `<AWAITING_SURVEY…>` marker from a scoping scaffold. Publishing refuses them.
- Use `{{SYMBOL}}` and `{{CONTACT_*}}` only where the persona needs them; they belong in
  procedures.

## Steps

1. Ask for the process types, the SAP service and entities, the tier, the thresholds (name,
   default, unit), and who reads, approves and authors. Never invent a value.
2. Classify each change with the table above.
3. Write `pack.json` and `base_prompt.txt`, with `packs/finance_ops/` as the model. Give the
   procedures to `owner-author-sop` with the exact constant names.
4. Validate offline: `./scripts/deploy.sh synth` with the use case enabled. A broken pack
   stops the synth with `packs/ is not publishable:` and one line per problem.
5. Validate against the deployed stack: `ops bundle import packs/<pack_id>` (a dry run). It
   prints the checks and the diff it would apply. The checks are: tools offered, model tiers
   served, groups and contacts present, process types that two packs both use, undeclared
   symbols, and the version chain.
6. Show that diff and get a yes, then run `ops bundle import packs/<pack_id> --apply`. It writes
   the procedures, then the pack, then `registry.json`, and restores everything if a write
   fails.
   - An edit to content only can go through the console or the MCP server instead, in a
     development stack only. A quality or production stack refuses every edit, and receives
     the pack only through `ops-promote-pack`.
   - A deploy of the development stack also imports the checkout, without the dry run. It fails
     for a pack that a console or MCP edit changed since the last deploy. In that case, either
     export those edits into the checkout (`ops bundle export <pack_id> --to-dir
     use-cases/business-operations/packs`, after you move the directory to another place) and
     merge, or run the import above to replace them.

## Self-check

```bash
ops bundle status <pack_id>        # the active version and where it came from
```

Then read back what you wrote:

- `list_packs` lists the pack with its `source` and routing;
- `read_rules` and `read_controls` show its constants and bounds;
- `read_base_prompt` returns the prompt, with both placeholders unchanged.

Report `OK: <pack_id>@<version> active` or `FAIL: <the refusal, verbatim>`.
