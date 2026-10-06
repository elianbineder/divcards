"""Read-only HTTP API (FastAPI) serving a built dataset to any client.

The API documents itself: the text below is the description shown at ``/docs`` and
``/redoc``, and every endpoint and field carries its own description.

Endpoints (all GET):

* ``/v1/cards``, ``/v1/cards/{slug}``   search cards, get one card
* ``/v1/areas``                         atlas areas and the cards they drop
* ``/v1/meta``, ``/v1/facets``          data version, values for filters
* ``/export/*.json``                    every card in one file
* ``/images/...``                       card art, reward icons and card frame (WebP)
* ``/health``                           liveness probe; ``/`` redirects to ``/docs``

Responses carry the data version as ETag, so clients revalidate cheaply and get
``304 Not Modified`` until a new dataset or costs file is deployed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .dataset import SORT_KEYS, Dataset, DatasetError
from .dataset import Query as CardQuery

MAX_LIMIT = 500
CACHED_PATHS = ("/v1/", "/export/")

DESCRIPTION = """\
Data on every **Path of Exile divination card**, extracted from the game files: names,
rewards and flavour texts exactly as the game writes them, card art, the atlas maps that
drop each card and estimated drop weights for the current league.

Read-only, no authentication, JSON. Unofficial fan project, not affiliated with or endorsed
by Grinding Gear Games.

### Export files

Pre-generated files with every card, best for bulk use (one request instead of many):

- [All cards](/export/cards.json): every field of every card, including drop-disabled cards.
- [Basic cards](/export/basic_cards.json): a lightweight summary of every card, for indexing
  and search.
- [Atlas areas](/export/areas.json): the atlas maps that drop cards, with their card slugs.

### API usage

- HTTPS only: plain HTTP requests are redirected (`308`) to the same URL over HTTPS.
- Use the export files for bulk data and `/v1/cards` to search and filter; `limit` goes up
  to 500, so the whole list fits in one request.
- Cards are identified by `slug` (`the-doctor`), atlas areas by the game `id`
  (`MapWorldsBurialChambers`).
- Responses carry an `ETag`: send it back in `If-None-Match` to get `304 Not Modified` until
  the data changes, usually at the start of a league.
- Errors return a JSON body with `detail`: `404` unknown card, `400` unknown area, sort key
  or query parameter, `422` invalid parameter value.
- Image fields (`art`, `icon`, `assets.frame`) point to WebP files. The card art (389×279)
  goes behind the official card frame (439×670), whose window is at (33, 62)–(413, 328).
- New fields and endpoints may be added within `/v1`; ignore fields you do not know.
  Breaking changes would use a new version path.

### Data notes

- **Rewards**: `reward.name` is the reward exactly as the game writes it ("Six-Link Astral
  Plate"). `reward.item` links it to a concrete item when there is one (unique, base item,
  gem, currency or another card); categories such as "Level 21 Spell Gem" have
  `item: null`. Extra reward lines become `properties` (`item_level`, `quality`, `links`,
  `level`…) and `flags` ("Two-Implicit", "Shaper Item").
