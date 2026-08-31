#!/usr/bin/env python3
"""
Core analytics engine for ArcGIS-sourced CSV data.

Uses DuckDB for SQL execution and any OpenAI-compatible API for natural language -> SQL.
Designed for datasets pulled via pull_arcgis.py.

Can be used as:
  - Importable module (used by app.py for web deployment)
  - Standalone CLI (python analytics_agent.py data/file.csv -q "question")
"""

import argparse
import json
import logging
import os
import re
import sys
import textwrap
import threading
import time

import duckdb
import openai
import pandas as pd

log = logging.getLogger("analytics")

# Retry ladder for ordinary 429s. Env-tunable (louisville-open-data-5pg): on a
# per-second-billed platform the default 2 x 16s of sleep is real money, and
# the right value depends on the provider's window. The ladder itself stays —
# it is what the provider fallback rides on.
MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3") or 3)
RETRY_BASE_DELAY = float(os.environ.get("LLM_RETRY_BASE_DELAY", "16") or 16)  # seconds


# Which tier served the last call. DISPLAY ONLY (dev mode / the debug event):
# it is written by every concurrent request, so nothing may branch on it.
# Anything that needs to know who served THIS call passes a provenance dict to
# _call_with_retry.
_last_tier_used = "free"


def get_last_tier_used() -> str:
    """Return which tier was used on the most recent LLM call."""
    return _last_tier_used



def is_quota_error(e: Exception) -> bool:
    """Billing exhaustion on the LLM account — only paying more money fixes it.

    Distinct from is_rate_limit_error: a 429 clears by itself in a minute; this
    does not. Cerebras retired its always-free tier in July 2026, so an expired
    trial now answers every call with HTTP 402 `payment_required`, which the
    openai SDK raises as a bare APIStatusError (no dedicated subclass) — it fell
    through to the generic handler and told users to reword their question.

    Checked BEFORE the rate-limit branch because OpenAI-compatible providers
    report the same condition as a 429 with `insufficient_quota`.
    """
    if getattr(e, "status_code", None) == 402:
        return True
    s = str(e).lower()
    return any(k in s for k in ("payment_required", "insufficient_quota", "insufficient_credit"))


# Some primary failures do not clear on a retry: a key that is out of credit
# keeps answering 402, and a retired model with no free replacement keeps
# 404-ing (and re-listing a several-hundred-entry catalogue to discover that).
# Every later call would pay to learn the same thing, three times per question.
# Latch it, go straight to the fallback provider, and recheck occasionally so
# credit added or a slug restored heals by itself.
_primary_unusable_at = None
PRIMARY_RECHECK_SECONDS = 900


def _primary_is_unusable() -> bool:
    """True while the primary is known to be unusable (within the recheck window)."""
    return (
        _primary_unusable_at is not None
        and (time.time() - _primary_unusable_at) < PRIMARY_RECHECK_SECONDS
    )


def _mark_primary_unusable(unusable: bool = True) -> None:
    global _primary_unusable_at
    _primary_unusable_at = time.time() if unusable else None


def is_daily_cap_error(e: Exception) -> bool:
    """OpenRouter's free-model DAILY allowance is spent (50/day under $10 of credits).

    It arrives as a 429, but unlike an ordinary rate limit it does not clear in
    16 seconds — it resets at midnight UTC. Waiting out the retry ladder just
    stalls the user for ~48s before the same failure, so this is handled on the
    quota path (straight to the fallback provider) rather than the rate-limit one.
    """
    s = str(e).lower()
    return "free-models-per-day" in s or ("per-day" in s and "free" in s)


class EmptyCompletionError(RuntimeError):
    """A provider returned a completion with no content.

    Its own class (not a bare ValueError) so `_call_with_retry` can fail the
    call over to the other provider and `app.is_service_error` can tell the user
    it is our end, not their wording.
    """


# The wordings OpenAI-compatible providers use for a prompt that overflows the
# model's window (code context_length_exceeded, "maximum context length is …",
# "context window", "too many tokens").
_CONTEXT_OVERFLOW = re.compile(
    r"context[_ ]?(?:window|length)|maximum context|too many tokens", re.I)


def _is_auth_error(e: Exception) -> bool:
    """A bad or revoked key: not transient — every later call fails the same
    way until a human fixes the key, so it earns the primary-unusable latch
    (which rechecks every PRIMARY_RECHECK_SECONDS and heals by itself)."""
    return isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError))


class StreamStalledError(RuntimeError):
    """A streamed completion went quiet: no chunk within the stall window.

    The failure mode the plain timeouts miss — observed live 2026-08-31 with
    OpenRouter's nemotron upstream overloaded: the stream stayed OPEN and
    dribbled a few tokens a minute, so nothing errored, the 90s cap eventually
    fired, and the reader got a truncated answer while a fast fallback sat
    idle. Raised mid-iteration so the caller's fallback path takes over.
    """


# Seconds a streamed completion may go without producing a chunk (including
# before the FIRST chunk) before it is abandoned. 0 disables the guard.
STREAM_STALL_SECONDS = float(os.environ.get("STREAM_STALL_SECONDS", "20") or 0)


def _iter_with_stall_guard(stream, stall_seconds: float | None = None):
    """Yield from `stream`, raising StreamStalledError if it goes quiet.

    A sync OpenAI stream blocks in __next__, so the wait happens on a reader
    thread feeding a queue; the consumer waits at most `stall_seconds` per
    chunk. On stall the underlying stream is closed (best-effort) and the
    daemon reader is left to die with it.
    """
    stall = STREAM_STALL_SECONDS if stall_seconds is None else stall_seconds
    if not stall or stall <= 0:
        yield from stream
        return
    import queue as _queue
    q: "_queue.Queue" = _queue.Queue()

    def _reader():
        try:
            for item in stream:
                q.put(("item", item))
            q.put(("done", None))
        except Exception as e:  # surfaced to the consumer in order
            q.put(("err", e))

    threading.Thread(target=_reader, daemon=True).start()
    while True:
        try:
            kind, val = q.get(timeout=stall)
        except _queue.Empty:
            try:
                stream.close()
            except Exception:
                pass
            raise StreamStalledError(
                f"LLM stream produced nothing for {stall:.0f}s; abandoning it")
        if kind == "item":
            yield val
        elif kind == "done":
            return
        else:
            raise val


