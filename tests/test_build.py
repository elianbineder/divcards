"""Dataset build against the installed game (skipped when it is missing)."""

import json

import pytest

pytest.importorskip("poe_ggpk")

from poe_divcards.build import BuildError, find_game, write_dataset  # noqa: E402
from poe_divcards.dataset import Dataset, Query  # noqa: E402


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from poe_ggpk import PoEFileSystem, Schema

    try:
        location = find_game()
    except BuildError:
        pytest.skip("Path of Exile is not installed")
    out = tmp_path_factory.mktemp("datasets")
    with PoEFileSystem(location) as fs:
        if fs.game != 1:
            pytest.skip("POE_GGPK points to Path of Exile 2")
        path, report = write_dataset(fs, Schema.load(), out, "it", images=False)
    return path, report


@pytest.mark.integration
def test_build_produces_cards(built):
    path, report = built
    cards = json.loads((path / "cards.json").read_text(encoding="utf-8"))
    assert report.cards == len(cards) > 400
    slugs = [c["slug"] for c in cards]
    assert len(slugs) == len(set(slugs))
    assert all(c["stack_size"] for c in cards)
    assert sum(1 for c in cards if c["art"]) > 0.95 * len(cards)


@pytest.mark.integration
def test_well_known_card(built):
    ds = Dataset(built[0])
    doctor = ds.get("the-doctor")
    assert doctor["reward"]["kind"] == "unique"
    assert doctor["reward"]["item"]["slug"] == "headhunter"
    assert doctor["i18n"]["en"]["reward_text"] == "Headhunter"
    assert doctor["reward"]["item"]["name"] == "Headhunter"
    assert set(doctor["i18n"]) == {"en"}
    total, _ = ds.search(Query(reward_item="headhunter", include_disabled=True))
    assert total >= 2


@pytest.mark.integration
def test_art_comes_from_the_ui_image_map(built):
    ds = Dataset(built[0])
    # Its art is a crop of a shared texture (3.dds), not <name>.dds.
    assert ds.get("abandoned-wealth")["art"] == "images/cards/abandoned-wealth.webp"
    assert all(c["art"] for c in ds.cards)
    assert ds.manifest["assets"]["frame"]


@pytest.mark.integration
def test_atlas_drops(built):
    ds = Dataset(built[0])
    assert "MapWorldsBurialChambers" in ds.get("the-doctor")["drops"]["atlas"]
    assert ds.areas["MapWorldsBurialChambers"]["names"]["en"] == "Burial Chambers"
    assert isinstance(ds.areas["MapWorldsBurialChambers"]["tier"], int)
    assert ds.areas["MapWorldsVaalPyramidUnique"]["tier"] == "Unique"
    assert all(set(c["drops"]) == {"atlas"} for c in ds.cards)
    assert all(c["scryable"] == bool(c["drops"]["atlas"]) for c in ds.cards)
    assert sum(1 for c in ds.cards if c["drops"]["atlas"]) > 300


@pytest.mark.integration
def test_rewards_keep_the_in_game_name_and_link_the_item(built):
    ds = Dataset(built[0])
    cases = {
        "the-celestial-justicar": ("Six-Link Astral Plate", "Astral Plate", {"links": 6}),
        "the-magma-crab": ("Level 21 Vaal Molten Shell", "Vaal Molten Shell", {"level": 21}),
        "damnation": ("The Original Scripture", "The Original Scripture", {"item_level": 83}),
    }
    for slug, (shown, linked, props) in cases.items():
        reward = ds.get(slug)["reward"]
        assert reward["name"] == shown
        assert reward["item"]["name"] == linked
        assert props.items() <= reward["properties"].items()
    # Generic class names are categories, not items.
    assert all((c["reward"]["item"] or {}).get("name") not in ("Ring", "Map") for c in ds.cards)
