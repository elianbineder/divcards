"""A small synthetic dataset, so the API can be tested without the game."""

import json

import pytest


def _card(slug, order, name, es_name, reward_style, reward, kind, stack, enabled=True, item=None,
          corrupted=False, tags=("unique_divination",), areas=()):
    def i18n(n, r):
        lines = [[{"text": r, "style": reward_style}]]
        if corrupted:
            lines.append([{"text": "Corrupted", "style": "corrupted"}])
        return {"name": n, "reward": lines, "reward_text": r + ("\nCorrupted" if corrupted else ""),
                "flavour": [[{"text": "Flavour of " + n}]], "flavour_text": "Flavour of " + n}

    return {
        "slug": slug, "id": f"Metadata/Items/DivinationCards/{slug}", "stack_size": stack,
        "drop_level": 10 + order, "stash_order": order, "in_game": True, "enabled": enabled,
        "tags": list(tags), "art": f"images/cards/{slug}.webp",
        "drops": {"atlas": list(areas)}, "scryable": bool(areas),
        "reward": {"kind": kind, "name": reward, "quantity": 1, "corrupted": corrupted,
                   "properties": {}, "flags": [], "item": item},
        "i18n": {"en": i18n(name, reward), "es": i18n(es_name, reward)},
    }


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("dataset")
    cards = [
        _card("the-doctor", 0, "The Doctor", "El doctor", "uniqueitem", "Headhunter", "unique", 8,
              item={"type": "unique", "slug": "headhunter", "name": "Headhunter", "icon": "images/items/belts/headhunter.webp"},
              areas=("MapWorldsBurialChambers",)),
        _card("house-of-mirrors", 1, "House of Mirrors", "Casa de los espejos", "currencyitem",
              "Mirror of Kalandra", "currency", 9, tags=("currency_divination",), areas=("MapWorldsShoreUnique",),
              item={"type": "base", "slug": "mirror-of-kalandra", "name": "Mirror of Kalandra", "id": "Metadata/Items/Currency/CurrencyDuplicate",
                    "icon": "images/items/currency/currencyduplicate.webp"}),
        _card("the-fiend", 2, "The Fiend", "El demonio", "uniqueitem", "Headhunter", "unique", 11, corrupted=True,
              areas=("MapWorldsFoundry", "MapWorldsBurialChambers"),
              item={"type": "unique", "slug": "headhunter", "name": "Headhunter", "icon": "images/items/belts/headhunter.webp"}),
        _card("the-cartographer", 3, "The Cartographer", "El cartógrafo", "normal", "Map", "normal", 5,
              enabled=False),
    ]
    (root / "cards.json").write_text(json.dumps(cards), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "format": 5, "label": "test", "game": 1, "built_at": "2026-10-05T00:00:00Z",
        "content_sha256": "0123456789abcdef" * 4, "source": {},
        "languages": {"en": "English", "es": "Spanish"}, "counts": {"cards": len(cards), "areas": 2},
        "assets": {"frame": "images/frames/divinationcard.webp"},
    }), encoding="utf-8")
    (root / "areas.json").write_text(json.dumps({
        "MapWorldsBurialChambers": {"id": "MapWorldsBurialChambers", "tier": 14, "unique_map": False,
                                    "on_atlas": True, "area_level": 81,
                                    "names": {"en": "Burial Chambers", "es": "Cámaras funerarias"}},
        "MapWorldsFoundry": {"id": "MapWorldsFoundry", "tier": 11, "unique_map": False, "on_atlas": True,
                             "area_level": 78, "names": {"en": "Foundry", "es": "Fundición"}},
        "MapWorldsShoreUnique": {"id": "MapWorldsShoreUnique", "tier": "Unique", "unique_map": True,
                                 "on_atlas": True, "area_level": 75, "names": {"en": "Mao Kun"}},
    }), encoding="utf-8")
    (root / "images" / "cards").mkdir(parents=True)
    (root / "images" / "cards" / "the-doctor.webp").write_bytes(b"RIFF....WEBP")
    return root
