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

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError

from openaiproxy.api.schemas import (
	ApiKeyCreate,
	ApiKeyCreateResponse,
	ApiKeyRead,
	ApiKeyUpdate,
	PageResponse,
)
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.apikey.crud import (
	count_apikeys,
	create_apikey_record,
	delete_apikey_record,
	select_apikey_by_hash,
	select_apikey_by_id,
	select_apikeys,
	update_apikey_record,
)
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.apikey import (
	ApiKeyTokenError,
	ApiKeyHashingError,
	compose_api_key_token_v2,
	generate_api_key,
	hash_api_key,
)
from openaiproxy.utils.timezone import current_time_in_timezone

router = APIRouter(tags=["应用API密钥管理"])

def _to_api_key_read(item: ApiKey) -> ApiKeyRead:
	return ApiKeyRead.model_validate(item, from_attributes=True)


@router.get(
	"/apikeys",
	dependencies=[Depends(check_api_key)],
	summary="获取API Key列表",
)
async def list_api_keys(
	name: Optional[str] = None,
	ownerapp_id: Optional[str] = None,
	enabled: Optional[bool] = None,
	expired: Optional[bool] = None,
	orderby: Optional[str] = None,
	offset: int = 0,
	limit: int = 20,
	*,
	session: AsyncDbSession,
) -> PageResponse[ApiKeyRead]:
	"""获取API Key列表"""
	safe_offset = max(offset, 0)
	safe_limit = max(limit, 0) if limit is not None else None

	api_keys = await select_apikeys(
		name=name,
		ownerapp_id=ownerapp_id,
		enabled=enabled,
		expired=expired,
		orderby=orderby,
		offset=safe_offset,
		limit=safe_limit,
		session=session,
	)
	raw_total = await count_apikeys(
		name=name,
		ownerapp_id=ownerapp_id,
		enabled=enabled,
		expired=expired,
		session=session,
	)
	total = raw_total if isinstance(raw_total, int) else raw_total[0]
	response_items = [_to_api_key_read(item) for item in api_keys]
	return PageResponse[ApiKeyRead](
		offset=safe_offset,
		total=int(total),
		data=response_items,
	)


@router.post(
	"/apikeys",
	dependencies=[Depends(check_api_key)],
	summary="创建API Key",
)
async def create_api_key(
	input: ApiKeyCreate,
	*,
	session: AsyncDbSession,
) -> ApiKeyCreateResponse:
	if not input.ownerapp_id:
		raise HTTPException(
			status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
			detail="ownerapp_id不能为空",
		)

	max_attempts = 10
	plaintext_key: Optional[str] = None
	key_hash: Optional[str] = None
	for _ in range(max_attempts):
		candidate = generate_api_key(length=48)
		try:
			candidate_hash = hash_api_key(input.ownerapp_id, candidate)
		except ApiKeyHashingError as exc:
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail="API Key哈希计算失败",
			) from exc
		existing = await select_apikey_by_hash(
			input.ownerapp_id,
			candidate_hash,
			session=session,
		)
		if existing is None:
			plaintext_key = candidate
			key_hash = candidate_hash
			break
	else:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key生成失败，请稍后重试",
		)

	if plaintext_key is None or key_hash is None:  # pragma: no cover - defensive guard
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key生成失败，请稍后重试",
		)

	try:
		encrypted_token = compose_api_key_token_v2(input.ownerapp_id, plaintext_key)
	except ApiKeyTokenError as exc:  # pragma: no cover - defensive guard
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key令牌生成失败",
		) from exc

	api_key_payload = {
		"name": input.name,
		"description": input.description,
		"ownerapp_id": input.ownerapp_id,
		"key": None,
		"key_hash": key_hash,
		"key_prefix": plaintext_key[:8],
		"key_version": 2,
		"expires_at": input.expires_at,
		"created_at": current_time_in_timezone(),
	}

	try:
		api_key = await create_apikey_record(session=session, apikey_payload=api_key_payload)
	except IntegrityError as exc:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="API Key已存在",
		) from exc

	base_payload = _to_api_key_read(api_key)
	return ApiKeyCreateResponse(
		**base_payload.model_dump(),
		token=encrypted_token,
	)

@router.get(
	"/apikeys/{api_key_id}",
	dependencies=[Depends(check_api_key)],
	summary="获取指定ID的API Key",
)
async def get_api_key(
	api_key_id: UUID,
	*,
	session: AsyncDbSession,
) -> ApiKeyRead:
	existed = await select_apikey_by_id(api_key_id, session=session)
	if not existed:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="API Key不存在",
		)
	return _to_api_key_read(existed)


@router.post(
	"/apikeys/{api_key_id}",
	dependencies=[Depends(check_api_key)],
	summary="更新API Key",
)
async def update_api_key(
	api_key_id: UUID,
	update: ApiKeyUpdate,
	*,
	session: AsyncDbSession,
) -> ApiKeyRead:
	existed = await select_apikey_by_id(api_key_id, session=session)
	if not existed:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="API Key不存在",
		)

	update_payload = update.model_dump(exclude_unset=True)
	if not update_payload:
		return _to_api_key_read(existed)

	existed = await update_apikey_record(
		session=session,
		api_key=existed,
		update_payload=update_payload,
	)
	return _to_api_key_read(existed)

@router.delete(
	"/apikeys/{api_key_id}",
	dependencies=[Depends(check_api_key)],
	summary="删除API Key",
)
async def delete_api_key(
	api_key_id: UUID,
	*,
	session: AsyncDbSession,
):
	existed = await select_apikey_by_id(api_key_id, session=session)
	if existed and existed.enabled:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="只能删除已禁用的API Key",
		)
	elif existed:
		await delete_apikey_record(session=session, api_key=existed)
	return {
		"code": 0,
		"message": "删除成功",
	}
