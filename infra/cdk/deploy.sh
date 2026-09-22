#!/usr/bin/env bash
# Deploy LouStack as the scoped lou-deploy role, then verify it the way the
# CLAUDE.md deploy rules require: not "the stack finished", but the API path
# answers through the public URL.
#
#   ./infra/cdk/deploy.sh            synth + diff + deploy + verify
#   ./infra/cdk/deploy.sh diff       synth + diff only (free, read-only)
#   ./infra/cdk/deploy.sh synth      render the template only
#
# Prerequisites (one time, human): infra/iam/README.md steps 0-7 (the lou
# profile + the scoped bootstrap), and the three secrets in SSM:
#   aws ssm put-parameter --profile lou --name /lou/prod/OPENROUTER_API_KEY   --type SecureString --value ...
#   aws ssm put-parameter --profile lou --name /lou/prod/CEREBRAS_PAID_API_KEY --type SecureString --value ...
#   aws ssm put-parameter --profile lou --name /lou/prod/ADMIN_TOKEN           --type SecureString --value ...
# The image build needs data/lou.duckdb (python data_model.py --materialize
# data/lou.duckdb) and data/rag_documents.duckdb in the checkout.
#
# Cost of a deploy: the image (~200 MB) in the bootstrap ECR repo (free tier
# 500 MB for 12 months, then ~$0.02/mo), a handful of CloudFormation calls,
# and the resources' idle cost of ~$0 (LOU_MIGRATION_COMPAT.md §cost).
set -euo pipefail
export AWS_PAGER="" JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
cd "$(dirname "$0")"
P="--profile lou"
STEP=${1:-deploy}

[ -f ../../data/lou.duckdb ] || { echo "data/lou.duckdb missing — run: python data_model.py --materialize data/lou.duckdb"; exit 1; }
[ -f ../../data/rag_documents.duckdb ] || { echo "data/rag_documents.duckdb missing — run: python rag.py ingest"; exit 1; }

# The CDK CLI (JS SDK) cannot drive the MFA prompt of the `lou` profile from
# a script, and does not read the AWS CLI's cached session. Hand it the session
# the CLI already holds (primed by `aws sts get-caller-identity --profile lou`)
# as environment variables — inside this process only; nothing is printed.
# Fail CLOSED: if the export fails or yields no session, stop here rather than
# let the CDK CLI fall through to the default credential chain (the
# workstation's `default` profile is another workload's key and must never
# deploy Lou). Then assert the identity the CLI will actually see.
creds=$(aws configure export-credentials $P --format env) \
  || { echo "!! no cached lou session — run: aws sts get-caller-identity --profile lou"; exit 1; }
eval "$creds"; unset creds
[ -n "${AWS_SESSION_TOKEN:-}" ] || { echo "!! export-credentials produced no session token"; exit 1; }
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
caller=$(aws sts get-caller-identity --query Arn --output text) || { echo "!! identity check failed"; exit 1; }
case "$caller" in
  *:assumed-role/lou-deploy/*) echo "== caller: $caller" ;;
  *) echo "!! refusing to run CDK as $caller (expected assumed-role/lou-deploy)"; exit 1 ;;
esac
cdk() { npx --yes aws-cdk@2 "$@"; }

case "$STEP" in
  synth) cdk synth --quiet && echo "template: cdk.out/LouStack.template.json" ;;
  diff)  cdk diff ;;
  deploy)
    cdk diff
    # Security-relevant changes (IAM statements, resource policies, security
    # groups) fail closed: the CLI's interactive prompt cannot be answered from
    # the shell this runs in, so the gate is explicit. Read `./deploy.sh diff`,
    # then re-run with LOU_ALLOW_BROADENING=1 for that one deploy. Changes with
    # no security impact deploy without it.
    if ! cdk diff --security-only --fail >/dev/null 2>&1; then
      if [ "${LOU_ALLOW_BROADENING:-}" != "1" ]; then
        echo "!! this deploy widens IAM/security state (see the diff above)."
        echo "!! re-run with LOU_ALLOW_BROADENING=1 ./infra/cdk/deploy.sh after reading it."
        exit 1
      fi
      echo "== broadening changes approved for this run (LOU_ALLOW_BROADENING=1)"
    fi
    # The image is built (arm64, needs Docker) and pushed here.
    cdk deploy --require-approval never --outputs-file cdk.out/outputs.json
    CF=$(python3 -c "import json; print(json.load(open('cdk.out/outputs.json'))['LouStack']['CloudFrontUrl'])")
    FN=$(python3 -c "import json; print(json.load(open('cdk.out/outputs.json'))['LouStack']['FunctionName'])")
    echo "== warm invoke (the one-time image-cache miss lands here, not on the first visitor)"
    aws lambda invoke $P --function-name "$FN" --cli-binary-format raw-in-base64-out \
      --payload '{"version":"2.0","routeKey":"$default","rawPath":"/api/health","rawQueryString":"","headers":{"host":"x"},"requestContext":{"http":{"method":"GET","path":"/api/health","protocol":"HTTP/1.1","sourceIp":"127.0.0.1","userAgent":"deploy"},"requestId":"x","stage":"$default"},"isBase64Encoded":false}' \
      /dev/null --query StatusCode --output text
    echo "== verify through CloudFront: $CF"
    curl -sf --max-time 30 "${CF}api/health" | python3 -c "import json,sys; d=json.load(sys.stdin); print('health:', d['status'], '| tables:', len(d['tables']))"
    body='{"question":""}'
    code_type=$(curl -sS -o /dev/null --max-time 30 -w '%{http_code} %{content_type}' \
      -X POST "${CF}api/ask" -H 'Content-Type: application/json' \
      -H "x-amz-content-sha256: $(printf '%s' "$body" | shasum -a 256 | cut -d' ' -f1)" --data "$body")
    echo "SSE probe: $code_type"
    case "$code_type" in "200 text/event-stream"*) echo "== DEPLOY VERIFIED: $CF" ;; *) echo "!! API probe failed"; exit 1 ;; esac
    ;;
  *) echo "usage: $0 [synth|diff|deploy]"; exit 2 ;;
esac
