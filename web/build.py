#!/usr/bin/env python3
"""Assemble the standalone report page.

The page must be self-contained (no fetch, no external scripts), so the report JSON
and the application code are inlined at build time.

Defaults to the sample report committed alongside this script, so a fresh clone can
build the page without first having to produce a report of its own. Pass a path to
render a real run instead:

    python web/build.py path/to/report.json
"""
import argparse
import json
import pathlib

HERE = pathlib.Path(__file__).parent


def build(report_path: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    tpl = (HERE / "_template.html").read_text(encoding="utf-8")
    app = (HERE / "_app.js").read_text(encoding="utf-8")

    # "</script>" inside embedded JSON would close the host <script> element early.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", blob)
    html = html.replace("</script>", app + "\n</script>", 1)
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", default=str(HERE / "sample_report.json"),
                    help="report JSON to render (default: the committed sample)")
    ap.add_argument("-o", "--out", default=str(HERE / "index.html"))
    args = ap.parse_args()

    src = pathlib.Path(args.report)
    if not src.exists():
        raise SystemExit(f"report not found: {src}\n"
                         f"Run without arguments to build from the committed sample.")
    p = build(src, pathlib.Path(args.out))
    print(f"{p} ({p.stat().st_size:,} bytes)  <- {src}")


if __name__ == "__main__":
    main()
