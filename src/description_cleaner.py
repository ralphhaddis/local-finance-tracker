"""Local, deterministic cleanup for noisy bank transaction descriptions.

This derives model text only. It never changes the raw transaction description.
"""

from __future__ import annotations

import re
import unicodedata


CLEANER_VERSION = "2"

_CARD_PREFIX = re.compile(
    r"^\s*\d{3,6}\s+\d{1,2}[A-Z]{3}\d{2}(?:\s+(?:CD|C|D))?\s*,?\s*",
    re.IGNORECASE,
)
_PAYROLL_PREFIX = re.compile(r"^\s*\d{1,2}[A-Z]{3}\s+(GRS)\b\s*", re.IGNORECASE)
_ACCOUNT_PREFIX = re.compile(r"^\s*(?:to|from)\s+a/c\s+\d+\s*,?\s*", re.IGNORECASE)
_PAYMENT_TAIL = re.compile(
    r"\s*,\s*(?:via\s+(?:mobile|online|telephone|internet)\b|"
    r"fp\s+\d{1,2}/\d{1,2}/\d{2,4}\b|fpo\b|faster\s+payment\b|"
    r"standing\s+order\b|direct\s+debit\b).*$",
    re.IGNORECASE,
)
_CARD_DATE = re.compile(r"\b\d{1,2}[A-Z]{3}\d{2}\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_SPACES = re.compile(r"\s+")


def clean_description(raw_description: str) -> str:
    """Return a compact merchant or payee text suitable for local classification."""
    text = unicodedata.normalize("NFKC", raw_description or "")
    is_card_transaction = _CARD_PREFIX.match(text) is not None
    text = _CARD_PREFIX.sub("", text, count=1)
    text = _PAYROLL_PREFIX.sub(r"\1 ", text, count=1)
    text = _ACCOUNT_PREFIX.sub("", text, count=1)
    text = _PAYMENT_TAIL.sub("", text, count=1)

    # Card exports put merchant first and location/terminal details after commas.
    # For person-to-person entries retain a second segment which may contain the payment memo.
    segments = [segment.strip() for segment in text.split(",") if segment.strip()]
    if len(segments) > 1:
        text = " ".join(segments[:1] if is_card_transaction else segments[:2])
    elif segments:
        text = segments[0]

    text = _CARD_DATE.sub(" ", text)
    text = _LONG_NUMBER.sub(" ", text)
    if is_card_transaction:
        text = re.sub(r"\b(?:gb|us|ie)\b.*$", " ", text, flags=re.IGNORECASE)
    text = text.replace("*", " ")
    text = text.casefold()
    text = re.sub(r"[^\w&'.-]+", " ", text, flags=re.UNICODE)
    return _SPACES.sub(" ", text).strip(" .,-_")
