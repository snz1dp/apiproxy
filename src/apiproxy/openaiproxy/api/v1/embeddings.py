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

from http import HTTPStatus
import orjson
import traceback
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from openaiproxy.api.schemas import EmbeddingsRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.api.v1.completions import (
	_build_openai_quota_exceeded_response,
	_build_openai_service_unavailable_response,
	_prepare_proxy_attempt,
	_resolve_default_target_protocol,
	_retry_proxy_attempt_after_capacity_exhausted,
)
from openaiproxy.logging import logger
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.database.models.node.model import ModelType, ProtocolType
from openaiproxy.services.nodeproxy.exceptions import (
    NodeModelQuotaExceeded,
)
from openaiproxy.services.nodeproxy.service import NodeProxyService, create_error_response
from openaiproxy.utils.viagateway import get_client_real_ip_via_gateway

try:  # pragma: no cover - optional dependency
	import tiktoken  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency
	tiktoken = None

_ENCODING_CACHE: Dict[str, Any] = {}


def _normalize_content_to_text(content: Any) -> str:
	if content is None:
		return ''
	if isinstance(content, str):
		return content
	if isinstance(content, bytes):
		try:
			return content.decode('utf-8', errors='ignore')
		except Exception:  # noqa: BLE001 - defensive
			return ''
	if isinstance(content, (int, float, bool)):
		return str(content)
	if isinstance(content, list):
		return ''.join(_normalize_content_to_text(item) for item in content)
	if isinstance(content, dict):
		parts: List[str] = []
		for key in ('text', 'content', 'value'):
			if key in content:
				parts.append(_normalize_content_to_text(content[key]))
		if not parts and 'message' in content:
			parts.append(_normalize_content_to_text(content['message']))
		if not parts and 'arguments' in content:
			parts.append(_normalize_content_to_text(content['arguments']))
		return ''.join(parts)
	return str(content)


def _get_tiktoken_encoding(model: Optional[str]) -> Any:
	if tiktoken is None:
		return None
	cache_key = model or 'default'
	if cache_key in _ENCODING_CACHE:
		return _ENCODING_CACHE[cache_key]
	encoding = None
	try:
		encoding = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding('cl100k_base')
	except Exception:  # noqa: BLE001 - fall back to default encoding
		try:
			encoding = tiktoken.get_encoding('cl100k_base')
		except Exception:  # noqa: BLE001 - optional dependency
			encoding = None
	if encoding is not None:
		_ENCODING_CACHE[cache_key] = encoding
	return encoding


def _estimate_tokens(text: str, model: Optional[str]) -> int:
	if not text:
		return 0
	encoding = _get_tiktoken_encoding(model)
	if encoding is not None:
		try:
			return len(encoding.encode(text))
		except Exception:  # noqa: BLE001 - heuristic fallback
			pass
	approx = len(text) // 4
	if approx == 0:
		return len(text.split()) or 0
	return approx


def _flatten_embedding_inputs(raw_input: Any) -> Iterable[str]:
	if isinstance(raw_input, str):
		yield raw_input
		return
	if isinstance(raw_input, (bytes, bytearray)):
		try:
			yield raw_input.decode('utf-8', errors='ignore')
		except Exception:  # noqa: BLE001 - defensive
			return
		return
	if isinstance(raw_input, list):
		for item in raw_input:
			yield _normalize_content_to_text(item)
		return
	yield _normalize_content_to_text(raw_input)


def _estimate_embedding_prompt_tokens(request: EmbeddingsRequest) -> int:
	total = 0
	for segment in _flatten_embedding_inputs(request.input):
		total += _estimate_tokens(segment, request.model)
	return total


