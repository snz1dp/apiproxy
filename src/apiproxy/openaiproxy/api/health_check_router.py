# /*********************************************
#                    _ooOoo_
#                   o8888888o
#                   88" . "88
#                   (| -_- |)
#                   O\  =  /O
#                ____/`---'\____
#              .'  \\|     |//  `.
#             /  \\|||  :  |||//  \
#            /  _||||| -:- |||||-  \
#            |   | \\\  -  /// |   |
#            | \_|  ''\---/''  |   |
#            \  .-\__  `-`  ___/-. /
#          ___`. .'  /--.--\  `. . __
#       ."" '<  `.___\_<|>_/___.'  >'"".
#      | | :  `- \`.;`\ _ /`;.`/ - ` : | |
#      \  \ `-.   \_ __\ /__ _/   .-` /  /
# ======`-.____`-.___\_____/___.-`____.-'======
#                    `=---='

# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#            佛祖保佑       永无BUG
#            心外无法       法外无心
#            三宝弟子       三德子宏愿
# *********************************************/

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from openaiproxy.logging import logger

router = APIRouter(tags=["心跳检查接口"])

class HealthResponse(BaseModel):
    status: str = "nok"
    db: str = "error check the server logs"
    """
    Do not send exceptions and detailed error messages to the client because it might contain credentials and other
    sensitive server information.
    """

    def has_error(self) -> bool:
        return any(v.startswith("error") for v in self.model_dump().values())


# /health is also supported by uvicorn
# it means uvicorn's /health serves first before the apiproxy instance is up
# therefore it's not a reliable health check for a apiproxy instance
# we keep this for backward compatibility
@router.get("/health")
async def health():
    return {"status": "ok"}

# /health_check evaluates key services
# It's a reliable health check for a apiproxy instance
@router.get("/api/health_check", response_model=HealthResponse)
async def health_check(
) -> HealthResponse:
    from sqlmodel import select

    from openaiproxy.services.deps import get_session, get_settings_service

    response = HealthResponse()

    settings = get_settings_service().settings
    session = next(get_session())

    # use a fixed valid UUId that UUID collision is very unlikely
    # try:
    #     # Check database to query a bogus flow
    #     stmt = select(Flow).where(Flow.id == uuid.uuid4())
    #     session.exec(stmt).first()
    #     response.db = "ok"
    # except Exception:  # noqa: BLE001
    #     logger.exception("Error checking database")

    if response.has_error():
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=response.model_dump())
    response.status = "ok"
    return response
