"""OpenAI 兼容透传适配器。

对于已经兼容 OpenAI images/generations 接口的后端节点，
此适配器直接透传请求，不做任何转换。
"""

from __future__ import annotations

from typing import Any

from openaiproxy.services.imagegen.adapters.base import (
    ImageGenerationAdapter,
    ProviderRequest,
)
from openaiproxy.services.imagegen.capability import ImageModelCapability


class OpenAIPassthroughAdapter(ImageGenerationAdapter):
    """OpenAI 兼容透传适配器。

    请求保持 OpenAI 格式不变，直接透传到后端。
    """

    @property
    def provider_name(self) -> str:
        """适配器标识名。"""
        return "openai"

    def build_request(
        self,
        payload: dict[str, Any],
        api_key: str,
        node_url: str,
    ) -> ProviderRequest:
        """直接透传原始 payload 到后端。

        Args:
            payload: 原始请求体字典
            api_key: 节点的 API Key
            node_url: 节点基础 URL

        Returns:
            OpenAI 格式的透传请求
        """
        return ProviderRequest(
            endpoint="/v1/images/generations",
            headers={
                "Content-Type": "application/json",
            },
            json_body=payload,
        )

    def get_model_capabilities(self) -> list[ImageModelCapability]:
        """返回 OpenAI 通用能力描述。

        Returns:
            通用能力描述列表
        """
        return [
            ImageModelCapability(
                model_name="dall-e-3",
                provider="openai",
                supported_sizes=["1024x1024", "1792x1024", "1024x1792"],
                default_size="1024x1024",
                supports_reference_image=False,
                n_range=(1, 1),
                supports_negative_prompt=False,
                supports_seed=True,
            ),
            ImageModelCapability(
                model_name="dall-e-2",
                provider="openai",
                supported_sizes=["1024x1024", "512x512", "256x256"],
                default_size="1024x1024",
                supports_reference_image=False,
                n_range=(1, 10),
                supports_negative_prompt=False,
                supports_seed=False,
            ),
        ]
