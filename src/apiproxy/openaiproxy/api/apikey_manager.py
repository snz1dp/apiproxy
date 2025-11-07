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

from openaiproxy.api.schemas import ApiKeyCreate, ApiKeyUpdate, PageResponse
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.apikey.crud import (
	count_apikeys,
	select_apikey_by_id,
	select_apikey_by_key,
	select_apikeys,
)
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.apikey import (
	ApiKeyEncryptionError,
	ApiKeyTokenError,
	compose_api_key_token,
	decrypt_api_key,
	encrypt_api_key,
	generate_api_key,
	parse_api_key_token,
)
from openaiproxy.utils.timezone import current_time_in_timezone

router = APIRouter(tags=["API Key管理"])

def _render_key_or_500(item: ApiKey) -> str:
	try:
		plaintext = decrypt_api_key(item.key)
	except ApiKeyEncryptionError as exc:  # pragma: no cover - defensive guard
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key解密失败",
		) from exc
	try:
		return compose_api_key_token(item.ownerapp_id, plaintext)
	except ApiKeyTokenError as exc:  # pragma: no cover - defensive guard
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key格式错误",
		) from exc


@router.get(
	"/apikeys",
	dependencies=[Depends(check_api_key)],
	summary="获取API Key列表",
)
async def list_api_keys(
	name: Optional[str] = None,
	ownerapp_id: Optional[str] = None,
	expired: Optional[bool] = None,
	orderby: Optional[str] = None,
	offset: int = 0,
	limit: int = 20,
	*,
	session: AsyncDbSession,
) -> PageResponse[ApiKey]:
	"""获取API Key列表"""
	safe_offset = max(offset, 0)
	safe_limit = max(limit, 0) if limit is not None else None

	api_keys = await select_apikeys(
		name=name,
		ownerapp_id=ownerapp_id,
		expired=expired,
		orderby=orderby,
		offset=safe_offset,
		limit=safe_limit,
		session=session,
	)
	raw_total = await count_apikeys(
		name=name,
		ownerapp_id=ownerapp_id,
		expired=expired,
		session=session,
	)
	total = raw_total if isinstance(raw_total, int) else raw_total[0]
	response_items = [item.model_copy(update={"key": _render_key_or_500(item)}) for item in api_keys]
	return PageResponse[ApiKey](
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
) -> ApiKey:
	if not input.ownerapp_id:
		raise HTTPException(
			status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
			detail="ownerapp_id不能为空",
		)

	plaintext_key = generate_api_key()
	existing_records = await select_apikeys(ownerapp_id=input.ownerapp_id, session=session)
	for existing in existing_records:
		try:
			if decrypt_api_key(existing.key) == plaintext_key:
				raise HTTPException(
					status_code=status.HTTP_400_BAD_REQUEST,
					detail="API Key已存在",
				)
		except ApiKeyEncryptionError as exc:  # pragma: no cover - defensive guard
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail="API Key解密失败",
			) from exc
	try:
		encrypted_key = encrypt_api_key(plaintext_key)
	except ApiKeyEncryptionError as exc:  # pragma: no cover - defensive guard
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key加密失败",
		) from exc

	api_key = ApiKey(
		name=input.name,
		description=input.description,
		ownerapp_id=input.ownerapp_id,
		key=encrypted_key,
		expires_at=input.expires_at,
		created_at=current_time_in_timezone(),
	)

	session.add(api_key)
	try:
		await session.commit()
	except IntegrityError as exc:
		await session.rollback()
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="API Key已存在",
		) from exc

	await session.refresh(api_key)
	try:
		composite_key = compose_api_key_token(input.ownerapp_id, plaintext_key)
	except ApiKeyTokenError as exc:  # pragma: no cover - defensive guard
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="API Key生成失败",
		) from exc
	return api_key.model_copy(update={"key": composite_key})

@router.get(
	"/apikeys/{api_key_id}",
	dependencies=[Depends(check_api_key)],
	summary="获取指定ID的API Key",
)
async def get_api_key(
	api_key_id: UUID,
	*,
	session: AsyncDbSession,
) -> ApiKey:
	existed = await select_apikey_by_id(api_key_id, session=session)
	if not existed:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="API Key不存在",
		)
	return existed.model_copy(update={"key": _render_key_or_500(existed)})


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
) -> ApiKey:
	existed = await select_apikey_by_id(api_key_id, session=session)
	if not existed:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="API Key不存在",
		)

	update_payload = update.model_dump(exclude_unset=True)
	if not update_payload:
		return existed.model_copy(update={"key": _render_key_or_500(existed)})

	for field, value in update_payload.items():
		setattr(existed, field, value)

	session.add(existed)
	await session.commit()
	await session.refresh(existed)
	return existed.model_copy(update={"key": _render_key_or_500(existed)})

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
	if existed:
		await session.delete(existed)
		await session.commit()

	return {
		"code": 0,
		"message": "删除成功",
	}
