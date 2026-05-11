from datetime import date as date_cls

from fastapi import APIRouter, Depends, Request, Query

from auth import require_roles

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _db(request: Request):
    return request.app.state.db


def _today() -> str:
    return date_cls.today().isoformat()


@router.get("/stats")
async def get_stats(
    request: Request,
    date: str = Query(default=None),
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    return _db(request).get_stats_for_date(date or _today())


@router.get("/tenders")
async def get_tenders(
    request: Request,
    date: str = Query(default=None),
    site: str = Query(default=None),
    keyword: str = Query(default=None),
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    return _db(request).get_tenders_for_date(date or _today(), site, keyword)


@router.get("/dates")
async def get_dates(
    request: Request,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    return _db(request).get_dates_with_data()
