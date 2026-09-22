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
    aws_certificatemanager as acm,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_codebuild as codebuild,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_scheduler_targets as scheduler_targets,
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
        # Cutover (louisville-open-data-lla): the public hostname + an ACM
        # certificate for it in us-east-1, requested and DNS-validated OUTSIDE
        # the stack (infra/cdk/cutover.sh) so CloudFormation never waits on a
        # human adding a record. Both or neither; until then the distribution
        # answers only on its *.cloudfront.net name.
        domain = str(ctx("lou:domain") or "").strip() or None
        cert_arn = str(ctx("lou:certificateArn") or "").strip() or None
        if bool(domain) != bool(cert_arn):
            raise ValueError("lou:domain and lou:certificateArn must be set together")

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
            lifecycle_rules=[s3.LifecycleRule(
                id="expire-refresh-snapshots",
                prefix="snapshots/",
                expiration=Duration.days(90),
                abort_incomplete_multipart_upload_after=Duration.days(2),
            )],
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
            domain_names=[domain] if domain else None,
            certificate=acm.Certificate.from_certificate_arn(self, "Cert", cert_arn) if cert_arn else None,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021 if domain else None,
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

        # ── the build plane: scheduled data refresh ─────────────────────────
        # (louisville-open-data-0nu) CodeBuild, not Lambda: the refresh pulls
        # 531 MB of CSVs plus KY SOS lookups (well past the 15-minute cap) and
        # then builds and pushes the container image, which needs Docker. The
        # build role is NOT created here: the guardrails force the runtime
        # boundary onto every role the stack creates, and that boundary denies
        # deploying (sts:AssumeRole, lambda:UpdateFunctionCode) by design. So
        # the human creates /lou/lou-build once (infra/iam/README.md) with the
        # same deploy ceiling the human's own principal has, trusting both
        # codebuild and scheduler, and the stack only references it.
        build_role_arn = ctx("lou:buildRoleArn") or self.format_arn(
            service="iam", region="", resource="role", resource_name="lou/lou-build")
        build_role = iam.Role.from_role_arn(self, "BuildRole", build_role_arn, mutable=False)
        build_logs = logs.LogGroup(
            self, "BuildLogs", log_group_name="/lou/build",
            retention=logs.RetentionDays.ONE_MONTH, removal_policy=cdk.RemovalPolicy.DESTROY)
        build = codebuild.Project(
            self, "Refresh",
            project_name="lou-refresh",
            description="Monthly data refresh: pull CSVs, profiles, corpus -> DuckDB artifact -> image -> deploy -> re-warm cache",
            role=build_role,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,   # Docker, for the arm64 image build
            ),
            environment_variables={
                "LOU_ALERT_EMAIL": codebuild.BuildEnvironmentVariable(value=str(alert_email or "")),
                "LOU_DATA_BUCKET": codebuild.BuildEnvironmentVariable(value=data_bucket.bucket_name),
                "LOU_SSM_PATH": codebuild.BuildEnvironmentVariable(value=ssm_path),
                "LOU_REPO": codebuild.BuildEnvironmentVariable(
                    value=str(ctx("lou:repoUrl") or "https://github.com/jimbottle/louisville-open-data-expenditure-bot.git")),
            },
            timeout=Duration.hours(3),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=build_logs)),
            build_spec=codebuild.BuildSpec.from_object(self._refresh_buildspec()),
        )
        # Monthly, 1st at 09:00 UTC (early morning US Eastern). Manual runs:
        # aws codebuild start-build --project-name lou-refresh --profile lou
        # In its own lou-* schedule group: the deploy policy scopes Scheduler to
        # schedule/lou-*/* and schedule-group/lou-*, and the "default" group's
        # ARN (schedule/default/<name>) would fall outside that.
        group = scheduler.ScheduleGroup(self, "RefreshGroup", schedule_group_name="lou-refresh",
                                        removal_policy=cdk.RemovalPolicy.DESTROY)
        sched = scheduler.Schedule(
            self, "RefreshSchedule",
            schedule_name="lou-refresh-monthly",
            schedule_group=group,
            description="Lou data refresh + redeploy",
            schedule=scheduler.ScheduleExpression.cron(minute="0", hour="9", day="1", month="*", year="*"),
            target=scheduler_targets.CodeBuildStartBuild(build, role=build_role),
        )
        # The L2 emits the group NAME, not a Ref, so CloudFormation would
        # otherwise create both in parallel and CreateSchedule can race the
        # group ("schedule group does not exist").
        sched.node.add_dependency(group)
        # A failed or stopped build is a silent stale-data outage otherwise.
        events.Rule(
            self, "RefreshFailed",
            rule_name="lou-refresh-failed",
            description="Lou refresh build failed/stopped/timed out -> lou-alerts",
            event_pattern=events.EventPattern(
                source=["aws.codebuild"],
                detail_type=["CodeBuild Build State Change"],
                detail={"project-name": [build.project_name], "build-status": ["FAILED", "STOPPED", "TIMED_OUT"]},
            ),
            targets=[events_targets.SnsTopic(alerts)],
        )

        # ── outputs ─────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "RefreshProject", value=build.project_name)
        cdk.CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
        cdk.CfnOutput(self, "CloudFrontUrl", value=f"https://{dist.distribution_domain_name}/")
        cdk.CfnOutput(self, "CloudFrontDomain", value=dist.distribution_domain_name,
                      description="CNAME target for the public hostname at cutover")
        if domain:
            cdk.CfnOutput(self, "PublicUrl", value=f"https://{domain}/")
            # deploy.sh reads these two back so an ordinary deploy keeps the
            # binding instead of silently detaching the production hostname.
            cdk.CfnOutput(self, "PublicDomain", value=domain)
            cdk.CfnOutput(self, "CertificateArn", value=cert_arn)
        cdk.CfnOutput(self, "DistributionId", value=dist.distribution_id)
        cdk.CfnOutput(self, "FunctionUrl", value=fn_url.url,
                      description="IAM-auth, CloudFront-only; direct requests are 403 by design")
        cdk.CfnOutput(self, "FunctionName", value=fn.function_name)
        cdk.CfnOutput(self, "StateTable", value=table.table_name)
        cdk.CfnOutput(self, "DataBucket", value=data_bucket.bucket_name)
        cdk.CfnOutput(self, "LogGroup", value=log_group.log_group_name)

    @staticmethod
    def _refresh_buildspec() -> dict:
        """The scheduled refresh, end to end, with no manual step
        (louisville-open-data-0nu). Each phase fails the build on error, which
        the RefreshFailed rule turns into an alert."""
        return {
            "version": "0.2",
            "env": {
                # ADMIN_TOKEN gates /api/cache; read from SSM at build time,
                # never stored on the project.
                "parameter-store": {"ADMIN_TOKEN": "/lou/prod/ADMIN_TOKEN"},
            },
            "phases": {
                "install": {
                    "runtime-versions": {"python": "3.12", "nodejs": "20"},
                    "commands": [
                        # CodeBuild keeps the working directory between commands,
                        # so every command below anchors itself with an absolute cd.
                        "git clone --depth 1 \"$LOU_REPO\" \"$CODEBUILD_SRC_DIR/src\" && git -C \"$CODEBUILD_SRC_DIR/src\" rev-parse --short HEAD",
                        "cd \"$CODEBUILD_SRC_DIR/src\" && pip install -q -r requirements.txt -r infra/cdk/requirements.txt beautifulsoup4",
                    ],
                },
                "build": {
                    "commands": [
                        # 1. Pull every dataset, rebuild contractor profiles (+SOS), re-ingest the corpus.
                        #    No Neo4j on this plane (graph/ is local tooling).
                        "cd \"$CODEBUILD_SRC_DIR/src\" && python refresh_data.py --skip-graph",
                        "cd \"$CODEBUILD_SRC_DIR/src\" && python rag.py ingest",
                        # 2. The serving artifact (schema snapshot included).
                        "cd \"$CODEBUILD_SRC_DIR/src\" && python data_model.py --materialize data/lou.duckdb",
                        # 3. Keep the inputs + artifact: a dated snapshot (90-day lifecycle) and latest/.
                        "cd \"$CODEBUILD_SRC_DIR/src\" && SNAP=$(date -u +%Y-%m-%d) && aws s3 sync data/ \"s3://$LOU_DATA_BUCKET/snapshots/$SNAP/\" --exclude '.*' --only-show-errors",
                        "cd \"$CODEBUILD_SRC_DIR/src\" && aws s3 cp data/lou.duckdb \"s3://$LOU_DATA_BUCKET/latest/lou.duckdb\" --only-show-errors && aws s3 cp data/rag_documents.duckdb \"s3://$LOU_DATA_BUCKET/latest/rag_documents.duckdb\" --only-show-errors",
                        # 4. Build + push the image and update the function (the stack is the deploy).
                        "cd \"$CODEBUILD_SRC_DIR/src/infra/cdk\" && npx --yes aws-cdk@2 -c \"lou:alertEmail=$LOU_ALERT_EMAIL\" deploy LouStack --require-approval never --outputs-file /tmp/outputs.json",
                    ],
                },
                "post_build": {
                    "commands": [
                        # 5. Verify through CloudFront, invalidate the cache (data changed under it), re-warm.
                        "export CF=$(python3 -c \"import json; print(json.load(open('/tmp/outputs.json'))['LouStack']['CloudFrontUrl'])\") && curl -sf --max-time 60 \"${CF}api/health\" >/dev/null",
                        # Fail CLOSED: refresh_data.clear_response_cache is deliberately
                        # non-fatal for the self-hosted flow; here a wrong token or an
                        # edge block would otherwise leave last month's answers cached
                        # while the build reports SUCCEEDED.
                        "curl -sf --max-time 30 -X DELETE \"${CF}api/cache\" -H \"X-Admin-Token: $ADMIN_TOKEN\" && echo cache-cleared",
                        "cd \"$CODEBUILD_SRC_DIR/src\" && python warm_cache.py --host \"${CF%/}\" --delay 5",
                    ],
                },
            },
        }

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
