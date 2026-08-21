"""文生图适配器单元测试。"""

from openaiproxy.services.imagegen.adapters.dashscope import DashScopeImageAdapter
from openaiproxy.services.imagegen.adapters.openai import OpenAIPassthroughAdapter
from openaiproxy.services.imagegen.registry import ImageAdapterRegistry


class TestDashScopeAdapter:
    """DashScope 适配器测试。"""

    def setup_method(self):
        """每个测试方法前初始化适配器。"""
        self.adapter = DashScopeImageAdapter()

    def test_provider_name(self):
        """测试 provider 名称。"""
        assert self.adapter.provider_name == "dashscope"

    def test_build_request_basic(self):
        """测试基本请求构建（透传 input 和 parameters）。"""
        payload = {
            "model": "wan2.7-image",
            "input": {"prompt": "一只猫"},
            "parameters": {"size": "1024*1024", "n": 2},
        }
        request = self.adapter.build_request(
            payload, api_key="test-key", node_url="https://dashscope.aliyuncs.com"
        )

        assert request.endpoint == "/services/aigc/multimodal-generation/generation"
        assert request.json_body["model"] == "wan2.7-image"
        assert request.json_body["input"] == {"prompt": "一只猫"}
        assert request.json_body["parameters"] == {"size": "1024*1024", "n": 2}

    def test_build_request_empty_input_and_parameters(self):
        """测试无 input 和 parameters 时使用空字典。"""
        payload = {"model": "wan2.7-image"}
        request = self.adapter.build_request(
            payload, api_key="test-key", node_url="https://dashscope.aliyuncs.com"
        )

        assert request.json_body["model"] == "wan2.7-image"
        assert request.json_body["input"] == {}
        assert request.json_body["parameters"] == {}

    def test_build_request_preserves_all_fields(self):
        """测试透传保留所有 input 和 parameters 字段。"""
        payload = {
            "model": "wan2.7-image",
            "input": {
                "prompt": "test",
                "negative_prompt": "模糊",
                "ref_img": "https://example.com/ref.png",
            },
            "parameters": {
                "size": "720*1280",
                "n": 4,
                "seed": 42,
            },
        }
        request = self.adapter.build_request(
            payload, api_key="test-key", node_url="https://dashscope.aliyuncs.com"
        )

        assert request.json_body["input"]["negative_prompt"] == "模糊"
        assert request.json_body["input"]["ref_img"] == "https://example.com/ref.png"
        assert request.json_body["parameters"]["seed"] == 42

    def test_build_request_headers(self):
        """测试请求头设置。"""
        payload = {"model": "wan2.7-image", "input": {"prompt": "test"}}
        request = self.adapter.build_request(
            payload, api_key="test-key", node_url="https://dashscope.aliyuncs.com"
        )
        assert request.headers["Content-Type"] == "application/json"

    def test_get_model_capabilities(self):
        """测试模型能力列表。"""
        capabilities = self.adapter.get_model_capabilities()
        assert len(capabilities) == 1
        assert capabilities[0].model_name == "wan2.7-image"

    def test_get_model_capability_wan27(self):
        """测试 wan2.7-image 能力查询。"""
        capability = self.adapter.get_model_capability("wan2.7-image")
        assert capability is not None
        assert capability.supports_reference_image is True
        assert capability.supports_negative_prompt is True
        assert capability.n_range == (1, 4)
        assert "1024*1024" in capability.supported_sizes

    def test_get_model_capability_unknown(self):
        """测试未知模型能力查询返回 None。"""
        capability = self.adapter.get_model_capability("nonexistent-model")
        assert capability is None


class TestOpenAIPassthroughAdapter:
    """OpenAI 透传适配器测试。"""

    def setup_method(self):
        """每个测试方法前初始化适配器。"""
        self.adapter = OpenAIPassthroughAdapter()

    def test_provider_name(self):
        """测试 provider 名称。"""
        assert self.adapter.provider_name == "openai"

    def test_build_request(self):
        """测试请求构建（直接透传 payload）。"""
        payload = {
            "model": "dall-e-3",
            "prompt": "a sunset",
            "size": "1792x1024",
            "n": 1,
        }
        request = self.adapter.build_request(
            payload, api_key="test-key", node_url="https://api.openai.com"
        )
        assert request.endpoint == "/v1/images/generations"
        assert request.json_body == payload

    def test_build_request_preserves_extra_fields(self):
        """测试透传保留额外字段。"""
        payload = {
            "model": "dall-e-3",
            "prompt": "a sunset",
            "quality": "hd",
            "response_format": "b64_json",
        }
        request = self.adapter.build_request(
            payload, api_key="test-key", node_url="https://api.openai.com"
        )
        assert request.json_body["quality"] == "hd"
        assert request.json_body["response_format"] == "b64_json"

    def test_get_model_capabilities(self):
        """测试模型能力列表。"""
        capabilities = self.adapter.get_model_capabilities()
        assert len(capabilities) >= 2
        model_names = [cap.model_name for cap in capabilities]
        assert "dall-e-3" in model_names
        assert "dall-e-2" in model_names


class TestImageAdapterRegistry:
    """适配器注册表测试。"""

    def test_register_and_get(self):
        """测试注册和获取。"""
        registry = ImageAdapterRegistry()
        adapter = DashScopeImageAdapter()
        registry.register(adapter)
        assert registry.get("dashscope") is adapter

    def test_get_nonexistent(self):
        """测试获取不存在的适配器。"""
        registry = ImageAdapterRegistry()
        assert registry.get("nonexistent") is None

    def test_list_providers(self):
        """测试列出所有 provider。"""
        registry = ImageAdapterRegistry()
        registry.register(DashScopeImageAdapter())
        registry.register(OpenAIPassthroughAdapter())
        providers = registry.list_providers()
        assert "dashscope" in providers
        assert "openai" in providers
