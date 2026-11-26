<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Break glass

Use this page to stop agent writes to SAP now. The steps go from the narrowest effect to the
widest. Stop at the first step that is enough. Each step says how to check that it worked and how
to undo it.

Under `deployment.mode: production`, `operator-policy trigger-mode` covers step 1 and the
[emergency stop](#emergency-stop) only. Run steps 2 to 4 as a deployment principal. Keep a copy
of this page, and your own on-call and SAP administrator contacts, somewhere that does not depend
on this deployment.

Start when you see one of these:

- a containment message on the ops topic;
- writes in SAP that no case explains;
- the business process agent doing something that its standard operating procedure (SOP) does
  not say.

`<own>` below is the AWS Systems Manager (SSM) parameter path
`/<project>/<env>/use-cases/business-operations`.

An admin can also stop everything from the console or the API in one call, before these steps or
together with them. The [emergency stop](#emergency-stop) sets the trigger mode to `manual` and
stops every run under way.

| Step | Stops | Works under |
|---|---|---|
| [1. Trigger mode `manual`](#1-trigger-mode-manual) | Unattended runs starting | Any Cedar mode |
| [2. Stop every run from starting](#2-stop-every-run-from-starting) | Every new run, console runs included | Any Cedar mode |
| [3. Deny SAP writes in Cedar](#3-deny-sap-writes-in-cedar) | SAP writes, for every use case on those targets | `ENFORCE` only |
| [4. The SAP layer refuses this use case](#4-the-sap-layer-refuses-this-use-case) | Every SAP call from this use case, reads too | Any Cedar mode |
| [5. The SAP side](#5-the-sap-side) | The SAP user, for every caller | Always |

## 1. Trigger mode `manual`

Containment does this on its own alarms.

```bash
./scripts/deploy.sh usecase run business-operations trigger-mode set manual
```

Check: `trigger-mode` prints `trigger-mode = manual`. The packs' own modes are left as they were,
so undoing this resumes exactly the packs that were on. Undo: `trigger-mode set auto` (asks
first). This step stops unattended runs from starting, and nothing else.

## 2. Stop every run from starting

```bash
./scripts/deploy.sh usecase run business-operations break-glass
```

The command does three things:

1. It does step 1.
2. It disables the invoker's event source mapping on the agent queue. A queued case waits there,
   for up to seven days, and is not dead-lettered.
3. It sets the invoker's reserved concurrency to 0.

The invoker is the only caller of the agent runtime, so no run starts, console runs included.

Check, with the invoker's name from SSM `<own>/invoker-function`:
`aws lambda get-function-concurrency --function-name <invoker>` shows `0`, and
`aws lambda list-event-source-mappings --function-name <invoker> --query 'EventSourceMappings[].State'`
shows `Disabled`.

Undo: `break-glass --undo`, which leaves the trigger mode `manual`. A run already under way
finishes within the invoker's 15 minutes, unless the [emergency stop](#emergency-stop) ended it.
Steps 3 to 5 stop its writes.

## 3. Deny SAP writes in Cedar

This step works only under `security.cedar.mode: ENFORCE`, and takes effect within seconds. One
`forbid` overrides every permit, so it stops SAP writes for every use case on those targets, not
only this one. Reads continue.

List each system's writes that are on. They are in the SSM parameter
`/<project>/<env>/layers/sap/systems`, and a write tool is `sap-<system>___odata_create`,
`___odata_update` or `___odata_function_import`. List only those, because CreatePolicy refuses an
action that the target does not list. Write `forbid.json`:

```json
{"cedar": {"statement": "forbid(principal, action in [AgentCore::Action::\"sap-s4___odata_create\", AgentCore::Action::\"sap-s4___odata_update\", AgentCore::Action::\"sap-s4___odata_function_import\"], resource == AgentCore::Gateway::\"arn:aws:bedrock-agentcore:<region>:<account>:gateway/<gateway id>\");"}}
```

Then create it on the platform's policy engine. The engine id is in SSM at
`/<project>/<env>/gateway/policy-engine-id`, and the gateway id at `/<project>/<env>/gateway/id`.
You choose the name: letters, digits and underscores, unique in the account and Region.

```bash
aws bedrock-agentcore-control create-policy --policy-engine-id <engine id> \
  --name break_glass_sap_writes --definition file://forbid.json \
  --validation-mode IGNORE_ALL_FINDINGS
```

Check: `aws bedrock-agentcore-control list-policies --policy-engine-id <engine id>` lists it
`ACTIVE`, and an SAP write tool call answers with an authorization error. Undo:
`aws bedrock-agentcore-control delete-policy --policy-engine-id <engine id> --policy-id <id>`.

The lasting form, for this use case only, is `layers.sap.access.business-operations.write: {}`
in `platform.yaml` and a deploy of `<project>-<env>-uc-business-operations-sap`. The deploy
removes the use case's write permits and narrows its SAP access claim. This path has not been
tested against a deployed policy engine.

## 4. The SAP layer refuses this use case

This step refuses reads too, in any Cedar mode. Write the use case's SAP access claim by hand:

```bash
aws ssm put-parameter --name /<project>/<env>/layers/sap/claims/business-operations \
  --type String --overwrite --value '{"break_glass": true}'
```

The layer's check trusts an SAP access claim only when CloudFormation wrote it. So within the
check's 60-second cache time, every SAP call from this use case's apps is refused. The layer's
write alert reaches the ops topic.

Check: the agent's SAP calls answer `Refused by the Gateway: no use case claims SAP access for
this client`. Undo: `./scripts/deploy.sh layer run sap rewrite`.

To turn writes off at the AWS for SAP MCP Server for every caller of one system, set
`layers.sap.systems.<system>.writes: []` and deploy the layer
([SAP layer walkthrough](../../../../layers/sap/walkthrough.md#writes-by-flow)). To undo it,
restore `writes` and deploy again.

## 5. The SAP side

Your SAP Basis administrator locks the user that SAP uses for these calls:

- on `basic`, the service user (in SU01, the SAP user maintenance transaction: Lock);
- on `m2m-sap`, the OAuth client's user;
- on `m2m-oidc` or `obo` (principal propagation), the user the token maps to. Or the identity
  provider (IdP) administrator disables the app.

This repository cannot do this step for you. It stops that user for every caller, people
included.

Check: an SAP call answers 401. Undo: unlock the user in SU01.

## Emergency stop

Start the stop with `POST /deployment/stop` and a `reason`. It is for `business-operations-admins`,
like the deployment's trigger mode. Or run
`./scripts/deploy.sh usecase run business-operations emergency-stop "<reason>"` with AWS
credentials. Once the stop is started, nothing new starts until an admin clears it. In one call,
the stop:

1. records the stop in SSM at `<own>/autonomy/emergency-stop`, which the audit-change alert
   watches. From then on, every path that starts a run refuses and names the stop:
   - Process, a question, the briefing and a rehearsal (`POST /runs`);
   - **Start *n* runs** on **Cases**;
   - a decision or ticket action that would resume a run;
   - the poller, SAP push, the sweeper and `rerun`;
2. sets the deployment's trigger mode to `manual`, every time, so the audit-change alert on the
   trigger mode reaches the ops topic for every stop;
3. pauses the agent queue's consumer, so queued work waits on the queue (up to seven days)
   instead of starting or being dead-lettered;
4. revokes every unexpired run grant. Then the run checks refuse every later Gateway call of
   every run under way, SAP reads and writes, case updates and notifications included;
5. for each run on a case, stops its runtime session and releases the case. A case that reads
   `processing` is set to `manual_review_required` with the reason, so a queued run is not
   started again when the queue delivers it again.

`GET /deployment/settings` returns the last stop as `last_stop`: who, when, why and each run
with what each step did for it. Earlier stops are in the stop function's log (`[STOP]`) and in
the trigger mode's parameter history.

What it leaves:

- **A write already made.** An SAP write that passed the run checks before its grant was revoked
  is in SAP, and its run may not have recorded it on the case. Check SAP for each stopped case
  before you start it again.
- **A run that ignores the stop.** If the runtime does not stop the session, the run keeps
  calling the model until its turns, its budget or the invoker's 15 minutes end it. Every Gateway
  call it makes is refused.
- **Questions too.** A question cannot write. But it reads SAP and runs the model that the stop
  was started for, so it is refused like every other start. People can still read cases.
- **No mode turns to `auto`.** `PUT /deployment/trigger-mode`, a pack's `PUT .../trigger-mode` and
  `trigger-mode set auto` refuse while the stop stands, and name the stop. Setting a mode to
  `manual` is still allowed.
- **`verify` still runs.** Its probe grants are not runs: they name the read client and no case,
  and expire in two minutes. So `./scripts/deploy.sh verify` passes during a stop. You can use it
  to check that the controls still refuse what they should.
- **A message the invoker had already received.** It is returned to the queue, and its case is
  marked `held_by_stop`, so its next delivery runs as new. That uses one of its three receives.
  If pausing the consumer failed (`last_stop.queue_held: false`), each queued message is returned
  at every delivery and is dead-lettered after three. Send the dead-letter queue's messages again
  after the clear (`rerun --dlq`).

Check: `last_stop.stands`, `queue_held` and `trigger_mode_set` are true and every run's
`session_stopped` and `released` are; `trigger-mode` prints `trigger-mode = manual`; a Process
attempt answers `An emergency stop stands`.

Clear: `POST /deployment/stop/clear` with a `reason`, admins only, or
`emergency-stop --clear "<reason>"`. The clear does three things:

- It writes the stop as `stands: false`, with who, when and why. The audit-change alert fires.
- It resumes the consumer.
- It leaves the trigger mode `manual`. Queued work runs, nothing new starts by itself, and a
  stopped case waits for a person.

There is nothing to undo for the stopped runs. Start a stopped case again by hand once you know
what happened, and run `trigger-mode set auto` last.

## Return and rehearsal

Return in the reverse order, once you know what happened and have fixed it. `trigger-mode set auto`
comes last, and only by choice.

Rehearse the steps on a development stack: a tabletop pass each quarter, and a real one (steps
1, 2 and 4, then their undo) twice a year.
