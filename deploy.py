#!/usr/bin/env python3
"""Build all three report pages and serve them together on one local port.

Each tool's page is self-contained, so "deploying" is just rebuilding each one and
copying it under a single document root with an index that links them.

    python3 deploy.py            # build + serve on 8777
    python3 deploy.py --port N   # different port
    python3 deploy.py --build    # rebuild only, do not serve
"""
import argparse
import functools
import http.server
import pathlib
import shutil
import socketserver
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "_site"

TOOLS = [
    ("cobweb", "Cobweb", "Атлас на кампаниите", "Campaign Atlas",
     "Групира злонамерени домейни в операциите зад тях, със запазени доказателства за всяка връзка.",
     "Groups malicious domains into the operations behind them, with the evidence for every link."),
    ("sealbox", "Sealbox", "Доклад за запечатани доказателства", "Evidence Seal Report",
     "Запазва заснета страница така, че байтовете ѝ да са доказуемо непроменени от времевия печат.",
     "Preserves a captured page so its bytes are provably unchanged since the timestamp."),
    ("shopwatch", "Shopwatch", "Триаж на търговци", "Merchant Disclosure Triage",
     "Подрежда онлайн магазини по това доколко идентификацията на търговеца изостава от закона.",
     "Ranks online shops by how far their trader identity falls short of the law."),
]


