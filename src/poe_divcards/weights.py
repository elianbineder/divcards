"""Drop weights estimated from the gold cost Faustus charges for each card.

Gold costs cannot be datamined and change every league, so they are entered by hand
in a costs file (``costs/<league>.json``)::

    {"league": "...", "updated_at": "2026-10-05",
     "costs": {"the-doctor": 1850, "rain-of-chaos": 35, "house-of-mirrors": null}}

Drop rate from the gold cost:

* common cards (cost < 125):           ``10^6 / cost``
* uncommon and rare cards (cost >= 125): ``13 * 10^9 / cost^3``

Faustus rounds costs DOWN to multiples of 5 (below 125), 25 (125 to 1049) or 50
(1050 and above): a displayed 175 may really be 190. To reduce that error the weight
is computed twice, with the displayed cost and with the cost + ``ROUNDING_OFFSET``,
and the mean of both is used. The formula is chosen by the displayed cost and used
for both computations.

Faustus sometimes shows a cost that is not a multiple of its range's step (670 in the
25 range); such costs are rounded to the nearest multiple (675) before computing the
weight. Costs above ``MAX_GOLD_COST`` are rejected (the highest one known is 1850).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMMON_LIMIT = 125
ROUNDING_OFFSET = 25
MAX_GOLD_COST = 2000


def rounding_step(cost: int) -> int:
    """Multiple Faustus rounds a displayed cost down to."""
    if cost < COMMON_LIMIT:
        return 5
    if cost < 1050:
        return 25
    return 50


def normalize_cost(cost: int) -> int:
    """Nearest multiple of the step of the cost's range (670 -> 675); halves round up."""
    step = rounding_step(cost)
    return max(step, step * int(cost / step + 0.5))


def valid_cost(cost: Any) -> bool:
    return isinstance(cost, int) and not isinstance(cost, bool) and 0 < cost <= MAX_GOLD_COST


def formula(cost: int) -> str:
    return "common" if cost < COMMON_LIMIT else "uncommon_rare"


def drop_rate(cost: float, kind: str) -> float:
    if kind == "common":
        return 10**6 / cost
    return 13 * 10**9 / cost**3


def _round(x: float) -> float:
    return float(f"{x:.6g}")


def weight(cost: int) -> dict[str, Any]:
    """Weight of a card whose displayed gold cost is ``cost``.

    ``max`` uses the displayed cost, ``min`` the cost + ROUNDING_OFFSET, ``value`` their mean.
    """
    kind = formula(cost)
    high = drop_rate(cost, kind)
    low = drop_rate(cost + ROUNDING_OFFSET, kind)
    return {
        "gold_cost": cost,
        "formula": kind,
        "value": _round((high + low) / 2),
        "min": _round(low),
        "max": _round(high),
    }


class CostsError(Exception):
    pass


@dataclass
class Costs:
    league: str
    updated_at: str | None
    costs: dict[str, int | None]
    sha256: str
    problems: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Costs":
        raw = Path(path).read_bytes()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CostsError(f"{path}: invalid JSON ({exc})") from exc
        if not isinstance(data, dict) or not isinstance(data.get("costs"), dict):
            raise CostsError(f"{path}: expected an object with a 'costs' mapping")
        costs = {k: v for k, v in data["costs"].items() if not k.startswith("_")}
        return cls(str(data.get("league") or ""), data.get("updated_at"), costs,
                   hashlib.sha256(raw).hexdigest())

    def check(self, cards: list[dict[str, Any]]) -> list[str]:
        """Problems in the costs against the dataset's cards (empty when everything is valid)."""
        by_slug = {c["slug"]: c for c in cards}
        problems = []
        if not self.league:
            problems.append("'league' is empty")
        for slug, cost in self.costs.items():
            card = by_slug.get(slug)
            if card is None:
                problems.append(f"{slug}: unknown card")
                continue
            if cost is None:
                continue
            if not valid_cost(cost):
                problems.append(f"{slug}: cost must be an integer from 1 to {MAX_GOLD_COST}, got {cost!r}")
                continue
            if card.get("enabled") is False:
                problems.append(f"{slug}: disabled cards have no gold cost; it is ignored")
        return problems

    def roundings(self, cards: list[dict[str, Any]]) -> list[str]:
        """Costs that will be rounded to the step of their range (not problems)."""
        enabled = {c["slug"] for c in cards if c.get("enabled") is not False}
        return [f"{slug}: {cost} -> {normalize_cost(cost)}" for slug, cost in self.costs.items()
                if slug in enabled and valid_cost(cost) and normalize_cost(cost) != cost]

    def missing(self, cards: list[dict[str, Any]]) -> list[str]:
        """Enabled cards without a cost."""
        return [c["slug"] for c in cards
                if c.get("enabled") is not False and self.costs.get(c["slug"]) is None]

    def weight_for(self, card: dict[str, Any]) -> dict[str, Any] | None:
        cost = self.costs.get(card["slug"])
        if card.get("enabled") is False or not valid_cost(cost):
            return None
        return weight(normalize_cost(cost))


