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

import json
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from openaiproxy.api.schemas import RerankRequest
from openaiproxy.api.utils import AccessKeyContext, check_access_key
from openaiproxy.logging import logger
from openaiproxy.services.database.models.proxy.model import RequestAction
from openaiproxy.services.deps import get_node_proxy_service
from openaiproxy.services.database.models.node.model import ModelType
from openaiproxy.services.nodeproxy.service import NodeProxyService


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
		except Exception:  # noqa: BLE001 - defensive
			pass
	approx = len(text) // 4
	if approx == 0:
		return len(text.split()) or 0
	return approx


def _flatten_rerank_inputs(raw_input: Any) -> Iterable[str]:
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


def _estimate_rerank_prompt_tokens(request: RerankRequest) -> int:
	"""Estimate token count for rerank request (query + documents)."""
	total = 0
	# Estimate from query
	for segment in _flatten_rerank_inputs(request.query):
		total += _estimate_tokens(segment, request.model)
	# Estimate from documents
	docs = request.documents or []
	for doc in docs:
		for segment in _flatten_rerank_inputs(doc):
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
	if total_tokens is None:
		total_tokens = prompt_tokens

	if prompt_tokens is not None and prompt_tokens >= 0:
		request_ctx.request_tokens = prompt_tokens
	if total_tokens is not None and total_tokens >= 0:
		request_ctx.response_tokens = total_tokens


router = APIRouter(tags=["OpenAI兼容接口"])


@router.post('/rerank')
async def rerank_v1(
	request: RerankRequest,
	raw_request: Request = None,
	nodeproxy_service: NodeProxyService = Depends(get_node_proxy_service),
	access_ctx: AccessKeyContext = Depends(check_access_key),
):
	"""Rerank API compatible with OpenAI-style rerank semantics.

	The endpoint forwards the request to a selected node that exposes a
	rerank-capable model and records basic token usage estimates.
	"""
	model_type = ModelType.rerank.value
	check_response = await nodeproxy_service.check_request_model(request.model, model_type)
	if check_response is not None:
		return check_response

	node_url = nodeproxy_service.get_node_url(request.model, model_type)
	if not node_url:
		return nodeproxy_service.handle_unavailable_model(request.model, model_type)

	logger.debug('应用 {} 将请求转发到节点 {}', access_ctx.ownerapp_id, node_url)

	request_dict = request.model_dump(exclude_none=True)
	prompt_token_estimate = _estimate_rerank_prompt_tokens(request)
	request_ctx = nodeproxy_service.pre_call(
		node_url,
		model_name=request.model,
		ownerapp_id=access_ctx.ownerapp_id,
		request_action=RequestAction.rerankdocs,
		request_count=prompt_token_estimate,
	)

	status_snapshot = nodeproxy_service.status
	node_status = status_snapshot.get(node_url) if isinstance(status_snapshot, dict) else None
	api_key = getattr(node_status, 'api_key', None) if node_status is not None else None

	response = await nodeproxy_service.generate(
		request_dict,
		node_url,
		'/v1/rerank',
		api_key,
	)

	payload = json.loads(response)
	_finalize_embedding_usage(
		payload=payload,
		request_ctx=request_ctx,
		prompt_estimate=prompt_token_estimate,
	)

	nodeproxy_service.post_call(node_url, request_ctx)
	return JSONResponse(payload)

