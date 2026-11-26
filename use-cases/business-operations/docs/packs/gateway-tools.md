<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Gateway tools for this use case

A tool here is a Lambda function behind a `business-operations-*` target on the shared
AgentCore Gateway. The platform's own targets are added a different way:
[docs/GATEWAY_TARGETS.md](../../../../docs/GATEWAY_TARGETS.md).

## Required changes

Four places, none optional:

1. **The target and its function.** Define the tool specs (`Name`, `Description`,
   `InputSchema`). Build the target with `uc_function` and `gateway_lambda_target`
   (`sap_kit/functions.py`), in its own module under
   `business_operations/`, the way `sop_tools.py` does. Name the target in `names.py`
   (`f"{NAME}-<target>"`), because the run checks and Cedar match on the prefix. The helper
   allows the Gateway role to invoke that function only. It fails the CDK synth (the step that
   turns the AWS Cloud Development Kit code into templates) on a tool name over 64 characters.
   Add the module's `create` to the `targets` dict in `stack.py`, which `policies.create` reads.
2. **The tool list that the packs are checked against.** Add the target and its specs to
   `packs.known_tools`. `validate_packs.py` accepts only names in that set in a pack's
   `gateway_tools`, and an import checks a bundle against the same set. An unknown name fails the
   synth.
3. **A Cedar permit.** Add a `_permit(...)` to `STATEMENTS` in `business_operations/policies.py`,
   with the scope the tool needs (`read`, or `write` for anything that changes data). Under
   `ENFORCE` the Gateway refuses everything that no policy permits, so a tool with no permit is
   not even listed. `cedar.py` has the condition helpers. Never write a permit without
   conditions on the shared policy engine.
4. **The pack's `gateway_tools`**, by full name (`business-operations-<target>___<tool>`), for
   every pack that should be able to call the tool.

## Run checks

Then decide whether the run checks must know about the tool.

- **A tool whose arguments name a case, a ticket, a process type or a pack** belongs in the
  matching set in `business_operations_check/run_checks.py` (`CASE_ARG_TOOLS`, `TICKET_ARG_TOOLS`,
  `PROCESS_TYPE_ARG_TOOLS`, `PACK_ARG_TOOLS`). The run checks pass a tool that is in none of these
  sets unchanged, and check no run grant for it.
- **A tool that changes data outside the run** goes in `READ_ONLY_DENIED_TOOLS` in the same file,
  and in `_READ_ONLY_DENIED_TOOLS` in `agent/utils/mcp_topology.py`. Listing it there also makes
  the run checks check it. So the tool needs a run grant, and the grant of a rehearsal or a
  question is refused. A unit test fails if the two sets differ, or name a tool that no target of
  this use case serves.

A change to the run checks is deployed with the gateway stack. Run
`./scripts/deploy.sh deploy --stack <project>-<env>-gateway` before this use case's stacks, or
the plain `deploy`.
