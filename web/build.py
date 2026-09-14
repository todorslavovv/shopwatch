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
import re
import shutil
import subprocess

HERE = pathlib.Path(__file__).parent


def syntax_check(paths) -> None:
    """Reject a build whose JavaScript does not parse.

    The page is assembled by string substitution, so a broken script still produces a
    perfectly well-formed HTML file that renders a blank shell and only fails in the
    browser. Catching it here turns a silent runtime failure into a build failure.
    Skipped when node is unavailable rather than blocking the build.
    """
    node = shutil.which("node")
    if not node:
        return
    for f in paths:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"JavaScript syntax error in {f.name}:\n{r.stderr.strip()}")


def check_element_ids(tpl: str, app: str) -> None:
    """Fail the build if the script addresses an element the template does not define.

    getElementById returns null silently and the TypeError only surfaces in a browser,
    so template/script drift produces a page that builds cleanly and renders nothing.
    """
    wanted = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", app))
    have = set(re.findall(r"id=['\"]([^'\"]+)['\"]", tpl))
    missing = sorted(wanted - have)
    if missing:
        raise SystemExit("template is missing element ids used by the script: "
                         + ", ".join(missing))


def build(report_path: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    js = [p for p in (HERE / f for f in ("_i18n.js", "_lang.js", "_app.js")) if p.exists()]
    syntax_check(js)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    tpl = (HERE / "_template.html").read_text(encoding="utf-8")
    app = "\n".join(f.read_text(encoding="utf-8") for f in js)

    # "</script>" inside embedded JSON would close the host <script> element early.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    check_element_ids(tpl, app)
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
