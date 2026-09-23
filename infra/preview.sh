#!/usr/bin/env bash
# Local preview of the AWS production deployment — the rehearsal step of the
# publish process (CLAUDE.md, "Publishing to production"). Runs the SAME
# image production runs (Dockerfile.lambda, arm64, the baked DuckDB artifact
# and corpus), under the same constraints (non-root uid, read-only filesystem,
# only /tmp writable), against a local DynamoDB with the same state backend
# and table schema, using the real LLM keys from .env. Costs nothing on AWS.
#
#   ./infra/preview.sh up        build the image, start DynamoDB Local + the app on http://127.0.0.1:8010
#   ./infra/preview.sh verify    the same probes deploy.sh runs against CloudFront, plus a real question
#   ./infra/preview.sh ok        record that THIS commit was previewed (deploy.sh checks for it)
#   ./infra/preview.sh down      stop and remove everything
#   ./infra/preview.sh           = up + verify + open the browser, then tell you to run `ok` when satisfied
#
# What differs from production, deliberately:
#   - no Lambda Web Adapter in the path (outside Lambda it exits and uvicorn
#     serves directly — streaming behaviour was proven on the real path by
#     spike/sse-lwa and is not what a code preview is for);
#   - no CloudFront/OAC in front: CLIENT_IP_SOURCE=peer, no body-hash header
#     needed (the frontend sends it anyway; it is ignored here);
#   - secrets from .env instead of SSM (SSM_PARAMETER_PATH unset).
set -euo pipefail
cd "$(dirname "$0")/.."
NET=lou-preview
IMG=lou-lambda:preview
PORT=${LOU_PREVIEW_PORT:-8010}   # not :8000 — that is the dev uvicorn convention (CLAUDE.md)
MARK=.preview-ok
STEP=${1:-all}

require() { command -v "$1" >/dev/null || { echo "!! $1 is required"; exit 1; }; }

up() {
  require docker
  [ -f .env ] || { echo "!! .env with the LLM keys is required (gitignored; see CLAUDE.md)"; exit 1; }
  [ -f data/lou.duckdb ] || { echo "!! data/lou.duckdb missing — run: python data_model.py --materialize data/lou.duckdb"; exit 1; }
  [ -f data/rag_documents.duckdb ] || { echo "!! data/rag_documents.duckdb missing — run: python rag.py ingest"; exit 1; }
  echo "== building $IMG from Dockerfile.lambda (the production image)"
  docker buildx build --platform linux/arm64 --provenance=false -f Dockerfile.lambda -t "$IMG" --load . \
    2>&1 | grep -E "self-check|ERROR|error:" || true
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  down_quiet
  echo "== DynamoDB Local (the state backend production uses)"
  docker run -d --rm --name lou-preview-dynamodb --network "$NET" amazon/dynamodb-local:latest \
    -jar DynamoDBLocal.jar -inMemory -sharedDb >/dev/null
  docker run --rm --network "$NET" -e AWS_ACCESS_KEY_ID=local -e AWS_SECRET_ACCESS_KEY=local -e AWS_DEFAULT_REGION=us-east-1 \
    "$IMG" python -c "import boto3, state_store; c=boto3.client('dynamodb', endpoint_url='http://lou-preview-dynamodb:8000'); state_store.ensure_table(c, 'lou-state'); print('table lou-state ready')"
  echo "== the app, as Lambda runs it: uid 1000, read-only, /tmp only"
  docker run -d --rm --name lou-preview-app --network "$NET" -p "127.0.0.1:$PORT:8000" \
    --user 1000:1000 --read-only --tmpfs /tmp:rw,size=1g -m 1769m \
    --env-file .env \
    -e STATE_BACKEND=dynamodb -e STATE_TABLE=lou-state -e DYNAMODB_ENDPOINT_URL=http://lou-preview-dynamodb:8000 \
    -e AWS_ACCESS_KEY_ID=local -e AWS_SECRET_ACCESS_KEY=local -e AWS_REGION=us-east-1 \
    -e CLIENT_IP_SOURCE=peer -e ADMIN_TOKEN=preview \
    "$IMG" >/dev/null
  for i in $(seq 1 60); do curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -sf --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null || { echo "!! app did not become healthy; logs:"; docker logs lou-preview-app 2>&1 | tail -20; exit 1; }
  echo "== preview up: http://127.0.0.1:$PORT  (commit $(git rev-parse --short HEAD))"
}

verify() {
  B="http://127.0.0.1:$PORT"
  curl -sf --max-time 10 "$B/api/health" >/dev/null || { echo "!! no preview at $B — run: $0 up"; exit 1; }
  curl -sf --max-time 10 "$B/api/health" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['status']=='ok', d; assert d['state_backend']['backend']=='dynamodb' and d['state_backend']['status']=='ok', d['state_backend']; print('health: ok | backend: dynamodb ok | tables:', len(d['tables']))"
  body='{"question":""}'
  ct=$(curl -sS -o /dev/null --max-time 30 -w '%{http_code} %{content_type}' -X POST "$B/api/ask" -H 'Content-Type: application/json' \
    -H "x-amz-content-sha256: $(printf '%s' "$body" | shasum -a 256 | cut -d' ' -f1)" --data "$body")
  echo "SSE probe: $ct"; case "$ct" in "200 text/event-stream"*) ;; *) echo "!! SSE probe failed"; exit 1;; esac
  curl -sf --max-time 10 -o /dev/null "$B/" && curl -sf --max-time 10 -o /dev/null "$B/static/vendor/chart.umd.min.js" && echo "static: index + vendored chart ok"
  q='{"question":"What was total spending in fiscal year 2025?"}'
  out=$(curl -sS --max-time 180 -X POST "$B/api/ask" -H 'Content-Type: application/json' --data "$q")
  n=$(printf '%s' "$out" | grep -c '"type": "interpretation"' || true)
  e=$(printf '%s' "$out" | grep -c '"type": "error"' || true)
  echo "real question (live LLM): $n interpretation frame(s), $e error(s)"
  [ "$n" -gt 0 ] && [ "$e" -eq 0 ] || { echo "!! the real question did not answer cleanly"; printf '%s\n' "$out" | tail -3; exit 1; }
  echo "== VERIFY PASSED"
}

ok() {
  git diff --quiet && git diff --cached --quiet || { echo "!! working tree has uncommitted changes; commit first so the marker names what was previewed"; exit 1; }
  sha=$(git rev-parse HEAD); echo "$sha" > "$MARK"
  echo "== previewed and approved: $sha  ->  ./infra/cdk/deploy.sh"
}

down_quiet() { docker rm -f lou-preview-app lou-preview-dynamodb >/dev/null 2>&1 || true; }
down() { down_quiet; docker network rm "$NET" >/dev/null 2>&1 || true; echo "== preview down"; }

case "$STEP" in
  up) up ;;
  verify) verify ;;
  ok) ok ;;
  down) down ;;
  all) up; verify; command -v open >/dev/null && open "http://127.0.0.1:$PORT" || true
       echo; echo "Preview it in the browser. When satisfied:  ./infra/preview.sh ok  &&  ./infra/cdk/deploy.sh"
       echo "Then:                                       ./infra/preview.sh down" ;;
  *) echo "usage: $0 [up|verify|ok|down]"; exit 2 ;;
esac
