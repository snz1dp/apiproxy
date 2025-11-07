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
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import ApiKeyUpdate, PageResponse
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.apikey.crud import (
	count_apikeys,
	select_apikey_by_id,
	select_apikey_by_key,
	select_apikeys,
)
from openaiproxy.services.database.models.apikey.model import ApiKey
from openaiproxy.utils.timezone import current_time_in_timezone

router = APIRouter(tags=["API Key管理"])


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

	return PageResponse[ApiKey](
		offset=safe_offset,
		total=int(total),
		data=api_keys,
	)


@router.post(
	"/apikeys",
	dependencies=[Depends(check_api_key)],
	summary="创建API Key",
)
async def create_api_key(
	input: ApiKey,
	*,
	session: AsyncDbSession,
) -> ApiKey:
	existed = await select_apikey_by_key(input.key, session=session)
	if existed:
		return existed

	if input.id:
		existed = await select_apikey_by_id(input.id, session=session)
		if existed:
			return existed
	else:
		input.id = uuid4()

	# Ensure persisted timestamp always uses timezone-aware datetime
	input.created_at = current_time_in_timezone()

	session.add(input)
	await session.commit()
	await session.refresh(input)
	return input


@router.post(
	"/apikeys/query",
	dependencies=[Depends(check_api_key)],
	summary="通过Key查询API Key",
)
async def query_api_key_by_key(
	key: str,
	*,
	session: AsyncDbSession,
) -> ApiKey:
	existed = await select_apikey_by_key(key, session=session)
	if not existed:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="API Key不存在",
		)
	return existed


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
	return existed


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
		return existed

	if "key" in update_payload:
		duplicated = await select_apikey_by_key(update_payload["key"], session=session)
		if duplicated and duplicated.id != existed.id:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="API Key已存在",
			)

	for field, value in update_payload.items():
		setattr(existed, field, value)

	session.add(existed)
	await session.commit()
	await session.refresh(existed)
	return existed


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
