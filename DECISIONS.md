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

## Domain → company check (2026-09-23)

17. **The target site is one source, not the check.** A 401/403/429, a bot challenge
    (narrow markers: `cf_chl_opt`, "Just a moment...", Incapsula/PerimeterX/DDoS-Guard
    pages — NOT Cloudflare's ordinary injected `/cdn-cgi/challenge-platform/` script) or
    a robots.txt disallow marks the site `blocked`/`robots_disallowed`, and the check
    continues with DNS, RDAP, TLS and search. No retries with another identity.
18. **Honest User-Agent and robots.txt** for the domain check, unlike sealbox's evidence
    capture (which records what a visitor would see). technopolis.bg and ardes.bg
    disallow all non-search-engine crawlers, so they resolve only through search.
19. **Authority beats majority.** registry > official (VIES) > the site's own pages >
    the site's pages seen through a search index > third parties. Frequency is used only
    as a tie-break inside one tier (a courier's ЕИК in the terms appears on one page, the
    trader's on several). Two indistinguishable candidates → not identified.
20. **Confidence:** site-published ЕИК 0.90, own pages via search 0.60, third party
    0.45; +0.07 official confirmation, +0.02 name agreement, −0.15 name conflict.
    Identified at ≥ 0.50. DNS, RDAP and certificates never contribute: they identify
    infrastructure. A company name without an ЕИК is reported (0.60) but never matched to
    a registry record, since there is no name search without the Registry Agency.
21. **A company name only counts with a legal form.** The page extractor sometimes
    returns sentence fragments ("та"); matching those against VIES produced false name
    conflicts on zora.bg.
22. **VIES "not found" is never "inactive".** VIES holds VAT-registered traders only.
23. **TLS is read separately** through sealbox's guard (`resolve_guarded`, pinned IP):
    sealbox's `fetch()` returns `tls: None` for every page because http.client closes the
    socket before `_tls_info` reads it. That is a sealbox bug, reported, not fixed here.
24. **Time budget**, not only per-read timeouts: the site crawl gets 40 s wall-clock in a
    worker thread; a slower crawl is abandoned, and at most 4 abandoned crawls may linger
    before direct fetching is skipped.
25. **Stdlib only**, including the Public Suffix List (vendored, 2023-10, plus com.bg /
    org.bg / net.bg / edu.bg / gov.bg) and DNS (Cloudflare's DoH JSON API): the Deck has
    no pip.

## Domain → company, v2: the site is one source (2026-09-23)

26. **Reverse the dependency.** v1 effectively required the target site; v2 collects from
    every source and escalates (stage B) only while the operator's own sources have not
    named an ЕИК. "Not identified" is returned only after every configured source ran.
27. **Wildcard DNS.** olx.bg resolves every name to one CloudFront target, so "the name
    resolves" proves nothing. A random probe name gives the wildcard's fingerprint; a
    guessed host (help., developer., legal. …) counts only if it resolves elsewhere.
28. **PDFs are legal documents too.** A stdlib extractor (FlateDecode, ToUnicode CMaps,
    PDF 1.5 object streams, position-based line breaks) reads terms published as PDF.
    Zip-bomb capped at 32 MB inflated.
29. **Operator statements are grammatical, not proximity-based.** "администраторът на
    уебсайта X.bg - дружеството „Name“" (connector words only between role and name) or
    "„Name“ …, дружество - собственик на … X.bg" (up to the next company or defined
    term). technopolis.bg's terms define ТЕХНОПОЛИС then ПРАКТИКЕР back to back; the
    proximity rule gave the shop to the wrong company, the grammatical one gets it right.
30. **The Internet Archive for refusing hosts only**, with the snapshot date on every
    claim. Archived-only evidence without official confirmation is capped at MEDIUM.
31. **Confidence levels, not numbers.** HIGH needs the ЕИК or an operator statement in
    the operator's own document; MEDIUM covers own-domain search evidence plus VIES, two
    independent public sources, or a name without an ЕИК; LOW is a candidate, never an
    assertion. A name conflict with the official record caps HIGH at MEDIUM.
32. **Crawler hygiene.** Never follows login/account/auth/checkout URLs; never probes a
    host that refused it; guessed legal paths only on a host that answers.
33. **Registry adapter = format, not transport.** It parses DeedV2 as published and reads
    only allowlisted fields; a person's `Indent` (their ЕГН) is never read.

## Domain → company, v3: asynchronous jobs with real progress (2026-09-23)

34. **Jobs in process, polling over HTTP.** The checker already runs one small threaded
    server; a dict of jobs behind a lock, worker threads bounded by the existing
    semaphore, and polling cover the need without Redis/Celery or SSE plumbing through
    the site proxy. Jobs are lost on restart (the page says so and offers a new lookup).
35. **Progress comes from the resolver**, via an optional `progress(stage, code, **info)`
    callback at its real stage boundaries and from the crawler the moment a document
    names an ЕИК. Nothing advances on a timer; no percentage is shown because none can be
    justified. With no callback the resolver is byte-for-byte the same.
36. **Provisional is labelled provisional.** The early candidate is `resolve()` without
    official data; the page shows it as "possible operator - verification in progress".
37. **One job per domain at a time**: a double-click, or a second visitor asking about the
    same site, gets the running job. The button is also locked client-side.
38. **Cancellation is cooperative**: the job is final for the page at once; the resolver
    stops at its next stage boundary (it cannot be killed mid-fetch).
39. **Found while verifying the async flow:** the archive fallback kept digging after an
    archived document had named the ЕИК (its stop test ignored archived copies) - fixed,
    olx.bg from the Deck went from ~57 s to ~33 s; www-twin lookups are limited to the
    main domain; and a "not identified" answer caused by a temporarily unavailable source
    is no longer cached, so a retry can succeed.

## Company compliance profile (2026-09-23)

40. **Two questions, two answers.** `domain_resolution` says who operates the domain;
    `profile` says what is known about each compliance field. A missing phone never
    lowers the resolution confidence.
41. **Per-field authority** (profile.AUTHORITATIVE / OFFICIAL_CORROBORATION): the register
    verifies identity, seat and registered contacts; VIES verifies VAT and only
    corroborates name and seat; first-party sources publish, and two independent
    first-party sources that agree corroborate. Two pages of one website are one voice.
42. **Conflicts are kept, not resolved.** Single-valued fields with disagreeing sources
    are `conflicting`, every value listed with its sources; the most authoritative is
    shown first. Phones and e-mails are multi-valued: several numbers on one site are not
    a conflict; independent sources sharing none of them are.
43. **Attribution on multi-party pages.** Where a page names several companies with an
    ЕИК (technopolis.bg's definitions list), only contacts in the operator's own passage
    count - Практикер's phone is not Технополис's.
44. **Display normalisation is non-destructive**: names (glued PDF text re-spaced,
    quotes and " - " before the legal form removed), addresses, phones (E.164, grouped),
    e-mails, VAT; every source keeps `raw`. Extractor debris ("та: Жар ЕООД", "и адрес
    на") is cleaned or rejected before it can manufacture a conflict.
45. **Contacts step is bounded**: only if phone or e-mail is still missing, contact-tier
    links (3 live, 2 archived) plus /contact and /kontakti on a host that answers.

## Real-shop benchmark, NAP register, AI fallback (2026-09-23)

46. **Examples are real and re-checkable, or they go.** The eight synthetic worklist
    entries (all NXDOMAIN, no archive copy) were removed. The 20 replacements come from
    the NAP public online-shop list, used only as a discovery pool; each shortfall was
    confirmed by hand on the rendered pages and carries URL, excerpt and date
    (`benchmark/shops.json`). Wording is "observed disclosure shortfall", never fraud.
47. **The examples are also the benchmark.** A published value Shopwatch misses is a
    capability gap with a technical reason (`benchmark/gaps.json`), not a test failure;
    the first response is to fix the deterministic tooling. Counts come only from
    `benchmark/run.py` over a recorded observation.
48. **NAP online-shop register as an official domain → operator source.** It is the one
    public source that links a domain to an ЕИК. Only the ЕИК, shop entry and dates are
    kept; a natural person's declared name is not. Its web-address filter lower-cases the
    query but compares case-sensitively ("CHUNCHE-BG.COM"), so the case-insensitive
    shop-name filter is the fallback.
49. **A label is a word, and what follows it must look like the field.** Substring labels
    read "Quickview" as UIC and "потребител" as тел. A company name needs its legal form
    (the capitalised run before it is the name); an address needs a number and a street or
    settlement marker. Prose after "Дружеството" or "адреса за фактуриране" was hiding
    real NO_COMPANY / NO_ADDRESS shortfalls (9 of 20 addresses in the baseline).
50. **A BG VAT number discloses the ЕИК, and "ЕИК BG…" discloses the VAT number.**
    Scoring either as missing was a false alarm.
51. **Unlabelled values only with context.** "+359…" numbers; an address in a contact
    block (an e-mail or +359 phone within three elements); a company named where the text
    says it is the trader ("е собственост на", "Общи условия X ЕООД уведомява", "X ООД,
    ЕИК", "a part of X Ltd.") - never a courier in the delivery terms or the web
    agency's "Designed by … LTD".
52. **Different company names or legal forms across pages** are their own finding
    (COMPANY_INCONSISTENT): "Органик Бранд ЕООД" in the footer and "… ООД" in the privacy
    policy is an inconsistent disclosure whichever is right.
53. **Page budget by tier share** (2 contact/about, 2 terms, 1 privacy, then the rest): a
    product page called "About Dura" pushed the privacy policy - which names the data
    controller - out of five pages.
54. **AI fallback: designed, off, no provider shipped.** It may only locate a page; the
    page, fetched and read by Shopwatch, is the evidence. The benchmark did not need it
    (see the gap log): every miss had a deterministic cause.

## Correction pass: scope, evidence handling, benchmark accuracy (2026-09-24)

55. **Privacy by source, purpose and field - no blanket rules.** A mailbox is classified by
    context (`privacy.classify_email`): the shop's domain, a function mailbox or the shop's
    name -> corporate; any other mailbox the shop publishes -> its published contact (a
    Gmail address is not private for being Gmail); a public body or courier named in legal
    boilerplate -> not the shop's contact. Identifiers (ЕГН, ЛНЧ, the 10-digit BG VAT
    that is an ЕГН) stay forbidden whatever the source.
56. **People who act for the company are company information.** Names and roles from the
    Commercial Register (verified) and from the company's own pages ("представлявано от
    управителя ...", published) are shown with source and date. The unsigned LIA gate is
    replaced by an opt-out switch (`PEOPLE-OFF`); objections still apply per person.
57. **NAP declared name kept.** It is the trader's identity in an official register, and
    the only name there is for a trader outside VIES whose site names nobody. It
    corroborates the site's name (NAP drops the legal form: "ЧУНЧЕ").
58. **VAT non-disclosure is information, not a shortfall.** "VAT number not publicly
    disclosed" + "VAT registration: valid according to VIES", until an applicable legal
    provision is identified and documented for the merchant. The candidate (E-Commerce
    Act art. 4 / Directive 2000/31/EC art. 5(1)(g)) could not be checked against the
    official text in this pass.
59. **NAP and independent resolution reported separately.** NAP itself supplies the
    domain -> ЕИК link, so the benchmark also runs every check with the NAP list switched
    off (`check(nap_source=...)`) and reports both figures.
60. **AI source identified but not retrieved is shown, labelled.** Three outcomes: cited
    page read and value on it (independently verified from the cited source); cited page
    unreadable (shown: "source could not be independently retrieved by Shopwatch"); cited
    page read without the value, or no source (not independently verified, claim kept
    for audit). None of them outranks the register, VIES or a first-party page.
