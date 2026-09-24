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
    assert {"ssm:GetParameters", "kms:Decrypt", "dynamodb:PutItem", "dynamodb:Scan", "logs:PutLogEvents"} <= actions
    assert "ssm:GetParametersByPath" not in actions, "outside the boundary; the loader fetches by name"
    assert not any(a.startswith(("iam:", "ec2:", "s3:Delete", "lambda:")) for a in actions), actions


def test_no_custom_resources(resources):
    """A CDK custom resource brings a provider Lambda + role outside /lou/,
    which the deploy guardrails deny. Log retention is an explicit LogGroup."""
    types = {r["Type"] for r in resources.values()}
    assert not any(t.startswith("Custom::") or t == "AWS::CloudFormation::CustomResource" for t in types), types


def test_log_groups_have_retention(resources):
    groups = {r["Properties"]["LogGroupName"]: r["Properties"]
              for r in resources.values() if r["Type"] == "AWS::Logs::LogGroup"}
    assert set(groups) == {"/aws/lambda/lou-bot", "/lou/build"}, groups.keys()
    assert groups["/aws/lambda/lou-bot"]["RetentionInDays"] == 7
    assert groups["/lou/build"]["RetentionInDays"] == 30


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
    # Custom origin request policy: the app's viewer headers plus the
    # CloudFront-added viewer address the rate limiter keys on; never Host.
    orp = _only(resources, "AWS::CloudFront::OriginRequestPolicy")["OriginRequestPolicyConfig"]
    assert b["OriginRequestPolicyId"] == {"Ref": next(
        k for k, r in resources.items() if r["Type"] == "AWS::CloudFront::OriginRequestPolicy")}
    headers = orp["HeadersConfig"]
    assert headers["HeaderBehavior"] == "whitelist"
    names = {h.lower() for h in headers["Headers"]}
    assert {"cloudfront-viewer-address", "content-type", "x-admin-token"} <= names
    assert "host" not in names
    assert not any(n.startswith("x-amz-") for n in names), "CloudFront rejects x-amz-* in a policy; OAC handles them"
    assert orp["QueryStringsConfig"]["QueryStringBehavior"] == "all"
    assert orp["CookiesConfig"]["CookieBehavior"] == "none"
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


# ── the boundary is the effective ceiling ──────────────────────────────────

def _boundary_allows(action: str, boundary: dict) -> bool:
    import fnmatch
    allowed = denied = False
    for st in boundary["Statement"]:
        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        hit = any(fnmatch.fnmatchcase(action, a) for a in acts)
        if hit and st["Effect"] == "Allow":
            allowed = True
        if hit and st["Effect"] == "Deny":
            denied = True
    return allowed and not denied


def test_every_runtime_grant_is_inside_the_permissions_boundary(resources):
    """The exec role's effective permissions are the intersection of its
    policy and LouPermissionsBoundary. A grant the boundary does not allow is
    silently dead at runtime — exactly how ssm:GetParametersByPath and
    dynamodb:Scan were missing on the first deploy attempt."""
    boundary = json.loads((ROOT / "infra" / "iam" / "lou-permissions-boundary.json").read_text())
    granted = set()
    for r in resources.values():
        if r["Type"] == "AWS::IAM::Policy":
            for st in r["Properties"]["PolicyDocument"]["Statement"]:
                a = st["Action"]
                granted.update(a if isinstance(a, list) else [a])
    assert granted, "no runtime grants found"
    outside = sorted(a for a in granted if not _boundary_allows(a, boundary))
    assert not outside, f"granted to the function but outside the boundary (dead at runtime): {outside}"


# ── alarms (louisville-open-data-5cn) ──────────────────────────────────────

def test_alarms_cover_errors_throttles_and_duration_and_notify_the_topic(resources):
    topic = _only(resources, "AWS::SNS::Topic")
    assert topic["TopicName"] == "lou-alerts"
    topic_ref = next(k for k, r in resources.items() if r["Type"] == "AWS::SNS::Topic")
    alarms = {r["Properties"]["AlarmName"]: r["Properties"]
              for r in resources.values() if r["Type"] == "AWS::CloudWatch::Alarm"}
    assert set(alarms) == {"lou-bot-errors", "lou-bot-throttles", "lou-bot-duration-near-timeout"}
    for name, a in alarms.items():
        assert a["Namespace"] == "AWS/Lambda", name
        assert a["AlarmActions"] == [{"Ref": topic_ref}] and a["OKActions"] == [{"Ref": topic_ref}], name
        assert a["TreatMissingData"] == "notBreaching", name
        assert a["Period"] == 300, name
    assert alarms["lou-bot-errors"]["MetricName"] == "Errors" and alarms["lou-bot-errors"]["Threshold"] == 1
    assert alarms["lou-bot-throttles"]["MetricName"] == "Throttles"
    d = alarms["lou-bot-duration-near-timeout"]
    assert d["MetricName"] == "Duration" and d["Statistic"] == "Maximum" and d["Threshold"] == 110000
    # No subscriber without the context value: the address stays out of git.
    assert not any(r["Type"] == "AWS::SNS::Subscription" for r in resources.values())


