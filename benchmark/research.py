"""Broad 'external research' crawl used to build benchmark ground truth.

Deliberately wider than Shopwatch: up to MAX_PAGES same-site pages chosen from every
legal/contact wording, the sitemap, and conventional paths, with raw-text searches for
every identifier. It answers "what can a careful researcher find on this site", so the
benchmark can tell a genuine missing disclosure from a page Shopwatch never reached.
Same guarded fetcher, honest User-Agent, robots.txt honoured. Not used by the product.

    python -m benchmark.research domain1 domain2 ... > out.json
"""
from __future__ import annotations

import json
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

from shopwatch import domaincheck as dc
from shopwatch.eik import validate_eik
from shopwatch.investigate import _Links, decode_body

MAX_PAGES = 30
VOCAB = ("общи", "условия", "terms", "контакт", "contact", "kontakt", "за нас", "za-nas",
         "about", "поверителност", "privacy", "policy", "политика", "gdpr", "бисквитки",
         "cookie", "доставка", "delivery", "dostavka", "връщане", "return", "vrashtane",
         "рекламац", "reklamac", "фирма", "company", "legal", "imprint", "impressum",
         "информация", "information", "pravila", "правила", "uslovia", "usloviya")
GUESS = ("/obshti-usloviya", "/obshti-uslovia", "/terms", "/terms-and-conditions",
         "/kontakti", "/contacts", "/contact", "/contact-us", "/za-nas", "/about",
         "/about-us", "/privacy-policy", "/politika-za-poveritelnost", "/page/terms",
         "/page/contacts", "/content/terms", "/info/terms", "/uslovia", "/usloviya")

EIK_RX = re.compile(r"(?i)(ЕИК|Е\.И\.К\.|БУЛСТАТ|UIC|EIK|Идентификационен\s+номер|ЕИК/БУЛСТАТ)"
                    r"[^\d\n]{0,25}?(\d[\d\s]{7,15}\d)")
VAT_RX = re.compile(r"(?i)(?:ИН\s*по\s*ЗДДС|ДДС\s*(?:номер|№)|VAT(?:\s*(?:ID|No\.?|number))?|"
                    r"ИН\s*по\s*ДДС|ИДДС)[^A-Z\n]{0,15}(BG\s?\d{9,10})|\b(BG\d{9,10})\b")
PHONE_RX = re.compile(r"(?i)(?:тел(?:ефон)?|gsm|моб(?:илен)?|phone|tel|horeca|viber)\.?\s*[:\-–]?"
                      r"\s*((?:\+|00)?(?:359)?[\s\-/()]*0?\d[\d\s\-/()]{6,16}\d)")
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,10}")
ADDR_RX = re.compile(r"(?i)(адрес(?:\s+на\s+управление)?|седалище(?:\s+и\s+адрес\s+на\s+управление)?|"
                     r"офис|магазин|address)\s*[:\-–]\s*(.{8,140}?)(?=\s{2,}|\n|ЕИК|тел|e-?mail|$)")


def excerpt(text: str, start: int, end: int, pad: int = 70) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - pad):end + pad]).strip()[:260]


def extract(text: str, url: str, domain: str) -> dict:
    out = {k: [] for k in ("eik", "vat", "company", "address", "phone", "email")}
    for m in EIK_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group(2))
        out["eik"].append({"value": digits, "valid": validate_eik(digits)["valid"],
                           "url": url, "excerpt": excerpt(text, m.start(), m.end())})
    for m in VAT_RX.finditer(text):
        v = (m.group(1) or m.group(2) or "").replace(" ", "")
        out["vat"].append({"value": v, "url": url, "excerpt": excerpt(text, m.start(), m.end())})
    for mn in dc.company_mentions(text, domain):
        out["company"].append({"value": mn["name"], "eik": mn["eik"], "operator": mn["operator"],
                               "url": url, "excerpt": mn["snippet"][:260]})
    for m in ADDR_RX.finditer(text):
        out["address"].append({"value": m.group(2).strip(), "url": url,
                               "excerpt": excerpt(text, m.start(), m.end())})
    for m in PHONE_RX.finditer(text):
        out["phone"].append({"value": m.group(1).strip(), "url": url,
                             "excerpt": excerpt(text, m.start(), m.end())})
    for m in EMAIL_RX.finditer(text):
        if not m.group(0).lower().endswith((".png", ".jpg", ".webp", ".svg", ".gif")):
            out["email"].append({"value": m.group(0), "url": url,
                                 "excerpt": excerpt(text, m.start(), m.end())})
    return out


def research(domain: str, max_pages: int = MAX_PAGES) -> dict:
    c = dc.Crawler(domain)                    # for robots.txt and the guarded fetcher
    got = {"domain": domain, "researched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "pages": [], "blocked": None, "fields": {}, "errors": []}
    texts: dict[str, str] = {}

    def fetch(u: str):
        if not c.allowed(u):
            got["blocked"] = got["blocked"] or "robots"
            return None
        try:
            r = c.base_fetch(dc.iri_to_uri(u), timeout=15, max_bytes=8_000_000,
                             user_agent=dc.USER_AGENT)
        except Exception as e:
            got["errors"].append(f"{u}: {type(e).__name__}: {str(e)[:80]}")
            return None
        if dc._is_challenge(r) or r.get("status") in (401, 403, 429):
            got["blocked"] = got["blocked"] or f"http_{r.get('status')}"
            return None
        if not 200 <= (r.get("status") or 0) < 300:
            return None
        final = r.get("final_url", u)
        if final in texts:
            return None
        ctype = (r.get("headers") or {}).get("content-type") or ""
        if "pdf" in ctype or r["body"][:5] == b"%PDF-":
            try:
                texts[final] = dc.extract_text(r["body"])
            except Exception:
                texts[final] = ""
            return "", final
        html = dc.strip_heavy(decode_body(r["body"], ctype))
        texts[final] = dc._text_of_html(html) + "\n" + " ".join(
            f"{k}:{v}" for k, v in re.findall(r"""(?i)href=["'](tel|mailto):([^"'>]+)""", html))
        return html, final

    home = fetch(f"https://{domain}/")
    queue: list[str] = []
    if home:
        html, final = home
        host = urlsplit(final).netloc
        lp = _Links()
        try:
            lp.feed(html)
        except Exception:
            pass
        links = [urljoin(final, h).split("#")[0] for h, t in lp.links
                 if h and not h.startswith(("mailto:", "tel:", "javascript:"))
                 and any(v in (h + " " + t).casefold() for v in VOCAB)]
        links = [u for u in links if dc.registrable_domain(urlsplit(u).hostname or "") == domain]
        sm = fetch(f"https://{host}/sitemap.xml")
        sm_links = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm[0] if sm else "")
                    if any(v in u.casefold() for v in VOCAB) and not u.endswith(".xml")][:15]
        queue = links + sm_links + [f"https://{host}{g}" for g in GUESS]
    for u in dict.fromkeys(queue):
        if len(texts) >= max_pages:
            break
        fetch(u)
    got["pages"] = list(texts)
    for url, text in texts.items():
        for k, v in extract(text, url, domain).items():
            got["fields"].setdefault(k, []).extend(v)
    got["text_chars"] = sum(len(t) for t in texts.values())
    return got


if __name__ == "__main__":
    print(json.dumps([research(d) for d in sys.argv[1:]], ensure_ascii=False, indent=1))
