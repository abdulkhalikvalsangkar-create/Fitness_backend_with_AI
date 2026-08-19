"""Authentication endpoints for Firebase-to-backend token exchange."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from apps.api.security import authenticate
from packages.auth.service import AuthService, AuthServiceError
from packages.domain.auth import FirebaseExchangeRequest, LogoutRequest, RefreshRequest
from packages.storage.db import get_db

router = APIRouter(prefix="/auth", tags=["authentication"])


def _auth_error(exc: AuthServiceError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc), headers={"WWW-Authenticate": "Bearer"})


@router.post("/exchange")
def exchange(request: FirebaseExchangeRequest, session: Session = Depends(get_db)):
    try:
        result = AuthService(session).exchange_firebase_token(request.firebase_id_token, device_id=request.device_id, device_name=request.device_name, platform=request.platform, app_version=request.app_version)
    except AuthServiceError as exc:
        raise _auth_error(exc) from exc
    return {"success": True, **result.model_dump(mode="json")}


@router.post("/refresh")
def refresh(request: RefreshRequest, session: Session = Depends(get_db)):
    try:
        result = AuthService(session).refresh(request.refresh_token, request.device_id)
    except AuthServiceError as exc:
        raise _auth_error(exc) from exc
    return {"success": True, **result.model_dump(mode="json")}


@router.post("/logout")
def logout(request: LogoutRequest, http_request: Request, session: Session = Depends(get_db)):
    principal = authenticate(http_request, {})
    removed = AuthService(session).logout_all(principal.user_id) if request.all_devices else int(AuthService(session).logout(principal.user_id, request.refresh_token))
    return {"success": True, "revoked": removed}


@router.post("/logout-all")
def logout_all(http_request: Request, session: Session = Depends(get_db)):
    principal = authenticate(http_request, {})
    return {"success": True, "revoked": AuthService(session).logout_all(principal.user_id)}
