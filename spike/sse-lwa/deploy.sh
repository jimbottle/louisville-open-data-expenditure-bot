#!/usr/bin/env bash
# Deploy the SSE spike (louisville-open-data-4l4). Throwaway; tear down with teardown.sh.
#
# Runs as --profile lou (the scoped lou-deploy role). Every resource is lou-* /
# under the /lou/ path and tagged Project=lou, which is the only namespace the
# role can touch. Idempotent: each step skips what already exists.
#
# What it creates and what it costs:
#   ECR repo lou-sse-spike            ~150 MB image; inside the 500 MB free tier
#   IAM role /lou/lou-sse-spike-exec  free (boundary LouPermissionsBoundary forced)
#   Lambda lou-sse-spike (arm64, 512MB, 120s) + Function URL, InvokeMode RESPONSE_STREAM
#                                     a few dozen invocations; inside the always-free tier
#   CloudFront distribution           inside the always-free tier (1 TB/mo)
#   Total for the spike: ~$0. Teardown leaves nothing but CloudWatch logs (cents).
#
# Steps are numbered so they can be re-run individually:  ./deploy.sh [step]
set -euo pipefail
export AWS_PAGER=""
P=(--profile lou --region us-east-1)
ACCT=012146975534
REGION=us-east-1
NAME=lou-sse-spike
ROLE_NAME=$NAME-exec
ROLE_ARN="arn:aws:iam::$ACCT:role/lou/$ROLE_NAME"
REPO="$ACCT.dkr.ecr.$REGION.amazonaws.com/$NAME"
TAG=${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)}
OUT=$(dirname "$0")/.deploy-state.json   # gitignored; records the ids for teardown + measurement
cd "$(dirname "$0")"

state() { python3 - "$OUT" "$1" "$2" <<'PY'
import json, sys, os
p, k, v = sys.argv[1:]
d = json.load(open(p)) if os.path.exists(p) else {}
d[k] = v
json.dump(d, open(p, "w"), indent=2)
PY
}
getstate() { python3 -c "import json,sys; print(json.load(open('$OUT')).get('$1',''))" 2>/dev/null || true; }

STEP=${1:-all}
run() { [ "$STEP" = all ] || [ "$STEP" = "$1" ]; }

echo "== caller: $(aws sts get-caller-identity "${P[@]}" --query Arn --output text)"

