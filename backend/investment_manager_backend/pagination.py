"""Shared cursor pagination primitives for collection endpoints."""

from __future__ import annotations

import base64
import binascii
from typing import Generic, TypeVar

from pydantic import BaseModel


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Standard Life collection response."""

    items: list[T]
    total: int
    next_cursor: str | None


def encode_cursor(offset: int) -> str:
    """Encode an offset without exposing its representation to API clients."""
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """Decode an opaque offset cursor, raising ``ValueError`` when invalid."""
    if cursor is None:
        return 0

    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
        offset = int(decoded)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("cursore non valido") from exc

    if offset < 0 or str(offset) != decoded:
        raise ValueError("cursore non valido")
    return offset
