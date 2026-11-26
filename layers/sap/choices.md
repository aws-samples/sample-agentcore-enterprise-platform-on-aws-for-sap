# Gateway, direct Lambda, or self-hosted MCP

Terms are defined in [Concepts](../../docs/start/concepts.md).

**How should a tool reach the agent?** This repository makes one design choice here. The
business process agent reaches every SAP and use-case tool through the AgentCore Gateway. Each
tool is a Lambda target or an MCP (Model Context Protocol) server registered as a target. The
agent does not call Lambda functions directly, and it does not run its own MCP process.

The Gateway fits an agent that needs managed sign-in, authorization on every call and tracing,
because the Gateway provides all three and nobody has to write them. It does not fit a
high-volume batch with fixed steps, because the MCP protocol adds nothing there.

## Comparison

| Dimension | Direct Lambda | Gateway (Lambda or MCP targets) | Self-hosted MCP server |
|---|---|---|---|
| Token overhead | Lowest: tool definition and response only | Low: the MCP framing on each call | Highest: session setup, discovery and framing |
| Latency | One Lambda invoke | A Gateway hop plus the target | Low once warm; session setup adds to the first call |
| Inbound auth | IAM (AWS Identity and Access Management), SigV4 signing | OAuth2 with a JWT (JSON Web Token), validated by the Gateway | You implement it |
| Per-call authorization | Your code | Cedar on the Gateway's policy engine, plus interceptor checks | Your code |
| Observability | CloudWatch per Lambda | Gateway logs and traces, plus CloudWatch per target | You implement it |
| MCP spec coverage | Not MCP | `tools/list` and `tools/call` | The full spec (resources, prompts, sampling) |
| Cross-tool state | Isolated per Lambda | Isolated per target | Shared in-process |
| Local development | Invoke the function | Deploy, or mock the Gateway | One process, easy to debug |
| Protocol code | None | None: the Gateway handles it | You implement JSON-RPC |

The AgentCore Gateway can also search its tools by meaning for the model. This repository does
not turn that on.

## Selection guidance

**Direct Lambda** fits:

- high-volume work with fixed steps, such as a month-end batch
- workloads where the token overhead matters
- a small number of tools that need no discovery
- AWS Step Functions orchestration, where Lambda is a built-in integration

**Gateway** fits:

- agents that need managed sign-in, tracing and authorization
- tool catalogs that grow
- tools that several agents or use cases share
- MCP compatibility without implementing the protocol

**Self-hosted MCP server** fits:

- tools that need state shared in one process, such as connection pools or caches
- the parts of the MCP specification the Gateway does not serve
- exploring an unfamiliar system while you design

## This repository's default

Every SAP and use-case tool the agents call goes through the Gateway. A few tools run in the
agent's own process instead:

- Code Interpreter, in the platform's agent patterns
- the skills tool of the SAP exception-handling agent, on its chat path. It reads standard
  operating procedures (SOPs) from Amazon S3.

The platform's own Gateway tools are Lambda targets. So are each use case's tools: case state,
notifications, procedure loading and knowledge search. SAP is reached through the AWS for SAP
MCP Server. The SAP layer runs one server runtime per SAP system and registers it as the target
`sap-<system>`. An SAP call therefore gets the same OAuth sign-in, Cedar policy and interceptor
checks as any other tool. It also gets the SAP layer's check of the SAP access claim.

There is one direct path, and it is deliberate. Three parts of the SAP exception-handling use
case call SAP over OData (Open Data Protocol) with the system's service account, outside the
Gateway:

- the poller
- the consumer of the SAP ingest queue
- the test-data seeder

They go outside the Gateway because no model and no run is behind their calls.
The [SAP layer walkthrough](walkthrough.md) describes how a
use case is given that path (`bypass_gateway`), and [Add gateway tools](../../docs/GATEWAY_TARGETS.md)
describes how to add a target.
