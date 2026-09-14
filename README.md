# Shopwatch M1 — offline merchant-identity core

Flags potentially fraudulent Bulgarian online shops by checking the merchant
identity data the shop itself publishes. **Fully offline**: no network calls.

## What it checks

1. **EIK structure** (`shopwatch/eik.py`) — `validate_eik()` runs the official
   checksum algorithm for 9- and 13-digit EIK/БУЛСТАТ numbers (both
   first/second-pass branches). Returns `{"valid", "kind", "reason"}`.
2. **Disclosure extraction** (`shopwatch/disclosure.py`) — `extract_merchant()`
   parses shop HTML with stdlib `html.parser` and finds EIK, company name,
   address, email, phone, VAT number anchored to Bulgarian/English labels,
   inline or in adjacent elements. Returns each field plus `evidence` snippets.
3. **Compliance scoring** (`shopwatch/compliance.py`) — `assess()` scores the
   extracted merchant (0–100, higher = more suspicious) with findings such as
   `NO_EIK`, `EIK_INVALID`, `NO_ADDRESS`, `NO_CONTACT`. The score is a rough
   triage heuristic, **not** a probability of fraud.

## Important limitation

A structurally **valid EIK does NOT mean the company exists**. Existence can
only be confirmed against the Bulgarian Commercial Register (ТР), which is a
later milestone. M1 only answers: "did the shop publish an identifier, and is
it well-formed?"

## Run

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
python -c "from shopwatch.eik import validate_eik; print(validate_eik('123456789'))"
```

## Web report

`web/index.html` is a standalone Merchant Disclosure Triage page: shops ranked worst-first
by how far their published trader identity falls short of what Bulgarian law requires,
with the extracted fields and the page snippets each value came from.

It states plainly what the tool does not claim — above all that a valid ЕИК checksum does
not mean the company exists, which needs the Commercial Register.

Rebuild:

```bash
python web/build.py
```

The page is self-contained — data and code inlined, no network calls.