if run 1; then
  echo "== 1. ECR repo $NAME"
  if ! aws ecr describe-repositories "${P[@]}" --repository-names "$NAME" >/dev/null 2>&1; then
    aws ecr create-repository "${P[@]}" --repository-name "$NAME" \
      --image-scanning-configuration scanOnPush=false \
      --tags Key=Project,Value=lou Key=ManagedBy,Value=spike --query repository.repositoryUri --output text
  fi
  echo "== 1b. build + push $REPO:$TAG (arm64, Lambda's cheaper arch; native on Apple silicon)"
  aws ecr get-login-password "${P[@]}" | docker login --username AWS --password-stdin "$ACCT.dkr.ecr.$REGION.amazonaws.com"
  docker buildx build --platform linux/arm64 --provenance=false -t "$REPO:$TAG" --push .
  DIGEST=$(aws ecr describe-images "${P[@]}" --repository-name "$NAME" --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
  state image "$REPO@$DIGEST"; state tag "$TAG"
  echo "   pushed $REPO@$DIGEST"
fi

if run 2; then
  echo "== 2. exec role $ROLE_ARN (boundary is mandatory — the guardrails deny CreateRole without it)"
  if ! aws iam get-role "${P[@]}" --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    aws iam create-role "${P[@]}" --role-name "$ROLE_NAME" --path /lou/ \
      --permissions-boundary "arn:aws:iam::$ACCT:policy/LouPermissionsBoundary" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      --tags Key=Project,Value=lou --query Role.Arn --output text
    # Only logs. The boundary already caps this; the inline policy is what the
    # function actually gets.
    aws iam put-role-policy "${P[@]}" --role-name "$ROLE_NAME" --policy-name logs \
      --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$REGION:$ACCT:log-group:/aws/lambda/$NAME*\"}]}"
    echo "   waiting 10s for IAM propagation"; sleep 10
  fi
  state role "$ROLE_ARN"
fi

if run 3; then
  IMAGE=$(getstate image); [ -n "$IMAGE" ] || { echo "run step 1 first"; exit 1; }
  echo "== 3. Lambda $NAME from $IMAGE"
  if aws lambda get-function "${P[@]}" --function-name "$NAME" >/dev/null 2>&1; then
    aws lambda update-function-code "${P[@]}" --function-name "$NAME" --image-uri "$IMAGE" --query 'CodeSha256' --output text
  else
    aws lambda create-function "${P[@]}" --function-name "$NAME" \
      --package-type Image --code ImageUri="$IMAGE" --role "$ROLE_ARN" \
      --architectures arm64 --memory-size 512 --timeout 120 \
      --tags Project=lou,ManagedBy=spike --query FunctionArn --output text
  fi
  aws lambda wait function-updated "${P[@]}" --function-name "$NAME"
  aws lambda wait function-active "${P[@]}" --function-name "$NAME"

  echo "== 3b. Function URL, InvokeMode RESPONSE_STREAM, AuthType NONE (direct hop; OAC variant is step 5)"
  if ! aws lambda get-function-url-config "${P[@]}" --function-name "$NAME" >/dev/null 2>&1; then
    aws lambda create-function-url-config "${P[@]}" --function-name "$NAME" \
      --auth-type NONE --invoke-mode RESPONSE_STREAM --query FunctionUrl --output text
    aws lambda add-permission "${P[@]}" --function-name "$NAME" \
      --statement-id public-url --action lambda:InvokeFunctionUrl \
      --principal '*' --function-url-auth-type NONE >/dev/null
  fi
  FURL=$(aws lambda get-function-url-config "${P[@]}" --function-name "$NAME" --query FunctionUrl --output text)
  state function_url "$FURL"
  echo "   $FURL"
fi

if run 4; then
  FURL=$(getstate function_url); [ -n "$FURL" ] || { echo "run step 3 first"; exit 1; }
  HOST=${FURL#https://}; HOST=${HOST%/}
  echo "== 4. CloudFront distribution in front of $HOST"
  DIST_ID=$(getstate distribution_id)
  if [ -z "$DIST_ID" ]; then
    # Managed policies: CachingDisabled + AllViewerExceptHostHeader (Function URLs
    # reject a forwarded Host header). No OAC on this first pass so the test
    # isolates CloudFront's streaming behaviour from SigV4 signing.
    CFG=$(python3 - "$HOST" <<'PY'
import json, sys, time
host = sys.argv[1]
print(json.dumps({
  "CallerReference": f"lou-sse-spike-{int(time.time())}",
  "Comment": "lou-sse-spike (throwaway, louisville-open-data-4l4)",
  "Enabled": True,
  "HttpVersion": "http2and3",
  "PriceClass": "PriceClass_100",
  "Origins": {"Quantity": 1, "Items": [{
    "Id": "lambda-url", "DomainName": host,
    "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                           "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                           "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5},
  }]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "lambda-url", "ViewerProtocolPolicy": "https-only",
    "AllowedMethods": {"Quantity": 7, "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
                       "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}},
    "Compress": False,
    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",          # Managed-CachingDisabled
    "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",  # Managed-AllViewerExceptHostHeader
  },
}))
PY
)
    DIST_ID=$(aws cloudfront create-distribution-with-tags "${P[@]}" \
      --distribution-config-with-tags "{\"DistributionConfig\": $CFG, \"Tags\": {\"Items\": [{\"Key\":\"Project\",\"Value\":\"lou\"},{\"Key\":\"ManagedBy\",\"Value\":\"spike\"}]}}" \
      --query Distribution.Id --output text)
    state distribution_id "$DIST_ID"
  fi
  CF_DOMAIN=$(aws cloudfront get-distribution "${P[@]}" --id "$DIST_ID" --query Distribution.DomainName --output text)
  state cloudfront_url "https://$CF_DOMAIN/"
  echo "   $DIST_ID https://$CF_DOMAIN/  (deploying; takes ~3-5 min)"
  aws cloudfront wait distribution-deployed "${P[@]}" --id "$DIST_ID"
  echo "   deployed"
fi

if run 5; then
  echo "== 5. (optional) switch the origin to OAC + AuthType AWS_IAM — the production shape."
  echo "   Run ./oac.sh after step 4 has been measured; see README."
fi

echo; echo "state: $OUT"; cat "$OUT"
