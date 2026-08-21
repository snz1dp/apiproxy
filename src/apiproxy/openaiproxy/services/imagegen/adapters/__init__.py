"""文生图适配器注册入口。

在模块加载时将所有内置适配器注册到全局注册表。
"""

from openaiproxy.services.imagegen.adapters.dashscope import DashScopeImageAdapter
from openaiproxy.services.imagegen.adapters.openai import OpenAIPassthroughAdapter
from openaiproxy.services.imagegen.registry import image_adapter_registry

# 注册所有内置适配器
image_adapter_registry.register(OpenAIPassthroughAdapter())
image_adapter_registry.register(DashScopeImageAdapter())
