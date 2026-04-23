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
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.deps import get_async_session
from openaiproxy.services.database.models.apikey.crud import select_apikey_by_hash, select_apikey_by_key
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.services.database.models.node.model import ProtocolType
from openaiproxy.utils.apikey import (
    ApiKeyEncryptionError,
    ApiKeyHashingError,
    ApiKeyTokenError,
    decrypt_api_key,
    hash_api_key,
    parse_api_key_token,
    parse_api_key_token_v2,
)
from openaiproxy.utils.timezone import current_time_in_timezone, current_timezone
from openaiproxy.constants import MANAGER_APP_ID, MANAGER_KEY_ID

AsyncDbSession = Annotated[AsyncSession, Depends(get_async_session)]

get_bearer_token = HTTPBearer(auto_error=False)


def _invalid_api_key_exception() -> HTTPException:
    """构造统一的无效管理密钥异常。"""
    return HTTPException(
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


def _missing_management_key_exception() -> HTTPException:
    """在未配置管理密钥时拒绝暴露敏感管理接口。"""
    return HTTPException(
        status_code=503,
        detail={
            'error': {
                'message': 'Management API key is not configured',
                'type': 'service_unavailable_error',
                'param': None,
                'code': 'management_api_key_not_configured',
            }
        },
    )

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
            raise _invalid_api_key_exception()
        return token
    else:
        # api_keys not set; allow all
        return None


async def check_strict_api_key(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(get_bearer_token),
) -> str:
    """校验管理接口密钥，并要求必须显式配置。"""

    access_keys = get_access_api_keys()
    if not access_keys:
        raise _missing_management_key_exception()

    if auth is None or (token := auth.credentials) not in access_keys.split(','):
        raise _invalid_api_key_exception()
    return token

class AccessKeyContext(BaseModel):
    ownerapp_id: str
    api_key_id: str  # May be None for static access keys
    request_protocol: ProtocolType


def _resolve_request_protocol(request: Request, x_api_key: Optional[str]) -> ProtocolType:
    """Resolve the northbound request protocol from path and headers."""
    if request.url.path.startswith('/v1/messages'):
        return ProtocolType.anthropic
    if x_api_key:
        return ProtocolType.anthropic
    return ProtocolType.openai

async def check_access_key(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(get_bearer_token),
    x_api_key: Annotated[Optional[str], Header(alias='x-api-key')] = None,
    *,
    session: AsyncDbSession,
    request: Request,
) -> AccessKeyContext:
    """Validate access keys for /v1 endpoints and expose owner app id."""

    request_protocol = _resolve_request_protocol(request, x_api_key)
    request.state.request_protocol = request_protocol

    token = auth.credentials if auth is not None and auth.credentials else None
    if token is None and x_api_key:
        token = x_api_key.strip() or None

    if token is None:
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

    if get_access_api_keys() and token in get_access_api_keys().split(','):
        request.state.ownerapp_id = MANAGER_APP_ID
        request.state.api_key_id = MANAGER_KEY_ID
        request.state.request_protocol = request_protocol
        return AccessKeyContext(
            ownerapp_id=MANAGER_APP_ID,
            api_key_id=MANAGER_KEY_ID,
            request_protocol=request_protocol,
        )

    ownerapp_id: Optional[str] = None
    plaintext_key: Optional[str] = None
    record: Optional[ApiKey] = None

    try:
        ownerapp_id, plaintext_key = parse_api_key_token_v2(token)
        record = await select_apikey_by_hash(
            ownerapp_id,
            hash_api_key(ownerapp_id, plaintext_key),
            session=session,
        )
    except (ApiKeyTokenError, ApiKeyHashingError):
        record = None

    if record is None:
        try:
            plain_payload = decrypt_api_key(token)
            ownerapp_id, plaintext_key = parse_api_key_token(plain_payload)
            record = await select_apikey_by_key(ownerapp_id, plaintext_key, session=session)
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

    expires_at = record.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=current_timezone())

    if expires_at and expires_at <= current_time_in_timezone():
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

    ownerapp_id_from_record = record.ownerapp_id or ""
    request.state.ownerapp_id = ownerapp_id_from_record
    request.state.api_key_id = str(record.id)
    request.state.request_protocol = request_protocol

    return AccessKeyContext(
        ownerapp_id=ownerapp_id_from_record,
        api_key_id=str(record.id),
        request_protocol=request_protocol,
    )
