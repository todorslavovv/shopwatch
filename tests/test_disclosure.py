"""Tests for shopwatch.disclosure — no network, inline HTML fixtures only.

EIKs below are SYNTHETIC, computed from arbitrary prefixes by the checksum
algorithm (same construction as tests/test_eik.py). No real company number.
"""

import time

import pytest

from shopwatch.disclosure import extract_merchant


def make_eik9(prefix8: str = "12345678") -> str:
    w1 = (1, 2, 3, 4, 5, 6, 7, 8)
    w2 = (3, 4, 5, 6, 7, 8, 9, 10)
    digits = [int(c) for c in prefix8]
    r = sum(d * w for d, w in zip(digits, w1)) % 11
    if r < 10:
        return prefix8 + str(r)
    r2 = sum(d * w for d, w in zip(digits, w2)) % 11
    return prefix8 + (str(r2) if r2 < 10 else "0")


EIK = make_eik9()  # == "123456786"


def test_label_and_value_in_one_tag_cyrillic():
    html = f"""<html><body>
      <p>Фирма: Тест ООД</p>
      <p>ЕИК: {EIK}</p>
      <p>Седалище: гр. София, ул. Примерна 1</p>
      <p>Телефон: +359 2 123 4567</p>
      <p>Ел. поща: shop@example.bg</p>
      </body></html>"""
    m = extract_merchant(html)
    assert m["eik"] == EIK
    assert m["company_name"] == "Тест ООД"
    assert "София" in m["address"]
    assert m["email"] == "shop@example.bg"
    assert m["phone"] is not None and "359" in m["phone"]
    assert set(m["evidence"]) >= {"eik", "company_name", "address"}


def test_label_and_value_in_sibling_tags():
    html = f"""<html><body><dl>
      <dt>ЕИК</dt><dd>{EIK}</dd>
      <dt>Адрес на управление</dt><dd>гр. Пловдив, ул. Тестова 5</dd>
      </dl></body></html>"""
    m = extract_merchant(html)
    assert m["eik"] == EIK
    assert "Пловдив" in m["address"]


def test_english_labels():
    html = f"""<html><body>
      <p>Company: Example Ltd</p>
      <p>UIC: {EIK}</p>
      <p>Address: 1 Test Street, Sofia</p>
      <p>Email: info@example.com</p>
      <p>Phone: +359888123456</p>
      <p>VAT: BG{EIK}</p>
      </body></html>"""
    m = extract_merchant(html)
    assert m["eik"] == EIK
    assert m["company_name"] == "Example Ltd"
    assert m["address"] == "1 Test Street, Sofia"
    assert m["email"] == "info@example.com"
    assert m["phone"] is not None
    assert m["vat_number"] == "BG" + EIK


def test_cyrillic_label_variants():
    html = f"""<html><body>
      <p>Е.И.К. / БУЛСТАТ: {EIK}</p>
      <p>Дружество: Пример АД</p>
      <p>Адрес за кореспонденция: гр. Варна, бул. Примерен 9</p>
      <p>ИН по ЗДДС: BG{EIK}</p>
      </body></html>"""
    m = extract_merchant(html)
    assert m["eik"] == EIK
    assert m["company_name"] == "Пример АД"
    assert "Варна" in m["address"]
    assert m["vat_number"] == "BG" + EIK


def test_absent_fields_are_none():
    m = extract_merchant("<html><body><p>Здравейте, това е магазин.</p></body></html>")
    assert m["eik"] is None
    assert m["company_name"] is None
    assert m["address"] is None
    assert m["email"] is None
    assert m["phone"] is None
    assert m["vat_number"] is None
    assert m["evidence"] == {}


def test_eik_embedded_in_footer_prose():
    html = f"""<html><body><main><h1>Магазин</h1></main>
      <footer><p>Тест ООД с ЕИК {EIK} е регистрирано в България.
      Всички права запазени.</p></footer></body></html>"""
    m = extract_merchant(html)
    assert m["eik"] == EIK
    assert len(m["evidence"]["eik"]) <= 200


def test_dash_separator_and_mailto_fallback():
    html = f"""<html><body>
      <p>ЕИК - {EIK}</p>
      <a href="mailto:contact@example.bg">пишете ни</a>
      </body></html>"""
    m = extract_merchant(html)
    assert m["eik"] == EIK
    assert m["email"] == "contact@example.bg"


def test_spaced_eik_digits():
    m = extract_merchant("<p>ЕИК: 123 456 786</p>")
    assert m["eik"] == EIK


def test_trailing_year_does_not_glue_onto_eik():
    html = f"<footer><p>Тест ООД с ЕИК {EIK}, 2024. Всички права запазени.</p></footer>"
    assert extract_merchant(html)["eik"] == EIK


def test_phone_like_number_is_not_an_eik():
    m = extract_merchant("<p>ЕИК: +35921234567</p>")
    assert m["eik"] is None


def test_input_over_5mb_rejected():
    with pytest.raises(ValueError):
        extract_merchant("x" * (5 * 1024 * 1024 + 1))


def test_non_string_rejected():
    with pytest.raises(TypeError):
        extract_merchant(None)  # type: ignore[arg-type]


# --- M2 audit defects -------------------------------------------------------


def test_m2_defect1_fragmented_eik_value():
    html = "<p><b>ЕИК</b>: <span>100</span><span>000001</span></p>"
    m = extract_merchant(html)
    assert m["eik"] == "100000001"


def test_m2_defect1_fragmented_eik_with_trailing_prose():
    # Assembly must still stop at the first non-numeric token: the year
    # after the fragments must not glue onto the number.
    html = "<p><b>ЕИК</b>: <span>100</span><span>000001</span>, 2024 година</p>"
    m = extract_merchant(html)
    assert m["eik"] == "100000001"


def test_m2_defect2_unlabelled_address():
    m = extract_merchant("<p>гр. София, бул. Витоша 12, ет.3</p>")
    assert m["address"] == "гр. София, бул. Витоша 12, ет.3"


def test_m2_defect2_village_address():
    m = extract_merchant("<p>с. Бистрица, ул. Речна 4</p>")
    assert m["address"] == "с. Бистрица, ул. Речна 4"


def test_m2_defect3_unlabelled_company():
    m = extract_merchant('<p>"ТЕСТ ТЪРГОВИЯ" ЕООД</p>')
    assert m["company_name"] == "ТЕСТ ТЪРГОВИЯ ЕООД"
    assert '"' not in m["company_name"]
    assert "ЕООД" in m["evidence"]["company_name"]


def test_m2_prose_city_without_thoroughfare_is_not_address():
    m = extract_merchant("<p>Доставяме до гр. София за един ден</p>")
    assert m["address"] is None


def test_m2_word_containing_legal_form_is_not_company():
    assert extract_merchant("<p>Най-добри цени в града</p>")["company_name"] is None
    assert extract_merchant("<p>Добре дошли в магазин ПРОДАД</p>")["company_name"] is None


def test_m2_unlabelled_9digit_number_is_not_eik():
    m = extract_merchant("<p>Поръчка 100000001 е изпратена</p>")
    assert m["eik"] is None


def test_m2_20k_nested_tags_under_1s():
    html = "<div>" * 20000 + "ЕИК: 123456786" + "</div>" * 20000
    t0 = time.perf_counter()
    m = extract_merchant(html)
    assert time.perf_counter() - t0 < 1.0
    assert m["eik"] == "123456786"
