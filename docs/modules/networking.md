<!-- markdownlint-disable MD041 -->

# Networking module

The `networking` stack (`stacks/networking_stack.py`) is the platform's
optional virtual private cloud (VPC): private subnets for the AgentCore runtime network interfaces,
interface endpoints for the AWS services the agents call, and a
least-privilege security group. Enabling it does two things at once — it
creates the VPC, *and* `app.py` places every runtime in it. This is not an
air-gapped network: the private subnets keep a network address translation
(NAT) route because agents call
AWS APIs and some patterns use the public internet.

## When it deploys

| Path | Setting |
|---|---|
| `platform.yaml` | `security.networking: true` |
| Env var / AWS Cloud Development Kit (CDK) context | `ENABLE_NETWORKING=true` / `-c enable_networking=true` |
| Guided workshop | Module C |
| Preset | `security-focused` and `platform-team` enable it |

Off by default.

## What it creates

| Resource | Details |
|---|---|
| VPC | `10.0.0.0/16` (or `security.vpc.cidr`), max 2 Availability Zones (AZs), 1 NAT gateway (2 with `egress: nat_per_az`); none of it with an imported `vpc_id` |
| Subnets | One public + one private (`PRIVATE_WITH_EGRESS`) `/24` per AZ |
| Runtime security group | No inbound rules; egress limited to TCP 443 (every dependency — Amazon Bedrock, AgentCore, Secrets Manager, ECR, CloudWatch Logs — is HTTPS) |
| Interface endpoints | Amazon Bedrock Runtime, Amazon Elastic Container Registry (ECR) (API + Docker), CloudWatch Logs, AgentCore Gateway (`com.amazonaws.<region>.bedrock-agentcore.gateway`) |
| Gateway endpoint | S3 (free; ECR image layers are stored in S3) |
| Endpoint policy | On the AgentCore endpoint, org-scoped via `control-library/vpce/agentcore-in-org.json` — only when `org_id` is set |
| AWS Systems Manager (SSM) parameters | `/<project>/<env>/networking/{vpc-id,private-subnet-ids,runtime-security-group-id}` plus `migration-dependency-probe-name` when private migration dependencies are declared |
| Outputs | `VpcId`, `PrivateSubnetIds`, `RuntimeSecurityGroupId` |

## Configuration

| Key (platform.yaml) | Env var | Default | Effect |
|---|---|---|---|
| `security.networking` | `ENABLE_NETWORKING` | `false` | Creates the stack and puts runtimes in VPC mode |
| `security.org_id` | `ORG_ID` | *(empty)* | Renders the org-scoped endpoint policy; **without it the AgentCore endpoint is created with no policy at all** |
| `security.vpc.cidr` | — | `10.0.0.0/16` | The created VPC's range, `/16` to `/22`. Pick a range your network does not already route (RISE with SAP needs ranges that do not overlap) |
| `security.vpc.egress` | — | `nat` | `nat_per_az` adds a second NAT gateway, so one AZ failing does not cut the other's egress |
| `security.vpc.vpc_id`, `private_subnet_ids` | — | *(empty)* | Use an existing VPC instead of creating one (below) |
| `security.vpc.endpoints` | — | `core` | `none` (imported VPC only) when your network team runs central interface endpoints |

### Using your own VPC

With `vpc_id`, `private_subnet_ids` (at least two, in different AZs) and the VPC's
`cidr`, the stack creates no VPC, subnet, NAT, route or gateway endpoint. It still
creates the runtime security group and the interface endpoints in your VPC, and
publishes the same SSM parameters and outputs, so everything placed from them is
unchanged. Routes, NAT and the S3 gateway endpoint stay yours. `preflight` checks the
subnets' AZ IDs before deploy, and `check_network.py` checks after it that they are
private (no `0.0.0.0/0` route to an internet gateway) and warns when the VPC has no S3
gateway endpoint. Choose the mode and CIDR at first deploy: changing either later
replaces the VPC, and AgentCore's network interfaces can keep using the old subnets for hours.

