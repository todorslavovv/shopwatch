"""Merchant disclosure extraction from shop HTML — offline only.

Uses only html.parser from the standard library. Finds identifying data
anchored to Bulgarian/English labels, either inline ("ЕИК: 123456789") or
split across adjacent elements (<span>ЕИК</span><span>123456789</span>),
plus two strictly-shaped unlabelled fallbacks (Bulgarian addresses and
company names with legal-form suffixes).

Safety: input is attacker-controlled. Never eval'd; all patterns are simple
bounded regexes (no nested quantifiers); input over 5 MB is rejected.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

MAX_HTML_BYTES = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# Label tables (matched case-insensitively via str.casefold() / re.IGNORECASE)
# ---------------------------------------------------------------------------

EIK_LABELS = (
    "еик/булстат",
    "е.и.к.",
    "булстат",
    "идентификационен номер",
    "еик",
    "uic",
)

VAT_LABELS = (
    "ин по зддс",
    "ддс номер",
    "vat number",
    "vat no",
    "vat",
)

ADDRESS_LABELS = (
    "адрес на управление",
    "адрес за кореспонденция",
    "седалище",
    "адрес",
    "address",
)

EMAIL_LABELS = (
    "ел. поща",
    "ел поща",
    "имейл",
    "e-mail",
    "email",
)

PHONE_LABELS = (
    "телефон",
    "тел",
    "phone",
)

COMPANY_LABELS = (
    "наименование",
    "дружество",
    "фирма",
    "company name",
    "company",
)

# ---------------------------------------------------------------------------
# Bounded value patterns (no nested quantifiers -> no catastrophic backtracking)
# ---------------------------------------------------------------------------

# A run of digits with small separators, e.g. "123 456 789", "123-456-789".
_TOKEN_STRIP = ".,:;-\u2013\u2014()[]\"'"


def _number_at_start(raw: str) -> tuple[str, bool] | None:
    """Leading number (optional BG prefix) from a raw label value.

    Consumes consecutive digit/separator tokens, so "123 456 786" works,
    but only up to the first comma/semicolon and stops at any token with
    other characters — trailing prose (years, phone numbers) can never
    glue onto the number. Returns (digits, had_bg_prefix).
    """
    first_seg = re.split(r"[,;]", raw.strip(), maxsplit=1)[0]
    tokens = [t.strip(_TOKEN_STRIP) for t in first_seg.split()]
    tokens = [t for t in tokens if t]
    i = 0
    had_bg = False
    while i < len(tokens) and not re.search(r"\d", tokens[i]):
        # A detached "BG" prefix (e.g. value split as "BG" | "123456789").
        if tokens[i].casefold().strip(_TOKEN_STRIP) == "bg":
            had_bg = True
        i += 1
    if i >= len(tokens):
        return None
    parts: list[str] = []
    for t in tokens[i : i + 4]:
        if not parts and t.casefold().startswith("bg"):
            had_bg = True
            t = t[2:]
        if not t or not re.fullmatch(r"[\d\-.]+", t):
            break
        parts.append(t)
        if len(_digits_only("".join(parts))) > 13:
            break
    if not parts:
        return None
    return _digits_only("".join(parts)), had_bg


def _clean_eik_candidate(raw: str) -> str | None:
    """Normalise a raw EIK value to digits, or None.

    A longer digit run (e.g. a phone/bank number) is skipped rather than
    truncated.
    """
    got = _number_at_start(raw)
    if not got:
        return None
    digits, _ = got
    if len(digits) == 9 or len(digits) == 13:
        return digits
    return None


def _clean_vat_candidate(raw: str) -> str | None:
    """Normalise a raw VAT value, preserving a leading BG prefix."""
    got = _number_at_start(raw)
    if not got:
        return None
    digits, had_bg = got
    if 9 <= len(digits) <= 13:
        return ("BG" + digits) if had_bg else digits
    return None


# ---------------------------------------------------------------------------
# Unlabelled but highly patterned lines (precision first: strict shapes only)
# ---------------------------------------------------------------------------

# Bulgarian legal-form suffixes (compared casefolded, whole-token only).
_LEGAL_FORMS = frozenset(
    ["еоод", "оод", "ад", "еад", "ет", "кд", "кда", "сд"]
)

_QUOTE_CHARS = "\"'«»„“”‘’`"

# Leading bullets/quotes stripped before shape tests.
_BULLETS = "\"'«»„“”‘’·•\\-–—*№ \t"

# Settlement marker + up to 3 name words + separator + thoroughfare marker.
# Unrolled (no nested quantifiers) and fully bounded.
_NAME_WORD = r"[а-яa-z][а-яa-z'\-]{0,29}"
_RE_UNLABELLED_ADDR = re.compile(
    r"(?:гр\.|град|с\.|село)"
    r"\s+" + _NAME_WORD
    + r"(?:\s+" + _NAME_WORD + r")?"
    + r"(?:\s+" + _NAME_WORD + r")?"
    + r"[\s,;]{1,10}"
    + r"(?:ул\.|бул\.|ж\.к\.|бл\.|пл\.|кв\.)"
)


def _unlabelled_address(chunk: str) -> str | None:
    """Accept a chunk as an address only if it OPENS with a settlement
    marker (гр./град/с./село) plus settlement name AND contains a
    thoroughfare marker (ул./бул./ж.к./бл./пл./кв.) right after.

    Prose merely mentioning a city ("Доставяме до гр. София ...") does not
    match: it neither starts with the marker nor carries a thoroughfare.
    """
    text = chunk.strip()
    if len(text) < 8 or len(text) > 300:
        return None
    low = text.casefold().lstrip(_BULLETS)
    if not _RE_UNLABELLED_ADDR.match(low):
        return None
    return text[:300]


def _unlabelled_company(chunk: str) -> str | None:
    """Accept a chunk as a company name only if its last whitespace-separated
    token (sentence punctuation stripped) EQUALS a legal-form suffix.

    A word merely containing one ("града", "ПРОДАД") does not match.
    Surrounding quotes are removed from the value; the caller records the
    raw line as evidence. Lines over 150 chars / 10 words are skipped:
    a sentence that happens to end in a suffix is not a name.
    """
    text = chunk.strip()
    if not text or len(text) > 150:
        return None
    words = text.split()
    if len(words) < 2 or len(words) > 10:
        return None
    raw_last = words[-1]
    core = raw_last.strip(_TOKEN_STRIP + _QUOTE_CHARS + "!?").strip(_QUOTE_CHARS)
    if core.casefold() not in _LEGAL_FORMS:
        return None
    if core.casefold() == "ет" and raw_last.rstrip(_QUOTE_CHARS).endswith("."):
        return None  # "ет." with a dot is almost certainly етаж (floor)
    value = re.sub(r"\s+", " ", "".join(
        ch for ch in text if ch not in _QUOTE_CHARS
    )).strip()
    return value or None


def _is_only_legal_form(value: str) -> bool:
    """Is this "name" nothing but a legal-form suffix?

    A labelled value such as "Фирма: ЕООД" yields "ЕООД", which is not a company
    name - and accepting it silently suppresses the NO_COMPANY finding, making a
    non-compliant shop look better than it is. The unlabelled path already requires
    at least two words; this applies the same floor to labelled values.
    """
    core = " ".join(value.split())
    core = "".join(ch for ch in core if ch not in _QUOTE_CHARS).strip(" .,;:-")
    return core.casefold() in _LEGAL_FORMS


_RE_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,10}"
)

# Phone: optional +359/00359/0 prefix then 6-12 more digits with separators.
_RE_PHONE = re.compile(
    r"(\+?359|0)?[\s\-/()]{0,3}\d[\d\s\-/()]{4,18}\d"
)

_RE_MAILTO = re.compile(r"^mailto:\s*([^?]+)", re.IGNORECASE)
_RE_TEL = re.compile(r"^(?:tel|callto):\s*(.+)$", re.IGNORECASE)


def _label_alternation(labels: tuple[str, ...]) -> str:
    # Longest labels first so "еик/булстат" wins over "еик".
    ordered = sorted(labels, key=len, reverse=True)
    return "|".join(re.escape(l) for l in ordered)


def _inline_search(chunk: str, labels: tuple[str, ...]) -> str | None:
    """Return the value after a "label ... value" occurrence in chunk.

    Finds the earliest label occurrence; when several labels match at the
    same position the LONGEST wins (so "адрес на управление" is never read
    as label "адрес" + value "на управление"). The value must be non-empty
    and may follow the label after :, -, or whitespace. Returns None when
    the chunk holds a bare label with no value (handled by pass 2).
    """
    # NOTE: positions are located on the casefolded string and mapped back
    # onto the original. This is exact because all labels are Cyrillic/Latin,
    # whose casefold is length-preserving.
    low = chunk.casefold()
    best: tuple[int, str] | None = None
    for lab in labels:
        folded = lab.casefold()
        pos = low.find(folded)
        if pos == -1:
            continue
        if best is None or pos < best[0] or (pos == best[0] and len(lab) > len(best[1])):
            best = (pos, lab)
    if best is None:
        return None
    pos, lab = best
    rest = chunk[pos + len(lab):]
    m = re.match(r"\s*[:\-–—]{0,3}\s*(.+?)\s*$", rest, re.DOTALL)
    if not m:
        return None
    return m.group(1)


class _TextCollector(HTMLParser):
    """Collect visible text nodes in document order, plus mailto:/tel: links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._buf: list[str] = []
        self.mailtos: list[str] = []
        self.tels: list[str] = []
        self._skip = 0  # depth inside script/style

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        if folded in ("script", "style"):
            self._skip += 1
            return
        if folded == "a":
            for name, val in attrs:
                if val is None:
                    continue
                if name.casefold() == "href":
                    m = _RE_MAILTO.match(val.strip())
                    if m:
                        self.mailtos.append(m.group(1).strip())
                        continue
                    t = _RE_TEL.match(val.strip())
                    if t:
                        self.tels.append(t.group(1).strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in ("script", "style") and self._skip:
            self._skip -= 1

    def _flush(self) -> None:
        text = "".join(self._buf).strip()
        if text:
            # Collapse internal whitespace; keep one chunk per block.
            text = re.sub(r"\s+", " ", text)
            if text:
                self.chunks.append(text)
        self._buf = []

    def handle_endtag_block(self) -> None:
        self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        # Block boundaries flush so adjacent elements stay separate chunks.
        if "\n" in data or len(data) > 400:
            if data.strip():
                self._buf.append(data)
            self._flush()
            return
        self._buf.append(data)

    def close(self) -> None:
        self._flush()
        super().close()


_BLOCK_TAGS = frozenset(
    "p div span li td th tr dt dd h1 h2 h3 h4 h5 footer header section article aside br hr".split()
)


class _BlockSplitter(_TextCollector):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if tag.casefold() in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        super().handle_endtag(tag)
        if tag.casefold() in _BLOCK_TAGS:
            self._flush()


def _snippet(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s)


def _is_label_only(chunk: str, labels: tuple[str, ...]) -> bool:
    folded = chunk.casefold().strip()
    for label in labels:
        lab = label.casefold()
        rest = folded[len(lab):] if folded.startswith(lab) else None
        if rest is not None and re.fullmatch(r"\s*[:\-–—]{0,3}\s*", rest):
            return True
    return False


def extract_merchant(html: str) -> dict:
    """Extract merchant identity fields from shop HTML.

    Returns a dict with keys eik, company_name, address, email, phone,
    vat_number (extracted string or None each) plus "evidence": a dict
    mapping each found field to the surrounding snippet (max 200 chars).

    Raises:
        TypeError: if html is not a string.
        ValueError: if the input exceeds 5 MB.
    """
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    if len(html.encode("utf-8", errors="ignore")) > MAX_HTML_BYTES:
        raise ValueError(
            f"HTML input exceeds {MAX_HTML_BYTES} bytes; refusing to parse"
        )

    parser = _BlockSplitter()
    parser.feed(html)
    parser.close()
    chunks = [c for c in parser.chunks if c]

    result: dict = {
        "eik": None,
        "company_name": None,
        "address": None,
        "email": None,
        "phone": None,
        "vat_number": None,
        "evidence": {},
    }

    def set_field(field: str, value: str | None, evidence: str) -> None:
        if value and result[field] is None:
            result[field] = value
            result["evidence"][field] = _snippet(evidence)

    # -- pass 1: inline "label ... value" inside a single chunk -------------
    for chunk in chunks:
        val = _inline_search(chunk, EIK_LABELS)
        if val and result["eik"] is None:
            cand = _clean_eik_candidate(val)
            if cand:
                set_field("eik", cand, chunk)
        val = _inline_search(chunk, VAT_LABELS)
        if val and result["vat_number"] is None:
            cand = _clean_vat_candidate(val)
            if cand:
                set_field("vat_number", cand, chunk)
        val = _inline_search(chunk, ADDRESS_LABELS)
        if val and result["address"] is None:
            addr = val.strip().rstrip(".,;")[:300]
            if len(addr) >= 3:
                set_field("address", addr, chunk)
        val = _inline_search(chunk, EMAIL_LABELS)
        if val and result["email"] is None:
            em = _RE_EMAIL.search(val[:200])
            if em:
                set_field("email", em.group(0), chunk)
        val = _inline_search(chunk, PHONE_LABELS)
        if val and result["phone"] is None:
            ph = _RE_PHONE.search(val[:60])
            if ph:
                digits = _digits_only(ph.group(0))
                if 6 <= len(digits) <= 15:
                    set_field("phone", ph.group(0).strip(), chunk)
        val = _inline_search(chunk, COMPANY_LABELS)
        if val and result["company_name"] is None:
            name = val.strip().rstrip(".,;")[:300]
            # Avoid swallowing a trailing EIK/address glued in one line:
            # cut at a following known label if present.
            cut = re.split(
                r"\s+(?:еик|булстат|uic|адрес|седалище|телефон|email|phone)\b",
                name,
                flags=re.IGNORECASE,
            )
            name = cut[0].strip().rstrip(",:-\u2013\u2014") if cut else name
            if len(name) >= 2:
                if not _is_only_legal_form(name):
                    set_field("company_name", name, chunk)

    # -- pass 2: label in one element, value in the next --------------------
    label_tables: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("eik", EIK_LABELS),
        ("vat_number", VAT_LABELS),
        ("address", ADDRESS_LABELS),
        ("email", EMAIL_LABELS),
        ("phone", PHONE_LABELS),
        ("company_name", COMPANY_LABELS),
    )
    for i, chunk in enumerate(chunks):
        if i + 1 >= len(chunks):
            break
        nxt = chunks[i + 1].strip()
        # Text of the next few elements concatenated: a labelled value may
        # be fragmented across inline tags (<span>100</span><span>000001</span>).
        # Token parsing still stops at the first non-numeric token, so this
        # cannot glue trailing prose onto the number.
        following = " ".join(chunks[i + 1 : i + 4]).strip()
        if not nxt:
            continue
        for field, labels in label_tables:
            if result[field] is not None:
                continue
            if field == "eik":
                cleaner = _clean_eik_candidate
            elif field == "vat_number":
                cleaner = _clean_vat_candidate
            else:
                cleaner = None
            if cleaner is not None:
                if _is_label_only(chunk, labels):
                    cand = cleaner(nxt[:60])
                    if cand:
                        set_field(field, cand, chunk + " | " + nxt)
                    elif following:
                        # M2 defect 1: value split across nested tags.
                        cand = cleaner(following[:120])
                        if cand:
                            set_field(field, cand, chunk + " | " + following)
                    break
                val = _inline_search(chunk, labels)
                if val and cleaner(val) is None and following:
                    # Label with an unparseable inline remainder, e.g. the
                    # first fragment of a split value: extend with the
                    # following elements before giving up.
                    cand = cleaner((val + " " + following)[:150])
                    if cand:
                        set_field(field, cand, chunk + " | " + following)
                continue
            if _is_label_only(chunk, labels):
                ev = chunk + " | " + nxt
                if field == "eik":
                    cand = _clean_eik_candidate(nxt[:60])
                    if cand:
                        set_field(field, cand, ev)
                elif field == "vat_number":
                    cand = _clean_vat_candidate(nxt[:60])
                    if cand:
                        set_field(field, cand, ev)
                elif field == "email":
                    em = _RE_EMAIL.search(nxt[:200])
                    if em:
                        set_field(field, em.group(0), ev)
                elif field == "phone":
                    ph = _RE_PHONE.search(nxt[:60])
                    if ph and 6 <= len(_digits_only(ph.group(0))) <= 15:
                        set_field(field, ph.group(0).strip(), ev)
                else:
                    if len(nxt) >= 2:
                        set_field(field, nxt[:300], ev)
                break

    # -- pass 3: fallbacks that need no label --------------------------------
    # A bare email address anywhere is distinctive enough to accept.
    if result["email"] is None:
        for chunk in chunks:
            em = _RE_EMAIL.search(chunk)
            if em:
                set_field("email", em.group(0), chunk)
                break
    if result["email"] is None and parser.mailtos:
        for m in parser.mailtos:
            em = _RE_EMAIL.search(m[:200])
            if em:
                set_field("email", em.group(0), "mailto:" + m)
                break
    if result["phone"] is None and parser.tels:
        for t in parser.tels:
            if 6 <= len(_digits_only(t[:60])) <= 15:
                set_field("phone", t.strip()[:60], "tel:" + t)
                break

    # -- pass 4: unlabelled but highly patterned Bulgarian lines -------------
    # Precision first: strict shape tests, fill only missing fields, and
    # never EIK (Decision 4: no bare digit run is an EIK without a label).
    if result["address"] is None:
        for chunk in chunks:
            addr = _unlabelled_address(chunk)
            if addr:
                set_field("address", addr, chunk)
                break
    if result["company_name"] is None:
        for chunk in chunks:
            name = _unlabelled_company(chunk)
            if name:
                # Value has quotes stripped; evidence keeps the raw line.
                if not _is_only_legal_form(name):
                    set_field("company_name", name, chunk)
                break

    return result
