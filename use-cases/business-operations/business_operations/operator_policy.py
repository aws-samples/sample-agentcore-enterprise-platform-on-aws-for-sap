# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`ops.py operator-policy <group>`: the least-privilege IAM policy for one group of verbs.

An operator otherwise runs every verb as whatever they hold, Admin in development. The
policy names this deployment's own resources, read from the parameters the stack
publishes, so it is printed per deployment rather than shipped as a file of wildcards.

Groups, and the verbs each one covers:

- `bundle`: every `bundle …` verb and `pack status` (the composer enforces its own
  promotion rules).
- `seed`: `seed`, `poll` and `rerun`, including `rerun --dlq`.
- `users`: `add-user` and `add-group`, on the shared user pool. Pool administration is
  this use case's administration: anyone holding it can grant themselves any group.
- `trigger-mode`: `trigger-mode get|set`, the switch and each pack's mode, and
  `emergency-stop`, which invokes the stop function.
- `evals`: `evals-run`, judges included, plus a results bucket and a lease table when
  `/<project>/<env>/evals/results-bucket` and `.../lease-table` name them, as a CI setup
  publishes them. It seeds SAP, writes cases and invokes the composer and the invoker,
  so it renders only for a stack whose published promotion role is `development`, and
  it may not write a case's `action_log`, where approvals live.

`evals-setup`, `kb-sync` and `deploy.sh` are left to a deployment principal: they
create evaluators, start ingestion jobs, or deploy.
"""

from __future__ import annotations

from business_operations.names import NAME
from infra_utils.platform_config import allowed_model_resources


class PolicyRefused(ValueError):
    """This group's policy is not rendered for this deployment; the message says why."""


GROUPS = ("bundle", "seed", "users", "trigger-mode", "evals")
#: Each group's function parameters (`<own>/<key>` holds a function name).
_FUNCTIONS = {
    "bundle": ("pack-composer-function",),
    "seed": ("test-data-function", "sap-poller-function", "operator-rerun-function"),
    "evals": ("test-data-function", "invoker-function", "pack-composer-function"),
    "trigger-mode": ("stop-function",),
}
#: The custom judge's model (`evals/setup_evaluations.py`, which a test pins it to).
JUDGE_MODEL = "us.anthropic.claude-sonnet-5-5"
#: The stack's promotion role, which the composer also runs under (`packs.py`).
ROLE_KEY = f"use-cases/{NAME}/promotion-role"
#: Case attributes the evals policy may not write: approvals live in `action_log`, and
#: the invoker trusts a `human_approved` entry there.
_EVALS_DENIED_CASE_ATTRIBUTES = ("action_log",)
_EVALS_KEYS = (
    f"use-cases/{NAME}/cases-table",
    f"use-cases/{NAME}/tickets-table",
    f"runtimes/{NAME}/arn",
    "evals/results-bucket",
    "evals/lease-table",
)


def parameters(group: str) -> list[str]:
    """The `/<project>/<env>/…`-relative parameters `render` needs for this group."""
    own = f"use-cases/{NAME}"
    keys = [f"{own}/{k}" for k in _FUNCTIONS.get(group, ())]
    if group == "seed":
        keys.append(f"{own}/agent-dlq-url")
    if group == "users":
        keys.append("auth/user-pool-id")
    if group == "evals":
        keys += [*_EVALS_KEYS, ROLE_KEY]
    return keys


def render(
    group: str, *, region: str, account: str, project: str, env: str, values: dict
) -> dict:
    """The policy document. `values` maps each of `parameters(group)` to its value;
    a parameter the deployment does not have (no SAP system, no seeder) is left out.

    Raises PolicyRefused for `evals` on a stack whose promotion role is not
    `development`, or not published: the policy can plant cases and drive the agent.
    """
    if group == "evals" and (role := values.get(ROLE_KEY)) != "development":
        raise PolicyRefused(
            f"this stack's promotion role is {role or 'not published (deploy first)'}: "
            "the evals policy seeds SAP and drives the agent, so it is rendered only "
            "for a development stack"
        )
    base = f"arn:aws:ssm:{region}:{account}:parameter/{project}/{env}"
    statements = [
        {
            "Sid": "ReadTheDeploymentsParameters",
            "Effect": "Allow",
            "Action": ["ssm:GetParameter"],
            "Resource": [f"{base}/use-cases/{NAME}/*", f"{base}/auth/*"],
        }
    ]
    own = f"use-cases/{NAME}"
    functions = [
        f"arn:aws:lambda:{region}:{account}:function:{values[f'{own}/{k}']}"
        for k in _FUNCTIONS.get(group, ())
        if values.get(f"{own}/{k}")
    ]
    if functions:
        statements.append(
            {
                "Sid": "InvokeThisUseCasesFunctions",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": functions,
            }
        )
    dlq = values.get(f"{own}/agent-dlq-url")
    if group == "seed" and dlq:
        statements.append(
            {
                "Sid": "DrainTheAgentDeadLetters",
                "Effect": "Allow",
                "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage"],
                "Resource": [
                    f"arn:aws:sqs:{region}:{account}:{dlq.rsplit('/', 1)[-1]}"
                ],
            }
        )
    pool = values.get("auth/user-pool-id")
    if group == "users" and pool:
        statements.append(
            {
                "Sid": "AdministerTheSharedPool",
                "Effect": "Allow",
                "Action": [
                    "cognito-idp:ListUsers",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminAddUserToGroup",
                    "cognito-idp:CreateGroup",
                ],
                "Resource": [f"arn:aws:cognito-idp:{region}:{account}:userpool/{pool}"],
            }
        )
    if group == "trigger-mode":
        statements.append(
            {
                "Sid": "SetTheTriggerMode",
                "Effect": "Allow",
                "Action": ["ssm:PutParameter"],
                "Resource": [f"{base}/use-cases/{NAME}/autonomy/*"],
            }
        )
        statements.append(
            {
                # `trigger-mode get` lists every pack's mode under one path.
                "Sid": "ListThePacksModes",
                "Effect": "Allow",
                "Action": ["ssm:GetParametersByPath"],
                "Resource": [f"{base}/use-cases/{NAME}/autonomy/packs"],
            }
        )
    if group == "evals":
        statements += _evals_statements(region, account, project, env, values)
    return {"Version": "2012-10-17", "Statement": statements}