## How runtimes opt in

With the stack enabled, `app.py` builds a `runtime_network` dict — `VPC` mode,
the private subnet IDs, and the runtime security group. It passes the dict to
every `RuntimeStack` (orchestrator and both A2A agents), and each of them also
takes a stack dependency on `networking`. The property shape is produced by
`infra_utils/runtime_network.py::build_network_config`, which exists to
prevent two historic defects:

- **The wrong property shape.** `CfnRuntime` wants
  `networkModeConfig: {subnets, securityGroups}`; an invented key passes
  synth and is ignored or rejected by CloudFormation.
- **Fallback to public, without an error.** Asking for VPC mode without subnets used to
  render `{"networkMode": "VPC"}` and nothing else — a "network isolated"
  deployment whose agents all ran on the public network. The helper raises
  `ValueError` instead.

Without the stack, runtimes deploy with `networkMode: PUBLIC` (the AgentCore
data plane still authenticates every call; "public" is the egress path, not
an open inbound endpoint).

## Interfaces

- The SSM parameters are the published interface: `scripts/check_network.py`
  and the module C verification read them instead of parsing CloudFormation
  outputs. Within a single synth, `app.py` connects the stack attributes
  directly.

## Security notes

- The runtime security group is the choke point for agent egress: HTTPS only,
  no inbound. Callers reach agents through the AgentCore data plane, never
  through the VPC.
- The endpoint policy constrains **SigV4 callers only**. OAuth/JSON Web Token
  (JWT) callers have no AWS Identity and Access Management (IAM) principal and pass via `Principal: "*"` — a VPC endpoint
  policy cannot constrain them (see item 2 in
  [Security controls](../SECURITY_CONTROLS.md)).
- Private subnets are the only valid placement: a public subnet gives
  AgentCore elastic network interfaces (ENIs) no internet route, so the runtimes
  would lose Amazon Bedrock access.
- To remove the NAT you must first add endpoints for *every* service your
  agents call; the shipped endpoint set is not exhaustive for all patterns.

## Verification

```bash
python scripts/check_network.py                  # expect VPC mode
python scripts/check_network.py --expect-public  # deployments without networking
```

Two live checks:

1. The private subnets are in Availability Zones where AgentCore can actually
   place interfaces.
2. Every deployed runtime reports `networkMode: VPC` in those subnets. This
   check would have caught the original "VPC built, agents still public" defect.
`scripts/verify.py` includes it automatically whenever `networking` is in the
deployment contract, and module C runs it as its verification.

## Known issues

- **Hourly cost.** The NAT gateway and each interface endpoint bill per hour
  whether or not agents run. The S3 gateway endpoint is free — without it,
  ECR layer pulls would bill as NAT data processing.
- **AZ IDs, not AZ names.** AgentCore supports VPC connectivity only in
  specific AZ *IDs* per region (three per region — see `SUPPORTED_ZONE_IDS`
  in `infra_utils/runtime_network.py`). An AZ *name* like `us-east-1a`
  maps to a different AZ *ID* in every account. A VPC that works in one
  account can fail in the next, with an opaque error at runtime creation,
  modules later. `check_network.py` catches it early; fix it by limiting the VPC
  to supported zones (`aws ec2 describe-availability-zones` shows the
  mapping). An unknown region returns no restriction — a stale table should
  not block a region AgentCore has since added.
- **ENIs remain after leaving VPC mode.** AgentCore `agentic_ai` network
  interfaces can remain for up to ~8 hours after a runtime stops using VPC
  mode. Destroying this stack can therefore fail on the subnets and security
  group.
  The NAT gateway and endpoints (the hourly charges) are deleted in the same
  run. Retry the destroy after the interfaces expire:
  `aws ec2 describe-network-interfaces --filters Name=interface-type,Values=agentic_ai`.
- **A missing `org_id` gives no error.** Unlike the memory resource policy (which fails
  without it), forgetting `org_id` here ships the AgentCore endpoint with no
  policy — no error, no warning from CDK.
