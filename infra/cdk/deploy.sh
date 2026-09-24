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
OUT=${LOU_OUTPUTS_FILE:-cdk.out/outputs.json}   # overridable so the test harness stays hermetic

DATA=${LOU_DATA_DIR:-../../data}
[ -f "$DATA/lou.duckdb" ] || { echo "data/lou.duckdb missing — run: python data_model.py --materialize data/lou.duckdb"; exit 1; }
[ -f "$DATA/rag_documents.duckdb" ] || { echo "data/rag_documents.duckdb missing — run: python rag.py ingest"; exit 1; }

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
CTX=()
# Cutover: the public hostname + its ACM certificate (see cutover.sh). Both or
# neither; the stack refuses one without the other.
#
# The LIVE STACK is the durable source of truth for the binding. If the
# deployed distribution already answers for a hostname and this run was not
# given one, adopt the live values — otherwise an ordinary deploy (a build-spec
# fix, the monthly refresh's cdk deploy, another workstation) would synthesize
# a distribution with no Aliases and detach production from CloudFront with no
# error at deploy time. Dropping the hostname on purpose is LOU_DROP_DOMAIN=1.
# Fail CLOSED: only "the stack does not exist yet" (the first deploy) means
# "no hostname". Any other failure (throttle, expired session, permission)
# stops the run — treating it as "no domain" would be the silent detach again.
# stderr goes to a temp file, never into the value: the CLI writes warnings
# to stderr on SUCCESSFUL calls too (urllib3/LibreSSL, deprecation notices),
# and a merged stream would hand CDK a hostname with a warning glued on.
stack_out() {
  local out err; err=$(mktemp)
  if out=$(aws cloudformation describe-stacks --stack-name LouStack \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>"$err"); then
    rm -f "$err"; printf '%s' "$out"
  elif grep -q 'does not exist' "$err"; then
    rm -f "$err"; printf ''
  else
    echo "!! cannot read LouStack outputs ($1): $(cat "$err")" >&2; rm -f "$err"
    return 1
  fi
}
if [ -z "${LOU_DOMAIN:-}" ] && [ "${LOU_DROP_DOMAIN:-}" != "1" ]; then
  live_domain=$(stack_out PublicDomain) || exit 1
  live_cert=$(stack_out CertificateArn) || exit 1
  if [ -n "$live_domain" ] && [ "$live_domain" != "None" ]; then
    [ -n "$live_cert" ] && [ "$live_cert" != "None" ] || { echo "!! stack has PublicDomain=$live_domain but no CertificateArn output; refusing"; exit 1; }
    LOU_DOMAIN=$live_domain; LOU_CERT_ARN=$live_cert
    echo "== keeping the live public hostname $LOU_DOMAIN (set LOU_DROP_DOMAIN=1 to detach it deliberately)"
  fi
fi
# Alarm e-mail (SNS subscription; louisville-open-data-5cn). Passed as CDK
# context so the address never lands in the public repo — and, like the
# hostname, ADOPTED from the live stack (its AlertEmail output) when this run
# was not given one. Without that, an ordinary deploy from a shell without
# LOU_ALERT_EMAIL synthesized a topic with no subscriber and destroyed the
# live one (2026-09-24); the monthly refresh then kept redeploying it empty.
# Removing it on purpose is LOU_DROP_ALERTS=1. Same fail-closed read as above.
if [ -z "${LOU_ALERT_EMAIL:-}" ] && [ "${LOU_DROP_ALERTS:-}" != "1" ]; then
  live_email=$(stack_out AlertEmail) || exit 1
  if [ -n "$live_email" ] && [ "$live_email" != "None" ] && [ "$live_email" != "none" ]; then
    LOU_ALERT_EMAIL=$live_email
    echo "== keeping the live alarm e-mail subscription (set LOU_DROP_ALERTS=1 to remove it deliberately)"
  else
    echo "!! no alarm e-mail: the alarms will have NO subscriber. Set LOU_ALERT_EMAIL=<address> to add one."
  fi
