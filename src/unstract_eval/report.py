"""Aggregate DocScores into per-profile summaries, a JSON record and Markdown."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .score import DocScore


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[*]", re.sub(r"\[\+\d+\]$", "[extra]", path))


def summarize(docs: list[DocScore]) -> dict[str, Any]:
    scored = sum(d.scored for d in docs)
    correct = sum(d.correct for d in docs)
    strict = sum(d.strict_correct for d in docs)
    per_field: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for d in docs:
        for r in d.results:
            key = _generic(r.path)
            per_field[key][0] += r.correct
            per_field[key][1] += 1
    costs = [d.cost for d in docs if d.cost is not None]
    return {
        "documents": len(docs),
        "errored_documents": sum(1 for d in docs if d.error),
        "fields_scored": scored,
        "fields_correct": correct,
        "accuracy": round(correct / scored, 4) if scored else 0.0,
        "exact_match_accuracy": round(strict / scored, 4) if scored else 0.0,
        "docs_fully_correct": sum(1 for d in docs if d.scored and d.correct == d.scored and not d.error),
        "cost_per_doc": round(sum(costs) / len(costs), 6) if costs else None,
        "per_field": {k: round(c / n, 4) for k, (c, n) in sorted(per_field.items())},
        "failures": [
            {"doc": d.doc, "path": r.path, "status": r.status, "kind": r.kind,
             "expected": r.expected, "actual": r.actual}
            for d in docs for r in d.results if not r.correct
        ] + [{"doc": d.doc, "path": "*", "status": "error", "kind": "-", "expected": None, "actual": d.error}
             for d in docs if d.error],
        "seconds_per_doc": round(sum(d.seconds or 0 for d in docs) / len(docs), 2) if docs else None,
    }


def gate(summary: dict, baseline: dict | None, min_accuracy: float | None, max_drop: float) -> list[str]:
    """Return a list of reasons the run should fail CI; empty means pass."""
    problems = []
    for profile, s in summary["profiles"].items():
        if min_accuracy is not None and s["accuracy"] < min_accuracy:
            problems.append(f"{profile}: accuracy {s['accuracy']:.1%} below --min-accuracy {min_accuracy:.1%}")
        if baseline and profile in baseline.get("profiles", {}):
            old = baseline["profiles"][profile]
            if old["accuracy"] - s["accuracy"] > max_drop:
                problems.append(f"{profile}: accuracy fell {old['accuracy']:.1%} -> {s['accuracy']:.1%}")
            for fld, acc in s["per_field"].items():
                prev = old.get("per_field", {}).get(fld)
                if prev is not None and prev - acc > max_drop:
                    problems.append(f"{profile}: field {fld} fell {prev:.0%} -> {acc:.0%}")
    return problems


def _fmt(v: Any) -> str:
    if v is None:
        return "null"
    s = str(v).replace("|", "/")
    return s if len(s) <= 48 else s[:45] + "..."


def to_markdown(summary: dict, baseline: dict | None = None, problems: list[str] | None = None) -> str:
    profiles = summary["profiles"]
    names = list(profiles)
    lines = [f"# Extraction eval: {summary['documents']} documents", ""]
    lines += ["| profile | field accuracy | exact-match would say | docs fully correct | wrong fields | cost/doc |",
              "|---|---|---|---|---|---|"]
    for n in names:
        s = profiles[n]
        cost = f"${s['cost_per_doc']:.4f}" if s["cost_per_doc"] is not None else "n/a"
        delta = ""
        if baseline and n in baseline.get("profiles", {}):
            d = s["accuracy"] - baseline["profiles"][n]["accuracy"]
            delta = f" ({d:+.1%} vs baseline)"
        lines.append(
            f"| {n} | {s['accuracy']:.1%} ({s['fields_correct']}/{s['fields_scored']}){delta} "
            f"| {s['exact_match_accuracy']:.1%} | {s['docs_fully_correct']}/{s['documents']} "
            f"| {len(s['failures'])} | {cost} |"
        )
    lines += ["", "## Per-field accuracy (worst first)", ""]
    fields = sorted({f for n in names for f in profiles[n]["per_field"]},
                    key=lambda f: min(profiles[n]["per_field"].get(f, 1.0) for n in names))
    lines += ["| field | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for f in fields:
        cells = [f"{profiles[n]['per_field'][f]:.0%}" if f in profiles[n]["per_field"] else "-" for n in names]
        lines.append(f"| `{f}` | " + " | ".join(cells) + " |")
    for n in names:
        fails = profiles[n]["failures"]
        lines += ["", f"## Wrong fields: {n} ({len(fails)})", ""]
        if not fails:
            lines.append("None.")
            continue
        lines += ["| doc | field | status | expected | got |", "|---|---|---|---|---|"]
        for f in fails:
            lines.append(f"| {f['doc']} | `{f['path']}` | {f['status']} | {_fmt(f['expected'])} | {_fmt(f['actual'])} |")
    if problems is not None:
        lines += ["", "## Gate", ""]
        lines += [f"- FAIL: {p}" for p in problems] or ["- PASS"]
    return "\n".join(lines) + "\n"
