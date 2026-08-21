"""DashScope（通义万相）文生图适配器。

适配阿里云 DashScope 平台的文生图接口，
当前支持 wan2.7-image 模型。
每个模型直接从原始请求 payload 中提取自己需要的参数组装请求。
"""

from __future__ import annotations

from typing import Any

from openaiproxy.services.imagegen.adapters.base import (
    ImageGenerationAdapter,
    ProviderRequest,
)
from openaiproxy.services.imagegen.capability import ImageModelCapability


class DashScopeImageAdapter(ImageGenerationAdapter):
    """DashScope 文生图适配器。

    适配阿里云 DashScope 平台的文生图接口，
    当前支持 wan2.7-image 模型。
    每个模型直接从原始 payload 中提取所需参数组装 DashScope 原生请求。
    """

    @property
    def provider_name(self) -> str:
        """适配器标识名。"""
        return "dashscope"

    def build_request(
        self,
        payload: dict[str, Any],
        api_key: str,
        node_url: str,
    ) -> ProviderRequest:
        """从原始 payload 中提取参数，组装 DashScope 原生请求。

        Args:
            payload: 原始请求体字典
            api_key: 节点的 API Key
            node_url: 节点基础 URL

        Returns:
            DashScope 格式的请求描述
        """
        model_name = payload.get("model", "")

        # 构建 input 部分
        input_body: dict[str, Any] = payload.get("input", {})

        # 构建 parameters 部分
        parameters: dict[str, Any] = payload.get("parameters", {})

        # 组装最终请求体
        body: dict[str, Any] = {
            "model": model_name,
            "input": input_body,
            "parameters": parameters,
        }

        return ProviderRequest(
            endpoint="/services/aigc/multimodal-generation/generation",
            headers={
                "Content-Type": "application/json",
            },
            json_body=body,
        )

    # TODO: 考虑去除模型能力，服务只做转发
    def get_model_capabilities(self) -> list[ImageModelCapability]:
        """返回 DashScope 下所有已知模型的能力描述。

        Returns:
            模型能力列表
        """
        return [
            ImageModelCapability(
                model_name="wan2.7-image",
                provider="dashscope",
                supported_sizes=[
                    "1024*1024", "720*1280", "1280*720",
                    "768*1024", "1024*768", "1K", "2K",
                ],
                default_size="1024*1024",
                supports_reference_image=True,
                max_reference_images=1,
                n_range=(1, 4),
                supports_negative_prompt=True,
                supports_seed=True,
            ),
        ]
