"""文生图适配器注册表。

管理所有已注册的 ImageGenerationAdapter 实例，
支持按 provider 名称查找适配器。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from openaiproxy.services.imagegen.adapters.base import ImageGenerationAdapter


class ImageAdapterRegistry:
    """适配器注册表，支持按 provider 名称查找。

    全局单例，在模块加载时注册所有内置适配器。
    """

    def __init__(self):
        """初始化空的适配器注册表。"""
        self._adapters: dict[str, ImageGenerationAdapter] = {}

    def register(self, adapter: ImageGenerationAdapter) -> None:
        """注册一个适配器实例。

        Args:
            adapter: 实现了 ImageGenerationAdapter 接口的适配器实例
        """
        self._adapters[adapter.provider_name] = adapter

    def get(self, provider_name: str) -> Optional[ImageGenerationAdapter]:
        """按 provider 名称查找适配器。

        Args:
            provider_name: Provider 标识名，如 'dashscope', 'openai'

        Returns:
            对应的适配器实例，未找到时返回 None
        """
        return self._adapters.get(provider_name)

    def list_providers(self) -> list[str]:
        """列出所有已注册的 provider 名称。

        Returns:
            provider 名称列表
        """
        return list(self._adapters.keys())


# 全局注册表实例
image_adapter_registry = ImageAdapterRegistry()
