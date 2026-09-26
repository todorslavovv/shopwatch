"""Investigate a live shop URL: fetch, follow the pages that carry trader identity, score.

The offline core (disclosure + compliance) answers "given this HTML, what did the shop
publish". This module answers the operator's actual question: "given this URL, what does
this shop publish anywhere a customer could reasonably find it".

Fetching is delegated to sealbox's guarded fetcher rather than reimplemented — the
address guard there is the security boundary for pointing this tool at hostile input,
and a second copy of it would be a second thing to get wrong.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from shopwatch.compliance import assess
from shopwatch.registry import check_vat, name_matches
from shopwatch.disclosure import extract_merchant
from shopwatch.eik import validate_eik

# Shops put the trader identity on a secondary page far more often than on the
# homepage. Ranked, not a flat list: emag.bg publishes its EIK on the terms page, but
# document order put three delivery-fee pages ahead of it and the page budget ran out
# first. Weakest hints (privacy, generic "информация") match far more pages than they
# should, so they go last rather than being dropped.
LINK_TIERS = (
    ("контакт", "kontakt", "contact", "за нас", "za-nas", "za_nas", "about",
     "impressum", "реквизит", "фирмена", "за компанията"),
    ("общи условия", "obshti-usloviya", "usloviya", "usloviza", "terms",
     "conditions", "ползване"),
    ("поверителност", "privacy", "gdpr", "лични данни", "информация"),
)
LINK_HINTS = tuple(h for tier in LINK_TIERS for h in tier)
TIER_SHARE = (2, 2, 1)

# Fields that decide the verdict. Once all are present there is nothing left to find.
WANTED = ("eik", "company_name", "address", "email", "phone", "vat_number")

# Markers of a page whose text is assembled in the browser. Deliberately narrow: the
# fresh phishing shops this tool exists for are static HTML, so these never fire on
# them, and a scam site that publishes nothing still scores as publishing nothing.
APP_SHELL_MARKERS = (
    "auraconfig", "data-aura-rendered-by", "__next_data__", "window.__nuxt__",
    "ng-app", "data-reactroot", "__remix_context__",
)

# Page builders (WordPress/Elementor, Shopify themes) routinely serve 2-4 MB of HTML with
# the trader identity in the footer, i.e. at the very END. batashop.bg's ЕИК sits at byte
# 2.2 M of a 2.34 MB page; a 2 MB cap cut it off. Scripts and styles are stripped before
# extraction (strip_heavy), so a larger download does not mean a larger parse.
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_PAGES = 5
DEFAULT_TIMEOUT = 30


def _sealbox_fetch():
    """Return sealbox's fetch(), importing it from a sibling checkout if needed."""
    try:
        from sealbox.capture import fetch  # noqa: PLC0415
        return fetch
    except ImportError:
        pass
    sibling = Path(__file__).resolve().parent.parent.parent / "sealbox"
    if (sibling / "sealbox" / "capture.py").exists():
        sys.path.insert(0, str(sibling))
        from sealbox.capture import fetch  # noqa: PLC0415
        return fetch
    raise ImportError(
        "shopwatch needs sealbox's guarded fetcher. Clone sealbox next to shopwatch, "
        "or put it on PYTHONPATH. Offline use (extract_merchant on HTML you already "
        "have) does not need it."
    )


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE)


_HEAVY = re.compile(r"(?is)<(script|style|svg|noscript|template)\b[^>]*>.*?</\1\s*>|<!--.*?-->")


def strip_heavy(html: str) -> str:
    """Drop script/style/svg/comment bodies: never trader text, often most of the bytes.
    JSON-LD is kept - it is the one script that carries identity."""
    return _HEAVY.sub(lambda m: m.group(0) if "ld+json" in m.group(0)[:120].lower() else " ",
                      html)


def iri_to_uri(url: str) -> str:
    """Percent-encode a Cyrillic path or query ("/политика-на-бисквитките/"): http.client
    sends ASCII only, and such links otherwise fail with UnicodeEncodeError."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="=&%+/:@!$'()*,;?-._~"), ""))


def decode_body(body: bytes, content_type: str | None) -> str:
    """Decode page bytes to text, trying the declared charset before assuming UTF-8.

    Bulgarian shops on older platforms still serve windows-1251. Mis-decoding it does
    not throw - it silently yields mojibake, the Cyrillic labels stop matching, and the
    shop looks like it published nothing at all. That failure mode is worse than a
    fetch error, so the fallback is explicit.
    """
    declared = None
    if content_type:
        m = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type, re.IGNORECASE)
        if m:
            declared = m.group(1)
    if not declared:
        m = _META_CHARSET.search(body[:4096])
        if m:
            declared = m.group(1).decode("ascii", "ignore")
    for enc in (declared, "utf-8", "windows-1251"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


class _Links(HTMLParser):
    """Collect (href, anchor text) pairs. Nothing is executed; hrefs stay strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href, self._text = None, []


