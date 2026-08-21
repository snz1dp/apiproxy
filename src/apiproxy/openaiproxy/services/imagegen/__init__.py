"""文生图多模型适配组件。

提供 Provider Adapter 插件化 + 模型能力描述两层架构，
使代理服务能够适配任意文生图后端（DashScope、OpenAI 等）。

当前职责：
- build_request: 由 Adapter 组装 Provider 原生请求（endpoint、headers、body）
- get_model_capabilities: 提供模型能力描述供客户端查询
- 响应处理：直接透传模型原始返回，不做格式转换

未来扩展方向：
- 新增 extract_usage 钩子，用于从响应中提取 input_tokens、output_tokens、
  缓存命中 token、图片生成数量等计费/统计字段
"""