def is_provider_error(e: Exception) -> bool:
    """A failure on the PROVIDER's side that a different provider can dodge:
    5xx, connection/timeout, a bad key, or OpenRouter's HTTP-200-with-an-error
    bodies ("Upstream error from Nvidia: Service temporarily overloaded", seen
    live 2026-08-31 killing the interpretation while a funded Cerebras key sat
    idle — the generic branch re-raised without ever trying it).

    NotFoundError is deliberately excluded: model_not_found has its own
    resolution path (_call_with_model_fallback) that must see it first.
    RateLimitError is excluded: the ladder handles it with its own pacing.
    """
    if isinstance(e, openai.RateLimitError):
        return False
    if isinstance(e, (openai.BadRequestError, openai.ConflictError,
                      openai.UnprocessableEntityError)):
        # 400/409/422 are OUR request being wrong (a malformed payload), not
        # the provider being down: the same request fails the same way on the
        # fallback, so crossing over burns a paid call and logs a "provider
        # error" for a local bug. ONE exception: a context-length overflow is
        # about the MODEL's window, and primary and fallback are different
        # models — the same request can fit the fallback's window, so that
        # 400 is allowed to cross.
        return bool(isinstance(e, openai.BadRequestError)
                    and _CONTEXT_OVERFLOW.search(str(e)))
    if isinstance(e, openai.NotFoundError):
        # Only a genuine model_not_found belongs to the resolution path. An
        # upstream 404 ("Provider returned error", code 404, seen live
        # 2026-08-31 from OpenRouter/Nvidia) is the provider being broken,
        # and must cross to the fallback like any other provider failure.
        return not _is_model_not_found(e)
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError,
                      openai.InternalServerError, openai.AuthenticationError,
                      openai.PermissionDeniedError)):
        return True
    # The SDK raises plain APIError for in-stream error payloads and other
    # provider-shaped failures; quota-ish ones were classified before this.
    return isinstance(e, openai.APIError)


def _call_with_retry(fn, on_retry=None, fallback_fn=None, provenance=None):
    """Run an LLM call, falling over to the fallback provider when the primary can't serve.

    One classification point, deliberately: an earlier version special-cased the
    first retry inside the rate-limit ladder, so "429 first, out-of-credit
    second" took the unclassified path — no fallback on a 402, and no latch on
    an insufficient_quota 429, which is the ~48s-per-question stall the
    classifier exists to prevent.

    Failure kinds and what each earns:
      * out of credit / past the daily allowance — the other provider, now. No
        amount of waiting fixes a spent key, and the primary is latched out.
      * empty completion — the other provider, now. It will not fill itself in.
      * ordinary rate limit — one more try on the primary (per-minute windows
        clear), then the other provider, then the rest of the ladder.

    provenance: optional dict, filled with {"used_fallback": bool}. Per call, so
    a caller can tell who served IT — SSE requests run on threadpool threads, and
    a module global would let a concurrent call answer that question wrongly.
    """
    global _last_tier_used

    def _served(via_fallback):
        global _last_tier_used
        _last_tier_used = "paid" if via_fallback else get_primary_tier()
        if provenance is not None:
            provenance["used_fallback"] = via_fallback

    if fallback_fn and _primary_is_unusable():
        try:
            result = fallback_fn()
            _served(True)
            return result
        except Exception as paid_err:
            # Fallback is unusable too — clear the latch so the primary is
            # retried normally below rather than being skipped on a stale flag.
            log.warning("Fallback provider failed while primary was latched out: %s", paid_err)
            _mark_primary_unusable(False)

    fallback_tried = False
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn()
            _served(False)
            return result
        except Exception as e:
            exhausted = is_quota_error(e) or is_daily_cap_error(e)
            empty = isinstance(e, EmptyCompletionError)
            rate_limited = isinstance(e, openai.RateLimitError)
            provider_down = not (exhausted or empty or rate_limited) and is_provider_error(e)

            if not (exhausted or empty or rate_limited or provider_down):
                log.error("LLM call failed (attempt %d): %s", attempt, e)
                raise

            # A plain rate limit gets one more shot at the primary first; the
            # others are hopeless on this key right now, so they cross over at
            # once. A transient provider failure (5xx / upstream overloaded)
            # falls over WITHOUT latching; a bad key latches below, because it
            # keeps failing until a human rotates it.
            if (exhausted or empty or provider_down or attempt > 1) and fallback_fn and not fallback_tried:
                fallback_tried = True
                log.info("Primary unavailable (%s), using fallback provider",
                         "exhausted" if exhausted else ("empty reply" if empty else (
                             "provider error" if provider_down else "rate limited")))
                if on_retry:
                    on_retry(attempt, MAX_RETRIES, 0)
                try:
                    result = fallback_fn()
                    _served(True)
                    if exhausted or _is_auth_error(e):
                        # Credit/allowance exhaustion and dead keys: durable
                        # states where every unlatched call would pay a doomed
                        # primary round-trip first. Latching on an ordinary 429
                        # or a 5xx blip would instead route 15 minutes of
                        # traffic to the billed provider over a hiccup.
                        _mark_primary_unusable()
                    return result
                except Exception as fallback_err:
                    log.warning("Fallback provider failed: %s", fallback_err)
                    if exhausted or empty or provider_down or attempt == MAX_RETRIES:
                        # Raise the PRIMARY's error, not the fallback's: app.py
                        # picks the user-facing message from this exception, and
                        # a Cerebras 402 here would say "the bill is unpaid" for
                        # an allowance that resets at midnight.
                        raise e from fallback_err
                    # Ordinary rate limit with attempts left: keep walking the ladder.

            if exhausted or empty or provider_down:
                log.error("Primary cannot serve (%s) and no fallback answered: %s",
                          "exhausted" if exhausted else ("empty reply" if empty else "provider error"), e)
                raise

            if attempt == MAX_RETRIES:
                log.warning("Rate limit: all %d retries exhausted (primary and fallback)", MAX_RETRIES)
                raise

            delay = RETRY_BASE_DELAY
            match = re.search(r'retry in ([\d.]+)s', str(e))
            if match:
                delay = min(float(match.group(1)) + 1, 60)
            log.info("Rate limited (attempt %d/%d), retrying in %.0fs", attempt, MAX_RETRIES, delay)
            if on_retry:
                on_retry(attempt, MAX_RETRIES, delay)
            time.sleep(delay)


DEFAULT_MODEL = "qwen-3-235b-a22b-instruct-2507"
DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"

# Rows rendered into the result table the model interprets and the UI shows.
# pandas splits this budget between the head and the tail, so a longer result
# is served with a gap in the middle — see execute_sql_safe.
MAX_DISPLAY_ROWS = 50

# Appended to a truncated result table. pandas renders the head and the tail
# with a bare "..." between them, and a model reading that enumerates the
# visible head and presents it as the whole answer: 102 grant funds became
# "here are the 24 sources", silently dropping everything past the gap.
#
# Deliberately order-NEUTRAL. order_for_display sorts by the measure only when
# the SQL contains no ORDER BY at all; anything carrying one is served as
# built, which includes ASC and keys like a month or a payee name. Calling the
# head "the largest" would be a reversal for exactly those queries — the same
# failure class the ordering rules were stripped back to avoid, just committed
# in prose instead of in the frame.
#
# Hashed into CACHE_VERSION (see app.py): it is model-visible input, so a
# change here must orphan answers written under the previous wording.
# The counts clause interpolated into the note. Module-level for the same
# reason as the note itself: it is the sentence the interpretation prompt tells
# the model to quote ("using the data row count from the note"), so editing it
# changes what the model reads and must invalidate cached answers. Kept beside
# TRUNCATION_NOTE so the pair is hashed together.
TRUNCATION_COUNTS = "this table has {rows} rows"
TRUNCATION_COUNTS_WITH_TOTALS = (
    "this table has {rows} rows, of which {entities} are data rows and the "
    "rest are totals/subtotals"
)

