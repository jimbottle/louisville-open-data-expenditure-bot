"""Shared mutable state for Lambda: rate-limit buckets, the response cache, and
the usage/error counters behind /api/health — in one on-demand DynamoDB table.

The self-hosted deploy keeps the module dicts + JSON files in app.py (one
long-lived process, one disk). On Lambda every execution environment is a
fresh, concurrent copy with a read-only filesystem, so that state would be
per-container: the 5/min rate limit unenforced, the cache hit rate near zero,
counters lost on every recycle. app.py dispatches to an instance of DynamoState
when STATE_BACKEND=dynamodb and leaves the local path untouched otherwise, so
the two never share code that could drift.

One table, partition key `pk` (string), TTL attribute `ttl`:

    rl#<ip>#<minute>           n                 counter, expires 2 min after the window
    cache#<version>:<question> events, touched   the SSE frames (JSON), LRU stamp, 30-day TTL
    stats#usage#<YYYY-MM-DD>   *_today counters  atomic ADD, expires after 3 days
    stats#errors               counters, last_*, recent (list of epoch seconds)
    stats#limits               rpm, rpd, ... as reported by the provider

Every write is a single conditional or atomic UpdateItem, so concurrent
containers never race. Items are tiny and traffic is a few hundred writes a
day: on-demand billing rounds to pennies (LOU_MIGRATION_COMPAT.md).

Semantics preserved from the local implementation, each for a documented
reason in app.py:
  - cache keys carry the prompt-version prefix, so a prompt change orphans
    old answers (they simply stop matching; TTL reclaims them);
  - LRU touch on hit, so warm starter answers are never the ones evicted;
  - MAX_CACHE_ENTRIES cap, enforced on every insert;
  - an entry carrying a dead LegislationDetail link is dropped on read.
Differences: the rate limit is a fixed one-minute window rather than a sliding
60 s (a client can get at most 2x the cap across a window boundary, never
more), and the limiter fails OPEN if DynamoDB is unreachable — reserved
concurrency on the function is the hard ceiling, and a DynamoDB outage must
not take the site down with it.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import time
from datetime import date, datetime

log = logging.getLogger("state_store")


def _best_effort(default):
    """Availability over bookkeeping: a DynamoDB error in a cache or stats call
    is logged and the call degrades (a miss, a no-op, zeros) instead of turning
    the request into a 500. The limiter's fail-open in rate_allow is the same
    stance. `default` may be a value or a zero-arg callable."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *a, **k):
            try:
                return fn(self, *a, **k)
            except Exception as e:  # noqa: BLE001
                log.error("%s: DynamoDB unavailable (%s: %s); degrading", fn.__name__, type(e).__name__, e)
                return default() if callable(default) else default
        return wrapper
    return deco


def _empty_error_summary() -> dict:
    return {
        "total_errors": 0, "errors_last_hour": 0, "sql_gen_errors": 0, "sql_exec_errors": 0,
        "interpretation_errors": 0, "rate_limit_errors": 0, "quota_errors": 0,
        "last_quota_error": None, "quota_error_recent": False, "last_error": None, "last_error_time": None,
    }

DEAD_LINK_MARKER = "LegislationDetail.aspx"
QUOTA_CATEGORIES = ("quota", "daily_cap")
_RECENT_PRUNE_AT = 200          # trim the error-timestamp list past this many entries
_ERROR_WINDOW_S = 3600.0
_QUOTA_DEGRADE_S = 3600.0


def from_env():
    """A DynamoState when STATE_BACKEND=dynamodb, else None (local path)."""
    backend = os.environ.get("STATE_BACKEND", "local").strip().lower()
    if backend in ("", "local"):
        return None
    if backend != "dynamodb":
        raise ValueError(f"STATE_BACKEND must be 'local' or 'dynamodb', got {backend!r}")
    table = os.environ.get("STATE_TABLE", "").strip()
    if not table:
        raise ValueError("STATE_BACKEND=dynamodb requires STATE_TABLE")
    return DynamoState(
        table,
        region=os.environ.get("AWS_REGION") or None,
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None,
        rpm_limit=int(os.environ.get("IP_RPM_LIMIT", "5")),
        max_cache_entries=int(os.environ.get("MAX_CACHE_ENTRIES", "500")),
        cache_ttl_days=int(os.environ.get("CACHE_TTL_DAYS", "30")),
    )


