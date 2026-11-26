# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The use case's state: its tables and buckets, and how a function is granted them.

Every resource is created with the stack as its scope, never inside a construct
of its own: a logical id comes from the construct path, so wrapping these would
change every id, and CloudFormation would read that as delete-and-create of
tables holding cases. `Cases` keeps the id the scaffold deployed it under.

Posture follows the platform. Under `deployment.mode: production` everything is
encrypted with the platform CMK (`security/kms-key-arn`) and retained, tables
with deletion protection on. Otherwise tables use the AWS-managed DynamoDB key,
buckets S3-managed keys and queues SQS-managed ones, and teardown deletes them.

The CMK is set on the CloudFormation resource, not handed to the L2 construct.
An L2 that knows its key adds a bare `kms:Decrypt` on it to every grant, and
app.py refuses a use-case grant on the platform key without `kms:ViaService`.
So grant through `Data.grant`, which adds the key statement with that condition;
a narrower grant (`table.grant(fn, "dynamodb:GetItem")`) pairs with
`Data.grant_key`. Without it a function works outside production and is refused
by KMS in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs

from infra_utils.use_case_context import UseCaseContext

# CDK's id for the singleton that empties a bucket with autoDeleteObjects.
_AUTO_DELETE_PROVIDER = "Custom::S3AutoDeleteObjectsCustomResourceProvider"


@dataclass
class Data:
    cases: dynamodb.Table
    config: dynamodb.Table
    run_grants: dynamodb.Table
    tickets: dynamodb.Table
    sops: s3.Bucket
    registry: s3.Bucket
    exemplars: s3.Bucket
    key_arn: str | None  # the platform CMK, under production only

    def outputs(self) -> dict[str, str]:
        """SSM keys under use-cases/<name>/, for the ports that read these."""
        return {
            "cases-table": self.cases.table_name,
            "config-table": self.config.table_name,
            "run-grants-table": self.run_grants.table_name,
            "tickets-table": self.tickets.table_name,
            "sops-bucket": self.sops.bucket_name,
            "registry-bucket": self.registry.bucket_name,
            "exemplars-bucket": self.exemplars.bucket_name,
        }

    def grant(
        self,
        grantee: iam.IGrantable,
        *resources: dynamodb.Table | s3.Bucket,
        write=False,
    ) -> None:
        for r in resources:
            if isinstance(r, dynamodb.Table):
                (r.grant_read_write_data if write else r.grant_read_data)(grantee)
            else:
                (r.grant_read_write if write else r.grant_read)(grantee)
        self.grant_key(grantee)

    def grant_cases(self, grantee: iam.IGrantable, *verbs: str) -> None:
        """Only the item verbs a function calls on the cases table: no Scan, DeleteItem
        or BatchWriteItem, which `grant(..., write=True)` would add."""
        self.cases.grant(grantee, *(f"dynamodb:{v}" for v in verbs))
        self.grant_key(grantee)

    def grant_key(
        self, grantee: iam.IGrantable, services: tuple[str, ...] = ("dynamodb", "s3")
    ) -> None:
        if not self.key_arn:
            return
        stack = cdk.Stack.of(self.cases)
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:DescribeKey",
                ],
                resources=[self.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": [
                            f"{service}.{stack.region}.amazonaws.com"
                            for service in services
                        ]
                    }
                },
            )
        )

    def encrypt(self, queue: sqs.Queue) -> None:
        """The platform CMK on a queue under production, set on the L1 as for the
        tables; everyone who sends or receives then needs `grant_key(..., ("sqs",))`."""
        if not self.key_arn:
            return
        cfn = queue.node.default_child
        cfn.sqs_managed_sse_enabled = None
        cfn.kms_master_key_id = self.key_arn


