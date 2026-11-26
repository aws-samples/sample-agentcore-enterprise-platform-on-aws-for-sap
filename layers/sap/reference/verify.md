# SAP layer verify checks

What `deploy.sh verify` checks about the SAP layer.

`deploy.sh verify` runs `layers/sap/verify.py` after the platform's checks and before any use
case's checks. It checks that:

- each runtime this layer builds is READY and has a log retention. On every flow except `obo`,
  the runtime's resource policy allows only the Gateway's role. (An `obo` runtime has no resource
  policy.) A runtime you bring yourself is not checked.
- each runtime verifies SAP's certificate. Verify reads the newest `Session Verify:` line of the
  runtime's start banner and fails on `False`. A runtime with no session since its last update
  has no banner to read: verify reports it as unverified and passes, except under
  `deployment.mode: production`, where it fails. Any SAP read through the Gateway starts a
  session.
- each target is READY.
- the Gateway's live policy engine is in `ENFORCE` whenever the check's settings say that Cedar
  enforces.
- through the first target, the platform's own M2M (machine-to-machine) app client is refused an
  SAP read and `odata_delete`. No SAP access claim names that client.

A successful SAP read is checked by each use case's own verify, because only a client that an
SAP access claim names can make one.

When any system has `senders`, verify also checks that:

- the bus's policy denies `PutEvents` to everyone except the sender roles, contains no allow
  statement, and restricts each sender role to its own source and topics.
- each sender role trusts exactly its `senders` and has only its one restricted permission.
- a test `PutEvents` call is refused. The call uses the credentials that verify runs with,
  source `sap.<first system>` and `detail-type` `verify-probe`.
- no rule on the bus routes any of these:
  - a `detail-type` that is not the name of a use case with an `ingest` list
  - a source outside that use case's `ingest`
  - a target with no dead-letter queue
- each rule matches its use case's subscription. The subscription is trusted only when a stack
  deploy wrote it. The rule's ARN must be the subscribed one. The rule must have exactly one
  target, which is the subscribed queue with the subscribed dead-letter queue (DLQ). It must have
  no input transform, no role and no HTTP parameters.

An `ingest` list with no rule is reported, not failed, because its use case may be turned off.
Whether a sender's event creates a case is for the use case's own verify to check, because only
SAP can put such an event.