# Emitted whenever a total was relocated, at EVERY table size — not only when
# the table is also truncated. A moved total sits at the bottom of a descending
# list, so without this the model reads it as the end of the ranking and reports
# the range running down to it: the same misreading as before, at the other end
# of the table. Most ROLLUP results are well under the truncation cap, so
# gating this on truncation left the common shape unexplained.
#
# Says what the rows ARE rather than what they are not, so it stays clear of the
# position words every model-visible surface here is held to.
TOTALS_MOVED_NOTE = (
    "[NOTE: the last {n} row(s) of this table are totals/subtotals, moved to "
    "the end. They are sums of the rows above, not entries in the ranking.]"
)

TRUNCATION_NOTE = (
    "[TRUNCATED: {counts}. Shown above are the first {half} rows and the last "
    "{half}, in the order the query produced them; the {omitted} rows in "
    "between are NOT shown. Do not describe the shown rows as the largest or "
    "the smallest unless the query's own ordering says so. Any list you give "
    "from this is partial — say how many you are naming, and quote the DATA "
    "row count when you say how many exist.]"
)


# ── Model fallback ───────────────────────────────────────────────────────────
# Providers deprecate models without notice (qwen-3-235b 404'd in prod while
# the container env still named it). On a model_not_found error we resolve a
# replacement from MODEL_FALLBACKS (then anything the account offers), record
# it module-wide so every later request uses it directly, and retry — no
# container rebuild or env change needed. /api/health surfaces the switch.

DEFAULT_MODEL_FALLBACKS = ["gpt-oss-120b", "zai-glm-4.7", "llama-3.3-70b", "llama3.1-8b"]

# OpenRouter slugs, for when OpenRouter is the primary. The bare Cerebras ids
# above can never match a 'vendor/model:free' slug, so without a provider-aware
# list every preference misses and the resolver falls through to "anything the
# account offers" — which on OpenRouter means an arbitrary entry from a
# catalogue of hundreds, almost all of them paid. Ranked by the 2026-08-18
# benchmark on the real NL->SQL task (see CLAUDE.md).
DEFAULT_OPENROUTER_MODEL_FALLBACKS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "poolside/laguna-s-2.1:free",
    "openai/gpt-oss-20b:free",
]

_active_model = None          # replacement model once a fallback has engaged
_model_fallback_event = None  # {"from", "to", "time"} of the last fallback
_fallback_lock = threading.Lock()  # SSE requests run on threadpool threads


def _fallback_preferences() -> list:
    env = os.environ.get("MODEL_FALLBACKS", "")
    if env.strip():
        return [m.strip() for m in env.split(",") if m.strip()]
    if _openrouter_key():
        return list(DEFAULT_OPENROUTER_MODEL_FALLBACKS)
    return list(DEFAULT_MODEL_FALLBACKS)


def get_active_model(configured: str) -> str:
    """The model actually in use: the configured one unless a fallback engaged."""
    return _active_model or configured


def get_model_fallback_event() -> dict | None:
    """Details of the last model fallback, or None if none has occurred."""
    return _model_fallback_event


def _is_model_not_found(exc: Exception) -> bool:
    if not isinstance(exc, openai.NotFoundError):
        return False
    # Prefer the structured error code (Cerebras/OpenAI send code=model_not_found);
    # fall back to a substring match only when no code is present.
    code = getattr(exc, "code", None)
    if code is None and isinstance(getattr(exc, "body", None), dict):
        # The openai SDK populates .code from a dict body itself; this branch
        # only guards non-standard OpenAI-compatible providers / hand-rolled
        # exceptions where that didn't happen.
        code = exc.body.get("code")
    if code is not None:
        return code == "model_not_found"
    return "model" in str(exc).lower()


# A short cooldown on re-listing the catalogue, separate from the primary latch.
# A failed listing is not evidence the primary is dead (so it must not latch),
# but a SUSTAINED outage would otherwise have every call re-pay the discovery:
# clients are built with max_retries=0 and a 60s timeout, and a /models endpoint
# that hangs rather than errors stalls each of the 2-3 calls per question with
# nothing streamed to the user meanwhile. One minute is short enough that a
# genuine one-off blip still re-resolves on the next question.
_catalogue_unavailable_at = None
CATALOGUE_RETRY_SECONDS = 60


def _catalogue_is_cooling_down() -> bool:
    return (
        _catalogue_unavailable_at is not None
        and (time.time() - _catalogue_unavailable_at) < CATALOGUE_RETRY_SECONDS
    )


def _mark_catalogue_unavailable(unavailable: bool = True) -> None:
    global _catalogue_unavailable_at
    _catalogue_unavailable_at = time.time() if unavailable else None


class ModelCatalogueUnavailable(RuntimeError):
    """The provider's model list could not be read.

    Kept distinct from "read it fine, nothing usable in there": that answer is
    durable and worth latching the primary out over, while this one may be a
    single timeout. Conflating them let one failed GET /models route fifteen
    minutes of traffic to the billed provider.
    """


def _resolve_fallback_model(client: openai.OpenAI, bad_model: str):
    """Pick a replacement the provider actually offers, or None if it offers none.

    Raises ModelCatalogueUnavailable if the catalogue could not be read at all.

    The last resort — "anything available" — is deliberately restricted to
    free models when the primary is OpenRouter. Its catalogue lists hundreds of
    models, nearly all paid: taking the first entry would silently move the bot
    onto an unvetted, billable model (or one that 402s on a free-tier key)
    because a free slug churned.
    """
    try:
        available = [m.id for m in client.models.list().data]
    except Exception as e:
        log.error("Model fallback: could not list provider models: %s", e)
        raise ModelCatalogueUnavailable(str(e)) from e
    for pref in _fallback_preferences():
        if pref != bad_model and pref in available:
            return pref
    if _openrouter_key():
        free = [m for m in available if m != bad_model and m.endswith(":free")]
        if not free:
            log.error("Model fallback: no free OpenRouter model available to replace '%s'", bad_model)
        return free[0] if free else None
    return next((m for m in available if m != bad_model), None)


