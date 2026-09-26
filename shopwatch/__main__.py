"""CLI: shopwatch <url> [url ...] — investigate live shops and emit a report.

    python -m shopwatch example.bg
    python -m shopwatch -f domains.txt -o report.json && python web/build.py report.json
"""
from __future__ import annotations

import argparse
import json
import sys

from shopwatch.investigate import DEFAULT_MAX_PAGES, investigate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shopwatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="shop URLs or bare domains")
    ap.add_argument("-f", "--file", help="file with one URL per line (# comments ok)")
    ap.add_argument("-o", "--out", help="write the report JSON here (default: stdout)")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                    help=f"pages fetched per shop (default: {DEFAULT_MAX_PAGES})")
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args(argv)

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            urls += [ln.strip() for ln in fh
                     if ln.strip() and not ln.lstrip().startswith("#")]
    if not urls:
        ap.error("give at least one URL, or -f with a list")

    report = []
    for url in urls:
        entry = investigate(url, max_pages=args.max_pages, timeout=args.timeout)
        a = entry["assessment"]
        score = "-" if a["score"] is None else a["score"]
        print(f"{a['verdict']:>13}  {score:>3}  {entry['domain']}"
              + (f"  ({len(entry['errors'])} errors)" if entry["errors"] else ""),
              file=sys.stderr)
        report.append(entry)

    blob = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
        print(f"wrote {args.out} ({len(report)} shops)", file=sys.stderr)
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
