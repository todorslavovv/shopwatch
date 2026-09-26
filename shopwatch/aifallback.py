"""AI-assisted research fallback: last resort, field by field, disabled by default.

Order of work, and this module is only step 4:

    1. deterministic first-party / public-source tooling      (domaincheck, profile)
    2. independent public-source verification                (VIES, register)
    3. when a benchmark shows a gap, improve 1-2 - not this
    4. only for a field still missing: ask a research provider WHERE it is published

The AI is a discovery mechanism; the public page it cites is the evidence. Shopwatch
fetches that URL itself, through the same guarded crawler, and re-extracts the field with
its own deterministic extractor:

    page read, value on it     ai_assisted              "independently verified from the
                                                         cited public source"
    page could not be read     ai_assisted_unretrieved  value shown with its cited source:
                                                         "source could not be independently
                                                         retrieved by Shopwatch"
    page read, value not on it not_independently_verified  the claim is kept for audit;
    or no source cited                                       the field shows no value

It never touches a field that already has a value or a conflict, never outranks the
register, VIES or a first-party page, and never runs when disabled, unconfigured, out of
budget or failing - the deterministic result is returned unchanged in all those cases.

No provider ships with Shopwatch, deliberately. Configure one in
~/.config/shopwatch/ai_fallback.json and register it in PROVIDERS.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Protocol

from shopwatch import profile as pr
from shopwatch.store import Store, now_iso

CONFIG_FILE = Path.home() / ".config" / "shopwatch" / "ai_fallback.json"
DEFAULTS = {"enabled": False, "provider": None, "max_requests_per_lookup": 2,
            "max_requests_per_day": 20, "eligible_fields": ["phone", "email"],
            "cache_days": 30}
ELIGIBLE_STATUS = ("not_found", "unavailable")     # never conflicting, never with a value


class ResearchProvider(Protocol):
    name: str

    def research(self, domain: str, field: str, known: dict) -> dict | None:
        """{"value", "source_url", "excerpt"} for ONE field, or None."""


PROVIDERS: dict[str, type] = {}      # none shipped; add e.g. {"my-llm": MyProvider}


def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def _matches(field: str, value: str, page: dict) -> bool:
    """Did Shopwatch's own extractor find this value on the page the AI pointed to?"""
    if field == "phone":
        want = pr.phone_key(value)
        return bool(want) and want in {pr.phone_key(p) for p in page["contacts"]["phones"]}
    if field == "email":
        want = pr.email_key(value)
        return bool(want) and want in {pr.email_key(e) for e in page["contacts"]["emails"]}
    if field == "registered_address":
        cands = [page["found"].get("address")] + [m.get("address") for m in page["mentions"]]
        return any(pr.addresses_agree(value, c) for c in cands if c)
    return False


def fill(prof: dict, domain: str, *, store: Store | None, fetch_page, cfg: dict | None = None,
         provider: ResearchProvider | None = None) -> dict:
    """Try the fallback on eligible missing fields. Returns the run record; mutates prof.

    fetch_page(url) -> page dict as Crawler.record makes it, or None.
    """
    cfg = cfg or load_config()
    run = {"enabled": bool(cfg.get("enabled")), "requests": 0, "fields": {}}
    if not prof or not cfg.get("enabled"):
        run["note"] = "disabled"
        return run
    provider = provider or (PROVIDERS[cfg["provider"]]() if cfg.get("provider") in PROVIDERS
                            else None)
    if provider is None:
        run["note"] = "no research provider configured"
        return run
    today = time.strftime("%Y-%m-%d")
    for field in cfg.get("eligible_fields") or []:
        f = prof["fields"].get(field)
        if not f or f.get("value") is not None or f["status"] not in ELIGIBLE_STATUS:
            continue                                   # found, conflicting, or not eligible
        hit = store.ai_cache_get(domain, field, cfg["cache_days"]) if store else None
        if hit is None:
            if run["requests"] >= cfg["max_requests_per_lookup"]:
                run["fields"][field] = "lookup_budget_exhausted"
                continue
            if store and store.ai_usage(today) >= cfg["max_requests_per_day"]:
                run["fields"][field] = "daily_budget_exhausted"
                continue
            run["requests"] += 1
            if store:
                store.ai_usage_add(today)
            known = {k: v.get("value") for k, v in prof["fields"].items() if v.get("value")}
            try:
                hit = provider.research(domain, field, known) or {}
            except Exception as e:                    # provider down, quota, bad answer
                run["fields"][field] = f"provider_error: {type(e).__name__}"
                continue
            hit = {k: (str(hit[k])[:lim] if hit.get(k) is not None else None)
                   for k, lim in (("value", 300), ("source_url", 500), ("excerpt", 300))}
            hit.update(provider=provider.name, discovered_at=now_iso())
            if store:
                store.ai_cache_put(domain, field, hit)
        _apply(prof, field, hit, fetch_page, run)
    prof["summary"] = pr.summarise(prof["fields"])
    return run


