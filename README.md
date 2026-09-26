# Shopwatch — merchant disclosure triage

A public-web triage tool for Bulgarian online shops. It answers two questions:

* **What does a shop disclose about its trader?** Company name, ЕИК, legal form,
  address, phone, e-mail, VAT number - and which of those are missing, malformed or
  inconsistent.
* **Which legal entity operates a domain?** From the site's own legal pages and
  documents, archived copies, the NAP public online-shop register and EU VIES.

> A missing, invalid or conflicting disclosure is a finding to investigate, not proof of
> fraud or of an established legal violation. Independent portfolio project; not an
> official tool of any authority.

## The problem

Bulgarian law expects an online trader to identify itself, and a buyer or an
investigator often needs to know who is really behind a shop. In practice that
information is scattered over terms pages, footers and PDFs, sometimes only in archived
copies, and sometimes absent or contradictory. Shopwatch collects what is publicly
stated, checks it against official sources where they are openly available, and shows
every value with the source it came from.

## Features

* **Domain → company check** - paste any address; the page shows, in place, which
  company operates the domain, with an explained confidence (HIGH / MEDIUM / LOW /
  INSUFFICIENT) and the decisive evidence. Runs as a job with real progress stages
  (`fetching → discovering → legal_sources → verifying → result`) and can be cancelled.
* **Company compliance profile** - once the operator is known, each field (ЕИК, legal
  name and form, registered address, phone, e-mail, VAT number and status, domain →
  operator) is checked on its own: `verified` / `corroborated` / `published` /
  `conflicting` / `not_found` / `unavailable`, with every source and the raw value.
* **Merchant disclosure triage** - extraction of the trader details a shop publishes,
  the official ЕИК/БУЛСТАТ checksum, and a list of observable shortfalls.
* **URL check form** - a simple form (`/check/`) that runs the disclosure triage on one
  shop URL.
* **Example findings** - 20 real Bulgarian shops with manually verified evidence (URL,
  excerpt, date), paginated 5 per page, generated from the benchmark below.
* **Bulgarian by default**, with an English switch.

## How it works

```
visitor ──► front server (public page, forwards two routes only)
               │   GET  /                 static triage page (built by web/build.py)
               │   POST /api/domain-check  ─┐
               │   GET  /check/?url=…      ─┤──► checker, 127.0.0.1:8778 (shopwatch.serve)
                                             │       rate limits · job queue · cache
                                             ▼
             A: target site (≤5 pages) · NAP online-shop register · DNS (DoH) · RDAP ·
                TLS certificate · Certificate Transparency · search (if configured)
             B: only while no ЕИК was found: related hosts and their legal pages, PDFs,
                conventional legal paths, Internet Archive copies of refusing hosts
             C: EU VIES · Commercial Register (adapter only, not connected)
             D: resolution by source authority → explained confidence → privacy allowlist
```

The target site is one source among several. When it refuses automated access
(401/403/429, bot challenge, robots.txt, timeout) it is marked `blocked` and the check
carries on with the other sources. Nothing tries to get past a block: honest
User-Agent, robots.txt honoured, no browser, no proxies.

