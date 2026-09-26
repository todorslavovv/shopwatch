"""Confirm a published identifier against the EU VAT registry (VIES).

The offline core answers "is this number well-formed". Only a registry answers "does
this trader exist", which is the question that actually matters and the one shopwatch
has been unable to answer since M1.

VIES is free, needs no key and no account, and is the authoritative EU source. Its
limits are stated honestly rather than hidden:

  * It covers VAT-REGISTERED traders only. A small Bulgarian shop under the
    registration threshold is legitimately absent, so "not found" is NEVER evidence of
    fraud. It is scored as information, not as a finding against the trader.
  * The service is a federation of national systems; the Bulgarian one goes down. An
    outage must read as "unknown", never as "not found".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

VIES = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{num}"
TIMEOUT = 20


def check_vat(number: str, country: str = "BG", *, timeout: int = TIMEOUT,
              opener=None) -> dict:
    """Look one identifier up in VIES.

    Returns {"status", "name", "address", "source"} where status is:
        "confirmed"  — the registry holds this number
        "not_found"  — the registry answered, and does not hold it
        "unavailable" — the registry could not be reached or errored
    """
    num = "".join(ch for ch in str(number) if ch.isdigit())
    out = {"status": "unavailable", "name": None, "address": None, "source": "VIES"}
    if not num:
        return out
    url = VIES.format(cc=country, num=num)
    try:
        fetch = opener or (lambda u: urllib.request.urlopen(u, timeout=timeout))
        with fetch(url) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return out                     # stays "unavailable": an outage is not a verdict
    if data.get("isValid"):
        out.update(status="confirmed",
                   name=(data.get("name") or "").strip() or None,
                   address=" ".join((data.get("address") or "").split()) or None)
    else:
        out["status"] = "not_found"
    return out


def name_matches(published: str | None, registered: str | None) -> bool | None:
    """Rough agreement between the name on the site and the one in the registry.

    Legal-form suffixes and quoting vary wildly ("ТЕХНОПОЛИС БЪЛГАРИЯ - ЕАД" on one
    side, '"Технополис България" ЕАД' on the other), so this compares the significant
    words only. Returns None when either side is missing rather than guessing.
    """
    if not published or not registered:
        return None
    forms = {"еоод", "оод", "ад", "еад", "ет", "кд", "кда", "сд", "ltd", "eood", "ood"}

    def words(s: str) -> set[str]:
        cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in s.lower())
        return {w for w in cleaned.split() if len(w) > 2 and w not in forms}

    a, b = words(published), words(registered)
    if not a or not b:
        return None
    return len(a & b) >= max(1, min(len(a), len(b)) // 2)


if __name__ == "__main__":        # self-check: no network, pure logic only
    assert name_matches('"ТЕХНОПОЛИС БЪЛГАРИЯ" ЕАД', "ТЕХНОПОЛИС БЪЛГАРИЯ - ЕАД")
    assert name_matches("МОДА СОФИЯ ООД", "ЕМАГ ИНТЕРНЕШЪНЪЛ - ООД") is False
    assert name_matches(None, "X") is None
    assert check_vat("")["status"] == "unavailable"
    print("registry self-check ok")
