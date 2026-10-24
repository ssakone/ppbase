"""ozzo-validation rules of the PocketBase 0.22 forms, with Go string semantics.

PocketBase 0.22.34 validates its forms with ozzo-validation v4.3.0 on Go
strings (``v0.22.34 go.mod``).  Three details change the answers; each was
measured on the official 0.22.34 binary with synthetic data:

- ``validation.Length(min, max)`` counts UTF-8 bytes, not characters
  (ozzo ``LengthRule.Validate`` uses ``LengthOfValue``, a ``reflect`` ``Len()``;
  only ``RuneLength`` counts runes).  ``forms/admin_password_reset_confirm.go:45``
  (``Length(10, 72)``) accepts five two-byte characters and refuses 37.
- Go's JSON decoder turns every lone UTF-16 surrogate escape into U+FFFD
  (three bytes), where Python's ``json`` keeps the surrogate, which cannot
  even be encoded to UTF-8.
- ``is.EmailFormat`` is govalidator ``IsEmail`` (ozzo-validation v4.3.0
  ``is/rules.go``): the ``rxEmail`` pattern of govalidator ``a9d515a09cc2``
  ``patterns.go``, as compiled into the 0.22.34 binary.  It is implemented
  below without a regular expression: the pattern has nested, overlapping
  repetitions on which Python's backtracking engine can take exponential
  time, where Go's RE2 engine is linear.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_SURROGATES = re.compile("[\ud800-\udfff]")


def go_string(value: str) -> str:
    """``value`` as Go's JSON decoder gives it: lone surrogates become U+FFFD."""
    return _SURROGATES.sub("�", value)


def byte_length(value: str) -> int:
    """Go ``len`` of the string, the unit of ozzo ``validation.Length``."""
    return len(go_string(value).encode("utf-8"))


def length_error(minimum: int, maximum: int) -> dict[str, str]:
    """ozzo ``validation.Length(minimum, maximum)`` error, both bounds set and different."""
    return {
        "code": "validation_length_out_of_range",
        "message": f"The length must be between {minimum} and {maximum}.",
    }


# ---------------------------------------------------------------------------
# govalidator IsEmail
# ---------------------------------------------------------------------------
#
# The pattern, with U = [\x{00A0}-\x{D7FF}\x{F900}-\x{FDCF}\x{FDF0}-\x{FFEF}]:
#
#   ^( C+(\.C+)*  |  ( " FWS? X )* FWS? " ) @ ( (A | A M* A) \. )+ (B | B N* B) \.? $
#
#   C   = [a-zA-Z] | \d | [!#$%&'*+\-/=?^_`{|}~] | U
#   FWS = ((\x20|\x09)* \x0d\x0a)? (\x20|\x09)+
#   X   = [\x01-\x08\x0b\x0c\x0e-\x1f\x7f] | \x21 | [\x23-\x5b] | [\x5d-\x7e] | U
#         | \( [\x01-\x09\x0b\x0c\x0d-\x7f]
#   A   = [a-zA-Z] | \d | U                M = A | - | \. | _ | ~
#   B   = [a-zA-Z] | U                     N = B | \d | - | _ | ~
#
# Its parentheses put the opening quote inside the repeated group, so the
# second alternative is not an RFC 5322 quoted string: '"john"@x.io' is
# refused and '"j"@x.io' or '"@x.io' accepted, as the binary does.  Go's \d
# is ASCII and its "$" only matches at the very end (no final newline).


def _in_u(char: str) -> bool:
    code = ord(char)
    return 0x00A0 <= code <= 0xD7FF or 0xF900 <= code <= 0xFDCF or 0xFDF0 <= code <= 0xFFEF


def _alpha(char: str) -> bool:
    return ("a" <= char <= "z") or ("A" <= char <= "Z")


def _digit(char: str) -> bool:
    return "0" <= char <= "9"


_ATOM_SPECIALS = frozenset("!#$%&'*+-/=?^_`{|}~")


def _atom_char(char: str) -> bool:  # C
    return _alpha(char) or _digit(char) or char in _ATOM_SPECIALS or _in_u(char)


def _dot_atom(local: str) -> bool:  # C+(\.C+)*
    parts = local.split(".")
    return all(parts) and all(_atom_char(char) for part in parts for char in part)


