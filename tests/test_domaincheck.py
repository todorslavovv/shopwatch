"""Domain-check tests - no network. Fakes stand in for the sites, DNS, search, archive,
VIES and the Commercial Register. The live acceptance run (olx.bg and four others) is in
tests/test_live.py and needs SHOPWATCH_LIVE=1.
"""
import json
import socket
import time
import types
import zlib

import pytest

from shopwatch import domaincheck as dc
from shopwatch import privacy, serve, sources
from shopwatch.pdftext import extract_text
from shopwatch.store import Store

dc._sealbox_fetch()        # puts the sibling sealbox checkout on sys.path for the guard tests

EIK = "204511873"          # checksum-valid
EIK2 = "131418803"
EIK3 = "200586330"


# ---- fakes ----------------------------------------------------------------------

def site(pages: dict, robots: str | None = None, status: dict | None = None,
         calls: list | None = None, ctype: dict | None = None):
    """Fake guarded fetcher. pages: url -> html (or bytes). status: url -> HTTP status."""
    def fetch(url, **kw):
        if calls is not None:
            calls.append((url, kw.get("user_agent")))
        assert kw.get("user_agent") == dc.USER_AGENT        # honest UA, every request
        if url.endswith("/robots.txt"):
            if robots is None:
                return {"final_url": url, "status": 404, "headers": {}, "body": b""}
            return {"final_url": url, "status": 200, "headers": {}, "body": robots.encode()}
        key = url if url in pages else url.rstrip("/") + "/" if url.rstrip("/") + "/" in pages \
            else None
        if key is None:
            return {"final_url": url, "status": (status or {}).get(url, 404), "headers": {},
                    "body": b"nope"}
        body = pages[key]
        return {"final_url": url, "status": (status or {}).get(url, 200),
                "headers": {"content-type": (ctype or {}).get(url, "text/html; charset=utf-8")},
                "body": body if isinstance(body, bytes) else body.encode("utf-8"),
                "resolved_ip": "93.184.216.34"}
    return fetch


def getter(nx=False, hosts: dict | None = None, wildcard: str | None = None, ct=None):
    """DoH / RDAP / crt.sh fake. hosts: name -> CNAME target (real hosts)."""
    hosts = hosts or {}

    def get(url, headers=None, timeout=10):
        if url.startswith(dc.DOH):
            name = url.split("name=")[1].split("&")[0]
            if name in hosts:
                return {"Status": 0, "Answer": [{"type": 5, "data": hosts[name]}]}
            if wildcard and "." in name and not name.count(".") < 2:
                return {"Status": 0, "Answer": [{"type": 5, "data": wildcard}]}
            if nx or name.count(".") >= 2:
                return {"Status": 3}
            return {"Status": 0, "Answer": [{"name": name, "type": 1, "data": "93.184.216.34"}]
                    if "type=A&" in url + "&" else []}
        if url == dc.RDAP_BOOTSTRAP:
            return {"services": [[["com"], ["https://rdap.example/"]]]}
        if url.startswith("https://rdap.example/"):
            return {"events": [{"eventAction": "registration", "eventDate": "2010-01-01"}],
                    "entities": [{"roles": ["registrar"],
                                  "vcardArray": ["vcard", [["fn", {}, "text", "Reg Inc"]]]},
                                 {"roles": ["registrant"],
                                  "vcardArray": ["vcard", [["fn", {}, "text", "Private Person"]]]}]}
        if url.startswith("https://crt.sh/"):
            if ct is None:
                raise OSError("crt.sh down")
            return [{"name_value": n} for n in ct]
        raise AssertionError(f"unexpected url {url}")
    return get


class FakeSearch:
    name = "fake"

    def __init__(self, results):
        self.results = results
        self.queries = []

    def configured(self):
        return True

    def search(self, q):
        self.queries.append(q)
        return [{"query": q, "result_url": r["url"], "result_title": r["title"],
                 "result_snippet": r["description"], "retrieved_at": "2026-09-23T00:00:00Z",
                 "provider": "fake"} for r in self.results]


class FakeArchive:
    PAUSE = 0

    def __init__(self, pages: dict | None = None):
        self.pages = pages or {}
        self.asked = []

    def snapshot(self, url, timeout=10):
        self.asked.append(url)
        return {"timestamp": "20250101000000", "url": url, "raw_url": url} \
            if url in self.pages else None

    def fetch(self, snap, timeout=15):
        body = self.pages[snap["url"]]
        if isinstance(body, bytes):
            return body, "application/pdf"
        return body.encode("utf-8"), "text/html; charset=utf-8"


class FakeRegistry:
    def __init__(self, records: dict | None = None, available=True):
        self.records, self.available = records or {}, available

    def status(self):
        return sources.AVAILABLE if self.available else sources.UNCONFIGURED

    def lookup_by_eik(self, eik):
        return self.records.get(eik)

    def search_by_company_name(self, name):
        return [r for r in self.records.values() if dc.name_matches(name, r["name"])]


def vies_table(table: dict):
    def check_vat(number, country="BG", timeout=10):
        name = table.get(f"{country}{number}")
        if name is None:
            return {"status": "not_found", "name": None, "address": None, "source": "VIES"}
        if name == "DOWN":
            return {"status": "unavailable", "name": None, "address": None, "source": "VIES"}
        return {"status": "confirmed", "name": name, "address": "гр. София", "source": "VIES"}
    return check_vat


def run(url, pages=None, results=None, vies=None, store=None, archive=None,
        registry=None, site_kw=None, get_kw=None, **kw):
    return dc.check(url, store=store, fetch=site(pages or {}, **(site_kw or {})),
                    getter=getter(**(get_kw or {})),
                    search=[FakeSearch(results)] if results is not None else [],
                    vat_checker=vies_table(vies or {}),
                    registry=registry or FakeRegistry(available=False),
                    archive=archive or FakeArchive(),
                    tls_fn=lambda host: {"status": "ok", "verified": True}, **kw)


