"""Session management."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..deps import IdentityDep, SessionServiceDep
from ..models.session import Session, SessionCreateRequest, SessionCreateResponse

router = APIRouter(tags=["sessions"])


@router.post("/session/create", response_model=SessionCreateResponse)
async def create_session(
    body: SessionCreateRequest,
    identity: IdentityDep,
    sessions: SessionServiceDep,
) -> SessionCreateResponse:
    """Resolve the caller to a stable user and return their session id.

    Resolve-or-create: within the inactivity window the same caller gets the
    same `session_id` back, which is what makes the 30s rule observable.
    """
    session = await sessions.resolve_or_create(body.ppid, identity.ip)
    return SessionCreateResponse(session_id=session.session_id)


@router.get("/session/{session_id}", response_model=Session, tags=["sessions"])
async def get_session(session_id: str, sessions: SessionServiceDep) -> Session:
    """Inspect a session. Useful for debugging and for the demo script."""
    session = await sessions.get(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"session {session_id} not found")
    return session
