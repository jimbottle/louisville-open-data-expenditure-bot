#!/usr/bin/env bash
# Measure the REAL cold start of the Lou Lambda image (louisville-open-data-n8w
# acceptance: "cold start under 5s measured on real Lambda"). Throwaway, like
# spike/sse-lwa: pushes the image built from Dockerfile.lambda to a lou-*
# ECR repo, creates a function, forces fresh execution environments at several
# memory sizes, and reads Init Duration from the REPORT log lines.
#
# Runs as --profile lou. Cost: a handful of invocations at up to 3 GB for a
# few seconds each — fractions of a cent; the ECR image (~185 MB) is inside the
# 500 MB free tier. Tear down with: ./coldstart.sh teardown
#
#   ./coldstart.sh 1      ECR repo + build + push      (agent can run)
#   ./coldstart.sh 2      exec role under /lou/        (classifier blocks the agent — human runs)
#   ./coldstart.sh 3      create function + measure    (agent can run)
#   ./coldstart.sh teardown
set -euo pipefail
export AWS_PAGER=""
P=(--profile lou --region us-east-1)
ACCT=012146975534
REGION=us-east-1
NAME=lou-bot-coldstart
ROLE_NAME=$NAME-exec
ROLE_ARN="arn:aws:iam::$ACCT:role/lou/$ROLE_NAME"
REPO="$ACCT.dkr.ecr.$REGION.amazonaws.com/$NAME"
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TAG=${TAG:-$(git -C "$REPO_ROOT" rev-parse --short HEAD)}
MEMORIES=${MEMORIES:-"1024 1769 3008"}
STEP=${1:-all}
run() { [ "$STEP" = all ] || [ "$STEP" = "$1" ]; }

echo "== caller: $(aws sts get-caller-identity "${P[@]}" --query Arn --output text)"

if [ "$STEP" = teardown ]; then
  aws lambda delete-function "${P[@]}" --function-name "$NAME" 2>/dev/null && echo "function deleted"
  aws iam delete-role-policy "${P[@]}" --role-name "$ROLE_NAME" --policy-name logs 2>/dev/null || true
  aws iam delete-role "${P[@]}" --role-name "$ROLE_NAME" 2>/dev/null && echo "role deleted"
  aws ecr delete-repository "${P[@]}" --repository-name "$NAME" --force >/dev/null 2>&1 && echo "repo deleted"
  aws logs delete-log-group "${P[@]}" --log-group-name "/aws/lambda/$NAME" 2>/dev/null && echo "log group deleted"
  exit 0
fi

if run 1; then
  echo "== 1. ECR repo $NAME + build/push $REPO:$TAG"
  aws ecr describe-repositories "${P[@]}" --repository-names "$NAME" >/dev/null 2>&1 || \
    aws ecr create-repository "${P[@]}" --repository-name "$NAME" --tags Key=Project,Value=lou Key=ManagedBy,Value=spike \
      --query repository.repositoryUri --output text
  aws ecr get-login-password "${P[@]}" | docker login --username AWS --password-stdin "$ACCT.dkr.ecr.$REGION.amazonaws.com"
  docker buildx build --platform linux/arm64 --provenance=false -f "$REPO_ROOT/Dockerfile.lambda" -t "$REPO:$TAG" --push "$REPO_ROOT"
  aws ecr describe-images "${P[@]}" --repository-name "$NAME" --image-ids imageTag="$TAG" \
    --query 'imageDetails[0].[imageDigest,imageSizeInBytes]' --output text
fi

if run 2; then
  echo "== 2. exec role $ROLE_ARN"
  if ! aws iam get-role "${P[@]}" --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    aws iam create-role "${P[@]}" --role-name "$ROLE_NAME" --path /lou/ \
      --permissions-boundary "arn:aws:iam::$ACCT:policy/LouPermissionsBoundary" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      --tags Key=Project,Value=lou --query Role.Arn --output text
    aws iam put-role-policy "${P[@]}" --role-name "$ROLE_NAME" --policy-name logs \
      --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$REGION:$ACCT:log-group:/aws/lambda/$NAME*\"}]}"
    echo "   waiting 10s for IAM propagation"; sleep 10
  fi
