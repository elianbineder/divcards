"""HTTP API over the synthetic dataset."""

import json

import pytest
from fastapi.testclient import TestClient

from poe_divcards.api import create_app


@pytest.fixture(scope="module")
def client(dataset_dir):
    return TestClient(create_app(dataset_dir, cors_origins=["https://divcards.example"]))


def slugs(resp):
    assert resp.status_code == 200, resp.text
    return [c["slug"] for c in resp.json()["items"]]


def test_meta(client):
    m = client.get("/v1/meta").json()
    assert m["label"] == "test"
    assert m["languages"] == {"en": "English", "es": "Spanish"}
    assert m["version"] == "0123456789abcdef"


def test_list_hides_disabled_cards_by_default(client):
    assert slugs(client.get("/v1/cards")) == ["the-doctor", "house-of-mirrors", "the-fiend"]
    assert "the-cartographer" in slugs(client.get("/v1/cards", params={"include_disabled": True}))


def test_list_summary_shape_and_image_urls(client):
    doctor = client.get("/v1/cards").json()["items"][0]
    assert doctor["name"] == "The Doctor"
    assert doctor["art"] == "/images/cards/the-doctor.webp"
    assert doctor["reward"]["item"]["icon"] == "/images/items/belts/headhunter.webp"
    assert doctor["reward"]["text"] == "Headhunter"


def test_search_is_localized_and_accent_insensitive(client):
    assert slugs(client.get("/v1/cards", params={"q": "doctor"})) == ["the-doctor"]
    assert slugs(client.get("/v1/cards", params={"q": "espejos", "lang": "es"})) == ["house-of-mirrors"]
    assert slugs(client.get("/v1/cards", params={"q": "cartografo", "lang": "Spanish",
                                                 "include_disabled": True})) == ["the-cartographer"]


def test_filters(client):
    assert slugs(client.get("/v1/cards", params={"kind": "currency"})) == ["house-of-mirrors"]
    assert slugs(client.get("/v1/cards", params={"reward_item": "headhunter"})) == ["the-doctor", "the-fiend"]
    assert slugs(client.get("/v1/cards", params={"corrupted": True})) == ["the-fiend"]
    assert slugs(client.get("/v1/cards", params={"stack_min": 9})) == ["house-of-mirrors", "the-fiend"]
    assert slugs(client.get("/v1/cards", params={"tag": "currency_divination"})) == ["house-of-mirrors"]


def test_sort_and_pagination(client):
    assert slugs(client.get("/v1/cards", params={"sort": "stack_size", "order": "desc"}))[0] == "the-fiend"
    assert slugs(client.get("/v1/cards", params={"sort": "name", "lang": "es"}))[0] == "house-of-mirrors"
    page = client.get("/v1/cards", params={"offset": 1, "limit": 1}).json()
    assert page["total"] == 3 and [c["slug"] for c in page["items"]] == ["house-of-mirrors"]


def test_card_detail_single_language(client):
    d = client.get("/v1/cards/the-doctor", params={"lang": "es"}).json()
    assert d["lang"] == "es"
    assert d["text"]["name"] == "El doctor"
    assert d["text"]["reward"] == [[{"text": "Headhunter", "style": "uniqueitem"}]]
    assert d["reward"]["kind"] == "unique"
    assert "i18n" not in d


def test_card_detail_all_languages(client):
    d = client.get("/v1/cards/the-doctor", params={"lang": "all"}).json()
    assert set(d["i18n"]) == {"en", "es"}
    assert "text" not in d


def test_errors(client):
    assert client.get("/v1/cards/nope").status_code == 404
    r = client.get("/v1/cards", params={"lang": "xx"})
    assert r.status_code == 400 and "Unknown language" in r.json()["detail"]
    assert client.get("/v1/cards", params={"sort": "bogus"}).status_code == 400
    assert client.get("/v1/cards", params={"limit": 10_000}).status_code == 422


