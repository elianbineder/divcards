"""Weights from Faustus gold costs."""

import json

import pytest

from poe_divcards.weights import MAX_GOLD_COST, Costs, normalize_cost, rounding_step, template, weight


def test_common_cards_use_the_inverse_formula():
    w = weight(50)
    assert w["formula"] == "common"
    assert w["max"] == 20000                       # 10^6 / 50
    assert w["min"] == pytest.approx(13333.3)      # 10^6 / 75
    assert w["value"] == pytest.approx((20000 + 10**6 / 75) / 2, rel=1e-5)


def test_uncommon_and_rare_cards_use_the_cubic_formula():
    w = weight(500)
    assert w["formula"] == "uncommon_rare"
    assert w["max"] == 104                         # 13 * 10^9 / 500^3
    assert w["min"] == pytest.approx(13e9 / 525**3, rel=1e-5)
    assert w["value"] == pytest.approx((104 + 13e9 / 525**3) / 2, rel=1e-5)


def test_formula_is_chosen_by_the_displayed_cost():
    # 110 + 25 crosses 125, but both computations use the common formula.
    w = weight(110)
    assert w["formula"] == "common"
    assert w["min"] == pytest.approx(10**6 / 135, rel=1e-5)


def test_rounding_steps():
    assert [rounding_step(c) for c in (5, 120, 125, 1025, 1049, 1050, 2000)] == [5, 5, 25, 25, 25, 50, 50]


@pytest.fixture
def cards():
    return [{"slug": "a", "enabled": True}, {"slug": "b", "enabled": True},
            {"slug": "c", "enabled": True}, {"slug": "old", "enabled": False}]


def _costs(tmp_path, costs, league="Test"):
    p = tmp_path / "costs.json"
    p.write_text(json.dumps({"league": league, "updated_at": "2026-10-05", "costs": costs}), encoding="utf-8")
    return Costs.load(p)


def test_costs_off_their_step_are_rounded_to_the_nearest_multiple():
    assert [normalize_cost(c) for c in (670, 662, 1075, 1060, 122, 123, 2, 1040, 1990)] == [
        675, 650, 1100, 1050, 120, 125, 5, 1050, 2000]
    assert normalize_cost(675) == 675 and normalize_cost(35) == 35


def test_check_reports_invalid_unknown_and_disabled_cards(tmp_path, cards):
    costs = _costs(tmp_path, {"a": 130, "b": 1260, "c": -5, "old": 100, "ghost": 50, "_note": "ignored"})
    problems = costs.check(cards)
    assert any(p.startswith("c: cost must be an integer from 1 to 2000") for p in problems)
    assert any(p.startswith("old: disabled cards") for p in problems)
    assert "ghost: unknown card" in problems
    assert len(problems) == 3
    # Costs off their step are not problems: they are rounded.
    assert costs.roundings(cards) == ["a: 130 -> 125", "b: 1260 -> 1250"]
    assert costs.weight_for(cards[0]) == weight(125)


def test_valid_costs_and_missing_cards(tmp_path, cards):
    costs = _costs(tmp_path, {"a": 35, "b": 1250, "c": None})
    assert costs.check(cards) == []
    assert costs.missing(cards) == ["c"]
    assert costs.weight_for(cards[0])["gold_cost"] == 35
    assert costs.weight_for(cards[2]) is None


def test_costs_above_the_maximum_are_rejected(tmp_path, cards):
    costs = _costs(tmp_path, {"a": MAX_GOLD_COST, "b": MAX_GOLD_COST + 50})
    assert costs.check(cards) == ["b: cost must be an integer from 1 to 2000, got 2050"]
    assert costs.weight_for(cards[0])["gold_cost"] == 2000
    assert costs.weight_for(cards[1]) is None


def test_disabled_cards_never_get_a_weight(tmp_path, cards):
    costs = _costs(tmp_path, {"old": 100})
    assert costs.weight_for(cards[3]) is None


def test_template_lists_enabled_cards_without_cost(cards):
    t = template(cards, "Test")
    assert t["league"] == "Test"
    assert t["costs"] == {"a": None, "b": None, "c": None}
