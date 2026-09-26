"""Company compliance profile: every field verified on its own, never by association.

Identifying the operator of a domain (domaincheck.resolve) and knowing that its phone,
e-mail or address are right are different questions. Each field here carries its own
value, status and sources, and the domain-resolution confidence is left untouched.

    status   verified      an authoritative source for THAT field confirms it
                           (Commercial Register: identity, seat, registered contacts;
                            VIES: the VAT number and its status)
             corroborated  confirmed by an official source that is not authoritative for
                           the field (VIES for name or seat), or by two independent
                           first-party sources that agree
             published     the company states it in one first-party source
             conflicting   sources disagree; every value is kept with its sources
             not_found     the sources that could hold it were read; it is not there
             unavailable   those sources could not be read (site blocked, VIES down,
                           register not connected) - "not checked", not "missing"

Values are normalised for display only; every source keeps the raw text it came from.
"""
from __future__ import annotations

import re

from shopwatch.registry import name_matches

FIELDS = ("eik", "legal_name", "legal_form", "registered_address", "phone", "email",
          "vat_number", "vat_status", "domain_association")

# Which source is authoritative (-> verified) and which merely corroborates, per field.
AUTHORITATIVE = {"eik": {"registry"}, "legal_name": {"registry"}, "legal_form": {"registry"},
                 "registered_address": {"registry"}, "phone": {"registry"},
                 "email": {"registry"}, "vat_number": {"vies"}, "vat_status": {"vies"},
                 "domain_association": {"registry", "nap_register"}}
OFFICIAL_CORROBORATION = {"eik": {"vies", "nap_register"}, "legal_name": {"vies", "nap_register"},
                          "legal_form": {"vies"},
                          "registered_address": {"vies"}}
AUTHORITY = ("registry", "vies", "nap_register", "company_document", "company_site",
             "company_archive", "public_source")
SOURCE_LABEL = {"registry": "Commercial Register", "vies": "VIES (EU VAT registry)",
                "nap_register": "NAP public e-shop list",
                "company_document": "Company legal document", "company_site": "Company website",
                "company_archive": "Company website (archived)",
                "public_source": "Independent public source"}
STATUSES = ("verified", "corroborated", "published", "conflicting", "ai_assisted",
            "ai_assisted_unretrieved", "not_independently_verified", "not_found", "unavailable")


def coverage_of(f: dict) -> str:
    """Tooling coverage, for development and provenance: who found it, if anyone."""
    if f["status"] == "conflicting":
        return "conflicting"
    if f.get("value") is not None:
        return "found_by_ai_fallback" if f["status"].startswith("ai_assisted") else \
            "found_by_deterministic_tooling"
    return "unavailable_to_deterministic_tooling" if f["status"] == "unavailable" else \
        "not_found_by_deterministic_tooling"


def summarise(fields: dict) -> dict:
    """Counts derived from field states only - nothing here is estimated."""
    count = lambda st: sum(1 for f in fields.values() if f["status"] == st)
    cov: dict = {}
    for f in fields.values():
        cov[f.get("coverage") or coverage_of(f)] = cov.get(f.get("coverage") or coverage_of(f), 0) + 1
    return {"fields": len(fields),
            "found": sum(1 for f in fields.values() if f.get("value") is not None),
            **{st: count(st) for st in STATUSES}, "coverage": cov}
MULTI_VALUED = {"phone", "email"}      # a company may publish several; that is no conflict


# --------------------------------------------------------------------------
# display normalisation (never applied to the stored raw value)
# --------------------------------------------------------------------------

_QUOTES = "„“”\"«»‘’`"
_FORMS = ("ЕООД", "ЕАД", "ООД", "АД", "ЕТ", "КДА", "КД", "СД")


