"""The 20-shop benchmark: dataset integrity, the comparison mechanism, and the extraction
rules each benchmark gap led to. No network: every page here is a fixture modelled on a
real page quoted in benchmark/shops.json."""
import json
import re
from pathlib import Path

import pytest

from benchmark import run
from shopwatch import sources
from shopwatch.disclosure import extract_merchant
from shopwatch.domaincheck import company_mentions
from shopwatch.eik import validate_eik
from shopwatch.investigate import investigate
from shopwatch.privacy import classify_email
from tests.test_investigate import fake_fetch

ROOT = Path(__file__).resolve().parent.parent
TRUTH = run.load()
SYNTHETIC = {"mega-promo-bg.sbs", "luxury-watches24.cfd", "bg-outlet-sale.icu", "parfum-store.one",
             "elektro-market.bg", "moda-sofia.shop", "tehnomag-bg.com", "detski-igri.eu"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ------------------------------------------------------------------ dataset integrity

def test_twenty_distinct_real_shops_none_synthetic():
    doms = [s["domain"] for s in TRUTH["shops"]]
    assert len(doms) == 20 and len(set(doms)) == 20
    assert not SYNTHETIC & set(doms)
    assert {r["domain"] for r in TRUTH["removed_examples"]} == SYNTHETIC
    assert not {r["domain"] for r in TRUTH["excluded_candidates"]} & set(doms)


def test_every_shop_was_discovered_in_the_nap_list_with_a_date():
    for s in TRUTH["shops"]:
        decl = s["official"]["nap_declarations"]
        assert decl, s["domain"]
        for d in decl:
            assert validate_eik(d["eik"])["valid"] and d["declared_at"] and ISO.match(d["retrieved_at"])
            assert run._digits(d["declared_domain"]) == run._digits(s["domain"]) or \
                s["domain"].split(".")[0] in d["declared_domain"].lower()


def test_every_shortfall_and_every_published_value_has_a_timestamped_source():
    for s in TRUTH["shops"]:
        assert s["shortfalls"], s["domain"]
        for sf in s["shortfalls"]:
            assert sf["summary_en"] and sf["summary_bg"] and sf["evidence"], (s["domain"], sf["code"])
            for e in sf["evidence"]:
                assert e["url"].startswith("https://") and ISO.match(e["retrieved_at"])
                assert e["source_type"] in ("company_site", "company_document", "nap_register", "vies")
        for k, f in s["published"].items():
            for e in f.get("evidence", []):
                assert e["url"].startswith("https://") and ISO.match(e["retrieved_at"]), (s["domain"], k)
                assert "current" in e and e.get("excerpt"), (s["domain"], k)


def test_wording_is_neutral():
    banned = re.compile(r"(?i)fraud|scam|illegal|измам|незаконн|престъп")
    for s in TRUTH["shops"]:
        for sf in s["shortfalls"]:
            assert not banned.search(sf["summary_en"] + sf["summary_bg"]), (s["domain"], sf["code"])


def test_shortfalls_agree_with_the_recorded_values():
    for s in TRUTH["shops"]:
        codes = {x["code"] for x in s["shortfalls"]}
        eik = s["published"]["eik"]
        if "EIK_INVALID" in codes:
            assert not validate_eik(eik["value"])["valid"]
        if "NO_EIK" in codes:
            assert not eik["published"]
        if "VAT_NOT_DISCLOSED" in codes:
            assert s["official"]["vies"]["status"] == "confirmed"
            assert not s["published"]["vat_number"]["published"]
        if "EIK_OTHER_COMPANY" in codes:
            declared = {d["eik"] for d in s["official"]["nap_declarations"]}
            assert validate_eik(eik["value"])["valid"] and eik["value"] not in declared


def test_public_worklist_is_exactly_the_verified_shops_with_what_they_publish():
    pub = json.loads((ROOT / "web" / "sample_report.json").read_text(encoding="utf-8"))
    assert [s["domain"] for s in pub["shops"]] == [s["domain"] for s in TRUTH["shops"]]
    text = json.dumps(pub, ensure_ascii=False)
    assert not any(d in text for d in SYNTHETIC)
    # A shop's advertised contact is shown whatever its provider...
    bulbs = next(s for s in pub["shops"] if s["domain"] == "bulbsandleds.com")
    assert bulbs["fields"]["email"]["value"] == ["bulbsandleds.com@gmail.com"]
    # ...the unrelated people's mailboxes left in biancandfred's template text are not.
    assert "t-online.de" not in text and "allexanderhuston" not in text
    sm = pub["summary"]
    assert sm["findings"] == sum(len(s["shortfalls"]) for s in pub["shops"])
    assert sm["findings"] == sm["shortfalls"] + sm["information"]


def test_vat_non_disclosure_is_information_not_a_shortfall():
    for s in TRUTH["shops"]:
        for x in s["shortfalls"]:
            if x["code"] == "VAT_NOT_DISCLOSED":
                assert x["kind"] == "information"
                assert "not publicly disclosed" in x["summary_en"] and "valid according to VIES" in x["summary_en"]
                assert not re.search(r"(?i)shortfall|violation|breach|required", x["summary_en"])
            else:
                assert x["kind"] == "shortfall"
    assert all(any(x["kind"] == "shortfall" for x in s["shortfalls"]) for s in TRUTH["shops"])


# ------------------------------------------------------------------ benchmark mechanism

def _obs(domain, merchant=None, codes=(), reachable=True, dc_eik=None, fields=None, conflicts=()):
    return {"domain": domain, "observed_at": "2026-09-23T00:00:00Z",
            "triage": {"merchant": {k: None for k in run.TRIAGE_KEY.values()} | (merchant or {}),
                       "codes": list(codes), "reachable": reachable, "pages": []},
            "domaincheck": {"eik": dc_eik, "conflicts": list(conflicts), "pages": [],
                            "fields": fields or {}}}


def test_external_evidence_that_shopwatch_missed_is_a_gap_not_an_error():
    shop = next(s for s in TRUTH["shops"] if s["domain"] == "kornelv.com")
    obs = _obs("kornelv.com", {"email": "eshop@kornelv.com"}, codes=["NO_EIK"], dc_eik="130334366")
    rep = run.report({"shops": [shop]}, [obs], gaps={})
    row = run.compare(shop, obs)["fields"]
    assert row["email"]["coverage"] == "found_by_deterministic_tooling"
    assert row["phone"]["coverage"] == "not_found_by_deterministic_tooling"
    assert row["eik"]["coverage"] == "not_found_anywhere"               # correctly absent
    assert row["domain_operator"]["coverage"] == "found_by_deterministic_tooling"
    gap = next(g for g in rep["gaps"] if g.get("field") == "phone")
    assert gap["reason"] == "unexplained"                               # flagged, not failed
    explained = run.report({"shops": [shop]}, [obs], gaps={"kornelv.com:phone": {
        "reason": "phone only in a footer image", "improved": False}})
    assert next(g for g in explained["gaps"] if g.get("field") == "phone")["reason"] \
        == "phone only in a footer image"


def test_coverage_classes():
    shop = next(s for s in TRUTH["shops"] if s["domain"] == "dobytoys.com")
    down = run.compare(shop, _obs("dobytoys.com", reachable=False))["fields"]
    assert down["phone"]["coverage"] == "unavailable_to_deterministic_tooling"
    wrong = run.compare(shop, _obs("dobytoys.com", {"phone": "0888000000"}))["fields"]
    assert wrong["phone"]["coverage"] == "conflicting"
    ai = run.compare(shop, _obs("dobytoys.com", fields={"phone": {"status": "ai_assisted"}}))
    assert ai["fields"]["phone"]["coverage"] == "found_by_ai_fallback"
    extra = run.compare(shop, _obs("dobytoys.com", {"vat_number": "BG206345126"}))["fields"]
    assert extra["vat_number"]["coverage"] == "conflicting"            # not what the site shows


def test_shortfall_reproduction_and_false_alarms():
    shop = next(s for s in TRUTH["shops"] if s["domain"] == "danishgranola.bg")
    obs = _obs("danishgranola.bg", codes=["NO_EIK", "NO_ADDRESS", "NO_PHONE"])
    c = run.compare(shop, obs)
    assert c["shortfalls"]["NO_ADDRESS"] and c["shortfalls"]["NO_PHONE"]
    assert c["false_alarms"] == ["NO_EIK"]            # the VAT number disclosed the ЕИК


def test_operator_resolution_is_reported_with_and_without_nap():
    shops = [s for s in TRUTH["shops"] if s["domain"] in ("kornelv.com", "bigvip.bg", "detetovteb.com")]
    obs = [_obs("kornelv.com", dc_eik="130334366"), _obs("bigvip.bg", dc_eik="205550384"),
           _obs("detetovteb.com", dc_eik="204565892")]
    obs[0]["domaincheck"]["without_nap"] = {"eik": "130334366", "confidence": "high"}   # own pages
    obs[1]["domaincheck"]["without_nap"] = {"eik": None, "confidence": "insufficient"}  # NAP only
    assert run.same_company("Панколита 2 ООД", "ПАНКОЛИТА 2")
    assert not run.same_company("Диверсо Хеър ЕООД", "ЕЛЪН ХЕЪР БУТИК")
    obs[2]["domaincheck"]["without_nap"] = {"eik": "148089312", "confidence": "high"}  # site's own
    op = run.report({"shops": shops}, obs, {})["domain_operator"]
    assert op["domain_operator"] == {"correct": 3, "named_without_eik": 0,
                                     "different_company": 0, "not_identified": 0}
    assert op["domain_operator_without_nap"] == {"correct": 1, "named_without_eik": 0,
                                                 "different_company": 1, "not_identified": 1}


# ------------------------------------------------------------------ extraction gaps closed

def test_labels_are_whole_words():
    # detetovteb.com: "Quickview" contains "uic"; the model number is no ЕИК.
    assert extract_merchant("<p>Quickview</p><p>Ново Model: 490156854</p>")["eik"] is None
    m = extract_merchant("<p>Data for entry in the Commercial or other register, UIC</p><p>148089312</p>")
    assert m["eik"] == "148089312"


def test_english_identification_code_label():
    # cattleyahome.eu
    m = extract_merchant("<p>Identification Code/Number: 205470828</p>")
    assert m["eik"] == "205470828"


def test_prose_after_a_label_is_not_a_company_or_an_address():
    m = extract_merchant("<p>Дружеството не носи отговорност при невярност на информацията.</p>"
                         "<p>Адрес за фактуриране, плащане и доставка се попълва при поръчка.</p>")
    assert m["company_name"] is None and m["address"] is None


def test_company_needs_a_legal_form_and_names_in_operator_context():
    # elanhairboutique.com, kornelv.com, detski-magazin.bg, a courier in delivery terms
    assert extract_merchant("<p>Интернет магазинът е собственост на Диверсо Хеър ЕООД.</p>")[
        "company_name"] == "Диверсо Хеър ЕООД"
    assert extract_merchant("<p>С настоящите Общи условия Корнел В ЕООД уведомява потребителите</p>")[
        "company_name"] == "Корнел В ЕООД"
    assert extract_merchant("<p>„Панколита 2“ ООД е създадена през 2011 г. в гр.Варна.</p>")[
        "company_name"] == "Панколита 2 ООД"
    assert extract_merchant("<p>Доставка чрез Еконт Експрес ООД до офис.</p>")["company_name"] is None
    assert extract_merchant("<p>Търговско наименование: David Toys</p>")["company_name"] is None


def test_address_heading_contact_block_and_label_debris():
    assert extract_merchant("<p>Адрес на магазина:</p><p>бул. „Александър Малинов“ 16, 1784 "
                            "Младост 1, София</p>")["address"].startswith("бул. „Александър Малинов“ 16")
    assert extract_merchant("<p>info@elveszett.com +359 877 211 226 Elveszett ж.к. Люлин 7, бл. 705, "
                            "вх. В</p>")["address"] == "ж.к. Люлин 7, бл. 705, вх. В"
    assert extract_merchant("<p>ЕООД със седалище и адрес на управление: гр. София, ж.к. "
                            "Гевгелийски, бл. 2</p>")["address"] == "гр. София, ж.к. Гевгелийски, бл. 2"
    assert extract_merchant("<p>Адрес: Sofia | Bulgaria</p>")["address"] is None    # no number


def test_unlabelled_international_phone():
    # biancandfred.com "Call us at: +359878429243"
    assert extract_merchant("<p>Call us at: +359878429243</p>")["phone"] == "+359878429243"


def test_vat_number_discloses_the_eik_and_bg_eik_is_the_vat_number():
    page = "<html><body><p>Органик Бранд ЕООД ДДС номер: BG207654830</p></body></html>"
    r = investigate("https://shop.bg/", fetcher=fake_fetch({"https://shop.bg/": page}),
                    verifier=lambda e: {"status": "confirmed", "name": "Органик Бранд - ЕООД"})
    assert r["merchant"]["eik"] == "207654830" and r["eik_from_vat"]
    assert "NO_EIK" not in {f["code"] for f in r["assessment"]["findings"]}
    page = "<html><body><p>„Айви Лайф 9“ ООД, ЕИК BG208475759</p></body></html>"
    r = investigate("https://shop.bg/", fetcher=fake_fetch({"https://shop.bg/": page}),
                    verifier=lambda e: {"status": "confirmed", "name": "Айви Лайф 9 - ООД"})
    assert r["merchant"]["eik"] == "208475759" and r["merchant"]["vat_number"] == "BG208475759"


def test_different_legal_forms_across_pages_are_reported():
    home = '<html><body><p>Органик Бранд ЕООД ДДС номер: BG207654830</p><a href="/privacy">Политика за поверителност</a></body></html>'
    privacy = "<html><body><p>Информация за собственика Органик Бранд ООД</p></body></html>"
    r = investigate("https://shop.bg/", fetcher=fake_fetch({"https://shop.bg/": home,
                                                            "https://shop.bg/privacy": privacy}),
                    verifier=lambda e: {"status": "confirmed", "name": "x"})
    assert "COMPANY_INCONSISTENT" in {f["code"] for f in r["assessment"]["findings"]}


def test_ownership_phrase_names_the_operator():
    ms = company_mentions("Интернет магазинът shop.bg е собственост на Диверсо Хеър ЕООД.", "shop.bg")
    assert any(m["operator"] for m in ms)


def test_mailboxes_are_classified_by_context_not_by_provider():
    assert classify_email("besteesrock@gmail.com", "besteesrock.com") == "corporate"
    assert classify_email("bulbsandleds.com@gmail.com", "bulbsandleds.com") == "corporate"
    assert classify_email("pankolita@abv.bg", "detski-magazin.bg") == "published_contact"
    assert classify_email("info@kzp.bg", "detski-magazin.bg") == "institution"


# ------------------------------------------------------------------ NAP adapter

def _nap_getter(rows_by_filter):
    calls = []
    def get(url, timeout=12):
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        calls.append(q)
        key = ("shopname", q["shopname"]) if q["shopname"] else ("shopwebaddr", q["shopwebaddr"])
        return {"data": rows_by_filter.get(key, [])}
    get.calls = calls
    return get


def test_nap_domain_declared_in_capitals_is_found_through_the_shop_name():
    # The list's address filter is case-sensitive: "CHUNCHE-BG.COM" never matches.
    get = _nap_getter({("shopname", "chunche-bg"): [
        {"DOMAIN": "CHUNCHE-BG.COM", "PLATFORM_DOMAIN": None, "SHOPNAME": "CHUNCHE-BG",
         "OWNER_INFO": "Наименование: ЧУНЧЕ<br/>ЕИК: 131467896", "REGDATE": "16.08.2019"},
        {"DOMAIN": "chunche-bg.com.example.org", "PLATFORM_DOMAIN": None,
         "OWNER_INFO": "ЕИК: 205550384"}]})
    r = sources.nap_eshops("chunche-bg.com", getter=get)
    assert [e["eik"] for e in r["entries"]] == ["131467896"]
    assert [c["shopwebaddr"] for c in get.calls] == ["chunche-bg.com", ""]


def test_nap_marketplace_rows_and_unreachable_list():
    get = _nap_getter({("shopwebaddr", "shop.bg"): [
        {"DOMAIN": " https://www.shop.bg/", "PLATFORM_DOMAIN": "olx.bg", "OWNER_INFO": "ЕИК: 131467896"}]})
    assert sources.nap_eshops("shop.bg", getter=get, pause=0)["entries"] == []
    def down(url, timeout=12):
        raise OSError("timeout")
    assert sources.nap_eshops("shop.bg", getter=down, pause=0)["status"] == sources.TEMP_UNAVAILABLE


def test_nap_empty_answer_under_load_is_retried_before_concluding_not_declared():
    row = {"DOMAIN": "www.dobytoys.com", "PLATFORM_DOMAIN": None,
           "OWNER_INFO": "Наименование: ДОБИ ТОЙС<br/>ЕИК: 207877153", "REGDATE": "29.11.2024"}
    calls = []
    def flaky(url, timeout=12):
        calls.append(url)
        return {"data": [row] if len(calls) > 2 else []}      # first pass: empty
    r = sources.nap_eshops("dobytoys.com", getter=flaky, pause=0)
    assert [e["eik"] for e in r["entries"]] == ["207877153"]
    assert r["entries"][0]["declared_name"] == "ДОБИ ТОЙС"


def test_link_budget_reaches_terms_and_privacy():
    # concretta.eu: "About Dura" is a product page; the privacy policy names the trader.
    from shopwatch.investigate import disclosure_links
    html = ('<a href="/dura/">About Dura</a><a href="/about-us/">About us</a>'
            '<a href="/contact/">Contact</a><a href="/terms-conditions/">Terms</a>'
            '<a href="/privacy-policy/">Privacy</a>')
    assert [u.rsplit("/", 2)[-2] for u in disclosure_links(html, "https://shop.bg/")[:4]] == \
        ["about-us", "contact", "terms-conditions", "privacy-policy"]


def test_web_agency_credit_is_not_the_trader():
    # besteesrock.com footer
    assert extract_merchant("<p>Designed by Vi Rock Bg LTD</p>")["company_name"] is None
    assert extract_merchant("<p>Concretta.eu, a part of Concreta 2017 Ltd., collects data.</p>")[
        "company_name"] == "Concreta 2017 Ltd."


def test_address_split_over_two_elements_after_its_label():
    # chunche-bg.com contact page
    assert extract_merchant("<p>Адрес</p><p>гр. София,</p><p>ул. Околовръстен път 253</p>"
                            "<p>Обадете ни се</p>")["address"] == "гр. София, ул. Околовръстен път 253"


def test_recorded_benchmark_run_has_every_gap_explained():
    # The committed observation reproduces the committed report; counts are derived.
    observed = json.loads((ROOT / "benchmark" / "observed.json").read_text(encoding="utf-8"))
    gaps = json.loads((ROOT / "benchmark" / "gaps.json").read_text(encoding="utf-8"))
    rep = run.report(TRUTH, observed, gaps)
    assert rep["shops"] == 20 and rep["unexplained"] == 0
    assert rep["shortfalls"]["total"] == sum(len(s["shortfalls"]) for s in TRUTH["shops"])
    for f, cov in rep["coverage"].items():
        assert sum(cov.values()) == 20, f
    assert rep["ai_fallback_used"] == 0


def test_a_word_ending_in_a_legal_form_is_not_a_company():
    # besteesrock.com's product list and bigvip.bg's terms: "MOTORH" + "EAD", "FL" + "OOD"
    assert company_mentions("T-SHIRTS | MOTORHEAD MOTORHEAD NO REMORSE", "shop.bg") == []
    assert company_mentions("in case of FLOOD or fire", "shop.bg") == []
    assert [m["name"] for m in company_mentions("Наспърс Класифайдс БългарияЕООД, ЕИК 175345805",
                                                "olx.bg")] == ["Наспърс Класифайдс България ЕООД"]


def test_people_named_by_the_company_keep_their_role_and_nothing_glued_on():
    from shopwatch.domaincheck import representatives
    got = representatives("Управител: Илия Баев Web site: http://x.bg Управител: Мария Иванова Тел: 0888 "
                          "представлявано от управителя Иван Петров Иванов. в лицето на Тест Търговия ЕООД")
    assert [(p["name"], p["role"]) for p in got] == [
        ("Иван Петров Иванов", "manager"), ("Илия Баев", "manager"), ("Мария Иванова", "manager")]
