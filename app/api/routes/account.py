from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import AuthenticatedUser, current_user
from app.worker.tasks import delete_account as delete_user_account


router = APIRouter(prefix="/account", tags=["account"])


@router.delete("", status_code=204)
def delete_account(user: AuthenticatedUser | None = Depends(current_user)) -> Response:
    if user is None:
        raise HTTPException(status_code=409, detail="Accounts are disabled when billing is disabled")
    delete_user_account(user.user_id)
    return Response(status_code=204)