def _finalize_embedding_usage(
	*,
	payload: Any,
	request_ctx,
	prompt_estimate: int,
) -> None:
	prompt_tokens: Optional[int] = None
	total_tokens: Optional[int] = None
	if isinstance(payload, dict):
		usage = payload.get('usage')
		if isinstance(usage, dict):
			prompt_value = usage.get('prompt_tokens')
			if isinstance(prompt_value, int) and prompt_value >= 0:
				prompt_tokens = prompt_value
			total_value = usage.get('total_tokens')
			if isinstance(total_value, int) and total_value >= 0:
				total_tokens = total_value

	if prompt_tokens is None and prompt_estimate > 0:
		prompt_tokens = prompt_estimate
	if prompt_tokens is not None and prompt_tokens >= 0:
		request_ctx.request_tokens = prompt_tokens
	if total_tokens is None:
		total_tokens = prompt_tokens

	if total_tokens is not None and total_tokens >= 0:
		request_ctx.total_tokens = total_tokens
		completion_tokens = 0
		if prompt_tokens is not None and prompt_tokens >= 0:
			completion_tokens = max(total_tokens - prompt_tokens, 0)
		else:
			completion_tokens = total_tokens
		request_ctx.response_tokens = completion_tokens


def _to_error_text(value: Any) -> Optional[str]:
	if value is None:
		return None
	if isinstance(value, str):
		text = value.strip()
		return text or None
	if isinstance(value, (int, float, bool)):
		return str(value)
	try:
		serialized = orjson.dumps(value).decode('utf-8')
	except (TypeError, ValueError):  # noqa: BLE001 - defensive
		serialized = str(value)
	serialized = serialized.strip()
	return serialized or None


def _to_error_stack(value: Any) -> Optional[str]:
	if value is None:
		return None
	if isinstance(value, str):
		text = value.strip()
		return text or value
	if isinstance(value, list):
		parts = [str(item).rstrip() for item in value if item is not None]
		joined = '\n'.join(parts).strip()
		return joined or None
	return _to_error_text(value)


def _extract_backend_error(payload: Any) -> Tuple[Optional[str], Optional[str]]:
	message: Optional[str] = None
	stack: Optional[str] = None

	if isinstance(payload, dict):
		error_obj = payload.get('error')
		if isinstance(error_obj, dict):
			message = (
				_to_error_text(error_obj.get('message'))
				or _to_error_text(error_obj.get('text'))
				or _to_error_text(error_obj.get('detail'))
				or _to_error_text(error_obj.get('code'))
			)
			stack = (
				_to_error_stack(error_obj.get('stack'))
				or _to_error_stack(error_obj.get('stack_trace'))
				or _to_error_stack(error_obj.get('traceback'))
			)
			data_obj = error_obj.get('data') if isinstance(error_obj.get('data'), dict) else None
			if stack is None and data_obj is not None:
				stack = (
					_to_error_stack(data_obj.get('stack'))
					or _to_error_stack(data_obj.get('stack_trace'))
					or _to_error_stack(data_obj.get('traceback'))
				)
		elif isinstance(error_obj, str):
			message = _to_error_text(error_obj)
		elif error_obj is not None:
			message = _to_error_text(error_obj)

		if message is None:
			for key in ('message', 'text', 'detail', 'error_message', 'errorDescription'):
				candidate = _to_error_text(payload.get(key))
				if candidate:
					message = candidate
					break

		if stack is None:
			for key in ('error_stack', 'stack', 'stack_trace', 'traceback'):
				candidate = _to_error_stack(payload.get(key))
				if candidate:
					stack = candidate
					break

		if message is None and payload.get('error_code') is not None:
			message = _to_error_text(payload.get('text') or payload.get('message'))
			if message is None:
				message = f'error_code={payload.get("error_code")}'
	elif isinstance(payload, str):
		stripped = payload.strip()
		message = stripped or payload

	return message, stack


def _apply_backend_error_info(request_ctx, message: Optional[str], stack: Optional[str]) -> None:
	if not message and not stack:
		return
	if message and not request_ctx.error_message:
		request_ctx.error_message = message
	if stack and not request_ctx.error_stack:
		request_ctx.error_stack = stack
	request_ctx.error = True


