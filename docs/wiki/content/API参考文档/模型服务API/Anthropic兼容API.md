# Anthropic兼容API

## 简介

本页描述项目新增的 Anthropic 兼容北向接口。当前实现统一挂载在 `/v1` 前缀下，覆盖以下端点：

- `POST /v1/messages`
- `POST /v1/messages/count_tokens`
- `POST /v1/messages/batches`
- `GET /v1/messages/batches`
- `GET /v1/messages/batches/{batch_id}`
- `POST /v1/messages/batches/{batch_id}/cancel`
- `GET /v1/messages/batches/{batch_id}/results`
- `GET /v1/models` 的 Anthropic 兼容返回形状

这些端点由 [src/apiproxy/openaiproxy/api/v1/anthropic.py](file://src/apiproxy/openaiproxy/api/v1/anthropic.py) 提供，协议转换逻辑由 [src/apiproxy/openaiproxy/api/v1/protocol_adapters.py](file://src/apiproxy/openaiproxy/api/v1/protocol_adapters.py) 负责。

## 鉴权与协议识别

- 所有 Anthropic 兼容端点复用 `check_access_key`。
- 请求可以使用 `Authorization: Bearer <token>`，也可以使用 `x-api-key: <token>`。
- `/v1/messages*` 路径会直接被识别为 Anthropic 北向协议。
- `GET /v1/models` 在携带 `x-api-key` 时会返回 Anthropic 模型列表结构。

## 路由与转换规则

### Messages

- 路径：`POST /v1/messages`
- 必填字段：`model`、`messages`
- 常用可选字段：`system`、`max_tokens`、`stream`、`temperature`、`top_p`、`stop_sequences`、`tools`

行为说明：

- 当目标节点支持 Anthropic 协议时，代理层直接调用下游 `/v1/messages`。
- 当目标节点仅支持 OpenAI 协议时，代理层会把 Anthropic `messages` 请求转换成 OpenAI `chat/completions` 请求，再把响应转换回 Anthropic 结构。
- 流式请求会返回 Anthropic 风格 SSE；如果下游是 OpenAI 节点，则会把 OpenAI 的 chunk 转换为 Anthropic 事件。

非流式响应核心字段：

- `id`
- `type`，固定为 `message`
- `role`，固定为 `assistant`
- `model`
- `content`，当前主要输出 `text` block
- `stop_reason`
- `usage.input_tokens`
- `usage.output_tokens`

错误响应结构：

- `type: error`
- `error.type`
- `error.message`

### Count Tokens

- 路径：`POST /v1/messages/count_tokens`
- 必填字段：`model`、`messages`

行为说明：

- 当目标节点支持 Anthropic 协议时，直接透传到下游 `/v1/messages/count_tokens`。
- 当目标节点仅支持 OpenAI 协议时，不会访问下游，而是在代理层基于 `system + messages` 做本地 token 估算。

响应字段：

- `input_tokens`

### Message Batches

- 创建：`POST /v1/messages/batches`
- 列表：`GET /v1/messages/batches`
- 详情：`GET /v1/messages/batches/{batch_id}`
- 取消：`POST /v1/messages/batches/{batch_id}/cancel`
- 结果：`GET /v1/messages/batches/{batch_id}/results`

行为说明：

- 若目标节点支持 Anthropic 协议，批任务接口会原生透传到下游 Anthropic 服务。
- 若目标节点仅支持 OpenAI 协议，代理层会把 `requests[].params` 中的 Anthropic 请求逐条转换为非流式 OpenAI `chat/completions` 调用，再把每条结果汇总为本地批任务。
- 本地批任务状态与结果只保存在代理进程内存中，适合作为兼容与调试能力，而非持久化任务系统。

创建批任务时至少需要：

- `requests` 数组
- `requests[].params.model`

批状态响应核心字段：

- `id`
- `type`，固定为 `message_batch`
- `processing_status`
- `request_counts.processing`
- `request_counts.succeeded`
- `request_counts.errored`
- `results_url`

批结果响应结构：

- `data[]`
- `data[].custom_id`
- `data[].result.type`
- `data[].result.message`

## Models 接口的 Anthropic 兼容形状

`GET /v1/models` 在 Anthropic 协议下返回：

- `data`
- `first_id`
- `last_id`
- `has_more`

其中 `data[]` 元素字段为：

- `type`，固定为 `model`
- `id`
- `display_name`
- `created_at`

## 实现约束

- `Embeddings` 与 `Rerank` 仍然是 OpenAI 兼容接口，不提供 Anthropic 伪转换入口。
- `messages/count_tokens` 在 OpenAI-only 节点上返回的是代理本地估算值，不是下游原生计数结果。
- `messages/batches` 的 OpenAI 兼容分支依赖进程内存维护批状态，代理重启后本地批结果不会保留。

## 相关文件

- [src/apiproxy/openaiproxy/api/v1/anthropic.py](file://src/apiproxy/openaiproxy/api/v1/anthropic.py)
- [src/apiproxy/openaiproxy/api/v1/protocol_adapters.py](file://src/apiproxy/openaiproxy/api/v1/protocol_adapters.py)
- [src/apiproxy/openaiproxy/api/v1/models.py](file://src/apiproxy/openaiproxy/api/v1/models.py)
- [src/apiproxy/openaiproxy/api/utils.py](file://src/apiproxy/openaiproxy/api/utils.py)
- [src/apiproxy/openaiproxy/services/nodeproxy/service.py](file://src/apiproxy/openaiproxy/services/nodeproxy/service.py)
