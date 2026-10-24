"""ozzo-validation rules of PocketBase 0.22 forms (ppbase/services/legacy_validation.py).

The expected answers were measured on the official PocketBase 0.22.34 binary
(``POST /api/admins/request-password-reset``, synthetic addresses only): ``True``
is a 204, ``False`` a ``validation_is_email`` error.  The regular expression
below is govalidator's ``rxEmail``, copied from the 0.22.34 binary (the only
string of the executable holding ``\\x{00A0}-\\x{D7FF}``) and identical to the
``Email`` pattern of govalidator ``a9d515a09cc2`` ``patterns.go:7``; it serves
as the oracle of the hand-written matcher on short inputs.
"""

from __future__ import annotations

import itertools
import random
import re
import time

import pytest

from ppbase.services.legacy_validation import (
    byte_length,
    go_string,
    is_email_format,
    length_error,
)

GOVALIDATOR_EMAIL = (
    r"^(((([a-zA-Z]|\d|[!#\$%&'\*\+\-\/=\?\^_`{\|}~]|[\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}"
    r"\x{FDF0}-\x{FFEF}])+(\.([a-zA-Z]|\d|[!#\$%&'\*\+\-\/=\?\^_`{\|}~]|[\x{00A0}-\x{D7FF}"
    r"\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])+)*)|((\x22)((((\x20|\x09)*(\x0d\x0a))?(\x20|\x09)+)?"
    r"(([\x01-\x08\x0b\x0c\x0e-\x1f\x7f]|\x21|[\x23-\x5b]|[\x5d-\x7e]|[\x{00A0}-\x{D7FF}"
    r"\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])|(\([\x01-\x09\x0b\x0c\x0d-\x7f]|[\x{00A0}-\x{D7FF}"
    r"\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}]))))*(((\x20|\x09)*(\x0d\x0a))?(\x20|\x09)+)?(\x22)))@"
    r"((([a-zA-Z]|\d|[\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])|(([a-zA-Z]|\d|"
    r"[\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])([a-zA-Z]|\d|-|\.|_|~|[\x{00A0}-"
    r"\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])*([a-zA-Z]|\d|[\x{00A0}-\x{D7FF}\x{F900}-"
    r"\x{FDCF}\x{FDF0}-\x{FFEF}])))\.)+(([a-zA-Z]|[\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-"
    r"\x{FFEF}])|(([a-zA-Z]|[\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])([a-zA-Z]|\d|"
    r"-|_|~|[\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])*([a-zA-Z]|[\x{00A0}-\x{D7FF}"
    r"\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}])))\.?$"
)


def _python_oracle() -> re.Pattern[str]:
    # Go's \x{XXXX} is Python's \uXXXX; Go's \d is ASCII (re.ASCII) and its
    # "$" only matches at the very end (\Z).
    pattern = re.sub(r"\\x\{([0-9A-Fa-f]{4})\}", lambda m: "\\u" + m.group(1), GOVALIDATOR_EMAIL)
    assert pattern.endswith("$")
    return re.compile(pattern[:-1] + r"\Z", re.ASCII)


ORACLE = _python_oracle()

