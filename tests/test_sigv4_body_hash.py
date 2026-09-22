"""Every client that POSTs a body to /api/ask must send x-amz-content-sha256.

Behind CloudFront Origin Access Control the origin request is SigV4-signed
with the body hash the viewer supplied; a POST body without the header is a
403 (measured in spike/sse-lwa/results.md, louisville-open-data-22e). The
header is inert on the current origin, so it ships ahead of the migration.
These tests pin the invariant on the three callers that reach the public URL.
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = "x-amz-content-sha256"


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_frontend_hashes_the_exact_body_it_sends():
    html = (ROOT / "static" / "index.html").read_text()
    # The helper exists, hashes with SubtleCrypto, and returns the header.
    assert "async function sigv4BodyHeader(body)" in html
    helper = html[html.index("async function sigv4BodyHeader"):html.index("async function streamAnswer")]
    assert "crypto.subtle.digest('SHA-256'" in helper
    assert f"'{HEADER}'" in helper
    # The fetch sends the same string it hashed: body is built once, then
    # passed both to the helper and as the request body.
    fetch_block = html[html.index("const resp = await fetch('/api/ask'"):]
    fetch_block = fetch_block[:fetch_block.index("signal: controller.signal")]
    assert "...(await sigv4BodyHeader(body))" in fetch_block
    assert re.search(r"\bbody,\s*$", fetch_block, re.M), "fetch must send the hashed `body` variable verbatim"
    assert "const body = JSON.stringify(" in html[html.index("async function streamAnswer"):html.index("const resp = await fetch('/api/ask'")]


def test_heartbeat_hash_constant_matches_its_body():
    sh = (ROOT / "monitoring" / "louisville-bot-heartbeat.sh").read_text()
    body = re.search(r"^ASK_BODY='(.*)'$", sh, re.M).group(1)
    const = re.search(r"^ASK_BODY_SHA256='([0-9a-f]{64})'$", sh, re.M).group(1)
    assert const == _sha256_hex(body)
    # And the curl call actually uses both.
    assert '-H "x-amz-content-sha256: $ASK_BODY_SHA256"' in sh
    assert '--data "$ASK_BODY"' in sh


def test_uptime_workflow_hashes_the_body_it_sends():
    yml = (ROOT / ".github" / "workflows" / "uptime.yml").read_text()
    probe = yml[yml.index("API end-to-end probe"):]
    assert "body='{\"question\":\"\"}'" in probe
    assert 'sha256sum' in probe and HEADER in probe
    assert '--data "$body"' in probe


def test_warm_cache_hashes_the_bytes_it_posts(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("warm_cache", ROOT / "warm_cache.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = (ROOT / "warm_cache.py").read_text()
    post = src[src.index("requests.post("):src.index("timeout=120")]
    assert "data=body" in post
    assert 'hashlib.sha256(body).hexdigest()' in post
    assert HEADER in post
