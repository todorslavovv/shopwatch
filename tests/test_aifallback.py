"""AI-assisted fallback: last resort, per field, off by default, never evidence by itself."""
import pytest

from shopwatch import aifallback as ai
from shopwatch import profile as pr
from shopwatch.store import Store


def field(value=None, status="not_found"):
    return {"value": value, "status": status, "sources": [], "values": [], "reason": None,
            "note": None}


def prof():
    fields = {"eik": field("204511873", "verified"), "phone": field(), "email": field(),
              "registered_address": field("гр. София, ул. Витоша 1", "conflicting")}
    return {"domain": "shop.bg", "identity_status": "verified", "fields": fields,
            "summary": pr.summarise(fields)}


def page(url="https://shop.bg/kontakti", phones=(), emails=(), host="shop.bg"):
    return {"url": url, "host": host, "retrieved_at": "2026-09-23T10:00:00Z", "archived": None,
            "legal_doc": False, "contacts": {"phones": list(phones), "emails": list(emails)},
            "found": {}, "mentions": []}


class Provider:
    name = "fake"

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def research(self, domain, field, known):
        self.asked.append(field)
        a = self.answers[field]
        if isinstance(a, Exception):
            raise a
        return a


ON = {**ai.DEFAULTS, "enabled": True, "eligible_fields": ["phone", "email", "registered_address"]}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "sw.db")


def test_disabled_by_default_and_without_a_config_file(tmp_path):
    assert ai.load_config(tmp_path / "missing.json")["enabled"] is False
    assert ai.PROVIDERS == {}                                   # no provider ships
    p = prof()
    run = ai.fill(p, "shop.bg", store=None, fetch_page=lambda u: None, cfg=ai.load_config(tmp_path / "x"))
    assert run["note"] == "disabled" and p["fields"]["phone"]["status"] == "not_found"


def test_enabled_without_a_provider_returns_the_deterministic_result(store):
    p = prof()
    run = ai.fill(p, "shop.bg", store=store, fetch_page=lambda u: None, cfg=ON)
    assert run["note"] == "no research provider configured"
    assert p["fields"]["phone"]["value"] is None


def test_only_missing_eligible_fields_are_asked_one_by_one(store):
    prov = Provider({"phone": None, "email": None})
    ai.fill(prof(), "shop.bg", store=store, fetch_page=lambda u: None, cfg=ON, provider=prov)
    # eik has a value, the address is conflicting: neither is ever sent to the AI.
    assert prov.asked == ["phone", "email"]


def test_value_confirmed_on_the_named_page_is_shown_as_ai_assisted(store):
    prov = Provider({"phone": {"value": "+359 2 970 4400", "source_url": "https://shop.bg/kontakti",
                               "excerpt": "Тел: +359 2 970 4400"}, "email": None})
    p = prof()
    ai.fill(p, "shop.bg", store=store, cfg=ON, provider=prov,
            fetch_page=lambda u: page(u, phones=["02 970 4400"]))
    f = p["fields"]["phone"]
    assert f["status"] == "ai_assisted" and f["coverage"] == "found_by_ai_fallback"
    src = f["sources"][0]
    assert src["url"] == "https://shop.bg/kontakti" and src["type"] == "company_site"
    assert src["discovery_method"] == "ai_assisted" and src["provider"] == "fake"
    assert p["summary"]["coverage"]["found_by_ai_fallback"] == 1
    assert p["fields"]["eik"]["status"] == "verified"            # nothing else moved


def test_ai_statement_without_reproducible_evidence_is_not_shown(store):
    prov = Provider({"phone": {"value": "+359 888 123 456", "source_url": "https://shop.bg/kontakti"},
                     "email": {"value": "office@shop.bg", "source_url": None}})
    p = prof()
    ai.fill(p, "shop.bg", store=store, cfg=ON, provider=prov,
            fetch_page=lambda u: page(u, phones=["02 970 4400"]))     # page lacks that number
    for k in ("phone", "email"):
        f = p["fields"][k]
        assert f["status"] == "not_independently_verified" and f["value"] is None
        assert f["ai_claim"]["value"]                                  # kept for audit only
    assert "does not contain" in p["fields"]["phone"]["ai_claim"]["note"]
    assert "no source" in p["fields"]["email"]["ai_claim"]["note"]


def test_cited_source_that_cannot_be_retrieved_is_shown_with_that_caveat(store):
    prov = Provider({"phone": {"value": "+359 2 970 4400", "source_url": "https://registry.example/x",
                               "excerpt": "Тел: +359 2 970 4400"}, "email": None})
    p = prof()
    ai.fill(p, "shop.bg", store=store, cfg=ON, provider=prov, fetch_page=lambda u: None)
    f = p["fields"]["phone"]
    assert f["status"] == "ai_assisted_unretrieved" and f["value"] == pr.display_phone("+359 2 970 4400")
    s = f["sources"][0]
    assert s["url"] == "https://registry.example/x" and s["retrieved"] is False
    assert s["type"] == "public_source" and s["discovery_method"] == "ai_assisted"
    assert f["coverage"] == "found_by_ai_fallback"
    # it never displaces a stronger source: a verified or conflicting field is not asked
    assert p["fields"]["eik"]["status"] == "verified"
    assert p["fields"]["registered_address"]["status"] == "conflicting"


def test_budgets_per_lookup_and_per_day(store):
    prov = Provider({"phone": None, "email": None, "registered_address": None})
    run = ai.fill(prof(), "shop.bg", store=store, fetch_page=lambda u: None, provider=prov,
                  cfg={**ON, "max_requests_per_lookup": 1})
    assert run["requests"] == 1 and run["fields"]["email"] == "lookup_budget_exhausted"
    run = ai.fill(prof(), "other.bg", store=store, fetch_page=lambda u: None, provider=prov,
                  cfg={**ON, "max_requests_per_day": 1})
    assert run["requests"] == 0 and run["fields"]["phone"] == "daily_budget_exhausted"


def test_answers_are_cached_compactly_and_reused(store):
    hit = {"value": "+359 2 970 4400", "source_url": "https://shop.bg/kontakti", "excerpt": "x" * 9000}
    prov = Provider({"phone": hit, "email": None})
    fetch = lambda u: page(u, phones=["02 970 4400"])
    ai.fill(prof(), "shop.bg", store=store, cfg=ON, provider=prov, fetch_page=fetch)
    ai.fill(prof(), "shop.bg", store=store, cfg=ON, provider=prov, fetch_page=fetch)
    assert prov.asked == ["phone", "email"]                            # second run: all cached
    cached = store.ai_cache_get("shop.bg", "phone", 30)
    assert set(cached) == {"value", "source_url", "excerpt", "provider", "discovered_at"}


def test_provider_failure_leaves_the_deterministic_result(store):
    prov = Provider({"phone": RuntimeError("quota"), "email": None})
    p = prof()
    run = ai.fill(p, "shop.bg", store=store, fetch_page=lambda u: None, cfg=ON, provider=prov)
    assert run["fields"]["phone"].startswith("provider_error")
    assert p["fields"]["phone"]["status"] == "not_found" and p["fields"]["phone"]["value"] is None