Evidence is ranked: `official` (NAP register, VIES, Registry Agency) > `company_source`
(the operator's own pages and documents, live or archived) > `public_source` >
`search_evidence` > `infrastructure` (DNS/TLS/RDAP/CT - supporting only, never
identifies a company). LOW confidence is shown as a candidate, never asserted; two
equally supported companies give "not identified" rather than a guess.

## Technology

* Python standard library at runtime (`http.server`, `html.parser`, `sqlite3`,
  `urllib`, `ssl`) plus one sibling package: the guarded fetcher of the author's
  `sealbox` project, used for every fetch of a target site (a checkout next to this
  one, or on `PYTHONPATH`); `pytest` for tests, which use a fake fetcher
* SQLite for the result store (`domaincheck.db`) and the daily counter (`day.db`)
* A self-contained HTML page with inlined JavaScript, built by `web/build.py`;
  `node` is used at build time to syntax-check the scripts and pre-render the paginated
  example pages

## Run it locally

```bash
python -m pytest tests -q                                   # offline test suite
python -m shopwatch example.bg -o report.json               # disclosure triage of live shops (CLI)
python -m shopwatch.domaincheck check example.bg            # one domain → company check, JSON out
PYTHONPATH=.:../sealbox python -m shopwatch.serve            # checker: http://127.0.0.1:8778/
python web/build.py                                         # build the page (web/index.html, page-N.html)
```

`shopwatch.serve` binds to 127.0.0.1 only and serves the URL check form at `/` and the
JSON API (`POST /api/domain-check`). The built triage page calls `/api/domain-check` on
its own origin, so to use its live field the page and the API must be served from one
origin - in the deployment below a small front server does that. `deploy.py` is an
older local preview script that serves the built page statically, without the API.

API:

```
POST /api/domain-check   {"url": "https://example.bg"}                  -> full result (synchronous)
POST /api/domain-check   {"url": "https://example.bg", "async": true}   -> 202 {"job_id", "status_url", ...}
GET  /api/domain-check/<job_id>                                         -> state, stages, provisional, result
POST /api/domain-check/<job_id>/cancel                                  -> cancelled
```

## Configuration

Everything works with no configuration. Optional:

* **Search**: a Brave Search API key in `~/.config/shopwatch/brave_token` (or
  `SHOPWATCH_BRAVE_TOKEN`).
* **Commercial Register**: Registry Agency messages dropped into
  `~/.config/shopwatch/registry_inbox/` (access is gated by the agency; see
  BLOCKERS.md).
* **AI-assisted fallback**: `~/.config/shopwatch/ai_fallback.json`, **off by default**
  and no provider ships (see below).
* `shopwatch.serve --port N --db PATH` for the checker's port and store.

## Deployment

The public demo runs on a Steam Deck as three systemd user services:

| service | what | listens on |
|---|---|---|
| `shopwatch-check` | `python3 -m shopwatch.serve --port 8778` (checker, with `sealbox` on `PYTHONPATH`) | 127.0.0.1:8778 |
| `shopwatch-web` | front server: the built page at `/` (`?page=N` → `page-N.html`); forwards only `/api/domain-check` (+ job status / cancel, ids checked by pattern, body ≤ 4 KB) and `/check/` to the checker | 127.0.0.1:8779 |
| `shopwatch-tunnel` | `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8779` | outbound to Cloudflare; its metrics on 127.0.0.1 only |

The checker's port is never exposed directly. The front server and the unit files are
part of that deployment, not of this repository. The public URL is a Cloudflare **Quick
Tunnel** (`trycloudflare.com`): a random hostname that **changes whenever the tunnel
process restarts**, no uptime guarantee, intended for testing and demos; Cloudflare
terminates TLS. Tested with Python 3.12 (Ubuntu) and 3.13 (SteamOS).

## Security

* The checker fetches third-party sites, so it is bound to localhost and reachable only
  through the front server, which forwards two named routes with a bounded body. The
  per-client limit reads the tunnel's `CF-Connecting-IP`, which is trustworthy only
  because the checker's port is not reachable from outside.
* Abuse limits: 8 checks / 5 min per client, 5 real fetches / 10 min per target domain,
  400 checks / day, 2 concurrent; results cached 10 min (VIES 24 h). Jobs: at most 2
  running + 2 queued, 150 s timeout.
* Every fetch of the target site and its related hosts - the part a visitor controls -
  goes through sealbox's address guard (no loopback, private or internal addresses,
  re-checked on every redirect); the SSRF boundary has its own tests. Fixed public
  endpoints (NAP, VIES, RDAP, crt.sh, Internet Archive, DNS-over-HTTPS) are called
  directly.
* Privacy: ЕГН/ЛНЧ are never kept; mailboxes are classified by context; people acting
  for a company are shown only with their source. See PRIVACY.md.

## Limitations

* A structurally **valid ЕИК does not mean the company exists** - that needs the
  Commercial Register, which has no open API; the adapter exists but is not connected.
* Extraction is heuristic (labels in Bulgarian and English, adjacent elements, PDFs).
  Sites that block automated access can only be read through archived copies.
* The findings are triage, not legal determinations; a VAT number that is not
  published is reported as information, not as a shortfall.