def build() -> list[str]:
    """Rebuild every page and stage it under _site/. Returns names that succeeded."""
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    ok = []
    for slug, *_ in TOOLS:
        d = ROOT / slug
        r = subprocess.run([sys.executable, "web/build.py"], cwd=d,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  {slug}: BUILD FAILED\n{r.stdout}{r.stderr}", file=sys.stderr)
            continue
        (OUT / slug).mkdir()
        (OUT / slug / "index.html").write_text(
            wrap(( d / "web" / "index.html").read_text(encoding="utf-8")),
            encoding="utf-8")
        ok.append(slug)
        print(f"  {slug}: ok")
    (OUT / "index.html").write_text(index_page(ok), encoding="utf-8")
    return ok


def wrap(content: str) -> str:
    """Wrap artifact-body HTML in a real standalone document.

    web/index.html is written as Artifact *content* - no <html>/<head>/<body>, because
    the Artifact runtime supplies them. Served standalone it therefore declares no
    charset, and a browser falls back to Latin-1 and renders Cyrillic as mojibake.

    The content leads with head-level elements (<title>, <link>, <style>) and then the
    first flow element, so split there rather than dumping everything into <head> and
    leaning on the parser's implicit body.
    """
    if content.lstrip().lower().startswith("<!doctype"):
        return content
    i = content.find("<div")
    if i == -1:                      # no flow content found: keep it all in body
        head, rest = "", content
    else:
        head, rest = content[:i], content[i:]
    return ('<!doctype html>\n<html lang="bg">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            + head.strip() + '\n</head>\n<body>\n'
            + rest.replace("</body>", "").replace("</html>", "").strip()
            + '\n</body>\n</html>\n')


def index_page(available: list[str]) -> str:
    cards = "\n".join(
        f'''    <a class="card{'' if slug in available else ' down'}" href="/{slug}/">
      <div class="ct">{tool}</div>
      <div class="cn" lang="bg">{bg}</div>
      <div class="ce" lang="en">{en}</div>
      <p lang="bg">{dbg}</p>
      <p class="en" lang="en">{den}</p>
      {'' if slug in available else '<div class="err">сглобяването се провали / build failed</div>'}
    </a>'''
        for slug, tool, bg, en, dbg, den in TOOLS)

    return f'''<!doctype html>
<html lang="bg"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Инструменти за киберразследване</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500&display=swap">
<style>
:root{{--paper:#EDEFF2;--surface:#FAFBFC;--surface-2:#F1F3F6;--ink:#14181F;--ink-soft:#5A6472;
  --ink-faint:#8B94A1;--rule:#CBD2DB;--rule-soft:#DFE4EA;--accent:#1F4E79;--accent-soft:#E3EBF3;
  --seal:#A8232B;--seal-soft:#F5E3E4}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--paper:#0F1319;--surface:#161B23;
  --surface-2:#1C222C;--ink:#E4E8EE;--ink-soft:#8994A3;--ink-faint:#6A7482;--rule:#2B333F;
  --rule-soft:#222933;--accent:#6FA8DC;--accent-soft:#17304A;--seal:#E06A72;--seal-soft:#3A1B1E}}}}
:root[data-theme="dark"]{{--paper:#0F1319;--surface:#161B23;--surface-2:#1C222C;--ink:#E4E8EE;
  --ink-soft:#8994A3;--ink-faint:#6A7482;--rule:#2B333F;--rule-soft:#222933;--accent:#6FA8DC;
  --accent-soft:#17304A;--seal:#E06A72;--seal-soft:#3A1B1E}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);
  font-family:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;font-size:15px;line-height:1.55}}
header{{border-bottom:2px solid var(--ink);background:var(--surface)}}
.in{{max-width:1000px;margin:0 auto;padding:20px 24px}}
.tag{{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--accent);border:1px solid var(--accent);padding:2px 7px}}
h1{{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:27px;margin:8px 0 3px;letter-spacing:.02em}}
.sub{{color:var(--ink-soft);font-size:13.5px;max-width:68ch}}
.grid{{max-width:1000px;margin:0 auto;padding:22px 24px 50px;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}
.card{{display:block;text-decoration:none;color:inherit;background:var(--surface);
  border:1px solid var(--rule);border-left:3px solid var(--accent);border-radius:2px;padding:16px 17px}}
.card:hover{{background:var(--surface-2);border-left-color:var(--seal)}}
.card:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.card.down{{border-left-color:var(--seal);opacity:.65}}
.ct{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink-faint)}}
.cn{{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:20px;font-weight:600;margin-top:3px}}
.ce{{font-size:12px;color:var(--ink-faint);font-family:"IBM Plex Mono",monospace;margin-top:1px}}
.card p{{font-size:13px;color:var(--ink-soft);margin:9px 0 0}}
.card p.en{{font-size:12px;color:var(--ink-faint);margin-top:5px}}
.err{{margin-top:9px;font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--seal)}}
footer{{max-width:1000px;margin:0 auto;padding:0 24px 44px;color:var(--ink-faint);font-size:12px;
  max-width:80ch}}
</style></head><body>
<header><div class="in">
  <span class="tag">локален преглед · local preview</span>
  <h1>Инструменти за киберразследване</h1>
  <div class="sub">Три самостоятелни доклада. Страниците се отварят на български;
    смяна на езика — с флаговете горе вдясно. <span style="color:var(--ink-faint)">Three
    standalone reports; pages open in Bulgarian, switch language with the flags.</span></div>
</div></header>
<div class="grid">
{cards}
</div>
<footer>Всяка страница е напълно самостоятелна — данните и кодът са вградени, няма мрежови заявки.
Сървърът е само за удобство. · Each page is fully self-contained; the server is a convenience only.</footer>
</body></html>'''


class UTF8Handler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler serves .html without a charset, leaving the browser to
    guess. Say utf-8 explicitly so the header matches what the files actually are."""

    def guess_type(self, path):
        t = super().guess_type(path)
        base = t.split(";")[0].strip() if isinstance(t, str) else t
        if base in ("text/html", "text/plain", "text/css", "application/javascript",
                    "text/javascript", "application/json"):
            return f"{base}; charset=utf-8"
        return t


def serve(port: int) -> None:
    handler = functools.partial(UTF8Handler, directory=str(OUT))

    class Reuse(socketserver.TCPServer):
        allow_reuse_address = True

    with Reuse(("127.0.0.1", port), handler) as httpd:
        print(f"\nserving {OUT} at http://localhost:{port}/   (ctrl-c to stop)")
        for slug, *_ in TOOLS:
            print(f"  http://localhost:{port}/{slug}/")
        httpd.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--build", action="store_true", help="rebuild only, do not serve")
    a = ap.parse_args()
    print("building:")
    got = build()
    if len(got) != len(TOOLS):
        print(f"warning: {len(TOOLS) - len(got)} page(s) failed to build", file=sys.stderr)
    if not a.build:
        serve(a.port)