def _record_model_fallback(from_model: str, to_model: str) -> None:
    """Idempotent: concurrent requests that raced through the same deprecation
    record (and ERROR-log) the switch exactly once."""
    global _active_model, _model_fallback_event
    with _fallback_lock:
        if _active_model == to_model:
            return
        _active_model = to_model
        _model_fallback_event = {
            "from": from_model,
            "to": to_model,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    log.error("MODEL FALLBACK: '%s' no longer exists at provider; now using '%s'", from_model, to_model)


def _call_with_model_fallback(make_call, client, model, on_retry=None, fallback_client=None,
                              fallback_model=None):
    """Run an LLM call, surviving provider model deprecation.

    make_call(client, model) must return a zero-arg callable. On a
    model_not_found error the replacement is resolved once, recorded
    module-wide, and the call retried with it.

    fallback_model is the model id to use on the FALLBACK client, for when the
    two clients are different providers (OpenRouter primary, Cerebras fallback):
    a slug like 'vendor/model:free' means nothing to Cerebras. Passing it also
    pins the fallback across a deprecation switch, because the replacement is
    resolved from the PRIMARY provider's catalogue. Left unset, both clients
    share one catalogue and the fallback follows the replacement as before.
    """
    global _last_tier_used
    model = get_active_model(model)
    fb_model = fallback_model or model
    fallback_fn = make_call(fallback_client, fb_model) if fallback_client else None
    try:
        return _call_with_retry(make_call(client, model), on_retry=on_retry, fallback_fn=fallback_fn)
    except openai.NotFoundError as e:
        if not _is_model_not_found(e):
            raise
        # Another thread may have already switched while this call was in flight.
        already = get_active_model(model)
        # `catalogue_read` separates "the provider has nothing for us" (durable,
        # latch it) from "we could not ask" (possibly a one-off timeout, so keep
        # re-asking). Both used to arrive as a bare None.
        catalogue_read = True
        if already != model:
            replacement = already
        elif fallback_fn and _catalogue_is_cooling_down():
            # Asked recently and could not get an answer; don't stall this
            # question on the same dead endpoint — fall back below instead.
            # Only when there IS a fallback: with none, the listing is the only
            # route to an answer (a Cerebras-only deployment whose MODEL was
            # deprecated), so paying the timeout beats failing outright.
            log.info("Skipping model re-resolution: the catalogue listing failed within the last %ds",
                     CATALOGUE_RETRY_SECONDS)
            replacement, catalogue_read = None, False
        else:
            try:
                replacement = _resolve_fallback_model(client, model)
                _mark_catalogue_unavailable(False)
            except ModelCatalogueUnavailable as list_err:
                log.warning("Could not read the model catalogue while replacing '%s': %s", model, list_err)
                _mark_catalogue_unavailable()
                replacement, catalogue_read = None, False
        if not replacement:
            # No replacement the provider offers (or models.list() failed). The
            # other provider is a whole second chance sitting right here, and a
            # model_not_found never reaches it through _call_with_retry — that
            # is neither an exhaustion nor a rate limit, so it re-raises first.
            # Without this, retiring the pinned free slug killed every question
            # while a funded Cerebras key stood idle.
            if fallback_fn:
                log.warning("No replacement for '%s' at the primary provider; using the fallback provider", model)
                # Latch only when the catalogue actually said there is nothing
                # usable. In THAT state every call lands here and would each pay
                # a 404 plus a fresh models.list() over a several-hundred-entry
                # catalogue, two or three times per question, to rediscover what
                # we already know; the latch pays it once per window and heals
                # when the window expires. A failed listing is not that: it may
                # be one timeout, and latching would spend fifteen minutes of
                # billed traffic without ever retrying the free model that is
                # probably still sitting in the catalogue.
                if catalogue_read:
                    _mark_primary_unusable()
                try:
                    # Through the ladder, not a bare call: this is the only path
                    # serving the question now, so the fallback deserves the same
                    # retries and the same on_retry progress events as anywhere
                    # else. A single transient 429 used to kill the question.
                    result = _call_with_retry(fallback_fn, on_retry=on_retry)
                    _last_tier_used = "paid"
                    return result
                except Exception as fallback_err:
                    log.warning("Fallback provider failed after model_not_found: %s", fallback_err)
                    # app.py classifies the exception it is given and does not
                    # look at __cause__, so an out-of-credit or spent-allowance
                    # fallback has to surface on its own terms — otherwise the
                    # user is told to "try again in a little while" about a
                    # condition that only money or midnight resolves.
                    if is_quota_error(fallback_err) or is_daily_cap_error(fallback_err):
                        raise
                    raise e from fallback_err
            raise
        log.warning("Model '%s' not found at provider; retrying with '%s'", model, replacement)
        if fallback_client and fallback_model is None:
            # Same provider on both keys, so the dead model is dead on the
            # fallback too: rebind it to the replacement. A cross-provider
            # fallback keeps its own pinned model.
            fallback_fn = make_call(fallback_client, replacement)
        provenance = {}
        result = _call_with_retry(make_call(client, replacement), on_retry=on_retry,
                                  fallback_fn=fallback_fn, provenance=provenance)
        # Record only when the REPLACEMENT served THIS call. If the other
        # provider's fallback rescued it, the replacement is unproven — pinning
        # it process-wide would put a failing round trip in front of every later
        # question and advertise it in /api/health. Read from the per-call dict,
        # not a global: a concurrent request would answer for the wrong call.
        if provenance.get("used_fallback"):
            log.warning("Replacement model '%s' did not serve the call (fallback provider did); not recording it", replacement)
        else:
            _record_model_fallback(model, replacement)
        return result


def init_database(csv_path: str) -> tuple[duckdb.DuckDBPyConnection, str]:
    """Load CSV into DuckDB and return connection + schema description."""
    con = duckdb.connect()
    con.execute(f"CREATE TABLE data AS SELECT * FROM read_csv_auto('{csv_path}')")
    schema_desc = build_schema_description(con, "data")
    return con, schema_desc


def build_schema_description(con: duckdb.DuckDBPyConnection, table: str) -> str:
    """Generate a schema description from the loaded DuckDB table."""
    columns = con.execute(f"DESCRIBE {table}").fetchall()
    sample = con.execute(f"SELECT * FROM {table} LIMIT 3").fetchdf()
    row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    lines = [f"Table: {table}", f"Row count: {row_count:,}", "", "Columns:"]
    for col_name, col_type, *_ in columns:
        line = f"  - {col_name} ({col_type})"
        if "VARCHAR" in col_type.upper():
            try:
                distincts = con.execute(
                    f"SELECT DISTINCT {col_name} FROM {table} WHERE {col_name} IS NOT NULL LIMIT 8"
                ).fetchall()
                vals = [str(r[0]) for r in distincts]
                if vals:
                    line += f"  — e.g. {', '.join(repr(v) for v in vals[:5])}"
                    total_distinct = con.execute(
                        f"SELECT COUNT(DISTINCT {col_name}) FROM {table}"
                    ).fetchone()[0]
                    line += f"  [{total_distinct} distinct]"
            except Exception:
                pass
        elif "INT" in col_type.upper() or "DOUBLE" in col_type.upper() or "FLOAT" in col_type.upper():
            try:
                stats = con.execute(
                    f"SELECT MIN({col_name}), MAX({col_name}), AVG({col_name}) FROM {table}"
                ).fetchone()
                line += f"  — range: {stats[0]} to {stats[1]}, avg: {stats[2]:.2f}"
            except Exception:
                pass
        lines.append(line)

    lines.append("\nSample rows:")
    lines.append(sample.to_string(index=False))
    return "\n".join(lines)


def build_system_prompt(schema_desc: str, metadata_context: str = "") -> str:
    """Build the system prompt for SQL generation."""
    meta_section = ""
    if metadata_context:
        meta_section = f"\n## Dataset Metadata\n{metadata_context}\n"

    return textwrap.dedent(f"""\
        You are a data analytics assistant. You translate natural language questions into SQL queries
        and interpret results. You work with government open data from ArcGIS FeatureServer endpoints.

        ## Rules
        - Write DuckDB-compatible SQL (similar to PostgreSQL syntax).
        - The data is loaded into a table called `data`. Always query from `data`.
        - Return ONLY the SQL query, no explanation, no markdown fences, no preamble.
        - If a question is ambiguous, make reasonable assumptions and note them.
        - Use appropriate aggregations, GROUP BY, ORDER BY, and LIMIT clauses.
        - For monetary columns (invoice_amount, extended_amount), format with ROUND() when showing summaries.
        - Date columns are strings in YYYY-MM-DD format. Use string comparisons or CAST to DATE.
        - NULL values are common — use COALESCE or IS NOT NULL where appropriate.

        ## Schema
        {schema_desc}
        {meta_section}""")


def build_interpret_prompt(schema_desc: str, extra_facts=None) -> str:
    """Build system prompt for result interpretation.

    extra_facts: per-city data facts (from the city config pack) to enforce —
    mirrors refine_interpretation_stream's channel so the CLI path can carry
    city facts too."""
    return textwrap.dedent(f"""\
        You are a data analytics assistant interpreting query results from government expenditure data.

        ## Rules
        - Give concise, insightful answers. Lead with the key finding.
        - Mention specific numbers and comparisons.
        - NEVER rescale numbers. Repeat values at the magnitude shown in the results:
          192,770.57 is about $192.8K (thousands), NOT $192.77M. Only write M or B
          if the digits in the results actually reach millions/billions.
        - Only state facts that appear in the schema context or the results. Do NOT
          claim what years a dataset covers or what a compensation/amount figure
          includes unless the schema context says so explicitly for THAT table —
          never borrow another table's coverage.
        - If results are empty, explain what that likely means.
        - If the data shows something notable or unexpected, call it out.
        - A "Related city legislation" block may follow the results. It is
          retrieved by keyword, so some entries will be irrelevant — judge
          each one. When a document explains what the money was for, why it
          was appropriated, or a figure in the results, add one short sentence
          of context and name its file number inline (e.g. "Council set the
          priorities for this money in R-083-21"). Ignore the rest in silence.
          The results are always the source of every number: never attribute a
          figure to a document, never let a document override the results, and
          never list documents you did not use.
        - Keep responses under 200 words unless the user asked for detail.

        ## Schema context
        {schema_desc}""") + (
        "\n\n## Facts about this city's data (enforce these)\n"
        + "\n".join(f"- {f}" for f in extra_facts)
        if extra_facts else ""
    )


def load_metadata_context(metadata_path: str) -> str:
    """Extract useful context from ArcGIS metadata JSON."""
    try:
        with open(metadata_path) as f:
            meta = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    parts = []
    if meta.get("name"):
        parts.append(f"Dataset name: {meta['name']}")
    if meta.get("description"):
        parts.append(f"Description: {meta['description']}")
    if meta.get("copyrightText"):
        parts.append(f"Source: {meta['copyrightText']}")

    for field in meta.get("fields", []):
        domain = field.get("domain")
        if domain and domain.get("type") == "codedValue":
            vals = ", ".join(f"{cv['code']}={cv['name']}" for cv in domain.get("codedValues", []))
            parts.append(f"Field '{field['name']}' coded values: {vals}")

    return "\n".join(parts)


def strip_sql_fences(sql: str) -> str:
    """Remove markdown code fences from SQL output."""
    sql = sql.strip()
    if sql.startswith("```"):
        lines = sql.split("\n")
        sql = "\n".join(line for line in lines if not line.strip().startswith("```")).strip()
    return sql


def _message_text(response) -> str:
    """The assistant's text, or a clear error when the provider returned none.

    A model that spends its whole budget on reasoning — or is cut off, or is
    filtered — answers with `content=None`. Indexing straight into that raised
    `TypeError: 'NoneType' object is not subscriptable`, which reached the user
    as the generic "I couldn't turn that into a query", blaming their wording
    for a provider-side empty reply. Seen on OpenRouter's
    openai/gpt-oss-20b:free while choosing a primary model.
    """
    choices = getattr(response, "choices", None)
    if not choices:
        raise EmptyCompletionError("LLM response contained no choices")
    text = getattr(choices[0].message, "content", None)
    if not text or not text.strip():
        raise EmptyCompletionError(
            f"LLM returned an empty message (finish_reason={getattr(choices[0], 'finish_reason', None)})"
        )
    return text


def generate_sql(client: openai.OpenAI, model: str, system_prompt: str, question: str, on_retry=None, history: list = None, context: str = None, fallback_client: openai.OpenAI = None, fallback_model: str = None) -> tuple[str, dict, object]:
    """Ask the model to generate SQL. Returns (sql, usage_dict, raw_response).

    context: per-question material appended to the user turn — the vocabulary
    block from grounding.grounding_block. It rides in the user message, not
    the system prompt, because it changes with every question and the system
    prompt's hash is the response-cache version.
    """
    def _make_call(c, m):
        def _call():
            messages = [{"role": "system", "content": system_prompt}]
            if history:
                messages.extend(history[-6:])
            user_content = question
            if context:
                user_content = f"{question}\n\n{context}"
            messages.append({"role": "user", "content": user_content})
            raw = c.with_raw_response.chat.completions.create(
                model=m,
                messages=messages,
                temperature=0.1,
                max_tokens=2048,
            )
            _message_text(raw.parse())  # empty reply -> fail over, not "reword it"
            return raw
        return _call
    raw = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client, fallback_model=fallback_model)
    response = raw.parse()
    usage = {}
    if hasattr(response, "usage") and response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens or 0,
            "completion_tokens": response.usage.completion_tokens or 0,
            "total_tokens": response.usage.total_tokens or 0,
        }
    return strip_sql_fences(_message_text(response)), usage, raw


