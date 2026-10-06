"""Importing a pasted '<cost> <name>' list into a costs file."""

from poe_divcards.weights import import_cost_list, template_tsv


def _card(slug, name, enabled=True):
    return {"slug": slug, "enabled": enabled, "i18n": {"en": {"name": name}}}


CARDS = [
    _card("the-doctor", "The Doctor"),
    _card("fire-of-unknown-origin", "Fire of Unknown Origin"),
    _card("the-wolf-s-shadow", "The Wolf's Shadow"),
    _card("rain-of-chaos", "Rain of Chaos"),
    _card("the-cartographer", "The Cartographer", enabled=False),
]


def test_import_matches_names_loosely_and_keeps_missing_cards():
    text = "\n".join([
        "925\tThe Doctor",
        "925\tFire Of Unknown Origin",      # different case
        "The Wolf’s Shadow   125",      # name first, typographic apostrophe
        "",
        "# comments are ignored",
    ])
    r = import_cost_list(text, CARDS, "Test", "2026-10-05")
    assert r.data["league"] == "Test" and r.data["updated_at"] == "2026-10-05"
    assert r.data["costs"] == {"the-doctor": 925, "fire-of-unknown-origin": 925,
                               "the-wolf-s-shadow": 125, "rain-of-chaos": None}
    assert r.imported == 3
    assert (r.unmatched, r.disabled, r.conflicts, r.invalid) == ([], [], [], [])


def test_import_reports_what_it_cannot_use():
    text = "\n".join([
        "250\tThe Cartographer",            # disabled in the dataset
        "300\tNot A Card",
        "925\tThe Doctor",
        "950\tThe Doctor",                  # conflicting duplicate
        "1,050\tRain of Chaos",             # thousands separator
        "just text",
    ])
    r = import_cost_list(text, CARDS)
    assert r.disabled == ["The Cartographer"]
    assert r.unmatched == ["Not A Card"]
    assert r.conflicts == ["The Doctor: 925 and 950 (first kept)"]
    assert r.invalid == ["just text"]
    assert r.data["costs"]["the-doctor"] == 925
    assert r.data["costs"]["rain-of-chaos"] == 1050
    assert "the-cartographer" not in r.data["costs"]


def test_import_rounds_costs_to_their_step():
    r = import_cost_list("670\tThe Doctor\n35\tRain of Chaos\n", CARDS)
    assert r.data["costs"]["the-doctor"] == 675
    assert r.data["costs"]["rain-of-chaos"] == 35
    assert r.rounded == ["The Doctor: 670 -> 675"]


def test_name_only_lines_are_pending_not_invalid():
    r = import_cost_list("\tThe Doctor\n925\tFire of Unknown Origin\n\tNot A Card\n\tThe Cartographer\n", CARDS)
    assert r.pending == 1 and r.imported == 1          # the disabled card is not pending
    assert r.invalid == ["Not A Card"]
    assert r.data["costs"]["the-doctor"] is None


def test_tsv_template_round_trips_through_import():
    text = template_tsv(CARDS, "3.29", "datasets/x")
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    # Alphabetical, empty cost column, disabled cards left out.
    assert lines == ["\tFire of Unknown Origin", "\tRain of Chaos", "\tThe Doctor", "\tThe Wolf's Shadow"]
    assert 'costs/3.29.json --league "3.29"' in text
    filled = text.replace("\tThe Doctor", "925\tThe Doctor")
    r = import_cost_list(filled, CARDS, "3.29")
    assert r.imported == 1 and r.pending == 3 and r.invalid == []
    assert r.data["costs"]["the-doctor"] == 925
