# SAP use case access

The API a use case calls at synth to reach SAP, the controls that decide each call, and how the
layer treats an SAP access claim written by hand. Synth is the step where the AWS Cloud
Development Kit (CDK) builds the templates.

## Use case access API

Call it at synth, from the layer's `lib/`. That directory is on the Python path while the layer
is enabled.

```python
from sap_layer import access

sap = access.SapAccess(ctx, "<use case>")  # its access row
sap.tool("s4", "odata_read")  # the tool name the model sees
sap.claim(
    stack,
    callers=[client_id],
    read=["s4"],
    write={"s4": ["update"]},
    governed_by="<its interceptor check>",
)  # or None
sap.permits(stack, read=["s4"], write={"s4": ["update"]}, scope_prefix="uc-<name>")
sap.grant_direct(fn, "s4")  # only for a system in `bypass_gateway`
sap.subscribe(stack, queue=queue, dlq=dlq, systems=["s4"])  # only for one in `ingest`
```

- **`claim`** publishes the use case's SAP access claim, `layers/sap/claims/<use case>`, from the
  use case's own stack. The SAP access claim names:
  - the calling apps. The app id is `client_id` on an Amazon Cognito token and `azp` on an Entra
    token, a person's token included.
  - the systems and operations those apps use

  The layer's check refuses any SAP call, read or write, from an app that no SAP access claim
  names. It also refuses anything outside the SAP access claim or outside the `access` row. The
  SAP access claim works like the authorizations an SAP role requests. The access row is the
  operator's upper limit on it, so a use case's own code cannot give itself more SAP access than
  the operator allowed.
- **`governed_by`** names the use case's own interceptor check. That check must be listed with a
  prefix that covers the SAP targets. It then decides for its own callers (run grants, limits),
  and must return None for any caller that its SAP access claim does not name. With
  `governed_by=None`, the layer allows the use case's writes only while Cedar is in `ENFORCE`. The
  use case's Cedar write permit is then the only control that allows a write.
- **`permits`** creates Cedar permits on the platform's policy engine, for each system named in
  `read` or `write`:
  - a read permit when the system is read
  - a write permit when the system has one of the named writes turned on

  Each permit is named for that use case, such as `sap_<system>_read_<hash>`. In brokered mode a
  permit matches the use case's scopes. In direct mode it matches its apps' `azp`. An `obo` read
  (principal propagation) is a person's read, so it needs no scope, and SAP authorizes the
  person.
- **`grant_direct`** lets one of the use case's own functions call SAP outside the Gateway, as the
  system's service account. It gives the function the system's secret and the published
  connection. On a token flow, the connection includes `flow`, `token_url`, `discovery_url` and
  `scopes`. These calls skip the SAP access claim check, which is why
  `access.<use case>.bypass_gateway` must name each system. To make these calls, copy
  `layers/sap/lib/sap_client/sap_http.py` and `sap_odata.py` into the function's asset. They are
  clients written with the Python standard library only, and they call only the configured origin.
  `sap_http.py` sends GET requests. `sap_odata.py` adds the POST of a direct write and keeps the
  OData cross-site request forgery (CSRF) token session.
- **`subscribe`** sends SAP's events for this use case from the layer's bus to the use case's own
  queue ([The sap-events bus](events.md)).

### Runtime parameters

At run time, read only what the layer publishes in SSM Parameter Store (AWS Systems Manager)
under `/<project>/<env>/layers/sap/`:

- `systems`: target, flow, type, writes and tools per system
- `systems/<system>/connection`: for direct systems
- `egress-security-group-id`: with the VPC (virtual private cloud), for a direct caller
- `check`: the check's settings
- When some system has `senders`, also `bus-arn`, `systems/<system>/sender-role-arn` and
  `ingest` (what the layer's verify checks)

## Cedar and the interceptor checks

| Control | Granularity | Applies in | What it does |
|---|---|---|---|
| A use case's Cedar permits (`permits`) | Its scopes (brokered) or apps (direct) | `ENFORCE` only | Allows its own tokens the SAP actions it was granted. Cedar refuses everything else by default, which also hides the rest from `tools/list` |
| The layer's check | Calling app, system, operation | Every mode | Refuses callers that no SAP access claim names, operations outside the SAP access claim, `odata_delete`, and writes with no `governed_by` check outside `ENFORCE` |
| A use case's own check | Whatever it decides (run, case, amount) | Every mode | For example: run grant, approval limit, approved write |
| SAP | The user it runs as | Always | That user's roles, and on `m2m-sap` the client's scopes ([sap-setup.md](../sap-setup.md#authorization-checks-in-sap)) |

With Cedar off or in `LOG_ONLY`:

- a client that an SAP access claim names reads SAP as the service account. Under `obo`, it reads
  as the mapped person.
- a write passes only when its SAP access claim names a `governed_by` check and that check
  allows it.

No SAP access claim names the platform's own app clients (the sample agent, the orchestrator), so
they cannot reach SAP at all.

## Hand-written claims and settings

Only AWS CloudFormation may write under `/<project>/<env>/layers/sap/`:

- The check trusts an SAP access claim, or the check's settings, only when a stack deploy wrote
  the latest version.
- No role that the stacks define may write there.
- Any other write is reported to the platform's operations topic. This needs an AWS CloudTrail
  trail that logs management events in the account.

To restore what the stacks define, with no template edit:

```bash
./scripts/deploy.sh layer run sap rewrite            # every stack that owns a claim, and the layer
./scripts/deploy.sh layer run sap rewrite <stack>    # also a stack whose claim was deleted
```

The same rule works as an emergency stop for one use case. Overwrite its SAP access claim by
hand. Within the check's 60-second cache time, every SAP call from its apps is refused, reads
included. `rewrite` restores the SAP access claim. A use case's own break-glass page can list this step
in order with its other steps ([Use cases](../../../use-cases/README.md)).