def test_etag_revalidation(client):
    first = client.get("/v1/cards")
    assert first.headers["etag"] == 'W/"0123456789abcdef"'
    again = client.get("/v1/cards", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_facets(client):
    f = client.get("/v1/facets").json()
    assert f["total"] == 3
    assert f["reward_kinds"] == {"unique": 2, "currency": 1}
    assert f["stack_size"] == {"min": 8, "max": 11}


def test_images_and_cors(client):
    r = client.get("/images/cards/the-doctor.webp", headers={"Origin": "https://divcards.example"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://divcards.example"
    assert "max-age" in r.headers["cache-control"]
    assert client.get("/images/../manifest.json").status_code == 404


def test_large_responses_are_compressed(client):
    r = client.get("/v1/cards", params={"include_disabled": True}, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_meta_exposes_frame(client):
    assert client.get("/v1/meta").json()["assets"] == {"frame": "/images/frames/divinationcard.webp"}


def test_drops_in_detail_are_localized(client):
    d = client.get("/v1/cards/the-fiend", params={"lang": "es"}).json()
    assert [a["name"] for a in d["drops"]["atlas"]] == ["Fundición", "Cámaras funerarias"]
    assert d["drops"]["atlas"][0]["tier"] == 11
    all_langs = client.get("/v1/cards/the-fiend", params={"lang": "all"}).json()
    assert all_langs["drops"]["atlas"][0]["names"]["en"] == "Foundry"


def test_area_filters(client):
    assert slugs(client.get("/v1/cards", params={"area": "mapworldsburialchambers"})) == ["the-doctor", "the-fiend"]
    assert slugs(client.get("/v1/cards", params={"scryable": False})) == []
    assert slugs(client.get("/v1/cards", params={"scryable": False, "include_disabled": True})) == ["the-cartographer"]
    assert len(slugs(client.get("/v1/cards", params={"scryable": True}))) == 3
    assert client.get("/v1/cards", params={"area": "Nowhere"}).status_code == 400
    first = client.get("/v1/cards").json()["items"][0]
    assert first["atlas_areas"] == 1 and first["scryable"] is True
    assert client.get("/v1/facets", params={"include_disabled": True}).json()["non_scryable"] == 1


def test_areas_endpoint(client):
    areas = client.get("/v1/areas", params={"lang": "es"}).json()
    assert [(a["id"], a["name"], a["cards"]) for a in areas] == [
        ("MapWorldsFoundry", "Fundición", ["the-fiend"]),
        ("MapWorldsBurialChambers", "Cámaras funerarias", ["the-doctor", "the-fiend"]),
        ("MapWorldsShoreUnique", "Mao Kun", ["house-of-mirrors"]),
    ]


def test_unique_maps_have_unique_tier_and_no_ruthless_lists(client):
    d = client.get("/v1/cards/house-of-mirrors").json()
    assert d["drops"] == {"atlas": [{"id": "MapWorldsShoreUnique", "name": "Mao Kun", "tier": "Unique",
                                     "unique_map": True, "on_atlas": True, "area_level": 75}]}


@pytest.fixture(scope="module")
def costs_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("costs") / "league.json"
    p.write_text(json.dumps({"league": "Test League", "updated_at": "2026-10-05", "costs": {
        "the-doctor": 1850, "house-of-mirrors": 2000, "the-fiend": 1250, "the-cartographer": 100,
    }}), encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def weighted(dataset_dir, costs_file):
    return TestClient(create_app(dataset_dir, costs=costs_file))


def test_without_costs_there_are_no_weights(client):
    assert client.get("/v1/meta").json()["costs"] is None
    assert "weight" not in client.get("/v1/cards/the-doctor").json()


def test_weights_in_cards(weighted):
    d = weighted.get("/v1/cards/the-doctor").json()
    assert d["weight"]["gold_cost"] == 1850 and d["weight"]["formula"] == "uncommon_rare"
    assert d["weight"]["min"] < d["weight"]["value"] < d["weight"]["max"]
    # Disabled cards never get a weight, even with a cost.
    assert "weight" not in weighted.get("/v1/cards/the-cartographer").json()


def test_sort_and_filter_by_weight(weighted):
    assert slugs(weighted.get("/v1/cards", params={"sort": "weight", "order": "desc"})) == [
        "the-fiend", "the-doctor", "house-of-mirrors"]
    # Weights: The Fiend ~6.46, The Doctor ~2.01, House of Mirrors ~1.60.
    assert slugs(weighted.get("/v1/cards", params={"weight_max": 1.7})) == ["house-of-mirrors"]
    assert slugs(weighted.get("/v1/cards", params={"weight_min": 5})) == ["the-fiend"]


def test_meta_reports_costs_and_problems(weighted):
    c = weighted.get("/v1/meta").json()["costs"]
    assert c["league"] == "Test League"
    assert (c["cards_with_cost"], c["cards_expected"]) == (3, 3)
    assert c["problems"] == ["the-cartographer: disabled cards have no gold cost; it is ignored"]


def test_costs_change_the_etag(client, weighted):
    assert client.get("/v1/cards").headers["etag"] != weighted.get("/v1/cards").headers["etag"]


def test_root_redirects_to_the_docs(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/docs"


def test_export_files(client):
    cards = client.get("/export/cards.json")
    assert cards.status_code == 200 and "etag" in cards.headers
    body = cards.json()
    assert body["meta"]["label"] == "test"
    # Every card, disabled ones included, with every field.
    assert [c["slug"] for c in body["cards"]] == ["the-doctor", "house-of-mirrors", "the-fiend", "the-cartographer"]
    assert body["cards"][0]["text"]["name"] == "The Doctor" and body["cards"][0]["art"] == "/images/cards/the-doctor.webp"
    basic = client.get("/export/basic_cards.json").json()["cards"]
    assert len(basic) == 4 and "text" not in basic[0] and basic[0]["reward"]["text"] == "Headhunter"
    areas = client.get("/export/areas.json").json()["areas"]
    assert [a["id"] for a in areas][:2] == ["MapWorldsFoundry", "MapWorldsBurialChambers"]


def test_openapi_documents_the_public_api(client):
    spec = client.get("/openapi.json").json()
    assert "### Export files" in spec["info"]["description"]
    assert {t["name"] for t in spec["tags"]} == {"cards", "areas", "export", "info"}
    params = {p["name"] for p in spec["paths"]["/v1/cards"]["get"]["parameters"]}
    assert "lang" not in params and "reward_item" in params        # English-only: lang is hidden
    assert spec["paths"]["/v1/cards/{slug}"]["get"]["summary"] == "Get a card"
    assert "servers" not in spec or spec["servers"] in ([], [{"url": "/"}])


def test_public_url_sets_the_server_and_absolute_image_urls(dataset_dir):
    app = create_app(dataset_dir, public_url="https://divcards.example/", source_url="https://github.com/x/y")
    c = TestClient(app)
    spec = c.get("/openapi.json").json()
    assert spec["servers"] == [{"url": "https://divcards.example"}]
    assert "https://github.com/x/y" in spec["info"]["description"]
    assert c.get("/v1/cards/the-doctor").json()["art"] == "https://divcards.example/images/cards/the-doctor.webp"
