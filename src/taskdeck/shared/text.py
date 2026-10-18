"""Plain text helpers shared by text-oriented jobs."""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def words(text: str) -> list[str]:
    """Split text into alphanumeric words, also breaking camelCase boundaries."""
    return _WORD.findall(_CAMEL_SPLIT.sub(" ", strip_accents(text)))


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
