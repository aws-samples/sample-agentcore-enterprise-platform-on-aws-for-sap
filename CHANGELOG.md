# Changelog

All notable changes to the Agentic Business Platform Accelerator for SAP on AWS are recorded
here. It stays on 0.x versions while it is a sample in preview.

## [0.1.0] - 2026-11-25

Initial release. The Accelerator is ported from the predecessor sample,
[sample-agentic-ai-process-automation-for-sap](https://github.com/aws-samples/sample-agentic-ai-process-automation-for-sap),
and rebuilt on the
[Enterprise Agentic AI Platform Accelerator](https://github.com/aws-samples/sample-agentcore-enterprise-platform).

Built on the platform's version 0.1.0 at commit [`83d1bdc2`](https://github.com/aws-samples/sample-agentcore-enterprise-platform/commit/83d1bdc25a697c9633cdc9de12bd4b571d8aa4f9) (2026-11-24).

### Added

- **The SAP layer** (`layers/sap/`). For each SAP system it deploys an AWS for SAP MCP Server
  and a Gateway target for it, with a choice of sign-in flows: one service account, or the
  signed-in user's own SAP identity (`obo`).
- **The SAP access claim check.** Each use case declares the SAP services and operations it
  calls, and the Gateway refuses any SAP call that no use case has claimed.
- **Use cases as drop-in folders.** A use case is one folder under `use-cases/` that deploys
  while `platform.yaml` lists it. `deploy.sh usecase new` scaffolds one, `usecase add` copies
  one in from a Git URL or a path and merges its configuration, and `usecase remove` deletes
  it again.
- **The SAP exception handling use case** (`use-cases/business-operations/`). A business
  process agent clears SAP exceptions, such as supplier invoices blocked for payment, by
  following written procedures. It posts the fix in SAP when the change is under your limit,
  and asks a person to approve the exact change when it is not.
- **The operator console**, the web app where people work cases, approve changes, and edit,
  rehearse and publish procedures without a redeploy.
- **Packs and procedures.** A pack holds the procedures, tools, checks and limits for one SAP
  process. The `finance_ops` pack ships eight procedures for supplier invoices.
- **The platform's Claude Code and Cowork on Amazon Bedrock use case** (`claude-3p-on-bedrock`),
  as it ships in the platform: Claude Code and Claude Desktop with Cowork, with every request
  served by Amazon Bedrock in your own account and sign-in through Microsoft Entra ID.
- **The hello-sap reference use case**, which reads SAP through the SAP layer as its own
  Gateway client and writes nothing.
- **Skills for coding agents**, starting at `start-here`: deploying, authoring a pack, adding
  an SAP process or a use case, running a case, tuning cost and checking a production
  deployment.
- **Operator commands**, run as `deploy.sh usecase run business-operations <command>`: among
  them `status`, `add-user`, `trigger-mode`, `emergency-stop`, `cost-report` and
  `production-check`.

[0.1.0]: https://github.com/aws-samples/sample-agentcore-enterprise-platform-on-aws-for-sap/releases/tag/v0.1.0
