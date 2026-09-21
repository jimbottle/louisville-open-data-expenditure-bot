# Results — 2026-09-21

**Verdict: PASS. SSE survives Lambda Web Adapter + streaming Function URL +
CloudFront, with and without OAC, on GET and POST. The migration target stays
Lambda; no Cloud Run re-plan.**

Setup: image `lou-sse-spike@sha256:ebc5caf6…` (python:3.12-slim + LWA 1.0.0,
`AWS_LWA_INVOKE_MODE=response_stream`), Lambda arm64 / 512 MB / 120 s, Function
URL `InvokeMode=RESPONSE_STREAM`, CloudFront with `Managed-CachingDisabled` +
`Managed-AllViewerExceptHostHeader`, `Compress=false`, `OriginReadTimeout=60`.
Client in Atlanta (`x-amz-cf-pop: ATL59-P14`), 30 events at 1/s unless noted.
`lag` = worst-case delay between the server emitting an event and the client
receiving it, relative to the first event.

| hop | method | HTTP | verdict | TTFB | total | chunks | max gap | lag |
|---|---|---|---|---|---|---|---|---|
| Function URL (first call on a warm instance) | GET | 200 | INCREMENTAL | 0.208 s | 30.23 s | 31/31 | 1.018 s | 18 ms |
| Function URL | GET | 200 | INCREMENTAL | 0.194 s | 30.24 s | 31/31 | 1.025 s | 17 ms |
| Function URL | POST | 200 | INCREMENTAL | 0.203 s | 30.22 s | 31/31 | 1.011 s | 2 ms |
| CloudFront (no OAC) | GET | 200 | INCREMENTAL | 0.195 s | 30.22 s | 31/31 | 1.017 s | 16 ms |
| CloudFront (no OAC) | POST | 200 | INCREMENTAL | 0.214 s | 30.24 s | 31/31 | 1.012 s | 11 ms |
| CloudFront (no OAC), repeat | GET | 200 | INCREMENTAL | 0.193 s | 30.22 s | 31/31 | 1.019 s | 19 ms |
| CloudFront + OAC, URL `AWS_IAM` | GET | 200 | INCREMENTAL | 0.225 s | 30.26 s | 31/31 | 1.022 s | 20 ms |
| CloudFront + OAC, URL `AWS_IAM` | POST | 200 | INCREMENTAL | 0.217 s | 30.24 s | 31/31 | 1.034 s | 32 ms |
| CloudFront + OAC, **70 events / 70 s** | POST | 200 | INCREMENTAL | 0.248 s | 70.29 s | 71/71 | 1.023 s | — |

Every chunk arrived on its own; nothing was coalesced at any hop. CloudFront
adds no measurable latency to the stream (its TTFB equals the direct URL's).
A 70 s stream passed through unbroken even though `OriginReadTimeout` is 60 s —
that timeout is idle time between bytes, not total duration.

Acceptance from the issue: incremental through CloudFront ✔, TTFB < 2 s ✔.

## Cold start (hello-world image, not representative of the real one)

From the CloudWatch REPORT line of the first invocation:
`Init Duration: 4457.75 ms`, `Max Memory Used: 73-76 MB`. That is LWA waiting
for uvicorn's readiness check on a 512 MB (CPU-throttled) function with a
53 MB image. The real image (~600 MB with the 112 MB DuckDB artifact, `PREBUILT_DB`)
will be measured in louisville-open-data-n8w; expect several seconds more and
plan the memory size around CPU, not RSS.

## Findings that change the migration

1. **Function URLs need two resource-policy grants since Oct 2025.**
   `lambda:InvokeFunctionUrl` (with `FunctionUrlAuthType`) *and*
   `lambda:InvokeFunction` (with `InvokedViaFunctionUrl=true`). With only the
   first, every request is `403 AccessDeniedException` and the error body says
   nothing useful. Cost this spike ~15 min. `deploy.sh` and `oac.sh` carry both;
   the IaC in louisville-open-data-i0q must too (CDK's `addFunctionUrl` may or
   may not — verify the synthesized policy).

2. **OAC + POST requires the viewer to send `x-amz-content-sha256`.**
   CloudFront signs the origin request with SigV4 and includes the body hash it
   is *given*. A POST with a JSON body and no header gets
   `403 "The request signature we calculated does not match"`. A body-less POST
   and all GETs are fine. The real frontend POSTs `{"question": …}` to
   `/api/ask`, so `static/index.html` must compute
   `crypto.subtle.digest("SHA-256", body)` and send it as that header, or the
   whole product 403s behind OAC. Filed as a task under the epic.
   (The alternative — no OAC, public Function URL — leaves the origin
   bypassable, which defeats the CloudFront rate limit/WAF in
   louisville-open-data-69m.)

3. **Function URLs reject a forwarded `Host` header**, so the origin request
   policy must be `AllViewerExceptHostHeader` (or a custom one that drops
   Host). `AllViewer` would 403 at the origin.

4. The response headers the real app already sends
   (`Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`) are
   sufficient; no CloudFront cache/origin policy customisation beyond the two
   managed policies was needed, and `Compress` must stay off for the SSE
   behaviour (compression would buffer).

Raw per-run JSON (arrival timestamps included) is in `results-*.json`
alongside this file.