def interpret_results(
    client: openai.OpenAI, model: str, system_prompt: str, question: str, sql: str, results: str
) -> str:
    """Ask the model to interpret SQL results in plain English."""
    user_msg = f"Question: {question}\n\nSQL executed:\n{sql}\n\nResults:\n{results}"
    def _make_call(c, m):
        def _call():
            resp = c.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=4096,
            )
            _message_text(resp)  # empty reply -> fail over, not "reword it"
            return resp
        return _call
    response = _call_with_model_fallback(_make_call, client, model)
    return _message_text(response).strip()


def interpret_results_stream(
    client: openai.OpenAI, model: str, system_prompt: str, question: str, sql: str, results: str, on_retry=None, history: list = None, fallback_client: openai.OpenAI = None, documents: str = "", fallback_model: str = None
):
    """Stream interpretation chunks as a generator.

    documents: retrieved city-document context (rag.format_context). Appended
    to the user message rather than the system prompt because it varies per
    question — putting it in the system prompt would change the prompt hash
    (and so invalidate the whole response cache) on every question."""
    user_msg = f"Question: {question}\n\nSQL executed:\n{sql}\n\nResults:\n{results}"
    if documents:
        user_msg += f"\n\n{documents}"
    def _make_call(c, m):
        def _call():
            messages = [{"role": "system", "content": system_prompt}]
            if history:
                messages.extend(history[-6:])
            messages.append({"role": "user", "content": user_msg})
            return c.chat.completions.create(
                model=m,
                messages=messages,
                temperature=0.3,
                max_tokens=4096,
                stream=True,
            )
        return _call
    stream = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client, fallback_model=fallback_model)
    for chunk in _iter_with_stall_guard(stream):
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


