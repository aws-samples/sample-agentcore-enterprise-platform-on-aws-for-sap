# SAP events bus

How SAP puts events on the layer's `sap-events` bus and how a use case subscribes to them.

Polling is enough for most use cases. Sometimes a use case needs a case sooner than its poll
interval. Then SAP can put an event on the layer's one `sap-events` bus with the AWS SDK for SAP
ABAP (SAP's programming language). The layer sends the event to that use case's own queue.

The use case decides what its events contain. For the SAP exception handling use case
(`business-operations`), an event is a nudge: it names document keys, and the use case responds
by reading those documents from SAP. So the SAP side needs no change for each use case, except
the call that records the event.

```yaml
layers:
  sap:
    systems:
      s4:
        # ... as in the walkthrough
        senders: [arn:aws:iam::<sap-account>:role/<sap-host-instance-role>]
    access:
      <use case>: {read: "*", bypass_gateway: [s4], ingest: [s4]}
```

- **`systems.<system>.senders`** are the principals that may assume the system's **sender
  role**. A sender is the SAP host's instance role on Amazon EC2, or an IAM Roles Anywhere role
  on premises (IAM is AWS Identity and Access Management). Each sender must be an IAM role ARN.
  It must never be:
  - an IAM user, because its access keys would stay on the SAP host permanently
  - an account
  - an IAM Identity Center role, because that role is a person
- **`access.<use case>.ingest`** is the upper limit: the systems whose events that use case may
  subscribe to. Config load refuses a system in `ingest` that has no `senders`. It also refuses a
  system that is missing from the same access row's `bypass_gateway`, because the use case
  responds to an event by reading SAP directly.

## Bus and sender roles

When any system has `senders`, the layer builds the following. When none has, it builds
nothing.

- **The bus**, `<project>-<env>-sap-events`. It is a custom Amazon EventBridge event bus, with
  classic rules and targets. Its resource policy contains only deny statements:
  - It denies `events:PutEvents` to every principal except the sender roles
    (`aws:PrincipalArn`). So nobody else in the account, an administrator included, can put an
    SAP system's event on the bus.
  - It denies each sender role any source except its own, and any topic outside its use cases'
    topics.

  The policy contains no allow statement. In the bus's own account, an allow statement there
  grants `PutEvents` by itself and would bypass the conditions below. This was measured on a
  deployed bus, where an allow statement let the sender role put any source and any topic.
- **One sender role per system** that has `senders`. The role trusts exactly those senders and
  has one permission: `events:PutEvents` on the bus, with `ForAllValues:StringEquals` on two
  keys:
  - `events:source` = `sap.<system>`
  - `events:detail-type` in the use cases whose `ingest` names the system

  Each of the two keys must also be present (`Null: false`). Without that, an empty set would
  pass.

  A system that no use case takes events from yet gets a role that can put nothing.
- **Parameters** in SSM Parameter Store (AWS Systems Manager): `layers/sap/bus-arn`,
  `layers/sap/systems/<system>/sender-role-arn` and `layers/sap/ingest`.

Every event has `source` set to `sap.<system>`, `detail-type` set to the use case's name, and
`detail` set to its body.

## Subscriptions

A use case subscribes from its own stack:

```python
sap.subscribe(stack, queue=queue, dlq=dlq, systems=["s4"])
```

That call creates one rule on the bus, `{"source": ["sap.s4"], "detail-type": ["<use case>"]}`.
The rule's target is `queue`, and `dlq` is the target's dead-letter queue (DLQ). The EventBridge
retry is set explicitly: 185 attempts over 24 hours. The call also adds `sqs:SendMessage` for
`events.amazonaws.com` to each queue's policy, with `aws:SourceArn` = that rule.

The `detail-type` is always the use case's own name, never an argument, so no use case can
subscribe to another use case's events. `subscribe` also refuses, at synth, a system outside the
use case's `ingest` limit. (Synth is the step where the AWS Cloud Development Kit, CDK, builds the
templates.)

`subscribe` also publishes `layers/sap/subscriptions/<use case>`: the rule's ARN, its one queue
and its one DLQ. `verify` compares every rule on the bus with these values. A stack might put its
own rule on another use case's topic, or a second target on its rule. `subscribe` does not check
such a stack, but `verify` fails it.

Both queues must be defined in the subscribing stack, and `dlq` must be a standard queue in the
same Region. EventBridge cannot deliver to a queue that is encrypted with an AWS owned key. For a
queue on a customer managed AWS KMS (Key Management Service) key, the key policy must let
`events.amazonaws.com` use `kms:Decrypt` and `kms:GenerateDataKey`.

- `subscribe` adds that permission, for this account's rules only (`aws:SourceAccount`), when the
  key is the queue's `encryption_master_key` and is defined in the same stack.
- `subscribe` cannot add it when another stack owns the key's policy, such as the platform's key.

The use case owns its queues, its key and its consumer. It also owns the alarms on its rule's
`FailedInvocations` and on its DLQ.

## SAP outbox

The SAP side is a generic outbox in [`abap/`](../abap/): table `ZAWS_EVT_OUTBOX`, class
`ZCL_AWS_EVT_OUTBOX` and report `ZAWS_EVT_DRAIN`.

A use case's BAdI (Business Add-In) implementation calls
`zcl_aws_evt_outbox=>record( iv_topic = '<use case>' iv_body = ... )`. The call is one INSERT in
the update task. The send job (report `ZAWS_EVT_DRAIN`) runs every minute. Each `PutEvents` call
sends at most ten rows of one topic. The job marks each row with its own result. It sends a
row at most five times in all, then leaves it at `E`.

The class's header describes the setup:

- the SDK's core and EventBridge transports
- the `/AWS1/IMG` profile `ZAWS_EVT`, mapped to the system's sender role
- the TVARVC variables `ZAWS_EVT_BUS_ARN` and `ZAWS_EVT_SYSTEM`
- the job user's role

A second use case on the same system changes nothing in SAP except its own BAdI. The operator
adds the system to that use case's `ingest`. On the next layer deploy, this adds the use case's
topic to the sender role. These objects are samples: activate and test them on your own system.

## Known limitations

- **A sender role trusts only `sts:AssumeRole`.** Its trust policy allows no
  `sts:SetSourceIdentity` and no `sts:TagSession`. If the AWS SDK for SAP ABAP sets a source
  identity or session tags when it assumes the role, the call fails with `AccessDenied`. This
  has not been tested on a live SAP host. On the first activation, check that the SAP host can
  assume the role, and add the action if it cannot.
- **A sender is trusted as a whole role.** If several hosts share one instance role, every one
  of those hosts can put events for the system. The trust policy has no condition on the
  instance (`ec2:SourceInstanceArn`). Give the SAP host its own instance role, and list only
  that role in `senders`.
