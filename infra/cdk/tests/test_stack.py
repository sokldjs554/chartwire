"""Assertions from spec §12.1 against the synthesized ChartwireStack.

Run: ``python -m pytest infra/cdk/tests -q`` (offline; ~10 s for the jsii kernel).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import cx_api
from aws_cdk.assertions import Match, Template

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app import STACK_NAME, build_app  # noqa: E402


@pytest.fixture(scope="module")
def assembly(tmp_path_factory: pytest.TempPathFactory) -> cx_api.CloudAssembly:
    return build_app(tmp_path_factory.mktemp("cdk.out")).synth()


@pytest.fixture(scope="module")
def template(assembly: cx_api.CloudAssembly) -> Template:
    return Template.from_json(assembly.get_stack_by_name(STACK_NAME).template)


def test_alb_idle_timeout_3600_and_drops_invalid_headers(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {
            "Scheme": "internet-facing",
            "LoadBalancerAttributes": Match.array_with(
                [
                    {"Key": "idle_timeout.timeout_seconds", "Value": "3600"},
                    {"Key": "routing.http.drop_invalid_header_fields.enabled", "Value": "true"},
                    {"Key": "access_logs.s3.enabled", "Value": "true"},
                ]
            ),
        },
    )


def test_target_group_readyz_and_deregistration_30s(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "HealthCheckPath": "/readyz",
            "Port": 8000,
            "TargetType": "ip",
            "TargetGroupAttributes": Match.array_with(
                [{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}]
            ),
        },
    )


def test_https_listener_is_conditional_on_cert_arn(template: Template) -> None:
    template.has_parameter("CertArn", {"Type": "String", "Default": ""})
    template.has_condition("HasCert", Match.any_value())
    template.has_resource(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Condition": "HasCert", "Properties": {"Port": 443, "Protocol": "HTTPS"}},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener", {"Port": 80, "Protocol": "HTTP"}
    )


def test_rds_pg16_encrypted_isolated_forced_ssl(template: Template) -> None:
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "Engine": "postgres",
            "EngineVersion": Match.string_like_regexp("^16"),
            "DBInstanceClass": "db.t4g.medium",
            "StorageEncrypted": True,
            "MultiAZ": True,
            "BackupRetentionPeriod": 7,
            "DeletionProtection": True,
            "PubliclyAccessible": False,
        },
    )
    template.has_resource_properties(
        "AWS::RDS::DBParameterGroup",
        {"Parameters": {"shared_preload_libraries": "pg_stat_statements", "rds.force_ssl": "1"}},
    )
    template.has_resource(
        "AWS::RDS::DBInstance", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"}
    )


def test_redis7_encrypted_with_auth_token(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ElastiCache::ReplicationGroup",
        {
            "Engine": "redis",
            "EngineVersion": Match.string_like_regexp("^7"),
            "CacheNodeType": "cache.t4g.small",
            "TransitEncryptionEnabled": True,
            "AtRestEncryptionEnabled": True,
            "AutomaticFailoverEnabled": True,
            "AuthToken": {"Fn::Join": ["", Match.array_with(["{{resolve:secretsmanager:"])]},
        },
    )
    template.resource_count_is("AWS::SecretsManager::Secret", 3)


def test_audio_bucket_kms_public_block_ssl_lifecycle(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    Match.object_like({"ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}})
                ]
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "LifecycleConfiguration": {
                "Rules": [Match.object_like({"ExpirationInDays": 90, "Status": "Enabled"})]
            },
        },
    )
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Effect": "Deny", "Condition": {"Bool": {"aws:SecureTransport": "false"}}}
                        )
                    ]
                )
            }
        },
    )
    template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})


def test_three_alarms_route_to_sns(template: Template) -> None:
    template.resource_count_is("AWS::CloudWatch::Alarm", 3)
    template.resource_count_is("AWS::SNS::Topic", 1)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    seen = {a["Properties"]["MetricName"] for a in alarms.values()}
    assert seen == {"HTTPCode_ELB_5XX_Count", "CPUUtilization", "UnHealthyHostCount"}
    (topic_id,) = template.find_resources("AWS::SNS::Topic")
    for alarm in alarms.values():
        assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic_id}]


def test_three_fargate_services_with_image_parameters(template: Template) -> None:
    template.has_parameter("ImageRepo", {"Type": "String"})
    template.has_parameter("ImageTag", {"Type": "String"})
    template.resource_count_is("AWS::ECS::Service", 3)
    template.has_resource_properties("AWS::ECS::Service", {"DesiredCount": 2, "LaunchType": "FARGATE"})
    template.has_resource_properties(
        "AWS::ApplicationAutoScaling::ScalableTarget", {"MinCapacity": 2, "MaxCapacity": 6}
    )
    template.has_resource_properties(
        "AWS::ApplicationAutoScaling::ScalingPolicy",
        {"TargetTrackingScalingPolicyConfiguration": Match.object_like({"TargetValue": 60})},
    )
    roles = set()
    for task in template.find_resources("AWS::ECS::TaskDefinition").values():
        (container,) = task["Properties"]["ContainerDefinitions"]
        env = {e["Name"]: e["Value"] for e in container["Environment"]}
        roles.add(env["CHARTWIRE_ROLE"])
        assert {s["Name"] for s in container["Secrets"]} >= {"CHARTWIRE_JWT_SECRET", "CHARTWIRE_DB_PASSWORD"}
        assert container["Image"] == {"Fn::Join": ["", [{"Ref": "ImageRepo"}, ":", {"Ref": "ImageTag"}]]}
        assert task["Properties"]["Cpu"] == "1024" and task["Properties"]["Memory"] == "2048"
    assert roles == {"api", "worker", "stt-worker"}


def test_task_role_is_least_privilege(template: Template) -> None:
    """The shared task role may only wrap/unwrap under the KEK and touch audio objects."""
    policies = template.find_resources(
        "AWS::IAM::Policy", {"Properties": {"Roles": [{"Ref": Match.string_like_regexp("^TaskRole")}]}}
    )
    assert len(policies) == 1
    (policy,) = policies.values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    by_sid = {s["Sid"]: s for s in statements}
    assert set(by_sid) == {"KekWrapUnwrap", "AudioObjects"}
    assert sorted(by_sid["KekWrapUnwrap"]["Action"]) == ["kms:Decrypt", "kms:GenerateDataKey"]
    kek_arn = by_sid["KekWrapUnwrap"]["Resource"]
    assert kek_arn["Fn::GetAtt"][0].startswith("Kek") and kek_arn["Fn::GetAtt"][1] == "Arn"
    assert sorted(by_sid["AudioObjects"]["Action"]) == ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    assert json.dumps(by_sid["AudioObjects"]["Resource"]).endswith('"/*"]]}')


def test_outputs_and_no_custom_resources(template: Template) -> None:
    for name in ("AlbDns", "AudioBucket", "DbEndpoint", "RedisEndpoint"):
        template.has_output(name, Match.any_value())
    template.resource_count_is("AWS::Lambda::Function", 0)
    template.resource_count_is("AWS::CloudFormation::CustomResource", 0)


def test_cdk_nag_has_no_errors(assembly: cx_api.CloudAssembly) -> None:
    errors = [
        m.entry.data
        for m in assembly.get_stack_by_name(STACK_NAME).messages
        if m.level == cx_api.SynthesisMessageLevel.ERROR
    ]
    assert errors == []


def test_committed_template_matches_synth(template: Template) -> None:
    """CI runs ``git diff --exit-code infra/cdk/cdk.out``; this is the same check without git."""
    committed = json.loads((HERE / "cdk.out" / "ChartwireStack.template.json").read_text(encoding="utf-8"))
    assert committed == template.to_json()


def test_cdk_json_flags() -> None:
    cfg = json.loads((HERE / "cdk.json").read_text(encoding="utf-8"))
    assert cfg["versionReporting"] is False
    assert cfg["pathMetadata"] is False
    assert cfg["assetMetadata"] is False
    assert isinstance(cdk.App, type)
