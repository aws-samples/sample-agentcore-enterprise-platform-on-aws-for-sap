# Production readiness checklist

This page lists the work and evidence to complete before you run the platform in production.
The platform is sample code. It gives you secure defaults, explicit configuration and tests for
the controls you select. It does not by itself make your application ready for production. Your
organization still decides its data classification, threat model, service levels, AI quality
thresholds, compliance controls and recovery objectives, and approves the evidence.

Each use case and layer you deploy may have its own production checklist. Complete those as
well. This page covers the platform they run on.

The sections are gates, G1 to G6: review points that the templates under `docs/` refer to. A gate closes when
its evidence is stored with the release and the accountable role has reviewed it, not when code
merges.

## Start from the production mode

`platform.yaml` has two deployment modes:

| Mode | Purpose | Resource lifecycle | Security posture |
|---|---|---|---|
| `workshop` | Disposable learning and evaluation environment | Fast creation and clean removal | Safe for exercises, with documented limitations |
| `production` | Long-lived platform | Retained data, controlled changes, backups and recovery | Secure defaults; disabling a required control needs an approved exception |

Start from `presets/production.yaml` (`deploy.sh design --profile production`). Validation rejects
insecure combinations in production mode, such as Cedar in any mode other than `ENFORCE`. A
workshop deployment is not evidence that a production deployment is ready.

## Definition of done

A release is ready for production when all of these are true:

1. No credential or sensitive payload appears in CloudFormation outputs, generated templates,
   dashboard files, browser state, logs or CI artifacts.
2. Sign-in uses your identity provider, public self-signup is off, and a real end-user sign-in
   and authorization flow has been tested.
3. AWS Identity and Access Management (IAM), model access, tool access, network access and encryption controls are enforced and
   tested against the live deployment.
4. Agent quality and AI security have measurable release thresholds.
5. Every capability you rely on is tested through the deployed agent: identity, tools, memory,
   traces, guardrails and alarms.
6. Service levels, alarms, runbooks, rollback, backup and recovery are approved.
7. Builds are reproducible, dependencies are locked, artifacts are scanned, and CI failures block
   promotion.
8. A fresh-account deployment, upgrade, rollback and removal have passed in a production-like
   staging environment.
9. Security, platform engineering, operations and the product owner have approved the evidence.

## G1: Design and ownership

- [ ] **Threat model.** Cover user to agent, agent to model, agent to tool, memory, third-party
      OAuth, continuous integration and delivery (CI/CD) and administrators. Include prompt injection, confused deputy, excessive
      agency, data exfiltration, access across use cases or customers, poisoned tool output and denial of wallet.
      Template: [THREAT_MODEL.md](THREAT_MODEL.md).
- [ ] **Data flow and classification.** Which prompts, responses, tool arguments, memory records,
      traces and identity claims may be stored, in which Region, for how long, under which key,
      and how they are deleted. Template: [DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md).
- [ ] **Availability, latency, recovery and data-loss targets**, confirmed against the Amazon
      Bedrock and Amazon Bedrock AgentCore features you use in each Region. Template:
      [SLO_RTO_RPO.md](SLO_RTO_RPO.md).
- [ ] **Support model.** A named service owner, security owner, on-call rotation, escalation
      path, dependency owners and change approvers. Template:
      [OPERATING_MODEL.md](OPERATING_MODEL.md).
- [ ] **Risk register.** Every deferred high or critical finding has an owner, expiry date,
      compensating control and accountable approver. Template:
      [RISK_REGISTER.md](RISK_REGISTER.md).

## G2: Identity and secrets

- [ ] With an enterprise identity provider, Amazon Cognito self-signup and native sign-in are off.
      Any emergency local access is admin-created, monitored and explicitly enabled.
- [ ] Browser clients use the authorization code flow with Proof Key for Code Exchange (PKCE); implicit grant is off.
- [ ] Issuer, audience, client, scopes, token age and the identity claim used for memory
      isolation are verified. Logout, revocation, token renewal and emergency access are
      documented.
- [ ] An automated sign-in test runs for each identity mode you use: it invokes a runtime as an
      end user and proves another user cannot read that user's memory.
- [ ] Every credential is in AWS Secrets Manager with a rotation owner and alarms on failed
      retrieval or rotation. Configuration contains secret names or ARNs only.

## G2: Authorization and least privilege

- [ ] Cedar is in `ENFORCE` with principal, action, tool and resource rules for each use case.
- [ ] Allowed and denied tool calls are tested through the live Gateway, including privilege
      escalation, modified claims, missing scopes, cross-account callers and direct calls that
      bypass the agent.
- [ ] Runtime IAM is scoped to approved models or inference profiles, Amazon Elastic Container Registry (Amazon ECR) repositories,
      log groups, AWS Key Management Service (AWS KMS) keys, parameters, memories, gateways and credential providers.
- [ ] Gateway exception reporting is at a production-safe level: clients get stable error codes,
      and redacted diagnostic context stays in protected logs.
- [ ] Private connectivity and controlled egress are on where your data classification requires
      them, with DNS, endpoint policies and failure behavior tested.

## G2: Data protection and audit

- [ ] Logs contain no raw prompts, responses, tool payloads, tokens or events beyond what your data
      classification allows; logging uses field allow-lists, redaction and correlation IDs.
- [ ] Retention and deletion are set for CloudWatch logs, traces, memory, audit data and build
      artifacts.
