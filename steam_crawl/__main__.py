import argparse
import json
import os
import re
import sqlite3
import sys

from .http import Client, SourceError
from .sources import Collector, app_id, import_steamdb
from .storage import Store


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect public Steam game data")
    parser.add_argument("--db", default="data/steam.sqlite3")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, choices=range(6), default=2)
    commands = parser.add_subparsers(dest="command", required=True)
    crawl = commands.add_parser("crawl", help="collect explicit public game IDs")
    crawl.add_argument("appids", type=app_id, nargs="+")
    crawl.add_argument("--country", default="us")
    crawl.add_argument("--language", default="english")
    crawl.add_argument("--steamspy", action="store_true")
    catalog = commands.add_parser("catalog", help="page through the official game catalog")
    catalog.add_argument("--max-pages", type=positive_int, default=1)
    catalog.add_argument("--last-appid", type=int, default=0)
    commands.add_parser("export", help="write latest observations as JSONL to stdout")
    steamdb = commands.add_parser("import-steamdb", help="import lawfully obtained local JSON")
    steamdb.add_argument("path")
    steamdb.add_argument("--authorized", action="store_true",
                         help="confirm permission to use the imported data")
    args = parser.parse_args(argv)
    store = None
    try:
        client = Client(timeout=args.timeout, retries=args.retries)
        if args.command == "import-steamdb" and not args.authorized:
            parser.error("SteamDB import requires permission and --authorized")
        if args.command == "crawl" and (
            not re.fullmatch(r"[a-zA-Z]{2}", args.country)
            or not re.fullmatch(r"[a-z_]{2,32}", args.language)
        ):
            parser.error("invalid country or language")
        if args.command == "catalog" and not 0 <= args.last_appid <= 4294967295:
            parser.error("--last-appid must be a nonnegative 32-bit integer")
        if args.command == "catalog" and not os.environ.get("STEAM_API_KEY"):
            parser.error("STEAM_API_KEY is required for catalog enumeration")
        store = Store(args.db)
        if args.command == "export":
            for record in store.export():
                print(json.dumps(record, ensure_ascii=False, allow_nan=False))
        elif args.command == "import-steamdb":
            count = import_steamdb(args.path, store)
            print(json.dumps({"imported": count}))
        elif args.command == "catalog":
            collector = Collector(client, store)
            for page in collector.catalog(
                os.environ["STEAM_API_KEY"], args.max_pages, args.last_appid
            ):
                print(json.dumps(page, ensure_ascii=False))
        else:
            collector = Collector(client, store, args.country.lower(), args.language)
            failed = False
            for appid in dict.fromkeys(args.appids):
                errors = collector.collect(appid, steamspy=args.steamspy)
                for error in errors:
                    print(f"appid={appid}: {error}", file=sys.stderr)
                failed |= bool(errors)
                print(json.dumps({"appid": appid, "errors": errors}))
            return 1 if failed else 0
        return 0
    except (SourceError, ValueError, OSError, sqlite3.Error):
        # Never print upstream exceptions: request URLs can contain API keys.
        print("Operation failed; check inputs, source availability and database access.",
              file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
