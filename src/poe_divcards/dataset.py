"""Read-only access to a built dataset (no game files needed).

The whole dataset (a few hundred cards) is kept in memory; filters and search
run over precomputed, accent-insensitive search keys. An optional costs file
(see ``weights``) adds a ``weight`` to each card.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .weights import Costs

SUPPORTED_FORMATS = {5}
SORT_KEYS = ("stash_order", "name", "stack_size", "drop_level", "weight")
NO_DROPS: dict[str, list[str]] = {"atlas": []}


class DatasetError(Exception):
    pass


def fold(text: str) -> str:
    """Lowercase without diacritics, so 'medico' matches 'médico'."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass(slots=True)
class Query:
    lang: str = "en"
    q: str | None = None
    kinds: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    corrupted: bool | None = None
    reward_item: str | None = None
    area: str | None = None
    scryable: bool | None = None
    stack_min: int | None = None
    stack_max: int | None = None
    weight_min: float | None = None
    weight_max: float | None = None
    include_disabled: bool = False
    sort: str = "stash_order"
    descending: bool = False
    offset: int = 0
    limit: int = 50


class Dataset:
    def __init__(self, root: str | os.PathLike[str], costs: Costs | str | os.PathLike[str] | None = None):
        self.root = Path(root)
        try:
            self.manifest: dict[str, Any] = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            cards: list[dict[str, Any]] = json.loads((self.root / "cards.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DatasetError(f"{self.root} is not a dataset (missing {Path(exc.filename).name})") from exc
        if self.manifest.get("format") not in SUPPORTED_FORMATS:
            raise DatasetError(f"Unsupported dataset format {self.manifest.get('format')}")
        self.cards = cards
        self.costs = costs if isinstance(costs, Costs) or costs is None else Costs.load(costs)
        for c in cards:
            c["weight"] = self.costs.weight_for(c) if self.costs else None
        areas_file = self.root / "areas.json"
        self.areas: dict[str, dict[str, Any]] = (
            json.loads(areas_file.read_text(encoding="utf-8")) if areas_file.is_file() else {})
        self._area_ids = {a.lower(): a for a in self.areas}
        self.by_slug = {c["slug"]: c for c in cards}
        self.languages: dict[str, str] = self.manifest["languages"]
        self._aliases = {code: code for code in self.languages}
        self._aliases.update({name.lower(): code for code, name in self.languages.items()})
        # Search key per card and language: name, reward, flavour and the linked item.
        self._search = {
            c["slug"]: {
                code: fold(" ".join((t["name"], t["reward_text"], t["flavour_text"],
                                     (c["reward"]["item"] or {}).get("name") or "")))
                for code, t in c["i18n"].items()
            }
            for c in cards
        }

    @property
    def version(self) -> str:
        """Changes with the dataset and with the costs file."""
        base = self.manifest.get("content_sha256", "") or self.manifest.get("label", "")
        if not self.costs:
            return base[:16]
        return hashlib.sha256((base + self.costs.sha256).encode()).hexdigest()[:16]

    def costs_info(self) -> dict[str, Any] | None:
        if not self.costs:
            return None
        expected = [c for c in self.cards if c.get("enabled") is not False]
        return {
            "league": self.costs.league,
            "updated_at": self.costs.updated_at,
            "cards_with_cost": sum(1 for c in expected if c["weight"]),
            "cards_expected": len(expected),
            "problems": self.costs.check(self.cards),
        }

    def language(self, lang: str | None) -> str:
        """Normalize a language code or name ('es', 'Spanish'); raises for unknown ones."""
        code = self._aliases.get((lang or "en").strip().lower())
        if code is None:
            raise DatasetError(f"Unknown language '{lang}'. Available: {', '.join(self.languages)}")
        return code

    @staticmethod
    def _text(card: dict[str, Any], lang: str) -> dict[str, Any]:
        return card["i18n"].get(lang) or card["i18n"]["en"]

    @staticmethod
    def _drops(card: dict[str, Any]) -> list[str]:
        return (card.get("drops") or NO_DROPS)["atlas"]

    def area_view(self, area_id: str, lang: str | None) -> dict[str, Any]:
        """Area with its name in ``lang``, or every name under ``names`` when ``lang`` is None."""
        a = self.areas.get(area_id) or {"id": area_id, "names": {}}
        view = {k: v for k, v in a.items() if k != "names"}
        if lang is None:
            view["names"] = a["names"]
        else:
            view["name"] = a["names"].get(lang) or a["names"].get("en") or area_id
        return view

    def area_list(self, lang: str, include_disabled: bool = False) -> list[dict[str, Any]]:
        """Every area that drops cards, with the slugs of those cards."""
        cards_by_area: dict[str, list[str]] = {a: [] for a in self.areas}
        for c in self.cards:
            if not include_disabled and c.get("enabled") is False:
                continue
            for a in self._drops(c):
                cards_by_area.setdefault(a, []).append(c["slug"])
        out = [{**self.area_view(a, lang), "cards": slugs} for a, slugs in cards_by_area.items() if slugs]
        # Tiered maps first (by tier), then unique maps ("Unique"), each by name.
        out.sort(key=lambda a: (not isinstance(a.get("tier"), int), a.get("tier") if isinstance(a.get("tier"), int) else 0,
                                fold(a["name"])))
        return out

    # -- queries ---------------------------------------------------------------------
    def search(self, query: Query) -> tuple[int, list[dict[str, Any]]]:
        lang = self.language(query.lang)
        if query.sort not in SORT_KEYS:
            raise DatasetError(f"Invalid sort '{query.sort}'. Use one of: {', '.join(SORT_KEYS)}")
        terms = fold(query.q).split() if query.q else []
        kinds = {k.lower() for k in query.kinds}
        area = None
        if query.area:
            area = self._area_ids.get(query.area.lower())
            if area is None:
                raise DatasetError(f"Unknown area '{query.area}' (see /v1/areas)")
        tags = {t.lower() for t in query.tags}

        def keep(c: dict[str, Any]) -> bool:
            if not query.include_disabled and c.get("enabled") is False:
                return False
            if kinds and c["reward"]["kind"] not in kinds:
                return False
            if tags and not tags.intersection(t.lower() for t in c["tags"]):
                return False
            if query.corrupted is not None and c["reward"]["corrupted"] != query.corrupted:
                return False
            if query.reward_item and (c["reward"]["item"] or {}).get("slug") != query.reward_item.lower():
                return False
            drops = self._drops(c)
            if area and area not in drops:
                return False
            if query.scryable is not None and c["scryable"] != query.scryable:
                return False
            w = (c.get("weight") or {}).get("value")
            if query.weight_min is not None and (w is None or w < query.weight_min):
                return False
            if query.weight_max is not None and (w is None or w > query.weight_max):
                return False
            stack = c.get("stack_size") or 0
            if query.stack_min is not None and stack < query.stack_min:
                return False
            if query.stack_max is not None and stack > query.stack_max:
                return False
            if terms:
                keys = self._search[c["slug"]]
                key = keys.get(lang) or keys["en"]
                if not all(t in key for t in terms):
                    return False
            return True

        hits = [c for c in self.cards if keep(c)]

        def sort_key(c: dict[str, Any]):
            if query.sort == "name":
                return (0, fold(self._text(c, lang)["name"]))
            v = (c.get("weight") or {}).get("value") if query.sort == "weight" else c.get(query.sort)
            return (v is None, v if v is not None else 0, c["slug"])

        hits.sort(key=sort_key, reverse=query.descending)
        if query.descending:
            # Cards without a value stay last in both orders.
            hits.sort(key=lambda c: sort_key(c)[0])
        page = hits[query.offset:query.offset + query.limit]
        return len(hits), [self.summary(c, lang) for c in page]

    def get(self, slug: str) -> dict[str, Any] | None:
        return self.by_slug.get(slug.lower())

    def summary(self, card: dict[str, Any], lang: str) -> dict[str, Any]:
        t = self._text(card, lang)
        return {
            "slug": card["slug"],
            "name": t["name"],
            "stack_size": card.get("stack_size"),
            "drop_level": card.get("drop_level"),
            "enabled": card.get("enabled"),
            "art": card.get("art"),
            "scryable": card["scryable"],
            "atlas_areas": len(self._drops(card)),
            "weight": card.get("weight"),
            "reward": {**card["reward"], "text": t["reward_text"]},
        }

    def detail(self, card: dict[str, Any], lang: str | None) -> dict[str, Any]:
        """Full card with its texts in ``lang`` under ``text``; with ``lang=None`` every
        language is included under ``i18n`` instead."""
        base = {k: v for k, v in card.items() if k != "i18n"}
        drops = card.get("drops") or NO_DROPS
        base["drops"] = {k: [self.area_view(a, lang) for a in v] for k, v in drops.items()}
        if lang is None:
            return {**base, "i18n": card["i18n"]}
        return {**base, "lang": lang, "text": self._text(card, lang)}

    def facets(self, include_disabled: bool = False) -> dict[str, Any]:
        cards = [c for c in self.cards if include_disabled or c.get("enabled") is not False]
        stacks = [c["stack_size"] for c in cards if c.get("stack_size")]
        return {
            "total": len(cards),
            "reward_kinds": dict(Counter(c["reward"]["kind"] for c in cards).most_common()),
            "tags": dict(Counter(t for c in cards for t in c["tags"]).most_common()),
            "stack_size": {"min": min(stacks, default=None), "max": max(stacks, default=None)},
            "corrupted_rewards": sum(1 for c in cards if c["reward"]["corrupted"]),
            "scryable": sum(1 for c in cards if c["scryable"]),
            "non_scryable": sum(1 for c in cards if not c["scryable"]),
        }
