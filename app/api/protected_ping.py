from fastapi import APIRouter, Depends

from app.core.deps import require_user

router = APIRouter(prefix="/api", tags=["internal"])


@router.get("/_ping")
def ping(_: bool = Depends(require_user)) -> dict:
    return {"pong": True}
