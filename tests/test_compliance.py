"""Tests for shopwatch.compliance — heuristic scoring, no network."""

from shopwatch.compliance import assess


def full_merchant():
    return {
        "eik": "123456786",  # synthetic, valid per algorithm
        "company_name": "Тест ООД",
        "address": "гр. София, ул. Примерна 1",
        "email": "shop@example.bg",
        "phone": "+35921234567",
        "vat_number": "BG123456786",
        "evidence": {},
    }


def test_full_disclosure_is_ok():
    res = assess(full_merchant())
    assert res["verdict"] == "ok"
    assert res["score"] <= 19


def test_missing_eik_is_noncompliant():
    m = full_merchant()
    m["eik"] = None
    res = assess(m)
    assert res["verdict"] == "noncompliant"
    assert any(f["code"] == "NO_EIK" for f in res["findings"])


def test_invalid_eik_flagged():
    m = full_merchant()
    m["eik"] = "123456789"  # bad checksum
    res = assess(m)
    assert any(f["code"] == "EIK_INVALID" for f in res["findings"])


def test_missing_address_and_contact():
    m = full_merchant()
    m["address"] = None
    m["email"] = None
    m["phone"] = None
    res = assess(m)
    codes = {f["code"] for f in res["findings"]}
    assert {"NO_ADDRESS", "NO_CONTACT"} <= codes
    assert res["score"] <= 100


def test_score_bounds():
    res = assess(
        {"eik": None, "company_name": None, "address": None,
         "email": None, "phone": None, "vat_number": None}
    )
    assert 0 <= res["score"] <= 100
    assert res["verdict"] == "noncompliant"
