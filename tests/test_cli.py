import json
from pathlib import Path

from unstract_eval.cli import main

EX = Path(__file__).resolve().parent.parent / "examples" / "invoices"


def args(*extra):
    return ["run", "--docs", str(EX / "docs"), "--golden", str(EX / "golden"),
            "--config", str(EX / "eval.json"), "--mock", str(EX / "mock_outputs.json"), *extra]


def test_example_run_scores_both_profiles(tmp_path, capsys):
    out = tmp_path / "run.json"
    code = main(args("--profile", "profile-a", "--profile", "profile-b", "--json", str(out)))
    assert code == 0
    data = json.loads(out.read_text())
    a, b = data["profiles"]["profile-a"], data["profiles"]["profile-b"]
    assert b["accuracy"] > a["accuracy"]
    # type-aware scoring forgives format noise that exact match counts as wrong
    assert a["accuracy"] > a["exact_match_accuracy"]
    paths = {f["path"] for f in a["failures"]}
    assert "due_date" in paths and "total" in paths
    assert any(f["status"] == "extra" for f in a["failures"])
    assert {f["path"] for f in b["failures"]} == {"po_number", "vendor_name"}
    assert "# Extraction eval: 5 documents" in capsys.readouterr().out


def test_min_accuracy_gate_fails(tmp_path):
    assert main(args("--profile", "profile-a", "--min-accuracy", "0.99")) == 1


def test_baseline_regression_is_caught(tmp_path):
    base = tmp_path / "base.json"
    assert main(args("--profile", "profile-b", "--json", str(base))) == 0
    data = json.loads(base.read_text())
    # rename so the same profile id is compared against the weaker outputs
    data["profiles"]["profile-a"] = data["profiles"].pop("profile-b")
    base.write_text(json.dumps(data))
    md = tmp_path / "run.md"
    assert main(args("--profile", "profile-a", "--baseline", str(base), "--markdown", str(md))) == 1
    text = md.read_text()
    assert "FAIL: profile-a: accuracy fell" in text
    assert "field line_items[*].quantity" in text or "field due_date" in text


def test_missing_credentials_is_usage_error(monkeypatch):
    monkeypatch.delenv("UNSTRACT_API_URL", raising=False)
    monkeypatch.delenv("UNSTRACT_API_KEY", raising=False)
    code = main(["run", "--docs", str(EX / "docs"), "--golden", str(EX / "golden")])
    assert code == 2