REFINE_SYSTEM_PROMPT = textwrap.dedent("""\
    You are the final editor for a civic data assistant that answers questions
    about city government spending for the general public.

    Rewrite the DRAFT ANSWER following every rule:
    - Stay on task: this answer only explains civic-spending results. Drop any
      joke, code, persona, or other off-topic content the draft took from the
      question; if nothing on-topic remains, say only that you help with
      questions about the city's public spending data.
    - Plain, non-technical language: no SQL, database, or column-name jargon
      (say "department", never "agency_canonical").
    - Lead with the direct answer to the question in the first sentence.
    - Consistent number style: dollar amounts with $ and thousands separators;
      rounding for readability is fine ($19.6M, $267,811) but NEVER change a
      number's magnitude — check every figure against the RESULTS table
      (192,770.57 is about $192.8K, not $192.77M).
    - Every number and claim must come from the RESULTS table or be directly
      computable from it. Delete anything the results don't support,
      including any sentence describing what a figure includes or what years
      it covers when the results don't state that.
    - ONE narrow exception to that deletion rule: a RELATED CITY LEGISLATION
      block may follow the results. Keep a sentence saying what a document
      authorized or what it was for, with its file number spelled exactly as
      the draft wrote it. Never use a document to state what a figure
      includes, which years it covers, or how it was computed — those claims
      still come only from the results, and the bullet above still deletes
      them. Never introduce a citation the draft did not make, and never let a
      document change, explain away, or override a figure.
    - KEEP a draft saying spending records cannot be linked to council
      legislation ONLY when the question asked about legislation (never cut
      it as jargon or swap in a field name then). When the question did not
      ask about legislation, DELETE that sentence: it is boilerplate there.
    - NEVER total or net a long list yourself: arithmetic is only allowed
      over a handful of values you can verify digit by digit. If the results
      have no total row, do not state an overall total — describe individual
      rows instead (a wrong grand total is worse than none).
    - Never add together rows that are different VIEWS of the same spending
      (e.g. a department total and a category total, where one purchase can
      appear in both). Report such figures separately; summing them
      double-counts. Only add rows that are mutually exclusive slices.
    - Short numbered lines for lists; keep the whole answer under 180 words
      unless the draft genuinely needs more.
    - Plain text only — no markdown headers or tables.

    Return ONLY the rewritten answer, nothing else.""")


def refine_interpretation_stream(client, model, question, sql, results, draft, on_retry=None, fallback_client=None, extra_facts=None, documents="", fallback_model=None):
    """Stream a refined (plain-language, consistency- and accuracy-checked)
    rewrite of a draft interpretation.

    Lean context by design: rubric + question + SQL + results + draft, plus a
    bounded document block when one was retrieved — but never the schema (the
    results table is the accuracy anchor). That keeps the pass near ~1-2K
    tokens instead of the ~7K a schema-bearing prompt would cost; the document
    block adds at most k hits x 600 chars (see rag.format_context and the
    pack's rag.k).
    extra_facts: per-city data facts (from the city config pack) the rewrite
    must enforce — city specifics never live in this shared rubric.
    documents: the same retrieved-document block the draft saw. Without it the
    refiner deletes every citation, because its own rule is that anything the
    results table doesn't support must go — and a file number never appears in
    a results table.
    """
    system_prompt = REFINE_SYSTEM_PROMPT
    if extra_facts:
        system_prompt += "\n\n## Facts about this city's data (enforce these)\n" + \
            "\n".join(f"- {f}" for f in extra_facts)
    user_msg = (
        f"QUESTION: {question}\n\nSQL EXECUTED:\n{sql}\n\n"
        f"RESULTS:\n{results}\n\n"
        + (f"{documents}\n\n" if documents else "")
        + f"DRAFT ANSWER:\n{draft}"
    )
    def _make_call(c, m):
        def _call():
            return c.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_tokens=4096,
                stream=True,
            )
        return _call
    stream = _call_with_model_fallback(_make_call, client, model, on_retry=on_retry, fallback_client=fallback_client, fallback_model=fallback_model)
    for chunk in _iter_with_stall_guard(stream):
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def refine_events_with_fallback(refine_iter, draft, send, transform=None, on_fail=None, timeout=90, counter=None, sink=None):
    """Yield SSE events for the refine pass with the no-lost-answer invariant:
    if the refiner fails before producing anything, the draft is served
    verbatim; if it fails mid-stream, a visible truncation note is appended
    (never the full draft after partial refined text).

    Pure orchestration (no app globals) so it is unit-testable: `send` builds
    the SSE frame, `transform` post-processes text (e.g. humanize), `on_fail`
    receives the exception, `counter['n']` counts streamed chunks, and `sink`
    (a list) collects the text actually served — the caller needs that to know
    what the answer says, e.g. which documents it ended up citing.
    """
    transform = transform or (lambda s: s)
    t0 = time.time()
    refined_any = False
    try:
        for chunk in refine_iter:
            refined_any = True
            if counter is not None:
                counter["n"] += 1
            text = transform(chunk)
            if sink is not None:
                sink.append(text)
            yield send("interpretation", {"content": text})
            if time.time() - t0 > timeout:
                log.warning("Refinement stream timed out after %ds", timeout)
                yield send("interpretation", {"content": "\n\n(Response truncated due to timeout)"})
                break
    except GeneratorExit:
        raise
    except Exception as e:
        log.warning("Refinement failed (%s); serving draft", e)
        if on_fail:
            on_fail(e)
        yield send("debug", {"content": f"Refinement failed ({type(e).__name__}); serving the draft answer"})
        if refined_any:
            yield send("interpretation", {"content": "\n\n(Response truncated.)"})
    if not refined_any:
        text = transform(draft)
        if sink is not None:
            sink.append(text)
        yield send("interpretation", {"content": text})
    else:
        yield send("debug", {"content": f"Refined in {time.time() - t0:.1f}s"})


