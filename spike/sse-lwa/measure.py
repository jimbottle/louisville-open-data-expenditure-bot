#!/usr/bin/env python3
"""Measure how an SSE stream actually arrives at the client.

    python measure.py https://xxx.lambda-url.us-east-1.on.aws/api/stream --label function-url
    python measure.py https://dxxx.cloudfront.net/api/stream --label cloudfront --method POST

Reports time-to-first-byte, per-chunk arrival times, the largest gap between
chunks, and a verdict: INCREMENTAL if chunks arrived spread across the stream's
duration, BATCHED if most of them landed together at the end.

Acceptance for 4l4: INCREMENTAL through CloudFront, TTFB under 2s.
"""
import argparse
import hashlib
import json
import statistics
import sys
import time

import httpx


def run(url: str, method: str, n: int, interval: float, label: str) -> dict:
    t0 = time.perf_counter()
    arrivals: list[float] = []
    events: list[dict] = []
    ttfb = None
    status = None
    headers = {}
    buf = b""
    params = {"n": n, "interval": interval}
    body = b"{}" if method == "POST" else None
    req_headers = {}
    if body is not None:
        # CloudFront OAC signs origin requests with SigV4; for a request WITH a
        # body the viewer must supply the body's SHA-256 or CloudFront answers
        # 403. Harmless on every other hop, so always send it for POST.
        req_headers = {"Content-Type": "application/json",
                       "x-amz-content-sha256": hashlib.sha256(body).hexdigest()}
    with httpx.Client(timeout=httpx.Timeout(10.0, read=n * interval + 30)) as client:
        with client.stream(method, url, params=params, content=body, headers=req_headers) as resp:
            status = resp.status_code
            headers = {k.lower(): v for k, v in resp.headers.items()}
            for chunk in resp.iter_raw():
                now = time.perf_counter() - t0
                if ttfb is None:
                    ttfb = now
                arrivals.append(now)
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    for line in frame.split(b"\n"):
                        if line.startswith(b"data: "):
                            try:
                                events.append({"recv": now, **json.loads(line[6:])})
                            except json.JSONDecodeError:
                                pass
    total = time.perf_counter() - t0
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    ticks = [e for e in events if "i" in e]
    # Server-side event i was emitted at t=i*interval; how late did it reach us
    # relative to when the first one did?
    lag = [e["recv"] - ttfb - e["t"] for e in ticks] if ticks else []
    expected_duration = (n - 1) * interval
    spread = (arrivals[-1] - arrivals[0]) if len(arrivals) > 1 else 0.0
    verdict = "INCREMENTAL" if spread >= 0.5 * expected_duration and len(arrivals) >= 0.5 * n else "BATCHED"
    if status != 200:
        verdict = f"HTTP {status}"
    return {
        "label": label, "url": url, "method": method, "status": status, "n": n, "interval": interval,
        "ttfb_s": round(ttfb, 3) if ttfb is not None else None,
        "total_s": round(total, 3),
        "chunks": len(arrivals), "events": len(ticks),
        "arrival_spread_s": round(spread, 3), "expected_spread_s": expected_duration,
        "max_gap_s": round(max(gaps), 3) if gaps else None,
        "median_gap_s": round(statistics.median(gaps), 3) if gaps else None,
        "max_lag_vs_server_s": round(max(lag), 3) if lag else None,
        "verdict": verdict,
        "resp_headers": {k: headers[k] for k in sorted(headers)
                         if k in ("content-type", "transfer-encoding", "content-length", "cache-control",
                                  "x-cache", "via", "x-amz-cf-pop", "x-spike-method", "x-spike-instance",
                                  "server", "x-amzn-requestid")},
        "arrivals_s": [round(a, 2) for a in arrivals],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--label", default="")
    ap.add_argument("--method", default="GET", choices=["GET", "POST"])
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args()
    r = run(a.url, a.method, a.n, a.interval, a.label or a.url)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"[{r['label']}] {r['method']} -> HTTP {r['status']}  verdict={r['verdict']}")
        print(f"  ttfb={r['ttfb_s']}s  total={r['total_s']}s  chunks={r['chunks']}  events={r['events']}/{r['n']}")
        print(f"  arrival spread={r['arrival_spread_s']}s (expected ~{r['expected_spread_s']}s)  "
              f"max gap={r['max_gap_s']}s  median gap={r['median_gap_s']}s  max lag vs server={r['max_lag_vs_server_s']}s")
        print(f"  headers: {r['resp_headers']}")
        print(f"  arrivals: {r['arrivals_s']}")
    sys.exit(0 if r["verdict"] == "INCREMENTAL" else 1)


if __name__ == "__main__":
    main()
