"""cached_tokens 独立存储与 token 恒等式闭环测试。

验证核心逻辑：
1. _apply_usage_to_context：prompt_tokens 保持原值不扣减 cached_tokens，cached_tokens 独立存储
2. _resolve_total_tokens：始终通过 request + response 计算，不取上游 total
3. _finalize_token_counts：兜底场景下 total 仍为 request + response
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

import pytest


@dataclass
class _MockRequestContext:
    """模拟 _RequestContext，仅保留 token 相关字段。"""

    start_time: float = 0.0
    request_id: object = field(default_factory=uuid4)
    request_tokens: Optional[int] = None
    response_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None


# ── _apply_usage_to_context 测试 ──────────────────────────────


class TestApplyUsageToContext:
    """验证 _apply_usage_to_context 的 token 解析和恒等式。"""

    def test_prompt_tokens_not_deducted_by_cached_tokens(self):
        """prompt_tokens 保持上游原值，cached_tokens 独立存储。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
        _apply_usage_to_context(ctx, usage)

        assert ctx.request_tokens == 100
        assert ctx.cached_tokens == 80
        assert ctx.response_tokens == 50
        # 恒等式：total = request + response
        assert ctx.total_tokens == 150

    def test_total_tokens_ignores_upstream_value(self):
        """上游 total_tokens 与 request+response 不一致时，采用加法计算。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 999,  # 上游 total 不等于 request+response
        }
        _apply_usage_to_context(ctx, usage)

        assert ctx.total_tokens == 300
        assert ctx.total_tokens != 999

    def test_cached_tokens_without_prompt_details(self):
        """没有 prompt_tokens_details 时 cached_tokens 保持 None。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "prompt_tokens": 50,
            "completion_tokens": 30,
            "total_tokens": 80,
        }
        _apply_usage_to_context(ctx, usage)

        assert ctx.cached_tokens is None
        assert ctx.request_tokens == 50
        assert ctx.response_tokens == 30
        assert ctx.total_tokens == 80

    def test_input_tokens_as_fallback_for_prompt(self):
        """上游使用 input_tokens 而非 prompt_tokens 时正确解析。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "input_tokens": 120,
            "output_tokens": 60,
            "total_tokens": 180,
        }
        _apply_usage_to_context(ctx, usage)

        assert ctx.request_tokens == 120
        assert ctx.response_tokens == 60
        assert ctx.total_tokens == 180

    def test_cached_tokens_from_input_tokens_details(self):
        """Anthropic 风格 usage 中 cached_tokens 也能独立提取。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "input_tokens": 200,
            "output_tokens": 80,
            "total_tokens": 280,
            "prompt_tokens_details": {"cached_tokens": 150},
        }
        _apply_usage_to_context(ctx, usage)

        assert ctx.request_tokens == 200
        assert ctx.cached_tokens == 150
        assert ctx.response_tokens == 80
        assert ctx.total_tokens == 280

    def test_reasoning_tokens_added_to_response(self):
        """completion_tokens_details.reasoning_tokens 追加到 response。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
        _apply_usage_to_context(ctx, usage)

        # response = completion(50) + reasoning(30) = 80
        assert ctx.response_tokens == 80
        # total = request(100) + response(80) = 180
        assert ctx.total_tokens == 180

    def test_empty_usage_dict_is_noop(self):
        """空 usage 字典不修改 context。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        _apply_usage_to_context(ctx, {})

        assert ctx.request_tokens is None
        assert ctx.response_tokens is None
        assert ctx.total_tokens == 0  # None + None → 0
        assert ctx.cached_tokens is None

    def test_zero_cached_tokens_not_stored(self):
        """cached_tokens=0 时不写入 context（保持 None）。"""
        from openaiproxy.api.v1.completions import _apply_usage_to_context

        ctx = _MockRequestContext()
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        _apply_usage_to_context(ctx, usage)

        assert ctx.cached_tokens is None
        assert ctx.request_tokens == 100
        assert ctx.total_tokens == 150


# ── _resolve_total_tokens 测试 ────────────────────────────────


class TestResolveTotalTokens:
    """验证 _resolve_total_tokens 始终通过加法计算。"""

    def test_normal_case(self):
        """正常场景：total = request + response。"""
        from openaiproxy.services.nodeproxy.service import NodeProxyService

        ctx = _MockRequestContext(
            request_tokens=100,
            response_tokens=50,
            total_tokens=999,  # 上游值应被忽略
        )
        result = NodeProxyService._resolve_total_tokens(ctx)
        assert result == 150

    def test_none_values_treated_as_zero(self):
        """request/response 为 None 时按 0 处理。"""
        from openaiproxy.services.nodeproxy.service import NodeProxyService

        ctx = _MockRequestContext(
            request_tokens=None,
            response_tokens=None,
            total_tokens=100,
        )
        result = NodeProxyService._resolve_total_tokens(ctx)
        assert result == 0

    def test_only_request_tokens(self):
        """只有 request_tokens 时 total = request。"""
        from openaiproxy.services.nodeproxy.service import NodeProxyService

        ctx = _MockRequestContext(
            request_tokens=200,
            response_tokens=None,
        )
        result = NodeProxyService._resolve_total_tokens(ctx)
        assert result == 200

    def test_negative_result_returns_zero(self):
        """计算结果为负时返回 0。"""
        from openaiproxy.services.nodeproxy.service import NodeProxyService

        ctx = _MockRequestContext(
            request_tokens=-100,
            response_tokens=50,
        )
        result = NodeProxyService._resolve_total_tokens(ctx)
        assert result == 0


# ── _finalize_token_counts 测试 ───────────────────────────────


class TestFinalizeTokenCounts:
    """验证 _finalize_token_counts 在兜底场景下仍保证恒等式。"""

    def test_total_is_sum_of_request_and_response(self):
        """兜底估算后 total 仍为 request + response。"""
        from openaiproxy.api.v1.completions import _finalize_token_counts

        ctx = _MockRequestContext(
            request_tokens=100,
            response_tokens=50,
            total_tokens=999,
        )
        _finalize_token_counts(
            request_ctx=ctx,
            prompt_estimate=0,
            completion_segments=[],
            model_name="gpt-4o-mini",
        )
        assert ctx.total_tokens == 150

    def test_fallback_estimates_used_when_upstream_missing(self):
        """上游未返回 token 时用估算值兜底，total 仍为加法。"""
        from openaiproxy.api.v1.completions import _finalize_token_counts

        ctx = _MockRequestContext()
        _finalize_token_counts(
            request_ctx=ctx,
            prompt_estimate=80,
            completion_segments=["Hello world response"],
            model_name="gpt-4o-mini",
        )
        # request_tokens 应被设为 prompt_estimate
        assert ctx.request_tokens == 80
        # response_tokens 应被估算（>0）
        assert ctx.response_tokens is not None and ctx.response_tokens > 0
        # 恒等式
        assert ctx.total_tokens == ctx.request_tokens + ctx.response_tokens
