"""EIK (ЕИК / UIC) structural validation — offline only.

A valid-format EIK does NOT mean the company exists. Existence can only be
confirmed via the Bulgarian Commercial Register (a later milestone).
This module performs the checksum test only.
"""

from __future__ import annotations

_W9_P1 = (1, 2, 3, 4, 5, 6, 7, 8)
_W9_P2 = (3, 4, 5, 6, 7, 8, 9, 10)
_W13_P1 = (2, 7, 3, 5)
_W13_P2 = (4, 9, 5, 7)

# Characters stripped during normalisation besides surrounding whitespace.
# Letters are NOT separators: anything alphabetic left after stripping makes
# the value invalid (e.g. "AB123" is invalid, not "123").
_SEPARATORS = set(" \t\n\r-./:,;" + "()[]")


def _normalise(value: str) -> str:
    text = value.strip()
    return "".join(ch for ch in text if ch not in _SEPARATORS)


def _check9(first8: list[int], check: int) -> tuple[bool, str]:
    """Apply the 9-digit check over 8 leading digits + check digit."""
    r = sum(d * w for d, w in zip(first8, _W9_P1)) % 11
    if r < 10:
        if check == r:
            return True, "ok"
        return False, f"checksum mismatch: expected {r}, got {check}"
    r2 = sum(d * w for d, w in zip(first8, _W9_P2)) % 11
    if r2 < 10:
        if check == r2:
            return True, "ok (second-pass check)"
        return False, f"checksum mismatch: expected {r2}, got {check}"
    if check == 0:
        return True, "ok (second-pass check, r2 == 10)"
    return False, f"checksum mismatch: expected 0, got {check}"


def _check13tail(middle4: list[int], check: int) -> tuple[bool, str]:
    """Apply the 13-digit tail check over digits 9..12 + check digit 13."""
    r = sum(d * w for d, w in zip(middle4, _W13_P1)) % 11
    if r < 10:
        if check == r:
            return True, "ok"
        return False, f"13-digit tail checksum mismatch: expected {r}, got {check}"
    r2 = sum(d * w for d, w in zip(middle4, _W13_P2)) % 11
    if r2 < 10:
        if check == r2:
            return True, "ok (second-pass tail check)"
        return False, f"13-digit tail checksum mismatch: expected {r2}, got {check}"
    if check == 0:
        return True, "ok (second-pass tail check, r2 == 10)"
    return False, f"13-digit tail checksum mismatch: expected 0, got {check}"


def validate_eik(value: str) -> dict:
    """Validate the structural format of a Bulgarian EIK/UIC.

    Returns {"valid": bool, "kind": "9"|"13"|None, "reason": str}.
    "kind" reflects the normalised length (9/13) even when the checksum
    fails; it is None when the length itself is not 9 or 13.
    Never raises on bad input (non-string input is reported invalid).
    """
    if not isinstance(value, str):
        return {"valid": False, "kind": None, "reason": "not a string"}
    digits = _normalise(value)
    if not digits:
        return {"valid": False, "kind": None, "reason": "empty value"}
    # str.isdigit() is True for superscripts ("\u00b2") and non-ASCII digit systems
    # ("\u0661"), but int() then either raises or silently accepts the foreign digits.
    # An EIK is ASCII by definition, and this input comes from attacker-controlled HTML,
    # so require ASCII 0-9 explicitly rather than trusting isdigit().
    if not all("0" <= ch <= "9" for ch in digits):
        return {"valid": False, "kind": None, "reason": "contains non-digit characters"}
    if len(digits) == 9:
        first8 = [int(c) for c in digits[:8]]
        ok, reason = _check9(first8, int(digits[8]))
        return {"valid": ok, "kind": "9", "reason": reason}
    if len(digits) == 13:
        first8 = [int(c) for c in digits[:8]]
        ok9, reason9 = _check9(first8, int(digits[8]))
        if not ok9:
            return {"valid": False, "kind": "13", "reason": f"base 9-digit check failed: {reason9}"}
        middle4 = [int(c) for c in digits[8:12]]
        ok13, reason13 = _check13tail(middle4, int(digits[12]))
        if not ok13:
            return {"valid": False, "kind": "13", "reason": reason13}
        return {"valid": True, "kind": "13", "reason": reason13}
    return {
        "valid": False,
        "kind": None,
        "reason": f"invalid length {len(digits)}: must be 9 or 13 digits",
    }
