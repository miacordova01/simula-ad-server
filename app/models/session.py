"""Session models.

A session is a continuous engagement window for one user. The user is resolved
from `ppid` when supplied, otherwise from the request IP.

The "mint a new session only after 30s of inactivity" rule is implemented as a
Redis key with a 30s TTL keyed on the resolved user. Every serve refreshes the
TTL. If the key has expired the next resolve mints a fresh session id. Letting
Redis expiry BE the inactivity rule means there is no sweeper job and no clock
skew between "what the API thinks" and "what the store holds".
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import Base, new_id, utcnow


class SessionCreateRequest(Base):
    # Publisher-provided pseudonymous id. Optional -- we fall back to IP.
    ppid: str | None = Field(default=None, max_length=200)


class SessionCreateResponse(Base):
    session_id: str


class Session(Base):
    session_id: str = Field(default_factory=lambda: new_id("sess"))
    # How the user was identified: "ppid" or "ip".
    user_key: str
    resolved_from: str
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    serve_count: int = 0
