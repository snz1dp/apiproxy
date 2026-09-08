"""节点独立API密钥管理接口"""

import hashlib
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from openaiproxy.api.schemas import (
    CreateNodeApiKey,
    NodeApiKeyResponse,
    UpdateNodeApiKey,
)
from openaiproxy.api.node_manager import _verify_node_protocols
from openaiproxy.api.utils import AsyncDbSession, check_api_key
from openaiproxy.services.database.models.node.crud import (
    create_node_api_key_record,
    select_node_api_key_by_hash,
    select_node_api_key_by_id,
    select_node_api_keys_by_node_id,
    select_node_by_id,
    update_node_api_key_record,
    delete_node_api_key_record,
)
from openaiproxy.services.database.models.node.model import NodeApiKey
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.utils.apikey import (
    ApiKeyEncryptionError,
    decrypt_api_key,
    encrypt_api_key,
)
from openaiproxy.utils.timezone import current_time_in_timezone

router = APIRouter(tags=["节点API密钥管理"])


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    """去除首尾空白，空串归一为 None"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _hash_node_api_key(plaintext: str) -> str:
    """计算节点API密钥的SHA256哈希（用于 node_id+hash 唯一约束的 upsert 查找）"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _to_response(record: NodeApiKey) -> NodeApiKeyResponse:
    """将 ORM 记录转换为响应体，api_key 字段不返回"""
    payload = record.model_dump()
    del payload["api_key"]
    del payload["api_key_hash"]
    return NodeApiKeyResponse.model_validate(payload)


async def _ensure_node_exists(node_id: UUID, *, session: AsyncDbSession):
    """校验节点存在并返回节点记录"""
    node = await select_node_by_id(node_id, session=session)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )
    return node


async def _verify_node_api_key(
    node,
    plaintext_key: str,
    *,
    verify: Optional[bool],
) -> None:
    """按节点配置验证API密钥可用性（复用节点创建时的 /v1/models 验证逻辑）

    Args:
        node: 节点 ORM 记录，提供 url、协议类型等运行时配置
        plaintext_key: 待验证的明文API密钥
        verify: 是否执行验证；False 时跳过，None/True 时按节点配置决定
    """
    await _verify_node_protocols(
        node_url=node.url,
        api_key=plaintext_key,
        protocol_type=node.protocol_type,
        auto_v1_api=bool(node.auto_v1_api),
        request_proxy_url=node.request_proxy_url,
        verify=verify,
        trusted_without_models_endpoint=bool(node.trusted_without_models_endpoint),
    )


@router.post(
    "/nodes/{node_id}/apikeys",
    dependencies=[Depends(check_api_key)],
    summary="创建节点API密钥",
)
async def create_node_apikey(
    node_id: UUID,
    payload: CreateNodeApiKey,
    *,
    session: AsyncDbSession,
) -> NodeApiKeyResponse:
    """为节点添加一个独立API密钥；同一节点重复提交相同密钥视为更新（幂等）

    verify 为 True（默认）时，先按节点配置请求 /v1/models 验证密钥可用性，
    验证失败直接返回 400，不落库。
    """
    node = await _ensure_node_exists(node_id, session=session)

    plaintext_key = payload.api_key.strip()
    await _verify_node_api_key(node, plaintext_key, verify=payload.verify)

    api_key_hash = _hash_node_api_key(plaintext_key)
    try:
        encrypted_key = encrypt_api_key(plaintext_key)
    except ApiKeyEncryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API密钥加密失败",
        ) from exc

    existing = await select_node_api_key_by_hash(
        node_id=node_id,
        api_key_hash=api_key_hash,
        session=session,
    )
    if existing is not None:
        # 同节点同密钥视为更新，避免撞唯一约束
        update_payload = {
            "priority": payload.priority,
            "max_tokens": payload.max_tokens,
            "expires_at": payload.expires_at,
            "enabled": payload.enabled if payload.enabled is not None else True,
        }
        if payload.name is not None:
            update_payload["name"] = _normalize_optional_str(payload.name)
        # 手动更新时清空自动禁用痕迹；重新启用则重置已用Tokens
        if update_payload["enabled"]:
            update_payload.update(disabled_at=None, disable_reason=None)
            if existing.tokens_used and payload.max_tokens is None:
                update_payload["tokens_used"] = 0
        record = await update_node_api_key_record(
            session=session,
            record=existing,
            update_payload=update_payload,
            updated_at=current_time_in_timezone(),
        )
    else:
        record = await create_node_api_key_record(
            session=session,
            payload={
                "node_id": node_id,
                "name": _normalize_optional_str(payload.name),
                "api_key": encrypted_key,
                "api_key_hash": api_key_hash,
                "priority": payload.priority,
                "max_tokens": payload.max_tokens,
                "expires_at": payload.expires_at,
                "enabled": payload.enabled if payload.enabled is not None else True,
            },
        )

    if record.enabled:
        get_node_proxy_service().restore_node_api_key_availability(record.id)
    return _to_response(record)


