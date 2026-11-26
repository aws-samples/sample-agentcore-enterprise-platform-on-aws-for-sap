<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Pack edit rights

Who may change which part of a pack, in which stack, and what record each edit leaves.
`pack_tiers.check_patch` is the one rule that decides. A standard operating procedure (SOP) is
called a procedure below.

## Content and structure

- **Content** (`display_name`, `description`, `base_prompt`, a mapped procedure's text) needs
  `business-operations-authors` and, when the pack sets `author_groups`, one of those groups. A
  pack with no `author_groups` (`finance_ops` sets none) lets any author who can see it edit it.
- **A declared constant's value is content too**, and needs the pack's
  `entitlement.constant_groups` as well.
- **Every other key is structure**, and needs `business-operations-pack-admins`. That includes
  constants, when the patch declares a name that the pack does not have. Procedures cite constants
  by name, and every procedure write path refuses a procedure that cites an undeclared symbol.
- **`AUTO_WRITE_LIMIT_USD` and `DUAL_APPROVAL_LIMIT_USD` are structure, even for a value
  change.** The run checks read them on every SAP write, and a stack may only lower either one.
  A pack that declares `DUAL_APPROVAL_LIMIT_USD` must also declare `AUTO_WRITE_LIMIT_USD`,
  because the run checks use only the auto limit to decide whether a write without an approval
  may run.
- **A stack lowers either limit with `PUT /packs/{pack_id}/controls`.** That route needs
  `business-operations-limit-owners` and, when the pack sets `limit_groups`, one of those groups.
  The authors group is not enough.
- **`write_amount_field` and `write_limit_waiver` are structure too.** A pack that grants an SAP
  write must declare `AUTO_WRITE_LIMIT_USD` or a waiver.
- **`source.service` and `source.entity` are restricted.** A `source.service` is an OData (Open
  Data Protocol) V2 service name, or a V4 service path with the same characters. A
  `source.entity` is an entity set name. Validation and the poller refuse anything else, so a pack cannot point
  the poller at another SAP path.

## Pack parts and tiers

How the [pack parts](../packs.md#pack-parts) fall into the two tiers, and the
[role](../roles.md) each part's owner has:

| Part | Tier | Group that may edit it | Owner's role |
|---|---|---|---|
| Intake | Structure | `business-operations-pack-admins` | SAP functional consultant and AI engineer |
| Procedures: procedure text, general instructions, name, description, rule values | Content | `business-operations-authors`, plus the pack's `author_groups`; a rule value also needs `constant_groups` | Process owner and process analysts |
| Procedures: `process_type_to_sop`, `shared_sops`, `dispositions`, declaring a new rule | Structure | `business-operations-pack-admins` | Process owner and process analysts |
| Controls | Structure | `business-operations-pack-admins` | Control owner |
| Agent configuration | Structure | `business-operations-pack-admins` | AI engineer |
| Evaluations: the example cases (`evals.json`) | Content | `business-operations-authors`, plus the pack's `author_groups`, through `PUT` and `DELETE /packs/{pack_id}/evaluations/{test_id}` | Process analysts, with the AI engineer |
| Evaluations: `seed` | Structure | `business-operations-pack-admins` | Process analysts, with the AI engineer |

Where the owner's role does not include the group, the owner decides the change and a pack admin
makes it.

## Access answers for the console

`GET /registry` returns each pack's `access`: what the signed-in caller may do in the pack, in
this stack. It answers per part (`edit.intake`, `edit.procedures_text`, `edit.rule_values`,
`edit.procedures_structure`, `edit.controls`, `edit.agent_configuration`, `edit.evaluations`,
`edit.example_cases`). It also says whether the caller may lower the pack's limits, set the
trigger mode, approve, start runs and rehearse. Each answer calls the function that decides that write
(`api/layer/python/pack_access.py`). A test checks the `edit` answers against
`PATCH /packs/{pack_id}`. The console uses `access` to mark and disable controls. Each route still
makes its own decision.

## Development only

All of these edits happen in development only. A quality or production stack refuses every pack
and procedure write over HTTP (`pack_tiers.edit_refusal`).

`GET /deployment/settings` says, in `access`, whether the caller may:

- create a pack (`may_create_pack`: the authors group, in development);
- edit the contacts (`may_edit_contacts`: the authors group, in any stack).

It uses the same rules that those routes decide with.

`POST /packs` creates a pack in development only, and a created pack starts **incomplete**. It is
published, and authors can edit and rehearse it. But no `process_type` routes to it, and the poller
ignores it until a pack admin completes it. A created pack gets only read tools, case state and
ticket escalation, never an SAP write or `send_notification` (`names.created_pack_tools`). The
reason: the create form is a browser form. If it accepted a tool list, anyone who may create a
pack could give that pack an SAP write grant. A pack admin adds the real SAP values when the pack is
completed.

## Versions

In a development stack every edit is a version. Each of these becomes the pack's next stored
version: a content patch, a create, a procedure written or deleted through
`/packs/{pack_id}/sops`, and an example case saved or deleted. The author is recorded in the
version's provenance and in an `edit` (or `create`) audit record. So `ops bundle versions` lists every edit with its author, and
`ops bundle rollback` returns to any of them. The composer builds each version itself from the
request's fields. No route sends it a bundle.

A write may name the version that it was made against (`expected_version`).
`GET /packs/{pack_id}/base_prompt` returns `version`, and
`GET /packs/{pack_id}/sops/{process_type}` returns `pack_version`. If someone else has saved the
pack since then, the write answers 409 and writes nothing. The console and the authoring MCP
(Model Context Protocol) server send `expected_version`.

## SOP writes

A procedure is addressed by a process type that maps to it, and a procedure file is always
mapped:

- A new procedure is created with its process type (`POST /packs/{pack_id}/sops`), which adds the
  mapping in the same version.
- A delete removes the file, together with every process type that maps to it.

Both change the mapping, which is structure. So both need `business-operations-pack-admins` as
well as the pack's author grant. Replacing a mapped procedure's text needs the author grant
alone. An alias reads the file it is an alias of, and a write to an alias is refused.

A procedure written through `/packs/{pack_id}/sops` (the Packs screen, or `tools/mcp-authoring/`)
is read at once by the next run and by `load_sop`. The write also starts a knowledge-base
ingestion job, so `search_sap_sops` serves the new text too.

When a job is already running, the response says `QUEUED`. The knowledge stack then starts one
more job automatically once the running job finishes (it checks every five minutes). So nobody
needs to start a sync (`POST /sops/sync`, or the authoring MCP server's `sync_sops`).

Beside each procedure is a `<key>.metadata.json` file that names its pack, so one pack's search
never returns another pack's procedure. The deploy writes any that are missing, and the bucket
refuses any other key that is not text.
