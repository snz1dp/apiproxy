"""文生图 Provider 适配器抽象基类。

定义 ImageGenerationAdapter 接口，所有 Provider 适配器必须实现此接口。
同时定义 ProviderRequest 数据类用于描述下游请求。

当前适配器职责：
- build_request: 组装 Provider 原生请求（endpoint、headers、body）
- get_model_capabilities: 返回模型能力描述

响应处理采用透传策略，不做格式转换。
未来如需提取 usage 信息（input_tokens、output_tokens、image_count 等），
可新增 extract_usage 可选钩子方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from openaiproxy.services.imagegen.capability import ImageModelCapability


@dataclass
class ProviderRequest:
    """适配后的下游请求描述。

    Attributes:
        endpoint: 下游 API 路径
        method: HTTP 方法
        headers: 额外请求头
        json_body: JSON 请求体
        timeout: 超时覆盖（秒）
    """

    endpoint: str
    """下游 API 路径"""

    method: str = "POST"
    """HTTP 方法"""

    headers: dict[str, str] = field(default_factory=dict)
    """额外请求头"""

    json_body: Optional[dict[str, Any]] = None
    """JSON 请求体"""

    timeout: Optional[float] = None
    """超时覆盖（秒）"""


class ImageGenerationAdapter(ABC):
    """文生图 Provider 适配器抽象基类。

    所有 Provider 适配器必须实现此接口，包括：
    - build_request: 从原始请求 payload 中提取所需参数，组装 Provider 原生请求
    - get_model_capabilities: 返回该 Provider 下所有已知模型的能力描述
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """适配器标识名，如 'dashscope', 'openai'。"""
        ...

    @abstractmethod
    def build_request(
        self,
        payload: dict[str, Any],
        api_key: str,
        node_url: str,
    ) -> ProviderRequest:
        """从原始请求 payload 中提取所需参数，组装 Provider 原生请求。

        Args:
            payload: 原始请求体字典
            api_key: 节点的 API Key
            node_url: 节点基础 URL

        Returns:
            转换后的下游请求描述
        """
        ...

    @abstractmethod
    def get_model_capabilities(self) -> list[ImageModelCapability]:
        """返回该 Provider 下所有已知模型的能力描述。

        Returns:
            模型能力列表
        """
        ...

    def get_model_capability(self, model_name: str) -> Optional[ImageModelCapability]:
        """查询单个模型的能力描述。

        Args:
            model_name: 模型名称

        Returns:
            模型能力描述，未找到时返回 None
        """
        for capability in self.get_model_capabilities():
            if capability.model_name == model_name:
                return capability
        return None
