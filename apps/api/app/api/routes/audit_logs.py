"""Minimal audit-log read endpoint (P0-8: "access and change logs
retained and reviewable"). This is deliberately small — Phase 4.2 builds
the actual review interface (filtering, tamper-evidence, retention
policy). What exists here is the bare minimum needed for 0.2's RBAC
requirement to have something real to test: audit logs contain who
looked at what PHI, so only compliance/admin can read them — a
`doctor` token, including the doctor whose own actions are in the log,
must not be able to pull this list.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.schemas.audit_log import AuditLogOut

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
def list_audit_logs(
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("compliance", "admin")),
) -> list[AuditLogOut]:
    rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).all()
    return [AuditLogOut.model_validate(r) for r in rows]