def disclosure_links(html: str, base_url: str, limit: int = 8) -> list[str]:
    """Same-host URLs likely to carry the trader identity, best guesses first.

    Same-host only: following an off-site link would mean fetching a third party and
    attributing whatever it says to this shop.
    """
    p = _Links()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    base_site = registrable(urlsplit(base_url).hostname or "")
    seen, ranked = set(), []
    same_host_links = 0
    for href, text in p.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        parts = urlsplit(full)
        if parts.scheme not in ("http", "https"):
            continue
        if registrable(parts.hostname or "") != base_site:
            continue
        full = full.split("#")[0]
        same_host_links += 1
        if full in seen or full.rstrip("/") == base_url.rstrip("/"):
            continue
        hay = (href + " " + text).casefold()
        for tier, hints in enumerate(LINK_TIERS):
            if any(h in hay for h in hints):
                seen.add(full)
                # "About Dura" is a product page: a hint in the URL beats one in the text.
                text_only = not any(h in href.casefold() for h in hints)
                ranked.append((tier, text_only, len(ranked), full))
                break
    disclosure_links.last_same_host_count = same_host_links
    ranked.sort()                                    # best tier, URL hits, then page order
    # With five pages, three contact/about variants must not crowd out the terms and the
    # privacy policy, which names the data controller - the trader - under the GDPR. So
    # each tier gets a share first (2, 2, 1), and only then the rest in rank order.
    head = [r for t, cap in enumerate(TIER_SHARE)
            for r in [q for q in ranked if q[0] == t][:cap]]
    return [r[3] for r in (head + [r for r in ranked if r not in head])[:limit]]


# Second-level suffixes in use for .bg. Without these, "a.com.bg" and "b.com.bg" would
# look like one site and the tool would happily follow a link to an unrelated trader.
MULTI_SUFFIX = ("com.bg", "org.bg", "net.bg", "edu.bg", "gov.bg", "co.uk", "com.tr")


def registrable(host: str) -> str:
    """The domain a trader owns, so subdomains of one shop count as that shop.

    olx.bg publishes its terms on help.olx.bg. Comparing bare hostnames made that look
    like a third party and the link was dropped, which is why olx.bg scored as
    publishing nothing at all.
    """
    host = (host or "").lower().strip(".").removeprefix("www.")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    for suf in MULTI_SUFFIX:
        if host.endswith("." + suf):
            return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def target_host(url: str) -> str:
    """Registrable-ish host for comparison: lowercase, no www."""
    parts = urlsplit(url if urlsplit(url).scheme else "https://" + url)
    return (parts.hostname or url).lower().removeprefix("www.")


