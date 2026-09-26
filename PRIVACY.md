# Privacy: domain → company check

Status: **draft, not signed.** Everything below is the engineering position the code
implements. The legitimate-interest assessment in section 3 is a draft for the controller
(the person operating this service) to review, amend and sign. It is not legal advice.

## 1. What is processed

| Data | Source | Stored | Shown |
|---|---|---|---|
| ЕИК, legal name, legal form, seat address | EU VIES; Commercial Register once contracted | yes, current state only | yes |
| Status, capital, registration date, events | Commercial Register once contracted | yes | yes |
| Business contacts the company publishes: e-mail (whatever the provider - a Gmail address the shop advertises is its contact), phone, address (compliance profile) | the shop's own pages and documents, live or archived; registered contacts from the Commercial Register | only inside the 10-minute result cache, purged within 6 h | yes, with source; mailboxes of public bodies and couriers named in legal boilerplate (КЗЛД, КЗП, Еконт...) are not attributed to the shop |
| Evidence metadata: source URL, claim, time, SHA-256 of the page | all sources | yes | yes |
| Names and roles of people who act for the company (manager, representative, owner) | Commercial Register once contracted (DeedV2 `Person/Name` and `Position`; `Indent` = ЕГН is never read); the company's own pages ("представлявано от управителя ...", "Управител: ...") | yes, with source and verification state (register -> verified; own pages -> published by the company) | yes, with source; off only if the controller creates `PEOPLE-OFF`; objections per person and field honoured (section 4) |
| ЕГН / ЛНЧ, personal ID numbers, a 10-digit BG VAT number (it *is* an ЕГН), a person's private contact details held in a register filing, credentials, birth date, sensitive categories | — | **never** | **never** |
| Domain registrant details | RDAP | **never read** | — |
| Declared operator of an online shop: ЕИК, declared name (as NAP publishes it), shop entry, declaration date | NAP public online-shop register | yes, 24 h cache | yes, with source |
| Example worklist (20 shops): what each shop publishes about its trader - company data and business contacts - and short evidence excerpts | the shops' own pages, NAP, VIES | in `benchmark/shops.json` | yes, as published; the mailboxes of apparently unrelated people left in one shop's template text (biancandfred.com) are not reproduced: the finding does not need them and they are not the trader's contacts |
| AI-assisted fallback (off by default) | a configured research provider | value, source URL, ≤300-char excerpt, provider, time per (domain, field) | if Shopwatch finds the value on the cited page ("independently verified from the cited public source"), or the cited page could not be retrieved ("source could not be independently retrieved"); not when the cited page says otherwise or no source is cited |
| Raw HTML, PDFs, archived copies, registry documents | — | **never** (hash, URL and snapshot date only) | — |

The rule lives in one place, `shopwatch/privacy.py` (`is_field_publishable`), as a
field-level **allowlist**. The store applies it before every write and the API applies it
again before every response. A field that is not on the list is dropped, including fields
a source adds in future. This follows CJEU C-200/23: a register filing containing a
personal datum does not by itself make publishing that datum lawful.

## 2. Purpose

Identify the legal entity that operates an online shop, so that a consumer-protection or
cybercrime investigator can triage reports about fraudulent shops. Nothing else: no
profiling of people, no marketing, no resale.

## 3. Legitimate-interest assessment (draft, Art. 6(1)(f) GDPR)

**Purpose test.** Identifying the operator of an online shop is a legitimate interest:
consumers and investigators need to know who is behind a shop, and Bulgarian law already
obliges online traders to publish that identity (Consumer Protection Act; E-Commerce Act
art. 4).

**Necessity test.** Company-level facts (ЕИК, name, seat) meet the purpose on their own
in most cases. A manager's name adds value only when the question is who acts for the
company, e.g. when a shop's operator company was set up shortly before a fraud wave. So
names are necessary only in that narrower case - which is also the case the public
registers and established Bulgarian company databases serve. Default: **on** (see Decision).

**Balancing test.** For company facts, the impact on individuals is negligible. For the
names of managers and owners: these are already public in the Commercial Register by
statute, the purpose is narrow, only name and role are kept (no ЕГН, no address, no
date of birth), the source and time are recorded against every name, retention is
limited (section 5), and the data-subject routes in section 4 exist. Residual risk:
listing a person next to a domain that later turns out to be fraudulent may suggest they
were involved. Mitigation: the page states that only official-register facts are facts
and everything else is evidence, never a conclusion.

**Decision.** Instruction from the operator of this service, 2026-09-24: legitimately
public company information is not excluded because it is personal data. Company facts +
names and roles of the people who act for the company, from the Commercial Register and
from the company's own publications, each with its source and verification state; ЕГН,
ЛНЧ and other identifiers never. For the controller to confirm:
Controller: ________________ Date: ____________

To switch names and roles off: create an empty file `PEOPLE-OFF` in the shopwatch
directory on the deployment host. Delete it to switch them on again.

## 4. Data-subject requests

Recorded, applied and audited in the store (`dsr_requests`, `restrictions`, `audit`):

```bash
python -m shopwatch.domaincheck admin open  <correction|objection|erasure|access|source_challenge> \
        --subject "Name" --eik 123456789 --field registered_address --note "what was asked"
python -m shopwatch.domaincheck admin resolve <id> <restrict|erase|correct|reject> --note "why"
python -m shopwatch.domaincheck admin access "Name"          # everything held about a person
```

Procedure: identify the field → identify its source → decide whether publication is
legally required (a Commercial Register fact still exists in the register whatever this
service does) → restrict, erase, correct or reject here → the outcome and reason are
audited. **Erasure removes what this service holds; it cannot remove a statutory register
entry**, and the reply to the requester must say so.

## 5. Retention

Domains not checked for 180 days are purged along with their evidence; entities no
longer linked to any domain are purged with their people; caches older than 7 days are
dropped. The purge runs with the background worker every 6 hours.

## 6. Transparency

Every returned claim carries its source and retrieval time. The page names the source of
each company fact (VIES or the Commercial Register) and labels everything else as
website, search-index or third-party evidence.
