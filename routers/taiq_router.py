from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from auth import require_roles

router = APIRouter(prefix="/taiq", tags=["taiq"])

# All roles can read; admin+ for logs; superadmin-only for destructive ops
_auth       = Depends(require_roles("user", "admin", "superadmin"))
_admin_up   = Depends(require_roles("admin", "superadmin"))
_superadmin = Depends(require_roles("superadmin"))


@router.get("/status")
async def taiq_status(
    request: Request,
    _user: dict = _auth,
):
    run = request.app.state.db.get_latest_cron_run()
    return {"run": run}


@router.get("/history")
async def taiq_history(
    request: Request,
    _user: dict = _auth,
):
    dates = request.app.state.db.get_cron_dates()
    return {"dates": dates}


@router.get("/date/{date_str}")
async def taiq_date(
    date_str: str,
    request: Request,
    keyword: Optional[str] = Query(default=None),
    _user: dict = _auth,
):
    db  = request.app.state.db
    run = db.get_cron_run_by_date(date_str)
    if not run:
        return {"run": None, "tenders": []}
    tenders = db.get_cron_tenders(run["id"], keyword=keyword)
    return {"run": run, "tenders": tenders}


@router.get("/run/{run_id}")
async def taiq_run_detail(
    run_id: int,
    request: Request,
    _user: dict = _auth,
):
    run = request.app.state.db.get_cron_run(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return run


@router.get("/run/{run_id}/tenders")
async def taiq_run_tenders(
    run_id: int,
    request: Request,
    keyword: Optional[str] = Query(default=None),
    _user: dict = _auth,
):
    db      = request.app.state.db
    tenders = db.get_cron_tenders(run_id, keyword=keyword)
    return {"tenders": tenders, "total": len(tenders)}


@router.get("/logs")
async def taiq_logs(
    request: Request,
    run_id: Optional[int] = Query(default=None),
    _user: dict = _admin_up,
):
    import cron_runner
    db         = request.app.state.db
    current_id = cron_runner.get_current_run_id()

    # Default to current run when no run_id given
    target_id = run_id if run_id is not None else current_id

    if target_id is None:
        return {"run_id": None, "lines": [], "live": False}

    # Current run → serve live from in-memory buffer
    if target_id == current_id:
        return {
            "run_id": target_id,
            "lines": cron_runner.get_log_buffer(),
            "live": True,
        }

    # Historical → read from log file stored on disk
    run = db.get_cron_run(target_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    log_file = run.get("log_file")
    if not log_file:
        return {"run_id": target_id, "lines": ["No log file recorded for this run."], "live": False}

    log_path = Path(log_file)
    if not log_path.exists():
        return {"run_id": target_id, "lines": ["Log file not found on disk."], "live": False}

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"run_id": target_id, "lines": lines, "live": False}


@router.post("/stop")
async def taiq_stop(
    request: Request,
    _user: dict = _superadmin,
):
    import cron_runner
    db  = request.app.state.db
    run = db.get_latest_cron_run()
    if not run or run["status"] != "running":
        return JSONResponse(
            {"error": "No active run to stop"}, status_code=400
        )
    db.request_cron_stop(run["id"])
    cron_runner.request_stop()
    return {"ok": True, "run_id": run["id"],
            "message": "Stop signal sent — job will halt within a few seconds"}


@router.post("/run")
async def taiq_trigger(
    request: Request,
    _user: dict = _superadmin,
):
    import threading
    import cron_runner
    run = request.app.state.db.get_latest_cron_run()
    if run and run["status"] == "running":
        return JSONResponse(
            {"error": "A run is already in progress"}, status_code=400
        )
    threading.Thread(
        target=cron_runner.run_daily_job, daemon=True, name="taiq-manual"
    ).start()
    return {"ok": True, "message": "Manual run triggered"}