def create(stack: cdk.Stack, ctx: UseCaseContext) -> Data:
    retain = ctx.production
    key_arn = ctx.ssm_value(stack, "security/kms-key-arn") if ctx.production else None
    removal = cdk.RemovalPolicy.RETAIN if retain else cdk.RemovalPolicy.DESTROY

    def table(id: str, pk: str, sk: str | None = None, ttl: str | None = None):
        # Unnamed: a custom name makes any key change undeployable, because
        # CloudFormation will not replace a custom-named resource.
        t = dynamodb.Table(
            stack,
            id,
            partition_key=dynamodb.Attribute(
                name=pk, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name=sk, type=dynamodb.AttributeType.STRING)
            if sk
            else None,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute=ttl,
            deletion_protection=retain,
            removal_policy=removal,
        )
        if key_arn:
            t.node.default_child.sse_specification = (
                dynamodb.CfnTable.SSESpecificationProperty(
                    sse_enabled=True, sse_type="KMS", kms_master_key_id=key_arn
                )
            )
        return t

    def bucket(id: str, versioned: bool, lifecycle: list[s3.LifecycleRule]):
        b = s3.Bucket(
            stack,
            id,
            versioned=versioned,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=lifecycle,
            removal_policy=removal,
            auto_delete_objects=not retain,
        )
        if key_arn:
            b.node.default_child.bucket_encryption = s3.CfnBucket.BucketEncryptionProperty(
                server_side_encryption_configuration=[
                    s3.CfnBucket.ServerSideEncryptionRuleProperty(
                        server_side_encryption_by_default=s3.CfnBucket.ServerSideEncryptionByDefaultProperty(
                            sse_algorithm="aws:kms", kms_master_key_id=key_arn
                        ),
                        bucket_key_enabled=True,
                    )
                ]
            )
        if versioned:
            _deny_version_deletes(stack, b, retain)
        return b

    # One record per case, keyed by its opaque derived id. TTL is server-set
    # only: the model must never reach `ttl`, or DynamoDB deletes the record of
    # a SAP write.
    cases = table("Cases", "case_id", ttl="ttl")
    # GET /cases?pack= and every per-pack read.
    cases.add_global_secondary_index(
        index_name="pack-status-index",
        partition_key=dynamodb.Attribute(
            name="pack_id", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
        projection_type=dynamodb.ProjectionType.ALL,
    )
    # The batch sweeper (`detected`) and the exemplar builder (`complete`). One index
    # per CloudFormation update: add the next one in a later deploy.
    cases.add_global_secondary_index(
        index_name="status-index",
        partition_key=dynamodb.Attribute(
            name="status", type=dynamodb.AttributeType.STRING
        ),
        projection_type=dynamodb.ProjectionType.ALL,
    )
    # The console's case list (`GET /cases`, `api/layer/python/case_query.py`) and the
    # done summary: one pack and status in time order. A multi-attribute partition key,
    # so no writer derives a key and DynamoDB indexes existing cases itself. It projects
    # only what a query filters, sorts and sums on, so an entry is about 1 KB where a
    # case with its traces is tens of KB; a page reads its cases whole by id.
    cases.add_global_secondary_index(
        index_name="pack-status-updated-index",
        partition_keys=[
            dynamodb.Attribute(name="pack_id", type=dynamodb.AttributeType.STRING),
            dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
        ],
        sort_key=dynamodb.Attribute(
            name="updated_at", type=dynamodb.AttributeType.STRING
        ),
        projection_type=dynamodb.ProjectionType.INCLUDE,
        non_key_attributes=[
            "created_at",
            "document_number",
            "exception_type",
            "process_type",
            "attributes",
            "disposition",
            "cost_summary",
        ],
    )
    # `GET /cases?sort=amount`: the same partitions, ordered by the amount the Cases
    # table shows (`case_query.amount_sort`, written with every case). Its own deploy:
    # one index per CloudFormation update. A stack from before both indexes upgrades
    # with one deploy under DEFER_CASES_AMOUNT_INDEX=1, then one without it
    # (docs/operating.md, Troubleshooting, Deploy).
    if os.environ.get("DEFER_CASES_AMOUNT_INDEX") != "1":
        cases.add_global_secondary_index(
            index_name="pack-status-amount-index",
            partition_keys=[
                dynamodb.Attribute(name="pack_id", type=dynamodb.AttributeType.STRING),
                dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            ],
            sort_key=dynamodb.Attribute(
                name="amount_sort", type=dynamodb.AttributeType.NUMBER
            ),
            projection_type=dynamodb.ProjectionType.INCLUDE,
            non_key_attributes=[
                "created_at",
                "document_number",
                "exception_type",
                "process_type",
                "attributes",
                "disposition",
                "cost_summary",
            ],
        )
    # Operator overrides of the SOP corpus's contacts and constants; a row
    # exists only because someone edited that symbol. `namespace` is "contact"
    # or "constant#<pack_id>", so one Query fetches a pack's overrides.
    config = table("Config", "namespace", "config_key")
    # Written by the invoker before each run, read by the run checks on every
    # Gateway call; a grant lives minutes, so TTL clears it.
    run_grants = table("RunGrants", "run_id", ttl="expires_at")
    # The sample ticketing system, kept apart from cases as if external.
    tickets = table("Tickets", "ticket_id")
    tickets.add_global_secondary_index(
        index_name="status-index",
        partition_key=dynamodb.Attribute(
            name="status", type=dynamodb.AttributeType.STRING
        ),
        projection_type=dynamodb.ProjectionType.ALL,
    )

    # The authored SOP corpus. Versioned so each run can record the VersionId
    # it read; old versions kept seven years, in Glacier after thirty days.
    sops = bucket(
        "Sops",
        True,
        [
            s3.LifecycleRule(
                id="glacier-noncurrent",
                noncurrent_version_transitions=[
                    s3.NoncurrentVersionTransition(
                        storage_class=s3.StorageClass.GLACIER,
                        transition_after=cdk.Duration.days(30),
                    )
                ],
                noncurrent_version_expiration=cdk.Duration.days(365 * 7),
            )
        ],
    )
    # The published pack registry and pack documents. Versioned for rollback of
    # a bad publish, not as an audit trail, hence ninety days.
    registry = bucket(
        "Registry",
        True,
        [
            s3.LifecycleRule(
                id="expire-noncurrent",
                noncurrent_version_expiration=cdk.Duration.days(90),
            )
        ],
    )
    # Machine-written exemplars, regenerated from case history, so unversioned.
    # Their own bucket because a knowledge-base data source ingests a whole
    # bucket: here they can never surface as an authored SOP.
    exemplars = bucket("Exemplars", False, [])

    return Data(cases, config, run_grants, tickets, sops, registry, exemplars, key_arn)


def _deny_version_deletes(stack: cdk.Stack, b: s3.Bucket, retain: bool) -> None:
    """No one deletes an object version, so a run's recorded VersionId stays readable.

    Outside production the bucket is emptied on teardown, and that provider is
    the one exception; under production nothing is exempt.
    """
    exempt = {}
    if not retain:
        provider = stack.node.try_find_child(_AUTO_DELETE_PROVIDER)
        if provider is None:
            raise RuntimeError(
                f"{_AUTO_DELETE_PROVIDER} not found: CDK renamed it, and teardown "
                "would be refused by the version-delete deny"
            )
        exempt = {"ArnNotEquals": {"aws:PrincipalArn": provider.role_arn}}
    b.add_to_resource_policy(
        iam.PolicyStatement(
            sid="DenyVersionDeletes",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:DeleteObjectVersion"],
            resources=[b.arn_for_objects("*")],
            conditions=exempt or None,
        )
    )