- **Styled texts**: `text.reward` and `text.flavour` are lines of segments carrying the
  game's text `style` (`uniqueitem`, `currencyitem`, `corrupted`…); `reward_text` and
  `flavour_text` are the same texts without formatting. A few texts include inline images
  (The Messenger's Harbinger glyphs): segments with `glyph` and an `image` URL.
- **Drops**: `drops.atlas` lists the atlas maps that drop the card (`tier` is a number, or
  `"Unique"` for unique maps). Cards that drop in no atlas map are **Non-Scryable**
  (`scryable: false`).
- **Disabled cards** (`enabled: false`) cannot currently drop. Search, areas and facets
  leave them out unless `include_disabled=true`.
- **Weights** estimate how common a card is (higher is more common) from the gold cost
  Faustus charges for it in the current league: `10^6 / cost` below 125 gold,
  `13·10^9 / cost^3` from 125. Faustus rounds costs down, so `value` is the mean of the
  weight at the displayed cost (`max`) and at the cost + 25 (`min`). They are estimates,
  not official drop rates.

### Miscellaneous

- Data version, league of the gold costs and counts: [/v1/meta](/v1/meta).
- Documentation styles: [Swagger UI](/docs), [ReDoc](/redoc).
"""

TAGS = [
    {"name": "cards", "description": "Search divination cards and get one by slug."},
    {"name": "areas", "description": "Atlas maps that drop divination cards."},
    {"name": "export", "description": "Pre-generated files with every card, for bulk use."},
    {"name": "info", "description": "Data version and the values available for filters."},
]


# -- response models (documented in /docs and /openapi.json) ------------------------
class Segment(BaseModel):
    """A run of text with one in-game style."""
    text: str
    style: str | None = Field(None, description="In-game text style: uniqueitem, currencyitem, gemitem, "
                                                "divination, rareitem, magicitem, normal, whiteitem, "
                                                "corrupted, augmented, default, enchanted, fractured…")
    size: int | None = Field(None, description="In-game font size, for long texts")
    glyph: str | None = Field(None, description="Inline image drawn by the game in place of text, e.g. a "
                                                "Harbinger-script glyph; text is empty")
    image: str | None = Field(None, description="Image (WebP) of the glyph")


class RewardItem(BaseModel):
    """Concrete item the reward refers to."""
    type: str = Field(description="unique, base (base item, gem or currency), card (another divination "
                                  "card), or a manually defined type such as currency")
    slug: str = Field(description="Item slug; for type card, the slug of that card")
    name: str | None = Field(None, description="Item name in game")
    id: str | None = Field(None, description="Game id of base items")
    category: str | None = Field(None, description="Category of manually defined items, e.g. Endgame Currency")
    icon: str | None = Field(None, description="Icon image (WebP)")


class Reward(BaseModel):
    kind: str = Field(description="unique, currency, gem, divination, rare, magic, normal or other")
    name: str | None = Field(None, description="Reward exactly as written in game, without the quantity")
    quantity: int
    corrupted: bool
    properties: dict[str, Any] = Field(default_factory=dict,
                                       description="Values from the reward text and name: item_level, "
                                                   "quality, map_tier, modifiers, links, level…")
    flags: list[str] = Field(default_factory=list,
                             description="Other reward lines verbatim, e.g. Two-Implicit, Shaper Item")
    item: RewardItem | None = Field(None, description="Concrete item, or null for category rewards")
    text: str | None = Field(None, description="Whole reward text without formatting (card lists only)")


class Area(BaseModel):
    id: str = Field(description="Game id of the area, e.g. MapWorldsBurialChambers")
    name: str | None = None
    names: dict[str, str] | None = Field(None, description="Names per language (lang=all only)")
    tier: int | Literal["Unique"] | None = Field(None, description='Map tier, or "Unique" for unique maps')
    unique_map: bool | None = None
    on_atlas: bool | None = Field(None, description="false for maps not placed on the Atlas")
    area_level: int | None = None


class AreaWithCards(Area):
    cards: list[str] = Field(description="Slugs of the cards this area drops")


class Drops(BaseModel):
    atlas: list[Area] = Field(description="Atlas maps whose drop list includes the card")


class Weight(BaseModel):
    """Estimated drop weight from the gold cost Faustus charges (higher is more common)."""
    gold_cost: int = Field(description="Gold cost shown by Faustus")
    formula: Literal["common", "uncommon_rare"] = Field(
        description="common: 10^6 / cost (below 125); uncommon_rare: 13·10^9 / cost^3")
    value: float = Field(description="Mean of min and max: the weight to use")
    min: float = Field(description="Weight at gold_cost + 25")
    max: float = Field(description="Weight at gold_cost")


class CostsInfo(BaseModel):
    league: str = Field(description="League of the gold costs")
    updated_at: str | None = Field(description="Date the gold costs were collected")
    cards_with_cost: int
    cards_expected: int = Field(description="Enabled cards")
    problems: list[str]


class CardSummary(BaseModel):
    slug: str
    name: str
    stack_size: int | None
    drop_level: int | None
    enabled: bool | None = Field(description="false: the card cannot currently drop")
    art: str | None = Field(description="Card art (WebP, 389×279)")
    scryable: bool = Field(description="false: Non-Scryable, drops in no atlas map")
    atlas_areas: int = Field(description="Number of atlas maps that drop the card")
    weight: Weight | None = Field(None, description="Missing when the card has no gold cost")
    reward: Reward


class CardPage(BaseModel):
    total: int = Field(description="Cards matching the filters")
    offset: int
    limit: int
    lang: str
    items: list[CardSummary]


class CardText(BaseModel):
    name: str
    reward: list[list[Segment]] = Field(description="Reward text: lines of styled segments")
    reward_text: str
    flavour: list[list[Segment]] = Field(description="Flavour text: lines of styled segments")
    flavour_text: str


class CardDetail(BaseModel):
    slug: str = Field(description="Card identifier, derived from its English name")
    id: str = Field(description="Game id of the card")
    stack_size: int | None
    drop_level: int | None
    stash_order: int | None = Field(description="Position in the in-game divination stash tab")
    in_game: bool | None
    enabled: bool | None = Field(description="false: the card cannot currently drop")
    tags: list[str]
    art: str | None = Field(description="Card art (WebP, 389×279)")
    drops: Drops
    scryable: bool = Field(description="false: Non-Scryable, drops in no atlas map")
    weight: Weight | None = Field(None, description="Missing when the card has no gold cost")
    reward: Reward
    overridden: bool | None = Field(None, description="true when part of the card was corrected by hand")
    lang: str | None = None
    text: CardText | None = Field(None, description="Name, reward and flavour texts")
    i18n: dict[str, CardText] | None = Field(None, description="Texts per language (lang=all only)")


class Meta(BaseModel):
    label: str = Field(description="Game patch of the data")
    version: str = Field(description="Changes whenever the data or the gold costs change (also the ETag)")
    built_at: str
    languages: dict[str, str]
    assets: dict[str, str] = Field(description="Shared images: frame (official card frame, 439×670), favicon (card icon)")
    costs: CostsInfo | None = Field(None, description="Gold costs in use; null when there are no weights")
    counts: dict[str, int]
    source: dict[str, Any]
    api_version: str


class Facets(BaseModel):
    total: int
    reward_kinds: dict[str, int]
    tags: dict[str, int]
    stack_size: dict[str, int | None]
    corrupted_rewards: int
    scryable: int
    non_scryable: int


# Hidden from the schema: the public data is English-only.
LANG_PARAM = Query("en", include_in_schema=False)


# -- application -------------------------------------------------------------------
_route_params: dict[int, frozenset[str]] = {}


def _query_param_names(dependant: Any) -> set[str]:
    """Query parameters of an endpoint and of its dependencies."""
    names = {p.alias for p in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_param_names(sub)
    return names


def known_query_params(request: Request) -> None:
    """Reject query parameters the endpoint does not take: an unknown parameter would make
    every request a different URL, bypassing the caches in front of the API."""
    route = request.scope.get("route")
    if route is None or not hasattr(route, "dependant"):
        return
    names = _route_params.get(id(route))
    if names is None:
        names = _route_params[id(route)] = frozenset(_query_param_names(route.dependant))
    unknown = sorted(set(request.query_params) - names)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown query parameter: {', '.join(unknown)}")


# -- application -------------------------------------------------------------------
def create_app(dataset: Dataset | str | os.PathLike[str], cors_origins: list[str] | None = None,
               image_base_url: str = "", costs: str | os.PathLike[str] | None = None,
               public_url: str = "", source_url: str = "",
               allowed_hosts: list[str] | None = None) -> FastAPI:
    """``public_url``: the API's public address, shown as the server in the docs and used
    as the image URL prefix unless ``image_base_url`` (e.g. a CDN) is given; without either,
    image URLs are paths on this host. ``costs``: gold costs file adding weights.
    ``source_url``: source code repository, linked from the docs. ``allowed_hosts``: host
    names the API answers to (any when None); others get 400, e.g. the hosting platform's
    own address, which would bypass a CDN in front of the public one."""
    ds = dataset if isinstance(dataset, Dataset) else Dataset(dataset, costs)
    etag = f'W/"{ds.version}"'
    public_url = public_url.rstrip("/")
    base = (image_base_url or public_url).rstrip("/")
    image_prefix = base + "/" if base else "/"

    description = DESCRIPTION
    if source_url:
        description += f"- Source code: [{source_url}]({source_url}).\n"
    app = FastAPI(
        title="Divination Cards API",
        version=__version__,
        summary="Path of Exile divination card data: rewards, art, atlas drops and drop weights.",
        description=description,
        openapi_tags=TAGS,
        servers=[{"url": public_url}] if public_url else None,
        license_info={"name": "MIT (source code)", "identifier": "MIT"},
        docs_url=None,   # served below with the dataset's favicon
        redoc_url=None,
        dependencies=[Depends(known_query_params)],
    )
    app.state.dataset = ds
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else ["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
        expose_headers=["ETag"],
    )

    @app.middleware("http")
    async def caching(request: Request, call_next):
        path = request.url.path
        if request.method == "GET" and path.startswith(CACHED_PATHS):
            if etag in request.headers.get("if-none-match", ""):
                return Response(status_code=304, headers={"ETag": etag})
            response = await call_next(request)
            if response.status_code == 200:
                response.headers["ETag"] = etag
                response.headers["Cache-Control"] = "public, max-age=300"
            return response
        response = await call_next(request)
        if path.startswith("/images/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.middleware("http")
    async def https_only(request: Request, call_next):
        """Redirect plain HTTP to HTTPS behind a proxy that reports the client's scheme in
        X-Forwarded-Proto (Heroku's router). Without that header (local runs) nothing changes."""
        if request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "http":
            host = request.headers.get("host", request.url.netloc).removesuffix(":80")
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"https://{host}{request.url.path}{query}", status_code=308)
        return await call_next(request)

    # Added last so it runs first: a request for another host is rejected before anything else.
    if allowed_hosts is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)

    @app.exception_handler(DatasetError)
    async def dataset_error(request: Request, exc: DatasetError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def urls(obj: Any) -> Any:
        """Turn the dataset's relative image paths into URLs."""
        if isinstance(obj, dict):
            return {k: (image_prefix + v if k in ("art", "icon", "frame", "favicon", "image") and isinstance(v, str) else urls(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [urls(v) for v in obj]
        return obj

    def meta_dict() -> dict[str, Any]:
        m = ds.manifest
        return {
            "label": m["label"], "version": ds.version, "built_at": m["built_at"],
            "languages": ds.languages, "assets": urls(m.get("assets", {})), "costs": ds.costs_info(),
            "counts": m["counts"], "source": m.get("source", {}),
            "api_version": __version__,
        }

    def without_none(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: without_none(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [without_none(v) for v in obj]
        return obj

    exports: dict[str, bytes] = {}

    def export(name: str) -> Response:
        """Export files are built on first request and kept in memory."""
        if name not in exports:
            if name == "cards.json":
                data = {"meta": meta_dict(), "cards": [urls(ds.detail(c, "en")) for c in ds.cards]}
            elif name == "basic_cards.json":
                data = {"meta": meta_dict(), "cards": [urls(ds.summary(c, "en")) for c in ds.cards]}
            else:
                data = {"meta": meta_dict(), "areas": ds.area_list("en")}
            exports[name] = json.dumps(without_none(data), ensure_ascii=False, separators=(",", ":")).encode()
        return Response(exports[name], media_type="application/json")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    # Favicon: the divination card icon from the dataset (FastAPI's own icon if it has none).
    favicon_rel = ds.manifest.get("assets", {}).get("favicon")
    favicon = ds.root / favicon_rel if favicon_rel else None
    icon = {"swagger_favicon_url": "/favicon.ico"} if favicon and favicon.is_file() else {}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico():
        if not icon:
            raise HTTPException(status_code=404, detail="No favicon")
        return FileResponse(favicon, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/docs", include_in_schema=False)
    def swagger_ui():
        return get_swagger_ui_html(openapi_url=app.openapi_url, title=app.title, **icon)

    @app.get("/redoc", include_in_schema=False)
    def redoc():
        return get_redoc_html(openapi_url=app.openapi_url, title=app.title,
                              **({"redoc_favicon_url": "/favicon.ico"} if icon else {}))

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "dataset": ds.manifest.get("label")}

    summaries: dict[str, dict[str, str]] = {}

    def summary_json(slug: str, lang: str) -> str:
        """A card summary serialized once per language through its response model, so
        pages are joined from ready JSON instead of validating every card per request."""
        if lang not in summaries:
            summaries[lang] = {
                c["slug"]: CardSummary.model_validate(urls(ds.summary(c, lang))).model_dump_json(exclude_none=True)
                for c in ds.cards
            }
        return summaries[lang][slug]

    for code in ds.languages if ds.cards else ():
        summary_json(ds.cards[0]["slug"], code)   # built at startup, not on the first request

    @app.get("/v1/cards", response_model=CardPage, response_model_exclude_none=True, tags=["cards"],
             summary="Search cards")
    def list_cards(
        lang: str = LANG_PARAM,
        q: str | None = Query(None, description="Words to find in the name, reward, flavour text or "
                                                "linked item (case- and accent-insensitive)"),
        kind: list[str] = Query([], description="Reward kind, repeatable: unique, currency, gem, divination, "
                                                "rare, magic, normal, other"),
        tag: list[str] = Query([], description="Card tag, repeatable, e.g. unique_divination"),
        reward_item: str | None = Query(None, description="Slug of the rewarded item, e.g. headhunter"),
        area: str | None = Query(None, description="Atlas area id that drops the card, e.g. MapWorldsBurialChambers"),
        scryable: bool | None = Query(None, description="true: cards that drop in atlas maps; "
                                                         "false: Non-Scryable cards"),
        corrupted: bool | None = Query(None, description="Only cards whose reward is (or is not) corrupted"),
        stack_min: int | None = Query(None, ge=1, description="Minimum stack size"),
        stack_max: int | None = Query(None, ge=1, description="Maximum stack size"),
        weight_min: float | None = Query(None, ge=0, description="Minimum weight (cards without one are excluded)"),
        weight_max: float | None = Query(None, ge=0, description="Maximum weight (cards without one are excluded)"),
        include_disabled: bool = Query(False, description="Include cards that cannot currently drop"),
        sort: str = Query("stash_order", description=f"One of: {', '.join(SORT_KEYS)}"),
        order: Literal["asc", "desc"] = Query("asc", description="Cards without a value for the sort key "
                                                                  "always come last"),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=MAX_LIMIT, description=f"Up to {MAX_LIMIT}, enough for every card"),
    ):
        """Search, filter, sort and paginate the cards. Returns card summaries; use
        `/v1/cards/{slug}` for every field, or the export files for everything at once.

        Examples: `?reward_item=headhunter` (cards that give Headhunter),
        `?area=MapWorldsBurialChambers` (cards that drop in Burial Chambers),
        `?scryable=false&sort=weight&order=desc` (most common Non-Scryable cards)."""
        code = ds.language(lang)
        total, items = ds.search(CardQuery(
            lang=code, q=q, kinds=tuple(kind), tags=tuple(tag), reward_item=reward_item,
            area=area, scryable=scryable,
            corrupted=corrupted, stack_min=stack_min, stack_max=stack_max,
            weight_min=weight_min, weight_max=weight_max,
            include_disabled=include_disabled, sort=sort, descending=order == "desc",
            offset=offset, limit=limit,
        ))
        body = (f'{{"total":{total},"offset":{offset},"limit":{limit},"lang":{json.dumps(code)},"items":['
                + ",".join(summary_json(item["slug"], code) for item in items) + "]}")
        return Response(body, media_type="application/json")

    @app.get("/v1/cards/{slug}", response_model=CardDetail, response_model_exclude_none=True, tags=["cards"],
             summary="Get a card", responses={404: {"description": "Unknown card slug"}})
    def get_card(slug: str, lang: str = Query("en", include_in_schema=False)):
        """Every field of one card, including drop-disabled cards. The slug is the card's
        English name in lowercase with dashes, e.g. `the-doctor`, `house-of-mirrors`."""
        card = ds.get(slug)
        if card is None:
            raise HTTPException(status_code=404, detail=f"Card '{slug}' not found")
        code = None if lang.lower() == "all" else ds.language(lang)
        return urls(ds.detail(card, code))

    @app.get("/v1/areas", response_model=list[AreaWithCards], response_model_exclude_none=True,
             tags=["areas"], summary="List atlas areas")
    def areas(lang: str = LANG_PARAM,
              include_disabled: bool = Query(False, description="Also list cards that cannot currently drop")):
        """Atlas maps that drop divination cards, tiered maps first (by tier) and then
        unique maps, each with the slugs of the cards it drops."""
        return ds.area_list(ds.language(lang), include_disabled)

    @app.get("/v1/meta", response_model=Meta, tags=["info"], summary="Data version")
    def meta():
        """Game patch of the data, its version (the ETag), counts, shared assets such as the
        card frame, and the league of the gold costs used for the weights."""
        return meta_dict()

    @app.get("/v1/facets", response_model=Facets, tags=["info"], summary="Filter values")
    def facets(include_disabled: bool = Query(False, description="Count cards that cannot currently drop")):
        """Values available for filtering, with how many cards have each: reward kinds, tags,
        stack size range, corrupted rewards and Scryable / Non-Scryable cards."""
        return ds.facets(include_disabled)

    @app.get("/export/cards.json", tags=["export"], summary="All cards",
             response_description="`{\"meta\": {...}, \"cards\": [CardDetail, ...]}`")
    def export_cards():
        """Every field of every card (the same as `/v1/cards/{slug}`), including
        drop-disabled cards, with the data version in `meta`."""
        return export("cards.json")

    @app.get("/export/basic_cards.json", tags=["export"], summary="Basic cards",
             response_description="`{\"meta\": {...}, \"cards\": [CardSummary, ...]}`")
    def export_basic_cards():
        """A lightweight summary of every card (the same as the items of `/v1/cards`),
        including drop-disabled cards, for indexing and search."""
        return export("basic_cards.json")

    @app.get("/export/areas.json", tags=["export"], summary="Atlas areas",
             response_description="`{\"meta\": {...}, \"areas\": [AreaWithCards, ...]}`")
    def export_areas():
        """Every atlas map that drops cards, with the slugs of its cards (the same as `/v1/areas`)."""
        return export("areas.json")

    images = ds.root / "images"
    if images.is_dir():
        app.mount("/images", StaticFiles(directory=images), name="images")
    return app


def app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory poe_divcards.api:app_from_env``.

    * ``POE_DIVCARDS_DATASET``: dataset directory (required).
    * ``POE_DIVCARDS_COSTS``: gold costs file (optional; without it there are no weights).
    * ``POE_DIVCARDS_PUBLIC_URL``: public address of the API (docs server, image URLs).
    * ``POE_DIVCARDS_IMAGE_BASE_URL``: prefix for image URLs when they live elsewhere (a CDN).
    * ``POE_DIVCARDS_CORS``: comma-separated allowed origins (default ``*``).
    * ``POE_DIVCARDS_SOURCE_URL``: source code repository linked from the docs.
    * ``POE_DIVCARDS_ALLOWED_HOSTS``: comma-separated host names the API answers to (default any).
    """
    path = os.environ.get("POE_DIVCARDS_DATASET")
    if not path:
        raise RuntimeError("Set POE_DIVCARDS_DATASET to the dataset directory")
    cors = os.environ.get("POE_DIVCARDS_CORS")
    origins = [o.strip() for o in cors.split(",") if o.strip()] if cors else None
    hosts_env = os.environ.get("POE_DIVCARDS_ALLOWED_HOSTS")
    hosts = [h.strip() for h in hosts_env.split(",") if h.strip()] if hosts_env else None
    return create_app(Path(path), origins, os.environ.get("POE_DIVCARDS_IMAGE_BASE_URL", ""),
                      os.environ.get("POE_DIVCARDS_COSTS") or None,
                      public_url=os.environ.get("POE_DIVCARDS_PUBLIC_URL", ""),
                      source_url=os.environ.get("POE_DIVCARDS_SOURCE_URL", ""),
                      allowed_hosts=hosts)
