# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Security Stack — KMS CMK, IAM policy templates, CloudTrail."""

import aws_cdk as cdk
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class SecurityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        enable_kms: bool = True,
        enable_cloudtrail: bool = True,
        retain_data: bool = False,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        lifecycle_policy = (
            cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY
        )

        # KMS CMK for AgentCore Memory encryption
        self.kms_key = (
            kms.Key(
                self,
                "AgentCoreKey",
                alias=f"alias/{prefix}-agentcore",
                description=f"CMK for {project_name} AgentCore resources",
                enable_key_rotation=True,
                removal_policy=lifecycle_policy,
            )
            if enable_kms
            else None
        )

        # CloudTrail for AgentCore API auditing
        if enable_cloudtrail:
            # Who read or wrote the audit log, kept apart from it. S3 server access
            # logs cannot be written with a customer managed key, so SSE-S3.
            access_logs = s3.Bucket(
                self,
                "TrailAccessLogs",
                removal_policy=lifecycle_policy,
                auto_delete_objects=not retain_data,
                encryption=s3.BucketEncryption.S3_MANAGED,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                versioned=retain_data,
            )
            trail_bucket = s3.Bucket(
                self,
                "TrailBucket",
                bucket_name=f"{prefix}-cloudtrail-{self.account}",
                removal_policy=lifecycle_policy,
                auto_delete_objects=not retain_data,
                encryption=(
                    s3.BucketEncryption.KMS
                    if retain_data and self.kms_key
                    else s3.BucketEncryption.S3_MANAGED
                ),
                encryption_key=self.kms_key if retain_data else None,
                enforce_ssl=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                server_access_logs_bucket=access_logs,
                versioned=retain_data,
            )
            trail_name = f"{prefix}-agentcore-trail"
            if self.kms_key:
                # CDK's Trail sets KmsKeyId but grants CloudTrail nothing on the key.
                # The ARN is formatted, not read off the trail, which would be a cycle.
                trail_arn = self.format_arn(
                    service="cloudtrail", resource="trail", resource_name=trail_name
                )
                cloudtrail_principal = iam.ServicePrincipal("cloudtrail.amazonaws.com")
                self.kms_key.add_to_resource_policy(
                    iam.PolicyStatement(
                        sid="CloudTrailEncryptsTheTrail",
                        principals=[cloudtrail_principal],
                        actions=["kms:GenerateDataKey*"],
                        resources=["*"],
                        conditions={
                            "StringEquals": {"aws:SourceArn": trail_arn},
                            "StringLike": {
                                "kms:EncryptionContext:aws:cloudtrail:arn": self.format_arn(
                                    service="cloudtrail",
                                    region="*",
                                    resource="trail",
                                    resource_name="*",
                                )
                            },
                        },
                    )
                )
                self.kms_key.add_to_resource_policy(
                    iam.PolicyStatement(
                        sid="CloudTrailDescribesTheKey",
                        principals=[cloudtrail_principal],
                        actions=["kms:DescribeKey"],
                        resources=["*"],
                        conditions={"StringEquals": {"aws:SourceArn": trail_arn}},
                    )
                )
            cloudtrail.Trail(
                self,
                "AgentCoreTrail",
                trail_name=trail_name,
                bucket=trail_bucket,
                encryption_key=self.kms_key,
                enable_file_validation=retain_data,
                is_multi_region_trail=retain_data,
            )

        if self.kms_key:
            cdk.CfnOutput(self, "KmsKeyArn", value=self.kms_key.key_arn)
            # Published so a use case can encrypt its own tables, buckets and
            # log groups with the platform CMK. app.py refuses a use-case grant
            # on it without kms:ViaService.
            ssm.StringParameter(
                self,
                "SSMKmsKeyArn",
                parameter_name=f"/{project_name}/{environment}/security/kms-key-arn",
                string_value=self.kms_key.key_arn,
            )
