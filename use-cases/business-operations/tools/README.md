<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# MCP servers for desktop clients

Two stdio Model Context Protocol (MCP) servers make this use case available in an MCP host, such
as Kiro or Claude Code. They answer different questions, and sign in as different identities.
Every use case shares the Gateway proxy (`sap_kit/mcp_gateway_proxy.py`), started here for this
one. The authoring server is in this directory.

| Server | Does | Signs in as | Needs |
|---|---|---|---|
| `sap_kit/mcp_gateway_proxy.py --use-case business-operations` | This use case's **read** tools on the Gateway that work outside a run: `search_sap_api_docs`, `check_vendor_risk`, and the SAP reads (`find_sap_services`, `get_metadata`, `get_service_hints`, `odata_read`, `odata_count`) | The `reader` client: a service identity, not you | The repo's `.venv` (boto3), and AWS credentials that can read the platform's SSM parameters and the reader client's secret |
| `mcp-authoring/mcp_authoring.py` | Authors packs and standard operating procedures (SOPs) over the REST API: `list_packs`, `read_rules`, `read_controls`, `read_deployment_settings`, `read_base_prompt`, `list_sops`, `get_sop`, `create_pack`, `publish_pack_content`, `lower_pack_limits`, `put_sop`, `create_sop`, `validate_sop`, `sync_sops` | **You**, with authorization code and Proof Key for Code Exchange (PKCE) on the console client, so the authors group check and each pack's `author_groups` apply | Python 3, stdlib only, and the console URL. No AWS credentials and no checkout: copy the file to an analyst's laptop |

The proxy offers only the reads that `ops.py` starts it with (`MCP_READ_TOOLS`), and no write.
The run checks refuse a case tool or SAP write from any client without a run grant, and only a run
that the server started has one. The proxy also leaves out the reads that the run checks refuse
outside a run (`get_case_state`, `load_sop`, `get_precedent`, `demo_get_ticket`).

The proxy's allowlist is a filter for a model, not a security boundary. Anyone who can read the
reader's secret can call the Gateway without the proxy. Under Cedar `ENFORCE`, the reader's
permits are what limit it. The authoring server has no rehearsal and no SOP delete. Rehearse on
the console's Packs screen.

## Setup

Print the host config for this deployment. The command is read-only: it looks up names and
creates nothing.

```bash
./scripts/deploy.sh usecase run business-operations mcp-config
```

It prints an `mcpServers` block with `business-operations-gateway` and, once the console is
deployed, `business-operations-authoring`. Keep the proxy's `env` block. A host starts the proxy
with an empty environment, so the proxy does not inherit `AWS_PROFILE` or the Region from your
shell, and boto3 reads `AWS_DEFAULT_REGION`.

- **Kiro:** paste the block into `.kiro/settings/mcp.json` in the workspace, or
  `~/.kiro/settings/mcp.json` for every workspace.
- **Claude Code:** paste it into `.mcp.json` at the repository root (project scope), or add each
  server with `claude mcp add-json <name> '<json>'`.

Other hosts take the same inner objects (`command`, `args`, `env`); some use `servers` rather than
`mcpServers` as the top-level key.

Sign in to the authoring server once, up front:

```bash
python3 use-cases/business-operations/tools/mcp-authoring/mcp_authoring.py login --app-url <ConsoleUrl>
```

It opens a browser and listens on `http://localhost:12800/callback`, the console client's
loopback redirect, so port 12800 must be free. Without `login`, the first tool call signs in,
within that call's timeout. `tools/list` works when you are signed out. A 400 or 403 from the API
is returned unchanged as the tool's error. The error says which tier a key belongs to, and where
to change it instead.

The authoring server does not work in direct (Entra) mode yet, because it signs in at the
Cognito user pool.
