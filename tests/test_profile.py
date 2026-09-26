"""Field-level compliance profile: each field verified on its own, conflicts kept visible,
"not found" never confused with "could not be checked". No network."""
import json

from shopwatch import domaincheck as dc
from shopwatch import profile as pr
from tests.test_domaincheck import (CHALLENGE, EIK, EIK2, EIK3, FakeArchive, FakeRegistry, _pdf,
                                    contact, getter, run, site, vies_table)

REG = {EIK: {"eik": EIK, "name": "ТЕСТ ТЪРГОВИЯ ЕООД", "legal_form": "ЕООД",
             "status": "registered", "registered_address": "гр. София, ул. Шипка 1",
             "corporate_phone": "02 970 4400", "corporate_email": "office@shop.bg",
             "website": "shop.bg", "persons": []}}


def page(url, *, found=None, mentions=None, phones=(), emails=(), archived=None, legal=False):
    host = url.split("/")[2]
    return {"url": url, "host": host, "archived": archived, "legal_doc": legal,
            "retrieved_at": "2026-09-23T00:00:00Z", "found": found or {},
            "mentions": mentions or [], "contacts": {"phones": list(phones), "emails": list(emails)}}


def build(pages, off=None, reg=None, reg_status="unconfigured", site_status="available", eik=EIK,
          evidence=()):
    return pr.build("shop.bg", eik, pages, off, reg, reg_status, site_status,
                    {"evidence": list(evidence)})


VIES_OK = {"status": "confirmed", "name": "ТЕСТ ТЪРГОВИЯ - ЕООД", "address": "ул. Шипка №1 гр.СОФИЯ",
           "retrieved_at": "2026-09-23T00:00:00Z"}


# 1. ЕИК extraction and verification -------------------------------------------------

def test_eik_status_follows_the_strongest_source():
    pages = [page("https://shop.bg/kontakti", found={"eik": EIK})]
    assert build(pages)["fields"]["eik"]["status"] == "published"
    assert build(pages, off=VIES_OK)["fields"]["eik"]["status"] == "corroborated"   # VIES: not the register
    reg = {**REG[EIK], "status": "confirmed"}
    f = build(pages, off=VIES_OK, reg=reg, reg_status="available")["fields"]["eik"]
    assert f["status"] == "verified" and {s["type"] for s in f["sources"]} >= {"registry", "vies"}


def test_same_company_with_a_different_eik_is_a_conflict():
    pages = [page("https://shop.bg/a", found={"eik": EIK},
                  mentions=[{"name": "Тест Търговия ЕООД", "eik": EIK, "operator": True,
                             "snippet": "..."}]),
             page("https://shop.bg/b", mentions=[{"name": "ТЕСТ ТЪРГОВИЯ ЕООД", "eik": EIK2,
                                                  "operator": False, "snippet": "..."}])]
    f = build(pages)["fields"]["eik"]
    assert f["status"] == "conflicting" and {v["value"] for v in f["values"]} == {EIK, EIK2}


# 2-3. normalisation keeps the raw value ------------------------------------------------

def test_company_name_normalisation_is_display_only():
    for raw in ("НаспърсКласифайдсБългарияЕООД", "“Наспърс Класифайдс България” ЕООД",
                "Наспърс Класифайдс България - ЕООД", "  Наспърс   Класифайдс  България ЕООД "):
        assert pr.display_name(raw) == "Наспърс Класифайдс България ЕООД", raw
    pages = [page("https://shop.bg/terms", legal=True,
                  mentions=[{"name": "НаспърсКласифайдсБългарияЕООД", "eik": EIK,
                             "operator": True, "snippet": "..."}])]
    f = build(pages)["fields"]["legal_name"]
    assert f["value"] == "Наспърс Класифайдс България ЕООД"
    assert f["sources"][0]["raw"] == "НаспърсКласифайдсБългарияЕООД"          # evidence intact


def test_address_normalisation_and_agreement():
    assert pr.display_address("ул. Ангел Кънчев №1 ет.3 обл.СОФИЯ, гр.СОФИЯ 1000") == \
        "ул. Ангел Кънчев № 1 ет. 3 обл. София, гр. София 1000"
    assert pr.addresses_agree("ул. Ангел Кънчев №1 ет.3 обл.СОФИЯ, гр.СОФИЯ 1000",
                              "гр. София, ул. “Ангел Кънчев” №1, ет. 3")
    assert pr.addresses_agree("Индустриална зона Божурище, ул. Летец Радул Милков №2, гр.БОЖУРИЩЕ",
                              "гр. Божурище (ПК 2227)")                 # partial, consistent
    assert not pr.addresses_agree("гр. София, бул. Витоша 15", "гр. Пловдив, ул. Шипка 1")
    assert not pr.plausible_address("и адрес на")
    assert pr.clean_address("и адрес на управление: гр. София, ул. Шипка 1") == "гр. София, ул. Шипка 1"


