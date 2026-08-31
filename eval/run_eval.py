#!/usr/bin/env python3
"""End-to-end accuracy eval: real model, real data, the real request path.

Drives POST /api/ask in-process (FastAPI TestClient — the same router,
prompts, grounding, repair loop and refinement the site serves) for every
question in eval/golden.yaml, then scores what came back:

  sql     the SERVED query, re-executed locally, passes the case's check
          against a reference query computed on the same data
  answer  the prose the reader saw states the reference number and avoids the
          case's forbidden phrases

Cached answers are bypassed (the cache is emptied first and dev_mode keeps
new ones from being written), and the run's stats/cache files go to a temp
dir so it never touches a real deployment's state.

Usage:
    set -a; source .env; set +a          # the provider keys
    python eval/run_eval.py                       # whichever provider .env selects
    python eval/run_eval.py --provider cerebras   # spare OpenRouter's 50/day free cap
    python eval/run_eval.py --only fire-vehicles-fy2024 --label after-grounding
    PREBUILT_DB=data/lou.duckdb python eval/run_eval.py   # faster boot

Each run writes eval/results/<timestamp>-<label>.md (a scorecard) and .json
(per-case detail, for diffing two runs). Cost: ~3 LLM calls per question.
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
import time

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _select_provider(provider: str) -> None:
    """Leave the environment as the operator set it, or force one provider.

    'cerebras' drops the OpenRouter key so the bot's own provider selection
    picks the Cerebras pay-as-you-go key — an eval of 20+ questions would
    otherwise spend most of OpenRouter's 50-request free daily allowance,
    which production shares."""
    if provider == "cerebras":
        for k in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "CEREBRAS_API_KEY"):
            os.environ.pop(k, None)
        if not os.environ.get("CEREBRAS_PAID_API_KEY"):
            sys.exit("--provider cerebras needs CEREBRAS_PAID_API_KEY in the environment")
    elif provider == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("--provider openrouter needs OPENROUTER_API_KEY in the environment")


def _parse_sse(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def _numeric_cells(df) -> list:
    vals = []
    for col in df.columns:
        for v in df[col].tolist():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                vals.append(float(v))
    return vals


def _text_cells(df) -> list:
    return [str(v) for col in df.columns for v in df[col].tolist() if isinstance(v, str)]


def _first_text_column(df):
    for col in df.columns:
        if df[col].dtype == object:
            return [str(v) for v in df[col].tolist()]
    return []


def _close(a: float, b: float, tol: float) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= tol


def _renderings(v: float) -> set:
    """The ways an answer might legitimately print a number."""
    out = set()
    a = abs(v)
    out.add(f"{a:,.0f}")
    out.add(f"{a:,.2f}")
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= scale * 0.95:
            x = a / scale
            for fmt in ("{:.0f}", "{:.1f}", "{:.2f}", "{:.3f}"):
                s = fmt.format(x)
                out.add(s + suffix)
                out.add(s + " " + suffix)
                out.add(s + " " + {"B": "billion", "M": "million", "K": "thousand"}[suffix])
    return out


def _answer_states(v: float, text: str) -> bool:
    t = text.replace(" ", " ").replace(" ", " ").replace("‑", "-")
    # "$1.58 M" and "$1.58M" both count; so does "1.6M" when the value is 1.58M.
    compact = re.sub(r"(?<=\d)\s+(?=[BMK]\b)", "", t)
    for r in _renderings(v):
        if r in t or r in compact:
            return True
    # One more: rounding to the nearest 0.1 of the scale unit ("$1.6M").
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= scale * 0.95:
            if f"{round(abs(v) / scale, 1)}{suffix}" in compact:
                return True
    return False


def _check_sql(case: dict, served_df, reference_rows: list, events: list) -> tuple:
    kind = case["check"]
    tol = float(case.get("tolerance", 0.01))
    has_error = any(e["type"] == "error" for e in events)
    ran_sql = any(e["type"] == "sql" for e in events)
    if kind == "expect_refusal":
        ok = not ran_sql and not has_error
        return ok, "no SQL executed" if ok else "SQL was executed for an off-topic question"
    if has_error:
        err = next(e["content"] for e in events if e["type"] == "error")
        return False, f"error event: {err[:120]}"
    if served_df is None:
        return False, "no SQL served"
    vacuous = len(served_df) == 0 or bool(served_df.isna().all().all())
    if kind == "expect_empty":
        return vacuous, "empty as expected" if vacuous else f"{len(served_df)} rows where none were expected"
    if vacuous:
        return False, "served query returned nothing"
    if kind == "scalar":
        ref = float(reference_rows[0][0])
        cells = _numeric_cells(served_df)
        hit = next((c for c in cells if _close(c, ref, tol)), None)
        if case.get("min_rows") and len(served_df) < int(case["min_rows"]):
            return False, f"only {len(served_df)} rows (min {case['min_rows']})"
        if hit is None:
            near = min(cells, key=lambda c: abs(c - ref)) if cells else None
            return False, f"reference {ref:,.2f} not in served cells (closest {near:,.2f})" if near is not None else f"reference {ref:,.2f}; served has no numeric cells"
        return True, f"{hit:,.2f} ≈ {ref:,.2f}"
    if kind == "top_labels":
        want = [str(r[0]) for r in reference_rows]
        got = _first_text_column(served_df)[:len(want)]
        if case.get("ordered", True):
            ok = got == want
        else:
            ok = set(got) == set(want)
        return ok, f"served {got}" if not ok else f"top {len(want)} match"
    if kind == "contains":
        cells = " | ".join(_text_cells(served_df))
        missing = [s for s in case["contains"] if s.lower() not in cells.lower()]
        return not missing, f"missing {missing}" if missing else "all present"
    if kind == "min_rows":
        n = len(served_df.dropna(how="all"))
        ok = n >= int(case["min_rows"])
        return ok, f"{n} rows"
    return False, f"unknown check {kind}"


def _check_answer(case: dict, reference_rows: list, answer: str, sql_ok: bool) -> tuple:
    kind = case["check"]
    if kind in ("expect_refusal", "expect_empty"):
        # The prose is the whole product here: it must not fabricate a figure.
        bad = re.search(r"\$\s?\d", answer)
        return not bad, "no fabricated figure" if not bad else "answer quotes a dollar figure"
    problems = []
    for phrase in case.get("answer_must_not_contain", []) or []:
        if phrase.lower() in answer.lower():
            problems.append(f"contains {phrase!r}")
    if kind == "scalar" and case.get("answer_scalar", True):
        ref = float(reference_rows[0][0])
        if not _answer_states(ref, answer):
            problems.append(f"does not state {ref:,.2f}")
    if kind == "contains" and case.get("answer_scalar", True) and reference_rows and \
            isinstance(reference_rows[0][0], (int, float)):
        ref = float(reference_rows[0][0])
        if not _answer_states(ref, answer):
            problems.append(f"does not state {ref:,.2f}")
    elif kind == "contains" and not case.get("answer_scalar", True):
        for s in case["contains"]:
            if s.lower().split(",")[0] not in answer.lower():
                problems.append(f"does not mention {s!r}")
    if kind == "top_labels" and sql_ok:
        top = str(reference_rows[0][0])
        if top.lower() not in answer.lower():
            problems.append(f"does not name the top entry {top!r}")
    return not problems, "; ".join(problems) if problems else "ok"


def run(cases: list, label: str, provider: str, out_dir: str) -> dict:
    _select_provider(provider)
    # Never touch a real deployment's state files, and bypass the response
    # cache so every question exercises the live path.
    tmp = tempfile.mkdtemp(prefix="lou-eval-")
    os.environ["STATS_DIR"] = tmp
    os.environ.setdefault("LOG_DIR", os.path.join(tmp, "logs"))
    os.environ.setdefault("INTER_CALL_PAUSE_SECONDS", "0")

    from fastapi.testclient import TestClient
    import app
    from analytics_agent import execute_sql_safe, get_active_model, get_primary_tier

    app.IP_RPM_LIMIT = 10_000  # the per-IP limit would stop the run after 5 questions
    results = []
    t_run = time.time()
    with TestClient(app.app) as client:
        app.response_cache.clear()
        model = get_active_model(app.MODEL)
        print(f"model: {model} ({get_primary_tier()}); {len(cases)} cases\n")
        for case in cases:
            t0 = time.time()
            resp = client.post("/api/ask", json={"question": case["question"], "dev_mode": True})
            elapsed = time.time() - t0
            events = _parse_sse(resp.text)
            sqls = [e["content"] for e in events if e["type"] == "sql"]
            answer = "".join(e.get("content", "") for e in events if e["type"] == "interpretation")
            logs = [e["content"] for e in events if e["type"] == "log"]
            repaired = any("Repaired query returned" in l for l in logs)
            repair_tried = any("Checking its filters" in l for l in logs)
            served_df = None
            exec_err = None
            if sqls:
                try:
                    with app.db_lock:
                        served_df, _ = execute_sql_safe(app.con, sqls[-1])
                except Exception as e:
                    exec_err = str(e)[:200]
            # reference_sql may be a list: any reference that passes counts (a
            # question with two defensible readings, e.g. "CARES money" as the
            # Treasury CRF alone or every CARES-labelled fund).
            refs = case["reference_sql"] if isinstance(case["reference_sql"], list) else [case["reference_sql"]]
            sql_ok, ans_ok, sql_note, ans_note = False, False, "", ""
            for ref_sql in refs:
                with app.db_lock:
                    reference_rows = app.con.execute(ref_sql).fetchall()
                s_ok, s_note = _check_sql(case, served_df, reference_rows, events)
                if exec_err:
                    s_ok, s_note = False, f"served SQL failed locally: {exec_err}"
                a_ok, a_note = _check_answer(case, reference_rows, answer, s_ok)
                if (s_ok and a_ok) or not sql_note:
                    sql_ok, ans_ok, sql_note, ans_note = s_ok, a_ok, s_note, a_note
                if s_ok and a_ok:
                    break
            r = {
                "id": case["id"], "tags": case.get("tags", []), "question": case["question"],
                "sql_ok": sql_ok, "sql_note": sql_note, "answer_ok": ans_ok, "answer_note": ans_note,
                "seconds": round(elapsed, 1), "repair_tried": repair_tried, "repaired": repaired,
                "served_sql": sqls[-1] if sqls else None, "first_sql": sqls[0] if sqls else None,
                "answer": answer.strip(),
            }
            results.append(r)
            flag = "PASS" if sql_ok and ans_ok else ("SQL-ONLY" if sql_ok else "FAIL")
            fix = " (repaired)" if repaired else (" (repair tried)" if repair_tried else "")
            print(f"[{flag:8s}] {case['id']:28s} {elapsed:5.1f}s{fix}\n"
                  f"           sql: {sql_note}\n           answer: {ans_note}")
    summary = {
        "label": label, "model": model, "provider": get_primary_tier(),
        "when": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": len(results),
        "sql_pass": sum(r["sql_ok"] for r in results),
        "answer_pass": sum(r["answer_ok"] for r in results),
        "both_pass": sum(r["sql_ok"] and r["answer_ok"] for r in results),
        "repairs": sum(r["repaired"] for r in results),
        "repair_tries": sum(r["repair_tried"] for r in results),
        "mean_seconds": round(sum(r["seconds"] for r in results) / max(1, len(results)), 1),
        "total_seconds": round(time.time() - t_run, 1),
        "results": results,
    }
    _write(summary, out_dir)
    print(f"\n{summary['both_pass']}/{summary['cases']} pass "
          f"(sql {summary['sql_pass']}, answer {summary['answer_pass']}); "
          f"{summary['repairs']} repaired of {summary['repair_tries']} tried; "
          f"mean {summary['mean_seconds']}s/question; model {model}")
    return summary


def _write(summary: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_dir, f"{stamp}-{summary['label']}")
    with open(base + ".json", "w") as f:
        json.dump(summary, f, indent=1)
    lines = [
        f"# Eval: {summary['label']}",
        "",
        f"- when: {summary['when']}",
        f"- model: `{summary['model']}` ({summary['provider']})",
        f"- **{summary['both_pass']}/{summary['cases']} pass** (sql {summary['sql_pass']}, answer {summary['answer_pass']})",
        f"- repairs: {summary['repairs']} succeeded of {summary['repair_tries']} attempted",
        f"- latency: mean {summary['mean_seconds']}s per question",
        "",
        "| case | tags | sql | answer | s | note |",
        "|---|---|---|---|---:|---|",
    ]
    for r in summary["results"]:
        note = r["sql_note"] if r["sql_ok"] else f"SQL: {r['sql_note']}"
        if not r["answer_ok"]:
            note += f" · answer: {r['answer_note']}"
        if r["repaired"]:
            note += " · repaired"
        lines.append(f"| {r['id']} | {', '.join(r['tags'])} | {'✅' if r['sql_ok'] else '❌'} | "
                     f"{'✅' if r['answer_ok'] else '❌'} | {r['seconds']} | {note.replace('|', '/')} |")
    lines += ["", "## Answers", ""]
    for r in summary["results"]:
        lines += [f"### {r['id']}", "", f"> {r['question']}", "",
                  "```sql", r["served_sql"] or "(none)", "```", "", r["answer"] or "(no answer)", ""]
    with open(base + ".md", "w") as f:
        f.write("\n".join(lines))
    print(f"\nwrote {base}.md")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", default=os.path.join(REPO, "eval", "golden.yaml"))
    ap.add_argument("--only", action="append", help="case id (repeatable)")
    ap.add_argument("--tag", action="append", help="only cases with this tag (repeatable)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--provider", choices=["env", "cerebras", "openrouter"], default="env")
    ap.add_argument("--out", default=os.path.join(REPO, "eval", "results"))
    args = ap.parse_args()
    with open(args.golden) as f:
        cases = yaml.safe_load(f)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    if args.tag:
        cases = [c for c in cases if set(c.get("tags", [])) & set(args.tag)]
    if not cases:
        sys.exit("no cases selected")
    run(cases, args.label, args.provider, args.out)


if __name__ == "__main__":
    main()
