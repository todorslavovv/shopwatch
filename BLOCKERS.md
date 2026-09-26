# BLOCKERS.md

M1/M2: no blockers (see git history).

## Domain → company check (2026-09-23)

Nothing blocks the feature: every case in the acceptance set resolves without any
credential. Two optional providers stay unconfigured until the owner acts, and each is
reported as `unconfigured` on the page rather than faked.

1. **Commercial Register (Registry Agency).** The Integration package (`schemas.zip`,
   06.10.2025, read in full) defines the XML format only - Envelopev2, DeedV2, Fields,
   Notifications - and **no transport, endpoint or authentication**; those come with the
   contract (paid service) or with access for official purposes (a separate route for
   public bodies, relevant if ГДБОП ever runs this). The adapter parses the official
   format and ingests delivered messages from `~/.config/shopwatch/registry_inbox/`;
   once a delivery channel writes there, verification upgrades from VIES to the register
   with no code change. No endpoint has been guessed.
2. **Search API key.** Brave Search API is implemented behind `SearchProvider`; with a
   key in `~/.config/shopwatch/brave_token` it is queried on every check. No legitimate
   keyless web-search API exists; without one the pipeline uses related hosts, legal
   pages, PDFs and the Internet Archive instead.
3. **Legitimate-interest sign-off** (PRIVACY.md) - only for names of managers/owners,
   which only the Commercial Register provides. Company identity never waits for it.

Known limits: crt.sh often answers 502 (reported as temporarily unavailable); the
Internet Archive rate-limits bursts (a check makes at most 10 lookups, 1 s apart);
Word-exported PDFs lose spaces between words (names are compared letter-for-letter).