@router.get(
    "/nodes/{node_id}/apikeys",
    dependencies=[Depends(check_api_key)],
    summary="获取节点API密钥列表",
)
async def list_node_apikeys(
    node_id: UUID,
    enabled: Optional[bool] = None,
    offset: int = 0,
    limit: int = 100,
    *,
    session: AsyncDbSession,
) -> list[NodeApiKeyResponse]:
    """查询节点下的API密钥列表；enabled 为空时返回全部（含禁用/过期）"""
    await _ensure_node_exists(node_id, session=session)
    records = await select_node_api_keys_by_node_id(
        node_id=node_id,
        enabled=enabled,
        offset=max(offset, 0),
        limit=max(limit, 0),
        session=session,
    )
    return [_to_response(record) for record in records]


@router.get(
    "/nodes/{node_id}/apikeys/{key_id}",
    dependencies=[Depends(check_api_key)],
    summary="获取节点API密钥详情",
)
async def get_node_apikey(
    node_id: UUID,
    key_id: UUID,
    *,
    session: AsyncDbSession,
) -> NodeApiKeyResponse:
    """按ID查询节点下的单个API密钥"""
    record = await select_node_api_key_by_id(key_id, session=session)
    if record is None or record.node_id != node_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API密钥不存在",
        )
    return _to_response(record)


@router.put(
    "/nodes/{node_id}/apikeys/{key_id}",
    dependencies=[Depends(check_api_key)],
    summary="更新节点API密钥",
)
async def update_node_apikey(
    node_id: UUID,
    key_id: UUID,
    payload: UpdateNodeApiKey,
    *,
    session: AsyncDbSession,
) -> NodeApiKeyResponse:
    """更新API密钥的权重、限额、过期时间、启用状态；传入新 api_key 时同步替换密文与哈希

    传入新 api_key 且 verify 为 True（默认）时，先按节点配置请求 /v1/models
    验证新密钥可用性，验证失败直接返回 400，不落库。
    """
    record = await select_node_api_key_by_id(key_id, session=session)
    if record is None or record.node_id != node_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API密钥不存在",
        )

    update_payload: dict = {}
    if payload.api_key is not None:
        plaintext_key = payload.api_key.strip()
        # 更换密钥时按节点配置验证新密钥可用性
        node = await _ensure_node_exists(node_id, session=session)
        await _verify_node_api_key(node, plaintext_key, verify=payload.verify)
        try:
            update_payload["api_key"] = encrypt_api_key(plaintext_key)
        except ApiKeyEncryptionError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="API密钥加密失败",
            ) from exc
        update_payload["api_key_hash"] = _hash_node_api_key(plaintext_key)
    if payload.name is not None:
        update_payload["name"] = _normalize_optional_str(payload.name)
    if payload.priority is not None:
        update_payload["priority"] = payload.priority
    if payload.max_tokens is not None:
        update_payload["max_tokens"] = payload.max_tokens
    if payload.expires_at is not None:
        update_payload["expires_at"] = payload.expires_at
    if payload.enabled is not None:
        update_payload["enabled"] = payload.enabled
        if payload.enabled:
            # 手动重新启用：清空自动禁用痕迹并重置已用Tokens（与 enable_node_api_key 语义一致）
            update_payload.update(
                disabled_at=None,
                disable_reason=None,
                tokens_used=0,
            )

    if update_payload:
        record = await update_node_api_key_record(
            session=session,
            record=record,
            update_payload=update_payload,
            updated_at=current_time_in_timezone(),
        )

    if record.enabled:
        get_node_proxy_service().restore_node_api_key_availability(record.id)
    return _to_response(record)


@router.delete(
    "/nodes/{node_id}/apikeys/{key_id}",
    dependencies=[Depends(check_api_key)],
    summary="删除节点API密钥",
)
async def delete_node_apikey(
    node_id: UUID,
    key_id: UUID,
    *,
    session: AsyncDbSession,
) -> dict:
    """删除节点下的API密钥记录"""
    record = await select_node_api_key_by_id(key_id, session=session)
    if record is None or record.node_id != node_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API密钥不存在",
        )

    await delete_node_api_key_record(session=session, record=record)
    # 立即从本实例内存中移除该密钥条目，避免刷新周期内继续被选中
    get_node_proxy_service().forget_node_api_key(key_id)
    return {"message": "删除成功"}
