"""文生图模型能力描述。

定义 ImageModelCapability 数据类，用于向客户端声明模型的参数约束，
包括支持的尺寸、是否支持参考图、风格列表等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ImageModelCapability:
    """文生图模型能力描述，供客户端查询。

    Attributes:
        model_name: 模型名称
        provider: 所属 Provider 标识
        supported_sizes: 支持的尺寸列表（格式如 "1024*1024"）
        default_size: 默认尺寸
        supports_reference_image: 是否支持参考图（图生图）
        max_reference_images: 最大参考图数量
        reference_image_formats: 参考图支持的格式
        supported_styles: 支持的风格列表，None 表示不支持风格选择
        n_range: 生成数量范围 (min, max)
        supports_negative_prompt: 是否支持负向提示词
        supports_seed: 是否支持固定 seed
        extra_parameters: 模型特有的额外参数描述（JSON Schema 风格）
    """

    model_name: str
    """模型名称"""

    provider: str
    """所属 Provider 标识，如 'dashscope', 'openai'"""

    supported_sizes: list[str] = field(default_factory=lambda: ["1024x1024"])
    """支持的尺寸列表"""

    default_size: str = "1024x1024"
    """默认尺寸"""

    supports_reference_image: bool = False
    """是否支持参考图（图生图）"""

    max_reference_images: int = 0
    """最大参考图数量"""

    reference_image_formats: list[str] = field(
        default_factory=lambda: ["png", "jpg", "jpeg", "webp"]
    )
    """参考图支持的格式"""

    supported_styles: Optional[list[str]] = None
    """支持的风格列表，None 表示不支持风格选择"""

    n_range: tuple[int, int] = (1, 1)
    """生成数量范围 (min, max)"""

    supports_negative_prompt: bool = False
    """是否支持负向提示词"""

    supports_seed: bool = False
    """是否支持固定 seed"""

    extra_parameters: dict[str, Any] = field(default_factory=dict)
    """模型特有的额外参数描述，JSON Schema 风格"""

    def to_openai_dict(self) -> dict[str, Any]:
        """序列化为 API 响应字典。

        Returns:
            模型能力描述的字典表示
        """
        result: dict[str, Any] = {
            "model": self.model_name,
            "provider": self.provider,
            "sizes": self.supported_sizes,
            "default_size": self.default_size,
            "supports_reference_image": self.supports_reference_image,
            "supports_negative_prompt": self.supports_negative_prompt,
            "supports_seed": self.supports_seed,
            "n_range": {"min": self.n_range[0], "max": self.n_range[1]},
        }
        if self.supports_reference_image:
            result["max_reference_images"] = self.max_reference_images
            result["reference_image_formats"] = self.reference_image_formats
        if self.supported_styles is not None:
            result["styles"] = self.supported_styles
        if self.extra_parameters:
            result["extra_parameters"] = self.extra_parameters
        return result