def test_alert_email_becomes_a_subscription_only_via_context():
    t = _synth(**{"lou:alertEmail": "ops@example.invalid"})
    sub = _only(t.to_json()["Resources"], "AWS::SNS::Subscription")
    assert sub["Protocol"] == "email" and sub["Endpoint"] == "ops@example.invalid"
    # deploy.sh reads it back so a later deploy without the variable keeps it.
    assert t.to_json()["Outputs"]["AlertEmail"]["Value"] == "ops@example.invalid"
    assert _synth().to_json()["Outputs"]["AlertEmail"]["Value"] == "none"


def test_waf_alarm_only_when_waf_is_on():
    res = _synth(**{"lou:waf": True}).to_json()["Resources"]
    alarms = {r["Properties"]["AlarmName"]: r["Properties"]
              for r in res.values() if r["Type"] == "AWS::CloudWatch::Alarm"}
    assert "lou-edge-blocked-spike" in alarms
    # The WebACL dimension is the ACL's VisibilityConfig MetricName, not its
    # name; a mismatch means a metric with no datapoints and an alarm stuck OK.
    acl = _only(res, "AWS::WAFv2::WebACL")
    dims = {d["Name"]: d["Value"] for d in alarms["lou-edge-blocked-spike"]["Dimensions"]}
    assert dims["WebACL"] == acl["VisibilityConfig"]["MetricName"]
    assert dims["WebACL"] != acl["Name"]
    assert dims["Region"] == "Global" and dims["Rule"] == "ALL"


# ── build plane: the scheduled refresh (louisville-open-data-0nu) ──────────

def test_refresh_build_uses_the_human_created_build_role_and_docker_on_arm(resources):
    proj = _only(resources, "AWS::CodeBuild::Project")
    assert proj["Name"] == "lou-refresh"
    assert "role/lou/lou-build" in json.dumps(proj["ServiceRole"])
    env = proj["Environment"]
    assert env["Type"] == "ARM_CONTAINER" and env["PrivilegedMode"] is True
    assert env["ComputeType"] == "BUILD_GENERAL1_SMALL"
    names = {v["Name"]: v for v in env["EnvironmentVariables"]}
    assert {"LOU_DATA_BUCKET", "LOU_REPO", "LOU_SSM_PATH", "LOU_ALERT_EMAIL"} <= set(names)
    assert proj["TimeoutInMinutes"] == 180
    assert "BuildLogs" in json.dumps(proj["LogsConfig"]["CloudWatchLogs"]["GroupName"])
    assert proj["Source"]["Type"] == "NO_SOURCE"
    spec = json.loads(proj["Source"]["BuildSpec"]) if isinstance(proj["Source"]["BuildSpec"], str) else proj["Source"]["BuildSpec"]
    assert spec["env"]["parameter-store"]["ADMIN_TOKEN"] == "/lou/prod/ADMIN_TOKEN"
    cmds = " ".join(spec["phases"]["build"]["commands"] + spec["phases"]["post_build"]["commands"])
    for step in ("refresh_data.py --skip-graph", "rag.py ingest", "--materialize data/lou.duckdb",
                 "s3 sync data/", "cdk@2", "deploy LouStack", "warm_cache.py"):
        assert step in cmds, step
    # The monthly deploy must keep the live hostname binding (roborev 4747).
    deploy_cmd = next(c for c in spec["phases"]["build"]["commands"] if "deploy LouStack" in c)
    assert "OutputKey=='PublicDomain'" in deploy_cmd and "OutputKey=='CertificateArn'" in deploy_cmd
    assert "lou:domain=$D" in deploy_cmd and "lou:certificateArn=$C" in deploy_cmd
    # The cache clear must be able to FAIL the build: a bare curl -f, not the
    # deliberately non-fatal refresh_data.clear_response_cache.
    assert 'curl -sf' in cmds and '-X DELETE "${CF}api/cache"' in cmds and "clear_response_cache" not in cmds
    # Every command anchors on an absolute path: CodeBuild keeps the cwd between commands.
    all_cmds = [c for ph in spec["phases"].values() for c in ph.get("commands", [])]
    assert all("$CODEBUILD_SRC_DIR" in c or c.startswith("export CF=") or c.startswith("curl ") for c in all_cmds), all_cmds
    # No CDK-created role for the build: the stack references, never creates, it.
    roles = [r["Properties"]["RoleName"] for r in resources.values() if r["Type"] == "AWS::IAM::Role"]
    assert roles == ["lou-lambda-exec"], roles


