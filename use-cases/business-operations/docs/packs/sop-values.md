<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# SOP-cited values

A standard operating procedure (SOP), called a procedure below, cites a value as `{{SYMBOL}}`.
Every path that gives procedure text to the model replaces the symbol the same way
(`agent/utils/config_overrides.py`). Each value is kept in one place, by kind:

| Kind | Examples | Where it is kept | Who changes it |
|---|---|---|---|
| Rule | `PRICE_VARIANCE_PCT`, `MISSING_GR_ESCALATION_DAYS` | The pack only: versioned, and moved by bundle | The pack's authors with its `constant_groups`, as a content edit in development |
| Contact | `{{CONTACT_AP_TEAM}}` | The stack only: `use_cases.business-operations.contacts` in `platform.yaml`, then `PUT /deployment/settings` | Operators, per stack |
| Limit | `AUTO_WRITE_LIMIT_USD`, `DUAL_APPROVAL_LIMIT_USD` | An upper limit in the pack, and a per-stack value that may only be lower | Pack admins set the upper limit. Operators lower it per stack through `PUT /packs/{pack_id}/controls` |

## Stack overrides

`PUT /packs/{pack_id}/controls` refuses a rule value. So a tolerance tuned in development moves
with the pack's next version, instead of staying in one stack. The route also refuses a limit above
the pack's value. A later version can lower the pack's limit below an existing override. That
override is then ignored: the run checks, the approval routes and the prompt all use the lower of
the two values. Null deletes an override of any declared constant.

These show each symbol's value in this stack, and its source: `GET /packs/{pack_id}/rules` and
`/controls`, `ops pack status <pack_id>` and the console. The source is one of `pack v7`,
`stack override (lowered from 100000)`, `platform.yaml` or `stack contact override`.

- A constant also has `guarded_by`: the write guards that the pack enables and that read the
  constant ([Guarded rules](#guarded-rules)).
- A rule in `GET /packs/{pack_id}/rules` also has `cited_by`: the process types whose procedure in
  the active version names the rule, read from that stored version.

The console's procedure editor lists these, under the preview, for the symbols that the text
cites. The pack card shows rule values read-only, with an input field only for each write limit.

## Guarded rules

Code also checks some rules. A write guard is a check that the agent runtime makes against SAP,
by reading SAP again, before an SAP write that releases a payment. A pack turns guards on in
`write_guards`. Each guard that a pack turns on reads its rules from the pack.
`agent/utils/guard_rules.py` has the map of guards to rules. A test fails if a guard reads a
constant that the map does not list:

| Guard | Reads |
|---|---|
| `within_tolerance_or_approved` | `PRICE_VARIANCE_PCT`, `PRICE_VARIANCE_ABS_USD`, `QTY_VARIANCE_PCT`, `QTY_VARIANCE_UNITS`, `LINE_AMOUNT_TOLERANCE_USD` |
| `no_duplicate` | `DUPLICATE_MATCH_WINDOW_USD` |
| `po_reference`, `gr_coverage` | No constant |

A guarded rule is a control as well as a procedure value. A change to it changes what the agent
may post without an approval, so agree it with the control owner. `GET /packs/{pack_id}/rules`
and `ops pack status` list each constant's `guarded_by`, for the guards that the pack turns on.
The limits show `[]`, because the run checks enforce them, not a guard.

## Upgrading a stack that overrides a rule constant

An earlier release of this repository accepted such overrides. The agent now ignores the row.
So the upgrade deploy fails while one remains, before it writes anything, and names each pack and
symbol. The rollback keeps the old release running. Each row is in the stack's Config table under
the namespace `constant#<pack_id>`.

- **In development**, set each value in the pack as an edit (a new version), then delete the
  row: `PUT /packs/<pack_id>/controls` with `{"limits": {"<SYMBOL>": null}}`, or clear its field
  on the Packs screen and save.
- **In quality or production**, make that edit in development, export it and import it here with
  a change reference, then delete the row.

Limit and contact rows stay.

## Known limitations

- **A symbol in `base_prompt.txt` is not checked.** The agent replaces symbols in the whole
  system prompt, the pack's base prompt included. Only procedures are checked for symbols that
  the pack does not declare, at synth, at publish and in `verify`. So an undeclared `{{SYMBOL}}`
  in `base_prompt.txt` reaches the model as written, and nothing refuses it. Cite values in a
  procedure, not in the base prompt. If the base prompt must cite one, check that the pack
  declares it.
