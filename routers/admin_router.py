from fastapi import APIRouter, Depends, HTTPException, Request

from auth import require_roles, hash_password
from models import CreateUserRequest, UpdateUserRequest

router = APIRouter(prefix="/admin", tags=["admin"])

_ROLE_HIERARCHY = {"user": 0, "admin": 1, "superadmin": 2}
_CREATABLE_ROLES = {"admin": ["user"], "superadmin": ["admin", "user"]}


def _db(request: Request):
    return request.app.state.db


@router.get("/users")
async def list_users(
    request: Request,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    return _db(request).list_users(current_user["role"], int(current_user["sub"]))


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    request: Request,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    allowed = _CREATABLE_ROLES.get(current_user["role"], [])
    if body.role not in allowed:
        raise HTTPException(status_code=403, detail=f"Cannot create a '{body.role}' account")
    db = _db(request)
    team_id = body.team_id or current_user.get("team_id") or "cnk"
    team_name = body.team_name or current_user.get("team_name") or team_id.upper()
    try:
        uid = db.create_user(
            body.username, body.email, hash_password(body.password),
            body.role, int(current_user["sub"]),
            team_id=team_id, team_name=team_name,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    db.log_activity(
        int(current_user["sub"]), current_user["username"], "create_user",
        details={"new_user": body.username, "role": body.role},
        ip_address=request.client.host if request.client else None,
    )
    return {"id": uid, "username": body.username, "role": body.role}


@router.put("/users/{user_id}")
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    request: Request,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    db = _db(request)
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if current_user["role"] == "admin" and target.get("created_by") != int(current_user["sub"]):
        raise HTTPException(status_code=403, detail="Cannot modify this user")

    kwargs = {}
    if body.username is not None:
        kwargs["username"] = body.username
    if body.email is not None:
        kwargs["email"] = body.email
    if body.is_active is not None:
        kwargs["is_active"] = 1 if body.is_active else 0

    db.update_user(user_id, **kwargs)
    db.log_activity(
        int(current_user["sub"]), current_user["username"], "update_user",
        details={"target_id": user_id, "changes": kwargs},
        ip_address=request.client.host if request.client else None,
    )
    return {"ok": True}


@router.delete("/users/{user_id}")
async def deactivate_user(
    user_id: int,
    request: Request,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
):
    db = _db(request)
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if current_user["role"] == "admin" and target.get("created_by") != int(current_user["sub"]):
        raise HTTPException(status_code=403, detail="Cannot deactivate this user")
    if target["role"] == "superadmin":
        raise HTTPException(status_code=403, detail="Cannot deactivate a superadmin")

    db.update_user(user_id, is_active=0)
    db.log_activity(
        int(current_user["sub"]), current_user["username"], "deactivate_user",
        details={"target_id": user_id},
        ip_address=request.client.host if request.client else None,
    )
    return {"ok": True}
