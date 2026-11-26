# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The knowledge stack: SOP and API-docs search, and agent knowledge on Aurora.

One stack, `<prefix>-uc-business-operations-knowledge`, behind the
`knowledge:` flags in config.py. It always exists (the deployment contract
lists it) and holds only a feature parameter when every flag is off.

- `sop_search` (on): an Amazon Bedrock knowledge base on S3 Vectors over the SOP bucket,
  behind `search_sap_sops`. The bucket is the main stack's: this stack reads its
  name from SSM, so it deploys after the main stack and never imports it. The
  search Lambda fills a hit's contacts and constants as `load_sop` does, so it
  reads the main stack's API layer, registry bucket and config table the same way.
- `api_docs_search` (off): a second knowledge base over `packs/_seed/api-docs/`,
  example OpenAPI specs, behind `search_sap_api_docs`.
- `agent_knowledge.enabled` (off): see agent_knowledge.py.

A custom resource starts an ingestion job at deploy whenever the corpus it
indexes changes in the checkout. That covers the shipped seed only: a bundle
import and a SOP write start their own job, and `ops.py kb-sync` starts one
on demand. A write that finds a job running sets the needs-reindex flag instead,
and a five-minute schedule here starts the follow-up job once that one finishes
(`knowledge_search/reindex.py`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

from business_operations import ASSET_EXCLUDE, agent_knowledge, names
from business_operations.config import KnowledgeConfig
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import gateway_lambda_target, uc_function

UC = Path(__file__).resolve().parents[1]
PACKS = UC / "packs"
API_DOCS_SEED = PACKS / "_seed" / "api-docs"
NAME = names.NAME
SCOPE = f"uc-{NAME}"

# The main stack publishes the SOP bucket here (the data layer).
SOPS_BUCKET_PARAM = "sops-bucket"
# ... and its API layer (api.py).
API_LAYER_PARAM = "api-layer-arn"
# The needs-reindex flag a SOP write sets when a job is running (sop_store). Not a
# CloudFormation resource: the writers create it and the follow-up deletes it. A
# teardown while it is set leaves one free String parameter behind.
REINDEX_FLAG_PARAM = "sops-reindex-pending"
# Titan Text Embeddings v2 at its default 1024 dimensions. The index dimension
# is fixed at create, so the model is not a setting: another model with another
# dimension would fail every ingestion.
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
DIMENSION = 1024

TARGET = f"{NAME}-knowledge"
SEARCH_TOOLS = {
    "search_sap_sops": {
        "Name": "search_sap_sops",
        "Description": (
            "Search one pack's Standard Operating Procedures for guidance the loaded "
            "SOP does not give: decision trees, thresholds, validation method, error "
            "handling. Each hit is a whole SOP, headed by its source URI. Use "
            "load_sop instead once the exception type is known."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "query": {"Type": "string", "Description": "What guidance you need"},
                "pack_id": {
                    "Type": "string",
                    "Description": "The pack whose SOPs to search: the case's pack",
                },
            },
            "Required": ["query", "pack_id"],
        },
    },
    "search_sap_api_docs": {
        "Name": "search_sap_api_docs",
        "Description": (
            "Search example SAP OData API documentation: endpoints, fields, example "
            "calls. The specs are examples, not this SAP system's; get_metadata is "
            "authoritative for which fields exist."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "query": {"Type": "string", "Description": "What API detail you need"}
            },
            "Required": ["query"],
        },
    },
}
# (policy id, tool). Every statement needs the use case's read scope.
SEARCH_POLICIES = {
    "search_sap_sops": "search_sops",
    "search_sap_api_docs": "search_api_docs",
}


def tree_hash(*roots: Path, pattern: str = "**/*") -> str:
    """A digest of every file under `roots` matching `pattern`, path and content."""
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(root.glob(pattern)) if root.is_dir() else ():
            if path.is_file():
                digest.update(str(path.relative_to(UC)).encode() + b"\0")
                digest.update(path.read_bytes())
    return digest.hexdigest()


