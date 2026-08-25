from fastapi import FastAPI

from app.api.routes import audit_logs, auth, consent, encounters, notes, patients
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title="Remedy Scribe API", version="0.1.0")

API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(patients.router, prefix=API_PREFIX)
app.include_router(encounters.router, prefix=API_PREFIX)
app.include_router(consent.router, prefix=API_PREFIX)
app.include_router(notes.router, prefix=API_PREFIX)
app.include_router(audit_logs.router, prefix=API_PREFIX)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": settings.environment}
