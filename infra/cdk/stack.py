"""ChartwireStack — the single production stack described in spec §12.1.

Synthesized only (there is no AWS account in this project); `tests/test_stack.py` pins the
properties that matter for the protocol (ALB idle timeout, /readyz health, deregistration
delay), for PHI (encryption everywhere, least-privilege task role) and for operations
(three alarms → SNS). Every resource name below is an identifier; nothing here is measured.
"""

from __future__ import annotations

from typing import Final

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from cdk_nag import NagPackSuppression, NagSuppressions
from constructs import Construct

API_PORT: Final = 8000
PG_PORT: Final = 5433
REDIS_PORT: Final = 6380
"""Non-default ports: cdk-nag RDS11/AEC5. Obscurity is not the control — the security groups are."""
ALB_IDLE_TIMEOUT_S: Final = 3600
"""Must exceed the longest consultation: a WebSocket ingest connection is idle-free but the ALB
still counts an idle timeout between frames of a paused recorder (spec §6.4 rule 5)."""
DEREGISTRATION_DELAY_S: Final = 30
"""Matches the drain budget of the api process (bye → flush ≤ 20 s → close 1012, §6.4 rule 7)."""

SERVICES: Final[tuple[tuple[str, int], ...]] = (("api", 2), ("worker", 1), ("stt-worker", 1))


class ChartwireStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        image_repo = cdk.CfnParameter(
            self,
            "ImageRepo",
            type="String",
            default="chartwire",
            description="컨테이너 이미지 저장소 (예: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/chartwire)",
        )
        image_tag = cdk.CfnParameter(
            self, "ImageTag", type="String", default="latest", description="컨테이너 이미지 태그"
        )
        cert_arn = cdk.CfnParameter(
            self,
            "CertArn",
            type="String",
            default="",
            description="ACM 인증서 ARN (비우면 443 리스너를 만들지 않음)",
        )

        # ------------------------------------------------------------------ network
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )
        flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        vpc.add_flow_log("FlowLog", destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group))

        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, description="ALB: public 80/443")
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP (recorder/viewer WebSocket)")
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS when CertArn is set")
        app_sg = ec2.SecurityGroup(self, "AppSg", vpc=vpc, description="ECS tasks (api/worker/stt-worker)")
        app_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(API_PORT), "ALB → api")
        db_sg = ec2.SecurityGroup(
            self, "DbSg", vpc=vpc, description="RDS PostgreSQL", allow_all_outbound=False
        )
        db_sg.add_ingress_rule(app_sg, ec2.Port.tcp(PG_PORT), "ECS tasks → PostgreSQL")
        redis_sg = ec2.SecurityGroup(
            self, "RedisSg", vpc=vpc, description="ElastiCache Redis", allow_all_outbound=False
        )
        redis_sg.add_ingress_rule(app_sg, ec2.Port.tcp(REDIS_PORT), "ECS tasks → Redis")

        # ------------------------------------------------------------------ keys, buckets, secrets
        kek = kms.Key(
            self,
            "Kek",
            description="chartwire tenant KEK root (AwsKmsKek.wrap/unwrap)",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        log_bucket = s3.Bucket(
            self,
            "AlbLogs",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(30))],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        audio_bucket = s3.Bucket(
            self,
            "Audio",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kek,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(90))],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        jwt_secret = secretsmanager.Secret(
            self,
            "JwtSecret",
            description="CHARTWIRE_JWT_SECRET (HS256)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64, exclude_punctuation=True
            ),
        )
        redis_auth = secretsmanager.Secret(
            self,
            "RedisAuth",
            description="ElastiCache AUTH token",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64, exclude_punctuation=True
            ),
        )

        # ------------------------------------------------------------------ PostgreSQL 16
        pg_engine = rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16)
        pg_params = rds.ParameterGroup(
            self,
            "PgParams",
            engine=pg_engine,
            description="pg_stat_statements for the perf study; TLS mandatory",
            parameters={"shared_preload_libraries": "pg_stat_statements", "rds.force_ssl": "1"},
        )
        db = rds.DatabaseInstance(
            self,
            "Db",
            engine=pg_engine,
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MEDIUM),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg],
            parameter_group=pg_params,
            credentials=rds.Credentials.from_generated_secret("chartwire_owner"),
            database_name="chartwire",
            port=PG_PORT,
            storage_encrypted=True,
            storage_encryption_key=kek,
            allocated_storage=50,
            max_allocated_storage=200,
            multi_az=True,
            backup_retention=cdk.Duration.days(7),
            deletion_protection=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            cloudwatch_logs_exports=["postgresql"],
            enable_performance_insights=True,
            performance_insight_encryption_key=kek,
        )

        # ------------------------------------------------------------------ Redis 7
        redis_subnets = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnets",
            description="chartwire redis (isolated subnets)",
            subnet_ids=[s.subnet_id for s in vpc.isolated_subnets],
        )
        redis = elasticache.CfnReplicationGroup(
            self,
            "Redis",
            replication_group_description="chartwire session state, streams, pub/sub",
            engine="redis",
            engine_version="7.1",
            cache_node_type="cache.t4g.small",
            num_cache_clusters=2,
            automatic_failover_enabled=True,
            multi_az_enabled=True,
            port=REDIS_PORT,
            transit_encryption_enabled=True,
            at_rest_encryption_enabled=True,
            kms_key_id=kek.key_id,
            auth_token=redis_auth.secret_value.unsafe_unwrap(),
            cache_subnet_group_name=redis_subnets.ref,
            security_group_ids=[redis_sg.security_group_id],
            auto_minor_version_upgrade=True,
        )

        # ------------------------------------------------------------------ ECS
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED)
        image = ecs.ContainerImage.from_registry(f"{image_repo.value_as_string}:{image_tag.value_as_string}")
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Runtime role shared by api/worker/stt-worker: KEK wrap/unwrap + audio objects only",
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="KekWrapUnwrap",
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                resources=[kek.key_arn],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AudioObjects",
                actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                resources=[audio_bucket.arn_for_objects("*")],
            )
        )
        app_secrets = {
            "CHARTWIRE_JWT_SECRET": ecs.Secret.from_secrets_manager(jwt_secret),
            "CHARTWIRE_REDIS_AUTH": ecs.Secret.from_secrets_manager(redis_auth),
            "CHARTWIRE_DB_PASSWORD": ecs.Secret.from_secrets_manager(db.secret, "password"),  # type: ignore[arg-type]
        }
        app_env = {
            "CHARTWIRE_DB_HOST": db.db_instance_endpoint_address,
            "CHARTWIRE_DB_PORT": str(PG_PORT),
            "CHARTWIRE_DB_NAME": "chartwire",
            "CHARTWIRE_REDIS_HOST": redis.attr_primary_end_point_address,
            "CHARTWIRE_REDIS_PORT": str(REDIS_PORT),
            "CHARTWIRE_REDIS_TLS": "1",
            "CHARTWIRE_OBJECTSTORE": f"s3://{audio_bucket.bucket_name}",
            "CHARTWIRE_KEK_PROVIDER": "aws-kms",
            "CHARTWIRE_KEK_KMS_KEY_ARN": kek.key_arn,
            "CHARTWIRE_ALLOW_SIM_FRAMES": "false",
        }
        log_group = logs.LogGroup(
            self, "AppLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=cdk.RemovalPolicy.DESTROY
        )

        services: dict[str, ecs.FargateService] = {}
        for role, desired in SERVICES:
            task = ecs.FargateTaskDefinition(
                self,
                f"Task-{role}",
                cpu=1024,
                memory_limit_mib=2048,
                task_role=task_role,
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.ARM64,
                    operating_system_family=ecs.OperatingSystemFamily.LINUX,
                ),
            )
            container = task.add_container(
                role,
                image=image,
                environment={**app_env, "CHARTWIRE_ROLE": role},
                secrets=app_secrets,
                logging=ecs.LogDrivers.aws_logs(stream_prefix=role, log_group=log_group),
                readonly_root_filesystem=True,
            )
            if role == "api":
                container.add_port_mappings(ecs.PortMapping(container_port=API_PORT))
            services[role] = ecs.FargateService(
                self,
                f"Service-{role}",
                cluster=cluster,
                task_definition=task,
                desired_count=desired,
                security_groups=[app_sg],
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                min_healthy_percent=100,
                max_healthy_percent=200,
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                enable_execute_command=False,
            )
        api = services["api"]
        api.auto_scale_task_count(min_capacity=2, max_capacity=6).scale_on_cpu_utilization(
            "Cpu60", target_utilization_percent=60
        )

        # ------------------------------------------------------------------ ALB
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
            idle_timeout=cdk.Duration.seconds(ALB_IDLE_TIMEOUT_S),
            drop_invalid_header_fields=True,
        )
        alb.log_access_logs(log_bucket, prefix="alb")
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "ApiTargets",
            vpc=vpc,
            port=API_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[api],
            deregistration_delay=cdk.Duration.seconds(DEREGISTRATION_DELAY_S),
            health_check=elbv2.HealthCheck(
                path="/readyz",
                interval=cdk.Duration.seconds(15),
                timeout=cdk.Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
        )
        alb.add_listener(
            "Http", port=80, open=False, default_action=elbv2.ListenerAction.forward([target_group])
        )
        has_cert = cdk.CfnCondition(
            self, "HasCert", expression=cdk.Fn.condition_not(cdk.Fn.condition_equals(cert_arn, ""))
        )
        # L1 on purpose: an L2 listener would add a DependsOn from the ECS service to this
        # conditional resource, which CloudFormation rejects (E3005).
        https = elbv2.CfnListener(
            self,
            "Https",
            load_balancer_arn=alb.load_balancer_arn,
            port=443,
            protocol="HTTPS",
            ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            certificates=[elbv2.CfnListener.CertificateProperty(certificate_arn=cert_arn.value_as_string)],
            default_actions=[
                elbv2.CfnListener.ActionProperty(
                    type="forward", target_group_arn=target_group.target_group_arn
                )
            ],
        )
        https.cfn_options.condition = has_cert

        # ------------------------------------------------------------------ alarms
        topic = sns.Topic(self, "Alarms", display_name="chartwire alarms", enforce_ssl=True)
        alarm_action = cw_actions.SnsAction(topic)
        alarms = [
            cloudwatch.Alarm(
                self,
                "Alb5xx",
                alarm_description="ALB 5XX > 10 in 5 min",
                metric=alb.metrics.http_code_elb(
                    elbv2.HttpCodeElb.ELB_5XX_COUNT, period=cdk.Duration.minutes(5), statistic="Sum"
                ),
                threshold=10,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            ),
            cloudwatch.Alarm(
                self,
                "DbCpu",
                alarm_description="RDS CPUUtilization > 80 %",
                metric=db.metric_cpu_utilization(period=cdk.Duration.minutes(5)),
                threshold=80,
                evaluation_periods=2,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            ),
            cloudwatch.Alarm(
                self,
                "UnhealthyHosts",
                alarm_description="api target group has an unhealthy task",
                metric=target_group.metrics.unhealthy_host_count(period=cdk.Duration.minutes(1)),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            ),
        ]
        for alarm in alarms:
            alarm.add_alarm_action(alarm_action)

        # ------------------------------------------------------------------ outputs
        cdk.CfnOutput(self, "AlbDns", value=alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "AudioBucket", value=audio_bucket.bucket_name)
        cdk.CfnOutput(self, "DbEndpoint", value=db.db_instance_endpoint_address)
        cdk.CfnOutput(self, "RedisEndpoint", value=redis.attr_primary_end_point_address)

        self._suppress_nag(
            alb_sg=alb_sg,
            buckets=(log_bucket, audio_bucket),
            secrets=(jwt_secret, redis_auth, db.node.find_child("Secret")),
            tasks=[svc.task_definition for svc in services.values()],
            task_role=task_role,
            audio_bucket=audio_bucket,
        )

    @staticmethod
    def _suppress_nag(
        *,
        alb_sg: ec2.SecurityGroup,
        buckets: tuple[s3.Bucket, ...],
        secrets: tuple[Construct, ...],
        tasks: list[ecs.TaskDefinition],
        task_role: iam.Role,
        audio_bucket: s3.Bucket,
    ) -> None:
        """cdk-nag ERROR suppressions. Every entry is justified in ``nag-suppressions.md``."""

        def sup(rule: str, reason: str, applies_to: list[str] | None = None) -> NagPackSuppression:
            return NagPackSuppression(id=rule, reason=reason, applies_to=applies_to)

        NagSuppressions.add_resource_suppressions(
            alb_sg,
            [
                sup(
                    "AwsSolutions-EC23",
                    "Public ALB: 80/443 from 0.0.0.0/0 is the product surface (WebSocket recorders in clinics).",
                )
            ],
        )
        for bucket in buckets:
            NagSuppressions.add_resource_suppressions(
                bucket,
                [
                    sup(
                        "AwsSolutions-S1",
                        "Server access logs would copy tenant/session object-key access patterns into a second bucket; "
                        "CloudTrail S3 data events are the planned audit path. The ALB log bucket is itself a log sink.",
                    )
                ],
            )
        for secret in secrets:
            NagSuppressions.add_resource_suppressions(
                secret,
                [
                    sup(
                        "AwsSolutions-SMG4",
                        "Rotation is out of scope (spec §8.2); rotating the JWT secret would invalidate live 15-minute tokens without a dual-key window.",
                    )
                ],
                apply_to_children=True,
            )
        for task in tasks:
            NagSuppressions.add_resource_suppressions(
                task,
                [
                    sup(
                        "AwsSolutions-ECS2",
                        "Environment variables carry only endpoints, ports and feature flags; every credential is injected through the `secrets` map.",
                    )
                ],
            )
        NagSuppressions.add_resource_suppressions(
            task_role,
            [
                sup(
                    "AwsSolutions-IAM5",
                    "Object-level access to the audio bucket is scoped to `bucket/*`: keys are {tenant}/{session}/{seq}.bin and the runtime must write and crypto-shred every object.",
                    applies_to=[
                        f"Resource::<{cdk.Stack.of(audio_bucket).get_logical_id(audio_bucket.node.default_child)}.Arn>/*"
                    ],  # type: ignore[arg-type]
                )
            ],
            apply_to_children=True,
        )