* The 20-shop example set is a selected benchmark sample, not a rate for Bulgarian
  shops.
* A live check takes about 5-60 s (worst case about 90 s). Jobs live in the checker
  process and are lost when it restarts.
* The public demo URL is a temporary Quick Tunnel.

## Project status

Working demo. The offline test suite passed on 2026-09-26: 236 passed / 11 skipped on
the development PC, 214 passed / 33 skipped on the Steam Deck (tests that need `node`
or live network are skipped where those are unavailable). Live acceptance tests exist
(`SHOPWATCH_LIVE=1 python -m pytest tests/test_live.py`) and were not part of that run.

## Technical reference

### Capability benchmark (`benchmark/`)

The 20 example shops double as a benchmark for the tool itself.

* `benchmark/shops.json` — hand-verified ground truth. Discovery seed: the NAP public
  online-shop list (listing there says nothing about compliance). For every shop and
  field (ЕИК, company, legal form, address, phone, e-mail, VAT, domain → operator): the
  value the site publishes or `null`, the URL, source type, current/archived, retrieval
  date and excerpt; the NAP declaration and VIES record; every shortfall with evidence.
* `benchmark/research.py` — the broad crawl used to build it. Not used by the product.
* `benchmark/run.py` — runs the product exactly as the website does and compares:

```bash
python -m benchmark.run --observe > benchmark/observed.json     # network, ~6 min
python -m benchmark.run --report benchmark/observed.json         # offline
python -m benchmark.run --publish benchmark/observed.json > web/sample_report.json
```

Every check runs twice - as the product does, and with the NAP list switched off -
because NAP itself supplies the domain → ЕИК link. The report gives both domain →
operator figures, per-field outcomes, which ground-truth shortfalls the triage
reproduces, its false alarms, and a capability-gap log with the technical reason for
every miss (`benchmark/gaps.json`). The public wording is "In our 20-shop benchmark,
Shopwatch detected N of M manually verified disclosure findings".

### Company compliance profile

| field | authoritative (→ verified) | corroborating | first-party |
|---|---|---|---|
| ЕИК, legal name, legal form, registered address | Commercial Register | VIES (name, VAT address) | legal pages, PDFs, archive |
| phone, e-mail | Commercial Register (registered contacts) | - | contact / legal pages, archive |
| VAT number, VAT status | VIES | - | published VAT number |
| domain → operator | Commercial Register (registered website) | - | operator statements, ЕИК on the site |

"Not found" means the sources were read and the value is not there; "unavailable" means
they could not be read. VIES is only ever a source for VAT and, as corroboration, name
and seat. A conflict keeps every distinct value with its sources.

### AI-assisted fallback (off by default)

Field-specific and last: only for an eligible field still `not_found` / `unavailable`
after deterministic extraction. The AI is only a discovery mechanism - it must name a
public URL, which Shopwatch fetches itself and re-extracts. Outcomes: `ai_assisted`
(verified from the cited page), `ai_assisted_unretrieved` (shown with a caveat),
`not_independently_verified` (no value shown). It never outranks the register, VIES or a
first-party page. No provider ships; disabled or failing, the deterministic result is
returned unchanged.

```json
{"enabled": false, "provider": null, "max_requests_per_lookup": 2,
 "max_requests_per_day": 20, "eligible_fields": ["phone", "email"], "cache_days": 30}
```

### Other commands

```bash
python -m shopwatch.domaincheck check shop.bg --refresh      # bypass caches (debugging)
python -m shopwatch.domaincheck refresh                      # re-verify, registry inbox, retention
python -m shopwatch.domaincheck admin open objection --eik 123456789 --field registered_address --note "..."
```

## Layout

```
shopwatch/        eik, disclosure, compliance (triage) · investigate (live crawl) ·
                  domaincheck, sources, profile, registry, store, privacy, pdftext,
                  aifallback (domain → company) · serve (localhost checker) · daylog
web/              page template, scripts, translations, build.py, sample_report.json
benchmark/        20-shop ground truth, runner, gap log
tests/            offline tests (+ opt-in live acceptance tests)
DECISIONS.md      design decisions · BLOCKERS.md external blockers · PRIVACY.md
```
