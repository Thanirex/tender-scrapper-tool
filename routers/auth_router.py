from fastapi import APIRouter, Depends, HTTPException, Request

from auth import verify_password, create_access_token, get_current_user
from models import LoginRequest

router = APIRouter(prefix="/auth", tags=["auth"])


def _db(request: Request):
    return request.app.state.db


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    db = _db(request)
    user = db.get_user_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    team_id = user.get("team_id") or "cnk"
    team_name = user.get("team_name") or team_id.upper()
    token = create_access_token(user["id"], user["username"], user["role"], team_id=team_id, team_name=team_name)
    db.log_activity(
        user["id"], user["username"], "login",
        ip_address=request.client.host if request.client else None,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "team_id": team_id,
            "team_name": team_name,
        },
    }


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return current_user
