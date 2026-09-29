"""audit_log repository: the LLD's record of governance events (upload-ingest-merge U8.4)."""
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import bindparam


def write(session: Session, *, organization_id: uuid.UUID, user_id: int, action: str, entity_type: str,
          entity_id: str, details: dict[str, Any]) -> None:
    """Add one audit row in the caller's transaction."""
    session.execute(text("""
        INSERT INTO audit_log (organization_id, user_id, action, entity_type, entity_id, details)
        VALUES (:org, :user_id, :action, :entity_type, :entity_id, :details)""")
        .bindparams(bindparam("details", type_=JSONB)),
        {"org": organization_id, "user_id": user_id, "action": action, "entity_type": entity_type,
         "entity_id": entity_id, "details": details})