def _quoted_char(char: str) -> bool:  # X without the "(" + character pair
    code = ord(char)
    return (
        0x01 <= code <= 0x08
        or code in (0x0B, 0x0C)
        or 0x0E <= code <= 0x1F
        or code == 0x7F
        or code == 0x21
        or 0x23 <= code <= 0x5B
        or 0x5D <= code <= 0x7E
        or _in_u(char)
    )


def _paired_char(char: str) -> bool:  # the character after "(" in X
    code = ord(char)
    return 0x01 <= code <= 0x09 or code in (0x0B, 0x0C) or 0x0D <= code <= 0x7F


# States of ( " FWS? X )* FWS? " : S starts a repetition or the tail; R is
# after a repetition's quote; R_* and T_* read the folding white space of a
# repetition or of the tail (PRE: blanks before CRLF, CR, LF, POST: blanks
# after it or without it); RP follows a "(" that may start a pair.
_S, _R, _R_PRE, _R_CR, _R_LF, _R_POST, _RP, _T_PRE, _T_CR, _T_LF, _T_POST, _END = range(12)
_BLANKS = (" ", "\t")


def _quote_sequence(local: str) -> bool:
    states = {_S}
    for char in local:
        following: set[int] = set()
        blank = char in _BLANKS
        for state in states:
            if state == _S:
                if char == '"':
                    following.update((_R, _END))
                elif blank:
                    following.update((_T_PRE, _T_POST))
                elif char == "\r":
                    following.add(_T_CR)
            elif state in (_R, _R_POST):
                if blank:
                    following.add(_R_POST)
                    if state == _R:
                        following.add(_R_PRE)
                elif char == "\r" and state == _R:
                    following.add(_R_CR)
                elif _quoted_char(char):
                    following.add(_S)
                    if char == "(":
                        following.add(_RP)
            elif state == _R_PRE:
                if blank:
                    following.add(_R_PRE)
                elif char == "\r":
                    following.add(_R_CR)
            elif state == _R_CR:
                if char == "\n":
                    following.add(_R_LF)
            elif state == _R_LF:
                if blank:
                    following.add(_R_POST)
            elif state == _RP:
                if _paired_char(char):
                    following.add(_S)
            elif state == _T_PRE:
                if blank:
                    following.add(_T_PRE)
                elif char == "\r":
                    following.add(_T_CR)
            elif state == _T_CR:
                if char == "\n":
                    following.add(_T_LF)
            elif state == _T_LF:
                if blank:
                    following.add(_T_POST)
            elif state == _T_POST:
                if blank:
                    following.add(_T_POST)
                elif char == '"':
                    following.add(_END)
        if not following:
            return False
        states = following
    return _END in states


def _label_edge(char: str) -> bool:  # A
    return _alpha(char) or _digit(char) or _in_u(char)


def _label_inner(char: str) -> bool:  # M
    return _label_edge(char) or char in "-._~"


def _tld_edge(char: str) -> bool:  # B
    return _alpha(char) or _in_u(char)


def _tld_inner(char: str) -> bool:  # N
    return _tld_edge(char) or _digit(char) or char in "-_~"


def _framed(value: str, edge: Callable[[str], bool], inner: Callable[[str], bool]) -> bool:
    """``edge | edge inner* edge``."""
    return (
        bool(value)
        and edge(value[0])
        and edge(value[-1])
        and all(inner(char) for char in value[1:-1])
    )


def _domain(domain: str) -> bool:
    # ((A | A M* A)\.)+ is the same language as (A | A M* A)\. because M holds
    # the dot and A, and the TLD holds no dot: the TLD is what follows the last
    # dot once the optional final dot is removed.
    if domain.endswith("."):
        domain = domain[:-1]
    head, dot, tld = domain.rpartition(".")
    return (
        bool(dot)
        and _framed(head, _label_edge, _label_inner)
        and _framed(tld, _tld_edge, _tld_inner)
    )


def is_email_format(value: str) -> bool:
    """ozzo ``is.EmailFormat`` (govalidator ``IsEmail``) of PocketBase 0.22.34."""
    value = go_string(value)
    # The domain cannot hold "@", so the address splits at the last one.
    local, at, domain = value.rpartition("@")
    if not at:
        return False
    return (_dot_atom(local) or _quote_sequence(local)) and _domain(domain)
