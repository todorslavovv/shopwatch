"""Merchant disclosure compliance scoring — offline heuristic.

Weighting (higher score = more suspicious; capped at 0-100):
    NO_EIK       +50  error    no identifier published at all
    EIK_INVALID  +40  error    published EIK fails the checksum
    NO_ADDRESS   +20  warning  no business address found
    NO_CONTACT   +20  warning  neither email nor phone found
    NO_COMPANY   +10  warning  no company name found
    NO_EMAIL     +10  warning  email missing but phone present
    NO_PHONE     +10  warning  phone missing but email present
    NO_VAT       +0   info     VAT number missing (not required for all traders)

Verdict thresholds: score 0-19 -> "ok", 20-49 -> "suspect", 50-100 ->
"noncompliant". A missing EIK alone (+50) is therefore always at least
"noncompliant".

The score is a rough heuristic for triage, NOT a probability of fraud and
NOT legal advice. A structurally valid EIK says nothing about whether the
company exists — that requires the Commercial Register (later milestone).
"""

from __future__ import annotations

import re

from shopwatch.eik import validate_eik

_OK_MAX = 19
_SUSPECT_MAX = 49


def assess(merchant: dict, registry: dict | None = None, *, company_names=()) -> dict:
    """Score a merchant dict as produced by extract_merchant().

    Returns {"score": 0-100, "verdict": "ok"|"suspect"|"noncompliant",
    "findings": [{"code", "severity", "message"}]}.
    """
    if not isinstance(merchant, dict):
        raise TypeError("merchant must be a dict")
    score = 0
    findings: list[dict] = []

    def add(code: str, severity: str, message: str, weight: int) -> None:
        nonlocal score
        findings.append({"code": code, "severity": severity, "message": message})
        score += weight

    reg = (registry or {}).get("status")
    eik = merchant.get("eik")
    if not eik:
        add(
            "NO_EIK",
            "error",
            "No EIK/UIC identifier published on the shop site.",
            50,
        )
    else:
        check = validate_eik(str(eik))
        if not check["valid"]:
            add(
                "EIK_INVALID",
                "error",
                f"Published EIK {eik!r} is structurally invalid: {check['reason']}.",
                40,
            )

    if not merchant.get("address"):
        add("NO_ADDRESS", "warning",
            "No business address with a street and number published.", 20)

    email = merchant.get("email")
    phone = merchant.get("phone")
    if not email and not phone:
        add("NO_CONTACT", "warning", "No email and no phone published.", 20)
    elif not email:
        add("NO_EMAIL", "warning", "No email published (phone present).", 10)
    elif not phone:
        add("NO_PHONE", "warning", "No phone published (email present).", 10)

    if not merchant.get("company_name"):
        add("NO_COMPANY", "warning",
            "No company name with its legal form (ООД, ЕООД, АД...) published.", 10)

    # "Органик Бранд ЕООД" in the footer, "Органик Бранд ООД" in the privacy policy: the
    # trader's identity is disclosed inconsistently, whichever of the two is right.
    distinct: dict[str, str] = {}
    for n in company_names:
        distinct.setdefault(" ".join(re.findall(r"\w+", n.casefold())), n)
    if len(distinct) > 1:
        add("COMPANY_INCONSISTENT", "warning",
            "Different company names or legal forms on different pages: "
            + "; ".join(list(distinct.values())[:3]) + ".", 10)

    if not merchant.get("vat_number"):
        add(
            "NO_VAT",
            "info",
            "No VAT number published (informational: not required for all traders).",
            0,
        )

    if reg == "confirmed":
        # A registry hit does not lower an otherwise-earned score; it records only that
        # the identifier resolves to a real, VAT-registered trader.
        findings.append({"code": "EIK_CONFIRMED", "severity": "info",
                         "message": "Identifier confirmed in the EU VAT registry."})
    elif reg == "not_found" and eik:
        # Weight 0, deliberately. VIES holds VAT-registered traders only, so a shop
        # below the registration threshold is legitimately absent. Scoring this would
        # punish exactly the small legitimate traders the tool must not accuse.
        findings.append({"code": "EIK_NOT_IN_VAT_REGISTRY", "severity": "info",
                         "message": "Not in the EU VAT registry. Expected for traders "
                                    "below the VAT threshold; not evidence of fraud."})
    elif reg == "unavailable" and eik:
        findings.append({"code": "REGISTRY_UNAVAILABLE", "severity": "info",
                         "message": "The VAT registry could not be reached, so "
                                    "existence was not verified."})

    score = max(0, min(100, score))
    if score <= _OK_MAX:
        verdict = "ok"
    elif score <= _SUSPECT_MAX:
        verdict = "suspect"
    else:
        verdict = "noncompliant"
    return {"score": score, "verdict": verdict, "findings": findings}
