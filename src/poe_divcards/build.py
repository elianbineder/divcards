"""Builds a divination card dataset from a Path of Exile installation.

This is the only part of the project that reads the game (through
``poe-ggpk-extractor``). Its output is a self-contained directory that the API
serves without needing the game, the schema or the Oodle decompressor::

    <out>/<label>/
        manifest.json          version, source, languages, counts, shared assets
        cards.json             one record per card, every language included
        areas.json             atlas areas where cards drop
        images/cards/*.webp    card art
        images/items/**.webp   icons of the reward items
        images/frames/*.webp   card frame
        images/glyphs/*.webp   inline images of the texts (Harbinger glyphs)

Sources (PoE1 tables):

* ``BaseItemTypes`` rows of class ``DivinationCard``: id, name, drop level.
* ``CurrencyItems``: stack size and reward text (``Description``).
* ``FlavourText``: flavour text.
* ``DivinationCardArt`` + ``art/uidivinationimages.txt``: art texture and crop.
* ``DivinationCardStashTabLayout``: stash order and enabled flags.
* ``AtlasNode`` + ``WorldAreas``: atlas maps that drop each card.
* ``UniqueStashLayout`` + ``Words`` + ``ItemVisualIdentity``: unique rewards and icons.

Facts the game files lack (e.g. a reward item missing from every table) can be
supplied through an overrides file, see ``apply_overrides``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import markup

DATASET_FORMAT = 5
CARD_CLASS = "DivinationCard"
# DivinationCardArt.ArtFile is a logical name ("Art/2DItems/Divination/Images/<Name>").
# ART_MAP maps each name to a texture and the rectangle to crop from it, e.g.
#   "Art/2DItems/Divination/Images/AbandonedWealth" "Art/Textures/.../Images/3.dds" 0 0 389 279
# ART_ROOT is the fallback location when a name is missing from the map.
ART_MAP = "art/uidivinationimages.txt"
ART_ROOT = "art/textures/interface/2d/divinationcards/"
FRAME_ART = "art/2ditems/divination/images/divinationcard"
# Inline <<name>> images in texts (Harbinger glyphs) are UI images under GLYPH_ROOT.
UI_IMAGES = "art/uiimages1.txt"
GLYPH_ROOT = "art/2dart/uiimages/ingame/harbingerglyph/"
ICON_ROOT = "art/2ditems/"
UNIQUE_WORDLIST = "UNIQUE_ITEM"
DEFAULT_OVERRIDES = "overrides.json"

_ART_LINE = re.compile(r'^"([^"]+)"\s+"([^"]+)"\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)')

# Datasets are English-only by default, to show names exactly as in the English client.
DEFAULT_LANGUAGES = ("en",)
# Language codes used by the dataset and the API -> folder names in the game data.
LANGUAGES = {
    "en": "English", "fr": "French", "de": "German", "ja": "Japanese", "ko": "Korean",
    "pt": "Portuguese", "ru": "Russian", "es": "Spanish", "th": "Thai", "zh-tw": "Traditional Chinese",
}

# Reward text style -> reward kind.
REWARD_STYLES = {
    "uniqueitem": "unique",
    "currencyitem": "currency",
    "gemitem": "gem",
    "divination": "divination",
    "rareitem": "rare",
    "magicitem": "magic",
    "normal": "normal",
    "whiteitem": "normal",
}

_QUANTITY = re.compile(r"^(\d[\d,]*)x\s+(.+)$")
_LINKS = re.compile(r"^(Five|Six)-Link(?:ed)?\s+(.+)$")
_GEM_LEVEL = re.compile(r"^Level\s+(\d+)\s+(.+)$")
_PROPERTY = re.compile(r"^([A-Za-z][A-Za-z ]*?):\s*(.*)$")
LINK_COUNTS = {"Five": 5, "Six": 6}


class BuildError(Exception):
    pass


def url_safe_path(path: str) -> str:
    """Image path usable in a URL as is: game file names may contain spaces and quotes
    ("amulets/Malachai's BrillianceAmulet.dds")."""
    return re.sub(r"[^a-z0-9/._-]+", "-", path.lower())


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "card"


def col(row: dict[str, Any], *names: str) -> Any:
    """Value of the first column present; tolerates renames in the community schema."""
    for n in names:
        if n in row:
            return row[n]
    return None


def parse_reward(text: str) -> dict[str, Any]:
    """Structured reward from its text, keeping the names exactly as the game writes them.

    ``name`` is the reward as displayed ("Six-Link Astral Plate", "Level 21 Vaal Molten
    Shell"), without the quantity. Following lines become ``properties`` ("Item Level: 100"
    -> ``item_level: 100``) or ``flags`` ("Two-Implicit", "Shaper Item"); "Corrupted" is
    reported by ``corrupted``.
    """
    lines = markup.parse(text)
    styles = {s.style for line in lines for s in line}
    kind = name = None
    quantity = 1
    for seg in (lines[0] if lines else []):
        if seg.style in REWARD_STYLES:
            kind = REWARD_STYLES[seg.style]
            name = seg.text.strip()
            m = _QUANTITY.match(name)
            if m:
                quantity, name = int(m.group(1).replace(",", "")), m.group(2).strip()
            break
    properties: dict[str, Any] = {}
    flags: list[str] = []
    pending: str | None = None  # "Implicit Modifier:" followed by the modifier on the next line
    for line in lines[1:]:
        plain = "".join(seg.text for seg in line).strip()
        if not plain or plain == "Corrupted":
            continue
        m = _PROPERTY.match(plain)
        if m:
            key, value = slugify(m.group(1)).replace("-", "_"), m.group(2).strip()
            if value:
                properties[key] = int(value) if value.isdigit() else value
                pending = None
            else:
                pending = key
        elif pending:
            properties[pending] = plain
            pending = None
        else:
            flags.append(plain)
    return {
        "kind": kind or "other",
        "name": name,
        "quantity": quantity,
        "corrupted": "corrupted" in styles,
        "properties": properties,
        "flags": flags,
    }


def drop_enabled(stash_slot: dict[str, Any] | None, reward_text: str) -> bool | None:
    """Whether a card currently drops (None if nothing says either way)."""
    if markup.to_plain(reward_text).strip() == "Disabled":
        return False
    if stash_slot is None:
        return None
    return bool(stash_slot.get("IsEnabled")) and bool(stash_slot.get("IsInGame"))


@dataclass
class BuildReport:
    cards: int = 0
    languages: list[str] = field(default_factory=list)
    images: int = 0
    warnings: list[str] = field(default_factory=list)


class DatasetBuilder:
    def __init__(self, fs, schema, languages: list[str] | None = None, images: bool = True,
                 log: Callable[[str], None] | None = None):
        from poe_ggpk import GameData

        if fs.game != 1:
            raise BuildError("Divination cards only exist in Path of Exile 1")
        self.fs = fs
        self.schema = schema
        self.images = images
        self.log = log or (lambda msg: None)
        self.report = BuildReport()
        self.en = GameData(fs, schema, language="English")
        self.areas: dict[str, dict[str, Any]] = {}
        self.assets: dict[str, str] = {}
        self._ui_maps: dict[str, dict[str, tuple[str, tuple[int, int, int, int]]]] = {}
        self.localized: dict[str, Any] = {}
        for code in languages or DEFAULT_LANGUAGES:
            if code not in LANGUAGES:
                raise BuildError(f"Unknown language '{code}' (valid: {', '.join(LANGUAGES)})")
            self.localized[code] = self.en if code == "en" else GameData(fs, schema, language=LANGUAGES[code])

    # -- helpers -------------------------------------------------------------------
    def _warn(self, msg: str) -> None:
        self.report.warnings.append(msg)
        self.log(f"warning: {msg}")

    def _localized_rows(self, code: str, table: str) -> list[dict[str, Any]] | None:
        """Rows of ``table`` in language ``code``; None if they do not align with English."""
        rows = self.localized[code].rows(table)
        if len(rows) != len(self.en.rows(table)):
            self._warn(f"{table} in {LANGUAGES[code]} has {len(rows)} rows, English has "
                       f"{len(self.en.rows(table))}; English texts are used instead")
            return None
        return rows

    def _texture(self, path: str) -> bytes | None:
        """PNG of a game texture, following ``*path`` redirects; None if unavailable."""
        from poe_ggpk.convert import ConversionError, decode_text, dds_to_png

        for _ in range(3):
            try:
                data = self.fs.read(path)
            except FileNotFoundError:
                return None
            if data[:4] == b"DDS ":
                try:
                    return dds_to_png(data)
                except ConversionError as exc:
                    self._warn(f"{path}: {exc}")
                    return None
            target = decode_text(data).strip().lstrip("*").strip().lower()
            if not target or target == path:
                return None
            path = target
        return None

    def _save_image(self, src: str, out_root: Path, rel: str,
                    crop: tuple[int, int, int, int] | None = None) -> str | None:
        """Convert ``src`` (cropped to ``crop``) to WebP at ``out_root/rel``; returns ``rel`` or None."""
        rel = url_safe_path(rel)
        if not self.images:
            return rel if self.fs.exists(src) else None
        target = out_root / rel
        if target.exists():
            return rel
        png = self._texture(src)
        if png is None:
            self._warn(f"texture not found or not decodable: {src}")
            return None
        from PIL import Image

        image = Image.open(io.BytesIO(png))
        if crop:
            image = image.crop(crop)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="WEBP", quality=90, method=4)
        self.report.images += 1
        return rel

    def _ui_map(self, path: str) -> dict[str, tuple[str, tuple[int, int, int, int]]]:
        """UI image file (e.g. ART_MAP): logical name (lowercase) -> (texture path, crop rectangle)."""
        if path not in self._ui_maps:
            from poe_ggpk.convert import decode_text

            entries: dict[str, tuple[str, tuple[int, int, int, int]]] = {}
            self._ui_maps[path] = entries
            try:
                text = decode_text(self.fs.read(path))
            except FileNotFoundError:
                self._warn(f"{path} not found")
                return entries
            for line in text.splitlines():
                m = _ART_LINE.match(line.strip())
                if m:
                    x1, y1, x2, y2 = map(int, m.groups()[2:])
                    entries[m.group(1).lower()] = (m.group(2).lower(), (x1, y1, x2, y2))
        return self._ui_maps[path]

    def _art_map(self) -> dict[str, tuple[str, tuple[int, int, int, int]]]:
        """Logical card art name (lowercase) -> (texture path, crop rectangle)."""
        return self._ui_map(ART_MAP)

    def _glyph(self, name: str, out: Path) -> str | None:
        """Image of an inline ``<<name>>`` glyph (Harbinger script), cropped from the UI atlas.

        Some names in the texts differ from the atlas: "HBGAd" is registered as "HGBAd" and
        "hbgi04" as "hbg04i", so those spellings are tried too.
        """
        ui = self._ui_map(UI_IMAGES)
        for candidate in dict.fromkeys((name, name.replace("HBG", "HGB", 1), re.sub(r"^hbgi(\d+)$", r"hbg\1i", name))):
            entry = ui.get(GLYPH_ROOT + candidate.lower())
            if entry:
                return self._save_image(entry[0], out, f"images/glyphs/{name.lower()}.webp", crop=entry[1])
        self._warn(f"inline image <<{name}>> not found in {UI_IMAGES}")
        return None

    def _art(self, art_file: str, out: Path, rel: str) -> str | None:
        entry = self._art_map().get(art_file.lower())
        if entry:
            return self._save_image(entry[0], out, rel, crop=entry[1])
        return self._save_image(f"{ART_ROOT}{art_file.lower().removeprefix('art/')}.dds", out, rel)

    def _atlas_drops(self) -> dict[int, list[str]]:
        """Card row -> atlas area ids that drop it (``AtlasNode.DivCards``).

        Also fills ``self.areas`` with the areas involved. Unique maps have tier 0 in the
        game data; their ``tier`` is "Unique" instead.
        """
        en = self.en
        areas_en = en.rows("WorldAreas")
        names = {code: (self._localized_rows(code, "WorldAreas") or areas_en) if code != "en" else areas_en
                 for code in self.localized}
        drops: dict[int, list[str]] = {}
        for node in en.rows("AtlasNode"):
            a = col(node, "Area1", "WorldAreasKey")
            if a is None or not (0 <= a < len(areas_en)):
                continue
            area_id = areas_en[a]["Id"]
            cards = col(node, "DivCards") or []
            if not cards:
                continue
            if area_id not in self.areas:
                unique_map = bool(node.get("IsUniqueMap"))
                self.areas[area_id] = {
                    "id": area_id,
                    "tier": "Unique" if unique_map and not node.get("Tier") else node.get("Tier"),
                    "unique_map": unique_map,
                    "on_atlas": not node.get("NotOnAtlas"),
                    "area_level": areas_en[a].get("AreaLevel"),
                    "names": {code: rows[a].get("Name") or areas_en[a]["Name"] for code, rows in names.items()},
                }
            for c in cards:
                target = drops.setdefault(c, [])
                if area_id not in target:
                    target.append(area_id)
        return drops

    def _icon(self, ivi_index: int | None, out: Path) -> str | None:
        if ivi_index is None:
            return None
        dds = (self.en.rows("ItemVisualIdentity")[ivi_index].get("DDSFile") or "").lower()
        if not dds.endswith(".dds"):
            return None
        rel = dds[len(ICON_ROOT):] if dds.startswith(ICON_ROOT) else dds.replace("/", "_")
        return self._save_image(dds, out, "images/items/" + rel[:-4] + ".webp")

    # -- build ---------------------------------------------------------------------
    def build(self, out: Path) -> list[dict[str, Any]]:
        en = self.en
        classes = en.rows("ItemClasses")
        class_ids = {i for i, c in enumerate(classes) if c["Id"] == CARD_CLASS}
        if not class_ids:
            raise BuildError(f"Item class '{CARD_CLASS}' not found")
        base = en.rows("BaseItemTypes")
        cards = [r for r in base if col(r, "ItemClassesKey", "ItemClass") in class_ids]
        self.log(f"{len(cards)} divination cards found")

        currency = {col(r, "BaseItemTypesKey", "BaseItemType"): r for r in en.rows("CurrencyItems")}
        art = {col(r, "BaseItemTypesKey", "BaseItemType"): r for r in en.rows("DivinationCardArt")}
        layout = {col(r, "StoredItem"): (n, r) for n, r in enumerate(en.rows("DivinationCardStashTabLayout"))}
        tags = en.rows("Tags")

        # Reward lookups (English names).
        uniques: dict[str, dict[str, Any]] = {}
        words = en.rows("Words")
        wordlists = self.schema.enumeration("Wordlists")
        unique_list = (wordlists.enumerators.index(UNIQUE_WORDLIST) + wordlists.indexing
                       if wordlists and UNIQUE_WORDLIST in wordlists.enumerators else None)
        # Unique names without a unique stash entry (so without an icon either).
        unique_words = {w["Text"]: n for n, w in enumerate(words)
                        if unique_list is not None and w.get("Wordlist") == unique_list}
        for r in en.rows("UniqueStashLayout"):
            w = r.get("WordsKey")
            if w is None or not (0 <= w < len(words)):
                continue
            name = words[w]["Text"]
            prev = uniques.get(name)
            if prev is None or (prev.get("IsAlternateArt") and not r.get("IsAlternateArt")):
                uniques[name] = r
        # A base named like its own class ("Ring" in "Rings") is a placeholder, and trade
        # proxies ("Map", "Blueprint") are not real items: rewards with those names are
        # categories, not concrete items.
        def generic(r: dict[str, Any]) -> bool:
            c = col(r, "ItemClassesKey", "ItemClass")
            class_name = classes[c].get("Name") if c is not None and 0 <= c < len(classes) else None
            return class_name in (r["Name"], r["Name"] + "s", r["Name"] + "es")

        bases: dict[str, dict[str, Any]] = {}
        for r in base:
            name = r.get("Name")
            rid = r["Id"].lower()
            if (name and name not in bases and "microtransaction" not in rid
                    and "/tradeproxy/" not in rid and not generic(r)):
                bases[name] = r

        # Slugs, unique and stable (based on the English name).
        slugs: dict[int, str] = {}
        used: set[str] = set()
        for r in cards:
            s = slugify(r["Name"] or r["Id"].rsplit("/", 1)[-1])
            if s in used:
                s = slugify(r["Id"].rsplit("/", 1)[-1])
            n = 2
            unique_s = s
            while unique_s in used:
                unique_s, n = f"{s}-{n}", n + 1
            used.add(unique_s)
            slugs[r["_index"]] = unique_s
        card_by_name = {r["Name"]: slugs[r["_index"]] for r in cards}
        # Base names by length, to find the base inside a magic name ("Perandus' Gold Ring").
        base_names = sorted((n for n in bases if " " in n), key=len, reverse=True)

        def find(kind: str, name: str) -> dict[str, Any] | None:
            """Concrete item called exactly ``name``."""
            if kind == "divination" and name in card_by_name:
                return {"type": "card", "slug": card_by_name[name], "name": name, "icon": None}
            if kind == "unique" and (name in uniques or name in unique_words):
                u = uniques.get(name)
                w = words[u["WordsKey"] if u else unique_words[name]]
                # Words.Text is the internal name; Text2 is the displayed one.
                display = w.get("Text2") or w["Text"]
                return {"type": "unique", "slug": slugify(display), "name": display,
                        "icon": self._icon(u.get("ItemVisualIdentityKey"), out) if u else None}
            b = bases.get(name)
            if b is not None:
                return {"type": "base", "id": b["Id"], "slug": slugify(name), "name": name,
                        "icon": self._icon(b.get("ItemVisualIdentity"), out)}
            return None

        def resolve(reward: dict[str, Any]) -> dict[str, Any] | None:
            """Item the reward refers to; the displayed reward name is left untouched.

            Besides exact names it understands "Six-Link <base>", "Level <n> <gem>"
            (also "<gem> Support") and magic names containing a base.
            """
            kind, name, props = reward["kind"], reward["name"], reward["properties"]
            item = find(kind, name)
            if item:
                return item
            m = _LINKS.match(name)
            if m:
                item = find(kind, m.group(2))
                if item:
                    props["links"] = LINK_COUNTS[m.group(1)]
                    return item
            m = _GEM_LEVEL.match(name)
            if m and kind == "gem":
                item = find(kind, m.group(2)) or find(kind, m.group(2) + " Support")
                if item:
                    props["level"] = int(m.group(1))
                    return item
            if kind in ("magic", "rare", "normal"):
                padded = f" {name} "
                for base_name in base_names:
                    if f" {base_name} " in padded:
                        return find(kind, base_name)
            return None

        localized = {}
        for code in self.localized:
            localized[code] = {t: (self._localized_rows(code, t) if code != "en" else en.rows(t))
                               for t in ("BaseItemTypes", "CurrencyItems", "FlavourText", "Words")}
        self.report.languages = list(self.localized)
        drops = self._atlas_drops()
        frame = self._art_map().get(FRAME_ART)
        if frame:
            rel = self._save_image(frame[0], out, "images/frames/divinationcard.webp", crop=frame[1])
            if rel:
                self.assets["frame"] = rel

        records = []
        for n, r in enumerate(cards, 1):
            i = r["_index"]
            slug = slugs[i]
            cur = currency.get(i, {})
            reward = parse_reward(cur.get("Description") or "")
            reward["item"] = resolve(reward) if reward["name"] else None

            art_row = art.get(i)
            art_rel = None
            if art_row and art_row.get("ArtFile"):
                art_rel = self._art(art_row["ArtFile"], out, f"images/cards/{slug}.webp")
            elif art_row is None:
                self._warn(f"{r['Name']}: no DivinationCardArt row")

            pos = layout.get(i)
            i18n = {}
            for code, t in localized.items():
                b_rows = t["BaseItemTypes"] or en.rows("BaseItemTypes")
                c_rows = t["CurrencyItems"] or en.rows("CurrencyItems")
                f_rows = t["FlavourText"] or en.rows("FlavourText")
                name = b_rows[i].get("Name") or r["Name"]
                reward_text = c_rows[cur["_index"]].get("Description", "") if cur else ""
                fk = col(r, "FlavourTextKey", "FlavourText")
                flavour_text = f_rows[fk].get("Text", "") if fk is not None else ""
                entry = {
                    "name": name,
                    "reward": markup.to_json(markup.parse(reward_text)),
                    "reward_text": markup.to_plain(reward_text),
                    "flavour": markup.to_json(markup.parse(flavour_text)),
                    "flavour_text": markup.to_plain(flavour_text),
                }
                for line in entry["reward"] + entry["flavour"]:
                    for seg in line:
                        if seg.get("glyph") and (image := self._glyph(seg["glyph"], out)):
                            seg["image"] = image
                i18n[code] = entry

            records.append({
                "slug": slug,
                "id": r["Id"],
                "stack_size": cur.get("StackSize"),
                "drop_level": r.get("DropLevel"),
                "stash_order": pos[0] if pos else None,
                "in_game": bool(pos[1].get("IsInGame")) if pos else None,
                # Drop enabled. A card is drop disabled when its stash slot is disabled or not
                # in game, or when its reward text is just "Disabled". Cards disabled without
                # any of these signals are set in overrides.json.
                "enabled": drop_enabled(pos[1] if pos else None, cur.get("Description") or ""),
                "tags": [tags[t]["Id"] for t in (col(r, "TagsKeys", "Tags") or []) if 0 <= t < len(tags)],
                "art": art_rel,
                "drops": {"atlas": drops.get(i, [])},
                # Only cards that drop in atlas areas can be scried; the rest are "Non-Scryable".
                "scryable": bool(drops.get(i)),
                "reward": reward,
                "i18n": i18n,
            })
            if n % 50 == 0:
                self.log(f"  {n}/{len(cards)}")
        records.sort(key=lambda c: (c["stash_order"] is None, c["stash_order"] or 0, c["slug"]))
        self.report.cards = len(records)
        return records


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _merge(target[k], v)
        else:
            target[k] = v


def apply_overrides(cards: list[dict[str, Any]], overrides: dict[str, Any]) -> list[str]:
    """Merge manual corrections into the cards; returns warnings.

    ``overrides`` maps a card slug to a partial card record, merged recursively
    (keys starting with ``_`` are comments and ignored)::

        {"deadly-joy": {"reward": {"item": {"type": "unique", "slug": "torrent-s-reclamation",
                                            "name": "Torrent's Reclamation"}}}}
    """
    by_slug = {c["slug"]: c for c in cards}
    warnings = []
    for slug, patch in overrides.items():
        if slug.startswith("_"):
            continue
        card = by_slug.get(slug)
        if card is None:
            warnings.append(f"override for unknown card '{slug}' ignored")
            continue
        _merge(card, {k: v for k, v in patch.items() if not k.startswith("_")})
        card["overridden"] = True
    return warnings


def write_dataset(fs, schema, out_dir: str | os.PathLike[str], label: str | None = None,
                  languages: list[str] | None = None, images: bool = True,
                  log: Callable[[str], None] | None = None,
                  overrides: dict[str, Any] | None = None) -> tuple[Path, BuildReport]:
    """Build the dataset into ``out_dir/<label>`` and return its path and report."""
    started = time.time()
    index_sha = hashlib.sha256(fs.read_bundle_file("_.index.bin")).hexdigest()
    label = label or f"poe1-{time.strftime('%Y%m%d')}-{index_sha[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise BuildError(f"Invalid label '{label}': use letters, digits, '.', '_' or '-'")
    target = Path(out_dir) / label
    target.mkdir(parents=True, exist_ok=True)

    builder = DatasetBuilder(fs, schema, languages, images=images, log=log)
    cards = builder.build(target)
    if overrides:
        for w in apply_overrides(cards, overrides):
            builder._warn(w)
    (target / "areas.json").write_text(json.dumps(builder.areas, ensure_ascii=False, separators=(",", ":")),
                                       encoding="utf-8")
    cards_json = json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
    (target / "cards.json").write_text(cards_json, encoding="utf-8")
    manifest = {
        "format": DATASET_FORMAT,
        "label": label,
        "game": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build_seconds": round(time.time() - started, 1),
        "content_sha256": hashlib.sha256(cards_json.encode("utf-8")).hexdigest(),
        "source": {"index_sha256": index_sha, "schema_created_at": schema.created_at},
        "languages": {code: LANGUAGES[code] for code in builder.report.languages},
        "images": images,
        "assets": builder.assets,
        "counts": {"cards": len(cards), "areas": len(builder.areas),
                   "overridden": sum(1 for c in cards if c.get("overridden"))},
        "warnings": builder.report.warnings,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, builder.report


def find_game(location: str | None = None) -> str:
    if location:
        return location
    env = os.environ.get("POE_GGPK")
    if env:
        return env
    from poe_ggpk.cli import DEFAULT_LOCATIONS

    for loc in DEFAULT_LOCATIONS[1]:
        p = Path(loc)
        if (p / "Content.ggpk").is_file() or (p / "Bundles2" / "_.index.bin").is_file():
            return str(p)
    raise BuildError("Path of Exile installation not found; pass --ggpk or set POE_GGPK")


def main_build(args) -> int:
    try:
        from poe_ggpk import PoEFileSystem, Schema
    except ImportError:
        print("The build step needs poe-ggpk-extractor: pip install -e ../poe-ggpk-extractor", file=sys.stderr)
        return 2
    location = find_game(args.ggpk)
    log = (lambda msg: print(msg, file=sys.stderr))
    log(f"Reading {location}")
    schema = Schema.load(args.schema)
    overrides_path = Path(args.overrides) if args.overrides else Path(DEFAULT_OVERRIDES)
    overrides = None
    if args.overrides or overrides_path.is_file():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        log(f"Applying overrides from {overrides_path}")
    with PoEFileSystem(location, index_file=args.index) as fs:
        path, report = write_dataset(fs, schema, args.output, args.label, args.lang,
                                     images=not args.no_images, log=log, overrides=overrides)
    log(f"Dataset written to {path}: {report.cards} cards, {len(report.languages)} languages, "
        f"{report.images} images, {len(report.warnings)} warnings")
    return 0