router = APIRouter(tags=["OpenAI兼容接口"])


@router.post('/embeddings')
async def embeddings_v1(
	request: EmbeddingsRequest,
	raw_request: Request = None,
	nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
	access_ctx: AccessKeyContext = Depends(check_access_key),
):
	"""Embedding API compatible with OpenAI's specification."""
	model_type = ModelType.embeddings.value
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

	logger.debug('应用 {} 将请求转发到节点 {}', access_ctx.ownerapp_id, node_url)

	request_dict = request.model_dump(exclude_none=True)
	request_payload = orjson.dumps(request_dict).decode('utf-8', errors='ignore')
	prompt_token_estimate = _estimate_embedding_prompt_tokens(request)
	total_token_estimate = prompt_token_estimate
	client_ip = get_client_real_ip_via_gateway(raw_request)
	error_response, attempt = _prepare_proxy_attempt(
		nodeproxy_service=nodeproxy_service,
		node_url=node_url,
		model_name=request.model,
		model_type=model_type,
		request_protocol=ProtocolType.openai,
		ownerapp_id=access_ctx.ownerapp_id,
		request_action=RequestAction.embeddings,
		request_count=prompt_token_estimate,
		estimated_total_tokens=total_token_estimate,
		stream=False,
		request_data=request_payload,
		client_ip=client_ip,
		api_key_id=access_ctx.api_key_id,
		protocol_resolver=_resolve_default_target_protocol,
		quota_error_builder=_build_openai_quota_exceeded_response,
		service_unavailable_builder=_build_openai_service_unavailable_response,
	)
	if error_response is not None:
		return error_response
	assert attempt is not None

	attempted_node_urls: set[str] = set()
	while True:
		response = await nodeproxy_service.generate(
			request_dict,
			attempt.node_url,
			'/v1/embeddings',
			attempt.api_key,
			protocol_type=attempt.target_protocol,
			request_proxy_url=attempt.request_proxy_url,
		)

		try:
			payload = orjson.loads(response)
		except Exception:  # noqa: BLE001
			error_message = f'Failed to decode backend embeddings response: {response!r}'
			stack = traceback.format_exc()
			_apply_backend_error_info(attempt.request_ctx, error_message, stack)
			nodeproxy_service.post_call(attempt.node_url, attempt.request_ctx)
			raise

		if NodeProxyService.is_backend_capacity_exhausted_error(payload):
			error_response, next_attempt = _retry_proxy_attempt_after_capacity_exhausted(
				nodeproxy_service=nodeproxy_service,
				current_attempt=attempt,
				payload=payload,
				attempted_node_urls=attempted_node_urls,
				model_name=request.model,
				model_type=model_type,
				request_protocol=ProtocolType.openai,
				allow_cross_protocol=False,
				ownerapp_id=access_ctx.ownerapp_id,
				request_action=RequestAction.embeddings,
				request_count=prompt_token_estimate,
				estimated_total_tokens=total_token_estimate,
				stream=False,
				request_data=request_payload,
				client_ip=client_ip,
				api_key_id=access_ctx.api_key_id,
				protocol_resolver=_resolve_default_target_protocol,
				quota_error_builder=_build_openai_quota_exceeded_response,
				service_unavailable_builder=_build_openai_service_unavailable_response,
				request_label='Embeddings 请求',
			)
			if error_response is not None:
				return error_response
			assert next_attempt is not None
			attempt = next_attempt
			continue

		attempt.request_ctx.response_data = response
		message, stack = _extract_backend_error(payload)
		_apply_backend_error_info(attempt.request_ctx, message, stack)
		_finalize_embedding_usage(
			payload=payload,
			request_ctx=attempt.request_ctx,
			prompt_estimate=prompt_token_estimate,
		)

		nodeproxy_service.post_call(attempt.node_url, attempt.request_ctx)
		return JSONResponse(payload)

