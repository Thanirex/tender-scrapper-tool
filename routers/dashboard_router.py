from fastapi import APIRouter, Depends, Request, Query

from auth import require_roles
from date_utils import now_ist_naive

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Read-only overview — visible to every logged-in role
_auth = Depends(require_roles("user", "admin", "superadmin"))


def _db(request: Request):
    return request.app.state.db


def _today() -> str:
    return now_ist_naive().strftime("%Y-%m-%d")


@router.get("/stats")
async def get_stats(
    request: Request,
    date: str = Query(default=None),
    current_user: dict = _auth,
):
    return _db(request).get_stats_for_date(date or _today())


@router.get("/report")
async def get_report(
    request: Request,
    days: int = Query(default=3, ge=1, le=14),
    current_user: dict = _auth,
):
    """Rolling report cards for the last N days (default 3, newest first)."""
    return {"days": _db(request).get_daily_report(days)}


@router.get("/tenders")
async def get_tenders(
    request: Request,
    date: str = Query(default=None),
    site: str = Query(default=None),
    keyword: str = Query(default=None),
    current_user: dict = _auth,
):
    return _db(request).get_tenders_for_date(date or _today(), site, keyword)


@router.get("/dates")
async def get_dates(
    request: Request,
    current_user: dict = _auth,
):
    return _db(request).get_dates_with_data()
