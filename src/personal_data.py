"""Columns whose names say they hold personal data.

``email``, ``customer_phone``, ``date_of_birth``: a column named for a kind of
personal data is tagged with it, so ``discover_schema`` and
``get_table_details`` show it, the ontology records it (``oba:personalData``)
and OBQC warns when a query returns it. Names, table names and types only: no
value is read.

The whole column name has to be the field, after optional words naming whose
or which it is (``customer_``, ``billing_``, ``primary_``): ``email_verified``,
``has_email`` and ``email_count`` are about an email, not one. Some fields are
personal anywhere (``email``, ``first_name``, ``ssn``); others only where the
column or its table names a person: ``address`` in ``customers`` or as
``billing_address``, not in ``stores``; ``name`` only in a table of people
(``customers.name``, not ``customer_segments.name``) or after a person
(``guest_name``). The idea follows NVIDIA Auto Ontology's PII rules, which
pass the columns they cannot decide to a model; these leave them untagged.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

#: Set to false to tag no column and warn of none.
DETECT_VARIABLE = "OBA_DETECT_PERSONAL_DATA"

EMAIL = "email"
PHONE = "phone"
NAME = "name"
ADDRESS = "address"
BIRTH_DATE = "birth_date"
GOVERNMENT_ID = "government_id"
PAYMENT_CARD = "payment_card"
BANK_ACCOUNT = "bank_account"
CREDENTIAL = "credential"
BIOMETRIC = "biometric"
NETWORK_ID = "network_id"
LOCATION = "location"
PERSONAL_ATTRIBUTE = "personal_attribute"
FINANCIAL = "financial"


def _fields(category: str, *names: str) -> dict[str, str]:
    return dict.fromkeys(names, category)


# Personal in any table. Keys are names with their separators removed:
# date_of_birth, dateOfBirth and DATEOFBIRTH are one field.
_ANYWHERE: dict[str, str] = {
    **_fields(EMAIL, "email", "emailaddress", "emailaddr"),
    **_fields(
        PHONE,
        "phone",
        "phonenumber",
        "phoneno",
        "phonenum",
        "telephone",
        "telephonenumber",
        "mobilephone",
        "mobilenumber",
        "mobileno",
        "cellphone",
        "cellnumber",
        "faxnumber",
    ),
    **_fields(
        NAME,
        "firstname",
        "lastname",
        "fullname",
        "middlename",
        "maidenname",
        "surname",
        "givenname",
        "familyname",
        "forename",
    ),
    **_fields(BIRTH_DATE, "dateofbirth", "birthdate", "birthday", "dob"),
    **_fields(
        GOVERNMENT_ID,
        "ssn",
        "socialsecurity",
        "socialsecuritynumber",
        "socialsecurityno",
        "passport",
        "passportnumber",
        "passportno",
        "driverslicense",
        "driverlicense",
        "driverslicence",
        "driverlicence",
        "drivinglicense",
        "drivinglicence",
        "driverslicensenumber",
        "driverlicensenumber",
        "nationalid",
        "nationalidnumber",
        "nationalinsurancenumber",
    ),
    **_fields(
        PAYMENT_CARD,
        "creditcard",
        "creditcardnumber",
        "creditcardno",
        "debitcard",
        "debitcardnumber",
        "cardnumber",
        "cardno",
        "ccnumber",
        "cvv",
        "cvc",
    ),
    **_fields(
        BANK_ACCOUNT, "iban", "bankaccount", "bankaccountnumber", "bankaccountno"
    ),
    **_fields(
        CREDENTIAL,
        "password",
        "passwd",
        "passwordhash",
        "passcode",
        "pincode",
        "securityanswer",
    ),
    **_fields(
        BIOMETRIC,
        "fingerprint",
        "faceprint",
        "voiceprint",
        "retinascan",
        "biometric",
        "biometrics",
    ),
    **_fields(NETWORK_ID, "ipaddress", "ipaddr", "ipv4", "ipv6", "macaddress"),
}

# Personal where the column or its table names a person. An address, a name,
# an age or a salary is also a store's, a product's, a job posting's.
_OF_A_PERSON: dict[str, str] = {
    **_fields(NAME, "name"),
    **_fields(
        ADDRESS,
        "address",
        "streetaddress",
        "street",
        "addressline",
        "postalcode",
        "postcode",
        "zipcode",
        "zip",
    ),
    **_fields(PHONE, "mobile", "cell", "fax"),
    **_fields(GOVERNMENT_ID, "taxid", "taxpayerid", "taxidnumber"),
    **_fields(BANK_ACCOUNT, "accountnumber"),
    **_fields(CREDENTIAL, "pin"),
    **_fields(LOCATION, "latitude", "longitude", "lat", "lng", "lon", "geolocation"),
    **_fields(
        PERSONAL_ATTRIBUTE,
        "gender",
        "sex",
        "age",
        "maritalstatus",
        "ethnicity",
        "race",
        "religion",
        "nationality",
        "sexualorientation",
    ),
    **_fields(FINANCIAL, "salary", "income", "annualincome", "creditscore"),
}

# Words before a field that say whose it is: customer_email, billing_address.
_PERSON_WORDS = frozenset(
    {
        "customer",
        "client",
        "user",
        "member",
        "employee",
        "staff",
        "patient",
        "guest",
        "contact",
        "person",
        "personal",
        "owner",
        "holder",
        "cardholder",
        "applicant",
        "student",
        "passenger",
        "traveler",
        "traveller",
        "subscriber",
        "recipient",
        "sender",
        "beneficiary",
        "spouse",
        "guardian",
        "emergency",
        "home",
        "private",
        "billing",
        "shipping",
        "mailing",
        "delivery",
    }
)

# Words before a field that say which one it is, and nothing of whose.
_QUALIFIERS = frozenset(
    {
        "primary",
        "secondary",
        "alternate",
        "alt",
        "main",
        "other",
        "work",
        "business",
        "office",
        "current",
        "previous",
        "prior",
        "old",
        "new",
        "backup",
        "recovery",
        "registered",
        "verified",
        "preferred",
        "legal",
    }
)

# Tables of people, by their nouns in the singular.
_PEOPLE = frozenset(
    {
        "person",
        "individual",
        "customer",
        "client",
        "user",
        "member",
        "employee",
        "staff",
        "worker",
        "patient",
        "guest",
        "contact",
        "applicant",
        "candidate",
        "student",
        "passenger",
        "traveler",
        "traveller",
        "subscriber",
        "holder",
        "cardholder",
        "beneficiary",
        "borrower",
        "resident",
        "citizen",
        "voter",
        "donor",
        "volunteer",
        "visitor",
        "lead",
        "prospect",
        "attendee",
        "participant",
        "respondent",
        "player",
        "driver",
        "rider",
        "shopper",
        "consumer",
        "author",
        "owner",
        "physician",
        "doctor",
        "nurse",
        "profile",
    }
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


def enabled() -> bool:
    """Whether columns are tagged and OBQC warns (``OBA_DETECT_PERSONAL_DATA``)."""
    return os.getenv(DETECT_VARIABLE, "true").strip().lower() == "true"


def _words(name: str) -> list[str]:
    """*name* in lower-case words: ``billingAddress_2`` is billing, address, 2."""
    return [
        word.lower()
        for part in _SEPARATORS.split(name)
        for word in _CAMEL.split(part)
        if word
    ]


def _singular(word: str) -> str:
    if word == "people":
        return "person"
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


@lru_cache(maxsize=1024)
def _people(table: str) -> tuple[bool, bool]:
    """Whether *table* holds people, and whether it names one at all.

    ``customers`` holds people; ``customer_addresses`` names one, so its street
    is someone's; ``customer_segments`` names one too, but its ``name`` is a
    segment's.
    """
    words = [_singular(word) for word in _words(table)]
    return bool(words) and words[-1] in _PEOPLE, any(w in _PEOPLE for w in words)


def category(table: str, column: str, data_type: str = "") -> str | None:
    """The kind of personal data *column* of *table* holds, by its name.

    Args:
        table: The table's name, without its schema.
        column: The column's name.
        data_type: Its SQL type; a boolean is a flag, never the data itself.

    Returns:
        One of the categories above, or None.
    """
    kind = data_type.strip().lower()
    if kind.startswith("bool") or kind == "bit":
        return None
    words = _words(column)
    holds_people, names_people = _people(table)
    for start in range(len(words)):
        before = words[:start]
        if any(w not in _PERSON_WORDS and w not in _QUALIFIERS for w in before):
            break
        whose = any(w in _PERSON_WORDS for w in before)
        field = "".join(words[start:])
        # address_line_2, phone2: a numbered one is the same field.
        for name in dict.fromkeys((field, field.rstrip("0123456789"))):
            if name in _ANYWHERE:
                return _ANYWHERE[name]
            if name in _OF_A_PERSON and (
                whose or (holds_people if name == "name" else names_people)
            ):
                return _OF_A_PERSON[name]
    return None


def tagged(table: str, columns: list[tuple[str, str]]) -> dict[str, str]:
    """*table*'s columns that hold personal data, with their categories.

    Args:
        table: The table's name.
        columns: (name, SQL type) of each column.

    Returns:
        Column name -> category, in column order; empty when detection is off.
    """
    if not enabled():
        return {}
    found = {}
    for name, data_type in columns:
        kind = category(table, name, data_type)
        if kind is not None:
            found[name] = kind
    return found
