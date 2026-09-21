#!/usr/bin/env bash
# Remove everything deploy.sh created (louisville-open-data-4l4). Safe to re-run.
# Leaves: the CloudWatch log group /aws/lambda/lou-sse-spike (deleted too, below)
# and nothing else. Every name is lou-* so the scoped role can delete it.
set -uo pipefail
export AWS_PAGER=""
P=(--profile lou --region us-east-1)
ACCT=012146975534
NAME=lou-sse-spike
ROLE_NAME=$NAME-exec
cd "$(dirname "$0")"
OUT=$PWD/.deploy-state.json
getstate() { python3 -c "import json,sys; print(json.load(open('$OUT')).get('$1',''))" 2>/dev/null || true; }

echo "== caller: $(aws sts get-caller-identity "${P[@]}" --query Arn --output text)"

DIST_ID=$(getstate distribution_id)
if [ -n "$DIST_ID" ]; then
  echo "== CloudFront $DIST_ID: disable, wait, delete (the wait is ~5 min; CloudFront requires it)"
  ETAG=$(aws cloudfront get-distribution-config "${P[@]}" --id "$DIST_ID" --query ETag --output text)
  aws cloudfront get-distribution-config "${P[@]}" --id "$DIST_ID" --query DistributionConfig \
    | python3 -c "import json,sys; d=json.load(sys.stdin); d['Enabled']=False; print(json.dumps(d))" > /tmp/lou-cf-disabled.json
  aws cloudfront update-distribution "${P[@]}" --id "$DIST_ID" --if-match "$ETAG" --distribution-config file:///tmp/lou-cf-disabled.json >/dev/null
  aws cloudfront wait distribution-deployed "${P[@]}" --id "$DIST_ID"
  ETAG=$(aws cloudfront get-distribution-config "${P[@]}" --id "$DIST_ID" --query ETag --output text)
  aws cloudfront delete-distribution "${P[@]}" --id "$DIST_ID" --if-match "$ETAG" && echo "   deleted"
  rm -f /tmp/lou-cf-disabled.json
fi
OAC_ID=$(getstate oac_id)
if [ -n "$OAC_ID" ]; then
  ETAG=$(aws cloudfront get-origin-access-control "${P[@]}" --id "$OAC_ID" --query ETag --output text 2>/dev/null)
  [ -n "$ETAG" ] && aws cloudfront delete-origin-access-control "${P[@]}" --id "$OAC_ID" --if-match "$ETAG" && echo "== OAC $OAC_ID deleted"
fi

echo "== Lambda $NAME"
aws lambda delete-function-url-config "${P[@]}" --function-name "$NAME" 2>/dev/null && echo "   url config deleted"
aws lambda delete-function "${P[@]}" --function-name "$NAME" 2>/dev/null && echo "   function deleted"

echo "== role /lou/$ROLE_NAME"
aws iam delete-role-policy "${P[@]}" --role-name "$ROLE_NAME" --policy-name logs 2>/dev/null
aws iam delete-role "${P[@]}" --role-name "$ROLE_NAME" 2>/dev/null && echo "   role deleted"

echo "== ECR repo $NAME (with images)"
aws ecr delete-repository "${P[@]}" --repository-name "$NAME" --force >/dev/null 2>&1 && echo "   repo deleted"

echo "== log group"
aws logs delete-log-group "${P[@]}" --log-group-name "/aws/lambda/$NAME" 2>/dev/null && echo "   log group deleted"

rm -f "$OUT"
echo "== done. Remaining lou-* resources:"
aws lambda list-functions "${P[@]}" --query "Functions[?starts_with(FunctionName,'lou-')].FunctionName" --output text
# (ecr:* is scoped to lou-* repos, so an unnamed DescribeRepositories is denied; name it)
aws ecr describe-repositories "${P[@]}" --repository-names "$NAME" --query 'repositories[].repositoryName' --output text 2>/dev/null || echo "   (repo $NAME gone)"