# What each SOP's metadata sidecar carries (pack_tiers.sop_metadata). Part of the
# ingestion digest: a change to it re-indexes the corpus at the next deploy.
SOP_METADATA = "pack_id/1"


def sop_sources() -> str:
    """The digest of every pack's SOPs, the files the SOP top-up seeds, and of the
    metadata ingestion indexes beside them."""
    files = tree_hash(
        *(p for p in sorted(PACKS.glob("[!_]*")) if p.is_dir()), pattern="sops/**/*"
    )
    return hashlib.sha256(f"{SOP_METADATA}:{files}".encode()).hexdigest()


class KnowledgeStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        ctx: UseCaseContext,
        cfg: KnowledgeConfig,
        contacts: dict[str, str | None],
        sap_enabled: bool,
        cedar_statement: Callable[[str, str, str], str],
        **kw,
    ):
        super().__init__(scope, id, **kw)
        ssm_prefix, prefix = ctx.ssm_prefix, ctx.prefix
        project, environment = ctx.project, ctx.environment
        self._retain = ctx.production
        self._out_prefix = f"{ssm_prefix}/use-cases/{NAME}"

        # Under production every bucket is on the platform CMK (security.md row 5).
        self._key_arn = (
            ctx.ssm_value(self, "security/kms-key-arn") if self._retain else None
        )
        gateway_id = ctx.ssm_value(self, "gateway/id")
        gateway_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{gateway_id}"
        # Not "GatewayRole": the main stack imports it under that id, and the inline
        # policy CDK attaches is named after the id, so the two would overwrite.
        gateway_role = iam.Role.from_role_arn(
            self, "KnowledgeGatewayRole", ctx.ssm_value(self, "gateway/role-arn")
        )

        self._publish(
            "knowledge-features",
            json.dumps(
                {
                    "sop_search": cfg.sop_search,
                    "api_docs_search": cfg.api_docs_search,
                    "agent_knowledge": cfg.agent_knowledge.enabled,
                }
            ),
        )

        targets: list[
            tuple[agentcore.CfnGatewayTarget, str, list[tuple[str, str]]]
        ] = []
        kb_ids: dict[str, str] = {}
        if cfg.sop_search or cfg.api_docs_search:
            kb_role = iam.Role(
                self,
                "KnowledgeBaseRole",
                assumed_by=iam.ServicePrincipal(
                    "bedrock.amazonaws.com",
                    conditions={"StringEquals": {"aws:SourceAccount": self.account}},
                ),
                description=f"{prefix} {NAME}: Amazon Bedrock knowledge bases on S3 Vectors",
            )
            kb_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"
                    ],
                )
            )
            self._via_s3(kb_role, "kms:Decrypt")
            if cfg.sop_search:
                sops = s3.Bucket.from_bucket_name(
                    self,
                    "SopsBucket",
                    ctx.ssm_value(self, f"use-cases/{NAME}/{SOPS_BUCKET_PARAM}"),
                )
                kb_ids["search_sap_sops"], data_source_id = self._knowledge_base(
                    "Sops",
                    "sops",
                    sops,
                    kb_role,
                    prefix,
                    sop_sources(),
                    whole_documents=True,
                )
                self._reindex_follow_up(ctx, kb_ids["search_sap_sops"], data_source_id)
            if cfg.api_docs_search:
                bucket, seeded = self._api_docs_bucket()
                kb_ids["search_sap_api_docs"], _ = self._knowledge_base(
                    "ApiDocs",
                    "api-docs",
                    bucket,
                    kb_role,
                    prefix,
                    tree_hash(API_DOCS_SEED),
                    after=seeded,
                )

            registry_bucket = ctx.ssm_value(self, f"use-cases/{NAME}/registry-bucket")
            config_table = ctx.ssm_value(self, f"use-cases/{NAME}/config-table")
            fn = uc_function(
                self,
                "KnowledgeSearch",
                ctx,
                UC / "knowledge_search",
                handler="handler.handler",
                # registry.py and config_overrides.py, as load_sop reads them.
                layers=[
                    _lambda.LayerVersion.from_layer_version_arn(
                        self,
                        "ApiLayer",
                        ctx.ssm_value(self, f"use-cases/{NAME}/{API_LAYER_PARAM}"),
                    )
                ],
                timeout=30,
                environment={
                    "SOPS_KB_ID": kb_ids.get("search_sap_sops", ""),
                    "API_DOCS_KB_ID": kb_ids.get("search_sap_api_docs", ""),
                    "REGISTRY_BUCKET": registry_bucket,
                    "CONFIG_TABLE": config_table,
                    "CONTACTS_JSON": json.dumps(contacts),
                    # As the runtime sets it, so both see the same packs.
                    "SAP_MCP_ENABLED": "true" if sap_enabled else "false",
                },
            )
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[f"arn:aws:s3:::{registry_bucket}/*"],
                )
            )
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:Query"],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/{config_table}"
                    ],
                )
            )
            if self._key_arn:
                fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=["kms:Decrypt"],
                        resources=[self._key_arn],
                        conditions={
                            "StringEquals": {
                                "kms:ViaService": [
                                    f"s3.{self.region}.amazonaws.com",
                                    f"dynamodb.{self.region}.amazonaws.com",
                                ]
                            }
                        },
                    )
                )
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:Retrieve"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb}"
                        for kb in kb_ids.values()
                    ],
                )
            )
            tools = [SEARCH_TOOLS[t] for t in kb_ids]
            targets.append(
                (
                    gateway_lambda_target(
                        self,
                        "KnowledgeTarget",
                        name=TARGET,
                        fn=fn,
                        tools=tools,
                        gateway_id=gateway_id,
                        gateway_role=gateway_role,
                    ),
                    TARGET,
                    [(SEARCH_POLICIES[t], t) for t in kb_ids],
                )
            )
            self._publish("knowledge-target-name", TARGET)
            self._publish("knowledge-tools", json.dumps(list(kb_ids)))

        if cfg.agent_knowledge.enabled:
            ak = agent_knowledge.AgentKnowledge(
                self,
                "AgentKnowledge",
                cfg=cfg.agent_knowledge,
                ctx=ctx,
                key_arn=self._key_arn,
            )
            targets.append(
                (
                    gateway_lambda_target(
                        self,
                        "AgentKnowledgeTarget",
                        name=agent_knowledge.TARGET,
                        fn=ak.tool_function,
                        tools=agent_knowledge.TOOLS,
                        gateway_id=gateway_id,
                        gateway_role=gateway_role,
                    ),
                    agent_knowledge.TARGET,
                    agent_knowledge.POLICIES,
                )
            )
            for key, value in ak.outputs().items():
                self._publish(key, value)
            # The main stack's exemplar builder writes precedents; it finds the
            # cluster through the parameters above, and is granted here.
            ak.grant_precedent_write(
                iam.Role.from_role_arn(
                    self,
                    "ExemplarBuilderRole",
                    ctx.ssm_value(self, f"use-cases/{NAME}/exemplar-builder-role-arn"),
                )
            )

        if targets and ctx.platform.security.cedar.enabled:
            engine_id = ctx.ssm_value(self, "gateway/policy-engine-id")
            previous = None
            for _, target_name, policies in targets:
                for policy_id, tool in policies:
                    policy = agentcore.CfnPolicy(
                        self,
                        f"Policy-{policy_id}",
                        name=names.policy_name(project, environment, policy_id),
                        description=f"{prefix} {NAME}: {tool} needs {SCOPE}/read",
                        policy_engine_id=engine_id,
                        definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                            cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                                statement=cedar_statement(
                                    names.tool_name(target_name, tool),
                                    "read",
                                    gateway_arn,
                                )
                            )
                        ),
                    )
                    # The schema comes from attached targets, and CreatePolicy is
                    # not idempotent on handler retry: one at a time, all targets first.
                    for t, _, _ in targets:
                        policy.add_resource_dependency(t)
                    if previous is not None:
                        policy.add_resource_dependency(previous)
                    previous = policy

    # ── helpers ──

    def _publish(self, key: str, value: str) -> None:
        ssm.StringParameter(
            self,
            f"Out-{key}",
            parameter_name=f"{self._out_prefix}/{key}",
            string_value=value,
        )

    def _via_s3(self, role: iam.IGrantable, *actions: str) -> None:
        """Use of the platform CMK through S3 only, the form the synth admits."""
        if self._key_arn:
            role.grant_principal.add_to_principal_policy(
                iam.PolicyStatement(
                    actions=list(actions),
                    resources=[self._key_arn],
                    conditions={
                        "StringEquals": {
                            "kms:ViaService": f"s3.{self.region}.amazonaws.com"
                        }
                    },
                )
            )

    def _api_docs_bucket(self) -> tuple[s3.Bucket, Construct]:
        """The API-docs corpus: a bucket the deploy mirrors from the checkout.

        Nobody else writes it, so the checkout is the corpus and a deploy
        prunes what the checkout dropped. An earlier version seeded it once and let an
        operator curate the bucket; here curating it is editing the seed.
        """
        bucket = s3.Bucket(
            self,
            "ApiDocsBucket",
            versioned=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=cdk.RemovalPolicy.RETAIN
            if self._retain
            else cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=not self._retain,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-noncurrent",
                    noncurrent_version_expiration=cdk.Duration.days(90),
                )
            ],
        )
        if self._key_arn:
            # Set on the L1 so CDK's grants emit no unconditioned KMS statement
            # for a key this stack does not own; _via_s3 grants it instead.
            bucket.node.default_child.add_property_override(
                "BucketEncryption.ServerSideEncryptionConfiguration",
                [
                    {
                        "ServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": self._key_arn,
                        },
                        "BucketKeyEnabled": True,
                    }
                ],
            )
        seeded = s3deploy.BucketDeployment(
            self,
            "ApiDocsSeed",
            sources=[s3deploy.Source.asset(str(API_DOCS_SEED), exclude=ASSET_EXCLUDE)],
            destination_bucket=bucket,
            prune=True,
        )
        self._via_s3(seeded.handler_role, "kms:Decrypt", "kms:GenerateDataKey")
        # An explicit deny beats any grant: only the deploy writes this corpus.
        # Version deletes are denied too under production, where nothing
        # auto-deletes the bucket; elsewhere auto-delete needs them.
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyWritesButTheDeploy",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject", "s3:DeleteObject"]
                + (["s3:DeleteObjectVersion"] if self._retain else []),
                resources=[bucket.arn_for_objects("*")],
                conditions={
                    "ArnNotEquals": {"aws:PrincipalArn": seeded.handler_role.role_arn}
                },
            )
        )
        self._publish("api-docs-bucket", bucket.bucket_name)
        return bucket, seeded

    def _knowledge_base(
        self,
        id: str,
        corpus: str,
        source: s3.IBucket,
        role: iam.Role,
        prefix: str,
        content_hash: str,
        *,
        whole_documents: bool = False,
        after: Construct | None = None,
    ) -> tuple[str, str]:
        """(knowledge base id, data source id)."""
        vectors = s3vectors.CfnVectorBucket(self, f"{id}VectorBucket")
        index = s3vectors.CfnIndex(
            self,
            f"{id}VectorIndex",
            vector_bucket_arn=vectors.attr_vector_bucket_arn,
            data_type="float32",
            dimension=DIMENSION,
            distance_metric="cosine",
            # Amazon Bedrock's own metadata keys must be non-filterable. Every other
            # key stays filterable, which the pack_id filter needs.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=[
                    "AMAZON_BEDROCK_METADATA",
                    "AMAZON_BEDROCK_TEXT",
                ]
            ),
        )
        index.add_resource_dependency(vectors)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetIndex",
                ],
                resources=[index.attr_index_arn],
            )
        )
        source.grant_read(role)

        kb = bedrock.CfnKnowledgeBase(
            self,
            f"{id}KnowledgeBase",
            name=names.knowledge_base_name(prefix, corpus),
            role_arn=role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    vector_bucket_arn=vectors.attr_vector_bucket_arn,
                    index_arn=index.attr_index_arn,
                ),
            ),
        )
        kb.add_resource_dependency(index)
        # Create validates the store with a live QueryVectors as this role, so
        # its policy must be attached first or the create fails with a 403.
        kb.node.add_dependency(role)

        data_source = bedrock.CfnDataSource(
            self,
            f"{id}DataSource",
            knowledge_base_id=kb.attr_knowledge_base_id,
            name=f"{corpus}-s3",
            # RETAIN, by decision: DELETE has to reach the vector store and this
            # role to empty it, and a delete that cannot leaves the stack in
            # DELETE_FAILED. The cost: a replaced data source leaves its vectors in
            # the index, to clear by hand.
            data_deletion_policy="RETAIN",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=source.bucket_arn
                ),
            ),
            # One vector per SOP: a hit is a whole SOP and never mixes two.
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="NONE"
                )
            )
            if whole_documents
            else None,
        )

        # Start an ingestion job when the corpus in the checkout changes. The digest in the description is what changes,
        # so an unchanged corpus re-runs nothing. Not the client token: a
        # reverted corpus would reuse an old token and start no job.
        call = cr.AwsSdkCall(
            service="@aws-sdk/client-bedrock-agent",
            action="StartIngestionJob",
            parameters={
                "knowledgeBaseId": kb.attr_knowledge_base_id,
                "dataSourceId": data_source.attr_data_source_id,
                "description": f"deploy {content_hash[:16]}",
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"{corpus}-ingestion"),
            # A job already running indexes the bucket as it now stands.
            ignore_error_codes_matching="ConflictException",
        )
        ingest = cr.AwsCustomResource(
            self,
            f"{id}Ingestion",
            on_create=call,
            on_update=call,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["bedrock:StartIngestionJob"],
                        resources=[kb.attr_knowledge_base_arn],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
        )
        ingest.node.add_dependency(data_source)
        if after is not None:
            ingest.node.add_dependency(after)

        self._publish(f"{corpus}-kb-id", kb.attr_knowledge_base_id)
        self._publish(f"{corpus}-data-source-id", data_source.attr_data_source_id)
        return kb.attr_knowledge_base_id, data_source.attr_data_source_id

    def _reindex_follow_up(
        self, ctx: UseCaseContext, kb_id: str, data_source_id: str
    ) -> None:
        """Re-index after a job a SOP write found running. Amazon Bedrock emits no event when
        an ingestion job ends, so a schedule polls the flag; idle, it reads one
        parameter."""
        flag = f"{self._out_prefix}/{REINDEX_FLAG_PARAM}"
        fn = uc_function(
            self,
            "SopReindex",
            ctx,
            UC / "knowledge_search",
            handler="reindex.handler",
            environment={
                "REINDEX_FLAG_PARAM": flag,
                "SOPS_KB_ID": kb_id,
                "SOPS_DATA_SOURCE_ID": data_source_id,
            },
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:GetParameter",
                    "ssm:PutParameter",
                    "ssm:DeleteParameter",
                ],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=flag.lstrip("/"),
                    )
                ],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob"],
                resources=[
                    self.format_arn(
                        service="bedrock",
                        resource="knowledge-base",
                        resource_name=kb_id,
                    )
                ],
            )
        )
        events.Rule(
            self,
            "SopReindexSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            description=f"{ctx.prefix} {NAME}: re-index SOPs a write queued",
        ).add_target(targets.LambdaFunction(fn))