fi

if run 3; then
  DIGEST=$(aws ecr describe-images "${P[@]}" --repository-name "$NAME" --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
  IMAGE="$REPO@$DIGEST"
  echo "== 3. function $NAME from $IMAGE"
  if ! aws lambda get-function "${P[@]}" --function-name "$NAME" >/dev/null 2>&1; then
    aws lambda create-function "${P[@]}" --function-name "$NAME" \
      --package-type Image --code ImageUri="$IMAGE" --role "$ROLE_ARN" \
      --architectures arm64 --memory-size 1024 --timeout 120 --ephemeral-storage Size=1024 \
      --environment "Variables={CEREBRAS_PAID_API_KEY=placeholder,OPENROUTER_API_KEY=placeholder,TRUSTED_PROXY_IPS=127.0.0.1}" \
      --tags Project=lou,ManagedBy=spike --query FunctionArn --output text
  else
    aws lambda update-function-code "${P[@]}" --function-name "$NAME" --image-uri "$IMAGE" --query CodeSha256 --output text
  fi
  # A container-image code update stays InProgress while Lambda optimises the
  # image (~30-60s); the active waiter returns before that, and the next
  # configuration update then fails with ResourceConflictException.
  aws lambda wait function-active-v2 "${P[@]}" --function-name "$NAME"
  aws lambda wait function-updated-v2 "${P[@]}" --function-name "$NAME"

  EVENT='{"version":"2.0","routeKey":"$default","rawPath":"/api/health","rawQueryString":"","headers":{"host":"x","accept":"*/*"},"requestContext":{"http":{"method":"GET","path":"/api/health","protocol":"HTTP/1.1","sourceIp":"127.0.0.1","userAgent":"coldstart"},"requestId":"x","stage":"$default"},"isBase64Encoded":false}'
  for MEM in $MEMORIES; do
    # Any configuration change retires the warm environments, so the next
    # invoke is a guaranteed cold start at this memory size.
    aws lambda update-function-configuration "${P[@]}" --function-name "$NAME" --memory-size "$MEM" \
      --environment "Variables={CEREBRAS_PAID_API_KEY=placeholder,OPENROUTER_API_KEY=placeholder,TRUSTED_PROXY_IPS=127.0.0.1,COLDSTART_NONCE=$(date +%s)}" >/dev/null
    aws lambda wait function-updated-v2 "${P[@]}" --function-name "$NAME"
    for i in 1 2 3; do
      [ $i -gt 1 ] && aws lambda update-function-configuration "${P[@]}" --function-name "$NAME" \
        --environment "Variables={CEREBRAS_PAID_API_KEY=placeholder,OPENROUTER_API_KEY=placeholder,TRUSTED_PROXY_IPS=127.0.0.1,COLDSTART_NONCE=$(date +%s)$i}" >/dev/null \
        && aws lambda wait function-updated-v2 "${P[@]}" --function-name "$NAME"
      aws lambda invoke "${P[@]}" --function-name "$NAME" --cli-binary-format raw-in-base64-out \
        --payload "$EVENT" --log-type Tail --query 'LogResult' --output text /tmp/lou-coldstart-out.json \
        | base64 -d | grep -o 'Init Duration: [0-9.]* ms\|Duration: [0-9.]* ms\|Max Memory Used: [0-9]* MB' | paste -sd' ' - \
        | sed "s/^/   mem=${MEM}MB run=$i  /"
      grep -q '"statusCode": *200\|"statusCode":200' /tmp/lou-coldstart-out.json || { echo "   !! non-200 body:"; head -c 300 /tmp/lou-coldstart-out.json; echo; }
    done
  done
  rm -f /tmp/lou-coldstart-out.json
fi