def display_name(raw: str | None) -> str | None:
    """'“Наспърс Класифайдс България” ЕООД', 'НАСПЪРС ... - ЕООД' and the glued
    'НаспърсКласифайдсБългарияЕООД' all display as 'Наспърс Класифайдс България ЕООД'."""
    if not raw:
        return None
    s = re.sub(r"[" + _QUOTES + r"]", " ", raw)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    core = s
    for f in _FORMS:
        if re.search(rf"(?<![А-ЯA-Z]){f}$", s):
            core = s[: -len(f)].rstrip(" ,-–")
            if " " not in core and len(re.findall(r"[а-я][А-Я]", core)) >= 1:
                core = re.sub(r"(?<=[а-яa-z])(?=[А-ЯA-Z])", " ", core)     # glued PDF text
            s = f"{core} {f}"
            break
    else:
        if " " not in s and len(re.findall(r"[а-я][А-Я]", s)) >= 2:
            s = re.sub(r"(?<=[а-яa-z])(?=[А-ЯA-Z])", " ", s)
            m = re.search(r"(?<=[а-я])(" + "|".join(_FORMS) + r")$", s)
            if m:
                s = s[: m.start()] + " " + m.group(1)
    return re.sub(r"\s+", " ", s).strip()


_ABBR = r"(гр|ул|бул|обл|ж\.к|жк|ет|ап|бл|вх|пл|кв|р-н|с|п\.к|№)"


def display_address(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"[" + _QUOTES + r"]", "", raw)
    s = re.sub(rf"(?i)(?<![\wА-я]){_ABBR}\.(?=\S)", lambda m: m.group(0) + " ", s)
    s = re.sub(r"№\s*", "№ ", s)
    s = re.sub(r"\s*,\s*", ", ", s)
    # VIES writes places in capitals ("обл.СОФИЯ, гр.СОФИЯ"): title-case long words only
    s = re.sub(r"\b[А-Я]{4,}\b", lambda m: m.group(0).capitalize(), s)
    return re.sub(r"\s+", " ", s).strip(" ,")


_ADDR_STOP = {"обл", "област", "гр", "град", "ул", "улица", "бул", "булевард", "ет", "етаж",
              "ап", "бл", "вх", "жк", "ж", "к", "район", "р", "н", "община", "общ", "пк",
              "п", "с", "no", "str", "българия", "bulgaria", "bg", "кв", "пл", "и", "на",
              "столица", "столична"}


def plausible_address(raw: str | None) -> bool:
    """A place, not a fragment: a settlement or street marker AND a number. The page
    extractor sometimes returns "и адрес на" or "гр. София, бул. Проф" (cut short)."""
    a = raw or ""
    return (10 <= len(a) <= 300 and bool(re.search(r"\d", a)) and
            bool(re.search(r"(?i)(гр\.|град|ул\.|бул\.|ж\.?к\.?|кв\.|с\.|обл|област|"
                           r"шосе|път|площад|пл\.|зона)", a)))


def plausible_name(raw: str | None) -> bool:
    """A company name carries a legal form; a trailing ЕТ after digits is a product line."""
    shown = display_name(raw)             # glued PDF text shows its legal form only once spaced
    if not shown or len(shown) > 90 or not _form_of(shown):
        return False
    return not (re.search(r"(?<![А-ЯA-Z])ЕТ\W*$", shown.upper()) and re.search(r"\d", shown))


def clean_address(raw: str | None) -> str | None:
    """Drop the label a legal text puts in front: "и адрес на управление: гр. София..."."""
    if not raw:
        return raw
    return re.sub(r"(?is)^.*?(?:седалище\s+и\s+)?адрес(?:\s+на\s+управление)?\s*[:\-–]\s*",
                  "", raw).strip(" ,;") or raw


def clean_name(raw: str | None) -> str | None:
    """Drop label debris the page extractor leaves in front: "та: Жар ЕООД" -> "Жар ЕООД"."""
    if not raw:
        return raw
    return raw.rsplit(":", 1)[-1].strip() if ":" in raw else raw


