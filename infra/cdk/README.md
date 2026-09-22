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

## Destroy

`npx aws-cdk@2 destroy LouStack --profile lou` removes everything except the
data bucket (RETAIN) and the bootstrap. The deploy role cannot delete the
boundary or its own policies by design.