@pytest.fixture(autouse=True)
def no_search_key(monkeypatch):
    monkeypatch.delenv("SHOPWATCH_BRAVE_TOKEN", raising=False)
    monkeypatch.setattr(sources.BraveSearch, "TOKEN_FILE",
                        sources.Path("/nonexistent/brave_token"))
    # Replace the module's reference, not time.sleep itself: that is shared by every test.
    monkeypatch.setattr(sources, "time", types.SimpleNamespace(sleep=lambda s: None))


HOME = '<html><body><a href="/kontakti">Контакти</a><a href="/usloviya">Общи условия</a></body></html>'


def contact(eik=EIK, name='"ТЕСТ ТЪРГОВИЯ" ЕООД'):
    return f"<html><body><p>ЕИК: {eik}</p><p>{name}</p><p>Адрес: гр. София, ул. Шипка 1</p>" \
           f"<p>Ел. поща: office@shop.bg</p></body></html>"


def status_of(r, source):
    return r["discovery"][source]["status"], r["discovery"][source]["outcome"]


# ---- normalisation / input validation -----------------------------------------

@pytest.mark.parametrize("raw,domain", [
    ("shop.bg", "shop.bg"), ("https://www.shop.bg/", "shop.bg"),
    ("https://shop.bg/path/page?x=1", "shop.bg"), ("WWW.SHOP.BG", "shop.bg"),
    ("a.com.bg", "a.com.bg"), ("магазин.бг", "xn--80aairftm.xn--90ae"),
])
def test_normalisation_www_path_idn(raw, domain):
    assert dc.normalise(raw)["registrable_domain"] == domain


@pytest.mark.parametrize("raw", [
    "", "   ", "ftp://shop.bg", "javascript:alert(1)", "http://127.0.0.1/",
    "http://[::1]/", "http://169.254.169.254/latest/meta-data", "http://2130706433/",
    "http://0x7f.1/", "https://user:pw@shop.bg/", "https://shop.bg:8443/", "com.bg",
    "https://shop.bg/\nHost: evil", "x" * 3000, "http://shop..bg",
])
def test_malformed_and_ssrf_inputs_rejected(raw):
    with pytest.raises(dc.InputError):
        dc.normalise(raw)


# ---- resolution from the operator's own site ------------------------------------

def test_company_website_with_eik_and_vies_corroboration():
    r = run("https://shop.bg/", {"https://shop.bg/": HOME, "https://shop.bg/kontakti": contact()},
            vies={f"BG{EIK}": '"ТЕСТ ТЪРГОВИЯ" ЕООД'})
    res = r["domain_resolution"]
    assert res["company_identified"] and res["confidence_level"] == "high"
    assert any("VIES" in x for x in res["reasons"])            # explainable
    assert r["company"]["eik"] == EIK and r["company"]["verified_by"] == "official"
    assert r["company"]["legal_form"] == "ЕООД"
    assert status_of(r, "target_site") == ("available", "evidence_found")
    assert status_of(r, "vies") == ("available", "evidence_found")
    assert r["status"] == "complete" and not r["conflicts"]
    assert r["sources"][0]["type"] == "official"
    assert all(s.get("retrieved_at") for s in r["sources"])


def test_eik_with_registry_corroboration():
    reg = FakeRegistry({EIK: {"eik": EIK, "name": "ТЕСТ ТЪРГОВИЯ ЕООД", "legal_form": "ЕООД",
                              "status": "registered", "registered_address": "гр. София",
                              "capital": "5000 BGN", "registration_date": "2012-01-01",
                              "persons": []}})
    r = run("shop.bg", {"https://shop.bg/": contact()}, registry=reg)
    assert r["company"]["verified_by"] == "registry"
    assert r["company"]["capital"] == "5000 BGN"
    assert status_of(r, "registry") == ("available", "evidence_found")
    assert any("Commercial Register" in x for x in r["domain_resolution"]["reasons"])