fi
[ -n "${LOU_ALERT_EMAIL:-}" ] && [ "${LOU_DROP_ALERTS:-}" != "1" ] && CTX+=(-c "lou:alertEmail=$LOU_ALERT_EMAIL")
[ -n "${LOU_DOMAIN:-}" ] && CTX+=(-c "lou:domain=$LOU_DOMAIN")
[ -n "${LOU_CERT_ARN:-}" ] && CTX+=(-c "lou:certificateArn=$LOU_CERT_ARN")
# ${CTX[@]+"${CTX[@]}"} expands to nothing when the array is empty: a plain
# "${CTX[@]}" is an unbound-variable error under `set -u` on bash < 4.4
# (macOS /bin/bash is 3.2).
cdk() { npx --yes aws-cdk@2 ${CTX[@]+"${CTX[@]}"} "$@"; }

case "$STEP" in
  synth) cdk synth --quiet && echo "template: cdk.out/LouStack.template.json" ;;
  diff)  cdk diff ;;
  deploy)
    # The publish process (CLAUDE.md): production is deployed only after THIS
    # commit was previewed locally in the production image and approved —
    # `./infra/preview.sh` then `./infra/preview.sh ok` writes the marker.
    # A different commit, a dirty tree, or no marker refuses. Bypass, for an
    # emergency only, with LOU_SKIP_PREVIEW=1 (it is logged in the output).
    if [ "${LOU_SKIP_PREVIEW:-}" = "1" ]; then
      echo "!! LOU_SKIP_PREVIEW=1: deploying WITHOUT a local preview of $(git -C ../.. rev-parse --short HEAD)"
    else
      head=$(git -C ../.. rev-parse HEAD)
      mark=$(cat ../../.preview-ok 2>/dev/null || true)
      if [ "$mark" != "$head" ]; then
        echo "!! commit $(git -C ../.. rev-parse --short HEAD) has not been previewed (marker: ${mark:0:7}${mark:+…}${mark:-none})."
        echo "!! run: ./infra/preview.sh   (build + run the production image locally, verify, look at it)"
        echo "!!      ./infra/preview.sh ok   then re-run this deploy.  Emergency bypass: LOU_SKIP_PREVIEW=1"
        exit 1
      fi
      git -C ../.. diff --quiet && git -C ../.. diff --cached --quiet \
        || { echo "!! working tree differs from the previewed commit; commit (and preview) first"; exit 1; }
      echo "== previewed commit ${head:0:7} approved locally"
    fi
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
    cdk deploy --require-approval never --outputs-file "$OUT"
    CF=$(python3 -c "import json; print(json.load(open('$OUT'))['LouStack']['CloudFrontUrl'])")
    FN=$(python3 -c "import json; print(json.load(open('$OUT'))['LouStack']['FunctionName'])")
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
    # The public hostname, when bound: pinned to CloudFront with --resolve so
    # this holds before AND after the DNS cutover (before it, public DNS still
    # points at the Air). A stripped alias or wrong certificate fails HERE.
    if [ -n "${LOU_DOMAIN:-}" ]; then
      CFD=$(python3 -c "import json; print(json.load(open('$OUT'))['LouStack']['CloudFrontDomain'])")
      IP=$(dig +short "$CFD" A | head -1)
      [ -n "$IP" ] || { echo "!! cannot resolve $CFD"; exit 1; }
      hn=$(curl -sS --resolve "$LOU_DOMAIN:443:$IP" -o /dev/null --max-time 30 -w '%{http_code}' "https://$LOU_DOMAIN/api/health" || echo "curl-failed")
      [ "$hn" = "200" ] && echo "== HOSTNAME VERIFIED on CloudFront: https://$LOU_DOMAIN/" \
        || { echo "!! https://$LOU_DOMAIN/ on CloudFront answered $hn — alias or certificate problem"; exit 1; }
    fi
    # A prompt change orphans every cached answer (CACHE_VERSION); re-warm so
    # the first visitors after a deploy do not pay the LLM latency. Spends
    # LLM calls (the only real cost of a deploy) — opt in.
    if [ "${LOU_WARM:-}" = "1" ]; then
      # warm_cache.py reads /api/cache first, which is admin-gated: without the
      # production ADMIN_TOKEN it cannot even see what is cached. Require it.
      [ -n "${ADMIN_TOKEN:-}" ] || { echo "!! LOU_WARM=1 needs ADMIN_TOKEN in the environment (the /lou/prod value)"; exit 1; }
      echo "== re-warming the starter answers through CloudFront"
      ( cd ../.. && ADMIN_TOKEN="$ADMIN_TOKEN" python3 warm_cache.py --host "${CF%/}" --delay 5 )
    fi
    rm -f ../../.preview-ok
    ;;
  *) echo "usage: $0 [synth|diff|deploy]"; exit 2 ;;
esac
