"""Domain -> legal entity -> official company facts, from lawful public sources only.

The question this answers is not "can the site be scraped" but "which legal entity
operates this domain, and what do official sources currently say about it". The target
site is ONE evidence source among several and no source is a hard dependency. When the
site refuses automated access (401/403/429, WAF or bot challenge, robots.txt, timeout)
that source is marked blocked and the check carries on. Nothing here tries to get past
a block: no browser, no stealth, no proxy rotation, no CAPTCHA handling, an honest
User-Agent, robots.txt respected on every host.

Stages (each degrades to a status, never to an exception):

    A  in parallel: target site | DNS | RDAP | TLS | CT logs | search (if configured)
    B  discovery, only while no strong candidate exists:
         legal pages on the domain's related hosts (help., developer., ... and names
         seen in Certificate Transparency), PDFs included; known legal paths on the
         main host when it is reachable; legal URLs surfaced by search; and, for a host
         that refused us, the Internet Archive's copy of its pages
    C  official corroboration: Registry Agency (when contracted) and EU VIES
    D  resolution by source authority with explained confidence, then the privacy
       allowlist, the store, the response

    python -m shopwatch.domaincheck check example.bg [--refresh]
    python -m shopwatch.domaincheck refresh              # re-verify + registry inbox
    python -m shopwatch.domaincheck admin ...            # data-subject requests
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.robotparser
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from shopwatch import aifallback, privacy, profile, sources
from shopwatch.disclosure import _BlockSplitter, extract_merchant
from shopwatch.eik import validate_eik
from shopwatch.investigate import (LINK_TIERS, _Links, _sealbox_fetch, decode_body, investigate,
                                   iri_to_uri, strip_heavy)
from shopwatch.pdftext import PdfError, extract_text
from shopwatch.registry import VIES, check_vat, name_matches
from shopwatch.store import Store, now_iso

USER_AGENT = sources.USER_AGENT
UA_TOKEN = "shopwatch"
MAX_URL_LEN = 2048
DIRECT_BUDGET = 30          # stage A: the target site crawl, wall-clock
DISCOVERY_BUDGET = 40       # stage B: related hosts, legal pages, archive, wall-clock
DIRECT_PAGES = 5            # entry page + up to 4 legal/contact pages
DISCOVERY_FETCHES = 14      # stage B page fetches across all hosts
PAGE_TIMEOUT = 12
LINGER_CAP = 4              # abandoned slow crawls still running; above this, skip
TTL = {"domain": 600, "dns": 3600, "rdap": 86400, "search": 86400, "ct": 86400, "nap": 86400,
       "vies": 86400, "bootstrap": 7 * 86400}
REVERIFY_AFTER = 7 * 86400

DOH = "https://cloudflare-dns.com/dns-query"          # RFC 8484 JSON API
RRTYPE = {"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28}
RDAP_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"  # RFC 9224

# Evidence categories, strongest first. A lower category never overrides a higher one,
# however many agree: majority vote is how a copied typo becomes a "fact".
AUTHORITY = ("registry", "official", "company_source", "public_source", "search_evidence")
LEVELS = ("insufficient", "low", "medium", "high")
VAT_ACTIVE = "vat_registered"

# Response bodies that are a bot challenge rather than the site. Deliberately narrow:
# Cloudflare injects /cdn-cgi/challenge-platform/ scripts into ordinary pages too.
CHALLENGE_MARKERS = ("cf_chl_opt", "<title>just a moment...</title>",
                     "attention required! | cloudflare", "_incapsula_resource",
                     "px-captcha", "ddos-guard.net/", "checking your browser before")

# Where operators put their legal identity, best first. Extends the disclosure crawl's
# tiers with the words legal documents use for themselves.
LEGAL_TIERS = (
    ("общи условия", "obshti-usloviya", "usloviya", "uslovia", "terms", "conditions",
     "regulation", "regulamin", "правила", "pravila", "legal", "imprint", "impressum",
     "реквизит", "фирмена"),
) + LINK_TIERS
KNOWN_LEGAL_PATHS = ("/terms", "/terms-and-conditions", "/obshti-usloviya", "/contact",
                     "/kontakti", "/about", "/imprint", "/company", "/privacy")


def _get_json(url: str, headers: dict | None = None, timeout: int = 10):
    return sources.get_json(url, headers, timeout)


CACHE_VERSION = "7"         # bump when a cached shape changes; old rows are then ignored


def cache_key(key: str) -> str:
    return f"v{CACHE_VERSION}:{key}"


def _cached(store: Store | None, kind: str, key: str, fn, refresh: bool = False):
    key = cache_key(key)
    if store and not refresh:
        hit = store.cache_get(kind, key, TTL[kind])
        if hit is not None:
            return hit
    val = fn()
    if store and val.get("status") not in ("unavailable", "error", sources.TEMP_UNAVAILABLE,
                                           sources.FAILED):
        store.cache_put(kind, key, val)
    return val



# --------------------------------------------------------------------------
# 1. URL normalisation
# --------------------------------------------------------------------------

class InputError(ValueError):
    """Bad user input. The message is safe to return to the caller."""


@lru_cache(maxsize=1)
def _psl() -> tuple[frozenset, frozenset, frozenset]:
    """Public Suffix List as (rules, wildcards, exceptions), punycoded.

    Vendored copy (2023-10) because the Deck has no pip. ponytail: refresh the file
    from publicsuffix.org when a new TLD matters; the parser does not change.
    """
    rules, wild, exc = set(), set(), set()
    path = Path(__file__).with_name("public_suffix_list.dat")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        rule = line.split()[0]
        try:
            rule = ".".join(lbl if lbl in ("*",) or lbl.startswith("!") else
                            lbl.encode("idna").decode("ascii")
                            for lbl in rule.split("."))
        except UnicodeError:
            continue
        if rule.startswith("!"):
            exc.add(rule[1:])
        elif rule.startswith("*."):
            wild.add(rule[2:])
        else:
            rules.add(rule)
    # Second levels Register.BG operated that the PSL does not list. Without them
    # a.com.bg and b.com.bg would collapse into one "site" (same list as investigate).
    rules.update(("com.bg", "org.bg", "net.bg", "edu.bg", "gov.bg"))
    return frozenset(rules), frozenset(wild), frozenset(exc)


def registrable_domain(host: str) -> str | None:
    """eTLD+1 per the Public Suffix List; None if the host IS a public suffix."""
    rules, wild, exc = _psl()
    labels = host.lower().strip(".").split(".")
    suffix_len = 1                                   # default rule "*"
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if cand in exc:
            suffix_len = len(labels) - i - 1
            break
        parent = ".".join(labels[i + 1:])
        if cand in rules or (parent and parent in wild):
            suffix_len = len(labels) - i
            break
    if len(labels) <= suffix_len:
        return None
    return ".".join(labels[-(suffix_len + 1):])


def normalise(raw: str) -> dict:
    """Validate and normalise user input. Raises InputError with a safe message."""
    if not isinstance(raw, str) or not raw.strip():
        raise InputError("empty URL")
    raw = raw.strip()
    if len(raw) > MAX_URL_LEN:
        raise InputError("URL too long")
    if any(c.isspace() or ord(c) < 32 for c in raw):
        raise InputError("URL contains whitespace or control characters")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlsplit(raw)
    if p.scheme.lower() not in ("http", "https"):
        raise InputError("only http and https URLs are supported")
    if p.username or p.password or "@" in p.netloc:
        raise InputError("URLs with credentials are not accepted")
    try:
        port = p.port
    except ValueError:
        raise InputError("invalid port") from None
    if port not in (None, 80, 443):
        raise InputError("only the standard web ports 80 and 443 are supported")
    host = (p.hostname or "").rstrip(".")
    if not host:
        raise InputError("no host name in URL")
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise InputError("enter a domain name, not an IP address")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise InputError("invalid internationalised domain name") from None
    if not re.fullmatch(r"[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})*\.([a-z]{2,63}|xn--[a-z0-9-]+)",
                        host):
        raise InputError("invalid host name")
    reg = registrable_domain(host)
    if not reg:
        raise InputError("that is a public suffix, not a registrable domain")
    scheme = p.scheme.lower()
    path = p.path or "/"
    netloc = host + (f":{port}" if port and port != (443 if scheme == "https" else 80) else "")
    return {"scheme": scheme, "registrable_domain": reg, "hostname": host, "port": port,
            "path": path,
            "canonical_url": urllib.parse.urlunsplit((scheme, netloc, path, p.query, ""))}



_LDJSON = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                     re.IGNORECASE | re.DOTALL)
_ORG_TYPES = {"organization", "corporation", "localbusiness", "store", "onlinestore",
              "onlinebusiness", "organisation"}


def jsonld_orgs(html: str) -> list[dict]:
    """Organization records from JSON-LD, restricted to identity fields."""
    out = []
    for block in _LDJSON.findall(html)[:10]:
        try:
            data = json.loads(block.strip()[:200000])
        except ValueError:
            continue
        stack = [data]
        while stack and len(out) < 5:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node[:50])
            elif isinstance(node, dict):
                types = node.get("@type")
                types = {str(t).lower() for t in (types if isinstance(types, list) else [types])}
                if types & _ORG_TYPES:
                    rec = {k: node.get(k) for k in ("legalName", "name", "taxID", "vatID",
                                                    "identifier", "url", "sameAs")
                           if isinstance(node.get(k), (str, list))}
                    if rec:
                        out.append(rec)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out



def _is_challenge(r: dict) -> bool:
    if (r.get("headers") or {}).get("cf-mitigated", "").lower() == "challenge":
        return True
    head = r.get("body", b"")[:60000].decode("utf-8", "ignore").casefold()
    return any(m in head for m in CHALLENGE_MARKERS)



# --------------------------------------------------------------------------
# polite, guarded fetching shared by every stage that touches the target's hosts
# --------------------------------------------------------------------------

_lingering = 0
_linger_lock = threading.Lock()


def _bounded(fn, budget: float) -> tuple[bool, object]:
    """Run fn in a worker with a wall-clock budget. (finished, result-or-None).

    Python threads cannot be killed, so a crawl that overruns is abandoned and counted;
    once LINGER_CAP of them are still running, new crawls are refused rather than
    letting a slow-drip server tie up every thread.
    """
    global _lingering
    with _linger_lock:
        if _lingering >= LINGER_CAP:
            return False, None
    box: dict = {}

    def run():
        global _lingering
        try:
            box["result"] = fn()
        except Exception as e:                         # a crawler bug is one source down
            box["error"] = f"{type(e).__name__}: {e}"
        finally:
            with _linger_lock:
                box["done"] = True
                if box.get("abandoned"):
                    _lingering -= 1

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(budget)
    with _linger_lock:
        if not box.get("done"):
            box["abandoned"] = True
            _lingering += 1
            return False, None
    return True, box.get("result", box.get("error"))


def _text_of_html(html: str) -> str:
    p = _BlockSplitter()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return "\n".join(c for c in p.chunks if c)


def _is_pdf(r: dict, url: str) -> bool:
    ct = ((r.get("headers") or {}).get("content-type") or "").lower()
    return "application/pdf" in ct or r.get("body", b"")[:5] == b"%PDF-"


class Crawler:
    """Guarded fetcher + politeness + evidence capture, shared across stages.

    robots.txt is honoured per host, a bot challenge becomes a refusal (never parsed as
    the operator's own text), and every page read is kept as evidence METADATA - URL,
    content hash, what it said - never as raw content.
    """

    def __init__(self, domain: str, fetch=None):
        self.domain = domain
        self.base_fetch = fetch or _sealbox_fetch()
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.pages: list[dict] = []
        self.refused_hosts: set[str] = set()
        self.flags = {"robots": False, "challenge": False}
        self.visited: set[str] = set()
        self.lock = threading.Lock()
        self.observer = None          # called when a page yields an identifier (progress)

    def allowed(self, u: str) -> bool:
        p = urlsplit(u)
        key = f"{p.scheme}://{p.netloc}"
        if key not in self.robots:
            rp = None
            try:
                r = self.base_fetch(key + "/robots.txt", timeout=PAGE_TIMEOUT,
                                    max_bytes=512 * 1024, user_agent=USER_AGENT)
                if r.get("status") == 200 and not _is_challenge(r):
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(decode_body(r["body"], None).splitlines())
                elif r.get("status") in (401, 403):
                    # RFC 9309 treats an unreachable-by-policy robots.txt as full disallow.
                    rp = urllib.robotparser.RobotFileParser()
                    rp.disallow_all = True
            except Exception:
                rp = None                              # no robots.txt: nothing disallowed
            self.robots[key] = rp
        rp = self.robots[key]
        return rp is None or rp.can_fetch(UA_TOKEN, u)

    def get(self, u, **kw):
        """Fetcher with the guarded-fetch signature, for investigate() and the crawl."""
        host = (urlsplit(u).hostname or "").lower()
        if not self.allowed(u):
            self.flags["robots"] = True
            self.refused_hosts.add(host)
            raise PermissionError("robots.txt disallows this path")
        kw["user_agent"] = USER_AGENT
        kw.setdefault("max_bytes", 8 * 1024 * 1024)
        u = iri_to_uri(u)
        r = self.base_fetch(u, **kw)
        self.visited.add(u)
        self.visited.add(r.get("final_url", u))
        if _is_challenge(r):
            self.flags["challenge"] = True
            self.refused_hosts.add(host)
            return {**r, "status": 403}                # a refusal, not the operator's page
        if r.get("status") in (401, 403, 429):
            self.refused_hosts.add(host)
        elif 200 <= (r.get("status") or 0) < 300:
            self.record(r.get("final_url", u), r["body"], r.get("headers") or {})
        return r

    def record(self, url: str, body: bytes, headers: dict, archived: str | None = None,
               via: str | None = None) -> dict | None:
        """Extract identity evidence from one document and keep its metadata."""
        pdf = "application/pdf" in (headers.get("content-type") or "").lower() \
            or body[:5] == b"%PDF-"
        html, text = "", ""
        if pdf:
            try:
                text = extract_text(body)
            except (PdfError, ValueError, IndexError):
                text = ""
            html = "".join(f"<p>{line}</p>" for line in text.splitlines())
        else:
            html = strip_heavy(decode_body(body, headers.get("content-type")))
            text = _text_of_html(html)
        try:
            found = extract_merchant(html) if html else {}
        except (TypeError, ValueError):
            found = {}
        page = {"url": url, "host": (urlsplit(url).hostname or "").lower(),
                "kind": "pdf" if pdf else "html",
                "sha256": hashlib.sha256(body).hexdigest(), "retrieved_at": now_iso(),
                "archived": archived, "via": via,
                "legal_doc": _legalish(url, text[:3000]),
                "found": {k: found.get(k) for k in ("eik", "vat_number", "company_name",
                                                    "address", "email", "phone")
                          if found.get(k)},
                "jsonld": [] if pdf else jsonld_orgs(html),
                "mentions": company_mentions(text, self.domain),
                "people": representatives(text),
                "contacts": contacts(html, text, found, self.domain, pdf),
                "links": [] if pdf else self.links(html, url)}
        with self.lock:
            self.pages.append(page)
        if self.observer and (page["found"].get("eik") or any(m["eik"] for m in page["mentions"])):
            try:
                self.observer()
            except Exception:
                pass                                   # progress must never break a crawl
        return page

    def links(self, html: str, base: str) -> list[tuple[int, str]]:
        """Same-domain links ranked by how likely they lead to legal identity text."""
        p = _Links()
        try:
            p.feed(html[:2_000_000])
            p.close()
        except Exception:
            pass
        out, seen = [], set()
        for href, anchor in p.links:
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            full = urljoin(base, href).split("#")[0]
            parts = urlsplit(full)
            if parts.scheme not in ("http", "https") or full in seen:
                continue
            if registrable_domain(parts.hostname or "") != self.domain:
                continue                               # never attribute a third party
            if _PRIVATE_AREA.search(full):
                continue                               # never walk into login or accounts
            hay = (href + " " + anchor).casefold()
            for tier, hints in enumerate(LEGAL_TIERS):
                if any(h in hay for h in hints):
                    seen.add(full)
                    out.append((tier, full))
                    break
        return out[:30]


# Sign-in, account and checkout URLs: nothing public to read there, and a crawler has
# no business touching authentication endpoints at all.
_PRIVATE_AREA = re.compile(r"(?i)(^https?://(login|auth|account|accounts|sso|id)\.|/(login|"
                           r"signin|sign-in|logout|register|account|myaccount|auth|oauth|"
                           r"cart|checkout)\b|[?&](client_id|redirect_uri|code_challenge)=)")


_PHONE_LABELLED = re.compile(
    r"(?i)(?:тел(?:ефон)?|факс|gsm|моб(?:илен)?|phone|tel|call|горещ[аи]? линия)\.?\s*[:\-–]?\s*"
    r"((?:\+|00)?(?:359)?[\s\-/()]*0?\d[\d\s\-/()]{5,16}\d)")
_HREF = re.compile(r"""(?i)href\s*=\s*["']\s*(tel|callto|mailto):([^"'>]{3,120})""")
_EMAIL_ANY = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,10}")


def contacts(html: str, text: str, found: dict, domain: str, pdf: bool) -> dict:
    """Phones and e-mails a first-party page publishes, raw as written.

    Phones only when labelled or given as tel: links - a bare digit run is as likely an
    order number. Every mailbox the shop publishes is its contact, Gmail or not; only a
    public body or courier named in the legal boilerplate is not (privacy.classify_email).
    """
    phones, emails = [], []
    if found.get("phone"):
        phones.append(found["phone"])
    if found.get("email"):
        emails.append(found["email"])
    for kind, val in _HREF.findall(html or "") if not pdf else []:
        (emails if kind.lower() == "mailto" else phones).append(val.strip())
    for m in _PHONE_LABELLED.finditer((text or "")[:300_000]):
        phones.append(m.group(1).strip())
    emails += _EMAIL_ANY.findall((text or "")[:300_000])
    emails = [e for e in dict.fromkeys(e.strip().removeprefix("mailto:").split("?")[0]
                                       for e in emails)
              if privacy.classify_email(e.lower(), domain) in ("corporate", "published_contact")]
    return {"phones": list(dict.fromkeys(phones))[:10], "emails": emails[:10]}


def _legalish(url: str, head: str) -> bool:
    hay = (url + " " + head[:600]).casefold()
    return any(h in hay for h in LEGAL_TIERS[0] + ("контакт", "contact", "за нас"))


def collect_site(url: str, crawler: Crawler) -> dict:
    """Stage A: the disclosure crawl of the target site itself, within a budget."""
    def run():
        return investigate(url, max_pages=DIRECT_PAGES, timeout=PAGE_TIMEOUT,
                           fetcher=crawler.get, verifier=lambda _eik: {
                               "status": "skipped", "name": None, "address": None,
                               "source": "VIES"})
    done, entry = _bounded(run, DIRECT_BUDGET)
    if not done:
        return {"status": "timeout", "entry": None}
    if not isinstance(entry, dict):
        return {"status": "error", "entry": None, "reason": entry}
    if entry["reachable"] and entry.get("offsite_redirect"):
        status = "redirected_abroad"
    elif entry["reachable"]:
        status = "ok"
    elif crawler.flags["robots"]:
        status = "robots_disallowed"
    elif entry["refused"] or crawler.flags["challenge"]:
        status = "blocked"
    else:
        status = "unreachable"
    return {"status": status, "challenge": crawler.flags["challenge"], "entry": entry}


def legal_crawl(crawler: Crawler, seeds: list[str], max_fetches: int = DISCOVERY_FETCHES,
                stop_when=None) -> int:
    """Breadth-first over legal-looking pages of the domain's hosts, depth <= 2.

    Seeds are fetched first in the given order; links found on them are queued by
    legal tier. Hosts that refused us are not asked again.
    """
    queue: list[tuple[int, int, str]] = [(-1, 0, s) for s in seeds]
    fetched = 0
    while queue and fetched < max_fetches:
        queue.sort(key=lambda q: q[0])
        tier, depth, u = queue.pop(0)
        host = (urlsplit(u).hostname or "").lower()
        if u in crawler.visited or host in crawler.refused_hosts:
            continue
        fetched += 1
        try:
            r = crawler.get(u, timeout=PAGE_TIMEOUT)
        except Exception:
            continue
        if not 200 <= (r.get("status") or 0) < 300:
            continue
        page = next((p for p in reversed(crawler.pages)
                     if p["url"] == r.get("final_url", u)), None)
        if stop_when and stop_when():
            break
        if page and depth < 2:
            for t, link in page["links"]:
                if link not in crawler.visited:
                    queue.append((t, depth + 1, link))
    return fetched


def _named(crawler: Crawler) -> bool:
    """The operator's own documents - live or archived - have named an ЕИК: stop digging.
    (_strong() deliberately ignores archived copies; here they are the whole point.)"""
    return any(i["tier"] == "company_source"
               for items in candidates(crawler.pages, [], crawler.domain)["eiks"].values()
               for i in items)


def archive_fallback(crawler: Crawler, starts: list[str], archive,
                     max_docs: int = 6, max_lookups: int = 10) -> dict:
    """For hosts that refused us: read the Internet Archive's copies of their pages.

    Only the archive is contacted for a refusing host. Starting from each host's
    archived entry page (or its "/bg" language root when the root was never archived),
    legal links are followed two levels deep - live where that host does not refuse us,
    from the archive where it does. Every claim made from an archived copy carries the
    snapshot date.
    """
    docs = lookups = 0
    queue: list[tuple[int, int, str]] = []
    seen: set[str] = set()

    def twin(url: str) -> str | None:
        """The www / non-www counterpart of a MAIN-domain URL: sites redirect between the
        two and the archive often holds a page under only one. Subdomains have none."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host == crawler.domain:
            return parts._replace(netloc="www." + host).geturl()
        if host == "www." + crawler.domain:
            return parts._replace(netloc=crawler.domain).geturl()
        return None

    def archived(url: str) -> dict | None:
        nonlocal lookups, docs
        snap = None
        for candidate in filter(None, (url, twin(url))):
            if lookups >= max_lookups:
                return None
            time.sleep(archive.PAUSE)                  # the archive rate-limits bursts
            lookups += 1
            snap = archive.snapshot(candidate)
            if snap:
                break
        if not snap:
            return None
        body, ctype = archive.fetch(snap)
        docs += 1
        return crawler.record(url, body, {"content-type": ctype},
                              archived=snap["timestamp"], via="web.archive.org")

    try:
        # Every host's entry page first, then links strictly by legal tier across all of
        # them: a developer portal's terms (tier 0) must not wait behind the main site's
        # contact pages (tier 1).
        for start in starts:
            if docs >= max_docs or _named(crawler):
                break
            page = archived(start)
            if page is None and urlsplit(start).path in ("", "/"):
                page = archived(start.rstrip("/") + "/bg")
            if page:
                queue += [(t, 1, lk) for t, lk in page["links"] if t <= 1]
        while queue and docs < max_docs and lookups < max_lookups:
            if _named(crawler):
                break
            queue.sort(key=lambda q: q[0])
            tier, depth, link = queue.pop(0)
            if link in seen or link in crawler.visited:
                continue
            seen.add(link)
            host = (urlsplit(link).hostname or "").lower()
            page = None
            if host not in crawler.refused_hosts:    # the live page first, if allowed
                before = len(crawler.pages)
                try:
                    crawler.get(link, timeout=PAGE_TIMEOUT)
                except Exception:
                    pass
                page = crawler.pages[-1] if len(crawler.pages) > before else None
            if page is None and host in crawler.refused_hosts:
                page = archived(link)
            if page and depth < 2:
                queue += [(t, depth + 1, lk) for t, lk in page["links"] if t <= 1]
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        return sources.state(sources.TEMP_UNAVAILABLE, sources.FOUND if docs else None,
                             documents=docs, note="the archive is rate-limiting requests")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return sources.state(sources.TEMP_UNAVAILABLE, sources.FOUND if docs else None,
                             documents=docs, error=str(e)[:160])
    return sources.state(sources.AVAILABLE, sources.FOUND if docs else sources.NOT_FOUND,
                         documents=docs, lookups=lookups)


# --------------------------------------------------------------------------
# reading company identity out of text
# --------------------------------------------------------------------------

_FORM = r"(ЕООД|ООД|ЕАД|АД|ЕТ|КДА|КД|СД|EOOD|OOD|EAD)"
# A sole trader is written "ЕТ „Name“"; a trailing bare "ЕТ" after capitals is far more
# often the end of an upper-case product line ("... КАПАЦИТ ЕТ") than a company.
_FORM_TRAILING = r"(ЕООД|ООД|ЕАД|АД|КДА|КД|СД|EOOD|OOD|EAD)"
_QUOTED = re.compile(r"[„“\"«]\s*([^„“”\"«»\n]{2,80}?)\s*[”“\"»]\s*[-–,]?\s*" + _FORM
                     + r"(?![А-Яа-яA-Za-z])")
# Every word at least two characters and nothing glued on before it: otherwise "ЕООД"
# reads as a company called "Е" of legal form "ООД".
# The form glued on with no space only after a lower-case letter (PDF text
# "...БългарияЕООД"): after capitals it is the end of a word - "MOTORHEAD", "FLOOD".
_UNQUOTED = re.compile(r"(?<![\wА-Яа-я])((?:[А-ЯA-Z][\wА-Яа-я.&'-]+\s+){0,5}"
                       r"[А-ЯA-Z][\wА-Яа-я.&'-]+?)"
                       r"(?:\s*[-–,]\s*|\s+|(?<=[а-яa-z]))" + _FORM_TRAILING + r"(?![А-Яа-яA-Za-z])")
_EIK_LABEL = re.compile(r"(?:ЕИК|БУЛСТАТ|UIC|EIK|Identification\s+Code(?:\s*/\s*Number)?)(?:\s*/\s*БУЛСТАТ)?\s*(?:по\s+БУЛСТАТ)?"
                        r"[\s:№-]*(?:BG)?\s*(\d{9}(?:\d{4})?)\b", re.IGNORECASE)
_VAT = re.compile(r"\bBG\s?(\d{9})\b")
# "със седалище и адрес на управление: гр. София, ул. ..." up to the next clause.
_SEAT = re.compile(r"(?i)седалище\s+и\s+адрес\s+на\s+управление\s*(?:в\s+)?[:\-–]?\s*"
                   r"(.{8,160}?)(?=\s*[,;]?\s*(?:ЕИК|БУЛСТАТ|тел|e-?mail|ел\.\s*поща|"
                   r"представлява|вписан|регистрира|I\.\s)|\.\s+[А-ЯA-Z]|$)")
_OPERATOR_WORDS = ("дминистратор", "оператор", "собственик", "собственост", "стопанисва", "управлява",
                   "поддържа", "търговец", "доставчик", "operator", "owner",
                   "administrator", "operated by", "owned by", "provider")


_PNAME = r"(?:[А-Я][а-я]+|[А-Я]{2,}|[A-Z][a-z]+)"
_PERSON = _PNAME + r"(?:[ \t]+|-)" + _PNAME + r"(?:(?:[ \t]+|-)" + _PNAME + r")?"
_ROLE = {"управител": "manager", "управители": "manager", "изпълнителен директор": "executive director",
         "изпълнителния директор": "executive director", "собственик": "owner",
         "managing director": "director", "director": "director", "manager": "manager",
         "owner": "owner"}
_REPRESENTED = re.compile(
    r"(?:[Пп]редставлява(?:но|на|н)?\s+от|в\s+лицето\s+на|represented\s+by)\s+"
    r"(?:(?:its|своя)\s+)?(?:(?P<role>управителя|изпълнителния\s+директор|собственика|"
    r"[Mm]anaging\s+[Dd]irector|[Dd]irector|[Mm]anager)\s*,?\s*)?(?P<name>" + _PERSON + r")")
_ROLE_LABEL = re.compile(
    r"(?P<role>Управител(?:и)?|Изпълнителен\s+директор|Собственик|Managing\s+Director|"
    r"Director|Manager|Owner)\s*[:\-–]\s*(?P<name>" + _PERSON + r")")
_LABEL_TOKENS = {"тел", "телефон", "gsm", "моб", "ел", "имейл", "email", "e-mail", "адрес",
                 "еик", "булстат", "tel", "phone", "address", "ддс", "vat"}
_FORMS_UP = {"ЕООД", "ООД", "ЕАД", "АД", "ЕТ", "КД", "КДА", "СД", "LTD", "EOOD", "OOD"}
_NOT_A_PERSON = re.compile(r"(?i)^(?:общи|република|търговск|европейск|комисия|национал|"
                           r"закон|политика|дружество|фирма)")


def representatives(text: str) -> list[dict]:
    """People a company names as acting for it: "представлявано от управителя Иван
    Петров", "Управител: Мария Иванова", "represented by its manager John Smith".

    Public company information, kept with its page like any other published field. A
    name followed by a legal form is a company, not a person.
    """
    flat = re.sub(r"\s+", " ", text or "")[:400_000]
    out: dict[str, dict] = {}
    for rx in (_REPRESENTED, _ROLE_LABEL):
        for m in rx.finditer(flat):
            toks = re.split(r"[ \t]+", m.group("name").strip())
            cyr = lambda t: bool(re.match(r"[А-Яа-я]", t))
            while toks and (toks[-1].casefold() in _LABEL_TOKENS or cyr(toks[-1]) != cyr(toks[0])):
                toks.pop()                  # "Мария Иванова Тел: ...", "Илия Баев Web site:"
            name = " ".join(toks)
            after = flat[m.end("name"):m.end("name") + 8]
            if len(toks) < 2 or _NOT_A_PERSON.match(name) or \
                    any(t.upper() in _FORMS_UP for t in toks) or \
                    re.match(r"\s*[-–]?\s*(?:ЕООД|ООД|ЕАД|АД|ЕТ|Ltd|LTD)\b", after):
                continue                            # a company, not a person
            role = re.sub(r"\s+", " ", (m.group("role") or "").lower())
            role = _ROLE.get(role) or _ROLE.get(role.removesuffix("я").removesuffix("а")) or \
                ("representative" if rx is _REPRESENTED else "manager")
            out.setdefault(name.casefold(), {"name": name, "role": role,
                                             "snippet": flat[max(0, m.start() - 60):m.end() + 40]})
    return list(out.values())[:10]


def company_mentions(text: str, domain: str) -> list[dict]:
    """Company names with a legal form, the ЕИК stated with them, and whether the text
    says that company operates this domain. Never returns an unvalidated ЕИК."""
    flat = re.sub(r"\s+", " ", text or "")[:400_000]
    low = flat.casefold()
    label = domain.casefold()
    out, seen = [], {}
    for rx in (_QUOTED, _UNQUOTED):
        for m in rx.finditer(flat):
            name = re.sub(r"\s+", " ", f"{m.group(1).strip()} {m.group(2)}")
            if len(name) > 90 or name.split()[0].casefold() in ("и", "на", "от", "the"):
                continue
            after = flat[m.end():m.end() + 300]
            e = _EIK_LABEL.search(after)
            eik = _eik_ok(e.group(1)) if e else None
            operator = _states_operator(low, m.start(), m.end(), label)
            key = (name.casefold(), eik)
            snippet = flat[max(0, m.start() - 120):m.end() + 160][:320]
            if key in seen:
                # The same party named again: keep the occurrence that states its role.
                prev = seen[key]
                if operator and not prev["operator"]:
                    prev.update(operator=True, snippet=snippet)
                continue
            seat = _SEAT.search(flat[m.end():m.end() + 400])
            # This party's own passage: up to the next quoted name, legal form or defined
            # term. Contacts found here belong to THIS company, not its neighbour.
            party = flat[m.end():m.end() + 500]
            nxt = re.search(r"[„“\"«]|(?<![А-Яа-яA-Za-z])" + _FORM + r"(?![А-Яа-яA-Za-z])|"
                            r"означава|\bmeans\b", party[1:])
            party = party[:nxt.start() + 1] if nxt else party
            seen[key] = {"name": name, "eik": eik, "operator": operator, "snippet": snippet,
                         "address": seat.group(1).strip(" ,;") if seat else None,
                         "phones": [p.group(1).strip() for p in _PHONE_LABELLED.finditer(party)],
                         "emails": _EMAIL_ANY.findall(party)}
            out.append(seen[key])
            if len(out) >= 20:
                return out
    return out


# Words that may sit between "administrator of <domain>" and the company it names.
_CONNECTORS = {"е", "са", "от", "на", "и", "дружеството", "дружество", "фирмата", "фирма",
               "компанията", "търговеца", "търговецът", "търговец", "is", "by", "the",
               "company", "a", "an", "of"}
_DEFINITION = re.compile(r"означава|озн\.|\bmeans\b|\bshall mean\b")


def _states_operator(low: str, start: int, end: int, label: str) -> bool:
    """Does the text say THIS company operates THIS domain?

    Two shapes, both common in Bulgarian terms and conditions:
      before: "администраторът на уебсайта X.bg - дружеството „Name“ ЕООД"
              only connector words may sit between the role phrase and the name;
      after:  "„Name“ ЕАД, седалище..., ЕИК ..., дружество - собственик на ... X.bg"
              up to the next company name or the next defined term.
    Proximity alone is not enough: definition lists put the next party right after the
    previous party's role phrase (technopolis.bg defines ТЕХНОПОЛИС, then ПРАКТИКЕР).
    """
    pre = low[max(0, start - 250):start]
    words = [w for w in _OPERATOR_WORDS if w in pre]
    if words and label in pre:
        anchor = max(max(pre.rfind(w) + len(w) for w in words), pre.rfind(label) + len(label))
        gap = re.sub(r"[^\wа-я]+", " ", pre[anchor:]).split()
        if len(gap) <= 6 and all(g in _CONNECTORS for g in gap) and \
                not _DEFINITION.search(pre[anchor:]):
            return True
    post = low[end:end + 400]
    nxt = re.search(r"[„“\"«]|(?<![а-яa-z])" + _FORM.lower() + r"(?![а-яa-z])", post[1:])
    stop = [x for x in (nxt.start() + 1 if nxt else None,
                        (_DEFINITION.search(post) or None) and _DEFINITION.search(post).start())
            if x is not None]
    post = post[:min(stop)] if stop else post
    return label in post and any(w in post for w in _OPERATOR_WORDS)


def _eik_ok(v: str | None) -> str | None:
    if not v:
        return None
    d = re.sub(r"\D", "", str(v))
    return d if len(d) in (9, 13) and validate_eik(d)["valid"] else None


def _vat_split(v: str | None) -> tuple[str, str] | None:
    """('BG', '123456789') for a company VAT number; None for anything else.

    A Bulgarian VAT number with TEN digits is a sole trader's personal number (ЕГН)
    and is never treated as an identifier here - it is forbidden personal data.
    """
    m = re.fullmatch(r"([A-Z]{2})\s*([0-9A-Z]{8,12})", (v or "").upper().replace(" ", ""))
    if not m:
        return None
    cc, num = m.groups()
    if cc == "BG" and not re.fullmatch(r"\d{9}|\d{13}", num):
        return None
    return cc, num


LEGAL_FORMS = (("ЕООД", "EOOD"), ("ЕАД", "EAD"), ("ООД", "OOD"), ("АД", "AD"),
               ("ЕТ", "ET"), ("КДА", "KDA"), ("КД", "KD"), ("СД", "SD"))


def legal_form(name: str | None) -> str | None:
    if not name:
        return None
    up = name.upper()
    for bg, lat in LEGAL_FORMS:
        if any(re.search(rf"(?<![А-ЯA-Z]){f}(?![А-ЯA-Z])", up) for f in (bg, lat)):
            return bg
    return None


def _plausible_name(name: str | None) -> bool:
    """A company name only counts if it carries a legal form and is name-sized.

    The page extractor sometimes returns a sentence fragment ("та. Той въвежда...");
    matching that against a registry would manufacture a name conflict out of noise.
    """
    if not name or len(name) > 90 or legal_form(name) is None:
        return False
    # A sole trader is written "ЕТ „Name“". A trailing ЕТ after a line with digits is an
    # upper-case product title ("... W1D2A964ADS КАПАЦИТ ЕТ"), not a trader.
    if re.search(r"(?<![А-ЯA-Z])ЕТ\W*$", name.upper()) and re.search(r"\d", name):
        return False
    return True


CLAIMS = {"nap_declaration": "operator declared this web shop to NAP (public e-shop list)",
          "eik_on_site": "ЕИК published by the operator",
          "vat_on_site": "VAT number published by the operator",
          "structured_data": "identifier in the site's structured data",
          "company_statement": "company named with its ЕИК in the operator's document",
          "operator_statement": "document states this company operates the domain",
          "search_snippet": "identifier in a search result",
          "official_record": "official record"}


def candidates(pages: list[dict], search_results: list[dict], domain: str,
               nap: list[dict] = ()) -> dict:
    """ЕИК -> evidence items. Only checksum-valid identifiers are admitted."""
    cands: dict[str, list[dict]] = {}
    names: list[dict] = []
    foreign: list[dict] = []

    def add(eik, tier, url, code, value, when, sha=None, **extra):
        cands.setdefault(eik, []).append({
            "tier": tier, "source_url": url, "claim_code": code, "value": value,
            "claim": f"{CLAIMS[code]}: {value}", "retrieved_at": when, "content_hash": sha,
            "host": (urlsplit(url).hostname or "").lower(), **extra})

    for p in pages:
        f, meta = p["found"], {"archived": p.get("archived"), "legal_doc": p.get("legal_doc"),
                               "kind": p.get("kind")}
        if _eik_ok(f.get("eik")):
            add(_eik_ok(f["eik"]), "company_source", p["url"], "eik_on_site", f["eik"],
                p["retrieved_at"], p["sha256"], operator=False, **meta)
        vs = _vat_split(f.get("vat_number"))
        if vs and vs[0] == "BG" and _eik_ok(vs[1]):
            add(vs[1], "company_source", p["url"], "vat_on_site", "BG" + vs[1],
                p["retrieved_at"], p["sha256"], operator=False, **meta)
        elif vs:
            foreign.append({"country": vs[0], "number": vs[1], "url": p["url"]})
        if _plausible_name(f.get("company_name")):
            names.append({"name": f["company_name"], "tier": "company_source", "url": p["url"],
                          "operator": False, "how": "label", "legal_doc": p.get("legal_doc")})
        for org in p.get("jsonld") or []:
            for k in ("taxID", "vatID", "identifier"):
                v = org.get(k)
                if not isinstance(v, str):
                    continue
                vs = _vat_split(v)
                if vs and vs[0] != "BG":
                    foreign.append({"country": vs[0], "number": vs[1], "url": p["url"]})
                    continue
                e = _eik_ok(vs[1] if vs else v)
                if e:
                    add(e, "company_source", p["url"], "structured_data", f"{k}={v}",
                        p["retrieved_at"], p["sha256"], operator=False, **meta)
            if isinstance(org.get("legalName"), str):
                names.append({"name": org["legalName"], "tier": "company_source",
                              "url": p["url"], "operator": False, "how": "structured_data",
                              "legal_doc": p.get("legal_doc")})
        for mn in p.get("mentions") or []:
            names.append({"name": mn["name"], "tier": "company_source", "url": p["url"],
                          "operator": mn["operator"], "how": "mention",
                          "legal_doc": p.get("legal_doc")})
            if mn["eik"]:
                code = "operator_statement" if mn["operator"] else "company_statement"
                add(mn["eik"], "company_source", p["url"], code,
                    f"{mn['name']}, ЕИК {mn['eik']}", p["retrieved_at"], p["sha256"],
                    operator=mn["operator"], name=mn["name"], snippet=mn["snippet"], **meta)

    for e in nap:
        # The operator itself declared this web shop to NAP: an official record of the
        # domain -> operator link, not merely a page that mentions a company.
        if _eik_ok(e.get("eik")):
            add(e["eik"], "official", sources.NAP_PAGE, "nap_declaration",
                f"{e.get('shop_name') or domain}, ЕИК {e['eik']}",
                e.get("retrieved_at") or now_iso(), operator=True, nap=True,
                declared_at=e.get("declared_at"), declared_name=e.get("declared_name"))
    for r in search_results:
        host = (urlsplit(r["result_url"]).hostname or "").lower()
        own = bool(host) and registrable_domain(host) == domain
        text = f"{r['result_title']} {r['result_snippet']}"
        # A third-party page counts only if it names the domain: otherwise the ЕИК in
        # its snippet belongs to whatever else that page is about.
        if not own and domain not in text.lower():
            continue
        for mn in company_mentions(text, domain):
            names.append({"name": mn["name"], "tier": "search_evidence",
                          "url": r["result_url"], "operator": mn["operator"]})
            if mn["eik"]:
                add(mn["eik"], "search_evidence", r["result_url"], "search_snippet",
                    f"{mn['name']}, ЕИК {mn['eik']}", r["retrieved_at"], own=own,
                    operator=mn["operator"], name=mn["name"])
        for rx in (_EIK_LABEL, _VAT):
            for m in rx.finditer(text):
                e = _eik_ok(m.group(1))
                if e and not any(i["source_url"] == r["result_url"]
                                 for i in cands.get(e, [])):
                    add(e, "search_evidence", r["result_url"], "search_snippet",
                        m.group(0)[:80], r["retrieved_at"], own=own, operator=False)
    return {"eiks": cands, "names": names, "foreign": foreign}


# --------------------------------------------------------------------------
# resolution: explained confidence, authority before numbers, never a guess
# --------------------------------------------------------------------------

def _names_agree(a: str | None, b: str | None) -> bool | None:
    """Word overlap, or - for PDFs that drop the spaces between words - the same letters.

    Word-exported PDFs often position every glyph and omit the space glyphs, so the text
    reads "НаспърсКласифайдсБългария ЕООД"; compared letter for letter, with legal forms
    and punctuation removed, it is still the registry's "Наспърс Класифайдс България".
    """
    m = name_matches(a, b)
    if m or not a or not b:
        return m
    core = lambda s: re.sub(r"[^\wа-я]|ЕООД|ЕАД|ООД|АД|ЕТ|EOOD|OOD|EAD", "",
                            s.upper(), flags=re.IGNORECASE)
    ca, cb = core(a), core(b)
    if ca and ca == cb:
        return True
    return False if (m is None and ca and cb) else m


class Reason(str):
    """An English reason that also carries a code and parameters for translation."""

    def __new__(cls, code: str, text: str, **params):
        obj = super().__new__(cls, text)
        obj.code, obj.params = code, params
        return obj

    def as_dict(self) -> dict:
        return {"code": self.code, "text": str(self), "params": self.params}


def _assess(eik: str, items: list[dict], names: list[dict], off: dict, reg: dict) -> dict:
    own = [i for i in items if i["tier"] == "company_source"]
    live_own = [i for i in own if not i.get("archived")]
    nap = [i for i in items if i.get("nap")]
    operator = [i for i in items if i.get("operator") and not i.get("nap") and
                (i["tier"] == "company_source" or i.get("own"))]
    search_own = [i for i in items if i["tier"] == "search_evidence" and i.get("own")]
    public_hosts = {i["host"] for i in items if i["tier"] in ("search_evidence",
                                                             "public_source")
                    and not i.get("own")}
    reg_ok, off_ok = reg.get("status") == "confirmed", off.get("status") == "confirmed"
    official_name = reg.get("name") if reg_ok else off.get("name") if off_ok else None
    mine = [n["name"] for n in names if n["tier"] == "company_source"] + \
           [i["name"] for i in items if i.get("name")]
    agree = [_names_agree(n, official_name) for n in mine] if official_name else []
    name_ok = True if any(agree) else (False if agree and all(a is False for a in agree)
                                       else None)
    reasons: list[Reason] = []
    shown = lambda nm: official_name if (official_name and _names_agree(nm, official_name)) \
        else nm
    if nap:
        reasons.append(Reason("nap_declared", "the operator declared this web shop to NAP "
                              "(public list of e-shops)", declared_at=nap[0].get("declared_at")))
    if operator:
        o = operator[0]
        arch = (o.get("archived") or "")[:8]
        reasons.append(Reason("operator_statement",
                              f"a document on {o['host']} states that "
                              f"{shown(o.get('name')) or 'the company'} operates this domain"
                              + (f" (archived {arch})" if arch else ""),
                              host=o["host"], name=shown(o.get("name")), archived=arch or None))
    if own:
        o = own[0]
        arch = (o.get("archived") or "")[:8]
        reasons.append(Reason("eik_in_own_doc",
                              f"ЕИК published in the operator's own "
                              f"{'legal document' if o.get('legal_doc') else 'page'} on "
                              f"{o['host']}" + (f" (archived copy {arch})" if arch else ""),
                              host=o["host"], legal=bool(o.get("legal_doc")),
                              archived=arch or None))
    if search_own:
        reasons.append(Reason("search_own", "a search index shows the ЕИК on the domain's "
                                            "own pages"))
    if public_hosts:
        reasons.append(Reason("public_sources", f"{len(public_hosts)} independent public "
                              f"source(s) associate it with the domain", n=len(public_hosts)))
    if reg_ok:
        reasons.append(Reason("registry_confirmed", "confirmed by the Commercial Register"))
    elif off_ok:
        reasons.append(Reason("vies_confirmed", "the ЕИК is a valid VAT registration in EU VIES"))
    if name_ok is True:
        reasons.append(Reason("name_match", "the official name matches the name the operator "
                                            "publishes"))

    if nap and not (own or operator):
        level = "high"                  # an official record of the domain -> operator link
    elif own or operator:
        level = "high"
        archived_only = Reason("archived_unconfirmed", "seen only in an archived copy and "
                                                       "not confirmed officially")
        if not live_own and not operator and not (reg_ok or off_ok):
            level = "medium"
            reasons.append(archived_only)
        elif not live_own and all(i.get("archived") for i in operator) and not (reg_ok or off_ok):
            level = "medium"
            reasons.append(archived_only)
    elif search_own and (reg_ok or off_ok):
        level = "medium"
    elif len(public_hosts) >= 2:
        level = "medium"
    else:
        level = "low"
        reasons.append(Reason("weak_only", "only weak, indirect evidence"))
    if name_ok is False and level == "high":
        level = "medium"
        reasons.append(Reason("name_conflict", "the official name differs from the name the "
                                               "operator publishes"))
    return {"eik": eik, "level": level, "reasons": reasons, "verified": reg_ok or off_ok,
            "verified_by": "registry" if reg_ok else "official" if off_ok else None,
            "operator": bool(operator or nap), "name_ok": name_ok,
            "official_name": official_name,
            "site_names": mine[:3], "evidence": items,
            "best_tier": min(items, key=lambda i: AUTHORITY.index(i["tier"]))["tier"],
            "pages": len({i["source_url"] for i in own})}


def resolve(found: dict, official: dict[str, dict], registry: dict[str, dict]) -> dict:
    """Pick the operating entity, or decline to. A wrong company is worse than none."""
    conflicts: list[dict] = []
    scored = [_assess(e, items, found["names"], official.get(e) or {}, registry.get(e) or {})
              for e, items in found["eiks"].items()]
    for s in scored:
        if s["name_ok"] is False:
            conflicts.append({"type": "name_mismatch", "eik": s["eik"],
                              "website_names": s["site_names"],
                              "official_name": s["official_name"],
                              "note": "the operator's published name differs from the "
                                      "official one: outdated text, a renamed company, or "
                                      "a different operator"})
    rank = lambda s: (-LEVELS.index(s["level"]), AUTHORITY.index(s["best_tier"]),
                      not s["operator"], not s["verified"], s["name_ok"] is not True,
                      -s["pages"])
    scored.sort(key=rank)
    ambiguous = len(scored) > 1 and rank(scored[0]) == rank(scored[1])
    if len(scored) > 1:
        conflicts.append({"type": "multiple_identifiers",
                          "eiks": [{"eik": s["eik"], "tier": s["best_tier"],
                                    "level": s["level"]} for s in scored[:5]],
                          "note": "more than one valid ЕИК is associated with this domain; "
                                  "the best-supported one is used only if it is clearly "
                                  "better supported"})
    if scored and not ambiguous and scored[0]["level"] in ("high", "medium"):
        top = scored[0]
        return {"company_identified": True, "eik": top["eik"],
                "confidence_level": top["level"], "reasons": top["reasons"],
                "basis": top["best_tier"], "verified": top["verified"],
                "verified_by": top["verified_by"], "evidence": top["evidence"],
                "conflicts": conflicts, "alternatives": [s["eik"] for s in scored[1:5]],
                "candidate": None}
    if ambiguous:
        return {"company_identified": False, "eik": None, "confidence_level": "insufficient",
                "reasons": [Reason("ambiguous", "two or more companies are equally "
                                               "supported; choosing one would be a guess")],
                "basis": None, "verified": False, "verified_by": None, "evidence": [],
                "conflicts": conflicts, "alternatives": [s["eik"] for s in scored[:5]],
                "candidate": None}
    if found["foreign"]:
        f = found["foreign"][0]
        return {"company_identified": True, "eik": None, "foreign_vat": f,
                "confidence_level": "high",
                "reasons": [Reason("foreign_vat", f"the operator publishes the {f['country']} "
                                   f"VAT number {f['country']}{f['number']}",
                                   vat=f["country"] + f["number"])],
                "basis": "company_source", "verified": False, "verified_by": None,
                "evidence": [], "conflicts": conflicts, "alternatives": [], "candidate": None}
    # A name alone names the operator only when the operator said so: a labelled
    # "Фирма:" field, structured data, a legal page, or an explicit operator statement.
    # A company mentioned in passing on a product page is not the operator.
    named = [n for n in found["names"] if n["tier"] == "company_source"
             and _plausible_name(n["name"])
             and (n.get("operator") or n.get("how") in ("label", "structured_data")
                  or n.get("legal_doc"))]
    named.sort(key=lambda n: not n.get("operator"))
    if named:
        n = named[0]
        return {"company_identified": True, "eik": None, "name": n["name"],
                "confidence_level": "medium",
                "reasons": [Reason("name_only", f"the operator's own page names {n['name']}"
                                   + (" as operator of the domain" if n.get("operator") else ""),
                                   name=n["name"], operator=bool(n.get("operator"))),
                            Reason("no_eik_for_name", "no ЕИК was published with it, so no "
                                                      "official record could be matched")],
                "basis": "company_source", "verified": False, "verified_by": None,
                "evidence": [], "conflicts": conflicts, "alternatives": [], "candidate": None}
    low = scored[0] if scored else None
    return {"company_identified": False, "eik": None, "confidence_level":
            "low" if low else "insufficient",
            "reasons": (low["reasons"] if low else
                        [Reason("none", "no source associated a company with this domain")]),
            "basis": None, "verified": False, "verified_by": None, "evidence": [],
            "conflicts": conflicts, "alternatives": [s["eik"] for s in scored],
            "candidate": {"eik": low["eik"], "reasons": [str(x) for x in low["reasons"]]}
            if low else None}


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def _parallel(budget: float = DIRECT_BUDGET + 5, **jobs):
    """Run independent collectors at once; each returns a status dict, never raises."""
    out, threads = {}, []
    for name, fn in jobs.items():
        def run(n=name, f=fn):
            try:
                out[n] = f()
            except Exception as e:                      # a collector bug is one source down
                out[n] = {"status": sources.FAILED, "error": f"{type(e).__name__}: {e}"[:200]}
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
    end = time.time() + budget
    for t in threads:
        t.join(max(0.0, end - time.time()))
    return {n: out.get(n, {"status": sources.TEMP_UNAVAILABLE, "error": "timeout"})
            for n in jobs}


def _dns_fingerprint(host: str, getter) -> tuple | None:
    """What a name resolves to: its first CNAME target, else its address set."""
    try:
        data = getter(f"{DOH}?" + urllib.parse.urlencode({"name": host, "type": "A"}),
                      {"Accept": "application/dns-json"})
    except (urllib.error.URLError, OSError, ValueError):
        return None
    ans = data.get("Answer") or [] if data.get("Status") == 0 else []
    cname = next((a.get("data") for a in ans if a.get("type") == 5), None)
    if cname:
        return ("CNAME", cname.lower())
    ips = tuple(sorted(a.get("data") for a in ans if a.get("type") in (1, 28)))
    return ("A", ips) if ips else None


def host_exists_fn(domain: str, getter):
    """Existence test that survives wildcard DNS.

    olx.bg answers every name with the same CloudFront target, so "it resolves" proves
    nothing there. A random probe name gives the wildcard's fingerprint; a real host is
    one that resolves somewhere else.
    """
    probe = _dns_fingerprint(f"shopwatch-probe-{secrets.token_hex(6)}.{domain}", getter)

    def exists(host: str) -> bool:
        fp = _dns_fingerprint(host, getter)
        return fp is not None and fp != probe
    return exists


def _strong(pages_found: dict) -> bool:
    return any(i["tier"] == "company_source" and not i.get("archived")
               for items in pages_found["eiks"].values() for i in items)


SITE_STATE = {"ok": sources.AVAILABLE, "redirected_abroad": sources.AVAILABLE,
              "blocked": sources.BLOCKED, "robots_disallowed": sources.BLOCKED,
              "timeout": sources.TEMP_UNAVAILABLE, "unreachable": sources.FAILED,
              "error": sources.FAILED}


STAGES = ("fetching", "discovering", "legal_sources", "verifying")


def _provisional(found: dict) -> dict | None:
    """The best candidate so far, BEFORE any official check. Shown as "possible
    operator, verification in progress" - never as the answer."""
    res = resolve(found, {}, {})
    if not res["company_identified"]:
        return None
    ev = res["evidence"][0] if res["evidence"] else {}
    name = next((i.get("name") for i in res["evidence"] if i.get("name")), None) or res.get("name")
    return {"eik": res.get("eik"), "name": name, "level": res["confidence_level"],
            "host": ev.get("host"), "archived": (ev.get("archived") or "")[:8] or None}


def check(raw_url: str, *, store: Store | None = None, refresh: bool = False,
          fetch=None, getter=_get_json, search=None, vat_checker=check_vat,
          registry=None, archive=None, tls_fn=None, progress=None, ai=None,
          nap_source=None) -> dict:
    """Run the full domain check. Raises InputError for bad input only.

    search:   list of SearchProvider (default: the configured ones)
    registry: a CompanyRegistryProvider (default: the Registry Agency adapter)
    archive:  a WaybackArchive-like object (default: the Internet Archive)
    nap_source: the NAP e-shop list lookup (default: sources.nap_eshops); the benchmark
              swaps it out to measure what the other sources resolve on their own
    progress: optional callback(stage, code, **info), called when the check actually
              enters a stage (one of STAGES) or finds something. It may raise to abort
              the check (job cancellation); nothing else about the check changes.
    """
    report = progress or (lambda *a, **k: None)
    n = normalise(raw_url)
    domain, hostname = n["registrable_domain"], n["hostname"]
    if store and not refresh:
        hit = store.cache_get("domain", cache_key(domain), TTL["domain"])
        if hit:
            return {**hit, "cached": True}
    tls_fn = tls_fn or tls_certificate
    providers = sources.search_providers(getter) if search is None else search
    registry = registry or sources.RegistryAgencyProvider(store)
    archive = archive or sources.WaybackArchive(getter)
    crawler = Crawler(domain, fetch)
    report("fetching", "trying_site")
    # Surface a candidate the moment a page names one, not when every source is back.
    crawler.observer = lambda: report("fetching", "evidence_found", provisional=
                                      _provisional(candidates(crawler.pages, [], domain)))

    # ---- stage A ------------------------------------------------------------------
    got = _parallel(
        site=lambda: collect_site(n["canonical_url"], crawler),
        dns=lambda: _cached(store, "dns", domain, lambda: dns_records(domain, getter), refresh),
        rdap=lambda: _cached(store, "rdap", domain, lambda: rdap(domain, getter, store),
                             refresh),
        tls=lambda: tls_fn(hostname),
        ct=lambda: _cached(store, "ct", domain, lambda: sources.ct_hosts(domain, getter),
                           refresh),
        search=lambda: _cached(store, "search", domain, lambda: sources.run_search(
            providers, sources.search_queries(domain, hostname, [])), refresh),
        nap=lambda: _cached(store, "nap", domain, lambda: (nap_source or sources.nap_eshops)(
            domain, getter, registrable_domain), refresh),
    )
    site, search_state = got["site"], got["search"]
    nap = got["nap"].get("entries") or []
    found = candidates(crawler.pages, search_state.get("results") or [], domain, nap)
    report("fetching", "site_read" if site["status"] in ("ok", "redirected_abroad") else
           "site_blocked" if site["status"] in ("blocked", "robots_disallowed") else
           "site_failed", site=SITE_STATE.get(site["status"], sources.FAILED),
           provisional=_provisional(found))

    # ---- stage B: only while the operator's own sources have not named an ЕИК --------
    discovery = {"related_hosts": sources.state(sources.NOT_APPLICABLE, None, hosts=[]),
                 "public_legal_pages": sources.state(sources.NOT_APPLICABLE, None, pages=[]),
                 "web_archive": sources.state(sources.NOT_APPLICABLE, None)}
    if not _strong(found):
        report("discovering", "related_hosts")
        pages_before = len(crawler.pages)
        entry_links = [lk for p in crawler.pages for _t, lk in p["links"]]
        linked_hosts = [urlsplit(u).hostname for u in entry_links]
        ct_names = got["ct"].get("hosts") or []
        crawled = {p["host"] for p in crawler.pages} | crawler.refused_hosts
        related = sources.rank_related(domain, hostname,
                                       [h for h in dict.fromkeys(linked_hosts + ct_names) if h],
                                       host_exists_fn(domain, getter), skip=crawled)[:4]
        # Related hosts first, then legal URLs a search index surfaced, then - only on a
        # main host that answers us - the conventional legal paths. A host that refused
        # us is never probed further.
        seeds = [f"https://{h}/" for h in related]
        seeds += [r["result_url"] for r in search_state.get("results") or []
                  if registrable_domain(urlsplit(r["result_url"]).hostname or "") == domain
                  and _legalish(r["result_url"], r["result_title"])][:3]
        if site["status"] in ("ok", "redirected_abroad"):
            base = f"{n['scheme']}://{hostname}"
            seeds += [base + p for p in KNOWN_LEGAL_PATHS[:5]]
        blocked = site["status"] in ("blocked", "robots_disallowed", "timeout")
        report("legal_sources", "legal_docs", hosts=related)
        crawler.observer = lambda: report("legal_sources", "evidence_found", provisional=
                                          _provisional(candidates(crawler.pages, [], domain)))

        def discover():
            legal_crawl(crawler, seeds, stop_when=lambda: _strong(
                candidates(crawler.pages, [], domain)))
            if _strong(candidates(crawler.pages, [], domain)):
                return None
            starts = ([n["canonical_url"]] if blocked else []) + \
                     [f"https://{h}/" for h in related if h in crawler.refused_hosts]
            if starts:
                report("legal_sources", "archive")
            return archive_fallback(crawler, starts, archive) if starts else None
        done, arch = _bounded(discover, DISCOVERY_BUDGET)
        new_pages = crawler.pages[pages_before:]
        discovery["related_hosts"] = sources.state(
            sources.AVAILABLE, sources.FOUND if related else sources.NOT_FOUND, hosts=related)
        legal_found = any(p["found"].get("eik") or any(m["eik"] for m in p["mentions"])
                          for p in new_pages if not p.get("archived"))
        # Running out of time after finding the evidence is still a source that worked.
        discovery["public_legal_pages"] = sources.state(
            sources.AVAILABLE if done or legal_found else sources.TEMP_UNAVAILABLE,
            sources.FOUND if legal_found else (sources.NOT_FOUND if done else None),
            pages=[p["url"] for p in new_pages if not p.get("archived")][:15],
            **({} if done else {"note": "discovery time budget reached"}))
        if isinstance(arch, dict):
            discovery["web_archive"] = arch
        elif not done:
            discovery["web_archive"] = sources.state(sources.TEMP_UNAVAILABLE, None,
                                                     note="discovery time budget reached")
        found = candidates(crawler.pages, search_state.get("results") or [], domain, nap)
        crawler.observer = None
        skipped = []
    else:
        skipped = ["discovering", "legal_sources"]     # the operator's own pages sufficed

    # A name without an ЕИК: ask the search index about that name with the domain.
    names = [x["name"] for x in found["names"] if x["tier"] == "company_source"]
    if providers and names and not found["eiks"]:
        more = sources.run_search(providers, sources.search_queries(domain, hostname,
                                                                    names)[-2:])
        search_state = {**search_state, "results": (search_state.get("results") or [])
                        + (more.get("results") or [])}
        found = candidates(crawler.pages, search_state["results"], domain, nap)

    # ---- stage C: official corroboration ----------------------------------------------
    report("verifying", "verifying", skipped=skipped, provisional=_provisional(found))
    order = sorted(found["eiks"], key=lambda e: min(
        (AUTHORITY.index(i["tier"]), not i.get("operator")) for i in found["eiks"][e]))[:3]
    reg_status = registry.status()
    reg_recs = {e: registry.lookup_by_eik(e) for e in order} if reg_status == \
        sources.AVAILABLE else {}
    # "status" is ours (was the register asked and did it answer); the trader's own
    # registered status travels as "registered_status".
    regmap = {e: ({**r, "registered_status": r.get("status"), "status": "confirmed"}
                  if r else {"status": "not_found"}) for e, r in reg_recs.items()}
    official = _parallel(10 + 5, **{e: (lambda e=e: vies(e, "BG", store, refresh,
                                                            vat_checker)) for e in order})
    res = resolve(found, official, regmap)
    if not res.get("eik") and res.get("name") and reg_status == sources.AVAILABLE:
        hits = registry.search_by_company_name(res["name"])
        if len(hits) == 1:                              # a unique official match only
            h = hits[0]
            found["eiks"].setdefault(h["eik"], []).append({
                "tier": "registry", "source_url": None, "claim_code": "official_record",
                "value": h["name"], "claim": f"{CLAIMS['official_record']}: {h['name']}",
                "retrieved_at": now_iso(), "content_hash": None, "host": "registry"})
            regmap[h["eik"]] = {**h, "registered_status": h.get("status"),
                                "status": "confirmed"}
            res = resolve(found, official, regmap)
    foreign_off = None
    if res.get("foreign_vat"):
        fv = res["foreign_vat"]
        foreign_off = vies(fv["number"], fv["country"], store, refresh, vat_checker)

    # ---- the compliance fields: collect published contacts if still missing ------------
    if res["company_identified"] and not all(
            any((p.get("contacts") or {}).get(k) for p in crawler.pages)
            for k in ("phones", "emails")):
        report("verifying", "contacts")
        _bounded(lambda: collect_contacts(crawler, n, archive,
                                          site["status"] not in ("ok", "redirected_abroad")),
                 CONTACT_BUDGET)

    return _finish(n, raw_url, store, crawler, site, got, search_state, discovery, found,
                   res, official, regmap, reg_status, foreign_off, ai or {})


CONTACT_BUDGET = 20


def collect_contacts(crawler: Crawler, n: dict, archive, blocked: bool,
                     max_live: int = 3, max_archived: int = 2) -> None:
    """Read the operator's contact / about pages that the identity search did not need.

    Contact-tier links already seen on the domain's pages, the main host first; live
    where the host answers us, else the archived copy. On a main host that answers,
    the two conventional contact paths are tried too. Bounded, and never a host that
    refused us live.
    """
    main = {n["hostname"], crawler.domain, "www." + crawler.domain}
    contact_hints = LEGAL_TIERS[1]
    links = [lk for p in crawler.pages for t, lk in p["links"]
             if any(h in lk.casefold() for h in contact_hints) or t == 1]
    if not blocked:
        base = f"{n['scheme']}://{n['hostname']}"
        links += [base + "/contact", base + "/kontakti"]
    links = sorted(dict.fromkeys(links),
                   key=lambda u: (urlsplit(u).hostname not in main, len(u)))
    live = arch = 0
    for link in links:
        if link in crawler.visited or any(p["url"] == link for p in crawler.pages):
            continue
        host = (urlsplit(link).hostname or "").lower()
        if host not in crawler.refused_hosts and live < max_live:
            live += 1
            try:
                crawler.get(link, timeout=PAGE_TIMEOUT)
            except Exception:
                pass
        elif host in crawler.refused_hosts and arch < max_archived:
            arch += 1
            try:
                time.sleep(archive.PAUSE)
                snap = archive.snapshot(link)
                if snap:
                    body, ctype = archive.fetch(snap)
                    crawler.record(link, body, {"content-type": ctype},
                                   archived=snap["timestamp"], via="web.archive.org")
            except Exception:
                pass
        if live >= max_live and arch >= max_archived:
            break


def vies(number: str, country: str = "BG", store: Store | None = None,
         refresh: bool = False, checker=check_vat) -> dict:
    return _cached(store, "vies", f"{country}{number}",
                   lambda: {**checker(number, country, timeout=10),
                            "retrieved_at": now_iso()}, refresh)


def _finish(n, raw_url, store, crawler, site, got, search_state, discovery, found, res,
            official, regmap, reg_status, foreign_off, ai: dict) -> dict:
    domain = n["registrable_domain"]
    eik = res.get("eik")
    company, sources_out, people = None, [], []
    site_name = next((x["name"] for x in found["names"] if x["tier"] == "company_source"
                      and x.get("operator")), None) or next(
        (x["name"] for x in found["names"] if x["tier"] == "company_source"
         and _plausible_name(x["name"])), None)
    if eik:
        reg, off = regmap.get(eik) or {}, official.get(eik) or {}
        if reg.get("status") == "confirmed":
            company = {k: reg.get(k) for k in ("name", "legal_form", "registered_address",
                                                "registration_date", "capital")}
            company.update(eik=eik, status=reg.get("registered_status"),
                           verified_by="registry")
            people = [dict(p, source="Commercial Register", status="verified",
                           last_verified=reg.get("received_at"))
                      for p in reg.get("persons") or []]
            sources_out.append({"type": "official", "name": "Bulgarian Registry Agency "
                                "(Commercial Register)", "url": None,
                                "retrieved_at": reg.get("received_at") or now_iso()})
        elif off.get("status") == "confirmed":
            company = {"eik": eik, "name": off.get("name"),
                       "legal_form": legal_form(off.get("name")), "status": VAT_ACTIVE,
                       "registered_address": off.get("address"), "registration_date": None,
                       "capital": None, "verified_by": "official"}
        else:
            named = next((i.get("name") for i in res["evidence"] if i.get("name")), None)
            name = named or site_name
            company = {"eik": eik, "name": name, "legal_form": legal_form(name),
                       "status": None, "registered_address": None, "registration_date": None,
                       "capital": None, "verified_by": None}
        if off.get("status") == "confirmed":
            sources_out.append({"type": "official", "name": "EU VIES (VAT registry)",
                                "url": VIES.format(cc="BG", num=eik),
                                "retrieved_at": off.get("retrieved_at"),
                                "claim_code": "official_record", "value": off.get("name")})
    elif res.get("foreign_vat") and foreign_off and foreign_off.get("status") == "confirmed":
        fv = res["foreign_vat"]
        company = {"eik": None, "vat_number": fv["country"] + fv["number"],
                   "name": foreign_off.get("name"),
                   "registered_address": foreign_off.get("address"), "status": VAT_ACTIVE,
                   "verified_by": "official", "foreign": True}
        sources_out.append({"type": "official", "name": "EU VIES (VAT registry)",
                            "url": VIES.format(cc=fv["country"], num=fv["number"]),
                            "retrieved_at": foreign_off.get("retrieved_at")})
    elif res.get("name"):
        company = {"eik": None, "name": res["name"], "legal_form": legal_form(res["name"]),
                   "verified_by": None}
    for ev in res["evidence"]:
        if ev["tier"] == "registry":
            continue
        sources_out.append({"type": ev["tier"], "url": ev["source_url"], "claim": ev["claim"],
                            "claim_code": ev["claim_code"], "value": ev["value"],
                            "retrieved_at": ev["retrieved_at"],
                            "archived": ev.get("archived"), "snippet": ev.get("snippet")})

    # ---- per-source state: availability kept apart from what was found -------------
    entry = site.get("entry")
    dns, rd, tls = got["dns"], got["rdap"], got["tls"]
    vies_state = (sources.state(sources.NOT_APPLICABLE, None,
                                note="no ЕИК or VAT number was found to validate")
                  if not official and not foreign_off else
                  sources.state(sources.TEMP_UNAVAILABLE, None)
                  if all((v or {}).get("status") == "unavailable"
                         for v in (list(official.values()) or [foreign_off])) else
                  sources.state(sources.AVAILABLE,
                                sources.FOUND if any((v or {}).get("status") == "confirmed"
                                                     for v in list(official.values())
                                                     + [foreign_off]) else sources.NOT_FOUND,
                                queried=list(official) + (["foreign VAT"] if foreign_off else [])))
    disc = {
        "target_site": sources.state(
            SITE_STATE.get(site["status"], sources.FAILED),
            (sources.FOUND if any(p for p in crawler.pages if p["host"] in
                                  (n["hostname"], domain) and not p.get("archived")
                                  and (p["found"].get("eik") or p["mentions"])) else
             sources.NOT_FOUND) if site["status"] in ("ok", "redirected_abroad") else None,
            detail=site["status"]),
        "search": {k: v for k, v in search_state.items() if k != "results"},
        "dns": sources.state(
            sources.TEMP_UNAVAILABLE if dns.get("status") == "unavailable" else sources.AVAILABLE,
            sources.NOT_FOUND if dns.get("status") == "nxdomain" else sources.FOUND
            if dns.get("records") else None, detail=dns.get("status"), supporting_only=True),
        "rdap": sources.state(
            sources.NOT_APPLICABLE if rd.get("status") == "unsupported_tld" else
            sources.TEMP_UNAVAILABLE if rd.get("status") == "unavailable" else
            sources.AVAILABLE, sources.FOUND if rd.get("status") == "ok" else None,
            detail=rd.get("status"), supporting_only=True),
        "tls": sources.state(
            sources.AVAILABLE if tls.get("status") in ("ok", "unverified") else sources.FAILED,
            sources.FOUND if tls.get("status") == "ok" else None, detail=tls.get("status"),
            supporting_only=True),
        "nap_register": {k: v for k, v in got["nap"].items() if k != "entries"}
                        | {"declarations": len(got["nap"].get("entries") or [])},
        "ct_logs": {k: v for k, v in got["ct"].items() if k != "hosts"}
                   | {"hosts_seen": len(got["ct"].get("hosts") or []), "supporting_only": True},
        **discovery,
        "vies": vies_state,
        "registry": sources.state(reg_status, (sources.FOUND if any(
            r.get("status") == "confirmed" for r in regmap.values()) else sources.NOT_FOUND)
            if reg_status == sources.AVAILABLE else None,
            note=None if reg_status == sources.AVAILABLE else
            "needs a Registry Agency contract or official-purpose access (see BLOCKERS.md)"),
    }
    verified = bool((company or {}).get("verified_by"))
    partial = site["status"] != "ok" or not verified
    disclosure = None
    if entry and entry.get("reachable"):
        m = dict(entry["merchant"])
        if m.get("vat_number") and not _vat_split(m.get("vat_number")):
            m["vat_number"] = None                     # 10-digit BG VAT = an ЕГН
        if privacy.classify_email(m.get("email"), domain) == "institution":
            m["email"] = None                          # КЗЛД, КЗП, a courier: not the shop
        disclosure = {"merchant": m, "assessment": entry["assessment"],
                      "sources": entry["sources"]}

    # People the operator's own pages name as acting for it: published, not verified.
    if res["company_identified"]:
        known = {p["name"].casefold() for p in people}
        for pg in crawler.pages:
            for p in pg.get("people") or []:
                if p["name"].casefold() not in known:
                    known.add(p["name"].casefold())
                    people.append({"name": p["name"], "role": p["role"],
                                   "source": "company_archive" if pg.get("archived") else
                                   "company_document" if pg.get("legal_doc") else "company_site",
                                   "source_url": pg["url"], "retrieved_at": pg["retrieved_at"],
                                   "status": "published"})

    level = res["confidence_level"]
    eik_off = dict(official.get(eik) or {}, url=VIES.format(cc="BG", num=eik)) if eik else None
    comp_profile = profile.build(domain, eik, crawler.pages, eik_off, regmap.get(eik) if eik else None,
                                 reg_status, disc["target_site"]["status"], res) \
        if res["company_identified"] else None

    def fetch_page(url):                     # the AI names a page; Shopwatch reads it itself
        before = len(crawler.pages)
        try:
            crawler.get(url, timeout=PAGE_TIMEOUT)
        except Exception:
            return None
        return crawler.pages[-1] if len(crawler.pages) > before else None
    ai_run = aifallback.fill(comp_profile, domain, store=store, fetch_page=fetch_page,
                             cfg=ai.get("cfg"), provider=ai.get("provider"))
    result = {
        "input_url": raw_url,
        "canonical_url": n["canonical_url"],
        "canonical_domain": domain,
        "hostname": n["hostname"],
        "status": "partial" if partial else "complete",
        "domain_resolution": {
            "status": "resolved" if res["company_identified"] else "unresolved",
            "company_identified": res["company_identified"],
            "confidence_level": level,
            "confidence": {"high": 0.9, "medium": 0.7, "low": 0.4}.get(level),
            "reasons": [str(x) for x in res["reasons"]],
            "reason_codes": [x.as_dict() if isinstance(x, Reason) else
                             {"code": None, "text": str(x), "params": {}}
                             for x in res["reasons"]],
            "basis": res["basis"],
            "candidate": res.get("candidate"),
        },
        "company": company,
        # Field-level compliance profile. Separate from domain_resolution on purpose: a
        # missing phone number says nothing about who operates the domain.
        "profile": comp_profile,
        "ai_fallback": ai_run,
        "people": people,
        "conflicts": res["conflicts"],
        "alternatives": res["alternatives"],
        "sources": sources_out,
        "discovery": disc,
        "target_site": {
            "fetch_status": disc["target_site"]["status"],
            "direct_fetch": site["status"],
            "pages_read": [{"url": p["url"], "retrieved_at": p["retrieved_at"],
                            "sha256": p["sha256"], "kind": p["kind"],
                            "archived": p.get("archived")} for p in crawler.pages][:25],
        },
        "infrastructure": {
            "supporting_only": True,
            "note": "Hosting, DNS and certificates identify infrastructure, not the legal "
                    "operator.",
            "dns": dns.get("records", {}),
            "rdap": {k: v for k, v in rd.items() if k != "status"},
            "tls": {k: v for k, v in tls.items() if k != "status"},
            "ct_hosts": (got["ct"].get("hosts") or [])[:40],
        },
        "search": {"status": search_state.get("status"),
                   "provider": search_state.get("provider"),
                   "results_used": [r for r in search_state.get("results") or []
                                    if any(r["result_url"] == e["source_url"]
                                           for e in res["evidence"])][:10]},
        "disclosure": disclosure,
        "checked_at": now_iso(),
        "cached": False,
    }
    result = apply_privacy(result, store)
    if store:
        persist(store, n, result, res, site)
        # An answer weakened by a source that was only temporarily down (archive rate
        # limit, time budget) is not cached: a retry a minute later may well succeed.
        degraded = not result["domain_resolution"]["company_identified"] and any(
            v.get("status") == sources.TEMP_UNAVAILABLE for v in result["discovery"].values()
            if not v.get("supporting_only"))
        if not degraded:
            store.cache_put("domain", cache_key(domain), result)
    return result


def apply_privacy(result: dict, store: Store | None) -> dict:
    """Last gate before output: allowlist the company block and each person's fields.

    Company identity is never withheld by this gate. People who act for the company are
    kept with their own source; identifiers (ЕГН, ЛНЧ...) never pass, and a restriction
    recorded for a field (PRIVACY.md section 4) removes it.
    """
    comp = result.get("company")
    blocked = store.restricted(comp["eik"]) if comp and comp.get("eik") and store \
        else frozenset()
    if comp:
        keep = {k: v for k, v in comp.items()
                if k in ("verified_by", "foreign") or privacy.is_field_publishable(k, "api")}
        for f in blocked:
            if f != "eik":
                keep.pop(f, None)
        result["company"] = keep
    result["people"] = [q for q in (privacy.filter_record(p, "api", subject="person",
                                                          restricted=blocked)
                                    for p in result.get("people") or []
                                    if privacy.is_field_publishable("name", "api",
                                                                    subject="person"))
                        if q.get("name")]
    return result


def persist(store: Store, n: dict, result: dict, res: dict, site: dict) -> None:
    did = store.upsert_domain(n["registrable_domain"], n["canonical_url"],
                              result["domain_resolution"]["status"])
    comp = result.get("company") or {}
    eid = None
    score = result["domain_resolution"]["confidence"]
    if comp.get("eik") and comp.get("verified_by"):
        eid, changed = store.upsert_entity(
            {"eik": comp["eik"], "name": comp.get("name"),
             "legal_form": comp.get("legal_form"), "status": comp.get("status"),
             "registered_address": comp.get("registered_address"),
             "source_timestamp": next((s.get("retrieved_at") for s in result["sources"]
                                       if s["type"] == "official"), None)},
            source=comp["verified_by"])
        if changed:
            result.setdefault("conflicts", []).append({
                "type": "official_record_changed", "fields": changed,
                "note": "the official record differs from what was stored at the last "
                        "check (renamed company, new address or status change)"})
        store.link(did, eid, score, result["domain_resolution"]["status"])
        store.subscribe(eid, "vies-poll", "active")
        store.subscribe(eid, "registry-agency", result["discovery"]["registry"]["status"])
        if result.get("people"):
            store.set_people(eid, comp["eik"], [p for p in result["people"]
                                                if p.get("status") == "verified"],
                             "registry", now_iso())
    for ev in res["evidence"]:
        store.add_evidence(did, eid, ev["tier"], ev["source_url"], ev["claim"], score,
                           ev["retrieved_at"], ev.get("content_hash"))
    if site["status"] != "ok":
        store.add_evidence(did, eid, "company_source", n["canonical_url"],
                           f"direct acquisition: {site['status']}", None, now_iso())



def tls_certificate(host: str, timeout: int = 10) -> dict:
    """The site's public certificate: subject, SANs, issuer, validity.

    Connects to an address that passed sealbox's guard, pinned, with SNI set - the same
    SSRF boundary as the page fetch. Only a certificate that verifies is described; an
    unverifiable one is recorded as such, since its contents are then unauthenticated.
    """
    _sealbox_fetch()                                   # puts sealbox on sys.path if needed
    from sealbox.capture import resolve_guarded  # noqa: PLC0415
    try:
        ip = resolve_guarded(host, 443)[0]
        with socket.create_connection((ip, 443), timeout=timeout) as raw:
            try:
                with ssl.create_default_context().wrap_socket(
                        raw, server_hostname=host) as tls:
                    cert = tls.getpeercert() or {}
            except ssl.SSLCertVerificationError as e:
                return {"status": "unverified", "verified": False,
                        "error": str(e)[:200]}
    except Exception as e:                             # CaptureError, refused, timeout
        return {"status": "unavailable", "error": f"{type(e).__name__}: {e}"[:200]}
    flat = lambda seq: {k: v for part in seq for k, v in part}
    return {"status": "ok", "verified": True,
            "subject": flat(cert.get("subject", ())), "issuer": flat(cert.get("issuer", ())),
            "san": [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"][:20],
            "not_before": cert.get("notBefore"), "not_after": cert.get("notAfter")}



# --------------------------------------------------------------------------
# DNS and RDAP - supporting evidence only, never proof of ownership
# --------------------------------------------------------------------------

def dns_records(domain: str, getter=_get_json) -> dict:
    out: dict = {"status": "ok", "records": {}}
    try:
        for rtype in ("A", "AAAA", "CNAME", "MX", "TXT", "NS"):
            q = urllib.parse.urlencode({"name": domain, "type": rtype})
            data = getter(f"{DOH}?{q}", {"Accept": "application/dns-json"})
            if data.get("Status") == 3:
                return {"status": "nxdomain", "records": {}}
            vals = [a.get("data", "")[:300] for a in data.get("Answer") or []
                    if a.get("type") == RRTYPE[rtype]]
            if vals:
                out["records"][rtype] = vals[:10]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"status": "unavailable", "records": {}, "error": str(e)[:200]}
    return out


def rdap(domain: str, getter=_get_json, store: Store | None = None) -> dict:
    """Registrar and lifecycle dates. Registrant details are deliberately not read."""
    tld = domain.rsplit(".", 1)[-1]
    try:
        boot = _cached(store, "bootstrap", "rdap", lambda: {
            "status": "ok", "services": getter(RDAP_BOOTSTRAP).get("services", [])})
        base = next((urls[0] for tlds, urls in boot["services"] if tld in tlds), None)
        if not base:
            return {"status": "unsupported_tld",
                    "note": f".{tld} publishes no RDAP service in the IANA registry"}
        data = getter(base.rstrip("/") + "/domain/" + urllib.parse.quote(domain))
    except urllib.error.HTTPError as e:
        return {"status": "not_found" if e.code == 404 else "unavailable",
                "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"status": "unavailable", "error": str(e)[:200]}
    registrar = None
    for ent in data.get("entities") or []:
        if "registrar" in (ent.get("roles") or []):
            for item in ((ent.get("vcardArray") or [None, []])[1] or []):
                if item and item[0] == "fn":
                    registrar = item[3]
    return {"status": "ok", "registrar": registrar,
            "events": {e.get("eventAction"): e.get("eventDate")
                       for e in data.get("events") or [] if e.get("eventAction")
                       in ("registration", "expiration", "last changed")},
            "domain_status": data.get("status") or [],
            "nameservers": [n.get("ldhName") for n in data.get("nameservers") or []][:8]}



# --------------------------------------------------------------------------
# automatic updates
# --------------------------------------------------------------------------

def refresh_stale(store: Store, vat_checker=check_vat, limit: int = 20) -> dict:
    """Re-verify retained entities whose official facts are older than REVERIFY_AFTER.

    ponytail: this polls VIES, the one official source available without a contract.
    The Registry Agency's push delivery of registrations/deletions is the real upgrade
    and slots in here once the contract and its Integration package are in hand.
    """
    done, changed = 0, 0
    for ent in store.stale_entities(REVERIFY_AFTER, limit):
        off = vat_checker(ent["eik"], "BG", timeout=10)
        if off.get("status") == "unavailable":
            continue
        rec = {"eik": ent["eik"], "source_timestamp": now_iso()}
        if off["status"] == "confirmed":
            rec.update(name=off.get("name"), registered_address=off.get("address"),
                       status=VAT_ACTIVE)
        else:
            rec.update(status="not_in_vies")
        _, ch = store.upsert_entity(rec, "official")
        done += 1
        changed += bool(ch)
    ingested = sources.RegistryAgencyProvider(store).ingest()
    purged = store.purge()
    return {"reverified": done, "changed": changed, "registry_records_ingested": ingested,
            "purged_domains": purged}


def start_worker(store: Store, every_s: int = 6 * 3600) -> threading.Thread:
    def loop():
        while True:
            try:
                print(f"domaincheck worker: {refresh_stale(store)}", flush=True)
            except Exception as e:
                print(f"domaincheck worker failed: {e}", flush=True)
            time.sleep(every_s)
    t = threading.Thread(target=loop, daemon=True, name="domaincheck-worker")
    t.start()
    return t


# --------------------------------------------------------------------------
# CLI: checks, the worker, and the data-subject workflow (19.)
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shopwatch.domaincheck",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="store path (default: domaincheck.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("url")
    c.add_argument("--refresh", action="store_true", help="ignore every cache (debugging)")
    sub.add_parser("refresh", help="re-verify stale entities and apply retention")
    adm = sub.add_parser("admin", help="data-subject requests")
    asub = adm.add_subparsers(dest="action", required=True)
    o = asub.add_parser("open")
    o.add_argument("kind", choices=("correction", "objection", "erasure", "access",
                                    "source_challenge"))
    o.add_argument("--subject", default="", help="person the request is about")
    o.add_argument("--eik")
    o.add_argument("--field")
    o.add_argument("--note", default="")
    r = asub.add_parser("resolve")
    r.add_argument("id", type=int)
    r.add_argument("decision", choices=("restrict", "erase", "correct", "reject"))
    r.add_argument("--note", required=True, help="why: which legal basis/retention duty")
    a = asub.add_parser("access")
    a.add_argument("subject")
    args = ap.parse_args(argv)

    store = Store(args.db) if args.db else Store()
    if args.cmd == "check":
        try:
            out = check(args.url, store=store, refresh=args.refresh)
        except InputError as e:
            print(f"input rejected: {e}")
            return 2
    elif args.cmd == "refresh":
        out = refresh_stale(store)
    elif args.action == "open":
        out = {"id": store.open_request(args.kind, args.subject, args.eik, args.field,
                                        args.note)}
    elif args.action == "resolve":
        out = store.resolve_request(args.id, args.decision, args.note)
    else:
        out = store.access_report(args.subject)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
