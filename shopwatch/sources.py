"""External evidence providers for the domain check, each behind a small interface.

Every provider reports its AVAILABILITY separately from what it FOUND:

    availability: available | unconfigured | temporarily_unavailable | not_applicable
                  | blocked | failed
    outcome:      evidence_found | no_evidence_found | None (not run)

"Search is not configured" and "search found nothing" are different facts, and the
page must never let one read as the other. No provider is a hard dependency: the
pipeline runs with whatever subset is available and says which ones were missing.

Providers here talk only to FIXED third-party hosts (search API, crt.sh, the Internet
Archive, DNS-over-HTTPS). The only user-controlled fetches go through sealbox's guarded
fetcher in domaincheck.py.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Protocol

from shopwatch.store import Store, now_iso

USER_AGENT = "shopwatch-domaincheck/1.0 (merchant identity verification; honours robots.txt)"
CONFIG = Path.home() / ".config" / "shopwatch"

AVAILABLE, UNCONFIGURED, TEMP_UNAVAILABLE = "available", "unconfigured", "temporarily_unavailable"
NOT_APPLICABLE, BLOCKED, FAILED = "not_applicable", "blocked", "failed"
FOUND, NOT_FOUND = "evidence_found", "no_evidence_found"


def get_json(url: str, headers: dict | None = None, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4 * 1024 * 1024).decode("utf-8"))


def state(status: str, outcome: str | None = None, **detail) -> dict:
    return {"status": status, "outcome": outcome, **detail}


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

class SearchProvider(Protocol):
    name: str

    def configured(self) -> bool: ...
    def search(self, query: str) -> list[dict]: ...


class BraveSearch:
    """Brave Search API (https://api.search.brave.com/res/v1/web/search). Key required."""
    name = "brave"
    URL = "https://api.search.brave.com/res/v1/web/search"
    TOKEN_FILE = CONFIG / "brave_token"

    def __init__(self, token: str | None = None, getter=get_json):
        self.token = token or os.environ.get("SHOPWATCH_BRAVE_TOKEN") or self._file()
        self.getter = getter

    def _file(self) -> str | None:
        try:
            return self.TOKEN_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def configured(self) -> bool:
        return bool(self.token)

    def search(self, query: str) -> list[dict]:
        data = self.getter(self.URL + "?" + urllib.parse.urlencode({"q": query, "count": 10}),
                           {"X-Subscription-Token": self.token})
        return [{"query": query, "result_url": (i.get("url") or "")[:500],
                 "result_title": (i.get("title") or "")[:200],
                 "result_snippet": re.sub(r"<[^>]+>", "", i.get("description") or "")[:500],
                 "retrieved_at": now_iso(), "provider": self.name}
                for i in ((data.get("web") or {}).get("results") or [])[:10]]


def search_providers(getter=get_json) -> list[SearchProvider]:
    """Configured providers in preference order. The project has no older search
    integration, so Brave is first; add further providers here, not in the pipeline."""
    return [p for p in (BraveSearch(getter=getter),) if p.configured()]


def search_queries(domain: str, hostname: str, names: list[str]) -> list[str]:
    qs = [f'"{domain}" ЕИК', f'site:{domain} "ЕИК"', f'"{domain}" "Общи условия"',
          f'site:{domain} "Общи условия"', f'"{domain}" EIK']
    if hostname != domain:
        qs.append(f'"{hostname}" ЕИК')
    qs += [f'"{domain}" "{n}"' for n in names[:2]]
    return qs


def run_search(providers: list[SearchProvider], queries: list[str], pause: float = 1.1) -> dict:
    if not providers:
        return state(UNCONFIGURED, None, provider=None, results=[],
                     note="no search provider configured")
    p = providers[0]
    results, errors = [], []
    for q in queries:
        try:
            results += p.search(q)
        except urllib.error.HTTPError as e:
            errors.append(f"HTTP {e.code}")
            if e.code in (401, 403):
                return state(FAILED, None, provider=p.name, results=results,
                             note="search key rejected")
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append(str(e)[:120])
        time.sleep(pause)                              # free tiers allow ~1 request/s
    if errors and not results:
        return state(TEMP_UNAVAILABLE, None, provider=p.name, results=[], errors=errors[:3])
    return state(AVAILABLE, FOUND if results else NOT_FOUND, provider=p.name,
                 results=results, queries=queries)


# --------------------------------------------------------------------------
# related hosts: Certificate Transparency + a short list of conventional names
# --------------------------------------------------------------------------

# Where companies conventionally put legal text and contact details. Checked only by
# DNS (does the name exist?), never by probing ports or paths.
LEGAL_SUBDOMAINS = ("legal", "help", "support", "about", "company", "corporate",
                    "developer", "developers", "business", "info")
_HOST_HINTS = ("developer", "help", "support", "legal", "about", "corporate", "company",
               "business", "info", "terms", "policies", "privacy", "pomosht", "za-nas")


def ct_hosts(domain: str, getter=get_json, timeout: int = 15) -> dict:
    """Host names seen in Certificate Transparency for *.domain (via crt.sh)."""
    url = "https://crt.sh/?" + urllib.parse.urlencode(
        {"q": f"%.{domain}", "output": "json", "exclude": "expired"})
    try:
        rows = getter(url, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return state(TEMP_UNAVAILABLE, None, hosts=[], error=str(e)[:160])
    names = set()
    for r in rows if isinstance(rows, list) else []:
        for n in str(r.get("name_value", "")).splitlines():
            n = n.strip().lower().lstrip("*.")
            if n.endswith("." + domain) and re.fullmatch(r"[a-z0-9.-]+", n):
                names.add(n)
    # crt.sh answers an abandoned query with HTTP 200 and an empty list, so "nothing"
    # is reported as no evidence, not as proof that no subdomains exist.
    return state(AVAILABLE, FOUND if names else NOT_FOUND, hosts=sorted(names)[:300])


def rank_related(domain: str, hostname: str, seen: list[str], exists,
                 skip: set[str] = frozenset()) -> list[str]:
    """Hosts most likely to carry the operator's legal text, best first, that exist.

    `seen` are names with evidence behind them (linked from the site, or in CT logs);
    the conventional names are guesses and must pass `exists`, which on a wildcard-DNS
    domain means "resolves somewhere other than a random name does".
    """
    def score(h):
        label = h[:-(len(domain) + 1)]
        first = label.split(".")[-1]
        conv = LEGAL_SUBDOMAINS.index(first) if first in LEGAL_SUBDOMAINS else 99
        return (0 if any(k in label for k in _HOST_HINTS) else 1, conv, label.count("."),
                label)
    guesses = [f"{s}.{domain}" for s in LEGAL_SUBDOMAINS]
    pool = [h for h in dict.fromkeys(seen + guesses)
            if h.endswith("." + domain) and h not in (hostname, "www." + domain)
            and h not in skip]
    ranked = [h for h in sorted(pool, key=score) if score(h)[0] == 0][:12]
    return [h for h in ranked if exists(h)]


# --------------------------------------------------------------------------
# Internet Archive: an independent public copy when the site itself refuses us
# --------------------------------------------------------------------------

class WaybackArchive:
    """Archived copies from the Internet Archive's public availability API.

    Used only for pages the site refused to serve us (403, challenge, robots). It is an
    independent public archive, not a way into the site: nothing is requested from the
    target, and the snapshot date is carried with every claim made from it.
    """
    API = "https://archive.org/wayback/available?"
    MAX_BYTES = 3 * 1024 * 1024
    PAUSE = 1.0                  # seconds between lookups; the archive answers bursts with 429

    def __init__(self, getter=get_json, opener=None):
        self.getter = getter
        self.opener = opener or (lambda u, t: urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": USER_AGENT}), timeout=t))

    def snapshot(self, url: str, timeout: int = 10) -> dict | None:
        data = self.getter(self.API + urllib.parse.urlencode({"url": url}), timeout=timeout)
        snap = ((data or {}).get("archived_snapshots") or {}).get("closest") or {}
        if not snap.get("available") or str(snap.get("status")) != "200":
            return None
        ts = str(snap.get("timestamp", ""))
        if not re.fullmatch(r"\d{14}", ts):
            return None
        # id_ = the archived bytes as captured, without the archive's toolbar rewriting
        return {"timestamp": ts, "url": url,
                "raw_url": f"https://web.archive.org/web/{ts}id_/{url}"}

    def fetch(self, snap: dict, timeout: int = 15) -> tuple[bytes, str]:
        with self.opener(snap["raw_url"], timeout) as r:
            return r.read(self.MAX_BYTES), r.headers.get("content-type", "")


# --------------------------------------------------------------------------
# NAP public list of e-shops (Наредба Н-18, чл. 52м): who declared this web shop
# --------------------------------------------------------------------------

NAP_LIST = "https://portal.nra.bg/pls/pub/!dec2009.REGREPESHOP.ajaxListDocuments"
_NAP_COLS = ("", "SHOPNAME", "DOMAIN", "PLATFORM_DOMAIN", "GOODS", "OWNER_INFO", "REGDATE",
             "ACTIVE_TO", "TERMDATE", "MEDIATORS")
NAP_PAGE = "https://portal.nra.bg/details/online-store-nrareg"


def nap_eshops(domain: str, getter=get_json, registrable=None, pause: float = 1.5) -> dict:
    """Operators that declared a web shop at this domain to NAP, from its public list.

    The list is public by law and is the one official source that links a DOMAIN to an
    operator's ЕИК. Kept: the ЕИК, the operator's declared name (as NAP publishes it,
    often without the legal form: "ЧУНЧЕ"), the shop entry and its dates. The declared
    name is the trader's identity even for a sole trader, so it is kept with its source
    like any other public trader field. Marketplace sellers (a platform address, no own
    domain) are ignored.
    """
    # The list's web-address filter lower-cases the query but compares case-sensitively,
    # so a shop declared as "CHUNCHE-BG.COM" or "Climavis.bg" never matches its own
    # domain. The shop-name filter is case-insensitive: fall back to the domain label.
    # The portal now and then answers a query with an empty list under load (seen for
    # bigvip.bg and dobytoys.com, both declared). "Not declared" is concluded only when a
    # second pass agrees - it is cached for a day.
    entries, err = [], None
    attempts = [("shopwebaddr", domain), ("shopname", domain.rsplit(".", 1)[0])]
    for n, (field, value) in enumerate(attempts * 2):
        if n == len(attempts) and pause:
            time.sleep(pause)
        q = {"draw": "1", "start": "0", "length": "25", "search[value]": "",
             "search[regex]": "false", "goodsident": "", "goodstype": "", "ownerident": "",
             "ownername": "", "platformwebaddr": "", "shopname": "", "shopwebaddr": "",
             "status": "1", field: value}
        for i, c in enumerate(_NAP_COLS):
            q[f"columns[{i}][data]"] = c
        try:
            data = getter(NAP_LIST + "?" + urllib.parse.urlencode(q), timeout=12)
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = str(e)[:160]
            continue
        for row in (data or {}).get("data") or []:
            dom = re.sub(r"^https?:?//", "", str(row.get("DOMAIN") or "").strip().lower())
            dom = dom.split("/")[0].removeprefix("www.")
            if not dom or row.get("PLATFORM_DOMAIN"):
                continue
            if (registrable(dom) if registrable else dom) != domain:
                continue                           # both filters match substrings
            owner = html.unescape(str(row.get("OWNER_INFO") or ""))
            m = re.search(r"ЕИК:\s*(\d{9}|\d{13})", owner)
            nm = re.search(r"Наименование:\s*([^<\n]{2,160}?)\s*(?:<br\s*/?>|$)", owner)
            if m:
                entries.append({"eik": m.group(1), "shop_name": (row.get("SHOPNAME") or "")[:120],
                                "declared_name": nm.group(1).strip() if nm else None,
                                "declared_domain": row.get("DOMAIN"),
                                "declared_at": row.get("REGDATE"), "retrieved_at": now_iso()})
        if entries:
            break
    if not entries and err:
        return state(TEMP_UNAVAILABLE, None, entries=[], error=err)
    return state(AVAILABLE, FOUND if entries else NOT_FOUND, entries=entries[:5], url=NAP_PAGE)


# --------------------------------------------------------------------------
# Bulgarian Commercial Register (Registry Agency)
# --------------------------------------------------------------------------

class CompanyRegistryProvider(Protocol):
    def status(self) -> str: ...
    def lookup_by_eik(self, eik: str) -> dict | None: ...
    def search_by_company_name(self, name: str) -> list[dict]: ...


NS = {"env": "http://www.registryagency.bg/schemas/envelopev2",
      "d": "http://www.registryagency.bg/schemas/deedv2",
      "f": "http://www.registryagency.bg/schemas/deedv2/Fields"}
LEGAL_FORM_BG = {"ET": "ЕТ", "SD": "СД", "KD": "КД", "OOD": "ООД", "EOOD": "ЕООД",
                 "AD": "АД", "EAD": "ЕАД", "KDA": "КДА", "K": "Кооперация"}
# DeedStatusType in DeedV2.xsd: N new, E re-registered from BULSTAT, C / L closed.
DEED_STATUS = {"N": "registered", "E": "registered", "C": "closed", "L": "closed"}


def parse_deeds(xml: bytes) -> list[dict]:
    """Current state of every Deed in a Registry Agency message (Envelopev2/DeedV2).

    Field-level allowlist by construction: only the fields named here are read. A
    person's `Indent` (their ЕГН/ЛНЧ) and `IndentType` are never touched; only
    Name and Position are taken. Fields whose FieldOperation is "Erase" are skipped.
    """
    root = ET.fromstring(xml)
    deeds = root.iter(f"{{{NS['d']}}}Deed")
    out = []
    for deed in deeds:
        rec = {"eik": deed.get("UIC"), "name": deed.get("CompanyName"),
               "legal_form": LEGAL_FORM_BG.get(deed.get("LegalForm", ""), deed.get("LegalForm")),
               "status": DEED_STATUS.get(deed.get("DeedStatus", ""), "unknown"),
               "registered_address": None, "capital": None, "registration_date": None,
               "persons": [], "website": None, "corporate_phone": None,
               "corporate_email": None}
        live = [el for el in deed.iter() if el.get("FieldOperation") != "Erase"]
        for el in live:
            tag = el.tag.split("}")[-1]
            if tag == "Seat":
                a = el.find("f:Address", NS)
                if a is not None:
                    parts = [a.findtext(f"f:{k}", "", NS) for k in
                             ("Settlement", "PostCode", "HousingEstate", "Street", "StreetNumber",
                              "Block", "Entrance", "Floor", "Apartment")]
                    rec["registered_address"] = ", ".join(p for p in parts if p) or None
                c = el.find("f:Contacts", NS)
                if c is not None:
                    rec["website"] = (c.findtext("f:URL", "", NS) or "").strip() or None
                    # The trader's registered contacts: company data, allowlisted as such.
                    rec["corporate_phone"] = (c.findtext("f:Phone", "", NS) or "").strip() or None
                    rec["corporate_email"] = (c.findtext("f:EMail", "", NS) or "").strip() or None
            elif tag == "Funds" and (el.text or "").strip():
                rec["capital"] = f"{el.text.strip()} {el.get('currency') or ''}".strip()
            elif tag == "EntryDate" and not rec["registration_date"]:
                rec["registration_date"] = (el.text or "").strip() or None
            elif tag in ("Manager", "Representative", "Representative101",
                         "Representative102", "Representative103", "Procurator"):
                p = el.find("f:Person", NS)
                name = p.findtext("f:Name", "", NS).strip() if p is not None else ""
                if name:
                    rec["persons"].append({"name": name,
                                           "role": (p.get("Position") or tag).strip()})
        out.append(rec)
    return out


class RegistryAgencyProvider:
    """Commercial Register adapter over the agency's official XML (Integration package).

    The Integration package (schemas.zip, 06.10.2025) defines the MESSAGE FORMAT —
    Envelopev2 / DeedV2 / Fields / Notifications XSDs — and no transport, endpoint or
    authentication. Those come with the paid contract (or with access for official
    purposes). So this adapter does not guess an endpoint: it ingests the official
    messages from an inbox directory that the contracted delivery channel writes to,
    keeps a compact current-state cache in the store, and answers lookups from it.

        ~/.config/shopwatch/registry_inbox/*.xml   <- delivered Envelopev2 messages
    """
    INBOX = CONFIG / "registry_inbox"

    def __init__(self, store: Store | None, inbox: Path | None = None):
        self.store = store
        self.inbox = inbox or self.INBOX

    def status(self) -> str:
        if not self.inbox.is_dir():
            return UNCONFIGURED
        return AVAILABLE if self.store else UNCONFIGURED

    def ingest(self) -> int:
        """Load delivered messages into the cache; processed files are removed."""
        if self.status() != AVAILABLE:
            return 0
        n = 0
        for f in sorted(self.inbox.glob("*.xml"))[:500]:
            try:
                for rec in parse_deeds(f.read_bytes()):
                    if rec["eik"]:
                        self.store.put_registry_record(rec)
                        n += 1
                f.unlink()
            except (ET.ParseError, OSError) as e:
                print(f"registry inbox: {f.name}: {e}", flush=True)
        return n

    def lookup_by_eik(self, eik: str) -> dict | None:
        if self.status() != AVAILABLE:
            return None
        return self.store.registry_record(eik)

    def search_by_company_name(self, name: str) -> list[dict]:
        if self.status() != AVAILABLE:
            return []
        return self.store.registry_by_name(name)
