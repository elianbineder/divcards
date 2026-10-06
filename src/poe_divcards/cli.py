"""Command line interface: ``poe-divcards build | serve``."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    origins = [o.strip() for o in args.cors.split(",") if o.strip()] if args.cors else None
    app = create_app(args.dataset, origins, args.image_base_url, args.costs, public_url=args.public_url)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_build(args) -> int:
    from .build import BuildError, main_build

    try:
        return main_build(args)
    except BuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_costs_template(args) -> int:
    import json
    from pathlib import Path

    from .dataset import Dataset
    from .weights import template, template_tsv

    out = Path(args.output)
    if out.exists() and not args.force:
        print(f"Error: {out} exists (use --force to overwrite)", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    cards = Dataset(args.dataset).cards
    if out.suffix.lower() == ".tsv":
        # Cost list for a spreadsheet, imported later with `costs import`.
        out.write_text(template_tsv(cards, args.league, Path(args.dataset).as_posix()), encoding="utf-8", newline="\n")
        count = sum(1 for c in cards if c.get("enabled") is not False)
    else:
        data = template(cards, args.league)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        count = len(data["costs"])
    print(f"{out}: {count} cards to fill in")
    return 0


def cmd_costs_import(args) -> int:
    import json
    from pathlib import Path

    from .dataset import Dataset
    from .weights import Costs, import_cost_list

    out = Path(args.output)
    if out.exists() and not args.force:
        print(f"Error: {out} exists (use --force to overwrite)", file=sys.stderr)
        return 1
    cards = Dataset(args.dataset).cards
    result = import_cost_list(Path(args.list).read_text(encoding="utf-8-sig"), cards, args.league,
                              args.updated_at)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for label, items in (("rounded to its step", result.rounded),
                         ("not a card", result.unmatched), ("disabled card, skipped", result.disabled),
                         ("conflicting costs", result.conflicts), ("unreadable line", result.invalid)):
        for item in items:
            print(f"{label}: {item}")
    problems = Costs.load(out).check(cards)
    for p in problems:
        print(f"problem: {p}")
    missing = sum(1 for v in result.data["costs"].values() if v is None)
    print(f"{out}: {result.imported} costs imported, {missing} enabled cards without cost"
          f"{f' ({result.pending} listed with the cost still empty)' if result.pending else ''}, "
          f"{len(result.unmatched) + len(result.conflicts) + len(result.invalid) + len(problems)} issues")
    return 1 if result.unmatched or result.conflicts or result.invalid or problems else 0


def cmd_costs_check(args) -> int:
    from .dataset import Dataset
    from .weights import Costs, CostsError

    try:
        costs = Costs.load(args.costs)
    except (CostsError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    cards = Dataset(args.dataset).cards
    problems = costs.check(cards)
    missing = costs.missing(cards)
    for note in costs.roundings(cards):
        print(f"note: rounded to its step when computing the weight: {note}")
    for p in problems:
        print(f"problem: {p}")
    filled = sum(1 for c in cards if c.get("enabled") is not False) - len(missing)
    print(f"league '{costs.league}': {filled} cards with cost, {len(missing)} without, {len(problems)} problems")
    if args.list_missing and missing:
        print("missing: " + ", ".join(missing))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poe-divcards", description="Divination card dataset and API")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Build a dataset from the game files")
    b.add_argument("--ggpk", help="Content.ggpk, game folder or _.index.bin (default: autodetect / POE_GGPK)")
    b.add_argument("--index", help="Reconstructed index (poe-ggpk reconstruct) for an index-less GGPK")
    b.add_argument("--schema", help="dat-schema file (default: cached download)")
    b.add_argument("-o", "--output", default="datasets", help="Parent directory of the dataset (default: datasets)")
    b.add_argument("--label", help="Dataset name, e.g. 3.29.3.3 (default: poe1-<date>-<index hash>)")
    b.add_argument("--lang", nargs="+", help="Language codes to include (default: en)")
    b.add_argument("--no-images", action="store_true", help="Skip converting card art and icons")
    b.add_argument("--overrides", help="JSON file with manual corrections (default: overrides.json if present)")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("serve", help="Serve a dataset over HTTP")
    s.add_argument("dataset", help="Dataset directory (datasets/<label>)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--cors", help="Comma-separated allowed origins (default: *)")
    s.add_argument("--public-url", default="", help="Public address of the API (docs server, image URLs)")
    s.add_argument("--image-base-url", default="", help="Prefix for image URLs when they live elsewhere, e.g. a CDN")
    s.add_argument("--costs", help="Gold costs file (costs/<league>.json) to add weights")
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("costs", help="Manage the per-league gold costs used for weights")
    csub = c.add_subparsers(dest="costs_command", required=True)
    t = csub.add_parser("template", help="Write an empty costs file with every enabled card")
    t.add_argument("dataset", help="Dataset directory")
    t.add_argument("-o", "--output", required=True,
                   help="Output file: costs/<league>.tsv (list to fill in, for `costs import`) or .json")
    t.add_argument("--league", default="", help="League name")
    t.add_argument("--force", action="store_true", help="Overwrite an existing file")
    t.set_defaults(func=cmd_costs_template)
    i = csub.add_parser("import", help="Build a costs file from a '<cost> <name>' list")
    i.add_argument("dataset", help="Dataset directory")
    i.add_argument("list", help="Text file, one card per line: '<cost> <name>' or '<name> <cost>'")
    i.add_argument("-o", "--output", required=True, help="Output file, e.g. costs/<league>.json")
    i.add_argument("--league", default="", help="League name")
    i.add_argument("--updated-at", help="Date the costs were collected (YYYY-MM-DD)")
    i.add_argument("--force", action="store_true", help="Overwrite an existing file")
    i.set_defaults(func=cmd_costs_import)
    k = csub.add_parser("check", help="Validate a costs file against a dataset")
    k.add_argument("dataset", help="Dataset directory")
    k.add_argument("costs", help="Costs file")
    k.add_argument("--list-missing", action="store_true", help="List the cards without a cost")
    k.set_defaults(func=cmd_costs_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
