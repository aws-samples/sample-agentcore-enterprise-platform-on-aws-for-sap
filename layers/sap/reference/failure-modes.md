# SAP layer failure modes

Errors at config load, synth and run time, and what causes each.

| You see | Because |
|---|---|
| `use case ... requires layer 'sap', which platform.yaml does not enable` | Add `layers.sap` |
| `use case ... has no row in layers.sap.access` | Add its access row. A use case that is not named gets no SAP access |
| `layers.sap needs its interceptor check` | Add `layers/sap/sap_check` with exactly the SAP targets' prefix |
| `flow <flow> needs ...` at config load | That flow's keys are missing ([Sign-in flows](../walkthrough.md#sign-in-flows)) |
| `flow obo needs identity.mode direct with idp entra_id` | `obo` exchanges the caller's Entra token, so the platform must take Entra tokens directly |
| `... is not on in layers.sap.systems.<system>.writes` | The `access` row names a write the system has off |
| `...ingest: <system> has no senders` or `is not in ...bypass_gateway` | An `ingest` system needs `senders`. It also needs the use case's direct access (`bypass_gateway`), because the use case responds to its events by reading SAP directly |
| `<use case> subscribes to [...], outside layers.sap.access.<use case>.ingest` | Add the system to that row's `ingest` |
| Outbox rows stay `E` for one topic | The sender role refuses a topic that no `ingest` names. Add the system to the use case's `ingest` and deploy the layer again. Then reset the rows' `attempts` |
| Target stays in `CREATING`, or "An error occurred when starting the runtime" | The server did not start: read its log group. Usual causes: the secret is missing in this Region or lacks a key, or a `scopes` entry contains a space |
| `Refused by the Gateway: no use case claims SAP access for this client` | No SAP access claim names that app. Add it to its use case's SAP access claim, inside that use case's `access` row |
| `... writes to SAP with no check of its own ...` | An SAP access claim with `governed_by: None` can write only under `security.cedar.mode: ENFORCE` |
| `Refused ... the call could not be authorized` on every SAP call | The check could not read its settings or an SAP access claim (a deploy in progress, a missing permission). When it cannot read them, it refuses the call |
| SAP answers 401, 403, 404 or 500 | See [sap-setup.md](../sap-setup.md#troubleshooting-sap-errors). On a token flow, also see [sap-trust.md](../sap-trust.md#troubleshooting-token-rejection) |
