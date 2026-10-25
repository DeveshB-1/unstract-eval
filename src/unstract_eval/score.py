"""Type-aware, field-level comparison of extracted JSON against a golden answer.

Extraction output is rarely byte-identical to a hand-written golden file even
when it is correct: "03/14/2026" vs "2026-03-14", "$1,250.00" vs 1250, "ACME
Corp." vs "Acme Corp". Exact matching reports those as failures and buries the
real errors. This module normalises by field type first, then compares, and
also records what a naive exact match would have said so the two can be
reported side by side.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

MATCH, MISMATCH, MISSING, EXTRA = "match", "mismatch", "missing", "extra"

_DATE_FORMATS_COMMON = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%d-%b-%Y", "%d-%B-%Y", "%Y%m%d",
]
_DATE_FORMATS_MDY = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"]
_DATE_FORMATS_DMY = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"]

_TRUE = {"true", "yes", "y", "1", "checked", "x"}
_FALSE = {"false", "no", "n", "0", "unchecked", ""}


@dataclass
class FieldResult:
    path: str
    status: str
    kind: str
    expected: Any = None
    actual: Any = None
    strict_match: bool = False

    @property
    def correct(self) -> bool:
        return self.status == MATCH


@dataclass
class Rules:
    """Per-field comparison rules, loaded from an eval config file.

    `fields` maps a path pattern (``total``, ``line_items[*].amount``) to
    ``{"type": "number"|"date"|"string"|"fuzzy"|"bool"|"exact", ...}``.
    """

    fields: dict[str, dict] = field(default_factory=dict)
    date_order: str = "MDY"
    ignore: list[str] = field(default_factory=list)
    number_tolerance: float = 0.01
    fuzzy_threshold: float = 0.9

    @classmethod
    def from_dict(cls, data: dict | None) -> "Rules":
        data = data or {}
        return cls(
            fields=data.get("fields", {}),
            date_order=data.get("date_order", "MDY").upper(),
            ignore=data.get("ignore", []),
            number_tolerance=float(data.get("number_tolerance", 0.01)),
            fuzzy_threshold=float(data.get("fuzzy_threshold", 0.9)),
        )

    def rule_for(self, path: str) -> dict:
        generic = _generic(path)
        for pattern, rule in self.fields.items():
            if _match(generic, pattern) or _match(path, pattern):
                return rule
        return {}

    def ignored(self, path: str) -> bool:
        generic = _generic(path)
        return any(_match(generic, p) for p in self.ignore)


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[*]", path)


def _match(path: str, pattern: str) -> bool:
    # "[" is a character class in fnmatch; treat it literally so that
    # "line_items[*].amount" means what it looks like.
    return fnmatch.fnmatchcase(path, pattern.replace("[", "[[]"))


# ---------------------------------------------------------------- normalisers

def parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = re.sub(r"(?i)\b(?:usd|eur|gbp|inr|rs)\b\.?", "", s)
    s = re.sub(r"[\s$€£₹¥,]", "", s)
    if s.endswith("-"):
        negative, s = True, s[:-1]
    if s.endswith("%"):
        s = s[:-1]
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


def parse_date(value: Any, order: str = "MDY") -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    s = re.sub(r"\s+", " ", value.strip())
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    ordered = _DATE_FORMATS_DMY + _DATE_FORMATS_MDY if order == "DMY" else _DATE_FORMATS_MDY + _DATE_FORMATS_DMY
    for fmt in _DATE_FORMATS_COMMON + ordered:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def norm_string(value: Any) -> str:
    s = "" if value is None else str(value)
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s.strip(" .,;:")


def infer_kind(expected: Any) -> str:
    if isinstance(expected, bool):
        return "bool"
    if isinstance(expected, (int, float)):
        return "number"
    if isinstance(expected, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected.strip()):
        return "date"
    return "string"


def _is_empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == [] or v == {}


def compare_scalar(expected: Any, actual: Any, kind: str, rule: dict, rules: Rules) -> bool:
    if _is_empty(expected):
        return _is_empty(actual)
    if _is_empty(actual):
        return False
    if kind == "exact":
        return expected == actual
    if kind == "number":
        e, a = parse_number(expected), parse_number(actual)
        tol = float(rule.get("tolerance", rules.number_tolerance))
        return e is not None and a is not None and abs(e - a) <= tol
    if kind == "date":
        order = rule.get("date_order", rules.date_order).upper()
        e, a = parse_date(expected, order), parse_date(actual, order)
        return e is not None and e == a
    if kind == "bool":
        e, a = parse_bool(expected), parse_bool(actual)
        return e is not None and e == a
    if kind == "fuzzy":
        threshold = float(rule.get("threshold", rules.fuzzy_threshold))
        ratio = difflib.SequenceMatcher(None, norm_string(expected), norm_string(actual)).ratio()
        return ratio >= threshold
    return norm_string(expected) == norm_string(actual)


def _strict(expected: Any, actual: Any) -> bool:
    return json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True)


# ---------------------------------------------------------------- recursion

def compare(expected: Any, actual: Any, rules: Rules | None = None, path: str = "") -> list[FieldResult]:
    """Compare a golden value against an extracted value; one result per leaf."""
    rules = rules or Rules()
    if path and rules.ignored(path):
        return []

    if isinstance(expected, dict):
        out: list[FieldResult] = []
        actual_dict = actual if isinstance(actual, dict) else {}
        for key, exp_val in expected.items():
            child = f"{path}.{key}" if path else key
            if key not in actual_dict and not _is_empty(exp_val):
                out.extend(_all_missing(exp_val, rules, child))
            else:
                out.extend(compare(exp_val, actual_dict.get(key), rules, child))
        return out

    if isinstance(expected, list):
        return _compare_list(expected, actual if isinstance(actual, list) else [], rules, path)

    rule = rules.rule_for(path)
    kind = rule.get("type") or infer_kind(expected)
    if _is_empty(actual) and not _is_empty(expected):
        return [FieldResult(path, MISSING, kind, expected, actual, False)]
    ok = compare_scalar(expected, actual, kind, rule, rules)
    return [FieldResult(path, MATCH if ok else MISMATCH, kind, expected, actual, _strict(expected, actual))]


def _all_missing(expected: Any, rules: Rules, path: str) -> list[FieldResult]:
    # Comparing against None marks every non-empty leaf MISSING and every
    # empty one (golden says "absent") as a MATCH.
    return compare(expected, None, rules, path)


def _ratio(results: list[FieldResult]) -> float:
    return sum(r.correct for r in results) / len(results) if results else 0.0


def _compare_list(expected: list, actual: list, rules: Rules, path: str) -> list[FieldResult]:
    """Order-insensitive list comparison.

    Each expected item is paired with the unused actual item that matches it
    best; leftovers on the actual side are reported as EXTRA so a model that
    invents a line item is penalised, not ignored.
    """
    out: list[FieldResult] = []
    unused = list(range(len(actual)))
    for i, exp_item in enumerate(expected):
        child = f"{path}[{i}]"
        best_j, best_res, best_score = None, None, -1.0
        for j in unused:
            res = compare(exp_item, actual[j], rules, child)
            score = _ratio(res)
            if score > best_score:
                best_j, best_res, best_score = j, res, score
        if best_j is not None and best_score > 0:
            unused.remove(best_j)
            out.extend(best_res)
        else:
            out.extend(_all_missing(exp_item, rules, child))
    for j in unused:
        out.append(FieldResult(f"{path}[+{j}]", EXTRA, "item", None, actual[j], False))
    return out


@dataclass
class DocScore:
    doc: str
    results: list[FieldResult]
    error: str | None = None
    cost: float | None = None
    seconds: float | None = None

    @property
    def scored(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(r.correct for r in self.results)

    @property
    def strict_correct(self) -> int:
        return sum(r.strict_match for r in self.results)


def score_document(doc: str, expected: dict, actual: Any, rules: Rules | None = None, **extra) -> DocScore:
    return DocScore(doc=doc, results=compare(expected, actual if actual is not None else {}, rules), **extra)
