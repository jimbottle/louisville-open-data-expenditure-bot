# Lou on AWS — the stack (`LouStack`)

CDK (Python) definition of the serverless deployment: Lambda container with
response streaming, a Function URL locked to CloudFront by Origin Access
Control, one DynamoDB table for shared state, an S3 bucket for refresh inputs,
a log group with retention, and an opt-in WAF rate rule. No VPC, no NAT, no
API Gateway — see the docstring in `lou_stack.py` for why each of those is a
constraint and not a preference.

```
viewer ─HTTPS─▶ CloudFront ─OAC─▶ Function URL (RESPONSE_STREAM) ─▶ Lambda lou-bot
                                                                     ├─ DynamoDB lou-state
                                                                     └─ SSM /lou/prod/*
```

## Files

| file | purpose |
|---|---|
| `app.py` | the CDK app: one stack, `lou0` bootstrap qualifier, `Project`/`ManagedBy` tags on everything |
| `lou_stack.py` | the resources |
| `cdk.json` | CLI config + tunables as context (`lou:memoryMb`, `lou:reservedConcurrency`, `lou:ssmPath`, `lou:waf`) |
| `deploy.sh` | synth / diff / deploy + the mandatory end-to-end verification |
| `../../tests/test_cdk_stack.py` | assertions on the synthesized template (the invariants below) |

## Invariants the tests pin

- Function name `lou-bot`, role `/lou/lou-lambda-exec` **with** `LouPermissionsBoundary`
  (the deploy guardrails deny anything else).
- Function URL: `AWS_IAM` + `RESPONSE_STREAM`, and **both** resource-policy grants
  (`InvokeFunctionUrl` and `InvokeFunction` via URL) for `cloudfront.amazonaws.com`
  scoped to this distribution. One grant alone is a 403 on every request.
- CloudFront: OAC on the origin, caching disabled, `AllViewerExceptHostHeader`,
  compression off, all methods allowed.
- Reserved concurrency and log retention are properties, not console clicks.
- No VPC configuration on the function; no CDK custom resources in the template.
- DynamoDB table `lou-state`, `pk` string key, TTL on `ttl`, on-demand.
- Every resource that supports tags carries `Project=lou`.

## The build plane (scheduled refresh, louisville-open-data-0nu)

`lou-refresh` is a CodeBuild project (arm64, Docker-capable, 3 h timeout)
started by EventBridge Scheduler on the 1st of each month at 09:00 UTC, or by
hand: `aws codebuild start-build --project-name lou-refresh --profile lou`.
Its buildspec (in `lou_stack.py`) clones `main`, runs
`refresh_data.py --skip-graph` + `rag.py ingest`, materialises the DuckDB
artifact, snapshots `data/` to `s3://lou-data-…/snapshots/<date>/` (90-day
lifecycle) and `latest/`, runs `cdk deploy` (new image, new function
version), verifies `/api/health` through CloudFront, clears the response
cache (`DELETE /api/cache` with the token from SSM) and re-warms it with
`warm_cache.py`. A FAILED/STOPPED/TIMED_OUT build hits the `lou-alerts`
topic via the `lou-refresh-failed` rule. The build runs as the
human-created `/lou/lou-build` role (see `infra/iam/README.md` step 6b).

## Deploy

```bash
pip install -r infra/cdk/requirements.txt
aws sts get-caller-identity --profile lou           # MFA prompt; must be assumed-role/lou-deploy
./infra/cdk/deploy.sh diff                          # free
./infra/cdk/deploy.sh                               # Tier 2: creates billable-in-principle resources
```

`deploy.sh` ends with the verification CLAUDE.md requires for any deploy: a
warm invoke, `/api/health` through CloudFront, and a POST to `/api/ask` that
must answer `200 text/event-stream`. The POST carries `x-amz-content-sha256`
because OAC signs the body hash (louisville-open-data-22e).

Secrets are **not** in the stack. The function reads `/lou/prod/*` from SSM at
cold start (`app._load_secrets_from_ssm`); put them there first
(louisville-open-data-8pf). Rotating a secret is `put-parameter` + a fresh
cold start (`aws lambda update-function-configuration ... --environment`
with a nonce, or just wait for the environments to recycle).

## Cutover (louisville-open-data-lla)

`cutover.sh` sequences it so nothing user-facing changes until the last step,
and that step is yours in Cloudflare:

1. `./infra/cdk/cutover.sh cert` — requests an ACM certificate for the
   hostname (free, us-east-1, tagged `Project=lou`) and prints the validation
   CNAME. Add it in Cloudflare as DNS-only.
2. `./infra/cdk/cutover.sh wait` — until ACM reports ISSUED.
3. `./infra/cdk/cutover.sh deploy` — the stack with the hostname and the
   certificate attached (`LOU_DOMAIN` / `LOU_CERT_ARN` context; both or
   neither). Users still reach the Air; CloudFront merely starts answering
   for the name.
4. `./infra/cdk/cutover.sh pretest` — pins the hostname to CloudFront with
   `curl --resolve` and runs the full verification (TLS, health, SSE probe, a
   real streamed answer) with public DNS untouched. This is the rollback
   rehearsal the issue asks for: if it fails there is nothing to undo.
5. Cloudflare: change the `louisville.raylytics.io` record from the
   cloudflared tunnel to a CNAME to the `CloudFrontDomain` output, DNS-only
   (grey cloud). Proxying through both adds a hop and a second place for an
   `/api` block to hide, the documented 2026 precedent.
6. `./infra/cdk/cutover.sh verify` — the mandatory end-to-end check through
   the real hostname. Then on the Air: stop the container but keep it
   (`docker stop louisville-bot`), and set `CONTAINER=` in the heartbeat
   LaunchAgent; the tunnel can stay up.
7. Rollback at any point: `./infra/cdk/cutover.sh rollback` prints it (DNS
   back, `docker start`). Decommission the container only after a week of
   clean monitoring.

Once bound, the hostname is durable: `deploy.sh` reads `PublicDomain` and
`CertificateArn` from the live stack and re-applies them on every deploy that
was not given `LOU_DOMAIN`/`LOU_CERT_ARN`, and its post-deploy probe hits the
hostname pinned to CloudFront. Detaching it is an explicit `LOU_DROP_DOMAIN=1`.
`tests/test_deploy_script.py` pins that against stubbed CLIs.

## Destroy

`npx aws-cdk@2 destroy LouStack --profile lou` removes everything except the
data bucket (RETAIN) and the bootstrap. The deploy role cannot delete the
boundary or its own policies by design.
