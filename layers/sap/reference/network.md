# SAP network paths

SAP S/4HANA Cloud, SAP Business Technology Platform (BTP) and RISE with SAP. The route each kind
of SAP needs is in the walkthrough's [network table](../walkthrough.md#network-paths-to-sap).

The business process agent, the Gateway and every function stay on AWS. SAP is only the OData
(Open Data Protocol) endpoint, and nothing is deployed on SAP BTP. For SAP S/4HANA Cloud or a
public BTP endpoint, set `base_url` to that endpoint (for example
`https://my-api.s4hana.ondemand.com/sap/opu/odata/sap/`). For an identity per person there, use
`obo` (principal propagation). It needs no BTP hosting.

Moving the agent itself onto BTP (Kyma or Cloud Foundry) is not covered. It would mean replacing
the AgentCore Gateway, Memory and Identity, and the rest of the platform. This sample connects to
SAP over a private AWS route. It does not use SAP Cloud Connector, which is for reaching SAP from
SAP BTP.

## RISE with SAP

RISE with SAP runs SAP in an AWS account that SAP manages, so SAP Enterprise Cloud Services
(ECS) makes the SAP-side changes through a service request. Ask ECS for:

1. **An AWS Transit Gateway attachment, or VPC peering,** between the platform VPC (virtual
   private cloud) and the RISE VPC. Their CIDR (IP address) ranges must not overlap. Choose the
   platform's range with `security.vpc.cidr`. Or deploy into a VPC that is already on your
   Transit Gateway, with `security.vpc.vpc_id`
   ([networking module](../../../docs/modules/networking.md#using-your-own-vpc)), so that you
   keep control of the routes. A Transit Gateway that you already share across accounts through
   AWS Resource Access Manager (RAM) works too.
2. **The platform's private subnets allowed** by the RISE security groups on the HTTPS port that
   `base_url` names. This is usually the SAP Web Dispatcher's port.
3. **A certificate from a publicly trusted certificate authority (CA)** on that endpoint. The AWS
   for SAP MCP (Model Context Protocol) Server verifies it ([threat model](../threat-model.md),
   SAP-12).
4. **The IP addresses of the DNS servers for the RISE domain.**

Then, in your network account:

- Route the RISE VPC's CIDR from the platform's private subnets to the attachment or the
  peering connection.
- Create an Amazon Route 53 Resolver outbound endpoint in the platform VPC. Give it a forwarding
  rule that sends the RISE domain to those DNS servers, so that `base_url`'s hostname resolves
  from the private subnets. Without it, the server and the direct callers fail on name
  resolution, not on a timeout.

The layer creates none of these. They depend on how your network account is organized, and the
networking stack owns the platform's route tables. Set `base_url` to the hostname that the
certificate names, never to an IP address.
