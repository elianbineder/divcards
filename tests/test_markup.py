"""Game text markup parsing."""

from poe_divcards.build import apply_overrides, drop_enabled, parse_reward, slugify
from poe_divcards.markup import parse, to_json, to_plain


def test_plain_text_is_one_unstyled_segment():
    assert to_json(parse("Just text")) == [[{"text": "Just text"}]]


def test_styled_reward_with_newline():
    lines = to_json(parse("<uniqueitem>{Headhunter}\r\n<corrupted>{Corrupted}"))
    assert lines == [[{"text": "Headhunter", "style": "uniqueitem"}],
                     [{"text": "Corrupted", "style": "corrupted"}]]


def test_mixed_segments_on_one_line():
    lines = to_json(parse("<default>{Quality:} <augmented>{+20%}"))
    assert lines == [[{"text": "Quality:", "style": "default"}, {"text": " "},
                      {"text": "+20%", "style": "augmented"}]]


def test_size_applies_to_nested_styles():
    lines = to_json(parse("<size:31>{Plain <uniqueitem>{Named}}"))
    assert lines == [[{"text": "Plain ", "size": 31},
                      {"text": "Named", "style": "uniqueitem", "size": 31}]]


def test_space_between_tag_and_brace():
    # The Eye of Terror: "<uniqueitem>{Mageblood}\r\n<brequelmutated> {Foulborn}"
    lines = to_json(parse("<uniqueitem>{Mageblood}\r\n<brequelmutated> {Foulborn}"))
    assert lines == [[{"text": "Mageblood", "style": "uniqueitem"}],
                     [{"text": "Foulborn", "style": "brequelmutated"}]]
    assert parse_reward("<uniqueitem>{Mageblood}\r\n<brequelmutated> {Foulborn}")["flags"] == ["Foulborn"]


def test_inline_glyphs_are_separate_segments():
    # The Messenger's flavour text is Harbinger script drawn with inline images.
    lines = to_json(parse("<<hbgi04>><<HBG04>>\r\nText <<HBGAt>>after"))
    assert lines == [[{"text": "", "glyph": "hbgi04"}, {"text": "", "glyph": "HBG04"}],
                     [{"text": "Text "}, {"text": "", "glyph": "HBGAt"}, {"text": "after"}]]
    assert to_plain("<<HBG04>>a") == "a"
    # Inside a styled block the glyph keeps the style.
    assert to_json(parse("<size:30>{<<HBG01>>}")) == [[{"text": "", "size": 30, "glyph": "HBG01"}]]


def test_unbalanced_markup_does_not_raise():
    assert to_plain("<uniqueitem>{Unclosed") == "Unclosed"
    assert to_plain("Stray } brace") == "Stray } brace"
    assert to_plain("a < b") == "a < b"


def test_parse_reward_kinds_and_quantities():
    assert parse_reward("<currencyitem>{3x Chaos Orb}") == {
        "kind": "currency", "name": "Chaos Orb", "quantity": 3, "corrupted": False,
        "properties": {}, "flags": []}
    assert parse_reward("<uniqueitem>{Headhunter}\r\n<corrupted>{Corrupted}")["corrupted"] is True
    assert parse_reward("<currencyitem>{1,500x Vivid Crystallised Lifeforce}")["quantity"] == 1500
    assert parse_reward("")["kind"] == "other"


def test_parse_reward_keeps_the_in_game_name_and_reads_extra_lines():
    r = parse_reward("<normal>{Six-Link Astral Plate}\r\n<default>{Item Level:} <normal>{100}\r\n"
                     "<default>{Crusader Item}")
    assert r["name"] == "Six-Link Astral Plate"
    assert r["properties"] == {"item_level": 100}
    assert r["flags"] == ["Crusader Item"]
    r = parse_reward("<gemitem>{Level 21 Vaal Molten Shell}\r\n<default>{Quality:} <augmented>{+20%}\r\n"
                     "<corrupted>{Corrupted}")
    assert r["name"] == "Level 21 Vaal Molten Shell"
    assert r["properties"] == {"quality": "+20%"} and r["corrupted"] and r["flags"] == []
    r = parse_reward("<rareitem>{Jewel}\r\n<default>{Implicit Modifier:} \r\n<magicitem>{Some modifier}")
    assert r["properties"] == {"implicit_modifier": "Some modifier"}


def test_slugify():
    assert slugify("The Wolven King's Bite") == "the-wolven-king-s-bite"
    assert slugify("Lantador's Lost Love") == "lantador-s-lost-love"
    assert slugify("Médico") == "medico"


def test_overrides_merge_recursively_and_flag_the_card():
    cards = [{"slug": "deadly-joy", "reward": {"kind": "unique", "item": None},
              "i18n": {"en": {"name": "Deadly Joy"}}}]
    warnings = apply_overrides(cards, {
        "_comment": "ignored",
        "deadly-joy": {"_why": "ignored", "reward": {"item": {"type": "unique", "slug": "x"}},
                       "i18n": {"en": {"name": "Fixed"}}},
        "missing-card": {},
    })
    assert cards[0]["reward"] == {"kind": "unique", "item": {"type": "unique", "slug": "x"}}
    assert cards[0]["i18n"]["en"] == {"name": "Fixed"}
    assert cards[0]["overridden"] is True and "_why" not in cards[0]
    assert warnings == ["override for unknown card 'missing-card' ignored"]


def test_drop_enabled_signals():
    slot = {"IsEnabled": True, "IsInGame": True}
    assert drop_enabled(slot, "<uniqueitem>{Headhunter}") is True
    assert drop_enabled(slot, "Disabled") is False                      # reward text says so
    assert drop_enabled({"IsEnabled": False, "IsInGame": True}, "x") is False
    assert drop_enabled({"IsEnabled": True, "IsInGame": False}, "x") is False
    assert drop_enabled(None, "x") is None


def test_image_paths_are_url_safe():
    from poe_divcards.build import url_safe_path
    assert url_safe_path("images/items/amulets/malachai's brillianceamulet.webp") == \
        "images/items/amulets/malachai-s-brillianceamulet.webp"
    assert url_safe_path("images/items/amulets/Ahn Artifact.webp") == "images/items/amulets/ahn-artifact.webp"
    assert url_safe_path("images/cards/the-doctor.webp") == "images/cards/the-doctor.webp"
