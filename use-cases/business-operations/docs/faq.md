# FAQ

Short answers about SAP exception handling, each with the page that has the detail. Questions
about the platform, the SAP layer, support and data handling are in the
[platform FAQ](../../../docs/start/faq.md).

## Cost

**What does a case cost?**
You pay for the AWS services it uses. Most of the bill is the Amazon Bedrock model calls that
each case makes. Most of the rest is serverless and bills per request. Measure both on a
development deployment before you size production.
[Measure your own cost](operating/cost.md#measuring-and-sizing-cost)

## SAP

**Does it work with SAP ECC?**
The shipped pack reads services that SAP S/4HANA publishes. On SAP ECC, the services a pack
needs are built with the classic approach (transaction SEGW).
[SAP Gateway deployment and OData version](../../../layers/sap/sap-setup.md#sap-gateway-deployment-and-odata-version)

**Can the agent change SAP on its own?**
Yes, but only when all three of these are true:

- The change is to the case's own document.
- The change is within the limit you set for each pack.
- Someone has started the case, or the trigger mode is `auto` for both the deployment and the
  case's pack.

Anything above the limit waits for an approver. Deletes are never possible.
[Controls and evidence](controls-and-evidence.md)

**Who made a change in SAP?**
By default, SAP's logs name the service user of the SAP system. The case record links each
change to the run and the approver.
[Controls and evidence](controls-and-evidence.md)

**Can it work with two SAP systems at once?**
Yes. Each pack names the system it works with. This has not run live yet, so test it on your first deploy.
[Add a second SAP system](../../../layers/sap/add-a-system.md)

## Changes

**Can we use ServiceNow or Jira instead of the sample tickets?**
The sample ticketing only stands in for your IT service management tool. Replace it before
production.
[Production checklist](production-checklist.md)

**Who can change what the agent does?**
Pack authors change the wording of a standard operating procedure (SOP) and the value of a
tolerance in the console of a development deployment. The change reaches quality and
production with the pack's next published version. Contacts, and a lowered write limit, are
set per deployment. Only a pack administrator can change what a pack reads or writes in SAP.
[Process owner guide](for-process-owners.md#change-permissions)

**How do we automate another SAP process?**
Bring your developer the SAP document, the exception types and the procedure for each type.
The developer runs the `dev-new-sap-process` skill with you.
[Process owner guide](for-process-owners.md#automating-another-process)

## Removal

**How do we remove it?**
Run `./scripts/deploy.sh destroy`. Then delete what the command does not own: each SAP
system's secret and, under `deployment.mode: production`, the data that it keeps on purpose.
To keep the platform and remove only this use case, destroy its stacks in order.
[Removal](operating/removal.md)