_LIST_LINE = re.compile(r"^(?:(?P<c1>\d[\d,]*)\s+(?P<n1>.+?)|(?P<n2>.+?)\s+(?P<c2>\d[\d,]*))$")


def _name_key(name: str) -> str:
    """Card name for matching: case-insensitive, typographic apostrophes and spaces normalized."""
    return " ".join(name.replace("\u2019", "'").replace("`", "'").casefold().split())


@dataclass
class ImportResult:
    data: dict[str, Any]
    imported: int = 0
    unmatched: list[str] = field(default_factory=list)          # lines whose name is no card
    disabled: list[str] = field(default_factory=list)           # disabled cards, skipped
    conflicts: list[str] = field(default_factory=list)          # same card with different costs
    invalid: list[str] = field(default_factory=list)            # lines without "cost name"
    pending: int = 0                                            # card names with no cost yet
    rounded: list[str] = field(default_factory=list)            # costs rounded to their step


def import_cost_list(text: str, cards: list[dict[str, Any]], league: str = "",
                     updated_at: str | None = None) -> ImportResult:
    """Costs file from a pasted list, one card per line: ``<cost> <name>`` or ``<name> <cost>``
    (tab or spaces). A line with only a card name (cost not filled in yet, as in a
    ``template_tsv`` file) is pending. Names are matched to the cards' English names;
    enabled cards without a cost are kept with a null cost."""
    by_name = {_name_key(c["i18n"]["en"]["name"]): c for c in cards}
    result = ImportResult(template(cards, league))
    result.data["updated_at"] = updated_at
    costs = result.data["costs"]
    seen: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LIST_LINE.match(line)
        if not m:
            card = by_name.get(_name_key(line))
            if card is None:
                result.invalid.append(line)
            elif card.get("enabled") is not False:  # disabled cards need no cost
                result.pending += 1
            continue
        cost = int((m.group("c1") or m.group("c2")).replace(",", ""))
        name = (m.group("n1") or m.group("n2")).strip()
        card = by_name.get(_name_key(name))
        if card is None:
            result.unmatched.append(name)
            continue
        slug = card["slug"]
        if card.get("enabled") is False:
            result.disabled.append(card["i18n"]["en"]["name"])
            continue
        if slug in seen:
            if seen[slug] != cost:
                result.conflicts.append(f"{card['i18n']['en']['name']}: {seen[slug]} and {cost} (first kept)")
            continue
        seen[slug] = cost
        if valid_cost(cost) and normalize_cost(cost) != cost:
            result.rounded.append(f"{card['i18n']['en']['name']}: {cost} -> {normalize_cost(cost)}")
            cost = normalize_cost(cost)
        costs[slug] = cost
        result.imported += 1
    return result


def template(cards: list[dict[str, Any]], league: str = "") -> dict[str, Any]:
    """Costs file with every enabled card and no cost, to be filled in by hand."""
    return {
        "_comment": "Displayed Faustus gold cost per card slug; null = unknown. Disabled cards are not listed.",
        "league": league,
        "updated_at": None,
        "costs": {c["slug"]: None for c in cards if c.get("enabled") is not False},
    }


def template_tsv(cards: list[dict[str, Any]], league: str = "", dataset: str = "<dataset>") -> str:
    """Cost list to fill in by hand (e.g. in a spreadsheet): ``<cost><TAB><name>`` per enabled
    card, alphabetical, with the cost column empty; imported with ``costs import``."""
    names = sorted((c["i18n"]["en"]["name"] for c in cards if c.get("enabled") is not False), key=str.casefold)
    target = f"costs/{league or '<league>'}.json"
    header = [
        f"# Faustus gold costs{' for league ' + league if league else ''}: one card per line, <cost><TAB><name>.",
        "# Write the cost shown by Faustus before each name; leave it empty if unknown.",
        "# Disabled cards are not listed. Lines starting with # are ignored.",
        f"# Import: poe-divcards costs import {dataset} <this file> -o {target}"
        + (f' --league "{league}"' if league else "") + " --updated-at <YYYY-MM-DD>",
    ]
    return "\n".join(header + [f"\t{n}" for n in names]) + "\n"