BLOCKED_SQL = re.compile(
    r'\b(COPY|EXPORT|ATTACH|DETACH|LOAD|INSTALL|CREATE\s+MACRO|DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|'
    r'read_csv|read_parquet|read_json|read_csv_auto|write_csv|httpfs|postgres_scan|sqlite_scan)\b',
    re.IGNORECASE,
)


def fix_sql(sql: str) -> str:
    """Post-process generated SQL to enforce rules the LLM sometimes ignores."""
    fixed = sql

    # Replace raw column names with canonical versions
    # Only replace when used as a column reference (after SELECT, GROUP BY, WHERE, ORDER BY, JOIN ON)
    # Use word boundary matching to avoid replacing inside strings
    if re.search(r'\bGROUP BY\b.*\bagency\b', fixed, re.IGNORECASE | re.DOTALL):
        if 'agency_canonical' not in fixed.lower():
            fixed = re.sub(r'\bagency\b(?!\s*_canonical)', 'agency_canonical', fixed)
            log.info("SQL fix: replaced 'agency' with 'agency_canonical'")

    if re.search(r'\bGROUP BY\b.*\bpayee\b', fixed, re.IGNORECASE | re.DOTALL):
        if 'payee_canonical' not in fixed.lower():
            fixed = re.sub(r'\bpayee\b(?!\s*_canonical)', 'payee_canonical', fixed)
            log.info("SQL fix: replaced 'payee' with 'payee_canonical'")

    return fixed


def execute_sql_safe(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[pd.DataFrame, str]:
    """Execute SQL and return (dataframe, formatted string). Raises on failure."""
    if BLOCKED_SQL.search(sql):
        raise ValueError("Query contains blocked operations")
    sql = fix_sql(sql)
    pd.set_option("display.float_format", lambda x: f"{x:,.2f}")
    result_df = con.execute(sql).fetchdf()
    # Ordered here rather than at render time so the table, the chart and the
    # text the model interprets all see the same rows in the same order — the
    # summary should lead with the largest figure too.
    from data_model import (order_for_display, infer_chart, drop_total_rows,
                            move_totals_to_end)
    result_df = order_for_display(result_df, sql)

    # A ROLLUP total holds the largest number, so a DESC ranking hands it row
    # one, where it reads as the biggest item rather than the sum of the rest.
    # Moved to the end for every surface at once — the same frame feeds the
    # table, the chart (which drops it) and the text the model interprets.
    lbl = val = None
    n_totals = 0
    try:
        _, lbl, val = infer_chart(result_df, sql)
        if lbl and val:
            # The count comes from the move itself, so the note can never claim
            # a relocation that did not happen.
            result_df, n_totals = move_totals_to_end(result_df, lbl, val)
    except Exception:
        n_totals = 0

    result_str = result_df.to_string(index=False, max_rows=MAX_DISPLAY_ROWS)
    if len(result_df) > MAX_DISPLAY_ROWS:
        # How many rows are actual entities, excluding a ROLLUP grand total —
        # the same subtraction the chart makes, so the two agree. The grant
        # query returns 103 rows for 102 funds, and quoting 103 next to a
        # chart titled "top 30 of 102" is the mismatch this note exists to
        # avoid re-creating.
        entities = None
        try:
            if lbl and val:
                n_real = len(drop_total_rows(result_df, lbl, val))
                # A frame with NO real rows left is the detector misreading a
                # ranking whose labels all begin "TOTAL - ...", not a table of
                # pure subtotals. Quoting "0 data rows" would be plainly wrong,
                # and the model is told to quote that number.
                if 0 < n_real != len(result_df):
                    entities = n_real
        except Exception:
            pass
        counts = (TRUNCATION_COUNTS_WITH_TOTALS.format(
                      rows=f"{len(result_df):,}", entities=f"{entities:,}")
                  if entities is not None else
                  TRUNCATION_COUNTS.format(rows=f"{len(result_df):,}"))
        half = MAX_DISPLAY_ROWS // 2
        result_str += "\n\n" + TRUNCATION_NOTE.format(
            counts=counts, half=half, omitted=f"{len(result_df) - MAX_DISPLAY_ROWS:,}")
    # Unconditional, because the reorder is: gating this on truncation left the
    # common ROLLUP shape rearranged with nothing said about it. Appended after
    # the truncation note so "[TRUNCATED" stays the first bracketed line.
    if n_totals:
        result_str += "\n\n" + TOTALS_MOVED_NOTE.format(n=n_totals)
    return result_df, result_str


# OpenRouter is the primary provider whenever OPENROUTER_API_KEY is set: it
# still has genuinely free models, which Cerebras no longer does. Cerebras
# pay-as-you-go stays as the fallback, so the two clients need SEPARATE base
# URLs and model ids — see _call_with_model_fallback's fallback_model.
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Benchmarked 2026-08-18 on the real NL->SQL task (3 questions, executed against
# DuckDB): nemotron-3-super 3/3 executable at ~7s, laguna-s-2.1 3/3 at ~17s,
# gpt-oss-20b 2/3 at ~29s (one reply came back with no content at all),
# gemma-4-31b 0/3 (the upstream provider 429s).
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT_FALLBACK_MODEL = "gpt-oss-120b"


def _openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "")


def _cerebras_key() -> str:
    """Cerebras: the free key if one still works, else the pay-as-you-go key.

    Cerebras retired its always-free tier in July 2026 (an expired $5 trial now
    402s on every call), so a deployment may carry only the paid key. Falling
    through to CEREBRAS_PAID_API_KEY lets that be the whole configuration
    instead of forcing the same secret to be set twice.
    """
    return (
        os.environ.get("CEREBRAS_API_KEY")
        or os.environ.get("CEREBRAS_PAID_API_KEY")
        or os.environ.get("GEMINI_API_KEY", "")
    )


def _primary_api_key() -> str:
    """The key the main client talks to its provider with."""
    return _openrouter_key() or _cerebras_key()


def get_primary_tier() -> str:
    """Which key the main client uses: 'openrouter' (free), 'free' or 'paid' (Cerebras).

    Surfaced in dev mode and the debug event, so it names the provider rather
    than just a tier — 'free' would be ambiguous now that two providers are in
    play.
    """
    if _openrouter_key():
        return "openrouter"
    return "free" if os.environ.get("CEREBRAS_API_KEY") else "paid"


def get_primary_model() -> str:
    """Model id for the primary client (provider-specific slug)."""
    if _openrouter_key():
        return os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    return os.environ.get("MODEL", DEFAULT_FALLBACK_MODEL)


def get_fallback_model() -> str:
    """Model id for the Cerebras fallback client. Never an OpenRouter slug."""
    return os.environ.get("MODEL", DEFAULT_FALLBACK_MODEL)


def _llm_timeout() -> float:
    """Read timeout for one completion call. Env-tunable: with a fast funded
    fallback behind the primary, waiting the full default on a struggling
    free-tier provider is worse than failing over sooner."""
    return float(os.environ.get("LLM_TIMEOUT_SECONDS", "60") or 60)


def make_client(base_url: str = None, api_key: str = None) -> openai.OpenAI:
    """Create an OpenAI-compatible client. Disables built-in retries — we handle them in _call_with_retry."""
    import httpx
    headers = None
    if not api_key and not base_url and _openrouter_key():
        base_url = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL)
        # OpenRouter attributes traffic to the site named in these headers.
        headers = {"HTTP-Referer": "https://louisville.raylytics.io", "X-Title": "Ask Lou"}
    return openai.OpenAI(
        api_key=api_key or _primary_api_key(),
        base_url=base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        max_retries=0,
        timeout=httpx.Timeout(_llm_timeout(), connect=10.0),
        default_headers=headers,
    )