def investigate(url: str, *, max_pages: int = DEFAULT_MAX_PAGES,
                timeout: int = DEFAULT_TIMEOUT, fetcher=None, verifier=None) -> dict:
    """Fetch a shop and score the trader identity it publishes.

    Returns a report entry: domain, merchant fields, evidence snippets, the URL each
    field came from, the pages visited, and the compliance assessment.
    """
    if not urlsplit(url).scheme:
        url = "https://" + url
    fetch = fetcher or _sealbox_fetch()

    merged = {f: None for f in WANTED}
    evidence: dict[str, str] = {}
    sources: dict[str, str] = {}
    visited: list[dict] = []
    errors: list[str] = []

    names_seen: list[str] = []
    queue = [url]
    pages = 0
    read_ok = False
    refused = False
    script_rendered = False
    shell_seen = False
    landed_on = None
    while queue and pages < max_pages:
        current = queue.pop(0)
        pages += 1
        try:
            r = fetch(iri_to_uri(current), timeout=timeout, max_bytes=MAX_BODY_BYTES)
        except Exception as e:                       # CaptureError and transport errors
            errors.append(f"{current}: {e}")
            visited.append({"url": current, "status": None, "error": str(e)})
            continue
        if current == url:
            landed_on = r.get("final_url", current)
        status = r.get("status")
        visited.append({"url": r.get("final_url", current), "status": status,
                        "bytes": len(r["body"]), "resolved_ip": r.get("resolved_ip")})
        if not (200 <= (status or 0) < 300):
            # A 403 bot-block or a 404 is not the shop's disclosure page. Parsing it
            # would score the blocker's error text as if the trader had published it.
            if status in (401, 403, 429):
                # Refused on purpose by a WAF or bot filter. Worth separating from a
                # site that is simply down: it says nothing about the trader, and it
                # is the operator's problem to route around, not the shop's fault.
                refused = True
            errors.append(f"{current}: HTTP {status}")
            continue
        read_ok = True
        html = strip_heavy(decode_body(r["body"], r["headers"].get("content-type")))
        low = html[:400000].casefold()
        if any(m in low for m in APP_SHELL_MARKERS):
            shell_seen = True
        try:
            found = extract_merchant(html)
        except (TypeError, ValueError) as e:
            errors.append(f"{current}: {e}")
            continue
        if found.get("company_name"):
            names_seen.append(found["company_name"])
        for field in WANTED:
            if merged[field] is None and found.get(field):
                merged[field] = found[field]
                sources[field] = r.get("final_url", current)
                if field in found.get("evidence", {}):
                    evidence[field] = found["evidence"][field]
        if all(merged[f] for f in WANTED):
            break
        if current == url:                            # only the entry page seeds the queue
            queue = disclosure_links(html, r.get("final_url", current))[: max_pages - 1]
            # A shop with no links at all in its served HTML is a JavaScript shell:
            # the nav, and with it any disclosure page, exists only after a browser
            # runs the app. We fetched something, but not the shop's actual content.
            if getattr(disclosure_links, "last_same_host_count", 0) == 0:
                script_rendered = True

    # A Bulgarian VAT number is "BG" + the ЕИК. A shop that publishes only BG207654830
    # has disclosed its ЕИК; scoring it NO_EIK would be a false alarm. 10 digits after
    # BG is a person's ЕГН, which validate_eik rejects, so that never becomes an ЕИК.
    eik_from_vat = False
    vat = (merged["vat_number"] or "").strip()
    if not merged["eik"] and vat.upper().startswith("BG") and \
            validate_eik(re.sub(r"\D", "", vat))["valid"]:
        merged["eik"] = re.sub(r"\D", "", vat)
        sources["eik"] = sources.get("vat_number")
        if "vat_number" in evidence:
            evidence["eik"] = evidence["vat_number"]
        eik_from_vat = True
    # ...and the other way round: "ЕИК BG208475759" is the VAT number written as the ЕИК.
    if merged["eik"] and not merged["vat_number"] and \
            re.search(r"BG\s?" + merged["eik"] + r"\b", evidence.get("eik") or ""):
        merged["vat_number"] = "BG" + merged["eik"]
        sources["vat_number"] = sources.get("eik")
        evidence["vat_number"] = evidence["eik"]

    # Registry check: the one question the page itself cannot answer.
    registry = {"status": "skipped", "name": None, "address": None, "source": "VIES"}
    if merged["eik"]:
        registry = (verifier or check_vat)(merged["eik"])
    name_ok = name_matches(merged["company_name"], registry.get("name"))

    asked = target_host(url)
    landed = target_host(landed_on) if landed_on else asked
    # Bulgarian disclosure law binds traders established in Bulgaria. bilka.bg redirects
    # to bilka.ro: scoring that against the EIK requirement is a category error, not a
    # finding, so it is reported out of scope rather than as a missing identifier.
    offsite = (registrable(landed) != registrable(asked)
               and landed.rsplit(".", 1)[-1] != asked.rsplit(".", 1)[-1])

    return {
        "domain": urlsplit(url).hostname or url,
        "final_host": landed,
        "offsite_redirect": offsite,
        "url": url,
        "merchant": dict(merged),
        "eik_from_vat": eik_from_vat,
        "evidence": evidence,
        "sources": sources,
        "pages_visited": visited,
        "errors": errors,
        "registry": registry,
        "registry_name_matches": name_ok,
        "reachable": read_ok,
        "refused": refused and not read_ok,
        "script_rendered": (script_rendered or shell_seen)
                           and not any(merged[f] for f in WANTED),
        "assessment": (
            {
                "score": None,
                "verdict": "out_of_scope",
                "findings": [{
                    "code": "REDIRECTED_ABROAD",
                    "severity": "error",
                    "message": f"{asked} redirects to {landed}, which is outside the "
                               f"Bulgarian jurisdiction this check applies.",
                }],
            }
            if read_ok and offsite else
            {
                "score": None,
                "verdict": "undetermined",
                "findings": [{
                    "code": "SCRIPT_RENDERED",
                    "severity": "error",
                    "message": "The pages are assembled in the browser: their text was "
                               "never present in what the server sent, so the shop's "
                               "disclosure was not visible to this check.",
                }],
            }
            if read_ok and (script_rendered or shell_seen)
            and not any(merged[f] for f in WANTED)
            else assess(merged, registry, company_names=names_seen)) if read_ok else {
            "score": None,
            "verdict": "blocked" if refused else "unreachable",
            "findings": [{
                "code": "BLOCKED" if refused else "NOT_FETCHED",
                "severity": "error",
                "message": (
                    "The site's bot protection refused the request (HTTP 403/429). "
                    "This says nothing about what the trader publishes."
                    if refused else
                    "No page could be read, so nothing can be said about what "
                    "this shop does or does not publish."),
            }],
        },
    }
