import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import require_roles
from date_utils import now_ist_naive

router = APIRouter(prefix="/review", tags=["review"])

# Everyone logged in can see review data and approve/reject
_auth = Depends(require_roles("user", "admin", "superadmin"))

_MONTH_RE  = re.compile(r"^\d{4}-\d{2}$")
_SOURCES   = ("taiq", "manual")
_DECISIONS = ("approved", "rejected")


class DecisionBody(BaseModel):
    status: str
    comment: str = ""


class CommentBody(BaseModel):
    comment: str = ""


def _current_month() -> str:
    return now_ist_naive().strftime("%Y-%m")


def _valid_month(month: "str | None") -> "str | None":
    """Return a validated YYYY-MM string, defaulting to the current IST month."""
    if not month:
        return _current_month()
    return month if _MONTH_RE.match(month) else None


def _redact_tender(user: dict, tender: "dict | None") -> "dict | None":
    """Regular users see decisions but not WHO made them (admins do).
    Their own decisions stay attributed so the edit button still works."""
    if not tender or user.get("role") in ("admin", "superadmin"):
        return tender
    if str(tender.get("reviewed_by") or "") != str(user.get("sub")):
        tender["reviewed_by"]      = None
        tender["reviewed_by_name"] = None
    return tender


def _redact_comments(user: dict, comments: list) -> list:
    if user.get("role") in ("admin", "superadmin"):
        return comments
    uid = str(user.get("sub"))
    for c in comments:
        if str(c.get("user_id") or "") != uid:
            c["user_id"]  = None
            c["username"] = None
    return comments


@router.get("/summary")
async def review_summary(
    request: Request,
    month: str = Query(default=None),
    _user: dict = _auth,
):
    month = _valid_month(month)
    if not month:
        return JSONResponse({"error": "month must be YYYY-MM"}, status_code=400)
    db      = request.app.state.db
    summary = db.get_review_summary(month)
    return {"month": month, **summary, "months": db.get_review_months(6)}


@router.get("/tenders")
async def review_tenders(
    request: Request,
    month: str = Query(default=None),
    status: str = Query(default=None),
    q: str = Query(default=None),
    user: dict = _auth,
):
    month = _valid_month(month)
    if not month:
        return JSONResponse({"error": "month must be YYYY-MM"}, status_code=400)
    if status and status not in ("approved", "rejected", "pending"):
        return JSONResponse({"error": "Invalid status filter"}, status_code=400)
    tenders = request.app.state.db.get_review_tenders(month, status=status, q=q)
    tenders = [_redact_tender(user, t) for t in tenders]
    return {"month": month, "tenders": tenders, "total": len(tenders)}


@router.get("/tender/{source}/{tender_id}")
async def review_tender_detail(
    source: str,
    tender_id: int,
    request: Request,
    user: dict = _auth,
):
    if source not in _SOURCES:
        return JSONResponse({"error": "Invalid source"}, status_code=400)
    db     = request.app.state.db
    tender = db.get_review_tender(source, tender_id)
    if not tender:
        return JSONResponse({"error": "Tender not found"}, status_code=404)
    return {
        "tender": _redact_tender(user, tender),
        "comments": _redact_comments(user, db.get_tender_comments(source, tender_id)),
    }


@router.post("/tender/{source}/{tender_id}/decision")
async def review_decision(
    source: str,
    tender_id: int,
    body: DecisionBody,
    request: Request,
    user: dict = _auth,
):
    if source not in _SOURCES:
        return JSONResponse({"error": "Invalid source"}, status_code=400)
    if body.status not in _DECISIONS:
        return JSONResponse(
            {"error": "status must be 'approved' or 'rejected'"}, status_code=400
        )
    comment = (body.comment or "").strip()
    if not comment:
        return JSONResponse(
            {"error": "A comment explaining the decision is required"},
            status_code=400,
        )

    db     = request.app.state.db
    tender = db.get_review_tender(source, tender_id)
    if not tender:
        return JSONResponse({"error": "Tender not found"}, status_code=404)

    # Pending → anyone may decide. Already decided → only admins/superadmins
    # or the person who made the original decision may change it.
    if tender["review_status"] != "pending":
        is_admin    = user.get("role") in ("admin", "superadmin")
        is_reviewer = str(tender.get("reviewed_by") or "") == str(user.get("sub"))
        if not (is_admin or is_reviewer):
            return JSONResponse(
                {"error": "Only admins or the original reviewer can change this decision"},
                status_code=403,
            )

    user_id = int(user["sub"])
    db.set_tender_review(source, tender_id, body.status,
                         user_id, user["username"], comment)
    db.log_activity(
        user_id, user["username"], f"tender_{body.status}",
        details={"source": source, "tender_id": tender_id,
                 "title": tender.get("title", "")[:120],
                 "previous_status": tender["review_status"]},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "ok": True,
        "tender": _redact_tender(user, db.get_review_tender(source, tender_id)),
        "comments": _redact_comments(user, db.get_tender_comments(source, tender_id)),
    }


@router.post("/tender/{source}/{tender_id}/comment")
async def review_comment(
    source: str,
    tender_id: int,
    body: CommentBody,
    request: Request,
    user: dict = _auth,
):
    if source not in _SOURCES:
        return JSONResponse({"error": "Invalid source"}, status_code=400)
    comment = (body.comment or "").strip()
    if not comment:
        return JSONResponse({"error": "Comment cannot be empty"}, status_code=400)

    db     = request.app.state.db
    tender = db.get_review_tender(source, tender_id)
    if not tender:
        return JSONResponse({"error": "Tender not found"}, status_code=404)

    db.add_tender_comment(source, tender_id, int(user["sub"]),
                          user["username"], comment)
    db.log_activity(
        int(user["sub"]), user["username"], "tender_commented",
        details={"source": source, "tender_id": tender_id,
                 "title": tender.get("title", "")[:120]},
        ip_address=request.client.host if request.client else None,
    )
    return {"ok": True,
            "comments": _redact_comments(user, db.get_tender_comments(source, tender_id))}
