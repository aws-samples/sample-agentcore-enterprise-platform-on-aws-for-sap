# SAP layer reference

The detail behind the [SAP layer walkthrough](../walkthrough.md), one topic per page. MCP is the
Model Context Protocol, and OData is the Open Data Protocol that SAP services use.

| Page | Covers |
|---|---|
| [Settings](settings.md) | Every key under `layers.sap.systems.<system>`, and the server image |
| [Sign-in details](sign-in.md) | Credential rotation, how direct callers sign in, the `unattended` block |
| [MCP server per system](server.md) | What is deployed per system, the server's own controls, write rules, OData V4, a custom server |
| [Network paths](network.md) | SAP S/4HANA Cloud, SAP Business Technology Platform (BTP) and RISE with SAP |
| [Use case access](use-case-access.md) | The synth API, runtime parameters, Cedar and the interceptor checks, SAP access claims written by hand |
| [Events bus](events.md) | The `sap-events` bus, sender roles, subscriptions and the SAP outbox |
| [Verify checks](verify.md) | What `deploy.sh verify` checks about the layer |
| [Failure modes](failure-modes.md) | Errors at config load, synth and run time, and their causes |