def ensure_table(client, name: str) -> None:
    """Create the table (pk HASH, on-demand, TTL on `ttl`) if it does not exist.
    For tests and the IaC smoke path; production creates it from the stack."""
    existing = client.list_tables().get("TableNames", [])
    if name not in existing:
        client.create_table(
            TableName=name,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=name)
    ttl = client.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
        client.update_time_to_live(TableName=name,
                                   TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})


def _s(v) -> dict:
    return {"S": str(v)}


def _n(v) -> dict:
    return {"N": str(int(v)) if float(v).is_integer() else repr(float(v))}


def _num(item: dict, key: str, default=0):
    v = item.get(key)
    if not v or "N" not in v:
        return default
    f = float(v["N"])
    return int(f) if f.is_integer() else f


def _str(item: dict, key: str, default=None):
    v = item.get(key)
    return v["S"] if v and "S" in v else default


class DynamoState:
    def __init__(self, table: str, *, region: str | None = None, endpoint_url: str | None = None,
                 rpm_limit: int = 5, max_cache_entries: int = 500, cache_ttl_days: int = 30,
                 client=None):
        if client is None:
            import boto3  # lazy: only the Lambda image carries boto3
            from botocore.config import Config
            client = boto3.client(
                "dynamodb", region_name=region, endpoint_url=endpoint_url,
                # A state lookup must never stall a request: short timeouts,
                # one retry. The limiter fails open on error (see module doc).
                config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 2}),
            )
        self._c = client
        self.table = table
        self.rpm_limit = rpm_limit
        self.max_cache_entries = max_cache_entries
        self.cache_ttl_s = cache_ttl_days * 86400

    # ── rate limit ──────────────────────────────────────────────────────────

    def rate_allow(self, ip: str, now: float | None = None) -> bool:
        """True if this request is within the per-IP cap for the current
        one-minute window. One conditional atomic increment; correct across
        any number of concurrent containers."""
        now = time.time() if now is None else now
        bucket = int(now // 60)
        try:
            self._c.update_item(
                TableName=self.table, Key={"pk": _s(f"rl#{ip}#{bucket}")},
                UpdateExpression="ADD n :one SET #ttl = :ttl",
                ConditionExpression="attribute_not_exists(n) OR n < :lim",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":one": _n(1), ":lim": _n(self.rpm_limit),
                                           ":ttl": _n((bucket + 2) * 60)},
            )
            return True
        except self._c.exceptions.ConditionalCheckFailedException:
            return False
        except Exception as e:  # noqa: BLE001 — availability over enforcement
            log.error("Rate limiter unavailable (%s: %s); allowing request", type(e).__name__, e)
            return True

    # ── response cache ──────────────────────────────────────────────────────

    @_best_effort(None)
    def cache_get(self, key: str, now: float | None = None) -> list[str] | None:
        now = time.time() if now is None else now
        pk = f"cache#{key}"
        r = self._c.get_item(TableName=self.table, Key={"pk": _s(pk)})
        item = r.get("Item")
        if not item:
            return None
        events = json.loads(_str(item, "events", "[]"))
        if any(DEAD_LINK_MARKER in f for f in events):
            # A cached answer never re-runs retrieval, so a dead citation link
            # would be replayed forever; drop it and let the question re-run.
            self._c.delete_item(TableName=self.table, Key={"pk": _s(pk)})
            log.info("Dropped cached answer carrying a dead citation link: %s", key[:60])
            return None
        # LRU touch: a served answer must not age out, and its TTL moves too.
        # Evicted between the read and the touch? Still a hit — serve it.
        try:
            self._c.update_item(
                TableName=self.table, Key={"pk": _s(pk)},
                UpdateExpression="SET touched = :now, #ttl = :ttl",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": _n(now), ":ttl": _n(now + self.cache_ttl_s)},
            )
        except self._c.exceptions.ConditionalCheckFailedException:
            pass
        return events

    @_best_effort(None)
    def cache_put(self, key: str, events: list[str], now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._c.put_item(TableName=self.table, Item={
            "pk": _s(f"cache#{key}"), "events": _s(json.dumps(events)),
            "touched": _n(now), "ttl": _n(now + self.cache_ttl_s),
        })
        self._evict_to_cap()

    @_best_effort(False)
    def cache_delete(self, key: str) -> bool:
        r = self._c.delete_item(TableName=self.table, Key={"pk": _s(f"cache#{key}")},
                                ReturnValues="ALL_OLD")
        return "Attributes" in r

    @_best_effort(0)
    def cache_clear(self) -> int:
        keys = [pk for pk, _ in self._scan_cache(prefix="", with_touched=False)]
        self._batch_delete(keys)
        return len(keys)

    @_best_effort(dict)
    def cache_items(self, prefix: str = "") -> dict[str, list[str]]:
        """key -> events for entries whose key starts with prefix (the current
        prompt version, normally). Admin listing + warm_cache verification."""
        out = {}
        for pk, item in self._scan_cache(prefix=prefix, with_touched=False, with_events=True):
            out[pk[len("cache#"):]] = json.loads(_str(item, "events", "[]"))
        return out

    @_best_effort(0)
    def cache_len(self, prefix: str = "") -> int:
        return sum(1 for _ in self._scan_cache(prefix=prefix, with_touched=False))

    def _evict_to_cap(self) -> None:
        rows = list(self._scan_cache(prefix="", with_touched=True))
        excess = len(rows) - self.max_cache_entries
        if excess <= 0:
            return
        rows.sort(key=lambda r: _num(r[1], "touched", 0.0))
        self._batch_delete([pk for pk, _ in rows[:excess]])
        log.info("Evicted %d least-recently-used cache entries (cap %d)", excess, self.max_cache_entries)

    def _scan_cache(self, prefix: str, with_touched: bool, with_events: bool = False):
        proj = ["pk"] + (["touched"] if with_touched else []) + (["events"] if with_events else [])
        kwargs = dict(
            TableName=self.table,
            FilterExpression="begins_with(pk, :p)",
            ExpressionAttributeValues={":p": _s(f"cache#{prefix}")},
            ProjectionExpression=", ".join(proj),
        )
        while True:
            r = self._c.scan(**kwargs)
            for item in r.get("Items", []):
                yield _str(item, "pk"), item
            if "LastEvaluatedKey" not in r:
                return
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def _batch_delete(self, pks: list[str]) -> None:
        for i in range(0, len(pks), 25):
            chunk = pks[i:i + 25]
            req = {self.table: [{"DeleteRequest": {"Key": {"pk": _s(pk)}}} for pk in chunk]}
            while req:
                r = self._c.batch_write_item(RequestItems=req)
                req = r.get("UnprocessedItems") or {}

    # ── stats ───────────────────────────────────────────────────────────────

    @_best_effort(None)
    def stats_error(self, category: str, detail: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else now
        msg = f"{category}: {detail}" if detail else category
        names = {"#cat": f"{category}_errors"}
        values = {":one": _n(1), ":msg": _s(msg), ":iso": _s(datetime.now().isoformat()),
                  ":t": {"L": [_n(now)]}, ":empty": {"L": []}}
        sets = ["last_error = :msg", "last_error_time = :iso",
                "recent = list_append(if_not_exists(recent, :empty), :t)"]
        if category in QUOTA_CATEGORIES:
            sets += ["last_quota_error = :msg", "last_quota_error_time = :qt"]
            values[":qt"] = _n(now)
        r = self._c.update_item(
            TableName=self.table, Key={"pk": _s("stats#errors")},
            UpdateExpression="ADD total_errors :one, #cat :one SET " + ", ".join(sets),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
            ReturnValues="UPDATED_NEW",
        )
        recent = r.get("Attributes", {}).get("recent", {}).get("L", [])
        if len(recent) > _RECENT_PRUNE_AT:
            keep = [x for x in recent if now - float(x["N"]) < _ERROR_WINDOW_S]
            # Best-effort trim; a concurrent append lost here costs one
            # timestamp in a count that is only ever compared with "> 5".
            self._c.update_item(TableName=self.table, Key={"pk": _s("stats#errors")},
                                UpdateExpression="SET recent = :r",
                                ExpressionAttributeValues={":r": {"L": keep}})

    @_best_effort(_empty_error_summary)
    def stats_error_summary(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        item = self._c.get_item(TableName=self.table, Key={"pk": _s("stats#errors")}).get("Item", {})
        recent = [float(x["N"]) for x in item.get("recent", {}).get("L", [])]
        last_quota_t = _num(item, "last_quota_error_time", None)
        return {
            "total_errors": _num(item, "total_errors"),
            "errors_last_hour": sum(1 for t in recent if now - t < _ERROR_WINDOW_S),
            "sql_gen_errors": _num(item, "sql_gen_errors"),
            "sql_exec_errors": _num(item, "sql_exec_errors"),
            "interpretation_errors": _num(item, "interpretation_errors"),
            "rate_limit_errors": _num(item, "rate_limit_errors"),
            "quota_errors": _num(item, "quota_errors") + _num(item, "daily_cap_errors"),
            "last_quota_error": _str(item, "last_quota_error"),
            "quota_error_recent": last_quota_t is not None and now - last_quota_t < _QUOTA_DEGRADE_S,
            "last_error": _str(item, "last_error"),
            "last_error_time": _str(item, "last_error_time"),
        }

    @_best_effort(None)
    def stats_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0,
                    today: str | None = None) -> None:
        today = today or date.today().isoformat()
        self._c.update_item(
            TableName=self.table, Key={"pk": _s(f"stats#usage#{today}")},
            UpdateExpression=("ADD requests_today :one, prompt_tokens_today :p, "
                              "completion_tokens_today :c, tokens_today :t "
                              "SET #date = :d, #ttl = :ttl"),
            ExpressionAttributeNames={"#date": "date", "#ttl": "ttl"},
            ExpressionAttributeValues={":one": _n(1), ":p": _n(prompt_tokens), ":c": _n(completion_tokens),
                                       ":t": _n(prompt_tokens + completion_tokens), ":d": _s(today),
                                       ":ttl": _n(time.time() + 3 * 86400)},
        )

    @_best_effort(lambda: {"requests_today": 0, "tokens_today": 0, "prompt_tokens_today": 0, "completion_tokens_today": 0, "date": date.today().isoformat()})
    def stats_usage_get(self, today: str | None = None) -> dict:
        today = today or date.today().isoformat()
        item = self._c.get_item(TableName=self.table, Key={"pk": _s(f"stats#usage#{today}")}).get("Item", {})
        return {
            "requests_today": _num(item, "requests_today"),
            "tokens_today": _num(item, "tokens_today"),
            "prompt_tokens_today": _num(item, "prompt_tokens_today"),
            "completion_tokens_today": _num(item, "completion_tokens_today"),
            "date": today,
        }

    @_best_effort(None)
    def stats_limits_set(self, limits: dict[str, int]) -> None:
        if not limits:
            return
        names = {f"#k{i}": k for i, k in enumerate(limits)}
        values = {f":v{i}": _n(v) for i, v in enumerate(limits.values())}
        self._c.update_item(
            TableName=self.table, Key={"pk": _s("stats#limits")},
            UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(limits))),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )

    @_best_effort(lambda: {k: None for k in ("rpm", "rpd", "tpm", "tpd", "rpm_remaining", "rpd_remaining", "tpm_remaining", "tpd_remaining")})
    def stats_limits_get(self) -> dict:
        item = self._c.get_item(TableName=self.table, Key={"pk": _s("stats#limits")}).get("Item", {})
        return {k: _num(item, k, None) for k in
                ("rpm", "rpd", "tpm", "tpd", "rpm_remaining", "rpd_remaining", "tpm_remaining", "tpd_remaining")}
