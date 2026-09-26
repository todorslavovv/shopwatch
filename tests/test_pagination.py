"""Example-findings pagination: 5 shops per page, a real ?page=N query parameter, plain
<a href> links, and pre-rendered pages so it works without JavaScript.

The pages are built from the committed sample with the page's own script (run in node),
then served by deploy.py's handler, which maps ?page=N to the pre-rendered page."""
import functools
import importlib.util
import json
import re
import shutil
import socketserver
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
DEPLOY = WEB.parent.parent / "deploy.py"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node renders the pages")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load("sw_build", WEB / "build.py")
SHOPS = [s["domain"] for s in json.loads((WEB / "sample_report.json").read_text(encoding="utf-8"))["shops"]]


def view(page_html: str) -> dict:
    """What a reader without JavaScript gets: the markup before the inlined script."""
    h = page_html.split("<script>")[0]
    return {"shops": re.findall(r'<article class="shop[^"]*">\s*<div class="shop-main">\s*'
                                r'<div class="shop-head">\s*<span class="shop-dom">([^<]+)<', h),
            "range": re.findall(r'class="pg-range">([^<]*)<', h),
            "active": re.findall(r'aria-current="page"[^>]*>(\d+)<', h),
            "prev": re.findall(r'<a class="pg-step" href="([^"]+)" rel="prev"', h),
            "next": re.findall(r'<a class="pg-step" href="([^"]+)" rel="next"', h),
            "disabled": re.findall(r'<span class="pg-step" aria-disabled="true">([^<]*)<', h),
            "numbers": re.findall(r'<a class="pg-num[^"]*" href="([^"]+)"', h)}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    d = tmp_path_factory.mktemp("site")
    build.build(WEB / "sample_report.json", d / "index.html")
    return d


@pytest.fixture(scope="module")
def server(site):
    if not DEPLOY.exists():
        pytest.skip("deploy.py (the server that maps ?page=N) is not next to this repo")
    deploy = _load("sw_deploy", DEPLOY)
    srv = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), functools.partial(deploy.UTF8Handler, directory=str(site)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def get(base, path):
    with urllib.request.urlopen(urllib.parse.urljoin(base, path), timeout=10) as r:
        assert r.status == 200
        return view(r.read().decode("utf-8"))


EXPECT = {1: "1–5", 2: "6–10", 3: "11–15", 4: "16–20"}


def test_build_writes_one_file_per_page_with_five_shops_each(site):
    assert sorted(p.name for p in site.glob("*.html")) == \
        ["index.html", "page-2.html", "page-3.html", "page-4.html"]
    seen = []
    for n in (1, 2, 3, 4):
        v = view((site / ("index.html" if n == 1 else f"page-{n}.html")).read_text(encoding="utf-8"))
        assert len(v["shops"]) == 5, n                       # pages 1-3 and the last one
        assert v["range"] == [f"Показани {EXPECT[n]} от 20"] and v["active"] == [str(n)]
        seen += v["shops"]
    assert sorted(seen) == sorted(SHOPS) and len(set(seen)) == 20   # every shop, once


def test_default_page_is_page_one(server):
    v = get(server, "/")
    assert v["range"] == ["Показани 1–5 от 20"] and v["active"] == ["1"]


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_each_page_by_query_parameter(server, n):
    v = get(server, f"/?page={n}")
    assert len(v["shops"]) == 5
    assert v["range"] == [f"Показани {EXPECT[n]} от 20"] and v["active"] == [str(n)]
    assert v["numbers"] == [f"?page={k}#findings" for k in (1, 2, 3, 4)]    # real <a href>


def test_previous_and_next_at_each_position(server):
    first, mid, last = get(server, "/?page=1"), get(server, "/?page=3"), get(server, "/?page=4")
    assert first["prev"] == [] and first["disabled"] == ["← Предишна"]
    assert first["next"] == ["?page=2#findings"]
    assert mid["prev"] == ["?page=2#findings"] and mid["next"] == ["?page=4#findings"]
    assert last["prev"] == ["?page=3#findings"] and last["next"] == []
    assert last["disabled"] == ["Следваща →"]


@pytest.mark.parametrize("q", ["?page=abc", "?page=0", "?page=-1", "?page=2.5", "?page="])
def test_invalid_page_number_is_page_one(server, q):
    assert get(server, "/" + q)["range"] == ["Показани 1–5 от 20"]


@pytest.mark.parametrize("q", ["?page=5", "?page=99"])
def test_page_above_the_last_is_the_last(server, q):
    v = get(server, "/" + q)
    assert v["range"] == ["Показани 16–20 от 20"] and v["active"] == ["4"]


def test_following_next_walks_every_page(server):
    url, ranges = "/", []
    while True:
        v = get(server, url)
        ranges.append(v["range"][0])
        if not v["next"]:
            break
        url = urllib.parse.urljoin(url, v["next"][0])        # the link as a browser follows it
    assert ranges == [f"Показани {EXPECT[n]} от 20" for n in (1, 2, 3, 4)]
    assert get(server, "/index.html?page=3")["range"] == ["Показани 11–15 от 20"]


@pytest.mark.parametrize("q,want", [("", 1), ("?page=2", 2), ("?x=1&page=3", 3), ("?page=4", 4),
                                    ("?page=abc", 1), ("?page=0", 1), ("?page=99", 4)])
def test_the_browser_script_reads_the_same_page_from_the_url(q, want):
    """With JavaScript the page re-renders from location.search: same page, same rule."""
    app = "\n".join((WEB / f).read_text(encoding="utf-8") for f in ("_i18n.js", "_lang.js", "_app.js"))
    blob = (WEB / "sample_report.json").read_text(encoding="utf-8")
    got = build.prerender(app, blob, q)
    assert f'aria-current="page" aria-label="Страница {want}">{want}<' in got["app"]
    assert got["app"].count('<article class="shop') == 5
