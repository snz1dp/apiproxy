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

from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any, Optional
import orjson
import traceback
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from openaiproxy.api.schemas import VideoGenerationRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import _build_backend_json_response
from openaiproxy.api.v1.embeddings import _apply_backend_error_info, _extract_backend_error
from openaiproxy.logging import logger
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.database.models.proxy.crud import (
    acquire_database_task_lock_transactionally,
    create_video_generation_task_entry,
    delete_video_generation_tasks_before,
    release_database_task_lock_transactionally,
    select_recoverable_video_generation_tasks,
    select_video_generation_task_by_id,
    select_video_generation_task_by_video_id,
    update_video_generation_task_entry,
)
from openaiproxy.services.database.models.proxy.model import (
    RequestAction,
    VideoGenerationTask,
    VideoTaskStatus,
)
from openaiproxy.services.deps import (
    async_session_scope,
    get_node_proxy_service,
    get_settings_service,
)
from openaiproxy.services.nodeproxy.exceptions import (
    ApiKeyQuotaExceeded,
    AppQuotaExceeded,
    NodeModelQuotaExceeded,
    NorthboundQuotaProcessingError,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.async_helpers import run_until_complete
from openaiproxy.utils.timezone import current_timezone
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

router = APIRouter(tags=["OpenAI兼容接口"])
_VIDEO_TASK_RECOVERY_LOCK_NAME = 'video_task_recovery'
_VIDEO_TASK_CLEANUP_LOCK_NAME = 'video_task_cleanup'
_VIDEO_TASK_TERMINAL_STATUSES = {
    VideoTaskStatus.succeeded,
    VideoTaskStatus.failed,
    VideoTaskStatus.canceled,
}


def _coerce_protocol_type(protocol_type: ProtocolType | str | None) -> ProtocolType:
    """归一化协议类型。"""

    if isinstance(protocol_type, ProtocolType):
        return protocol_type
    if isinstance(protocol_type, str):
        return ProtocolType(protocol_type)
    return ProtocolType.openai


def _normalize_video_task_status(raw_status: Any) -> VideoTaskStatus:
    """将下游返回的任务状态映射为本地枚举。"""

    if isinstance(raw_status, VideoTaskStatus):
        return raw_status
    if isinstance(raw_status, str):
        normalized = raw_status.strip().lower()
        if normalized in {'succeeded', 'completed', 'done'}:
            return VideoTaskStatus.succeeded
        if normalized in {'failed', 'error', 'expired'}:
            return VideoTaskStatus.failed
        if normalized in {'canceled', 'cancelled'}:
            return VideoTaskStatus.canceled
    return VideoTaskStatus.processing


def _is_video_task_terminal(status: VideoTaskStatus | str | None) -> bool:
    """判断视频任务是否处于终态。"""

    return _normalize_video_task_status(status) in _VIDEO_TASK_TERMINAL_STATUSES


def _default_video_filename(video_id: str) -> str:
    """构造默认的视频下载文件名。"""

    return f'{video_id}.mp4'


async def _create_video_task_dispatch_record(
    *,
    request: VideoGenerationRequest,
    request_dict: dict[str, Any],
    node_url: str,
    api_key: Optional[str],
    protocol_type: ProtocolType,
    request_proxy_url: Optional[str],
    request_ctx,
    access_ctx: AccessKeyContext,
) -> UUID:
    """在转发请求前创建可恢复的视频任务记录。"""

    async with async_session_scope() as session:
        task_entry = await create_video_generation_task_entry(
            session=session,
            task_payload={
                'request_log_id': getattr(request_ctx, 'log_id', None),
                'node_id': getattr(request_ctx, 'node_id', None),
                'ownerapp_id': access_ctx.ownerapp_id,
                'api_key_id': access_ctx.api_key_id,
                'model_name': request.model,
                'status': VideoTaskStatus.dispatching,
                'node_url': node_url,
                'backend_api_key': api_key,
                'protocol_type': protocol_type,
                'request_proxy_url': request_proxy_url,
                'request_payload': request_dict,
            },
        )
        return task_entry.id


async def _load_video_task_by_video_id(video_id: str) -> Optional[VideoGenerationTask]:
    """按视频任务ID加载持久化任务记录。"""

    async with async_session_scope() as session:
        return await select_video_generation_task_by_video_id(
            session=session,
            video_id=video_id,
        )


async def _store_video_task_response_text(task_id: UUID, response_text: str) -> None:
    """持久化最近一次原始文本响应。"""

    async with async_session_scope() as session:
        task_entry = await select_video_generation_task_by_id(
            session=session,
            task_id=task_id,
        )
        if task_entry is None:
            return
        await update_video_generation_task_entry(
            session=session,
            video_task=task_entry,
            update_payload={
                'latest_response_text': response_text,
            },
        )


def _build_video_task_payload_update(
    *,
    task_entry: VideoGenerationTask,
    payload: dict[str, Any],
    store_create_payload: bool = False,
    increment_recovery_attempts: bool = False,
) -> dict[str, Any]:
    """根据下游结构化响应构造任务更新字段。"""

    now = datetime.now(tz=current_timezone())
    next_status = _normalize_video_task_status(payload.get('status'))
    error_message, _ = _extract_backend_error(payload)
    if error_message:
        next_status = VideoTaskStatus.failed
    elif _is_video_task_terminal(task_entry.status) and not _is_video_task_terminal(next_status):
        next_status = _normalize_video_task_status(task_entry.status)

    update_payload: dict[str, Any] = {
        'latest_response_payload': payload,
        'status': next_status,
        'error_message': error_message,
    }
    if store_create_payload:
        update_payload['create_response_payload'] = payload
    if isinstance(payload.get('id'), str):
        update_payload['video_id'] = payload['id']
    if increment_recovery_attempts:
        update_payload['recovery_attempts'] = int(task_entry.recovery_attempts or 0) + 1
        update_payload['last_recovered_at'] = now
    if _is_video_task_terminal(next_status):
        update_payload['completed_at'] = task_entry.completed_at or now
    return update_payload


async def _update_video_task_from_payload(
    *,
    task_id: UUID,
    payload: dict[str, Any],
    store_create_payload: bool = False,
    increment_recovery_attempts: bool = False,
) -> None:
    """将结构化响应同步回视频任务记录。"""

    async with async_session_scope() as session:
        task_entry = await select_video_generation_task_by_id(
            session=session,
            task_id=task_id,
        )
        if task_entry is None:
            return
        await update_video_generation_task_entry(
            session=session,
            video_task=task_entry,
            update_payload=_build_video_task_payload_update(
                task_entry=task_entry,
                payload=payload,
                store_create_payload=store_create_payload,
                increment_recovery_attempts=increment_recovery_attempts,
            ),
        )


async def _mark_video_task_failed(
    *,
    task_id: UUID,
    error_message: str,
    payload: Optional[dict[str, Any]] = None,
    increment_recovery_attempts: bool = False,
) -> None:
    """将视频任务标记为失败。"""

    now = datetime.now(tz=current_timezone())
    async with async_session_scope() as session:
        task_entry = await select_video_generation_task_by_id(
            session=session,
            task_id=task_id,
        )
        if task_entry is None:
            return
        update_payload: dict[str, Any] = {
            'status': VideoTaskStatus.failed,
            'error_message': error_message,
            'completed_at': task_entry.completed_at or now,
        }
        if payload is not None:
            update_payload['latest_response_payload'] = payload
        if increment_recovery_attempts:
            update_payload['recovery_attempts'] = int(task_entry.recovery_attempts or 0) + 1
            update_payload['last_recovered_at'] = now
        await update_video_generation_task_entry(
            session=session,
            video_task=task_entry,
            update_payload=update_payload,
        )


async def _cache_video_task_content(
    *,
    task_id: UUID,
    content_data: bytes,
    content_type: str,
    content_filename: str,
) -> None:
    """缓存视频内容到数据库。"""

    now = datetime.now(tz=current_timezone())
    async with async_session_scope() as session:
        task_entry = await select_video_generation_task_by_id(
            session=session,
            task_id=task_id,
        )
        if task_entry is None:
            return
        update_payload: dict[str, Any] = {
            'content_data': content_data,
            'content_type': content_type,
            'content_filename': content_filename,
            'content_size': len(content_data),
        }
        if not _is_video_task_terminal(task_entry.status):
            update_payload['status'] = VideoTaskStatus.succeeded
            update_payload['completed_at'] = task_entry.completed_at or now
        await update_video_generation_task_entry(
            session=session,
            video_task=task_entry,
            update_payload=update_payload,
        )


def _build_video_task_not_found_response(video_id: str):
    """Build a not-found response for untracked video tasks."""

    return create_error_response(
        HTTPStatus.NOT_FOUND,
        f'Video task `{video_id}` is not available.',
        error_type='not_found_error',
    )


def _build_video_task_forbidden_response(video_id: str):
    """Build a forbidden response for video tasks owned by another access key."""

    return create_error_response(
        HTTPStatus.FORBIDDEN,
        f'Access to video task `{video_id}` is denied.',
        error_type='permission_error',
    )


async def _resolve_video_task_entry(
    *,
    video_id: str,
    access_ctx: AccessKeyContext,
    nodeproxy_service: NodeProxyService,
):
    """Resolve a tracked video task and enforce access policy consistency."""

    task_entry = await _load_video_task_by_video_id(video_id)
    if task_entry is None:
        return None, _build_video_task_not_found_response(video_id)

    stored_api_key_id = task_entry.api_key_id
    stored_ownerapp_id = task_entry.ownerapp_id
    if stored_api_key_id is not None and access_ctx.api_key_id != stored_api_key_id:
        return None, _build_video_task_forbidden_response(video_id)
    if stored_api_key_id is None and stored_ownerapp_id is not None and access_ctx.ownerapp_id != stored_ownerapp_id:
        return None, _build_video_task_forbidden_response(video_id)

    model_name = task_entry.model_name
    if isinstance(model_name, str) and not nodeproxy_service.is_model_allowed(model_name, access_ctx.effective_allowed_models):
        return None, _build_video_task_forbidden_response(video_id)
    return task_entry, None


async def _acquire_video_task_lock(task_name: str, lease_seconds: int) -> Optional[str]:
    """获取视频任务相关的数据库级任务锁。"""

    owner_token = f'{task_name}:{get_settings_service().settings.instance_id}:{uuid4()}'
    acquired = await acquire_database_task_lock_transactionally(
        task_name=task_name,
        owner_token=owner_token,
        lease_seconds=lease_seconds,
    )
    if not acquired:
        return None
    return owner_token


async def _release_video_task_lock(task_name: str, owner_token: Optional[str]) -> None:
    """释放视频任务相关的数据库级任务锁。"""

    if owner_token is None:
        return
    await release_database_task_lock_transactionally(
        task_name=task_name,
        owner_token=owner_token,
    )


async def recover_video_generation_tasks(
    *,
    nodeproxy_service: Optional[NodeProxyService] = None,
) -> tuple[int, int]:
    """恢复未完成的视频任务，或在无法恢复时标记失败。"""

    settings = get_settings_service().settings
    lease_seconds = max(int(settings.video_tasks_recovery_interval or 60), 30)
    owner_token = await _acquire_video_task_lock(
        _VIDEO_TASK_RECOVERY_LOCK_NAME,
        lease_seconds,
    )
    if owner_token is None:
        return 0, 0

    if nodeproxy_service is None:
        nodeproxy_service = get_node_proxy_service()

    refreshed_count = 0
    failed_count = 0
    try:
        async with async_session_scope() as session:
            task_entries = await select_recoverable_video_generation_tasks(
                session=session,
            )

        for task_entry in task_entries:
            if task_entry.video_id is None:
                if task_entry.latest_response_text:
                    try:
                        payload = orjson.loads(task_entry.latest_response_text)
                    except Exception:  # noqa: BLE001
                        payload = None
                    if isinstance(payload, dict) and isinstance(payload.get('id'), str):
                        await _update_video_task_from_payload(
                            task_id=task_entry.id,
                            payload=payload,
                            store_create_payload=task_entry.create_response_payload is None,
                            increment_recovery_attempts=True,
                        )
                        refreshed_count += 1
                        continue

                await _mark_video_task_failed(
                    task_id=task_entry.id,
                    error_message='服务在等待上游创建任务响应时异常退出，且未持久化到有效 video_id，已标记失败。',
                    increment_recovery_attempts=True,
                )
                failed_count += 1
                continue

            response_text = await nodeproxy_service.generate(
                request=None,
                node_url=task_entry.node_url,
                endpoint=f'/v1/videos/{task_entry.video_id}',
                api_key=task_entry.backend_api_key,
                protocol_type=_coerce_protocol_type(task_entry.protocol_type),
                request_proxy_url=task_entry.request_proxy_url,
                method='GET',
            )
            if not isinstance(response_text, str):
                response_text = str(response_text)

            await _store_video_task_response_text(task_entry.id, response_text)
            try:
                payload = orjson.loads(response_text)
            except Exception:  # noqa: BLE001
                await _mark_video_task_failed(
                    task_id=task_entry.id,
                    error_message=f'恢复任务状态时解析下游响应失败: {response_text!r}',
                    increment_recovery_attempts=True,
                )
                failed_count += 1
                continue

            await _update_video_task_from_payload(
                task_id=task_entry.id,
                payload=payload,
                increment_recovery_attempts=True,
            )
            if _extract_backend_error(payload)[0]:
                failed_count += 1
            else:
                refreshed_count += 1

        return refreshed_count, failed_count
    finally:
        await _release_video_task_lock(_VIDEO_TASK_RECOVERY_LOCK_NAME, owner_token)


async def recover_video_generation_tasks_on_startup(
    *,
    nodeproxy_service: Optional[NodeProxyService] = None,
) -> tuple[int, int]:
    """在服务启动时执行一次视频任务恢复。"""

    refreshed_count, failed_count = await recover_video_generation_tasks(
        nodeproxy_service=nodeproxy_service,
    )
    if refreshed_count or failed_count:
        logger.info(
            '视频任务启动恢复完成，刷新 {} 条，标记失败 {} 条',
            refreshed_count,
            failed_count,
        )
    return refreshed_count, failed_count


async def cleanup_video_generation_tasks() -> int:
    """清理超过保留期的终态视频任务记录。"""

    settings = get_settings_service().settings
    hold_days = max(int(settings.video_tasks_hold_days or 0), 0)
    if hold_days <= 0:
        return 0

    lease_seconds = max(int(settings.video_tasks_cleanup_interval or 3600), 30)
    owner_token = await _acquire_video_task_lock(
        _VIDEO_TASK_CLEANUP_LOCK_NAME,
        lease_seconds,
    )
    if owner_token is None:
        return 0

    try:
        cutoff = datetime.now(tz=current_timezone()) - timedelta(days=hold_days)
        async with async_session_scope() as session:
            return await delete_video_generation_tasks_before(
                session=session,
                before=cutoff,
            )
    finally:
        await _release_video_task_lock(_VIDEO_TASK_CLEANUP_LOCK_NAME, owner_token)


def recover_video_generation_tasks_task() -> None:
    """同步包装的视频任务恢复定时任务。"""

    try:
        refreshed_count, failed_count = run_until_complete(
            recover_video_generation_tasks()
        )
        if refreshed_count or failed_count:
            logger.info(
                '视频任务恢复轮询完成，刷新 {} 条，标记失败 {} 条',
                refreshed_count,
                failed_count,
            )
    except Exception:  # noqa: BLE001
        logger.exception('视频任务恢复轮询执行失败')


def cleanup_video_generation_tasks_task() -> None:
    """同步包装的视频任务清理定时任务。"""

    try:
        removed_count = run_until_complete(cleanup_video_generation_tasks())
        if removed_count > 0:
            logger.info('已删除 {} 条过期视频任务记录', removed_count)
    except Exception:  # noqa: BLE001
        logger.exception('视频任务清理执行失败')


async def _proxy_video_json_task_request(
    *,
    video_id: str,
    endpoint_suffix: str,
    method: str,
    request_action: RequestAction,
    raw_request: Request,
    nodeproxy_service: NodeProxyService,
    access_ctx: AccessKeyContext,
):
    """Proxy JSON-based follow-up video task requests using stored runtime metadata."""
    task_entry, error_response = await _resolve_video_task_entry(
        video_id=video_id,
        access_ctx=access_ctx,
        nodeproxy_service=nodeproxy_service,
    )
    if error_response is not None:
        return error_response

    node_url = task_entry.node_url
    request_payload = orjson.dumps({'video_id': video_id}).decode('utf-8', errors='ignore')
    client_ip = get_client_real_ip_via_gateway(raw_request)
    try:
        request_ctx = nodeproxy_service.pre_call(
            node_url,
            model_name=task_entry.model_name,
            model_type=ModelType.video_generation.value,
            request_protocol=ProtocolType.openai,
            ownerapp_id=access_ctx.ownerapp_id,
            request_action=request_action,
            request_count=0,
            estimated_total_tokens=None,
            request_data=request_payload,
            client_ip=client_ip,
            api_key_id=access_ctx.api_key_id,
        )
    except (NodeModelQuotaExceeded, ApiKeyQuotaExceeded, AppQuotaExceeded) as exc:
        message = str(exc) or '配额已耗尽'
        logger.warning('配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')
    except NorthboundQuotaProcessingError as exc:
        message = exc.detail or str(exc) or '北向配额处理失败'
        logger.warning('北向配额处理异常: {}', message)
        return create_error_response(HTTPStatus.SERVICE_UNAVAILABLE, message, error_type='service_unavailable_error')

    response = await nodeproxy_service.generate(
        request=None,
        node_url=node_url,
        endpoint=f'/v1/videos/{video_id}{endpoint_suffix}',
        api_key=task_entry.backend_api_key,
        protocol_type=_coerce_protocol_type(task_entry.protocol_type),
        request_proxy_url=task_entry.request_proxy_url,
        method=method,
    )
    request_ctx.response_data = response
    if isinstance(response, str):
        await _store_video_task_response_text(task_entry.id, response)

    try:
        payload = orjson.loads(response)
    except Exception:  # noqa: BLE001
        error_message = f'Failed to decode backend video task response: {response!r}'
        stack = traceback.format_exc()
        _apply_backend_error_info(request_ctx, error_message, stack)
        await _mark_video_task_failed(
            task_id=task_entry.id,
            error_message=error_message,
        )
        nodeproxy_service.post_call(node_url, request_ctx)
        raise

    message, stack = _extract_backend_error(payload)
    _apply_backend_error_info(request_ctx, message, stack)
    await _update_video_task_from_payload(
        task_id=task_entry.id,
        payload=payload,
    )

    nodeproxy_service.post_call(node_url, request_ctx)
    return _build_backend_json_response(payload)


async def _proxy_video_content_request(
    *,
    video_id: str,
    raw_request: Request,
    nodeproxy_service: NodeProxyService,
    access_ctx: AccessKeyContext,
):
    """Proxy binary video download requests using stored runtime metadata."""
    task_entry, error_response = await _resolve_video_task_entry(
        video_id=video_id,
        access_ctx=access_ctx,
        nodeproxy_service=nodeproxy_service,
    )
    if error_response is not None:
        return error_response

    if task_entry.content_data is not None:
        return Response(
            content=task_entry.content_data,
            media_type=task_entry.content_type or 'video/mp4',
            headers={
                'Content-Disposition': f'attachment; filename="{task_entry.content_filename or _default_video_filename(video_id)}"',
            },
        )

    node_url = task_entry.node_url
    request_payload = orjson.dumps({'video_id': video_id, 'content': True}).decode('utf-8', errors='ignore')
    client_ip = get_client_real_ip_via_gateway(raw_request)
    try:
        request_ctx = nodeproxy_service.pre_call(
            node_url,
            model_name=task_entry.model_name,
            model_type=ModelType.video_generation.value,
            request_protocol=ProtocolType.openai,
            ownerapp_id=access_ctx.ownerapp_id,
            request_action=RequestAction.videos_content,
            request_count=0,
            estimated_total_tokens=None,
            request_data=request_payload,
            client_ip=client_ip,
            api_key_id=access_ctx.api_key_id,
        )
    except (NodeModelQuotaExceeded, ApiKeyQuotaExceeded, AppQuotaExceeded) as exc:
        message = str(exc) or '配额已耗尽'
        logger.warning('配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')
    except NorthboundQuotaProcessingError as exc:
        message = exc.detail or str(exc) or '北向配额处理失败'
        logger.warning('北向配额处理异常: {}', message)
        return create_error_response(HTTPStatus.SERVICE_UNAVAILABLE, message, error_type='service_unavailable_error')

    response_payload = await nodeproxy_service.generate(
        request=None,
        node_url=node_url,
        endpoint=f'/v1/videos/{video_id}/content',
        api_key=task_entry.backend_api_key,
        protocol_type=_coerce_protocol_type(task_entry.protocol_type),
        request_proxy_url=task_entry.request_proxy_url,
        method='GET',
        response_mode='bytes',
    )

    if isinstance(response_payload, (bytes, bytearray)):
        try:
            payload = orjson.loads(response_payload)
        except Exception:  # noqa: BLE001
            request_ctx.response_data = f'<binary {len(response_payload)} bytes>'
            await _cache_video_task_content(
                task_id=task_entry.id,
                content_data=bytes(response_payload),
                content_type='video/mp4',
                content_filename=_default_video_filename(video_id),
            )
            nodeproxy_service.post_call(node_url, request_ctx)
            return Response(
                content=bytes(response_payload),
                media_type='video/mp4',
                headers={
                    'Content-Disposition': f'attachment; filename="{_default_video_filename(video_id)}"',
                },
            )

        request_ctx.response_data = orjson.dumps(payload).decode('utf-8', errors='ignore')
        message, stack = _extract_backend_error(payload)
        _apply_backend_error_info(request_ctx, message, stack)
        await _update_video_task_from_payload(
            task_id=task_entry.id,
            payload=payload,
        )
        nodeproxy_service.post_call(node_url, request_ctx)
        return _build_backend_json_response(payload)

    request_ctx.response_data = str(response_payload)
    nodeproxy_service.post_call(node_url, request_ctx)
    return Response(
        content=str(response_payload).encode('utf-8', errors='ignore'),
        media_type='video/mp4',
        headers={
            'Content-Disposition': f'attachment; filename="{video_id}.mp4"',
        },
    )


@router.post('/videos/generations')
async def video_generations_v1(
    request: VideoGenerationRequest,
    raw_request: Request = None,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Video generation API compatible with OpenAI-style specifications."""
    model_type = ModelType.video_generation.value
    check_response = await nodeproxy_service.check_request_model(
        request.model,
        model_type,
        request_protocol=ProtocolType.openai,
        allow_cross_protocol=False,
        effective_allowed_models=access_ctx.effective_allowed_models,
    )
    if check_response is not None:
        return check_response

    try:
        node_url = nodeproxy_service.get_node_url(
            request.model,
            model_type,
            request_protocol=ProtocolType.openai,
            allow_cross_protocol=False,
        )
    except NodeModelQuotaExceeded as exc:
        message = exc.detail or str(exc) or '模型配额已耗尽'
        logger.warning('节点模型配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')
    if not node_url:
        return nodeproxy_service.handle_unavailable_model(request.model, model_type)

    logger.debug('应用 {} 将视频生成请求转发到节点 {}', access_ctx.ownerapp_id, node_url)

    request_dict = request.model_dump(exclude_none=True)
    request_payload = orjson.dumps(request_dict).decode('utf-8', errors='ignore')
    client_ip = get_client_real_ip_via_gateway(raw_request)
    try:
        request_ctx = nodeproxy_service.pre_call(
            node_url,
            model_name=request.model,
            model_type=model_type,
            request_protocol=ProtocolType.openai,
            ownerapp_id=access_ctx.ownerapp_id,
            request_action=RequestAction.videos_generations,
            request_count=0,
            estimated_total_tokens=None,
            request_data=request_payload,
            client_ip=client_ip,
            api_key_id=access_ctx.api_key_id,
        )
    except (NodeModelQuotaExceeded, ApiKeyQuotaExceeded, AppQuotaExceeded) as exc:
        message = str(exc) or '配额已耗尽'
        logger.warning('配额不足: {}', message)
        return create_error_response(HTTPStatus.TOO_MANY_REQUESTS, message, error_type='quota_exceeded')
    except NorthboundQuotaProcessingError as exc:
        message = exc.detail or str(exc) or '北向配额处理失败'
        logger.warning('北向配额处理异常: {}', message)
        return create_error_response(HTTPStatus.SERVICE_UNAVAILABLE, message, error_type='service_unavailable_error')

    status_snapshot = nodeproxy_service.status
    node_status = status_snapshot.get(node_url) if isinstance(status_snapshot, dict) else None
    api_key = getattr(node_status, 'api_key', None) if node_status is not None else None
    target_protocol = getattr(node_status, 'protocol_type', ProtocolType.openai) if node_status is not None else ProtocolType.openai
    request_proxy_url = getattr(node_status, 'request_proxy_url', None) if node_status is not None else None

    task_id = await _create_video_task_dispatch_record(
        request=request,
        request_dict=request_dict,
        node_url=node_url,
        api_key=api_key,
        protocol_type=_coerce_protocol_type(target_protocol),
        request_proxy_url=request_proxy_url,
        request_ctx=request_ctx,
        access_ctx=access_ctx,
    )

    response = await nodeproxy_service.generate(
        request_dict,
        node_url,
        '/v1/videos/generations',
        api_key,
        protocol_type=target_protocol,
        request_proxy_url=request_proxy_url,
    )
    request_ctx.response_data = response
    if isinstance(response, str):
        await _store_video_task_response_text(task_id, response)

    try:
        payload = orjson.loads(response)
    except Exception:  # noqa: BLE001
        error_message = f'Failed to decode backend video response: {response!r}'
        stack = traceback.format_exc()
        _apply_backend_error_info(request_ctx, error_message, stack)
        await _mark_video_task_failed(
            task_id=task_id,
            error_message=error_message,
        )
        nodeproxy_service.post_call(node_url, request_ctx)
        raise

    message, stack = _extract_backend_error(payload)
    _apply_backend_error_info(request_ctx, message, stack)
    await _update_video_task_from_payload(
        task_id=task_id,
        payload=payload,
        store_create_payload=True,
    )

    nodeproxy_service.post_call(node_url, request_ctx)
    return _build_backend_json_response(payload)


@router.get('/videos/{video_id}')
async def video_retrieve_v1(
    video_id: str,
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Retrieve a tracked video generation task."""
    return await _proxy_video_json_task_request(
        video_id=video_id,
        endpoint_suffix='',
        method='GET',
        request_action=RequestAction.videos_retrieve,
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
    )


@router.post('/videos/{video_id}/cancel')
async def video_cancel_v1(
    video_id: str,
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Cancel a tracked video generation task."""
    return await _proxy_video_json_task_request(
        video_id=video_id,
        endpoint_suffix='/cancel',
        method='POST',
        request_action=RequestAction.videos_cancel,
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
    )


@router.get('/videos/{video_id}/content')
async def video_content_v1(
    video_id: str,
    raw_request: Request,
    nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
    access_ctx: AccessKeyContext = Depends(check_access_key),
):
    """Download the binary content for a tracked video generation task."""
    return await _proxy_video_content_request(
        video_id=video_id,
        raw_request=raw_request,
        nodeproxy_service=nodeproxy_service,
        access_ctx=access_ctx,
    )