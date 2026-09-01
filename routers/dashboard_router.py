import json

from fastapi import APIRouter, Depends, Request, Query

from auth import require_roles
from date_utils import now_ist_naive
from paths import APP_DIR

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Read-only overview — visible to every logged-in role
_auth = Depends(require_roles("user", "admin", "superadmin"))

# Sites that are not scraper entries in sites_config.json but still show up
# as a source on the dashboard.
_EXTRA_SITE_NAMES = {"taiq": "TAiQ"}


def _db(request: Request):
    return request.app.state.db


def _today() -> str:
    return now_ist_naive().strftime("%Y-%m-%d")


def _site_display_names(team_id: str) -> dict:
    """site_key -> display_name, read from the team's sites_config.json.

    sites_config.json is the single source of truth for site names: the
    dashboard used to carry its own hard-coded map, which silently left every
    site added after it was written showing as a raw uppercase key.
    """
    team_id = (team_id or "cnk").lower().strip()
    cfg_dir    = APP_DIR / "configs" / "teams" / team_id
    sites_file = cfg_dir / "sites_config.json"
    if not sites_file.exists():
        sites_file = APP_DIR / "sites_config.json"
    try:
        with open(sites_file, "r", encoding="utf-8") as f:
            sites = json.load(f)
    except Exception:
        sites = {}

    names = {
        key: (cfg.get("display_name") or key.upper())
        for key, cfg in sites.items()
        if isinstance(cfg, dict)
    }
    names.update(_EXTRA_SITE_NAMES)
    return names


def _label_rows(rows, names: dict):
    """Attach display_name to any list of dicts carrying a `site` key."""
    for row in rows or []:
        if isinstance(row, dict) and "site" in row:
            key = str(row.get("site") or "").lower()
            row["display_name"] = names.get(key) or key.upper()


def _label_sites(payload: dict, team_id: str) -> dict:
    """Attach a human display_name to every site-bearing row of a payload."""
    names = _site_display_names(team_id)
    _label_rows(payload.get("by_site"), names)
    _label_rows(payload.get("sessions"), names)
    return payload


@router.get("/stats")
async def get_stats(
    request: Request,
    date: str = Query(default=None),
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    current_user: dict = _auth,
):
    team_id = current_user.get("team_id", "cnk")
    stats = _db(request).get_stats_for_date(
        date_str=date, start_date=start_date, end_date=end_date, team_id=team_id
    )
    return _label_sites(stats, team_id)


@router.get("/report")
async def get_report(
    request: Request,
    days: int = Query(default=3, ge=1, le=90),
    current_user: dict = _auth,
):
    """Rolling report cards for the last N days (default 3, newest first).

    The cap is 90 rather than 14 because the same series also feeds the
    dashboard's Performance Overview chart, which offers a 30-day window.
    """
    team_id = current_user.get("team_id", "cnk")
    return {"days": _db(request).get_daily_report(days, team_id=team_id)}


@router.get("/overview")
async def get_overview(
    request: Request,
    current_user: dict = _auth,
):
    """All-time totals for the KPI row — independent of the calendar selection."""
    team_id = current_user.get("team_id", "cnk")
    return _label_sites(_db(request).get_overview_totals(team_id), team_id)


@router.get("/tenders")
async def get_tenders(
    request: Request,
    date: str = Query(default=None),
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    site: str = Query(default=None),
    keyword: str = Query(default=None),
    current_user: dict = _auth,
):
    team_id = current_user.get("team_id", "cnk")
    if start_date:
        tenders = _db(request).get_tenders_for_date_range(
            start_date, end_date or start_date, site, keyword, team_id=team_id
        )
    else:
        tenders = _db(request).get_tenders_for_date(
            date or _today(), site, keyword, team_id=team_id
        )
    _label_rows(tenders, _site_display_names(team_id))
    return tenders


@router.get("/dates")
async def get_dates(
    request: Request,
    current_user: dict = _auth,
):
    team_id = current_user.get("team_id", "cnk")
    return _db(request).get_dates_with_data(team_id=team_id)


@router.get("/taiq-report")
async def get_taiq_report(
    request: Request,
    date: str = Query(default=None),
    start_date: str = Query(default=None),
    end_date: str = Query(default=None),
    current_user: dict = _auth,
):
    """Detailed TAiQ run analytics, rejection funnel, per-site health, and keyword leaderboard."""
    team_id = current_user.get("team_id", "cnk")
    return _db(request).get_taiq_detailed_report(date_str=date, start_date=start_date, end_date=end_date, team_id=team_id)
