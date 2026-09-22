"""LouStack — the whole serverless deployment of the Lou bot as code.

    viewer ──HTTPS──▶ CloudFront ──OAC (SigV4)──▶ Lambda Function URL (RESPONSE_STREAM)
                                                       │
                                        Lambda (container: app + DuckDB artifact)
                                                       │
                                     DynamoDB lou-state (rate limit, cache, counters)
                                     SSM /lou/prod/*   (secrets, read at cold start)
                                     S3 lou-data-*     (refresh inputs; offline job)

Design constraints, each with a reason recorded in LOU_MIGRATION_COMPAT.md or
the bd epic louisville-open-data-ru6:

* NO VPC, NO NAT. A VPC-attached function needs a NAT (~$32/mo) to reach the
  LLM providers; that alone exceeds the whole cost envelope. The deploy role's
  guardrails deny ec2:CreateVpc outright, so this cannot regress by accident.
* NO API Gateway. Its 29 s hard timeout cannot carry an answer that can run
  past a minute; a Function URL in response-stream mode can (spike 4l4).
* Everything is named lou-* / under /lou/ and tagged Project=lou — the only
  namespace the scoped deploy role may touch, and what the budget filters on.
* No CDK custom resources: their provider roles are created outside /lou/
  and would be denied. So log retention is an explicit LogGroup, not the
  logRetention prop, and OAC uses the native L2 support.
* WAF is written but OFF by default (context lou:waf): a CloudFront web ACL
  is $5/mo + $1/rule before traffic, which by itself breaks the ~$0 idle
  target. The DynamoDB limiter in the app plus reserved concurrency are the
  circuit breakers; flip the flag if abuse ever warrants paying for the edge.
"""
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Size,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_wafv2 as wafv2,
)
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNCTION_NAME = "lou-bot"
TABLE_NAME = "lou-state"
# AWS/WAFV2 publishes the WebACL dimension as the ACL's VisibilityConfig
# MetricName, NOT its name — the alarm and the ACL must share this string.
WAF_METRIC_NAME = "lou-edge"


class LouStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        memory_mb = int(ctx("lou:memoryMb") or 1769)
        reserved = int(ctx("lou:reservedConcurrency") or 10)
        ssm_path = str(ctx("lou:ssmPath") or "/lou/prod").rstrip("/")
        waf_on = bool(ctx("lou:waf"))

        # ── shared state ────────────────────────────────────────────────────
        # One on-demand table for the three things that are per-container on
        # Lambda (state_store.py): rate-limit buckets, the response cache and
        # the counters behind /api/health. TTL reclaims buckets and old cache
        # versions. Contents are rebuildable (warm_cache.py), so DESTROY.
        table = dynamodb.Table(
            self, "State",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ── data refresh inputs ─────────────────────────────────────────────
        # The 531 MB of CSVs are build inputs for the offline refresh job
        # (louisville-open-data-0nu), never read by the serving function — the
        # DuckDB artifact is baked into the image. Name carries account+region
        # (the S3 naming rule the deploy policies enforce; see infra/iam).
        data_bucket = s3.Bucket(
            self, "Data",
            bucket_name=f"lou-data-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ── execution role ──────────────────────────────────────────────────
        # Explicit name under /lou/ WITH the permissions boundary: the deploy
        # guardrails deny CreateRole anywhere else or without it. The boundary
        # is the runtime ceiling; the inline policy below is what the function
        # actually gets (logs, its table, its secrets — nothing sideways).
        role = iam.Role(
            self, "ExecRole",
            role_name="lou-lambda-exec",
            path="/lou/",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            permissions_boundary=iam.ManagedPolicy.from_managed_policy_name(
                self, "Boundary", "LouPermissionsBoundary"),
            description="Runtime role for the Lou bot Lambda",
        )
        # Every action here must also be allowed by LouPermissionsBoundary
        # (the effective permission is the intersection); tests/test_cdk_stack.py
        # checks that against infra/iam/lou-permissions-boundary.json.
        role.add_to_policy(iam.PolicyStatement(
            sid="ReadSecretsAtColdStart",
            actions=["ssm:GetParameter", "ssm:GetParameters"],   # by name; no path listing
            resources=[self.format_arn(service="ssm", resource="parameter", resource_name="lou/*")],
        ))
        role.add_to_policy(iam.PolicyStatement(
            sid="DecryptSecureStringsViaSsmOnly",
            actions=["kms:Decrypt"],
            resources=["*"],
            conditions={"StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}},
        ))
        role.add_to_policy(iam.PolicyStatement(
            sid="StateTable",
            # Exactly what state_store.py calls — not grant_read_write_data,
            # which also grants stream actions the boundary does not.
            actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                     "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem"],
            resources=[table.table_arn],
        ))

        # ── logs ────────────────────────────────────────────────────────────
        log_group = logs.LogGroup(
            self, "Logs",
            log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        log_group.grant_write(role)

        # ── the function ────────────────────────────────────────────────────
        # Container image (Dockerfile.lambda) built by CDK from the repo root
        # and pushed to the lou0 bootstrap ECR repo; .dockerignore keeps the
        # CSVs out of the context. arm64: cheaper per ms, native on the build
        # machine. Sizing from spike/lambda-image/results.md.
        fn = lambda_.DockerImageFunction(
            self, "Fn",
            function_name=FUNCTION_NAME,
            description="Lou bot: FastAPI + DuckDB behind Lambda Web Adapter (response streaming)",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=str(REPO_ROOT),
                file="Dockerfile.lambda",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            architecture=lambda_.Architecture.ARM_64,
            memory_size=memory_mb,
            timeout=Duration.seconds(120),
            ephemeral_storage_size=Size.mebibytes(1024),
            # Hard compute ceiling: a pathological request bills ~460 GB-s
            # (LOU_MIGRATION_COMPAT.md risk #2); ten of them at once is the
            # most the free tier is ever exposed to.
            reserved_concurrent_executions=reserved,
            role=role,
            log_group=log_group,
            environment={
                # Everything else (PREBUILT_DB, DATA_DIR, STATS_DIR, DUCKDB_TEMP_DIR,
                # LWA settings) is baked into Dockerfile.lambda.
                "STATE_BACKEND": "dynamodb",
                "STATE_TABLE": table.table_name,
                "CLIENT_IP_SOURCE": "cloudfront",     # behind OAC; see app._client_ip
                "SSM_PARAMETER_PATH": ssm_path,       # OPENROUTER_API_KEY, CEREBRAS_PAID_API_KEY, ADMIN_TOKEN
            },
        )

        # ── Function URL, streaming, IAM-locked, CloudFront-only ────────────
        fn_url = fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )

        # ── CloudFront ──────────────────────────────────────────────────────
        # OAC signs origin requests with SigV4, so the URL above is reachable
        # ONLY through this distribution (a direct request is 403). Caching is
        # off (every answer is a live SSE stream); Host is dropped from the
        # origin request because Function URLs reject a forwarded Host;
        # compression stays off — it would buffer the stream (spike 4l4).
        web_acl = self._web_acl() if waf_on else None
        # Custom origin request policy rather than the managed
        # AllViewerExceptHostHeader: that one forwards viewer headers only, and
        # the rate limiter keys on CloudFront-Viewer-Address, a CloudFront-added
        # header. Allow-list the viewer headers the app actually reads plus that
        # one; Host stays out (Function URLs reject a forwarded Host).
        origin_request_policy = cloudfront.OriginRequestPolicy(
            self, "OriginRequest",
            origin_request_policy_name="lou-viewer-headers-and-address",
            comment="App headers + CloudFront-Viewer-Address; never Host",
            # x-amz-content-sha256 (the OAC body hash, 22e) is deliberately NOT
            # listed: CloudFront rejects x-amz-* headers in a policy ("not
            # allowed") because it consumes them for SigV4 itself — the spike's
            # POSTs through OAC never had it forwarded and worked.
            header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list(
                "CloudFront-Viewer-Address", "Content-Type", "Accept", "Accept-Language",
                "User-Agent", "Origin", "X-Admin-Token"),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
        )
        dist = cloudfront.Distribution(
            self, "Cdn",
            comment="Lou bot (lou-bot Function URL, OAC)",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin.with_origin_access_control(
                    fn_url,
                    read_timeout=Duration.seconds(60),
                    keepalive_timeout=Duration.seconds(5),
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=origin_request_policy,
                compress=False,
            ),
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            web_acl_id=web_acl.attr_arn if web_acl else None,
        )

        # Since Oct 2025 a Function URL needs BOTH grants: InvokeFunctionUrl
        # (URL auth) AND InvokeFunction restricted to via-URL calls. The OAC
        # helper adds the first; with only that, every request is a 403
        # (louisville-open-data-bac, found the hard way in spike 4l4).
        fn.add_permission(
            "CloudFrontInvokeViaUrl",
            principal=iam.ServicePrincipal("cloudfront.amazonaws.com"),
            action="lambda:InvokeFunction",
            invoked_via_function_url=True,
            source_arn=self.format_arn(
                service="cloudfront", region="", resource="distribution",
                resource_name=dist.distribution_id),
        )

        # ── alarms ──────────────────────────────────────────────────────────
        # Internal signals the external dead-man's switch (healthchecks.io,
        # monitoring/README.md) cannot see: function errors, throttles at the
        # concurrency ceiling (the abuse signal), and requests near the 120 s
        # timeout. One SNS topic; the email is a context value (lou:alertEmail,
        # kept out of git) so the public repo does not carry an address.
        # Alarms are $0.10/month each — three of them, inside the envelope.
        alerts = sns.Topic(self, "Alerts", topic_name="lou-alerts", display_name="Lou bot alerts")
        alert_email = ctx("lou:alertEmail")
        if alert_email:
            alerts.add_subscription(subs.EmailSubscription(str(alert_email)))
        notify = cw_actions.SnsAction(alerts)

        def alarm(cid: str, name: str, metric: cw.Metric, threshold: float, description: str,
                  periods: int = 1, comparison=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD):
            a = cw.Alarm(
                self, cid, alarm_name=name, alarm_description=description, metric=metric,
                threshold=threshold, evaluation_periods=periods, comparison_operator=comparison,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(notify)
            a.add_ok_action(notify)
            return a

        alarm("ErrorsAlarm", f"{FUNCTION_NAME}-errors",
              fn.metric_errors(period=Duration.minutes(5), statistic="Sum"), 1,
              "lou-bot: one or more invocation errors (init failure, timeout, unhandled exception) in 5 min")
        alarm("ThrottlesAlarm", f"{FUNCTION_NAME}-throttles",
              fn.metric_throttles(period=Duration.minutes(5), statistic="Sum"), 1,
              f"lou-bot: reserved concurrency ({reserved}) hit — a burst or abuse; the DynamoDB limiter should have caught it first")
        alarm("DurationAlarm", f"{FUNCTION_NAME}-duration-near-timeout",
              fn.metric_duration(period=Duration.minutes(5), statistic="Maximum"), 110_000,
              "lou-bot: a request ran within 10 s of the 120 s timeout (stalled upstream stream or pathological retry ladder)")
        if web_acl:
            alarm("WafBlockedAlarm", "lou-edge-blocked-spike",
                  cw.Metric(namespace="AWS/WAFV2", metric_name="BlockedRequests", statistic="Sum",
                            period=Duration.minutes(5),
                            dimensions_map={"WebACL": WAF_METRIC_NAME, "Region": "Global", "Rule": "ALL"}),
                  50, "lou edge: the WAF rate rule is blocking a spike of requests")

        # ── outputs ─────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
        cdk.CfnOutput(self, "CloudFrontUrl", value=f"https://{dist.distribution_domain_name}/")
        cdk.CfnOutput(self, "DistributionId", value=dist.distribution_id)
        cdk.CfnOutput(self, "FunctionUrl", value=fn_url.url,
                      description="IAM-auth, CloudFront-only; direct requests are 403 by design")
        cdk.CfnOutput(self, "FunctionName", value=fn.function_name)
        cdk.CfnOutput(self, "StateTable", value=table.table_name)
        cdk.CfnOutput(self, "DataBucket", value=data_bucket.bucket_name)
        cdk.CfnOutput(self, "LogGroup", value=log_group.log_group_name)

    def _web_acl(self) -> wafv2.CfnWebACL:
        """Optional edge rate rule: blocks an IP past 300 requests per 5 min
        before Lambda is ever invoked. Costs $5/mo + $1 for the rule, hence
        opt-in (context lou:waf=true)."""
        return wafv2.CfnWebACL(
            self, "WebAcl",
            name="lou-edge-rate-limit",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True, metric_name=WAF_METRIC_NAME, sampled_requests_enabled=True),
            rules=[wafv2.CfnWebACL.RuleProperty(
                name="lou-per-ip-rate",
                priority=0,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=300, aggregate_key_type="IP")),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True, metric_name="lou-per-ip-rate",
                    sampled_requests_enabled=True),
            )],
        )