def address_key(raw: str | None) -> frozenset:
    words = re.findall(r"[а-яa-z]+|\d+", (raw or "").lower())
    return frozenset(w for w in words if w not in _ADDR_STOP and not re.fullmatch(r"\d{4}", w))


def addresses_agree(a: str | None, b: str | None) -> bool:
    """Same place, however punctuated: the smaller token set sits inside the larger.
    VIES 'ул. Ангел Кънчев №1 ет.3 обл.СОФИЯ, гр.СОФИЯ 1000' = 'гр. София, ул. “Ангел
    Кънчев” №1, ет. 3'."""
    ka, kb = address_key(a), address_key(b)
    if not ka or not kb:
        return False
    small, big = sorted((ka, kb), key=len)
    # A shorter address that sits wholly inside the longer one ("гр. Божурище (ПК 2227)"
    # inside the full seat) is consistent, not contradicting.
    return small <= big or len(small & big) >= max(2, int(len(small) * 0.8))


def phone_key(raw: str | None) -> str | None:
    """E.164 digits for a Bulgarian number, else None. Short codes (*9090) are not
    phone numbers in this sense and are dropped rather than guessed."""
    d = re.sub(r"[^\d+]", "", raw or "")
    if d.startswith("00359"):
        d = "+359" + d[5:]
    elif d.startswith("359") and len(d) >= 11:
        d = "+" + d
    elif d.startswith("0") and 8 <= len(d) <= 10:
        d = "+359" + d[1:]
    if not d.startswith("+359"):
        return None
    national = d[4:]
    if national.startswith("0"):              # "+359 0700 ..." keeps a trunk 0 by mistake
        national = national[1:]
        d = "+359" + national
    return d if 7 <= len(national) <= 9 and national.isdigit() else None


def display_phone(raw: str | None) -> str | None:
    k = phone_key(raw)
    if not k:
        return None
    n = k[4:]
    if n.startswith("2"):                                         # Sofia
        parts = [n[:1], n[1:4], n[4:]]
    elif n[:3] in ("700", "800", "900"):                          # national-rate numbers
        parts = [n[:3], n[3:5], n[5:]]
    elif n[:2] in ("87", "88", "89", "98", "99", "43"):          # mobile
        parts = [n[:2], n[2:5], n[5:]]
    else:                                                         # other area codes
        parts = [n[:2], n[2:5], n[5:]]
    return "+359 " + " ".join(p for p in parts if p)


def email_key(raw: str | None) -> str | None:
    s = (raw or "").strip().lower().removeprefix("mailto:").split("?")[0].strip(" .,;:<>()[]")
    if not re.fullmatch(r"[a-z0-9._%+\-]{1,64}@[a-z0-9.\-]{1,253}\.[a-z]{2,10}", s):
        return None
    if s.rsplit(".", 1)[1] in ("png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"):
        return None
    return s


def vat_key(raw: str | None) -> str | None:
    m = re.fullmatch(r"(BG)?\s*(\d{9}|\d{13})", re.sub(r"[\s.\-]", "", (raw or "").upper()))
    return f"BG{m.group(2)}" if m else None


# --------------------------------------------------------------------------
# field builder
# --------------------------------------------------------------------------

def source(kind: str, raw, *, url=None, retrieved_at=None, archived=None, note=None) -> dict:
    return {"type": kind, "label": SOURCE_LABEL[kind], "raw": raw, "url": url,
            "retrieved_at": retrieved_at, "archived": archived, "note": note}


def _independent(sources: list[dict]) -> int:
    """Distinct (kind, host) pairs: two pages of one website are one voice."""
    def host(u):
        m = re.match(r"https?://([^/]+)", u or "")
        return m.group(1).lower().removeprefix("www.") if m else None
    return len({(s["type"] if s["type"] in ("registry", "vies") else "first_party", host(s["url"]),
                 bool(s.get("archived"))) for s in sources})


