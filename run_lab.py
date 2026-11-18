#!/usr/bin/env python3
"""Run the agent tool-call reliability lab.

Examples:
  python run_lab.py --provider mock                       # harness smoke test, no API calls
  python run_lab.py --provider bedrock --model us.anthropic.claude-haiku-5-5 --runs 3

Each run writes to results/<timestamp>-<provider>/:
  runs.jsonl    one line per run: config, outcome, answer, transcript, tokens
  spans.jsonl   every span from every run, with status and attributes
  summary.json  aggregated numbers
  summary.md    tables for the README and the write-up
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lab import telemetry
from lab.agent import run_once
from lab.backend import FAULTS, Backend, open_total_usd
from lab.providers import PROVIDERS
from lab.tools import BUILDERS

TASK = {
    "prompt": "What is the total value in US dollars of all open orders for Acme Corp?",
    "expected": open_total_usd("C-100"),
}

DEFAULT_INSTRUMENTATION = {"v1": "standard", "v2": "contract"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=PROVIDERS, default=os.getenv("LAB_PROVIDER", "mock"))
    p.add_argument("--model", default=os.getenv("LAB_MODEL_ID"))
    p.add_argument("--runs", type=int, default=3, help="runs per (tool version, fault) cell")
    p.add_argument("--versions", default="v1,v2")
    p.add_argument("--faults", default="none," + ",".join(FAULTS),
                   help="comma list; 'none' is the no-fault baseline")
    p.add_argument("--instrumentation", choices=["auto", "standard", "contract"], default="auto",
                   help="auto pairs v1 with standard tracing and v2 with contract-aware tracing")
    p.add_argument("--sleep", type=float, default=0.0, help="seconds between runs, for API rate limits")
    p.add_argument("--out", default="results")
    return p.parse_args()


def main():
    args = parse_args()
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    faults = [f.strip() for f in args.faults.split(",") if f.strip()]
    for v in versions:
        if v not in BUILDERS:
            sys.exit(f"unknown version {v}")

    provider_obj, memory, otlp = telemetry.setup()
    provider = PROVIDERS[args.provider](args.model)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) / f"{stamp}-{args.provider}"
    out.mkdir(parents=True, exist_ok=True)
    runs_f = open(out / "runs.jsonl", "w")
    spans_f = open(out / "spans.jsonl", "w")

    print(f"provider={args.provider} model={provider.model} expected=${TASK['expected']:,.2f} "
          f"otlp_export={'on' if otlp else 'off'}")
    print(f"writing to {out}\n")

    records = []
    for version in versions:
        instr = DEFAULT_INSTRUMENTATION[version] if args.instrumentation == "auto" else args.instrumentation
        for fault in faults:
            for i in range(1, args.runs + 1):
                backend = Backend(fault)
                tools = BUILDERS[version](backend)
                provider.reset()
                memory.clear()
                meta = {"tool_version": version, "fault": fault, "run": i, "instrumentation": instr}
                try:
                    result = run_once(provider, tools, TASK, meta, instr)
                except Exception as e:
                    result = {"trace_id": None, "final_text": "", "tokens": {"input": 0, "output": 0},
                              "transcript": [], "outcome": "harness_error", "answer": None,
                              "harness_error": f"{type(e).__name__}: {e}"}
                spans = [telemetry.span_to_dict(s) for s in memory.get_finished_spans()]
                error_spans = [s for s in spans if s["status"] == "ERROR"]
                tool_spans = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "execute_tool"]
                rec = {
                    **meta,
                    "provider": args.provider,
                    "model": provider.model,
                    "expected": TASK["expected"],
                    "outcome": result["outcome"],
                    "answer": result["answer"],
                    "trace_id": result["trace_id"],
                    "error_span_count": len(error_spans),
                    "tool_call_count": len(tool_spans),
                    "silent_failure": result["outcome"] == "wrong" and not error_spans,
                    "backend_calls": dict(backend.calls),
                    "tokens": result["tokens"],
                    "final_text": result["final_text"],
                    "transcript": result["transcript"],
                }
                if "harness_error" in result:
                    rec["harness_error"] = result["harness_error"]
                records.append(rec)
                runs_f.write(json.dumps(rec) + "\n")
                for s in spans:
                    spans_f.write(json.dumps({**meta, **s}) + "\n")
                ans = f"${rec['answer']:,.2f}" if rec["answer"] is not None else "-"
                print(f"{version} {instr:9} {fault:16} run {i}: {rec['outcome']:15} answer={ans:>14} "
                      f"error_spans={rec['error_span_count']} tool_calls={rec['tool_call_count']}"
                      + (f"  [{rec['harness_error']}]" if "harness_error" in rec else ""))
                if args.sleep:
                    time.sleep(args.sleep)

    runs_f.close()
    spans_f.close()
    provider_obj.force_flush()

    summary = summarize(records, versions, faults)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    md = render_markdown(summary, args, provider.model)
    (out / "summary.md").write_text(md)
    print("\n" + md)


def summarize(records, versions, faults):
    cells = defaultdict(list)
    for r in records:
        cells[(r["tool_version"], r["fault"])].append(r)

    table = []
    for v in versions:
        for f in faults:
            rs = cells[(v, f)]
            count = lambda o: sum(1 for r in rs if r["outcome"] == o)
            table.append({
                "tool_version": v,
                "instrumentation": rs[0]["instrumentation"] if rs else None,
                "fault": f,
                "runs": len(rs),
                "correct": count("correct"),
                "wrong": count("wrong"),
                "honest_failure": count("honest_failure"),
                "no_answer": count("no_answer"),
                "harness_error": count("harness_error"),
                "runs_with_error_spans": sum(1 for r in rs if r["error_span_count"] > 0),
                "silent_failures": sum(1 for r in rs if r["silent_failure"]),
            })

    headline = {}
    injected = [f for f in faults if f != "none"]
    for v in versions:
        rows = [t for t in table if t["tool_version"] == v and t["fault"] in injected]
        all_rows = [t for t in table if t["tool_version"] == v]
        headline[v] = {
            "faults_injected": len(rows),
            "faults_never_visible_in_telemetry": sum(1 for t in rows if t["runs_with_error_spans"] == 0),
            "faults_causing_failures": sum(1 for t in rows if t["correct"] < t["runs"]),
            "faults_failing_invisibly": sum(
                1 for t in rows if t["correct"] < t["runs"] and t["runs_with_error_spans"] == 0),
            "fault_runs": sum(t["runs"] for t in rows),
            "fault_runs_correct": sum(t["correct"] for t in rows),
            "fault_runs_silent_failure": sum(t["silent_failures"] for t in rows),
            "all_runs": sum(t["runs"] for t in all_rows),
            "all_runs_correct": sum(t["correct"] for t in all_rows),
        }
    return {"table": table, "headline": headline}


def render_markdown(summary, args, model):
    lines = [
        "# Agent tool-call reliability: results",
        "",
        f"Provider `{args.provider}`, model `{model}`, {args.runs} runs per cell.",
        "",
    ]
    if args.provider == "mock":
        lines += ["> Mock provider: these numbers only show the harness works. They are not findings.", ""]
    lines += ["## Headline", ""]
    for v, h in summary["headline"].items():
        lines.append(
            f"- **{v}**: {h['faults_injected']} faults injected. {h['faults_never_visible_in_telemetry']} never "
            f"produced an ERROR span. {h['faults_causing_failures']} caused at least one incorrect run, and "
            f"{h['faults_failing_invisibly']} of those did so with every span healthy. "
            f"Fault runs correct: {h['fault_runs_correct']} of {h['fault_runs']}. "
            f"Silent failures (wrong answer, all spans OK): {h['fault_runs_silent_failure']}."
        )
    lines += [
        "",
        "## By fault",
        "",
        "| Tools | Tracing | Fault | Correct | Wrong | Honest fail | No answer | Runs with ERROR span | Silent failures |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in summary["table"]:
        lines.append(
            f"| {t['tool_version']} | {t['instrumentation']} | {t['fault']} | {t['correct']}/{t['runs']} | "
            f"{t['wrong']} | {t['honest_failure']} | {t['no_answer'] + t['harness_error']} | "
            f"{t['runs_with_error_spans']}/{t['runs']} | {t['silent_failures']} |"
        )
    lines += [
        "",
        "Silent failure = the agent returned a wrong number and every span in the trace had status OK.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
