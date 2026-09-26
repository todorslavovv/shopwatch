"""Live acceptance runs against real sites. Network required; off by default.

    SHOPWATCH_LIVE=1 python -m pytest tests/test_live.py -v

The expected answers are the public record, used only to check the result: nothing in
the resolver knows about any of these domains. olx.bg is the hard case - a script-built
site on wildcard DNS whose operator is named only in a PDF on a developer portal.
"""
import os

import pytest

from shopwatch import domaincheck as dc

pytestmark = pytest.mark.skipif(os.environ.get("SHOPWATCH_LIVE") != "1",
                                reason="live network test; set SHOPWATCH_LIVE=1")

EXPECTED = [
    ("https://www.olx.bg", "175345805"),         # Наспърс Класифайдс България ЕООД
    ("technomarket.bg", "200586330"),
    ("jarcomputers.com", "131418803"),
    ("zora.bg", "121839316"),
]


@pytest.mark.parametrize("url,eik", EXPECTED)
def test_known_operator_is_identified(url, eik):
    r = dc.check(url, refresh=True)
    assert r["domain_resolution"]["company_identified"], r["domain_resolution"]
    assert r["company"]["eik"] == eik
    assert r["domain_resolution"]["confidence_level"] in ("high", "medium")


def test_olx_resolves_regardless_of_direct_fetch_status():
    r = dc.check("https://www.olx.bg", refresh=True)
    assert r["company"]["eik"] == "175345805"
    assert "Наспърс" in r["company"]["name"]
    assert any(s["type"] == "company_source" for s in r["sources"])


def test_blocked_site_never_returns_a_wrong_company():
    """technopolis.bg forbids all crawlers but the big search engines. Without a search
    key the honest answer may be 'not identified'; a wrong company is never acceptable."""
    r = dc.check("technopolis.bg", refresh=True)
    assert r["target_site"]["fetch_status"] == "blocked"
    if r["domain_resolution"]["company_identified"]:
        # Its archived terms name "Технополис България" ЕАД, ЕИК 130858590, as owner of
        # the online shop - and define a sister company a line later.
        assert r["company"]["eik"] == "130858590", r["company"]


LIVE_ALL = EXPECTED + [("technopolis.bg", "130858590")]


@pytest.mark.parametrize("url,eik", LIVE_ALL)
def test_compliance_profile_for_known_operators(url, eik):
    """14-18: the five acceptance domains still resolve, and their profile keeps VIES in
    its VAT role and first-party sources in the domain -> operator role."""
    r = dc.check(url, refresh=True)
    if url == "technopolis.bg" and not r["domain_resolution"]["company_identified"]:
        pytest.skip("archive unavailable this run; the resolver correctly declined to guess")
    f = r["profile"]["fields"]
    assert f["eik"]["value"] == eik and f["eik"]["status"] in ("verified", "corroborated")
    assert f["vat_number"]["value"] == f"BG{eik}" and f["vat_number"]["status"] == "verified"
    assert f["vat_status"]["value"] == "valid"
    assert f["legal_name"]["status"] in ("verified", "corroborated")
    assert all(s["type"] != "vies" for s in f["domain_association"]["sources"])
    assert f["domain_association"]["sources"], "a first-party source must link domain and company"
    assert r["domain_resolution"]["confidence_level"] == "high"