def build_field(name: str, cands: list[tuple[str, str, dict]], *, same=None,
                missing: dict | None = None) -> dict:
    """cands: (display_value, comparison_key, source). `same(a, b)` compares keys."""
    same = same or (lambda a, b: a == b)
    groups: list[dict] = []
    for disp, key, src in cands:
        if not key:
            continue
        g = next((g for g in groups if same(g["key"], key)), None)
        if g is None:
            g = {"key": key, "value": disp, "sources": []}
            groups.append(g)
        elif AUTHORITY.index(src["type"]) < min(AUTHORITY.index(x["type"]) for x in g["sources"]):
            g["value"] = disp              # agreeing sources: show the authoritative wording
        g["sources"].append(src)
    if not groups:
        return {"value": None, "status": (missing or {}).get("status", "not_found"),
                "reason": (missing or {}).get("reason"), "note": (missing or {}).get("note"),
                "params": (missing or {}).get("params", {}), "sources": [], "values": []}

    rank = lambda g: min(AUTHORITY.index(s["type"]) for s in g["sources"])
    groups.sort(key=lambda g: (rank(g), -len(g["sources"])))
    primary = groups[0]
    kinds = {s["type"] for s in primary["sources"]}

    if name in MULTI_VALUED:
        # Several numbers on one site are several numbers. A conflict is two independent
        # sources that share none of them.
        by_origin: dict = {}
        for g in groups:
            for s in g["sources"]:
                by_origin.setdefault((s["type"], s.get("archived") is not None,
                                      re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", s["url"] or "")),
                                     set()).add(g["key"])
        origins = list(by_origin.values())
        shared = set.intersection(*origins) if len(origins) > 1 else set()
        conflict = len(origins) > 1 and not shared
        if shared:
            primary = next(g for g in groups if g["key"] in shared)
            kinds = {s["type"] for s in primary["sources"]}
    else:
        conflict = len(groups) > 1

    if kinds & AUTHORITATIVE.get(name, set()):
        status = "verified"
    elif kinds & OFFICIAL_CORROBORATION.get(name, set()) or _independent(primary["sources"]) > 1:
        status = "corroborated"
    else:
        status = "published"
    if conflict:
        status = "conflicting"
    return {"value": primary["value"], "status": status,
            "sources": primary["sources"],
            "values": [{"value": g["value"], "sources": g["sources"]} for g in groups],
            "note": None}


def build(domain: str, eik: str | None, pages: list[dict], off: dict | None, reg: dict | None,
          reg_status: str, site_status: str, resolution: dict) -> dict:
    """The compliance profile for the resolved company.

    pages     first-party pages/documents as the crawler recorded them (live and archived)
    off       VIES answer for BG<eik> (or None when no ЕИК)
    reg       Commercial Register record for the ЕИК (status confirmed / not_found) or None
    """
    off, reg = off or {}, reg or {}
    reg_ok, off_ok = reg.get("status") == "confirmed", off.get("status") == "confirmed"
    reg_src = lambda raw: source("registry", raw, retrieved_at=reg.get("received_at"))
    vies_src = lambda raw: source("vies", raw, url=off.get("url"),
                                  retrieved_at=off.get("retrieved_at"))

    def page_src(p, raw):
        kind = "company_archive" if p.get("archived") else \
            "company_document" if p.get("legal_doc") else "company_site"
        return source(kind, raw, url=p["url"], retrieved_at=p.get("retrieved_at"),
                      archived=p.get("archived"))

    mine = list(pages)                     # the crawler only records the domain's own hosts
    read_main = [p for p in mine if p.get("host", "").removeprefix("www.") == domain]
    blocked = site_status in ("blocked", "temporarily_unavailable", "failed")
    no_pages = {"status": "unavailable", "reason": "site_blocked" if blocked else "no_pages",
                "note": "the target site blocks automated retrieval and no first-party copy "
                        "could be read" if blocked else "no first-party page could be read"}
    contacts_missing = no_pages if not mine else {
        "status": "not_found", "reason": "checked",
        "params": {"n": len(mine), "main_blocked": bool(blocked and not read_main)},
        "note": f"not published in the {len(mine)} first-party page(s)/document(s) read"
                + ("; the site itself blocks automated retrieval" if blocked and not read_main
                   else "")}

    # ---- ЕИК, name, form -----------------------------------------------------------
    eik_c, name_c, form_c, addr_c, vat_c = [], [], [], [], []
    for p in mine:
        f = p.get("found") or {}
        if eik and re.sub(r"\D", "", f.get("eik") or "") == eik:
            eik_c.append((eik, eik, page_src(p, f["eik"])))
        for m in p.get("mentions") or []:
            if m.get("eik") == eik and eik:
                eik_c.append((eik, eik, page_src(p, m["snippet"])))
                name_c.append((display_name(m["name"]), m["name"], page_src(p, m["name"])))
                if m.get("address"):
                    addr_c.append((display_address(m["address"]), m["address"],
                                   page_src(p, m["address"])))
        if eik and re.sub(r"\D", "", f.get("eik") or "") == eik:
            if f.get("company_name"):
                name_c.append((display_name(f["company_name"]), f["company_name"],
                               page_src(p, f["company_name"])))
            if f.get("address"):
                addr_c.append((display_address(f["address"]), f["address"],
                               page_src(p, f["address"])))
        if f.get("vat_number") and vat_key(f["vat_number"]):
            vat_c.append((vat_key(f["vat_number"]), vat_key(f["vat_number"]),
                          page_src(p, f["vat_number"])))
    if reg_ok:
        eik_c.append((eik, eik, reg_src(eik)))
        if reg.get("name"):
            name_c.append((display_name(reg["name"]), reg["name"], reg_src(reg["name"])))
        if reg.get("legal_form"):
            form_c.append((reg["legal_form"], reg["legal_form"], reg_src(reg["legal_form"])))
        if reg.get("registered_address"):
            addr_c.append((display_address(reg["registered_address"]), reg["registered_address"],
                           reg_src(reg["registered_address"])))
    if off_ok:
        eik_c.append((eik, eik, vies_src(f"BG{eik}")))
        if off.get("name"):
            name_c.append((display_name(off["name"]), off["name"], vies_src(off["name"])))
        if off.get("address"):
            addr_c.append((display_address(off["address"]), off["address"],
                           vies_src(off["address"])))
    name_c = [(display_name(clean_name(raw)), clean_name(raw), src) for _d, raw, src in name_c
              if plausible_name(clean_name(raw))]
    addr_c = [(display_address(clean_address(raw)), clean_address(raw), src)
              for _d, raw, src in addr_c if plausible_address(clean_address(raw))]
    # The name the operator declared to NAP - often without its legal form ("ЧУНЧЕ"), so
    # it skips the legal-form test: it corroborates the site's name, and is the only name
    # there is for a trader outside VIES whose site names nobody.
    for ev in resolution.get("evidence") or []:
        if ev.get("nap") and ev.get("declared_name") and eik and eik in (ev.get("value") or ""):
            name_c.append((display_name(ev["declared_name"]), ev["declared_name"],
                           source("nap_register", ev["declared_name"], url=ev.get("source_url"),
                                  retrieved_at=ev.get("retrieved_at"))))
    for disp, raw, src in list(name_c):
        form = _form_of(raw)
        if form and src["type"] != "registry":
            form_c.append((form, form, src))

    name_same = lambda a, b: bool(name_matches(a, b)) or _glued(a) == _glued(b)
    # The same company named with a DIFFERENT ЕИК in a first-party text is a real
    # contradiction (a courier or a sister company under another name is not).
    names_seen = [raw for _d, raw, _s in name_c]
    for p in mine:
        for m in p.get("mentions") or []:
            if m.get("eik") and eik and m["eik"] != eik and \
                    any(name_same(m["name"], n) for n in names_seen):
                eik_c.append((m["eik"], m["eik"], page_src(p, m["snippet"])))
    unofficial = {"status": "unavailable", "reason": "registry_unconfigured",
                  "note": "the Commercial Register is not connected"} if reg_status != "available" \
        else {"status": "not_found", "reason": "registry_no_record"}
    # NAP's e-shop list: the operator declared this shop under this ЕИК (corroboration).
    for ev in resolution.get("evidence") or []:
        if ev.get("nap") and eik and eik in (ev.get("value") or ""):
            eik_c.append((eik, eik, source("nap_register", ev.get("value"),
                                           url=ev.get("source_url"),
                                           retrieved_at=ev.get("retrieved_at"))))
    fields = {
        "eik": build_field("eik", eik_c, missing=unofficial),
        "legal_name": build_field("legal_name", name_c, same=name_same, missing=unofficial),
        "legal_form": build_field("legal_form", form_c, missing=unofficial),
        "registered_address": build_field("registered_address", addr_c,
                                          same=lambda a, b: addresses_agree(a, b),
                                          missing=unofficial),
    }

    # ---- phone / e-mail: first-party, cross-checked --------------------------------
    phone_c, email_c = [], []
    for p in mine:
        parties = {m["eik"] for m in p.get("mentions") or [] if m.get("eik")}
        own = next((m for m in p.get("mentions") or [] if m.get("eik") == eik), None)
        if len(parties) > 1:
            # A page naming several companies (definitions lists, partner pages): take
            # only the contacts written in the operator's own passage.
            got = {"phones": (own or {}).get("phones", []),
                   "emails": (own or {}).get("emails", [])}
        else:
            got = p.get("contacts") or {}
        for raw in got.get("phones", []):
            if phone_key(raw):
                phone_c.append((display_phone(raw), phone_key(raw), page_src(p, raw)))
        for raw in got.get("emails", []):
            if email_key(raw):
                email_c.append((email_key(raw), email_key(raw), page_src(p, raw)))
    if reg_ok and reg.get("corporate_phone") and phone_key(reg["corporate_phone"]):
        phone_c.append((display_phone(reg["corporate_phone"]), phone_key(reg["corporate_phone"]),
                        reg_src(reg["corporate_phone"])))
    if reg_ok and reg.get("corporate_email") and email_key(reg["corporate_email"]):
        email_c.append((email_key(reg["corporate_email"]), email_key(reg["corporate_email"]),
                        reg_src(reg["corporate_email"])))
    fields["phone"] = build_field("phone", phone_c, missing=contacts_missing)
    fields["email"] = build_field("email", email_c, missing=contacts_missing)

    # ---- VAT -------------------------------------------------------------------------
    vies_answered = off.get("status") in ("confirmed", "not_found")
    if eik and off_ok:
        vat_c.append((f"BG{eik}", f"BG{eik}", vies_src(f"BG{eik}")))
    fields["vat_number"] = build_field(
        "vat_number", vat_c,
        missing={"status": "not_found", "reason": "not_vat_registered",
                 "note": "not in VIES and not published by the company"} if vies_answered else
        {"status": "unavailable", "reason": "vies_unavailable" if eik else "no_eik"})
    if not eik:
        fields["vat_status"] = {"value": None, "status": "unavailable", "reason": "no_eik",
                                "sources": [], "values": [], "note": None}
    elif vies_answered:
        val = "valid" if off_ok else "not_registered"
        fields["vat_status"] = {"value": val, "status": "verified",
                                "sources": [vies_src("valid" if off_ok else "invalid")],
                                "values": [], "note": None}
    else:
        fields["vat_status"] = {"value": None, "status": "unavailable",
                                "reason": "vies_unavailable", "sources": [], "values": [],
                                "note": "VIES did not answer"}

    # ---- domain -> operator -------------------------------------------------------------
    assoc = []
    for ev in resolution.get("evidence") or []:
        if ev.get("nap"):
            src = source("nap_register", ev.get("value"), url=ev.get("source_url"),
                         retrieved_at=ev.get("retrieved_at"),
                         note=f"declared {ev.get('declared_at') or ''}".strip())
            assoc.append((domain, domain, src))
            continue
        if ev.get("tier") != "company_source":
            continue
        kind = "company_archive" if ev.get("archived") else \
            "company_document" if ev.get("legal_doc") else "company_site"
        assoc.append((domain, domain, source(kind, ev.get("snippet") or ev.get("value"),
                                             url=ev.get("source_url"),
                                             retrieved_at=ev.get("retrieved_at"),
                                             archived=ev.get("archived"),
                                             note="operator statement" if ev.get("operator")
                                             else "ЕИК published")))
    if reg_ok and reg.get("website") and domain in (reg["website"] or "").lower():
        assoc.append((domain, domain, reg_src(reg["website"])))
    fields["domain_association"] = build_field(
        "domain_association", assoc,
        missing={"status": "not_found", "reason": "no_first_party_link",
                 "note": "no first-party source links this domain to the company"})
    if fields["domain_association"]["value"]:
        fields["domain_association"]["value"] = domain

    # ---- identity + summary -------------------------------------------------------------
    identity = ("verified" if reg_ok else
                "corroborated" if off_ok and fields["legal_name"]["status"] != "conflicting"
                else "conflicting" if fields["legal_name"]["status"] == "conflicting" else
                "published" if eik or fields["legal_name"]["value"] else "unverified")
    for k, f in fields.items():
        f["coverage"] = coverage_of(f)
        # The disclosure-triage question, kept apart from "is it known at all": did the
        # shop itself publish this (live or archived), whatever official sources say?
        f["disclosed_by_site"] = any(s["type"].startswith("company_")
                                     for v in f.get("values") or [] for s in v["sources"])
    return {"domain": domain, "identity_status": identity, "fields": fields,
            "summary": summarise(fields)}


def _form_of(name: str | None) -> str | None:
    up = (name or "").upper()
    for f in _FORMS:
        if re.search(rf"(?<![А-ЯA-Z]){f}(?![А-ЯA-Z])", up):
            return f
    return None


def _glued(name: str | None) -> str:
    s = re.sub(r"[^\wа-я]|" + "|".join(_FORMS), "", (name or "").upper(), flags=re.IGNORECASE)
    return s


if __name__ == "__main__":        # self-check
    assert display_name("НаспърсКласифайдсБългарияЕООД") == "Наспърс Класифайдс България ЕООД"
    assert display_name("“Наспърс Класифайдс България” ЕООД") == "Наспърс Класифайдс България ЕООД"
    assert display_name("Наспърс Класифайдс България - ЕООД") == "Наспърс Класифайдс България ЕООД"
    assert display_phone("02 970 4400") == "+359 2 970 4400"
    assert display_phone("+359 888 123 456") == "+359 88 812 3456"
    assert phone_key("*9090") is None
    assert addresses_agree("ул. Ангел Кънчев №1 ет.3 обл.СОФИЯ, гр.СОФИЯ 1000",
                           "гр. София, ул. “Ангел Кънчев” №1, ет. 3")
    assert not addresses_agree("гр. София, бул. Витоша 15", "гр. Пловдив, ул. Шипка 1")
    assert display_address("ул. Ангел Кънчев №1 ет.3 обл.СОФИЯ, гр.СОФИЯ 1000") == \
        "ул. Ангел Кънчев № 1 ет. 3 обл. София, гр. София 1000"
    assert vat_key("bg 204 511 873") == "BG204511873"
    assert email_key("mailto:Office@Shop.bg?subject=x") == "office@shop.bg"
    print("profile self-check ok")