def test_refresh_is_scheduled_monthly_and_failures_alert(resources):
    sched = _only(resources, "AWS::Scheduler::Schedule")
    assert sched["Name"] == "lou-refresh-monthly"
    assert sched["ScheduleExpression"] == "cron(0 9 1 * ? *)"
    # Not the "default" group: its ARN is schedule/default/<name>, outside the
    # deploy policy's schedule/lou-*/* scope (first 0nu deploy failed on it).
    grp = _only(resources, "AWS::Scheduler::ScheduleGroup")
    assert grp["Name"] == "lou-refresh"
    assert sched["GroupName"] == "lou-refresh"
    grp_id = next(k for k, r in resources.items() if r["Type"] == "AWS::Scheduler::ScheduleGroup")
    sched_res = next(r for r in resources.values() if r["Type"] == "AWS::Scheduler::Schedule")
    assert grp_id in sched_res.get("DependsOn", []), "schedule must wait for its group (create-order race)"
    assert "role/lou/lou-build" in json.dumps(sched["Target"]["RoleArn"])
    proj_id = next(k for k, r in resources.items() if r["Type"] == "AWS::CodeBuild::Project")
    assert sched["Target"]["Arn"] == {"Fn::GetAtt": [proj_id, "Arn"]}
    rule = _only(resources, "AWS::Events::Rule")
    assert rule["Name"] == "lou-refresh-failed"
    pat = rule["EventPattern"]
    assert pat["source"] == ["aws.codebuild"]
    assert set(pat["detail"]["build-status"]) == {"FAILED", "STOPPED", "TIMED_OUT"}
    topic_ref = next(k for k, r in resources.items() if r["Type"] == "AWS::SNS::Topic")
    assert rule["Targets"][0]["Arn"] == {"Ref": topic_ref}


def test_data_bucket_expires_refresh_snapshots(resources):
    b = _only(resources, "AWS::S3::Bucket")
    rules = b["LifecycleConfiguration"]["Rules"]
    snap = next(r for r in rules if r["Id"] == "expire-refresh-snapshots")
    assert snap["Prefix"] == "snapshots/" and snap["ExpirationInDays"] == 90 and snap["Status"] == "Enabled"


# ── cutover: custom domain (louisville-open-data-lla) ──────────────────────

def test_no_custom_domain_by_default(resources):
    dist = _only(resources, "AWS::CloudFront::Distribution")["DistributionConfig"]
    assert "Aliases" not in dist or not dist["Aliases"]
    # No ViewerCertificate block at all = the default *.cloudfront.net certificate.
    assert "ViewerCertificate" not in dist or dist["ViewerCertificate"].get("CloudFrontDefaultCertificate") is True


def test_custom_domain_attaches_hostname_and_certificate():
    arn = "arn:aws:acm:us-east-1:012146975534:certificate/00000000-0000-0000-0000-000000000000"
    t = _synth(**{"lou:domain": "louisville.raylytics.io", "lou:certificateArn": arn})
    res = t.to_json()["Resources"]
    dist = _only(res, "AWS::CloudFront::Distribution")["DistributionConfig"]
    assert dist["Aliases"] == ["louisville.raylytics.io"]
    vc = dist["ViewerCertificate"]
    assert vc["AcmCertificateArn"] == arn and vc["SslSupportMethod"] == "sni-only"
    assert vc["MinimumProtocolVersion"] == "TLSv1.2_2021"
    outs = t.to_json()["Outputs"]
    assert outs["PublicUrl"]["Value"] == "https://louisville.raylytics.io/"
    # deploy.sh reads these back to keep the binding on ordinary deploys.
    assert outs["PublicDomain"]["Value"] == "louisville.raylytics.io"
    assert outs["CertificateArn"]["Value"] == arn


def test_custom_domain_requires_both_values():
    with pytest.raises(ValueError):
        _synth(**{"lou:domain": "louisville.raylytics.io"})
