from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    mfa_code: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ClinicianOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: str

    model_config = {"from_attributes": True}
