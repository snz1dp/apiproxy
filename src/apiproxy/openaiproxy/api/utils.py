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

import os
from pydantic import BaseModel
from typing import Optional, Annotated
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.deps import get_async_session
from openaiproxy.services.database.models.apikey.crud import select_apikey_by_key
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.apikey import (
    ApiKeyEncryptionError,
    ApiKeyTokenError,
    decrypt_api_key,
    parse_api_key_token,
)
from openaiproxy.utils.timezone import current_time_in_timezone
from openaiproxy.constants import MANAGER_APP_ID, MANAGER_KEY_ID

AsyncDbSession = Annotated[AsyncSession, Depends(get_async_session)]

get_bearer_token = HTTPBearer(auto_error=False)

def get_access_api_keys():
    return os.getenv('APIPROXY_APIKEYS', None)

async def check_api_key(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(get_bearer_token),
) -> str:
    """Check if client provide valid api key.

    Adopted from https://github.com/lm-sys/FastChat/blob/v0.2.35/fastchat/serve/openai_api_server.py#L108-L127
    """  # noqa
    if get_access_api_keys():
        if auth is None or (
            token := auth.credentials
        ) not in get_access_api_keys().split(','):
            raise HTTPException(
                status_code=401,
                detail={
                    'error': {
                        'message': 'Please request with valid api key!',
                        'type': 'invalid_request_error',
                        'param': None,
                        'code': 'invalid_api_key',
                    }
                },
            )
        return token
    else:
        # api_keys not set; allow all
        return None

class AccessKeyContext(BaseModel):
    ownerapp_id: str
    api_key_id: str  # May be None for static access keys

async def check_access_key(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(get_bearer_token),
    *,
    session: AsyncDbSession,
    request: Request,
) -> str:
    """Validate access keys for /v1 endpoints and expose owner app id."""

    if auth is None or not auth.credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Please request with valid api key!",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key",
                },
            },
        )

    token = auth.credentials
    if get_access_api_keys() and token in get_access_api_keys().split(','):
        request.state.ownerapp_id = MANAGER_APP_ID
        request.state.api_key_id = MANAGER_KEY_ID
        return AccessKeyContext(
            ownerapp_id=MANAGER_APP_ID,
            api_key_id=MANAGER_KEY_ID,
        )

    try:
        plain_payload = decrypt_api_key(token)
        ownerapp_id, plaintext_key = parse_api_key_token(plain_payload)
    except (ApiKeyEncryptionError, ApiKeyTokenError):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Please request with valid api key!",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key",
                },
            },
        )

    record = await select_apikey_by_key(ownerapp_id, plaintext_key, session=session)
    if record is None or record.enabled is False:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Please request with valid api key!",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key",
                },
            },
        )

    if record.expires_at and record.expires_at <= current_time_in_timezone():
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "API key has expired",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "expired_api_key",
                },
            },
        )

    request.state.ownerapp_id = ownerapp_id
    request.state.api_key_id = str(record.id)

    return AccessKeyContext(
        ownerapp_id=ownerapp_id,
        api_key_id=str(record.id),
    )
