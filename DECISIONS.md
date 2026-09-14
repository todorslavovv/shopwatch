# DECISIONS.md

1. **EIK separators**: normalisation strips whitespace plus `- . / : , ; ( ) [ ]`.
   Letters are NOT separators — anything alphabetic remaining makes the value
   invalid (e.g. `"AB123"` is invalid). Rationale: simpler and stricter; a
   `BG` VAT prefix belongs to `vat_number` handling, not `validate_eik`.
2. **`kind` on checksum failure**: `kind` reflects normalised length (`"9"` /
   `"13"`) even when the checksum fails; `None` only when the length itself is
   wrong. Rationale: more useful for triage ("published a 9-digit-looking
   number that fails checksum").
3. **Non-string input** to `validate_eik` returns `{"valid": False, ...}`
   instead of raising. Rationale: validator used on scraped data; never crash
   the pipeline on junk.
4. **No global EIK scraping**: extraction only accepts digit runs near an
   EIK/БУЛСТАТ/UIC label (inline or adjacent element). A bare 9-digit number
   anywhere is NOT claimed as an EIK. Rationale: avoids false positives from
   phone numbers, prices, quantities.
4b. **Number parsing is token-based**: from a label value, consecutive
   digit/separator tokens are consumed (`123 456 786` works), stopping at the
   first comma/semicolon or non-numeric token. Trailing prose (e.g. a year
   `2024`) can never glue onto the number, and a phone-length run under an
   EIK label is rejected rather than truncated.
5. **Returned `eik` is digits-only** (normalised); `vat_number` preserves a
   leading `BG` prefix as `"BG"+digits`. Rationale: matches how these are
   conventionally stored/compared.
6. **`Тел` accepted as phone label** (common abbreviation of `Телефон`), and
   `Идентификационен номер` treated as an EIK label. Bare emails and
   `mailto:`/`tel:` links accepted without a visible label; address/company
   require a label. Rationale: emails/`tel:` links are distinctive; bare
   addresses/names are not.
7. **Case folding**: label matching uses `re.IGNORECASE` for inline patterns
   and explicit `str.casefold()` comparisons for adjacent-element detection.
8. **stdlib only** (`html.parser`): sufficient via block-level chunk splitting,
   so no BeautifulSoup/lxml dependency.
9. **Compliance weights/thresholds**: NO_EIK +50, EIK_INVALID +40, NO_ADDRESS
   +20, NO_CONTACT +20, NO_COMPANY/NO_EMAIL/NO_PHONE +10 each, NO_VAT +0
   (info). Verdicts: 0–19 ok, 20–49 suspect, 50+ noncompliant. A missing EIK
   alone always yields noncompliant. The score is a documented heuristic, not
   a probability.
10. **Oversize HTML** (> 5 MB by UTF-8 byte length) raises `ValueError`.
11. **No stubs**: everything implemented; nothing deferred.

## M2 (audit fixes, extractor only — eik.py untouched)

12. **Fragmented labelled values**: pass 2 still tries the single next chunk
    first (m1 behaviour preserved, so a following numeric chunk can never
    glue onto a complete value), and only then the next up-to-3 chunks joined
    with spaces. Token parsing still stops at the first non-numeric token and
    at commas/semicolons. Applied to EIK and VAT only (both numeric and
    label-anchored); address/company/email/phone keep single-chunk values.
    A label with an unparseable inline remainder (e.g. first fragment of a
    split value) is likewise extended with following chunks before giving up.
13. **Unlabelled address**: chunk must OPEN (modulo leading quotes/bullets)
    with гр./град/с./село + 1–3 name words AND a thoroughfare marker
    (ул./бул./ж.к./бл./пл./кв.) within the same tight span. Deliberately
    missed: 4+ word settlement names, mid-sentence addresses, marker-less
    lines. Whole chunk (<=300 chars) is returned as the address.
14. **Unlabelled company**: last whitespace token (sentence punctuation
    stripped) must EQUAL a legal form (ЕООД/ООД/АД/ЕАД/ЕТ/КД/КДА/СД); line
    capped at 150 chars / 10 words. Quotes are removed from the value but the
    raw line is kept as evidence. "ет." WITH a trailing dot is rejected as
    етаж (floor) — so a trader styled "…ЕТ." at sentence end is a deliberate
    miss. A lone suffix with no name is rejected (>= 2 words required).
15. **EIK stays label-anchored** (Decision 4 unchanged): unlabelled digit runs
    are never EIKs. New unlabelled heuristics are address/company only.
16. **Perf**: 20k nested tags cost 0.18s, all inside stdlib html.parser;
    extraction passes add ~0ms. New patterns are unrolled/bounded, no nested
    quantifiers.