def _evals_statements(
    region: str, account: str, project: str, env: str, values: dict
) -> list[dict]:
    """What `evals-run` reads and writes beyond its three functions; a resource the
    deployment does not publish is left out."""
    cases, tickets, runtime, bucket, leases = (values.get(k) for k in _EVALS_KEYS)

    def table(name: str) -> str:
        return f"arn:aws:dynamodb:{region}:{account}:table/{name}"

    def group(name: str) -> list[str]:
        arn = f"arn:aws:logs:{region}:{account}:log-group:{name}"
        return [arn, f"{arn}:*"]

    out = [
        {
            "Sid": "ReadTheRuntimeArn",
            "Effect": "Allow",
            "Action": ["ssm:GetParameter"],
            "Resource": [
                f"arn:aws:ssm:{region}:{account}:parameter/{project}/{env}/runtimes/{NAME}/arn"
            ],
        },
        {
            # Both evaluator ARN forms the service documents: built-ins carry no
            # account. No resource type exists for listing evaluators.
            "Sid": "JudgeSessions",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:Evaluate", "bedrock-agentcore:GetEvaluator"],
            "Resource": [
                "arn:aws:bedrock-agentcore:::evaluator/Builtin.*",
                f"arn:aws:bedrock-agentcore:{region}:{account}:evaluator/*",
            ],
        },
        {
            "Sid": "FindTheCustomJudge",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:ListEvaluators"],
            "Resource": ["*"],
        },
        {
            # A custom judge's model runs on the caller's permissions.
            "Sid": "InvokeTheCustomJudgesModel",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel"],
            "Resource": allowed_model_resources([JUDGE_MODEL]),
        },
        {
            "Sid": "FindTheSpansGroup",
            "Effect": "Allow",
            "Action": ["logs:DescribeLogGroups"],
            "Resource": [f"arn:aws:logs:{region}:{account}:log-group:*"],
        },
        {
            # GetQueryResults has no resource type; a query id is only ever this
            # principal's own StartQuery's.
            "Sid": "ReadQueryResults",
            "Effect": "Allow",
            "Action": ["logs:GetQueryResults"],
            "Resource": ["*"],
        },
        {
            "Sid": "ReadTheInterceptorsVerdicts",
            "Effect": "Allow",
            "Action": ["logs:FilterLogEvents"],
            "Resource": group(f"/aws/lambda/{project}-{env}-egress-interceptor"),
        },
    ]
    spans = group("aws/spans")
    if runtime:
        spans += group(
            f"/aws/bedrock-agentcore/runtimes/{runtime.split('/')[-1]}-DEFAULT"
        )
    out.append(
        {
            "Sid": "ReadTheSessionsSpans",
            "Effect": "Allow",
            "Action": ["logs:StartQuery"],
            "Resource": spans,
        }
    )
    if cases:
        out.append(
            {
                "Sid": "WriteAndReadTheEvalCases",
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                "Resource": [table(cases)],
            }
        )
        # A planted case with invented approvals would be an approved SAP write with no
        # human: the invoker reads approvals from `action_log`. evals-run never writes it.
        out.append(
            {
                "Sid": "NeverWriteACasesApprovals",
                "Effect": "Deny",
                "Action": ["dynamodb:PutItem"],
                "Resource": [table(cases)],
                "Condition": {
                    "ForAnyValue:StringEquals": {
                        "dynamodb:Attributes": list(_EVALS_DENIED_CASE_ATTRIBUTES)
                    }
                },
            }
        )
    if tickets:
        out.append(
            {
                "Sid": "ReadTheirTickets",
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem"],
                "Resource": [table(tickets)],
            }
        )
    if bucket:
        out.append(
            {
                "Sid": "StoreAndReadResults",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            }
        )
    if leases:
        out.append(
            {
                "Sid": "TakeAndReturnTheLease",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                "Resource": [table(leases)],
                # This environment's lease only: the table holds every lane's.
                "Condition": {
                    "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [env]}
                },
            }
        )
    return out