- [ ] KMS keys are retained, the audit trail is your approved multi-Region or organization trail,
      and immutable copies reach your log archive where required.

## G3: Verification

- [ ] `deploy.sh verify` passes against the deployment. Your own tests also go through the
      deployed runtime and cover:
      - an end-user sign-in
      - an allowed and a refused tool call
      - memory recalled across invocations
      - the invocation's trace, found by correlation ID
      - guardrail and model allow-list denials
      - fresh metrics on each required alarm
- [ ] A production runtime reports unhealthy when its configured Gateway, memory, identity,
      guardrail or tracing setup fails.
- [ ] Each use case has a versioned evaluation dataset (prompts, expected tool choices, policy
      outcomes, scoring rubric). A release goes ahead only if it meets the thresholds for task
      success, tool correctness, groundedness where relevant, harmful-content policy and
      regression from the approved baseline.
- [ ] Adversarial tests cover prompt injection, indirect injection, tool misuse, data
      exfiltration, sensitive-data disclosure and resource exhaustion.
- [ ] The framework patterns, identity modes, topologies and Regions you use are the ones tested;
      see the [support matrix](SUPPORT_MATRIX.md).
- [ ] Load, soak, concurrency, quota, throttling, timeout, retry, malformed-event and
      dependency-failure tests have run.

## G4: Operations and resilience

Starting objectives to review against your use case:

| Measure | Starting target |
|---|---|
| End-to-end successful request rate | At least 99.9% monthly, excluding valid policy blocks and invalid client requests |
| Tool execution success | At least 99% for platform-owned tools |
| Trace coverage | At least 99% of accepted requests have a searchable correlation trace |
| Unauthorized model or tool use | 0 |
| Quality regression | No critical evaluation failure and no material regression from the approved baseline |
| Recovery | Your approved recovery time objective (RTO) and recovery point objective (RPO), demonstrated in a recovery exercise |

- [ ] Baseline and peak traffic are measured, and load is tested at twice the forecast peak or
      your agreed margin.
- [ ] Canaries cover authentication, runtime invocation, Gateway tool calls, memory recall and
      trace search, and alarm on missing data as well as bad data.
- [ ] Every alarm has an owner, severity, customer impact, first diagnostic query, mitigation,
      rollback step and escalation path.
- [ ] Dashboards cover the dependencies: Amazon Bedrock, AgentCore, Amazon Cognito or your
      identity provider, Gateway tools, AWS KMS, Secrets Manager and CloudWatch delivery.
- [ ] Retries are bounded with jitter, repeatable actions are idempotent, and concurrency limits,
      request budgets and load shedding are in place.
- [ ] Deployment waves, health gates, automatic rollback conditions and a tested last-known-good
      release are defined.
- [ ] Backup, restore, Region or account recovery, key access loss, identity provider outage,
      tool outage and corrupted configuration are tested, with recovery times recorded.
- [ ] Cost is attributed (inference profiles or equivalent, resource tags, per-use-case token
      metrics), with budgets, anomaly alarms and denial-of-wallet limits.
- [ ] Service quotas are reviewed before promotion, with alarms before operational limits.

## G5: Software supply chain

- [ ] Python and Node dependencies are locked, including transitive versions and hashes, and the
      lock files are used for local, CI, AWS CodeBuild and container builds.
- [ ] Container base images and build tools are referenced by an exact digest; downloaded build artifacts are
      verified.
- [ ] Every image and release has a software bill of materials (SBOM). Source, infrastructure as
      code (IaC), dependencies, licenses, containers and secrets are scanned. Images are signed
      with provenance.
- [ ] Lint, unit tests, AWS Cloud Development Kit (CDK) synth, Terraform validation, security scans, live integration tests,
      AI security tests and evaluation thresholds block promotion, with no `continue-on-error` on
      a security gate.
- [ ] Release branches are protected, changes are reviewed, deployment approval is separate from
      authorship, and production roles are restricted.
- [ ] One immutable artifact is promoted through development, staging and production, with no
      rebuild between them.

## G6: Rehearse and approve launch

- [ ] From a fresh staging account, design, deploy, sign in through your identity provider, run
      every verification, upgrade, roll back, recover and remove, using only the documentation.
      Record duration, manual steps, failures and fixes.
- [ ] Someone who did not build the deployment repeats the rehearsal.
- [ ] Keyboard, screen-reader, error-message and offline-state checks pass for every interface
      your users see.
- [ ] An operational readiness review is held, and security, operations, platform engineering
      and the product owner sign the evidence and the remaining risks.

## Release evidence

Keep a versioned, non-sensitive evidence index for each release:

| Evidence | Accountable role |
|---|---|
| Architecture, data flow, threat model and security decisions | Security lead |
| IAM and authorization review, negative-test results | Security lead |
| Dependency locks, SBOM, scans, signatures and provenance | Release engineer |
| Functional, framework, identity and topology test results | Test lead |
| Agent evaluation and AI security reports | Agent quality owner |
| Load, soak, quota, game-day, rollback and recovery reports | Operations lead |
| Service levels, dashboards, alarms and runbooks | Service owner |
| Risk register and final approvals | Product owner |

Evidence containing customer data, credentials, account identifiers or security-sensitive traces
stays in your approved evidence store. A repository contains only the index, templates and
sanitized examples.
