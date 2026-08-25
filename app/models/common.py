"""Shared model primitives."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str, nbytes: int = 8) -> str:
    """Prefixed, URL-safe, collision-resistant id (e.g. ``camp_9f3a...``).

    Prefixed ids are worth the few extra bytes: they make logs and support
    tickets self-describing, and they make it impossible to pass a campaign id
    where a variant id is expected without it being obvious.
    """
    return f"{prefix}_{secrets.token_hex(nbytes)}"


class OS(StrEnum):
    ios = "ios"
    android = "android"
    web = "web"
    unknown = "unknown"


class Surface(StrEnum):
    native = "native"


class Base(BaseModel):
    """Base for all API/domain models."""

    model_config = ConfigDict(
        populate_by_name=True,
        use_enum_values=True,
        str_strip_whitespace=True,
        extra="forbid",
    )
