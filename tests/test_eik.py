"""Tests for shopwatch.eik — no network.

All test numbers are SYNTHETIC: each is built by applying the checksum
algorithm from the milestone spec to an arbitrary prefix, never copied
from a real company. No real EIK appears here.
"""

from shopwatch.eik import validate_eik

W9_P1 = (1, 2, 3, 4, 5, 6, 7, 8)
W9_P2 = (3, 4, 5, 6, 7, 8, 9, 10)
W13_P1 = (2, 7, 3, 5)
W13_P2 = (4, 9, 5, 7)


def check9(prefix8: str) -> str:
    """Compute the correct 9th digit for an 8-digit prefix, per spec."""
    digits = [int(c) for c in prefix8]
    r = sum(d * w for d, w in zip(digits, W9_P1)) % 11
    if r < 10:
        return str(r)
    r2 = sum(d * w for d, w in zip(digits, W9_P2)) % 11
    return str(r2) if r2 < 10 else "0"


def check13tail(middle4: str) -> str:
    """Compute the correct 13th digit for digits 9..12, per spec."""
    digits = [int(c) for c in middle4]
    r = sum(d * w for d, w in zip(digits, W13_P1)) % 11
    if r < 10:
        return str(r)
    r2 = sum(d * w for d, w in zip(digits, W13_P2)) % 11
    return str(r2) if r2 < 10 else "0"


def find_prefix9(predicate, start=10_000_000, stop=10_200_000) -> str:
    """Search for an 8-digit prefix satisfying predicate(r, r2)."""
    for i in range(start, stop):
        p = f"{i:08d}"
        digits = [int(c) for c in p]
        r = sum(d * w for d, w in zip(digits, W9_P1)) % 11
        r2 = sum(d * w for d, w in zip(digits, W9_P2)) % 11
        if predicate(r, r2):
            return p
    raise AssertionError("no prefix found in range")


def find_middle4(first_digit: str, predicate) -> str:
    for i in range(1000):
        m = first_digit + f"{i:03d}"
        digits = [int(c) for c in m]
        r = sum(d * w for d, w in zip(digits, W13_P1)) % 11
        r2 = sum(d * w for d, w in zip(digits, W13_P2)) % 11
        if predicate(r, r2):
            return m
    raise AssertionError("no middle4 found")


def mutate_last(eik: str) -> str:
    return eik[:-1] + ("0" if eik[-1] != "0" else "1")


# --- 9-digit ---------------------------------------------------------------


def test_9digit_pass1_valid():
    eik = "12345678" + check9("12345678")
    assert eik == "123456786"
    res = validate_eik(eik)
    assert res == {"valid": True, "kind": "9", "reason": "ok"}


def test_9digit_pass1_invalid_mutated_check():
    bad = mutate_last("123456786")
    res = validate_eik(bad)
    assert res["valid"] is False and res["kind"] == "9"


def test_9digit_pass2_valid():
    # r == 10 on pass 1 -> second weight set decides.
    prefix = find_prefix9(lambda r, r2: r == 10 and r2 < 10)
    eik = prefix + check9(prefix)
    res = validate_eik(eik)
    assert res["valid"] is True and res["kind"] == "9"


def test_9digit_pass2_invalid_mutated_check():
    prefix = find_prefix9(lambda r, r2: r == 10 and r2 < 10)
    bad = mutate_last(prefix + check9(prefix))
    res = validate_eik(bad)
    assert res["valid"] is False and res["kind"] == "9"


def test_9digit_r2_is_10_requires_zero():
    prefix = find_prefix9(lambda r, r2: r == 10 and r2 == 10)
    assert validate_eik(prefix + "0")["valid"] is True
    assert validate_eik(prefix + "1")["valid"] is False


# --- 13-digit --------------------------------------------------------------


def test_13digit_pass1_valid():
    base9 = "12345678" + check9("12345678")  # 123456786
    middle4 = base9[8] + "000"
    eik13 = base9 + middle4[1:] + check13tail(middle4)
    assert eik13 == "1234567860001"
    res = validate_eik(eik13)
    assert res["valid"] is True and res["kind"] == "13"


def test_13digit_pass1_invalid_mutated_tail():
    bad = mutate_last("1234567860001")
    res = validate_eik(bad)
    assert res["valid"] is False and res["kind"] == "13"


def test_13digit_bad_base9_rejected():
    bad_base = mutate_last("123456786") + "0001"
    res = validate_eik(bad_base)
    assert res["valid"] is False and res["kind"] == "13"


def test_13digit_tail_pass2_valid_and_invalid():
    base9 = "12345678" + check9("12345678")
    middle4 = find_middle4(base9[8], lambda r, r2: r == 10 and r2 < 10)
    good = base9 + middle4[1:] + check13tail(middle4)
    assert validate_eik(good)["valid"] is True
    assert validate_eik(mutate_last(good))["valid"] is False


def test_13digit_tail_r2_is_10_requires_zero():
    base9 = "12345678" + check9("12345678")
    middle4 = find_middle4(base9[8], lambda r, r2: r == 10 and r2 == 10)
    assert validate_eik(base9 + middle4[1:] + "0")["valid"] is True
    assert validate_eik(base9 + middle4[1:] + "1")["valid"] is False


# --- normalisation / shape -------------------------------------------------


def test_separators_and_whitespace_accepted():
    assert validate_eik("  123-456-786  ")["valid"] is True
    assert validate_eik("123.456.786")["valid"] is True


def test_invalid_length():
    assert validate_eik("12345") == {
        "valid": False,
        "kind": None,
        "reason": "invalid length 5: must be 9 or 13 digits",
    }


def test_non_digits_rejected():
    res = validate_eik("12345678A")
    assert res["valid"] is False and res["kind"] is None


def test_empty_rejected():
    assert validate_eik("   ")["valid"] is False


def test_smoke_example_runs():
    res = validate_eik("123456789")
    assert set(res) == {"valid", "kind", "reason"}


def test_unicode_digits_rejected_without_raising():
    """str.isdigit() accepts superscripts and non-ASCII digits; int() does not.

    HTML is attacker-controlled input, so this must return a verdict, never raise.
    """
    for bad in ("\u00b2" * 9, "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669", "\u00b9\u00b2\u00b3" * 3):
        out = validate_eik(bad)
        assert out["valid"] is False
        assert "non-digit" in out["reason"]