def _apply(prof: dict, field: str, hit: dict, fetch_page, run: dict) -> None:
    f = prof["fields"][field]
    if not hit.get("value"):
        f["coverage"] = "not_found_anywhere"
        run["fields"][field] = "not_found"
        return
    disp = {"phone": pr.display_phone, "email": pr.email_key,
            "registered_address": pr.display_address}[field](hit["value"])
    page = fetch_page(hit["source_url"]) if hit.get("source_url") else None
    if hit.get("source_url") and page is None:
        # A public source was named but Shopwatch could not read it (blocked, down): the
        # value is shown with that caveat, never as if Shopwatch had seen it.
        host = urlsplit(hit["source_url"]).hostname or ""
        own = host.removeprefix("www.").endswith(prof.get("domain") or "\0")
        src = pr.source("company_site" if own else "public_source", hit["value"],
                        url=hit["source_url"], retrieved_at=None,
                        note="cited by AI-assisted research; not retrieved by Shopwatch")
        src.update(discovery_method="ai_assisted", retrieved=False, provider=hit.get("provider"),
                   ai_discovered_at=hit.get("discovered_at"),
                   excerpt=(hit.get("excerpt") or "")[:300])
        f.update(value=disp, status="ai_assisted_unretrieved", coverage="found_by_ai_fallback",
                 sources=[src], values=[{"value": disp, "sources": [src]}], reason=None, note=None)
        run["fields"][field] = "found_unretrieved"
        return
    if page is None or not _matches(field, hit["value"], page):
        # No source, or the cited page says otherwise: the AI's statement alone is not
        # evidence. Kept for audit with its source; the field shows no value.
        f.update(status="not_independently_verified", coverage="not_found_by_deterministic_tooling",
                 ai_claim={"value": hit["value"], "source_url": hit.get("source_url"),
                           "provider": hit.get("provider"),
                           "note": "suggested by AI-assisted research; " +
                                   ("no source was cited" if not hit.get("source_url") else
                                    "the cited page was read and does not contain the value")})
        run["fields"][field] = "unverified"
        return
    first_party = page["host"].removeprefix("www.").endswith(prof.get("domain") or "\0")
    kind = "company_archive" if page.get("archived") else \
        ("company_document" if page.get("legal_doc") else "company_site") if first_party else \
        "public_source"
    src = pr.source(kind, hit["value"], url=page["url"], retrieved_at=page.get("retrieved_at"),
                    archived=page.get("archived"))
    src.update(discovery_method="ai_assisted", retrieved=True, provider=hit.get("provider"),
               ai_discovered_at=hit.get("discovered_at"), excerpt=(hit.get("excerpt") or "")[:300])
    f.update(value=disp, status="ai_assisted", coverage="found_by_ai_fallback",
             sources=[src], values=[{"value": disp, "sources": [src]}], reason=None, note=None)
    run["fields"][field] = "found"
