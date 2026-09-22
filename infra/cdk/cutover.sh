#!/usr/bin/env bash
# Cutover of louisville.raylytics.io from the Air (cloudflared tunnel) to
# LouStack (louisville-open-data-lla). Every step is separately runnable and
# idempotent; the DNS change itself is a human action in Cloudflare and is
# never performed here.
#
#   ./infra/cdk/cutover.sh cert       request the ACM certificate; prints the validation CNAME to add in Cloudflare
#   ./infra/cdk/cutover.sh wait       wait until the certificate is ISSUED
#   ./infra/cdk/cutover.sh deploy     deploy the stack with the hostname + certificate attached
#   ./infra/cdk/cutover.sh pretest    hit the hostname ON CloudFront (--resolve) before DNS moves: the rollback-safe rehearsal
#   ./infra/cdk/cutover.sh verify     after DNS moves: the mandatory end-to-end check through the real hostname
#   ./infra/cdk/cutover.sh rollback   print the rollback steps (DNS back + start the container)
#
# Runs as --profile lou. The certificate is free; nothing here adds cost.
set -euo pipefail
export AWS_PAGER=""
DOMAIN=${LOU_DOMAIN:-louisville.raylytics.io}
P=(--profile lou --region us-east-1)
cd "$(dirname "$0")"
STATE=$PWD/.cutover-state.json      # gitignored (cdk.out sibling): certificate ARN
getstate() { python3 -c "import json,sys; print(json.load(open('$STATE')).get('$1',''))" 2>/dev/null || true; }
state() { python3 - "$STATE" "$1" "$2" <<'PY'
import json, sys, os
p, k, v = sys.argv[1:]
d = json.load(open(p)) if os.path.exists(p) else {}
d[k] = v
json.dump(d, open(p, "w"), indent=2)
PY
}
STEP=${1:-}

case "$STEP" in
  cert)
    ARN=$(getstate certificate_arn)
    if [ -z "$ARN" ]; then
      ARN=$(aws acm request-certificate "${P[@]}" --domain-name "$DOMAIN" --validation-method DNS \
        --tags Key=Project,Value=lou --query CertificateArn --output text)
      state certificate_arn "$ARN"
    fi
    echo "certificate: $ARN"
    echo "== add this CNAME in Cloudflare (DNS-only / grey cloud), then run: $0 wait"
    for i in 1 2 3 4 5 6; do
      rec=$(aws acm describe-certificate "${P[@]}" --certificate-arn "$ARN" \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Name,Value]' --output text 2>/dev/null || true)
      [ -n "$rec" ] && [ "$rec" != "None	None" ] && break
      python3 -c "import time; time.sleep(5)"
    done
    printf '   name : %s\n   value: %s\n' $rec
    ;;
  wait)
    ARN=$(getstate certificate_arn); [ -n "$ARN" ] || { echo "run: $0 cert"; exit 1; }
    echo "waiting for $ARN to be ISSUED (DNS validation; usually a few minutes after the record exists)"
    aws acm wait certificate-validated "${P[@]}" --certificate-arn "$ARN"
    aws acm describe-certificate "${P[@]}" --certificate-arn "$ARN" --query 'Certificate.[Status,NotAfter]' --output text
    ;;
  deploy)
    ARN=$(getstate certificate_arn); [ -n "$ARN" ] || { echo "run: $0 cert"; exit 1; }
    [ "$(aws acm describe-certificate "${P[@]}" --certificate-arn "$ARN" --query Certificate.Status --output text)" = ISSUED ] \
      || { echo "certificate is not ISSUED yet — run: $0 wait"; exit 1; }
    LOU_DOMAIN="$DOMAIN" LOU_CERT_ARN="$ARN" ./deploy.sh deploy
    ;;
  pretest)
    # The distribution now answers for $DOMAIN, but public DNS still points at
    # the Air. Pin the hostname to CloudFront for this process only and run
    # the full verification — TLS name, health, SSE probe, a real answer —
    # with nothing changed for real users. If this fails, nothing to roll back.
    CF=$(aws cloudformation describe-stacks "${P[@]}" --stack-name LouStack \
      --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomain'].OutputValue" --output text)
    IP=$(dig +short "$CF" A | head -1); [ -n "$IP" ] || { echo "could not resolve $CF"; exit 1; }
    echo "== $DOMAIN pinned to CloudFront ($CF -> $IP) for this test only"
    R=(--resolve "$DOMAIN:443:$IP")
    curl -sS "${R[@]}" --max-time 30 "https://$DOMAIN/api/health" | python3 -c "import json,sys; d=json.load(sys.stdin); print('health:', d['status'], '| backend:', d['state_backend']['status'], '| tables:', len(d['tables']))"
    body='{"question":""}'
    ct=$(curl -sS "${R[@]}" -o /dev/null --max-time 30 -w '%{http_code} %{content_type}' -X POST "https://$DOMAIN/api/ask" \
      -H 'Content-Type: application/json' -H "x-amz-content-sha256: $(printf '%s' "$body" | shasum -a 256 | cut -d' ' -f1)" --data "$body")
    echo "SSE probe: $ct"
    q='{"question":"What was total spending in fiscal year 2025?"}'
    n=$(curl -sS "${R[@]}" --max-time 120 -X POST "https://$DOMAIN/api/ask" -H 'Content-Type: application/json' \
      -H "x-amz-content-sha256: $(printf '%s' "$q" | shasum -a 256 | cut -d' ' -f1)" --data "$q" | grep -c '"type": "interpretation"')
    echo "real question: $n interpretation frame(s)"
    case "$ct" in "200 text/event-stream"*) [ "$n" -gt 0 ] && echo "== PRETEST PASSED — safe to move DNS: CNAME $DOMAIN -> $CF (DNS-only)";; *) echo "!! pretest failed"; exit 1;; esac
    ;;
  verify)
    echo "== resolving $DOMAIN now: $(dig +short "$DOMAIN" | tr '\n' ' ')"
    curl -sS --max-time 30 "https://$DOMAIN/api/health" | python3 -c "import json,sys; d=json.load(sys.stdin); print('health:', d['status'], '| backend:', d['state_backend'])"
    body='{"question":""}'
    ct=$(curl -sS -o /dev/null --max-time 30 -w '%{http_code} %{content_type}' -X POST "https://$DOMAIN/api/ask" \
      -H 'Content-Type: application/json' -H "x-amz-content-sha256: $(printf '%s' "$body" | shasum -a 256 | cut -d' ' -f1)" --data "$body")
    echo "SSE probe: $ct"
    curl -sSI --max-time 30 "https://$DOMAIN/" | grep -i '^via:\|^x-cache:\|^server:' || true
    case "$ct" in "200 text/event-stream"*) echo "== CUTOVER VERIFIED through $DOMAIN";; *) echo "!! verification failed — roll back (see: $0 rollback)"; exit 1;; esac
    ;;
  rollback)
    cat <<MSG
ROLLBACK (DNS is the switch; nothing in AWS needs to change):
  1. Cloudflare: point $DOMAIN back at the cloudflared tunnel CNAME (proxied, as before).
  2. On the Air (mac-shell): docker start louisville-bot   (kept stopped-but-present after cutover)
  3. Verify: curl -sf https://$DOMAIN/api/health ; the SSE probe as in 'verify'.
  4. Heartbeat LaunchAgent: restore CONTAINER=louisville-bot if it was emptied.
The louisville-data / louisville-state / louisville-logs volumes are untouched until decommission.
MSG
    ;;
  *) echo "usage: $0 cert|wait|deploy|pretest|verify|rollback"; exit 2 ;;
esac