# 4. VAT / VIES -------------------------------------------------------------------------

def test_vat_verified_by_vies_and_statuses_kept_apart():
    pages = [page("https://shop.bg/k", found={"eik": EIK})]
    ok = build(pages, off=VIES_OK)["fields"]
    assert ok["vat_number"] == {**ok["vat_number"], "value": f"BG{EIK}", "status": "verified"}
    assert ok["vat_status"]["value"] == "valid" and ok["vat_status"]["status"] == "verified"
    no = build(pages, off={"status": "not_found"})["fields"]
    assert no["vat_status"]["value"] == "not_registered" and no["vat_number"]["status"] == "not_found"
    down = build(pages, off={"status": "unavailable"})["fields"]
    assert down["vat_status"]["status"] == "unavailable" and down["vat_status"]["reason"] == "vies_unavailable"


def test_vies_never_vouches_for_phone_email_or_domain():
    f = build([page("https://shop.bg/k", found={"eik": EIK})], off=VIES_OK)["fields"]
    for k in ("phone", "email", "domain_association"):
        assert all(s["type"] != "vies" for s in f[k]["sources"]), k


# 5-6. extraction -----------------------------------------------------------------------

def test_phone_and_email_extraction_from_a_page():
    c = dc.contacts('<a href="tel:+35929704400">x</a><a href="mailto:office@shop.bg">m</a>',
                    "Телефон: 02 970 4400, GSM 0888 123 456. Пишете на ivan.petrov@gmail.com "
                    "или sales@shop.bg. Поръчка 123456789. Жалби до КЗЛД: kzld@cpdp.bg",
                    {}, "shop.bg", False)
    assert {pr.phone_key(p) for p in c["phones"]} == {"+35929704400", "+359888123456"}
    # The shop publishes the Gmail address as its contact: kept. The regulator's is not
    # the shop's contact, even though the page prints it.
    assert c["emails"] == ["office@shop.bg", "ivan.petrov@gmail.com", "sales@shop.bg"]
    assert pr.display_phone("+359 0700 10 800") == "+359 700 10 800"
    assert pr.phone_key("*9090") is None


