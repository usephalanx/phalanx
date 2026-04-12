"""Pydantic v2 request/response models for auth endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Schema for creating a new user (registration)."""

    email: EmailStr
    password: str = Field(min_length=6, description="Minimum 6 characters")
    display_name: str = Field(min_length=1, max_length=100)


class UserLogin(BaseModel):
    """Schema for user login."""

    email: EmailStr
    password: str


class UserResponse(BaseModel):
    """Public user representation returned by the API."""

    id: int
    email: str
    display_name: str
    avatar_url: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    """Response containing access and refresh tokens plus user info."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class RefreshRequest(BaseModel):
    """Schema for token refresh requests."""

    refresh_token: str = Field(description="A valid refresh JWT")


class RefreshResponse(BaseModel):
    """Response containing a new access/refresh token pair."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# Backward-compatible aliases — existing code imports these names.
RegisterRequest = UserCreate
LoginRequest = UserLogin
