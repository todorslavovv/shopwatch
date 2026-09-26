"""Investigation tests — no network: a fake fetcher stands in for sealbox's."""
import pytest

from shopwatch.investigate import decode_body, disclosure_links, investigate

HOME = """<html><body>
  <a href="/products">Продукти</a>
  <a href="https://facebook.com/shop">Контакти във Facebook</a>
  <a href="/kontakti">Контакти</a>
  <a href="/obshti-usloviya">Общи условия</a>
  <a href="#top">Нагоре</a>
</body></html>"""

CONTACT = """<html><body>
  <p>ЕИК: 204511873</p>
  <p>Адрес: гр. София, бул. Витоша 15</p>
  <p>Тел: +359 2 970 4400</p>
  <p>Ел. поща: office@shop.bg</p>
  <p>"ТЕСТ ТЪРГОВИЯ" ЕООД</p>
</body></html>"""


def fake_fetch(pages):
    def _fetch(url, **kw):
        if url not in pages:
            raise RuntimeError(f"unexpected fetch: {url}")
        return {"final_url": url, "status": 200, "resolved_ip": "93.184.216.34",
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": pages[url].encode("utf-8")}
    return _fetch


def test_decode_cp1251_is_not_mojibake():
    body = "<p>ЕИК: 204511873</p>".encode("windows-1251")
    assert "ЕИК" in decode_body(body, "text/html; charset=windows-1251")
    assert "ЕИК" in decode_body(body, None)          # undeclared: fallback must catch it


def test_links_are_same_host_and_hinted():
    links = disclosure_links(HOME, "https://shop.bg/")
    assert links == ["https://shop.bg/kontakti", "https://shop.bg/obshti-usloviya"]


def test_investigation_follows_to_the_contact_page():
    r = investigate("https://shop.bg/", fetcher=fake_fetch({
        "https://shop.bg/": HOME,
        "https://shop.bg/kontakti": CONTACT,
        "https://shop.bg/obshti-usloviya": "<html><body>условия</body></html>",
    }))
    assert r["merchant"]["eik"] == "204511873"
    assert r["sources"]["eik"] == "https://shop.bg/kontakti"
    assert r["assessment"]["verdict"] == "ok"
    assert r["errors"] == []


def test_unreachable_shop_is_reported_not_raised():
    def boom(url, **kw):
        raise RuntimeError("cannot resolve")
    r = investigate("https://gone.bg/", fetcher=boom)
    assert r["errors"] and r["assessment"]["verdict"] == "unreachable"
    assert r["merchant"]["eik"] is None and r["reachable"] is False


def test_bare_domain_gets_a_scheme():
    r = investigate("shop.bg", fetcher=fake_fetch({"https://shop.bg": HOME}))
    assert r["url"] == "https://shop.bg"


def test_bot_block_is_not_scored_as_noncompliant():
    """A 403 means we were refused, not that the trader published nothing."""
    def blocked(url, **kw):
        return {"final_url": url, "status": 403, "resolved_ip": "1.2.3.4",
                "headers": {"content-type": "text/html"},
                "body": b"<html><body>Access denied</body></html>"}
    r = investigate("https://blocked.bg/", fetcher=blocked)
    assert r["assessment"]["verdict"] == "blocked"
    assert r["assessment"]["score"] is None


def test_rate_limit_refuses_after_the_window_budget():
    """A public form is a fetch-proxy if nothing bounds it."""
    from shopwatch.serve import PER_IP_CHECKS, rate_limit
    ip = "203.0.113.9"
    assert all(rate_limit(ip) is None for _ in range(PER_IP_CHECKS))
    assert "Too many checks" in (rate_limit(ip) or "")
    assert rate_limit("203.0.113.10") is None      # limit is per client, not global


def test_one_target_cannot_be_hammered_by_many_clients():
    from shopwatch.serve import PER_TARGET_FETCHES, target_key, target_limit
    host = target_key("https://www.victim.bg/some/page?a=1")
    assert host == "victim.bg"
    assert all(target_limit(host) is None for _ in range(PER_TARGET_FETCHES))
    assert "retry in" in (target_limit(host) or "")           # capped regardless of client
    assert target_limit("other.bg") is None


def test_daylog_keeps_today_and_drops_older(tmp_path):
    from shopwatch import daylog
    p = tmp_path / "d.db"
    daylog.record("shop.bg", {"assessment": {"verdict": "ok", "score": 5}}, path=p)
    assert daylog.get("shop.bg", path=p)["assessment"]["score"] == 5
    with daylog._conn(p) as c:
        c.execute("UPDATE checks SET day = '1999-12-31'")
    assert daylog.get("shop.bg", path=p) is None and daylog.today(path=p) == []


def test_bot_protection_is_distinguished_from_a_dead_site():
    """403 means refused, which is not the same claim as unreachable."""
    def waf(url, **kw):
        return {"final_url": url, "status": 403, "resolved_ip": "18.165.72.92",
                "headers": {"content-type": "text/html"}, "body": b"Request blocked"}
    r = investigate("https://olx.bg/", fetcher=waf)
    assert r["assessment"]["verdict"] == "blocked"
    assert r["assessment"]["findings"][0]["code"] == "BLOCKED"
    assert r["refused"] is True

    def dead(url, **kw):
        raise RuntimeError("cannot resolve")
    assert investigate("https://gone.bg/", fetcher=dead)["assessment"]["verdict"] == "unreachable"


def test_disclosure_links_rank_contact_pages_above_delivery_terms():
    """emag.bg's EIK page lost to three delivery pages on document order alone."""
    html = """<a href="/help/dostavka-usloviya">Условия за доставка</a>
              <a href="/info/usloviza-za-polzvane">Условия за ползване</a>
              <a href="/help/kontakt">Контакт</a>
              <a href="/info/lichni-danni">Лични данни</a>"""
    got = disclosure_links(html, "https://shop.bg/")
    assert got[0].endswith("/help/kontakt")            # tier 1 wins despite being third
    assert got[-1].endswith("/info/lichni-danni")      # weakest hint last


def test_javascript_shell_is_not_called_noncompliant():
    """bilka.bg serves zero links; its nav only exists after a browser runs the app."""
    def spa(url, **kw):
        return {"final_url": url, "status": 200, "resolved_ip": "1.2.3.4",
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>"}
    r = investigate("https://spa.bg/", fetcher=spa)
    assert r["assessment"]["verdict"] == "undetermined"
    assert r["assessment"]["findings"][0]["code"] == "SCRIPT_RENDERED"
    assert r["script_rendered"] is True


def test_redirect_abroad_is_out_of_scope_not_noncompliant():
    """bilka.bg redirects to bilka.ro — Bulgarian EIK law does not reach it."""
    def ro(url, **kw):
        return {"final_url": "https://www.bilka.ro/", "status": 200,
                "resolved_ip": "1.2.3.4",
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": "<html><body><a href='/gdpr'>GDPR</a>office@bilka.ro</body></html>".encode()}
    r = investigate("https://bilka.bg/", fetcher=ro)
    assert r["assessment"]["verdict"] == "out_of_scope"
    assert r["offsite_redirect"] is True and r["final_host"] == "bilka.ro"


def test_www_and_path_redirects_stay_in_scope():
    def same(url, **kw):
        return {"final_url": "https://www.shop.bg/home", "status": 200,
                "resolved_ip": "1.2.3.4", "headers": {"content-type": "text/html"},
                "body": b"<html><body><a href='/x'>x</a>EIK: 204511873</body></html>"}
    r = investigate("shop.bg", fetcher=same)
    assert r["offsite_redirect"] is False


def test_registry_confirmation_is_reported_without_changing_the_score():
    def found(url, **kw):
        return {"final_url": url, "status": 200, "resolved_ip": "1.2.3.4",
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": ("<html><body><a href='/k'>Контакти</a>"
                         "<p>ЕИК: 130858590</p><p>Адрес: гр. София, бул. Витоша 1</p>"
                         "<p>Тел: 0700 12 340</p><p>\"ТЕХНОПОЛИС БЪЛГАРИЯ\" ЕАД</p>"
                         "</body></html>").encode()}
    reg = {"status": "confirmed", "name": "ТЕХНОПОЛИС БЪЛГАРИЯ - ЕАД",
           "address": "гр. София", "source": "VIES"}
    r = investigate("https://t.bg/", fetcher=found, verifier=lambda e: reg)
    codes = [f["code"] for f in r["assessment"]["findings"]]
    assert "EIK_CONFIRMED" in codes
    assert r["registry_name_matches"] is True


def test_absence_from_the_vat_registry_never_accuses():
    """Small traders below the VAT threshold are legitimately absent."""
    from shopwatch.compliance import assess
    merchant = {"eik": "204511873", "company_name": "МАЛКА ФИРМА ЕООД",
                "address": "гр. Русе, ул. Дунав 2", "email": "a@b.bg", "phone": "0888",
                "vat_number": None}
    base = assess(merchant)
    missing = assess(merchant, {"status": "not_found"})
    assert missing["score"] == base["score"]          # weight must stay zero
    assert missing["verdict"] == base["verdict"]
    assert "EIK_NOT_IN_VAT_REGISTRY" in [f["code"] for f in missing["findings"]]


def test_registry_outage_is_not_a_verdict():
    from shopwatch.registry import check_vat
    def boom(url):
        raise OSError("network down")
    assert check_vat("130858590", opener=boom)["status"] == "unavailable"


def test_subdomains_of_the_shop_are_followed_but_strangers_are_not():
    """olx.bg keeps its terms on help.olx.bg — same trader, different host."""
    from shopwatch.investigate import registrable
    html = """<a href="https://help.olx.bg/terms">Общи условия</a>
              <a href="https://evil.example.com/contact">Контакти</a>
              <a href="https://other.com.bg/za-nas">За нас</a>"""
    got = disclosure_links(html, "https://www.olx.bg/")
    assert got == ["https://help.olx.bg/terms"]
    assert registrable("help.olx.bg") == "olx.bg"
    assert registrable("shop.com.bg") == "shop.com.bg"      # not "com.bg"
    assert registrable("a.shop.com.bg") == "shop.com.bg"


def test_app_shell_pages_are_undetermined_not_noncompliant():
    """olx.bg's help pages are a Salesforce shell: 354KB of HTML, no readable text."""
    def shell(url, **kw):
        return {"final_url": url, "status": 200, "resolved_ip": "1.2.3.4",
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": ("<html><body><a href='/terms'>Общи условия</a>"
                         "<div data-aura-rendered-by='7:0'></div>"
                         "<script>var auraConfig={};</script></body></html>").encode()}
    r = investigate("https://olx.bg/", fetcher=shell)
    assert r["assessment"]["verdict"] == "undetermined"
    assert r["assessment"]["score"] is None


def test_static_scam_site_with_no_disclosure_still_scores():
    """The shell rule must not become an excuse for sites that genuinely publish nothing."""
    def bare(url, **kw):
        return {"final_url": url, "status": 200, "resolved_ip": "1.2.3.4",
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": b"<html><body><h1>SALE</h1><a href='/buy'>Buy</a></body></html>"}
    r = investigate("https://scam.bg/", fetcher=bare)
    assert r["assessment"]["verdict"] == "noncompliant"
    assert r["assessment"]["score"] == 100
