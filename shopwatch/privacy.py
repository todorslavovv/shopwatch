"""The one place that decides which field may be stored or shown.

Every GDPR decision in the domain check goes through is_field_publishable(). No other
module is allowed to decide on its own that a field is fine to keep: scattered checks
drift, and a single missed one is a leak. The store calls filter_record() before a
write, and the API calls it again before a response, so a field that slips past one
still has to get past the other.

Field-level ALLOWLIST, not a blocklist. A registry filing carries far more than any
purpose needs (CJEU C-200/23 concerned exactly the Bulgarian Commercial Register
publishing personal data that the law did not require). Anything not named here is
dropped, including fields that do not exist yet.
"""
from __future__ import annotations

import re
from pathlib import Path

PURPOSE = "merchant_identity_verification"

# Company facts. Publishable subject to the source's own terms.
ALLOWED_COMPANY_FIELDS = frozenset({
    "eik", "name", "legal_form", "status", "registration_date", "capital",
    "registered_address", "activity", "events", "vat_number",
    "corporate_email", "corporate_phone",
    "source", "source_url", "source_timestamp", "last_verified",
})

# People who act for a company - manager, representative, owner - as a register or the
# company itself publishes them. Personal data, and also legitimately public company
# information: kept with the source and verification state, never with an identifier.
PERSONAL_FIELDS = frozenset({"name", "role", "source", "source_url", "retrieved_at",
                             "last_verified", "status"})

# Never stored, never shown, whatever the source says.
FORBIDDEN_FIELDS = frozenset({
    "egn", "lnch", "personal_id", "id_number", "password", "credential",
    "authentication_token", "private_phone", "private_email", "birth_date",
    "home_address", "nationality", "signature",
})

# The controller's switch (PRIVACY.md section 3): people are shown unless this file
# exists. Objections and restrictions (section 4) apply per person and field either way.
# ponytail: file presence is the switch; a dated record if the decision is ever revised.
PEOPLE_OFF = Path(__file__).resolve().parent.parent / "PEOPLE-OFF"

# Mailbox names that belong to a function, not a person.
_ROLE_MAILBOX = re.compile(
    r"^(info|office|contact|contacts|kontakt|kontakti|sales|support|help|shop|store|"
    r"orders?|poruchki|zakazi|hello|admin|service|obsluzhvane|reklamacii|"
    r"reklamacia|returns|billing|accounting|legal|privacy|gdpr|dpo|noreply|"
    r"no-reply|web|webshop|online|marketing|team|enquiries|enquiry|inquiries)([._-]?\w*)?@", re.IGNORECASE)


# Mailboxes of the public bodies and couriers that Bulgarian legal boilerplate names
# ("КЗЛД: kzld@cpdp.bg"). They are published on the shop's pages but are not its contact.
_INSTITUTIONS = ("cpdp.bg", "kzp.bg", "nra.bg", "nap.bg", "europa.eu", "government.bg",
                 "gov.bg", "mvr.bg", "bnb.bg", "econt.com", "speedy.bg")


def personal_data_enabled() -> bool:
    return not PEOPLE_OFF.exists()


def is_field_publishable(field: str, source: str, purpose: str = PURPOSE, *,
                         subject: str = "company") -> bool:
    """True if `field` about `subject` ("company" | "person") may be kept and shown.

    `source` is accepted so that a source-specific licence restriction has a place to
    live; no source currently narrows the allowlist further.
    """
    f = (field or "").lower()
    if f in FORBIDDEN_FIELDS or purpose != PURPOSE:
        return False
    if subject == "person":
        return f in PERSONAL_FIELDS and personal_data_enabled()
    return f in ALLOWED_COMPANY_FIELDS


def classify_email(email: str | None, site: str | None) -> str | None:
    """What a mailbox published on the shop's own pages is, from its context:

    'corporate'          on the shop's domain, a function mailbox, or the shop's name
    'published_contact'  any other mailbox the shop publishes - bulbsandleds.com@gmail.com
                         is the shop's advertised contact, and a Gmail address is not
                         private for being a Gmail address
    'institution'        a public body or courier named in legal boilerplate: published
                         on the page, but not the shop's contact
    None                 not an e-mail address
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower()
    if site and (domain == site or domain.endswith("." + site)):
        return "corporate"
    if any(domain == d or domain.endswith("." + d) for d in _INSTITUTIONS):
        return "institution"
    if _ROLE_MAILBOX.match(email):
        return "corporate"         # info@abv.bg: a function mailbox
    # besteesrock@gmail.com for besteesrock.com: the shop's own name, not a person's.
    label = re.sub(r"[^a-z0-9]", "", (site or "").lower().split(".")[0])
    local = re.sub(r"[^a-z0-9]", "", email.rsplit("@", 1)[0].lower())
    if len(label) >= 5 and label in local:
        return "corporate"
    return "published_contact"


def filter_record(record: dict, source: str, *, subject: str = "company",
                  restricted: frozenset[str] = frozenset()) -> dict:
    """Drop every field that is not publishable, plus any field under restriction."""
    return {k: v for k, v in record.items()
            if k not in restricted and is_field_publishable(k, source, subject=subject)}


if __name__ == "__main__":        # self-check
    assert is_field_publishable("eik", "vies")
    assert not is_field_publishable("egn", "registry")
    assert not is_field_publishable("raw_filing", "registry")        # allowlist, not blocklist
    assert is_field_publishable("name", "registry", subject="person") == personal_data_enabled()
    assert not is_field_publishable("egn", "registry", subject="person")
    assert classify_email("office@shop.bg", "shop.bg") == "corporate"
    assert classify_email("info@abv.bg", "shop.bg") == "corporate"
    assert classify_email("ivan.petrov@gmail.com", "shop.bg") == "published_contact"
    assert classify_email("kzld@cpdp.bg", "shop.bg") == "institution"
    assert classify_email("carpethousebg@gmail.com", "carpethouse-bg.com") == "corporate"
    assert classify_email("enquiries.editbg@gmail.com", "editshop.bg") == "corporate"
    assert classify_email("ivan@gmail.com", "ivan.bg") == "published_contact"  # label too short
    assert filter_record({"eik": "1", "egn": "2", "x": 3}, "t") == {"eik": "1"}
    print("privacy self-check ok")
