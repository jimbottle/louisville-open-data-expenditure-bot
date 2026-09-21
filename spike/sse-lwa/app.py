"""Throwaway SSE spike for louisville-open-data-4l4.

Mirrors the shape of the real /api/ask endpoint — a FastAPI StreamingResponse
over a SYNC generator, media type text/event-stream, one event per tick — so
that whatever buffering Lambda Web Adapter / the Function URL / CloudFront
apply to *this* stream is what they would apply to the real one.

GET and POST both work: the real frontend POSTs, and CloudFront OAC treats
POST bodies differently (SigV4 needs a body hash), so the spike measures both.
"""
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
START = time.time()


@app.get("/api/health")
def health():
    return {"status": "ok", "uptime_s": round(time.time() - START, 1)}


def _events(n: int, interval: float):
    t0 = time.time()
    for i in range(n):
        payload = {"i": i, "t": round(time.time() - t0, 3), "server_ts": time.time()}
        yield f"event: tick\ndata: {json.dumps(payload)}\n\n"
        time.sleep(interval)
    yield f"event: done\ndata: {json.dumps({'n': n, 'elapsed': round(time.time() - t0, 3)})}\n\n"


@app.api_route("/api/stream", methods=["GET", "POST"])
async def stream(request: Request, n: int = 30, interval: float = 1.0):
    # Same headers the real app sends (app.py): no-cache + no-transform is what
    # tells CloudFront/proxies not to buffer or compress the stream.
    return StreamingResponse(
        _events(n, interval),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Spike-Method": request.method,
            "X-Spike-Instance": os.environ.get("AWS_LAMBDA_LOG_STREAM_NAME", "local")[-12:],
        },
    )
