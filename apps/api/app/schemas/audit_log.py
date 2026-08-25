from datetime import datetime

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    id: str
    actor_clinician_id: str | None
    action: str
    entity_type: str
    entity_id: str
    created_at: datetime

    model_config = {"from_attributes": True}