def test_phone_and_email_come_from_the_operator_page_through_the_pipeline():
    page_html = contact() + "<p>Телефон: 02 970 4400</p>"
    r = run("shop.bg", {"https://shop.bg/": page_html}, vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"})
    f = r["profile"]["fields"]
    assert f["phone"]["value"] == "+359 2 970 4400" and f["phone"]["status"] == "published"
    assert f["email"]["value"] == "office@shop.bg"
    assert f["phone"]["sources"][0]["url"] == "https://shop.bg/"


# 7-8. missing ---------------------------------------------------------------------------

def test_missing_phone_and_email_are_not_found_not_guessed():
    f = build([page("https://shop.bg/k", found={"eik": EIK})])["fields"]
    for k in ("phone", "email"):
        assert f[k]["value"] is None and f[k]["status"] == "not_found"
        assert "first-party" in f[k]["note"]


# 9-11. conflicts -------------------------------------------------------------------------

def test_conflicting_phone_between_independent_sources():
    pages = [page("https://shop.bg/kontakti", phones=["02 111 1111"]),
             page("https://shop.bg/kontakti", phones=["02 222 2222"], archived="20240101000000")]
    f = build(pages)["fields"]["phone"]
    assert f["status"] == "conflicting"
    assert {v["value"] for v in f["values"]} == {"+359 2 111 1111", "+359 2 222 2222"}


def test_several_numbers_on_one_site_are_not_a_conflict():
    f = build([page("https://shop.bg/k", phones=["02 111 1111", "0888 123 456"])])["fields"]["phone"]
    assert f["status"] == "published" and len(f["values"]) == 2


def test_shared_number_across_sources_is_corroborated():
    pages = [page("https://shop.bg/kontakti", phones=["02 111 1111", "0888 123 456"]),
             page("https://shop.bg/kontakti", phones=["+359 2 111 1111"], archived="20240101000000")]
    f = build(pages)["fields"]["phone"]
    assert f["status"] == "corroborated" and f["value"] == "+359 2 111 1111"


def test_conflicting_email_between_independent_sources():
    pages = [page("https://shop.bg/k", emails=["office@shop.bg"]),
             page("https://help.shop.bg/terms", emails=["info@shop.bg"], legal=True)]
    f = build(pages)["fields"]["email"]
    assert f["status"] == "conflicting" and len(f["values"]) == 2


def test_conflicting_address_keeps_both_with_their_sources():
    reg = {**REG[EIK], "status": "confirmed"}
    pages = [page("https://shop.bg/kontakti", found={"eik": EIK, "address": "гр. Пловдив, бул. Руски 5"})]
    f = build(pages, reg=reg, reg_status="available")["fields"]["registered_address"]
    assert f["status"] == "conflicting"
    by = {v["value"]: {s["type"] for s in v["sources"]} for v in f["values"]}
    assert by == {"гр. София, ул. Шипка 1": {"registry"}, "гр. Пловдив, бул. Руски 5": {"company_site"}}
    assert f["value"] == "гр. София, ул. Шипка 1"             # shown first: the register's


# 12. archived contact information --------------------------------------------------------

def test_archived_contact_information_is_marked_archived():
    archive = FakeArchive({"https://shop.bg/": '<a href="/kontakti">Контакти</a>',
                           "https://shop.bg/kontakti": contact() + "<p>Тел: 02 970 4400</p>"})
    r = run("shop.bg", {"https://shop.bg/": CHALLENGE}, archive=archive,
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"}, site_kw={"status": {"https://shop.bg/": 403}})
    ph = r["profile"]["fields"]["phone"]
    assert ph["value"] == "+359 2 970 4400"
    assert ph["sources"][0]["type"] == "company_archive" and ph["sources"][0]["archived"]


# 13. not_found vs unavailable vs unconfigured vs conflicting ---------------------------

def test_missing_is_not_the_same_as_unchecked():
    blocked = build([], site_status="blocked")["fields"]
    assert blocked["phone"]["status"] == "unavailable" and blocked["phone"]["reason"] == "site_blocked"
    read = build([page("https://shop.bg/k", found={"eik": EIK})])["fields"]
    assert read["phone"]["status"] == "not_found"
    no_reg = build([], eik=None)["fields"]["registered_address"]
    assert no_reg["status"] == "unavailable" and no_reg["reason"] == "registry_unconfigured"
    reg_empty = build([], eik=None, reg_status="available")["fields"]["registered_address"]
    assert reg_empty["status"] == "not_found" and reg_empty["reason"] == "registry_no_record"


def test_register_unconfigured_and_available_without_record_stay_distinct():
    r1 = run("shop.bg", {"https://shop.bg/": contact()})
    assert r1["discovery"]["registry"] == {**r1["discovery"]["registry"], "status": "unconfigured",
                                           "outcome": None}
    r2 = run("shop.bg", {"https://shop.bg/": contact()}, registry=FakeRegistry({}))
    assert r2["discovery"]["registry"]["status"] == "available"
    assert r2["discovery"]["registry"]["outcome"] == "no_evidence_found"


def test_registry_fills_identity_and_contacts_as_verified():
    reg = FakeRegistry(REG)
    r = run("shop.bg", {"https://shop.bg/": contact()}, registry=reg,
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"})
    f = r["profile"]["fields"]
    assert r["profile"]["identity_status"] == "verified"
    for k in ("eik", "legal_name", "legal_form", "registered_address", "phone", "email",
              "domain_association"):
        assert f[k]["status"] == "verified", (k, f[k])
    assert r["profile"]["summary"]["verified"] >= 8


def test_missing_phone_does_not_lower_domain_resolution():
    r = run("shop.bg", {"https://shop.bg/": f"<p>ЕИК: {EIK}</p><p>„Тест Търговия“ ЕООД</p>"},
            vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"})
    assert r["profile"]["fields"]["phone"]["status"] == "not_found"
    assert r["domain_resolution"]["confidence_level"] == "high"
    # Several disclosure shortfalls at once (no phone, e-mail, address, VAT number) are
    # compliance findings; they never pull a clearly identified operator down.
    f = r["profile"]["fields"]
    assert {f[k]["status"] for k in ("phone", "email")} == {"not_found"}
    assert {"NO_ADDRESS", "NO_CONTACT"} <= {x["code"] for x in r["disclosure"]["assessment"]["findings"]}
    assert r["domain_resolution"]["confidence_level"] == "high"


def test_summary_counts_are_derived_from_field_states():
    r = run("shop.bg", {"https://shop.bg/": contact()}, vies={f"BG{EIK}": "ТЕСТ ТЪРГОВИЯ ЕООД"})
    p = r["profile"]
    states = [f["status"] for f in p["fields"].values()]
    for k in ("verified", "corroborated", "published", "conflicting", "not_found", "unavailable"):
        assert p["summary"][k] == states.count(k)
    assert p["summary"]["found"] == sum(1 for f in p["fields"].values() if f["value"] is not None)


# 14-15. the OLX and Technopolis shapes, offline ------------------------------------------

def test_olx_shape_archived_legal_document_and_vies_keep_their_roles():
    pdf = _pdf("администраторът на уебсайта Market.bg - дружеството “Маркет Плейс България” "
               f"ЕООД, регистрирано в Търговския регистър, с ЕИК {EIK3}, със седалище и адрес на "
               "управление: гр. София, ул. Ангел Кънчев №1, ет. 3. Е-поща: contact@market.bg")
    refused = {u: 403 for u in ("https://market.bg/", "https://developer.market.bg/",
                                "https://developer.market.bg/robots.txt")}
    archive = FakeArchive({
        "https://developer.market.bg/bg": '<a href="/bg/terms-and-conditions">Общи условия</a>',
        "https://developer.market.bg/bg/terms-and-conditions": '<a href="/doc/r.bg.pdf">Общи условия</a>',
        "https://developer.market.bg/doc/r.bg.pdf": pdf})
    r = run("market.bg", {}, archive=archive,
            vies={f"BG{EIK3}": "МАРКЕТ ПЛЕЙС БЪЛГАРИЯ - ЕООД"},
            get_kw={"wildcard": "w.example.", "hosts": {"developer.market.bg": "p.example."}},
            site_kw={"status": refused})
    f = r["profile"]["fields"]
    assert r["company"]["eik"] == EIK3 and r["domain_resolution"]["confidence_level"] == "high"
    assert {s["type"] for s in f["domain_association"]["sources"]} == {"company_archive"}
    assert {s["type"] for s in f["vat_number"]["sources"]} == {"vies"}
    assert f["registered_address"]["status"] == "published"      # no VIES address in this fake
    assert f["email"]["value"] == "contact@market.bg"


def test_definition_list_contacts_stay_with_their_own_company():
    text = ('"ТЕХНОПОЛИС" – означава "Технополис България" ЕАД със седалище гр. София, ЕИК '
            '130858590, тел: 070012340, e-mail: shop@technopolis.bg , дружество – собственик на '
            'онлайн магазина на www.technopolis.bg "ПРАКТИКЕР" – означава „Практикер Ритейл“ ЕООД '
            'със седалище гр. София, ЕИК 200525782, тел: 070010045, e-mail: shop@praktiker.bg')
    ms = dc.company_mentions(text, "technopolis.bg")
    p = page("https://technopolis.bg/terms", legal=True, mentions=ms,
             phones=["070012340", "070010045"], emails=["shop@technopolis.bg", "shop@praktiker.bg"])
    f = pr.build("technopolis.bg", "130858590", [p], None, None, "unconfigured", "available",
                 {"evidence": []})["fields"]
    assert [v["value"] for v in f["phone"]["values"]] == ["+359 700 12 340"]
    assert [v["value"] for v in f["email"]["values"]] == ["shop@technopolis.bg"]


def test_nap_declared_name_corroborates_or_stands_in_for_the_company_name():
    from shopwatch import sources

    def nap(name):
        return lambda *a: sources.state(sources.AVAILABLE, sources.FOUND, entries=[
            {"eik": EIK, "declared_name": name, "shop_name": "shop", "declared_domain": "shop.bg",
             "declared_at": "01.01.2024", "retrieved_at": "2026-09-24T00:00:00Z"}])
    # The site names the company; NAP (without the legal form) agrees: corroboration.
    r = run("shop.bg", {"https://shop.bg/": contact()}, nap_source=nap("ТЕСТ ТЪРГОВИЯ"))
    ln = r["profile"]["fields"]["legal_name"]
    assert {s["type"] for s in ln["sources"]} >= {"nap_register"} and ln["status"] == "corroborated"
    # Nobody else names it (not in VIES, nothing on the site): the declared name is shown.
    r = run("shop.bg", {"https://shop.bg/": "<p>Магазин за играчки</p>"}, nap_source=nap("СТАНДАРТ 34"))
    ln = r["profile"]["fields"]["legal_name"]
    assert ln["value"] == "СТАНДАРТ 34" and ln["sources"][0]["type"] == "nap_register"
    assert r["domain_resolution"]["confidence_level"] == "high"
