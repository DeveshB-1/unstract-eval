from unstract_eval.score import (
    EXTRA, MATCH, MISMATCH, MISSING, Rules, compare, parse_date, parse_number,
)


def by_path(results):
    return {r.path: r for r in results}


def test_parse_number_handles_currency_grouping_and_negatives():
    assert parse_number("$1,250.00") == 1250.0
    assert parse_number("2,07,500.00") == 207500.0  # Indian digit grouping
    assert parse_number("Rs. 37,350") == 37350.0
    assert parse_number("(42.10)") == -42.10
    assert parse_number("USD 99") == 99.0
    assert parse_number("n/a") is None
    assert parse_number(True) is None


def test_parse_date_formats_and_order():
    assert str(parse_date("03/14/2026")) == "2026-03-14"
    assert str(parse_date("May 20th, 2026")) == "2026-05-20"
    assert str(parse_date("18 Apr 2026")) == "2026-04-18"
    assert str(parse_date("04/05/2026", "MDY")) == "2026-04-05"
    assert str(parse_date("04/05/2026", "DMY")) == "2026-05-04"
    assert parse_date("not a date") is None


def test_format_differences_are_matches_but_not_exact_matches():
    expected = {"date": "2026-03-14", "total": 847.76, "vendor": "Acme Corp"}
    actual = {"date": "03/14/2026", "total": "$847.76", "vendor": "ACME CORP."}
    res = compare(expected, actual)
    assert all(r.status == MATCH for r in res)
    assert not any(r.strict_match for r in res)


def test_real_errors_are_reported():
    res = by_path(compare({"total": 4895.0, "due": "2026-05-16", "po": "PO-1"},
                          {"total": 4075.0, "due": "2026-05-15"}))
    assert res["total"].status == MISMATCH
    assert res["due"].status == MISMATCH
    assert res["po"].status == MISSING


def test_null_golden_matches_empty_output():
    res = compare({"po_number": None}, {"po_number": ""})
    assert res[0].status == MATCH


def test_list_of_objects_is_order_insensitive_and_penalises_extras():
    expected = {"items": [{"d": "Paper", "a": 10}, {"d": "Toner", "a": 20}]}
    actual = {"items": [{"d": "Toner", "a": 20}, {"d": "Paper", "a": 10}, {"d": "Invented", "a": 0}]}
    res = compare(expected, actual)
    statuses = [r.status for r in res]
    assert statuses.count(MATCH) == 4
    assert statuses.count(EXTRA) == 1


def test_missing_list_item_counts_each_field():
    expected = {"items": [{"d": "Paper", "a": 10}, {"d": "Toner", "a": 20}]}
    res = compare(expected, {"items": [{"d": "Paper", "a": 10}]})
    assert [r.status for r in res].count(MISSING) == 2


def test_rules_fuzzy_and_ignore():
    rules = Rules.from_dict({"fields": {"vendor": {"type": "fuzzy", "threshold": 0.9}}, "ignore": ["debug.*"]})
    res = by_path(compare({"vendor": "Brightline Logistics Inc", "debug": {"x": 1}},
                          {"vendor": "Brightline Logistics", "debug": {"x": 2}}, rules))
    assert res["vendor"].status == MATCH
    assert "debug.x" not in res


def test_rule_patterns_apply_to_list_items():
    rules = Rules.from_dict({"fields": {"items[*].qty": {"type": "number", "tolerance": 0.5}}})
    res = compare({"items": [{"qty": 2}]}, {"items": [{"qty": "2.4"}]}, rules)
    assert res[0].status == MATCH
