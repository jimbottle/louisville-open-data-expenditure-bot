"""The synthesized LouStack template (infra/cdk) holds the invariants the
migration depends on. Synth needs no AWS account and no Docker (the image asset
is only staged, not built).
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infra" / "cdk"))
os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from lou_stack import LouStack  # noqa: E402


def _synth(**context) -> Template:
    app = cdk.App(context={"lou:waf": False, **context})
    stack = LouStack(app, "LouStack",
                     env=cdk.Environment(account="012146975534", region="us-east-1"),
                     synthesizer=cdk.DefaultStackSynthesizer(qualifier="lou0"))
    cdk.Tags.of(app).add("Project", "lou")
    cdk.Tags.of(app).add("ManagedBy", "cdk")
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _synth()


@pytest.fixture(scope="module")
def resources(template) -> dict:
    return template.to_json()["Resources"]


def _only(resources, rtype):
    found = [r for r in resources.values() if r["Type"] == rtype]
    assert len(found) == 1, f"expected exactly one {rtype}, got {len(found)}"
    return found[0]["Properties"]


# ── namespace + guardrails ──────────────────────────────────────────────────

def test_function_is_named_and_sized_as_measured(resources):
    fn = _only(resources, "AWS::Lambda::Function")
    assert fn["FunctionName"] == "lou-bot"
    assert fn["Architectures"] == ["arm64"]
    assert fn["MemorySize"] == 1769 and fn["Timeout"] == 120
    assert fn["EphemeralStorage"] == {"Size": 1024}
    assert fn["ReservedConcurrentExecutions"] == 10
    assert fn["PackageType"] == "Image"
    assert "VpcConfig" not in fn, "no VPC — a NAT alone breaks the cost envelope"
    env = fn["Environment"]["Variables"]
    assert env["STATE_BACKEND"] == "dynamodb" and env["CLIENT_IP_SOURCE"] == "cloudfront"
    assert env["SSM_PARAMETER_PATH"] == "/lou/prod"
    assert "OPENROUTER_API_KEY" not in env and "ADMIN_TOKEN" not in env, "secrets never in the template"


def test_execution_role_is_under_lou_path_with_the_boundary(resources):
    role = _only(resources, "AWS::IAM::Role")
    assert role["RoleName"] == "lou-lambda-exec" and role["Path"] == "/lou/"
    boundary = json.dumps(role["PermissionsBoundary"])
    assert "LouPermissionsBoundary" in boundary
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "lambda.amazonaws.com"}


def test_role_policy_is_minimal(resources):
    policies = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::IAM::Policy"]
    actions = set()
    for p in policies:
        for st in p["PolicyDocument"]["Statement"]:
            a = st["Action"]
            actions.update(a if isinstance(a, list) else [a])
    assert {"ssm:GetParametersByPath", "kms:Decrypt", "dynamodb:PutItem", "logs:PutLogEvents"} <= actions
    assert not any(a.startswith(("iam:", "ec2:", "s3:Delete", "lambda:")) for a in actions), actions


def test_no_custom_resources(resources):
    """A CDK custom resource brings a provider Lambda + role outside /lou/,
    which the deploy guardrails deny. Log retention is an explicit LogGroup."""
    types = {r["Type"] for r in resources.values()}
    assert not any(t.startswith("Custom::") or t == "AWS::CloudFormation::CustomResource" for t in types), types


def test_log_group_has_retention(resources):
    lg = _only(resources, "AWS::Logs::LogGroup")
    assert lg["LogGroupName"] == "/aws/lambda/lou-bot"
    assert lg["RetentionInDays"] == 7


def test_every_taggable_resource_carries_project_lou(resources):
    taggable = ("AWS::Lambda::Function", "AWS::DynamoDB::Table", "AWS::S3::Bucket",
                "AWS::IAM::Role", "AWS::Logs::LogGroup", "AWS::CloudFront::Distribution")
    for r in resources.values():
        if r["Type"] in taggable:
            props = r["Properties"]
            tags = props.get("Tags") or props.get("DistributionConfig", {}).get("Tags")
            if r["Type"] == "AWS::CloudFront::Distribution":
                tags = props.get("Tags")
            assert tags is not None, f"{r['Type']} has no Tags"
            assert {"Key": "Project", "Value": "lou"} in tags, r["Type"]


# ── streaming path: Function URL + both grants + CloudFront/OAC ─────────────

def test_function_url_is_iam_locked_and_streaming(resources):
    url = _only(resources, "AWS::Lambda::Url")
    assert url["AuthType"] == "AWS_IAM" and url["InvokeMode"] == "RESPONSE_STREAM"


def test_both_function_url_grants_exist_for_cloudfront_only(resources):
    """Since Oct 2025 a Function URL needs InvokeFunctionUrl AND
    InvokeFunction(InvokedViaFunctionUrl); with one, every request is 403
    (louisville-open-data-bac)."""
    perms = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::Lambda::Permission"]
    by_action = {p["Action"]: p for p in perms}
    assert set(by_action) == {"lambda:InvokeFunctionUrl", "lambda:InvokeFunction"}, by_action.keys()
    for p in perms:
        assert p["Principal"] == "cloudfront.amazonaws.com"
        assert "SourceArn" in p and "distribution" in json.dumps(p["SourceArn"])
    # The auth-type condition is optional on the URL grant (the spike's working
    # policy omitted it); if present it must match the URL's AWS_IAM.
    assert by_action["lambda:InvokeFunctionUrl"].get("FunctionUrlAuthType") in (None, "AWS_IAM")
    assert by_action["lambda:InvokeFunction"].get("InvokedViaFunctionUrl") is True


def test_cloudfront_uses_oac_no_cache_no_compress_no_host(resources):
    dist = _only(resources, "AWS::CloudFront::Distribution")["DistributionConfig"]
    oac = _only(resources, "AWS::CloudFront::OriginAccessControl")["OriginAccessControlConfig"]
    assert oac["OriginAccessControlOriginType"] == "lambda" and oac["SigningBehavior"] == "always"
    origin = dist["Origins"][0]
    assert "OriginAccessControlId" in origin
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert origin["CustomOriginConfig"]["OriginReadTimeout"] == 60
    b = dist["DefaultCacheBehavior"]
    assert b["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"          # Managed-CachingDisabled
    assert b["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"  # Managed-AllViewerExceptHostHeader
    assert b["Compress"] is False
    assert set(b["AllowedMethods"]) >= {"GET", "POST", "OPTIONS"}
    assert b["ViewerProtocolPolicy"] == "redirect-to-https"
    assert dist["HttpVersion"] == "http2and3" and dist["PriceClass"] == "PriceClass_100"
    assert "WebACLId" not in dist, "WAF is opt-in ($5/mo base)"


# ── state + data ────────────────────────────────────────────────────────────

def test_state_table_matches_state_store_contract(resources):
    t = _only(resources, "AWS::DynamoDB::Table")
    assert t["TableName"] == "lou-state"
    assert t["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert t["AttributeDefinitions"] == [{"AttributeName": "pk", "AttributeType": "S"}]
    assert t["BillingMode"] == "PAY_PER_REQUEST"
    assert t["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}


def test_data_bucket_follows_the_naming_rule_and_is_private(resources):
    b = _only(resources, "AWS::S3::Bucket")
    assert b["BucketName"] == "lou-data-012146975534-us-east-1"
    assert b["PublicAccessBlockConfiguration"]["BlockPublicAcls"] is True
    assert "BucketEncryption" in b
    bucket_res = [r for r in resources.values() if r["Type"] == "AWS::S3::Bucket"][0]
    assert bucket_res.get("DeletionPolicy") == "Retain"


# ── tunables ────────────────────────────────────────────────────────────────

def test_waf_is_present_in_code_and_attaches_when_enabled():
    t = _synth(**{"lou:waf": True})
    acl = _only(t.to_json()["Resources"], "AWS::WAFv2::WebACL")
    assert acl["Scope"] == "CLOUDFRONT" and acl["Name"] == "lou-edge-rate-limit"
    rule = acl["Rules"][0]["Statement"]["RateBasedStatement"]
    assert rule["Limit"] == 300 and rule["AggregateKeyType"] == "IP"
    dist = _only(t.to_json()["Resources"], "AWS::CloudFront::Distribution")["DistributionConfig"]
    assert "WebACLId" in dist


def test_memory_and_concurrency_are_context_tunable():
    fn = _only(_synth(**{"lou:memoryMb": 2048, "lou:reservedConcurrency": 3}).to_json()["Resources"],
               "AWS::Lambda::Function")
    assert fn["MemorySize"] == 2048 and fn["ReservedConcurrentExecutions"] == 3
