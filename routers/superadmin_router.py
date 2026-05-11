from fastapi import APIRouter, Depends, Request, Query

from auth import require_roles

router = APIRouter(prefix="/superadmin", tags=["superadmin"])


def _db(request: Request):
    return request.app.state.db


@router.get("/logs")
async def get_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, le=200),
    user_id: int = Query(default=None),
    current_user: dict = Depends(require_roles("superadmin")),
):
    return _db(request).get_activity_logs(page, limit, user_id)
