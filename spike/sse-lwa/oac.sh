#!/usr/bin/env bash
# Step 5 of the spike: switch the CloudFront origin to Origin Access Control
# (SigV4-signed origin requests) and lock the Function URL to AuthType AWS_IAM.
# This is the production shape — the Function URL stops being publicly
# callable — and it is measured separately because SigV4 changes how POST
# bodies are handled (the viewer must send x-amz-content-sha256).
set -euo pipefail
export AWS_PAGER=""
P=(--profile lou --region us-east-1)
ACCT=012146975534
NAME=lou-sse-spike
OUT=$(dirname "$0")/.deploy-state.json
cd "$(dirname "$0")"
getstate() { python3 -c "import json,sys; print(json.load(open('$OUT')).get('$1',''))"; }
state() { python3 - "$OUT" "$1" "$2" <<'PY'
import json, sys
p, k, v = sys.argv[1:]
d = json.load(open(p)); d[k] = v; json.dump(d, open(p, "w"), indent=2)
PY
}
DIST_ID=$(getstate distribution_id); [ -n "$DIST_ID" ] || { echo "deploy.sh step 4 first"; exit 1; }

echo "== 1. OAC (type lambda, sign always)"
OAC_ID=$(getstate oac_id)
if [ -z "$OAC_ID" ]; then
  OAC_ID=$(aws cloudfront create-origin-access-control "${P[@]}" --origin-access-control-config \
    "{\"Name\":\"$NAME-oac\",\"Description\":\"lou-sse-spike\",\"SigningProtocol\":\"sigv4\",\"SigningBehavior\":\"always\",\"OriginAccessControlOriginType\":\"lambda\"}" \
    --query OriginAccessControl.Id --output text)
  state oac_id "$OAC_ID"
fi
echo "   $OAC_ID"

echo "== 2. Function URL -> AuthType AWS_IAM; allow only this distribution to invoke it"
aws lambda update-function-url-config "${P[@]}" --function-name "$NAME" --auth-type AWS_IAM --invoke-mode RESPONSE_STREAM --query AuthType --output text
aws lambda remove-permission "${P[@]}" --function-name "$NAME" --statement-id public-url 2>/dev/null || true
aws lambda add-permission "${P[@]}" --function-name "$NAME" --statement-id cloudfront-oac \
  --action lambda:InvokeFunctionUrl --principal cloudfront.amazonaws.com \
  --source-arn "arn:aws:cloudfront::$ACCT:distribution/$DIST_ID" >/dev/null 2>&1 || echo "   (permission already present)"

echo "== 3. attach the OAC to the origin"
ETAG=$(aws cloudfront get-distribution-config "${P[@]}" --id "$DIST_ID" --query ETag --output text)
aws cloudfront get-distribution-config "${P[@]}" --id "$DIST_ID" --query DistributionConfig \
  | python3 -c "import json,sys; d=json.load(sys.stdin); d['Origins']['Items'][0]['OriginAccessControlId']='$OAC_ID'; print(json.dumps(d))" > /tmp/lou-cf-oac.json
aws cloudfront update-distribution "${P[@]}" --id "$DIST_ID" --if-match "$ETAG" --distribution-config file:///tmp/lou-cf-oac.json --query Distribution.Status --output text
rm -f /tmp/lou-cf-oac.json
echo "   propagating (~3-5 min)"; aws cloudfront wait distribution-deployed "${P[@]}" --id "$DIST_ID"
state oac_enabled true
echo "== done. The Function URL should now return 403 directly; CloudFront should still stream."
