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
from typing import Optional, Annotated
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel.ext.asyncio.session import AsyncSession
from openaiproxy.services.deps import get_async_session

AsyncDbSession = Annotated[AsyncSession, Depends(get_async_session)]

get_bearer_token = HTTPBearer(auto_error=False)

def get_access_api_keys():
    return os.getenv('ACCESS_API_KEYS', None)

async def check_api_key(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(get_bearer_token),
) -> str:
    """Check if client provide valid api key.

    Adopted from https://github.com/lm-sys/FastChat/blob/v0.2.35/fastchat/serve/openai_api_server.py#L108-L127
    """  # noqa
    if get_access_api_keys():
        if auth is None or (
                token := auth.credentials) not in get_access_api_keys().split(','):
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