LABELS = ".".join(["b" * 60] * 3)
MEASURED = {
    # Plain, specials, unicode and the RFC corner cases.
    "nobody@c11.test": True,
    "NoBody@C11.TEST": True,
    "!#$%&'*+-/=?^_`{|}~@c11.test": True,
    "user+tag@c11.test": True,
    "jos\u00e9@c11.test": True,
    "user@\u00e9xample.test": True,
    "\u0661@c11.test": True,
    "a" * 64 + "@c11.test": True,
    "a" * 65 + "@c11.test": True,
    "a" * 64 + "@" + LABELS + "." + "c" * 7: True,
    # The three addresses of the review: refused by 0.22.34.
    "someone@[127.0.0.1]": False,
    "us..er@example.com": False,
    ".user@example.com": False,
    "user.@c11.test": False,
    # govalidator's quoted alternative puts the opening quote inside the
    # repeated group: one character per quote pair, so most RFC quoted local
    # parts are refused and a few odd ones accepted.
    '"john"@c11.test': False,
    '"john doe"@c11.test': False,
    '""@c11.test': False,
    '"a@b"@c11.test': False,
    '"a\\b"@c11.test': False,
    '"a(\\b"@c11.test': False,
    '"a("b"@c11.test': False,
    '"a("@c11.test': False,
    '"a\tb"@c11.test': False,
    '"a\r\n b"@c11.test': False,
    '"a\r\nb"@c11.test': False,
    '"a\nb"@c11.test': False,
    '"a\r\n \r\n b"@c11.test': False,
    '"a\x01b"@c11.test': False,
    '"ab@c11.test': False,
    '" "@c11.test': False,
    '"j"@c11.test': True,
    '"@c11.test': True,
    '"(""@c11.test': True,
    ' "@c11.test': True,
    # Domains.
    "user@localhost": False,
    "user@c11.test.": True,
    "user@c11.test..": False,
    "user@c11..test": False,
    "user@.c11.test": False,
    "user@-c11.test": False,
    "user@c11-.test": False,
    "user@c_11.test": True,
    "user@c~11.test": True,
    "a@b-c.test": True,
    "user@c11.t3st": True,
    "user@c11.test1": False,
    "user@c11.1test": False,
    "user@c11.te-st": True,
    "user@c11.te_st": True,
    "a@b.t~t": True,
    "user@c11.t": True,
    "user@123.test": True,
    "a@1.2.3.4": False,
    "a@b.": False,
    "a@b.\u00e9": True,
    "user@c11.t\uff11st": True,
    "user@c\u0661.test": True,
    # Shapes.
    "user name@c11.test": False,
    "user.c11.test": False,
    "a@b@c11.test": False,
    "user@c11.test\n": False,
    "@c11.test": False,
    "user@": False,
    # Unicode ranges of govalidator (U+00A0-U+D7FF, U+F900-U+FDCF, U+FDF0-U+FFEF).
    "a\u00a0b@c11.test": True,
    "a\u009fb@c11.test": False,
    "a\ud7ffb@c11.test": True,
    "a\ue000b@c11.test": False,
    "a\uf900b@c11.test": True,
    "a\ufdcfb@c11.test": True,
    "a\ufdd0b@c11.test": False,
    "a\ufdf0b@c11.test": True,
    "a\uffefb@c11.test": True,
    "a\ufff0b@c11.test": False,
    "a\U0001f600b@c11.test": False,
    # A lone surrogate escape is U+FFFD for Go.
    "\ud800@c11.test": False,
}


@pytest.mark.parametrize(("address", "accepted"), MEASURED.items())
def test_is_email_format_matches_the_022_binary(address: str, accepted: bool) -> None:
    assert is_email_format(address) is accepted
    assert bool(ORACLE.match(go_string(address))) is accepted


def test_matcher_agrees_with_the_govalidator_pattern() -> None:
    """Exhaustive short local parts and domains, then a seeded random sample."""
    local_alphabet = ['"', " ", "\t", "\r", "\n", "a", "(", "\\", ".", "@", "\x01", "\u00a0", ")"]
    domain_alphabet = ["a", "1", "-", ".", "_", "~", "\u00e9", "@", "\uff11"]
    checked = 0
    for size in range(5):
        for combo in itertools.product(local_alphabet, repeat=size):
            address = "".join(combo) + "@x.io"
            assert is_email_format(address) is bool(ORACLE.match(address)), repr(address)
            checked += 1
    for size in range(6):
        for combo in itertools.product(domain_alphabet, repeat=size):
            address = "u@" + "".join(combo)
            assert is_email_format(address) is bool(ORACLE.match(address)), repr(address)
            checked += 1
    rnd = random.Random(20261024)
    edges = "\ud7ff\uf900\ufdcf\ufdd0\ufdf0\uffef\ufff0\U0001f600\x00"
    alphabet = local_alphabet + domain_alphabet + list("bZ9!#`{\x7f\x0b" + edges)
    for _ in range(50_000):
        address = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
        if rnd.random() < 0.5 and "@" not in address:
            address += "@" + "".join(rnd.choice(domain_alphabet) for _ in range(rnd.randint(1, 6)))
        assert is_email_format(address) is bool(ORACLE.match(address)), repr(address)
        checked += 1
    assert checked == 147_371


def test_matcher_is_linear_on_hostile_input() -> None:
    """The pattern backtracks exponentially in Python's re; the matcher must not."""
    hostile = [
        "a@" + "a." * 20_000 + "!",
        "a@" + "a-" * 20_000 + "a.1",
        '"' + '"\r\n a' * 10_000 + "@x.io",
        "a" * 50_000 + "@" + "b" * 50_000,
    ]
    started = time.monotonic()
    for address in hostile:
        assert is_email_format(address) is False
    assert time.monotonic() - started < 2.0


def test_go_strings_and_byte_lengths() -> None:
    assert go_string("a\ud800b\udfff") == "a\ufffdb\ufffd"
    assert byte_length("a" * 10) == 10
    assert byte_length("\u00e9" * 5) == 10  # 5 characters, 10 bytes: valid for Length(10, 72)
    assert byte_length("\u00e9" * 37) == 74  # 37 characters, 74 bytes: refused
    assert byte_length("\U0001f600" * 18) == 72
    assert byte_length("\ud800" * 10) == 30  # Go reads each lone surrogate as U+FFFD
    assert length_error(10, 72) == {
        "code": "validation_length_out_of_range",
        "message": "The length must be between 10 and 72.",
    }