def test_registry_unconfigured_is_reported_and_does_not_stop_resolution():
    r = run("shop.bg", {"https://shop.bg/": contact()}, vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"})
    assert status_of(r, "registry") == ("unconfigured", None)
    assert r["domain_resolution"]["company_identified"]


def test_no_search_provider_is_unconfigured_not_no_evidence():
    r = run("shop.bg", {"https://shop.bg/": contact()})
    assert status_of(r, "search") == ("unconfigured", None)
    assert r["domain_resolution"]["company_identified"]


def test_search_provider_is_actually_queried_when_configured():
    fs = FakeSearch([])
    dc.check("shop.bg", fetch=site({"https://shop.bg/": contact()}), getter=getter(),
             search=[fs], vat_checker=vies_table({}), registry=FakeRegistry(available=False),
             archive=FakeArchive(), tls_fn=lambda h: {"status": "ok"})
    assert fs.queries and any('"shop.bg" ЕИК' == q for q in fs.queries)


def test_company_name_without_eik_is_medium_and_never_invents_an_eik():
    page = "<html><body><p>Фирма: „Малък Магазин“ ЕООД</p><p>Тел: 0888 123 456</p></body></html>"
    r = run("shop.bg", {"https://shop.bg/": page})
    assert r["company"]["eik"] is None and r["company"]["verified_by"] is None
    assert r["domain_resolution"]["confidence_level"] == "medium"
    assert r["status"] == "partial"


def test_product_text_ending_in_a_legal_form_is_not_a_company():
    page = "<html><body><p>Сушилня GORENJE W1D2A964ADS КАПАЦИТ ЕТ</p></body></html>"
    r = run("shop.bg", {"https://shop.bg/": page})
    assert r["company"] is None
    assert not dc._plausible_name("та. Той въвежда и мотото")


def test_domain_redirect_is_followed_and_attributed_to_final_url():
    def fetch(url, **kw):
        if url.endswith("robots.txt"):
            return {"final_url": url, "status": 404, "headers": {}, "body": b""}
        return {"final_url": "https://www.shop.bg/", "status": 200, "headers": {},
                "body": contact().encode()}
    r = dc.check("shop.bg", fetch=fetch, getter=getter(), search=[],
                 vat_checker=vies_table({f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"}),
                 registry=FakeRegistry(available=False), archive=FakeArchive(),
                 tls_fn=lambda h: {"status": "ok"})
    assert r["company"]["eik"] == EIK
    assert r["target_site"]["pages_read"][0]["url"] == "https://www.shop.bg/"


# ---- blocked target: the site is one source, not the check ------------------------

CHALLENGE = "<html><head><title>Just a moment...</title></head><script>cf_chl_opt={}</script></html>"


def test_blocked_site_plus_search_evidence(monkeypatch):
    calls = []
    results = [{"url": "https://shop.bg/obshti-usloviya", "title": "Общи условия",
                "description": f"Търговец: „ТЕСТ ТЪРГОВИЯ“ ЕООД, ЕИК: {EIK}"}]
    r = dc.check("shop.bg", fetch=site({"https://shop.bg/": CHALLENGE},
                                       status={"https://shop.bg/": 403}, calls=calls),
                 getter=getter(), search=[FakeSearch(results)],
                 vat_checker=vies_table({f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"}),
                 registry=FakeRegistry(available=False), archive=FakeArchive(),
                 tls_fn=lambda h: {"status": "ok"})
    assert r["target_site"]["fetch_status"] == "blocked"
    # No second attempt with another identity, and the refusing host is not probed again.
    assert [u for u, _ in calls].count("https://shop.bg/") == 1
    assert not any(u.startswith("https://shop.bg/obshti") for u, _ in calls)
    assert r["company"]["eik"] == EIK and r["company"]["verified_by"] == "official"
    assert r["domain_resolution"]["basis"] == "search_evidence"
    assert r["domain_resolution"]["confidence_level"] == "medium"
    assert status_of(r, "search") == ("available", "evidence_found")


def test_blocked_site_plus_archived_legal_page():
    archive = FakeArchive({
        "https://shop.bg/": '<a href="/obshti-usloviya">Общи условия</a>',
        "https://shop.bg/obshti-usloviya":
            "<p>Администратор на сайта shop.bg е „ТЕСТ ТЪРГОВИЯ“ ЕООД, ЕИК " + EIK + "</p>"})
    r = run("shop.bg", {"https://shop.bg/": CHALLENGE}, archive=archive,
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"},
            site_kw={"status": {"https://shop.bg/": 403}})
    assert r["target_site"]["fetch_status"] == "blocked"
    assert r["company"]["eik"] == EIK
    assert r["domain_resolution"]["confidence_level"] == "high"
    assert status_of(r, "web_archive") == ("available", "evidence_found")
    ev = [s for s in r["sources"] if s["type"] == "company_source"]
    assert ev and all(s["archived"] == "20250101000000" for s in ev)   # dated, not hidden


def test_archived_only_and_unconfirmed_is_not_high():
    archive = FakeArchive({
        "https://shop.bg/": '<a href="/obshti-usloviya">Общи условия</a>',
        "https://shop.bg/obshti-usloviya": f"<p>ЕИК: {EIK}</p>"})
    r = run("shop.bg", {"https://shop.bg/": CHALLENGE}, archive=archive,
            site_kw={"status": {"https://shop.bg/": 403}})
    assert r["domain_resolution"]["confidence_level"] == "medium"
    assert any("archived" in x for x in r["domain_resolution"]["reasons"])


def test_robots_disallow_is_honoured_and_archive_used_instead():
    calls = []
    archive = FakeArchive({"https://shop.bg/": contact()})
    r = dc.check("shop.bg", fetch=site({"https://shop.bg/": contact()},
                                       robots="User-agent: *\nDisallow: /\n", calls=calls),
                 getter=getter(), search=[], vat_checker=vies_table({}),
                 registry=FakeRegistry(available=False), archive=archive,
                 tls_fn=lambda h: {"status": "ok"})
    assert r["target_site"]["direct_fetch"] == "robots_disallowed"
    assert [u for u, _ in calls] == ["https://shop.bg/robots.txt"]
    assert r["company"]["eik"] == EIK                            # from the archive


def test_operator_named_in_pdf_on_a_related_host_behind_wildcard_dns():
    """The shape of the olx.bg case, on a fictional domain: the main site is a script
    shell, *.domain is wildcard DNS, and the operator is named only in a PDF linked
    from the terms page of a developer portal."""
    pdf = _pdf("Приемането е споразумение с администратора на уебсайта Market.bg - "
               "дружеството “Маркет Плейс България” ЕООД, регистрирано в Търговския регистър, "
               f"с ЕИК {EIK3}, със седалище в гр. София.")
    pages = {"https://market.bg/": "<html><div id=app></div></html>",
             "https://developer.market.bg/": '<a href="/terms-and-conditions">Общи условия</a>',
             "https://developer.market.bg/terms-and-conditions":
                 '<a href="/doc/regulations.bg.pdf">Общи условия (PDF)</a>',
             "https://developer.market.bg/doc/regulations.bg.pdf": pdf}
    r = run("market.bg", pages, vies={f"BG{EIK3}": "МАРКЕТ ПЛЕЙС БЪЛГАРИЯ - ЕООД"},
            get_kw={"wildcard": "wild.cdn.example.", "hosts": {
                "developer.market.bg": "portal.cdn.example."}},
            site_kw={"ctype": {"https://developer.market.bg/doc/regulations.bg.pdf":
                               "application/pdf"}})
    assert r["discovery"]["related_hosts"]["hosts"] == ["developer.market.bg"]
    assert r["company"]["eik"] == EIK3
    assert r["domain_resolution"]["confidence_level"] == "high"
    assert any("operates this domain" in x for x in r["domain_resolution"]["reasons"])


def test_wildcard_dns_guesses_do_not_count_as_hosts():
    exists = dc.host_exists_fn("market.bg", getter(wildcard="wild.cdn.example.",
                                                    hosts={"help.market.bg": "sf.example."}))
    assert exists("help.market.bg") and not exists("about.market.bg")


# ---- conflicts and weak evidence ---------------------------------------------------

def test_foreign_company_via_vat_in_structured_data():
    page = ('<html><script type="application/ld+json">{"@type":"Organization",'
            '"legalName":"Shop SRL","vatID":"RO12345678"}</script></html>')
    r = run("magazin-demo.ro", {"https://magazin-demo.ro/": page},   # shop.ro is a suffix
            vies={"RO12345678": "SHOP SRL"})
    assert r["company"]["eik"] is None and r["company"]["foreign"]
    assert r["company"]["name"] == "SHOP SRL" and r["company"]["verified_by"] == "official"


def test_third_party_page_about_something_else_is_ignored():
    results = [{"url": "https://catalog.example/firm", "title": "Фирма АБВ ООД",
                "description": f"АБВ ООД, ЕИК {EIK2}, гр. Пловдив"}]   # never names shop.bg
    r = run("shop.bg", {"https://shop.bg/": "<html>нищо</html>"}, results=results,
            vies={f"BG{EIK2}": "АБВ ООД"})
    assert r["domain_resolution"]["company_identified"] is False and r["company"] is None


def test_single_third_party_mention_is_low_and_not_identified():
    results = [{"url": "https://catalog.example/firm", "title": "shop.bg",
                "description": f"shop.bg - АБВ ООД, ЕИК {EIK2}"}]
    r = run("shop.bg", {"https://shop.bg/": "<html>нищо</html>"}, results=results,
            vies={f"BG{EIK2}": "АБВ ООД"})
    res = r["domain_resolution"]
    assert res["company_identified"] is False and res["confidence_level"] == "low"
    assert res["candidate"]["eik"] == EIK2                    # shown, but not asserted


def test_multiple_companies_on_site_prefers_the_one_whose_name_matches():
    pages = {"https://shop.bg/": HOME,
             "https://shop.bg/kontakti": contact(EIK, '"ТЕСТ ТЪРГОВИЯ" ЕООД'),
             "https://shop.bg/usloviya": f"<html><p>Куриер: ЕИК: {EIK2}</p></html>"}
    r = run("shop.bg", pages, vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД", f"BG{EIK2}": "КУРИЕР ООД"})
    assert r["company"]["eik"] == EIK
    assert any(c["type"] == "multiple_identifiers" for c in r["conflicts"])
    assert EIK2 in r["alternatives"]


def test_indistinguishable_companies_are_not_guessed():
    pages = {"https://shop.bg/": HOME,
             "https://shop.bg/kontakti": f"<html><p>ЕИК: {EIK}</p></html>",
             "https://shop.bg/usloviya": f"<html><p>ЕИК: {EIK2}</p></html>"}
    r = run("shop.bg", pages, vies={f"BG{EIK}": "А ООД", f"BG{EIK2}": "Б ООД"})
    assert r["domain_resolution"]["company_identified"] is False
    assert set(r["alternatives"]) == {EIK, EIK2}


def test_operator_statement_is_not_given_to_a_neighbouring_company():
    text = ("Администратор на shop.bg е „Тест Търговия“ ЕООД, ЕИК " + EIK + ". Плащанията "
            "се обработват от „Пей Сървисиз“ ЕАД, ЕИК " + EIK2 + ".")
    ms = {m["name"]: m["operator"] for m in dc.company_mentions(text, "shop.bg")}
    assert ms == {"Тест Търговия ЕООД": True, "Пей Сървисиз ЕАД": False}


def test_site_eik_outranks_conflicting_third_party_eik():
    results = [{"url": "https://catalog.example/x", "title": "shop.bg",
                "description": f"shop.bg се управлява от ДРУГА ООД, ЕИК {EIK2}"}]
    r = run("shop.bg", {"https://shop.bg/": contact()}, results=results,
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД", f"BG{EIK2}": "ДРУГА ООД"})
    assert r["company"]["eik"] == EIK
    conflict = next(c for c in r["conflicts"] if c["type"] == "multiple_identifiers")
    assert {e["eik"] for e in conflict["eiks"]} == {EIK, EIK2}


def test_old_or_renamed_company_name_is_flagged_and_not_high():
    r = run("shop.bg", {"https://shop.bg/": contact(EIK, '"СТАРО ИМЕ" ЕООД')},
            vies={f"BG{EIK}": "НОВО НАИМЕНОВАНИЕ ЕООД"})
    assert r["company"]["name"] == "НОВО НАИМЕНОВАНИЕ ЕООД"      # official wins
    assert any(c["type"] == "name_mismatch" for c in r["conflicts"])
    assert r["domain_resolution"]["confidence_level"] == "medium"


def test_company_renamed_between_checks_is_detected(tmp_path):
    store = Store(tmp_path / "s.db")
    pages = {"https://shop.bg/": contact(EIK, "")}
    run("shop.bg", pages, vies={f"BG{EIK}": "ПЪРВО ИМЕ ЕООД"}, store=store)
    r = run("shop.bg", pages, vies={f"BG{EIK}": "ВТОРО ИМЕ ЕООД"}, store=store, refresh=True)
    assert any(c["type"] == "official_record_changed" for c in r["conflicts"])


def test_not_in_vat_registry_is_not_called_inactive():
    r = run("shop.bg", {"https://shop.bg/": contact()}, vies={})
    assert status_of(r, "vies") == ("available", "no_evidence_found")
    assert r["company"]["eik"] == EIK and r["company"]["verified_by"] is None
    assert r["company"].get("status") is None
    assert r["status"] == "partial"


def test_vies_unavailable_is_partial_not_a_verdict():
    r = run("shop.bg", {"https://shop.bg/": contact()}, vies={f"BG{EIK}": "DOWN"})
    assert status_of(r, "vies") == ("temporarily_unavailable", None)
    assert r["domain_resolution"]["company_identified"]
    assert r["company"]["verified_by"] is None


def test_dns_only_evidence_identifies_nothing():
    r = run("shop.bg", {"https://shop.bg/": "<html><body>Hello</body></html>"})
    assert r["infrastructure"]["dns"] and r["infrastructure"]["supporting_only"]
    assert r["domain_resolution"]["confidence_level"] == "insufficient"
    assert r["company"] is None


def test_tls_only_evidence_identifies_nothing():
    r = dc.check("shop.bg", fetch=site({"https://shop.bg/": "<html>Hello</html>"}),
                 getter=getter(), search=[], vat_checker=vies_table({}),
                 registry=FakeRegistry(available=False), archive=FakeArchive(),
                 tls_fn=lambda h: {"status": "ok", "verified": True,
                                   "subject": {"organizationName": "Тест Търговия ЕООД"}})
    assert r["company"] is None
    assert r["discovery"]["tls"]["supporting_only"] is True


def test_domain_that_does_not_resolve():
    def fetch(url, **kw):
        raise OSError("cannot resolve")
    r = dc.check("gone.bg", fetch=fetch, getter=getter(nx=True), search=[],
                 vat_checker=vies_table({}), registry=FakeRegistry(available=False),
                 archive=FakeArchive(), tls_fn=lambda h: {"status": "unavailable"})
    assert r["discovery"]["dns"]["outcome"] == "no_evidence_found"
    assert r["target_site"]["fetch_status"] == "failed"
    assert r["domain_resolution"]["company_identified"] is False


def test_slow_site_is_abandoned_within_budget(monkeypatch):
    monkeypatch.setattr(dc, "DIRECT_BUDGET", 0.3)
    monkeypatch.setattr(dc, "DISCOVERY_BUDGET", 0.3)

    def slow(url, **kw):
        time.sleep(1.0)
        return {"final_url": url, "status": 404, "headers": {}, "body": b""}
    t0 = time.time()
    r = dc.check("slow.bg", fetch=slow, getter=getter(), search=[], vat_checker=vies_table({}),
                 registry=FakeRegistry(available=False), archive=FakeArchive(),
                 tls_fn=lambda h: {"status": "ok"})
    assert time.time() - t0 < 2.5
    assert r["target_site"]["fetch_status"] == "temporarily_unavailable"


def test_large_response_is_capped_by_the_fetcher():
    seen = []

    def fetch(url, **kw):
        seen.append(kw.get("max_bytes"))
        return {"final_url": url, "status": 200, "headers": {}, "body": b"x" * 100}
    dc.check("big.bg", fetch=fetch, getter=getter(), search=[], vat_checker=vies_table({}),
             registry=FakeRegistry(available=False), archive=FakeArchive(),
             tls_fn=lambda h: {"status": "ok"})
    # 8 MB: page builders put the trader footer past 2 MB (batashop.bg); still a hard cap.
    assert seen and all(m is not None and m <= 8 * 1024 * 1024 for m in seen)


def test_rdap_never_reads_registrant():
    r = dc.rdap("shop.com", getter())
    assert r["registrar"] == "Reg Inc" and "Private Person" not in json.dumps(r)


# ---- PDF and Commercial Register formats ------------------------------------------

def _pdf(text: str) -> bytes:
    """A minimal PDF with a ToUnicode CMap, one glyph per character, like Google Docs."""
    chars = sorted(set(text))
    code = {c: i + 1 for i, c in enumerate(chars)}
    bf = " ".join(f"<{code[c]:04X}> <{ord(c):04X}>" for c in chars)
    cmap = (f"begincmap 1 begincodespacerange <0000> <FFFF> endcodespacerange "
            f"{len(chars)} beginbfchar {bf} endbfchar endcmap").encode()
    glyphs = " ".join(f"5 0 Td <{code[c]:04X}> Tj" for c in text)
    content = f"BT /F1 12 Tf 1 0 0 -1 0 20 Tm {glyphs} ET".encode()
    return (b"%PDF-1.4\n1 0 obj << /Type /Page /Resources << /Font << /F1 2 0 R >> >>"
            b" /Contents 4 0 R >> endobj\n2 0 obj << /Type /Font /ToUnicode 3 0 R >> endobj\n"
            b"3 0 obj << /Length 1 >> stream\n" + cmap + b"\nendstream endobj\n"
            b"4 0 obj << /Length 1 /Filter /FlateDecode >> stream\n" + zlib.compress(content)
            + b"\nendstream endobj\n")


def test_pdf_text_extraction_rebuilds_words():
    assert extract_text(_pdf("ЕИК 175345805")) == "ЕИК 175345805"


def test_pdf_zip_bomb_is_refused(monkeypatch):
    from shopwatch import pdftext
    monkeypatch.setattr(pdftext, "MAX_INFLATED", 1000)
    bomb = (b"%PDF-1.4\n1 0 obj << /Filter /FlateDecode >> stream\n"
            + zlib.compress(b"A" * 100000) + b"\nendstream endobj\n")
    with pytest.raises(pdftext.PdfError):
        extract_text(bomb)


DEED = """<Message xmlns="http://www.registryagency.bg/schemas/envelopev2"
 xmlns:d="http://www.registryagency.bg/schemas/deedv2"
 xmlns:f="http://www.registryagency.bg/schemas/deedv2/Fields"><Body><Deeds>
 <d:Deed DeedStatus="N" CompanyName="ТЕСТ ТЪРГОВИЯ" GUID="g" UIC="{eik}" LegalForm="EOOD">
  <d:SubDeed>
   <f:Seat FieldOperation="Add"><f:Address><f:Settlement>гр. София</f:Settlement>
     <f:Street>ул. Шипка</f:Street><f:StreetNumber>1</f:StreetNumber></f:Address>
     <f:Contacts><f:Phone/><f:Fax/><f:EMail/><f:URL>shop.bg</f:URL></f:Contacts></f:Seat>
   <f:Seat FieldOperation="Erase"><f:Address><f:Settlement>гр. Стар</f:Settlement></f:Address></f:Seat>
   <f:Funds FieldOperation="Add" currency="BGN">5000</f:Funds>
   <f:Managers><f:Manager FieldOperation="Add"><f:Person><f:Indent>8001011234</f:Indent>
     <f:Name>Иван Иванов</f:Name><f:IndentType>EGN</f:IndentType></f:Person></f:Manager></f:Managers>
  </d:SubDeed></d:Deed></Deeds></Body></Message>""".format(eik=EIK)


def test_registry_deed_parsing_follows_the_official_schema_and_drops_egn():
    rec = sources.parse_deeds(DEED.encode())[0]
    assert rec["eik"] == EIK and rec["legal_form"] == "ЕООД" and rec["status"] == "registered"
    assert rec["registered_address"] == "гр. София, ул. Шипка, 1"     # erased seat skipped
    assert rec["capital"] == "5000 BGN" and rec["website"] == "shop.bg"
    assert rec["persons"] == [{"name": "Иван Иванов", "role": "Manager"}]
    assert "8001011234" not in json.dumps(rec)


def test_registry_inbox_ingest_then_lookup(tmp_path):
    store = Store(tmp_path / "s.db")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "m1.xml").write_text(DEED, encoding="utf-8")
    reg = sources.RegistryAgencyProvider(store, inbox)
    assert reg.status() == "available" and reg.ingest() == 1
    assert not list(inbox.glob("*.xml"))
    got = reg.lookup_by_eik(EIK)
    assert got["name"] == "ТЕСТ ТЪРГОВИЯ"
    assert got["persons"] == [{"name": "Иван Иванов", "role": "Manager"}]  # ЕГН never kept
    assert reg.search_by_company_name("ТЕСТ ТЪРГОВИЯ ЕООД")[0]["eik"] == EIK
    assert sources.RegistryAgencyProvider(store, tmp_path / "none").status() == "unconfigured"


# ---- SSRF boundary (sealbox's guard, as used by this tool) ------------------------

def test_guard_refuses_private_loopback_linklocal_and_metadata():
    from sealbox.capture import address_is_forbidden
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254",
               "::1", "fe80::1", "::ffff:127.0.0.1", "100.64.0.1"):
        assert address_is_forbidden(ip), ip
    assert address_is_forbidden("93.184.216.34") is None


def test_dns_rebinding_mixed_answer_is_refused(monkeypatch):
    from sealbox import capture

    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(capture.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(capture.CaptureError):
        capture.resolve_guarded("rebind.example", 443)


def test_redirect_to_private_address_is_refused(monkeypatch):
    from sealbox import capture

    def fake_getaddrinfo(host, port, **kw):
        ip = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(capture.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(capture.CaptureError):
        capture._open("http://internal.example/", timeout=1, proxy=None, allow_private=False)


def test_tls_collector_refuses_loopback():
    assert dc.tls_certificate("localhost")["status"] == "unavailable"


# ---- privacy ----------------------------------------------------------------------

def test_ten_digit_bg_vat_is_an_egn_and_never_shown():
    page = "<html><p>ИН по ЗДДС: BG8001011234</p><p>Ел. поща: ivan.petrov@gmail.com</p></html>"
    r = run("shop.bg", {"https://shop.bg/": page})
    assert r["disclosure"]["merchant"]["vat_number"] is None
    assert "8001011234" not in json.dumps(r, ensure_ascii=False)
    # The shop's published contact is kept whatever its provider: Gmail is not private.
    assert r["disclosure"]["merchant"]["email"] == "ivan.petrov@gmail.com"


def test_a_regulator_mailbox_in_legal_boilerplate_is_not_the_shops_contact():
    page = "<html><p>Жалби: Комисия за защита на личните данни, e-mail: kzld@cpdp.bg</p></html>"
    r = run("shop.bg", {"https://shop.bg/": page})
    assert r["disclosure"]["merchant"]["email"] is None


def test_people_are_shown_with_their_source_and_never_with_an_identifier(tmp_path, monkeypatch):
    reg = FakeRegistry({EIK: {"eik": EIK, "name": "ТЕСТ ЕООД", "status": "registered",
                              "persons": [{"name": "Иван Иванов", "role": "Управител",
                                           "egn": "8001011234"}]}})
    pages = {"https://shop.bg/": contact()}
    r = run("shop.bg", pages, registry=reg)
    assert r["people"][0]["name"] == "Иван Иванов"
    assert r["people"][0]["source"] == "Commercial Register" and r["people"][0]["status"] == "verified"
    assert "8001011234" not in json.dumps(r, ensure_ascii=False)
    # The controller's switch still turns people off; company identity is never withheld.
    monkeypatch.setattr(privacy, "PEOPLE_OFF", tmp_path / "PEOPLE-OFF")
    (tmp_path / "PEOPLE-OFF").write_text("off")
    r = run("shop.bg", pages, registry=reg)
    assert r["people"] == [] and r["company"]["eik"] == EIK


def test_representative_named_by_the_company_is_published_not_verified():
    page = contact().replace("</body>", "<p>Администратор на лични данни е ТЕСТ ТЪРГОВИЯ ЕООД, "
                             "представлявано от управителя Мария Петрова Иванова.</p></body>")
    r = run("shop.bg", {"https://shop.bg/": page}, vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"})
    p = r["people"][0]
    assert (p["name"], p["role"], p["status"]) == ("Мария Петрова Иванова", "manager", "published")
    assert p["source_url"] == "https://shop.bg/" and p["retrieved_at"]


def test_store_filters_before_writing_and_restriction_applies_on_output(tmp_path):
    store = Store(tmp_path / "s.db")
    store.upsert_entity({"eik": EIK, "name": "X ЕООД", "egn": "8001011234",
                         "raw_filing": "..."}, source="official")
    assert "8001011234" not in json.dumps(store.entity(EIK))
    rid = store.open_request("objection", "", EIK, "registered_address", "sole trader home")
    store.resolve_request(rid, "restrict", "not legally required for this purpose")
    r = run("shop.bg", {"https://shop.bg/": contact()},
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"}, store=store)
    assert "registered_address" not in r["company"] and r["company"]["eik"] == EIK


def test_data_subject_access_and_erasure(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.db")
    eid, _ = store.upsert_entity({"eik": EIK, "name": "X ЕООД"}, source="registry")
    store.set_people(eid, EIK, [{"name": "Иван Иванов", "role": "Управител"}],
                     "registry", "2026-09-23")
    assert store.access_report("Иван Иванов")["held"][0]["eik"] == EIK
    rid = store.open_request("erasure", "Иван Иванов", EIK, None, "")
    store.resolve_request(rid, "erase", "no longer manager")
    assert store.access_report("Иван Иванов")["held"] == []


def test_retention_purge_forgets_old_domains(tmp_path):
    store = Store(tmp_path / "s.db")
    did = store.upsert_domain("old.bg", "https://old.bg/", "resolved")
    eid, _ = store.upsert_entity({"eik": EIK, "name": "X"}, source="official")
    store.link(did, eid, 0.9, "resolved")
    with store._db() as db:
        db.execute("UPDATE domains SET last_checked='2000-01-01T00:00:00Z'")
    assert store.purge() == 1 and store.entity(EIK) is None


def test_worker_reverifies_stale_entities(tmp_path):
    store = Store(tmp_path / "s.db")
    store.upsert_entity({"eik": EIK, "name": "СТАРО ЕООД"}, source="official")
    with store._db() as db:
        db.execute("UPDATE entities SET last_verified='2000-01-01T00:00:00Z'")
    store.link(store.upsert_domain("shop.bg", "https://shop.bg/", "resolved"),
               store.entity(EIK)["id"], 0.9, "resolved")
    out = dc.refresh_stale(store, vies_table({f"BG{EIK}": "НОВО ЕООД"}))
    assert out["reverified"] == 1 and out["changed"] == 1
    assert store.entity(EIK)["legal_name"] == "НОВО ЕООД"


# ---- API --------------------------------------------------------------------------

def test_api_validates_limits_and_runs(monkeypatch):
    monkeypatch.setattr(serve, "STORE", None)
    serve._hits.clear(); serve._day.clear(); serve._targets.clear()
    assert serve.domain_check("1.1.1.1", b"not json")[0] == 400
    assert serve.domain_check("1.1.1.1", b'{"url": "http://10.0.0.1"}')[0] == 400
    ok = serve.domain_check("1.1.1.1", b'{"url": "shop.bg"}',
                            runner=lambda url, store: {"domain_resolution": {}, "url": url})
    assert ok[0] == 200 and ok[1]["url"] == "shop.bg"
    for _ in range(serve.PER_IP_CHECKS):
        serve.domain_check("2.2.2.2", b'{"url": "shop.bg"}',
                           runner=lambda url, store: {"domain_resolution": {}})
    assert serve.domain_check("2.2.2.2", b'{"url": "shop.bg"}')[0] == 429


def test_definition_lists_attach_each_role_to_its_own_company():
    """technopolis.bg's terms define ТЕХНОПОЛИС, then ПРАКТИКЕР, each followed by its
    own "owner of the online shop at <domain>". The phrase must stay with its party."""
    text = ('"ТЕХНОПОЛИС" – означава "Технополис България" ЕАД със седалище гр. София, '
            'Хипермаркет Технополис, ЕИК 130858590, тел: 0700, e-mail: x , дружество – '
            'собственик на хипермаркети Технополис и онлайн магазина на www.technopolis.bg '
            '"ПРАКТИКЕР" – означава „Практикер Ритейл“ ЕООД със седалище гр. София, ЕИК '
            '200525782, дружество – собственик на онлайн магазина на www.praktiker.bg')
    op = lambda d: {m["name"] for m in dc.company_mentions(text, d) if m["operator"]}
    assert op("technopolis.bg") == {"Технополис България ЕАД"}
    assert op("praktiker.bg") == {"Практикер Ритейл ЕООД"}


def test_every_host_refuses_us_and_the_archive_carries_the_operator_pdf():
    """olx.bg as seen from a residential IP it blocks: every CloudFront host returns 403,
    including the developer portal. The archive has the portal's /bg page (its root was
    never archived), the terms page, and the PDF naming the operator."""
    pdf = _pdf("администраторът на уебсайта Market.bg - дружеството “Маркет Плейс България” "
               f"ЕООД, регистрирано в Търговския регистър, с ЕИК {EIK3}")
    refused = {u: 403 for u in ("https://market.bg/", "https://developer.market.bg/",
                                "https://developer.market.bg/robots.txt")}
    archive = FakeArchive({
        "https://developer.market.bg/bg": '<a href="/bg/terms-and-conditions">Общи условия</a>',
        "https://developer.market.bg/bg/terms-and-conditions":
            '<a href="/doc/regulations.bg.pdf">Общи условия</a>',
        "https://developer.market.bg/doc/regulations.bg.pdf": pdf})
    r = run("market.bg", {"https://market.bg/": "denied", "https://developer.market.bg/": "denied"},
            archive=archive, vies={f"BG{EIK3}": "МАРКЕТ ПЛЕЙС БЪЛГАРИЯ - ЕООД"},
            get_kw={"wildcard": "wild.cdn.example.",
                    "hosts": {"developer.market.bg": "portal.cdn.example."}},
            site_kw={"status": refused})
    assert r["target_site"]["fetch_status"] == "blocked"
    assert r["company"]["eik"] == EIK3 and r["company"]["verified_by"] == "official"
    assert r["domain_resolution"]["confidence_level"] == "high"
    assert status_of(r, "web_archive") == ("available", "evidence_found")
    assert "https://developer.market.bg/" in archive.asked     # root first, then /bg


def test_names_compare_across_missing_spaces():
    assert dc._names_agree("НаспърсКласифайдсБългария ЕООД", "Наспърс Класифайдс България - ЕООД")
    assert dc._names_agree("А ООД", "Б ООД") is False


def test_crawler_never_follows_login_account_or_checkout_links():
    c = dc.Crawler("shop.bg", fetch=lambda *a, **k: None)
    html = ('<a href="https://login.shop.bg/?client_id=1">Условия за вход</a>'
            '<a href="/account/terms">Общи условия на профила</a>'
            '<a href="/obshti-usloviya">Общи условия</a>')
    assert [u for _t, u in c.links(html, "https://shop.bg/")] == ["https://shop.bg/obshti-usloviya"]



def _word_style_pdf(text: str, width: float = 500.0) -> bytes:
    """Like Word's export: every glyph placed with its own Td, NO space glyphs - a word
    gap is just a longer move - and glyph widths declared in a /W array."""
    chars = sorted(set(text) - {" "})
    code = {c: i + 1 for i, c in enumerate(chars)}
    bf = " ".join(f"<{code[c]:04X}> <{ord(c):04X}>" for c in chars)
    cmap = (f"begincmap 1 begincodespacerange <0000> <FFFF> endcodespacerange "
            f"{len(chars)} beginbfchar {bf} endbfchar endcmap").encode()
    adv, ops, gap = width / 1000 * 10, [], 0.0
    for c in text:
        if c == " ":
            gap += adv                                   # the pen moves, nothing is drawn
            continue
        ops.append(f"{(adv if ops else 0) + gap:.2f} 0 Td <{code[c]:04X}> Tj")
        gap = 0.0
    content = f"BT /F1 10 Tf 1 0 0 1 0 0 Tm {' '.join(ops)} ET".encode()
    return (b"%PDF-1.5\n1 0 obj << /Type /Page /Resources << /Font << /F1 2 0 R >> >>"
            b" /Contents 4 0 R >> endobj\n2 0 obj << /Type /Font /Subtype /Type0"
            b" /DescendantFonts [5 0 R] /ToUnicode 3 0 R >> endobj\n"
            b"5 0 obj << /Type /Font /Subtype /CIDFontType2 /DW " + str(width).encode()
            + b" /W [ ] >> endobj\n3 0 obj << /Length 1 >> stream\n" + cmap
            + b"\nendstream endobj\n4 0 obj << /Length 1 /Filter /FlateDecode >> stream\n"
            + zlib.compress(content) + b"\nendstream endobj\n")


def test_pdf_without_space_glyphs_gets_its_word_gaps_back():
    assert extract_text(_word_style_pdf("Наспърс Класифайдс България ЕООД")) == \
        "Наспърс Класифайдс България ЕООД"


def test_archive_falls_back_to_the_www_twin_of_a_link():
    """technopolis.bg's archived /bg page links relatively to /bg/terms-and-conditions,
    but the archive holds that page only under www.technopolis.bg."""
    archive = FakeArchive({
        "https://shop.bg/": '<a href="/obshti-usloviya">Общи условия</a>',
        "https://www.shop.bg/obshti-usloviya":
            "<p>Администратор на сайта shop.bg е „ТЕСТ ТЪРГОВИЯ“ ЕООД, ЕИК " + EIK + "</p>"})
    r = run("shop.bg", {"https://shop.bg/": CHALLENGE}, archive=archive,
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"},
            site_kw={"status": {"https://shop.bg/": 403}})
    assert r["company"]["eik"] == EIK
    assert "https://www.shop.bg/obshti-usloviya" in archive.asked


def test_unidentified_result_with_a_temporarily_down_source_is_not_cached(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.db")

    class DownArchive(FakeArchive):
        def snapshot(self, url, timeout=10):
            raise OSError("archive down")
    run("shop.bg", {"https://shop.bg/": CHALLENGE}, archive=DownArchive(), store=store,
        site_kw={"status": {"https://shop.bg/": 403}})
    assert store.cache_get("domain", dc.cache_key("shop.bg"), 600) is None
    run("shop.bg", {"https://shop.bg/": contact()}, store=store)        # a normal answer is
    assert store.cache_get("domain", dc.cache_key("shop.bg"), 600) is not None   # cached
