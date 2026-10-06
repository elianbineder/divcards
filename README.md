# DivCards

HTTP API for **Path of Exile Divination Card** data: names, rewards and flavour texts
exactly as the game writes them, card art, the atlas maps that drop each card and
estimated drop weights for the current league.

The API and its documentation are available at `https://api.divcards.app`:
[Swagger UI](https://api.divcards.app/docs), [ReDoc](https://api.divcards.app/redoc), and
export files with every card
([`/export/cards.json`](https://api.divcards.app/export/cards.json),
[`/export/basic_cards.json`](https://api.divcards.app/export/basic_cards.json),
[`/export/areas.json`](https://api.divcards.app/export/areas.json)).

The data is extracted from the game files with
[poe-ggpk-extractor](https://github.com/elianbineder/poe-ggpk-extractor).

> Unofficial fan-made project, not affiliated with or endorsed by Grinding Gear Games.

## Configuration

The API server is configured through environment variables. Only the dataset is required:

| Variable | Description |
|---|---|
| `POE_DIVCARDS_DATASET` | dataset directory, built with `poe-divcards build` |

<details>
<summary>Optional variables</summary>

| Variable | Default | Description |
|---|---|---|
| `POE_DIVCARDS_COSTS` | none | gold costs file of the current league (`costs/<league>.json`); without it the API serves no weights |
| `POE_DIVCARDS_PUBLIC_URL` | none | public address of the API: shown as the server in the docs and used as the prefix of image URLs |
| `POE_DIVCARDS_SOURCE_URL` | none | source code repository, linked from the docs |
| `POE_DIVCARDS_CORS` | `*` | comma-separated origins allowed to call the API from browsers |
| `POE_DIVCARDS_IMAGE_BASE_URL` | `POE_DIVCARDS_PUBLIC_URL` | prefix of image URLs when the images are served elsewhere (a CDN) |

</details>

<details>
<summary>Build options</summary>

`poe-divcards build` reads the game installation, which is detected automatically:

| Option | Default | Description |
|---|---|---|
| `--ggpk` | `POE_GGPK` or autodetected | `Content.ggpk`, game folder or `_.index.bin` |
| `--index` | none | reconstructed index for a pre-league torrent GGPK (`poe-ggpk reconstruct`) |
| `-o` | `datasets` | parent directory of the dataset |
| `--label` | `poe1-<date>-<index hash>` | dataset name; use the game patch |
| `--overrides` | `overrides.json` | manual corrections |
| `--no-images` | off | list image paths without writing the files (quick data checks) |

</details>

## Development environment set up

Prerequisites:

- Python 3.10+.
- To build datasets: Windows with Path of Exile installed, and a checkout of
  [poe-ggpk-extractor](https://github.com/elianbineder/poe-ggpk-extractor) next to this
  repository.

```bash
git clone https://github.com/elianbineder/poe-ggpk-extractor
git clone https://github.com/elianbineder/divcards
cd divcards
python -m venv .venv
.venv\Scripts\activate
pip install -e ../poe-ggpk-extractor -e ".[build,dev]"
```

To work only on the API server, without building datasets, `pip install -e ".[dev]"` is
enough, given a dataset folder built elsewhere.

Build the dataset of the installed game version:

```bash
poe-divcards build --label 3.29.3.3
```

It is written to `datasets/3.29.3.3/` (`cards.json`, `areas.json`, `manifest.json` and WebP
images); `manifest.json` lists in `warnings` what could not be read. Datasets contain game
content and are not committed.

## Run the API server

```bash
poe-divcards serve datasets/3.29.3.3 --costs costs/3.29.json
```

The documentation is then at http://127.0.0.1:8000. For development, uvicorn can reload on
code changes:

```bash
set POE_DIVCARDS_DATASET=datasets/3.29.3.3
set POE_DIVCARDS_COSTS=costs/3.29.json
uvicorn --factory poe_divcards.api:app_from_env --reload
```

(In PowerShell, set the variables with `$env:POE_DIVCARDS_DATASET = "datasets/3.29.3.3"`.)

## Architecture

```
src/poe_divcards/
    api.py        FastAPI app: endpoints, response models and the documentation text
    dataset.py    loads a dataset and runs the queries (search, filters, areas, facets)
    weights.py    gold costs files and the weight formulas
    build.py      builds a dataset from the game files (needs poe-ggpk-extractor)
    markup.py     parses the game's text markup into styled segments
    cli.py        `poe-divcards` command: build, serve, costs
costs/            gold costs of each league: <league>.tsv (filled in by hand) and <league>.json
overrides.json    manual corrections applied by the build
tests/            pytest suite
```

The server never reads the game: everything it needs is in the dataset folder and the
costs file, so it runs without the extractor or the GPL Oodle decompressor.

## Dependencies

Runtime dependencies are declared in `pyproject.toml` (`fastapi`, `uvicorn`); the extras
`build` (extractor, Pillow) and `dev` (pytest, httpx2) are only for development.

## Testing

```bash
pytest
```

The API tests use a small synthetic dataset. `tests/test_build.py` builds from the installed
game and is skipped when it is missing.

## Helper scripts

| Command | Description |
|---|---|
| `poe-divcards build` | build a dataset from the game files |
| `poe-divcards serve` | run the API server |
| `poe-divcards costs template` | write the list of cards to fill in with the league's gold costs |
| `poe-divcards costs import` | import a filled-in list into `costs/<league>.json` |
| `poe-divcards costs check` | validate a costs file against a dataset |

## Updating the data

**Each league**, the gold costs (they cannot be datamined):

```bash
poe-divcards costs template datasets/<patch> -o costs/<league>.tsv --league "<league>"
# fill in the cost Faustus shows before each name (<cost><TAB><name>)
poe-divcards costs import datasets/<patch> costs/<league>.tsv -o costs/<league>.json --league "<league>" --updated-at <YYYY-MM-DD>
poe-divcards costs check datasets/<patch> costs/<league>.json --list-missing
```

Enter costs exactly as Faustus shows them; off-step values (670) are rounded and reported.
Commit both files.

**After a game patch** with data changes, rebuild the dataset (`poe-divcards build --label
<patch>`), check `warnings` in its `manifest.json` and add the costs of any new cards. If
the build fails at the start of a league, the community dat-schema may not be updated yet.

Review `overrides.json` after each patch: it disables cards the game data does not mark as
disabled (they may have been re-enabled) and defines reward items missing from the game
tables. Card slugs come from the English names, so a renamed card gets a new slug.

## License

The source code is released under the [MIT License](LICENSE). Card names, texts and artwork
are the property of Grinding Gear Games; this repository contains no game content.
