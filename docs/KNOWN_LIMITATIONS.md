# v0.1.0 Known Limitations

Review these before an AWS Experience-Based Acceleration (EBA) workshop or a customer deployment.

1. **This is a sample baseline, not an AWS service or production
   certification.** The production profile has automated synthesis coverage.
   It has no completed customer threat model, operational readiness review,
   backup/restore rehearsal, or production launch approval. No stack has
   run in production mode yet, so CloudTrail delivery to the trail bucket
   under the platform key is unproven live. After the first production
   deploy, run `aws cloudtrail get-trail-status` and check that
   `LatestDeliveryError` is empty.
2. **Release evidence is narrower than configuration breadth.** The strongest
   live evidence is in `us-east-1`. Direct Entra ID, brokered Okta/Ping,
   distributed deployments, and other Regions require customer-specific live
   validation. IdP group mapping (`identity.group_claim`) is also unproven
   live. Two things are not tested: the string form in which Cognito stores
   an array claim, and Entra's group overage, where a user in more than 200
   groups gets no `groups` claim. A wrong parse can only miss a group; it
   never grants one. Sign in as a federated user with groups and check the
   token before you rely on group-based access.
3. **Model metadata is not model invocation.** `deploy.sh doctor` checks that
   the configured model or inference profile is discoverable without incurring
   inference cost. Account entitlement, Marketplace prerequisites, runtime
   AWS Identity and Access Management (IAM) permissions, and quota are proven only by a successful post-build invocation.
4. **A private pre-built migration image cannot always be inspected.** The
   customer must prove a `linux/arm64` manifest, refer to the image by an immutable digest, and
   provide registry access where required. Source builds performed by the
   accelerator target arm64.
5. **Migration automation stops at the target runtime.** Customer data,
   existing private connectivity, DNS/private certificate authority (CA), event sources, traffic
   routers, cutover, and rollback execution are not modified. Readiness
   commands validate evidence and approval records only.
6. **No generic data-copy or trigger executor exists.** The built-in
   `retain-source-v1` adapter moves no records. Queue shadow consumers are
   specifically unsafe because they can steal production messages.
7. **Production supply-chain controls are incomplete.** Images are not yet
   signed, and no release software bill of materials (SBOM) or provenance bundle is published.
   Immutable promotion of one artifact through dev, staging, and production is not
   implemented.
8. **AI quality evaluation is outside the current verifier.** There is no
   versioned customer evaluation set, prompt-injection campaign, quality
   threshold, or per-agent inference cost attribution.
9. **Some security controls require first-deployment calibration.** Exercise
   Cedar in `LOG_ONLY` before enforcing it. For production, publish a numbered Amazon Bedrock
   Guardrail version and use that version. Verify interceptor event shapes
   against each live Gateway target.
10. **VPC mode is not air-gapped.** The default private subnets retain a NAT
    route for APIs and internet dependencies, and NAT gateways plus interface
    endpoints incur hourly cost. AgentCore network interfaces can delay
    networking-stack removal.
11. **Facilitator and accessibility qualification is incomplete.** The EBA
    flow has not yet completed three clean-account rehearsals by independent
    facilitators or a full keyboard, screen-reader, stale-state, and fallback
    review.
12. **The manifest schema has no independent version field yet.** Use a fixed
    accelerator release (a tag) with each manifest. Review generated `platform.yaml`
    reference changes during upgrades. Run `design` plus `diff` before
    applying a newer tag.

Track the remaining production work with the
[production readiness checklist](PRODUCTION_READINESS_PLAN.md) and use the
[support matrix](SUPPORT_MATRIX.md) for current evidence.
