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
from html import escape
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


# A stub DOM just big enough for render(): the page's own script, run in node, produces
# the HTML a browser would, so the pre-rendered pages cannot drift from the live ones.
_STUB = """
const els = {};
const el = id => els[id] || (els[id] = {innerHTML: '', textContent: '', placeholder: '',
  dataset: {}, disabled: false, readOnly: false, setAttribute() {}, addEventListener() {},
  querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}});
globalThis.document = {getElementById: el, querySelectorAll: () => [], documentElement: {}, title: ''};
globalThis.localStorage = {getItem: () => null, setItem() {}};
"""
TEXT_IDS = ("tag", "h1", "sub", "checkh", "checkp", "prov")


def prerender(app: str, blob: str, search: str) -> dict:
    """Render the page for one ?page= query exactly as the browser script does."""
    node = shutil.which("node")
    if not node:
        raise SystemExit("node is required to pre-render the example-findings pages")
    script = (_STUB + f"globalThis.location = {{search: {json.dumps(search)}}};\n"
              "(function () {\nconst DATA = " + blob + ";\n" + app + "\n"
              "process.stdout.write(JSON.stringify(Object.assign({app: els.app.innerHTML},"
              " Object.fromEntries(" + json.dumps(list(TEXT_IDS)) +
              ".map(k => [k, els[k].textContent])))));\n})();")
    r = subprocess.run([node], input=script, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pre-render failed:\n{r.stderr.strip()}")
    return json.loads(r.stdout)


def fill(html: str, got: dict) -> str:
    """Put the pre-rendered content into the empty template elements (markup only,
    never inside the inlined script)."""
    head, sep, script = html.partition("<script>")
    for k in TEXT_IDS + ("app",):
        val = got[k] if k == "app" else escape(got[k])
        head = re.sub(rf'(id="{k}"[^>]*>)(</)', lambda m: m.group(1) + val + m.group(2),
                      head, count=1)
    return head + sep + script


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
    # Page 1 is index.html; pages 2..N are page-N.html beside it, served for ?page=N.
    for old in out.parent.glob("page-*.html"):
        old.unlink()
    first = prerender(app, blob, "?page=1")
    pages = max(1, first["app"].count('class="pg-num'))
    out.write_text(fill(html, first), encoding="utf-8")
    for n in range(2, pages + 1):
        (out.parent / f"page-{n}.html").write_text(
            fill(html, prerender(app, blob, f"?page={n}")), encoding="utf-8")
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
