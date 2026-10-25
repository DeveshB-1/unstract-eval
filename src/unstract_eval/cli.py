"""unstract-eval: run a golden set through an Unstract API deployment and score it.

    unstract-eval run --docs DIR --golden DIR [--profile ID ...] [--config eval.json]
                      [--baseline prev.json] [--min-accuracy 0.95] [--max-drop 0.02]
                      [--json out.json] [--markdown out.md] [--mock fixture.json]

Credentials come from --api-url/--api-key or UNSTRACT_API_URL/UNSTRACT_API_KEY.
Exit status: 0 pass, 1 gate failed, 2 usage or API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .client import DeploymentClient, UnstractError
from .report import gate, summarize, to_markdown
from .score import DocScore, Rules, score_document

DOC_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt", ".docx", ".doc", ".xlsx", ".csv"}


def collect(docs_dir: Path, golden_dir: Path) -> list[tuple[Path, dict]]:
    pairs = []
    for doc in sorted(p for p in docs_dir.iterdir() if p.suffix.lower() in DOC_SUFFIXES):
        golden = golden_dir / f"{doc.stem}.json"
        if not golden.exists():
            print(f"skip {doc.name}: no golden file {golden.name}", file=sys.stderr)
            continue
        pairs.append((doc, json.loads(golden.read_text())))
    return pairs


def run_profile(client: DeploymentClient, pairs, profile: str | None, rules: Rules) -> list[DocScore]:
    scores = []
    for doc, expected in pairs:
        try:
            ex = client.extract(doc, llm_profile_id=profile)
        except UnstractError as e:
            scores.append(DocScore(doc=doc.name, results=[], error=str(e)))
            continue
        err = ex.error if ex.output is None else None
        scores.append(score_document(doc.name, expected, ex.output, rules,
                                     error=err, cost=ex.cost, seconds=ex.seconds))
    return scores


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="unstract-eval", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="extract every document and score it against its golden JSON")
    r.add_argument("--docs", type=Path, required=True)
    r.add_argument("--golden", type=Path, required=True)
    r.add_argument("--config", type=Path, help="eval.json with per-field rules")
    r.add_argument("--profile", action="append", default=[],
                   help="llm_profile_id to evaluate; repeat to compare profiles (default: deployment's own)")
    r.add_argument("--api-url", default=os.environ.get("UNSTRACT_API_URL"))
    r.add_argument("--api-key", default=os.environ.get("UNSTRACT_API_KEY"))
    r.add_argument("--baseline", type=Path, help="JSON from an earlier run to diff against")
    r.add_argument("--min-accuracy", type=float)
    r.add_argument("--max-drop", type=float, default=0.02,
                   help="allowed accuracy drop vs baseline, overall and per field (default 0.02)")
    r.add_argument("--json", dest="json_out", type=Path)
    r.add_argument("--markdown", type=Path)
    r.add_argument("--mock", type=Path, help="serve outputs from a fixture instead of a live deployment")
    r.add_argument("--poll-interval", type=float, default=2.0)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rules = Rules.from_dict(json.loads(args.config.read_text()) if args.config else None)
    pairs = collect(args.docs, args.golden)
    if not pairs:
        print("no documents with golden files found", file=sys.stderr)
        return 2

    if args.mock:
        from .mock import MockUnstract
        mock = MockUnstract.from_file(args.mock)
        client = DeploymentClient("https://mock.unstract.local/deployment/api/demo-org/invoices/",
                                  mock.api_key, transport=mock.transport(), sleep=lambda s: None)
    else:
        if not args.api_url or not args.api_key:
            print("set --api-url/--api-key or UNSTRACT_API_URL/UNSTRACT_API_KEY (or use --mock)", file=sys.stderr)
            return 2
        client = DeploymentClient(args.api_url, args.api_key, poll_interval=args.poll_interval)

    profiles = args.profile or [None]
    summary = {
        "tool": f"unstract-eval {__version__}",
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mock": bool(args.mock),
        "documents": len(pairs),
        "profiles": {},
    }
    with client:
        for profile in profiles:
            name = profile or "default"
            print(f"running {len(pairs)} documents with profile {name} ...", file=sys.stderr)
            summary["profiles"][name] = summarize(run_profile(client, pairs, profile, rules))

    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    problems = gate(summary, baseline, args.min_accuracy, args.max_drop)
    summary["gate"] = {"passed": not problems, "problems": problems}
    md = to_markdown(summary, baseline, problems)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2, default=str))
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(md)
    print(md)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
