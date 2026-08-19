"""Typed contracts for the authentication boundary."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AuthBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FirebaseExchangeRequest(AuthBase):
    firebase_id_token: str = Field(min_length=1, max_length=8192)
    device_id: Optional[str] = Field(default=None, max_length=191)
    device_name: Optional[str] = Field(default=None, max_length=191)
    platform: Optional[str] = Field(default=None, max_length=32)
    app_version: Optional[str] = Field(default=None, max_length=32)


class RefreshRequest(AuthBase):
    refresh_token: str = Field(min_length=1, max_length=512)
    device_id: Optional[str] = Field(default=None, max_length=191)


class LogoutRequest(AuthBase):
    refresh_token: Optional[str] = Field(default=None, max_length=512)
    all_devices: bool = False


class AuthenticatedUser(BaseModel):
    user_id: str
    firebase_uid: str
    email: Optional[str] = None


class TokenResponse(AuthBase):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_expires_in: int
    user: Optional[AuthenticatedUser] = None


class FirebaseIdentity(BaseModel):
    uid: str
    email: Optional[str] = None
    email_verified: bool = False
    display_name: Optional[str] = None
