from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    ok: bool


class MeResponse(BaseModel):
    authenticated: bool
