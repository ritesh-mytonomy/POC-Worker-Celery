"""Internal API authentication: every /internal/* request carries X-Internal-Key (R4.3)."""
import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings
from app.logging import get_logger

log = get_logger(__name__)


def require_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401 unless X-Internal-Key equals INTERNAL_API_KEY (constant-time compare)."""
    expected = get_settings().INTERNAL_API_KEY.encode("utf-8")
    supplied = (x_internal_key or "").encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        log.warning("internal_auth_failed", header_present=x_internal_key is not None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_internal_key")
