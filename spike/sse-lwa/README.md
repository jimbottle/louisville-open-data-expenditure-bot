# SSE streaming spike (louisville-open-data-4l4)

Throwaway. Answers one question before any migration code is written: **does a
FastAPI `StreamingResponse` over a sync generator still arrive token-by-token
after Lambda Web Adapter, a streaming Function URL, and CloudFront?** Mangum
buffers; LWA in `response_stream` mode is the only Python route; CloudFront's
behaviour for chunked origin responses is the unverified risk.

Nothing here is the real app. `app.py` is a 40-line FastAPI that emits one SSE
event per second for 30 s, with the same headers the real `/api/ask` sends.

## Files

| file | purpose |
|---|---|
| `app.py` | the hello-world stream, GET and POST (`/api/stream?n=30&interval=1`) |
| `Dockerfile` | `python:3.12-slim` + LWA 1.0.0 as an extension, `AWS_LWA_INVOKE_MODE=response_stream` |
| `measure.py` | client that records TTFB and per-chunk arrival, prints a verdict (`INCREMENTAL` / `BATCHED`) |
| `deploy.sh` | ECR repo, exec role, Lambda + Function URL (`RESPONSE_STREAM`), CloudFront. Idempotent; `--profile lou` |
| `oac.sh` | step 5: switch the origin to OAC + `AuthType AWS_IAM` (production shape) |
| `teardown.sh` | delete all of it |
| `.deploy-state.json` | ids written by deploy.sh for the other scripts (gitignored) |

## Run

```bash
# local: proves the image serves; LWA exits outside Lambda so this does NOT test streaming mode
docker buildx build --platform linux/arm64 -t lou-sse-spike:local --load .
docker run --rm -p 8766:8000 lou-sse-spike:local
python measure.py http://127.0.0.1:8766/api/stream --label docker-local -n 5 --interval 0.5

# deploy (Tier 2 — creates billable-in-principle resources; ~$0 in practice)
./deploy.sh                     # steps 1-4
python measure.py "$(jq -r .function_url .deploy-state.json)api/stream" --label function-url
python measure.py "$(jq -r .function_url .deploy-state.json)api/stream" --label function-url-post --method POST
python measure.py "$(jq -r .cloudfront_url .deploy-state.json)api/stream" --label cloudfront
python measure.py "$(jq -r .cloudfront_url .deploy-state.json)api/stream" --label cloudfront-post --method POST
./oac.sh                        # step 5, then re-measure the two cloudfront lines
./teardown.sh
```

Run each hop 2-3 times; the first invocation includes a cold start, which is a
separate number from streaming behaviour.

## Acceptance (from the issue)

`INCREMENTAL` through CloudFront, TTFB under 2 s (warm). Result is recorded on
the bd issue either way. If chunks arrive batched at the Function URL, LWA is
misconfigured. If they arrive batched only at CloudFront, try origin/cache
policy changes; if still batched, the target flips to Cloud Run and the epic is
re-planned.

## Results

See the bd issue louisville-open-data-4l4 (and `results.md` here once run).