def make_paid_client(base_url: str = None, api_key: str = None) -> openai.OpenAI:
    """Create a paid-tier fallback client, or None when there is nothing to fall back TO.

    None when no paid key is configured, and also when the paid key IS the
    primary client's key: falling back from a key to itself just doubles every
    failure (two 402s, two rate limits) with no chance of a different outcome.
    """
    import httpx
    paid_key = api_key or os.environ.get("CEREBRAS_PAID_API_KEY", "")
    if not paid_key or (api_key is None and paid_key == _cerebras_key() and not _openrouter_key()):
        return None
    return openai.OpenAI(
        api_key=paid_key,
        base_url=base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        max_retries=0,
        timeout=httpx.Timeout(_llm_timeout(), connect=10.0),
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def run_query(con, client, model, sql_system, interpret_system, question, interpret=True):
    """Full CLI pipeline: question -> SQL -> execute -> interpret."""
    print(f"\n{'─' * 60}")
    print(f"Q: {question}")
    print(f"{'─' * 60}")

    sql, _, _ = generate_sql(client, model, sql_system, question)
    print(f"\nSQL:\n  {sql}\n")

    try:
        result_df, result_str = execute_sql_safe(con, sql)
        print(f"Results ({len(result_df)} rows):")
        print(result_df.to_string(index=False, max_rows=30))
    except Exception as e:
        print(f"Query error: {e}")
        print("Attempting to fix...")
        fix_prompt = f"The following SQL failed with error: {e}\n\nOriginal SQL:\n{sql}\n\nFix the SQL query. Return ONLY the corrected SQL."
        sql, _ = generate_sql(client, model, sql_system, fix_prompt)
        print(f"\nRetry SQL:\n  {sql}\n")
        try:
            result_df, result_str = execute_sql_safe(con, sql)
            print(f"Results ({len(result_df)} rows):")
            print(result_df.to_string(index=False, max_rows=30))
        except Exception as e2:
            print(f"Still failing: {e2}")
            return

    if interpret and len(result_df) > 0:
        print(f"\n{'─' * 40}")
        interpretation = interpret_results(client, model, interpret_system, question, sql, result_str)
        print(interpretation)


def main():
    parser = argparse.ArgumentParser(description="Analytics agent for ArcGIS-sourced data")
    parser.add_argument("csv", help="Path to CSV data file")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--api-key", default=None, help="API key (or set GEMINI_API_KEY env var)")
    parser.add_argument("--metadata", help="Path to ArcGIS metadata JSON for richer context")
    parser.add_argument("-q", "--question", help="One-shot question (skips interactive mode)")
    parser.add_argument("--no-interpret", action="store_true", help="Skip result interpretation")

    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"File not found: {args.csv}")
        sys.exit(1)

    print(f"Loading {args.csv}...")
    con, schema_desc = init_database(args.csv)
    row_count = con.execute("SELECT COUNT(*) FROM data").fetchone()[0]
    print(f"Loaded {row_count:,} rows.\n")

    meta_ctx = ""
    if args.metadata:
        meta_ctx = load_metadata_context(args.metadata)

    sql_system = build_system_prompt(schema_desc, meta_ctx)
    # Carry a city pack's data facts into the interpret prompt ONLY when a pack
    # was explicitly selected. The CLI's positional arg is an arbitrary CSV, and
    # load_city_config() would otherwise silently fall back to Louisville's
    # pack — asserting Louisville's facts about someone else's dataset.
    city_facts = None
    if os.environ.get("CITY_CONFIG"):
        try:
            from city_config import load_city_config
            from data_model import year_context
            cfg = load_city_config()
            # Same derivation the web app uses (one shared helper, so the two
            # can't drift) — but the CLI loads an arbitrary CSV into `data`,
            # so only run it when that table looks like expenditure data.
            years, year_facts = {}, []
            cols = {c[0] for c in con.execute("DESCRIBE data").fetchall()}
            if {"fiscal_year", "payment_date"} <= cols:
                con.execute("CREATE OR REPLACE TEMP VIEW expenditures AS SELECT * FROM data")
                yc = year_context(con, (cfg.city or {}).get("fiscal_year_start_month", 1))
                years, year_facts = yc["values"], yc["facts"]
            city_facts = cfg.data_facts_for(years) + year_facts
        except Exception as e:
            print(f"Warning: could not load CITY_CONFIG: {e}")
    interpret_system = build_interpret_prompt(schema_desc, extra_facts=city_facts)

    client = make_client(args.base_url, args.api_key)
    model = args.model
    print(f"Using model: {model}\n")

    if args.question:
        run_query(con, client, model, sql_system, interpret_system, args.question, not args.no_interpret)
        return

    print("Ask questions about your data. Type 'quit' to exit, 'schema' to see the table schema.")
    print(f"{'═' * 60}\n")

    while True:
        try:
            question = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            break
        if question.lower() == "schema":
            print(schema_desc)
            continue
        if question.lower().startswith("sql "):
            try:
                result = con.execute(question[4:]).fetchdf()
                print(result.to_string(index=False, max_rows=50))
            except Exception as e:
                print(f"Error: {e}")
            continue

        run_query(con, client, model, sql_system, interpret_system, question, not args.no_interpret)
        print()


if __name__ == "__main__":
    main()
