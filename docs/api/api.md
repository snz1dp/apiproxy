# API 接口文档

## 鉴权说明

- 管理接口中的节点管理、节点模型配额、请求日志、API Key 管理使用 `check_api_key`。
- 管理接口中的 API 密钥配额、应用配额使用 `check_strict_api_key`。
- `check_api_key` 在未配置 `APIPROXY_APIKEYS` 时会放行请求；`check_strict_api_key` 在未配置 `APIPROXY_APIKEYS` 时会直接返回 `503`。
- OpenAI 兼容接口（`/v1/*`）默认使用应用 API Key 鉴权（`check_access_key`）；如果请求头中的 Bearer Token 命中 `APIPROXY_APIKEYS`，也会被当作静态访问密钥接受。
- Anthropic 兼容接口同样复用 `check_access_key`，支持 `x-api-key` 作为应用 API Key；其中 `/v1/messages*` 路径会被识别为 Anthropic 北向协议。
- `GET /v1/models` 是协议感知接口：携带 Bearer Token 时默认返回 OpenAI 兼容格式，携带 `x-api-key` 时返回 Anthropic 兼容格式。

---

## OpenAI 兼容接口

说明：除 `/v1/models` 由代理服务本地组装返回外，`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/rerank` 当前实现都会将上游节点返回的 JSON 结果透传给客户端。下文中的响应字段是当前实现兼容的常见结构，最终以实际上游节点返回为准。

### 获取可用模型列表

- **方法**: `GET`
- **路径**: `/v1/models`
- **鉴权**: 应用 API Key
- **说明**: 获取当前可用的模型列表。默认返回 OpenAI 兼容格式；当请求被识别为 Anthropic 协议时，会返回 Anthropic 兼容格式。

**响应参数**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| object | string | 固定值 "list" |
| data | array | 模型卡片数组 |

**ModelCard 结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 模型标识 |
| object | string | 固定值 "model" |
| created | int | 创建时间戳 |
| owned_by | string | 所属者，固定为 "apiproxy" |
| root | string | 根模型名 |
| permission | array | 权限列表 |

**Anthropic 兼容响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| data | array | 模型卡片数组 |
| first_id | string/null | 首个模型 ID |
| last_id | string/null | 最后一个模型 ID |
| has_more | bool | 是否存在下一页，当前固定为 `false` |

**Anthropic ModelCard 结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| type | string | 固定值 `model` |
| id | string | 模型标识 |
| display_name | string | 展示名称，当前与模型标识相同 |
| created_at | string | 创建时间，占位值为 `1970-01-01T00:00:00Z` |

---

### Chat Completions 接口

- **方法**: `POST`
- **路径**: `/v1/chat/completions`
- **鉴权**: 应用 API Key
- **说明**: 对话补全接口，支持流式和非流式响应。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model | string | 是 | - | 模型名称 |
| messages | array/string | 是 | - | 对话消息列表或字符串 |
| temperature | float | 否 | 0.7 | 温度参数 |
| top_p | float | 否 | 1.0 | Top-p采样参数 |
| tools | array | 否 | None | 工具定义列表 |
| tool_choice | string/object | 否 | None | 工具选择策略 |
| logprobs | bool | 否 | False | 是否返回logprobs |
| top_logprobs | int | 否 | None | 返回top logprobs数量 |
| n | int | 否 | 1 | 生成数量 |
| logit_bias | dict | 否 | None | Logit偏置 |
| max_tokens | int | 否 | None | 最大生成token数 |
| stop | string/array | 否 | [] | 停止词 |
| stream | bool | 否 | False | 是否流式输出 |
| stream_options | object | 否 | None | 流式选项 |
| presence_penalty | float | 否 | 0.0 | 存在惩罚 |
| frequency_penalty | float | 否 | 0.0 | 频率惩罚 |
| user | string | 否 | None | 用户标识 |
| response_format | object | 否 | None | 响应格式 |
| repetition_penalty | float | 否 | 1.0 | 重复惩罚（扩展参数） |
| session_id | int | 否 | -1 | 会话ID（扩展参数） |
| ignore_eos | bool | 否 | False | 忽略EOS（扩展参数） |
| skip_special_tokens | bool | 否 | True | 跳过特殊token（扩展参数） |
| spaces_between_special_tokens | bool | 否 | True | 特殊token之间是否保留空格（扩展参数） |
| top_k | int | 否 | 40 | Top-k采样（扩展参数） |
| seed | int | 否 | None | 随机种子 |
| min_new_tokens | int | 否 | None | 最小新生成token数 |
| min_p | float | 否 | 0.0 | Min-p采样参数 |

**响应参数**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 响应ID |
| object | string | 固定值 "chat.completion" |
| created | int | 创建时间戳 |
| model | string | 模型名称 |
| choices | array | 选择列表 |
| usage | object | 使用量信息 |

---

### Completions 接口

- **方法**: `POST`
- **路径**: `/v1/completions`
- **鉴权**: 应用 API Key
- **说明**: 文本补全接口，支持流式和非流式响应。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model | string | 是 | - | 模型名称 |
| prompt | string/array | 是 | - | 提示文本 |
| suffix | string | 否 | None | 后缀 |
| temperature | float | 否 | 0.7 | 温度参数 |
| n | int | 否 | 1 | 生成数量 |
| logprobs | int | 否 | None | Logprobs数量 |
| max_tokens | int | 否 | 16 | 最大生成token数 |
| stop | string/array | 否 | None | 停止词 |
| stream | bool | 否 | False | 是否流式输出 |
| stream_options | object | 否 | None | 流式选项 |
| top_p | float | 否 | 1.0 | Top-p采样参数 |
| echo | bool | 否 | False | 是否回显 |
| presence_penalty | float | 否 | 0.0 | 存在惩罚 |
| frequency_penalty | float | 否 | 0.0 | 频率惩罚 |
| user | string | 否 | None | 用户标识 |
| repetition_penalty | float | 否 | 1.0 | 重复惩罚（扩展参数） |
| session_id | int | 否 | -1 | 会话ID（扩展参数） |
| ignore_eos | bool | 否 | False | 忽略EOS（扩展参数） |
| skip_special_tokens | bool | 否 | True | 跳过特殊token（扩展参数） |
| spaces_between_special_tokens | bool | 否 | True | 特殊token之间是否保留空格（扩展参数） |
| top_k | int | 否 | 40 | Top-k采样（扩展参数） |
| seed | int | 否 | None | 随机种子 |

**响应参数**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 响应ID |
| object | string | 固定值 "text_completion" |
| created | int | 创建时间戳 |
| model | string | 模型名称 |
| choices | array | 选择列表 |
| usage | object | 使用量信息 |

---

### Embeddings 接口

- **方法**: `POST`
- **路径**: `/v1/embeddings`
- **鉴权**: 应用 API Key
- **说明**: 文本向量化接口。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model | string | 是 | - | 模型名称 |
| input | string/array | 是 | - | 输入文本 |
| user | string | 否 | None | 用户标识 |

**响应参数**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| object | string | 固定值 "list" |
| data | array | 向量数据列表 |
| model | string | 模型名称 |
| usage | object | 使用量信息 |

---

### Rerank 接口

- **方法**: `POST`
- **路径**: `/v1/rerank`
- **鉴权**: 应用 API Key
- **说明**: 文档重排序接口。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model | string | 是 | - | 模型名称 |
| query | string/array | 是 | - | 查询文本 |
| documents | array | 否 | None | 文档列表 |
| user | string | 否 | None | 用户标识 |

---

## Anthropic 兼容接口

说明：Anthropic 兼容接口统一挂载在 `/v1` 前缀下，覆盖 `messages`、`messages/count_tokens` 与 `messages/batches` 相关端点。当前实现会优先选择支持 Anthropic 协议的节点；如果命中 OpenAI-only 节点，则代理层会做最小必要的协议转换。

### Messages 接口

- **方法**: `POST`
- **路径**: `/v1/messages`
- **鉴权**: 应用 API Key，支持 `Authorization: Bearer <token>` 或 `x-api-key: <token>`
- **说明**: Anthropic 兼容消息生成接口，支持流式和非流式响应。若后端节点仅支持 OpenAI 协议，代理层会将 `messages` 请求转换为 `chat/completions`，并将响应再转换回 Anthropic 格式。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model | string | 是 | - | 模型名称 |
| messages | array | 是 | - | Anthropic 消息数组，元素包含 `role` 与 `content` |
| system | string/array | 否 | None | 系统提示，会参与 token 估算与协议转换 |
| max_tokens | int | 否 | 1024 | 最大输出 token 数 |
| stream | bool | 否 | False | 是否使用 SSE 流式响应 |
| temperature | number | 否 | None | 采样温度 |
| top_p | number | 否 | None | Top-p 采样参数 |
| stop_sequences | array | 否 | None | 停止序列 |
| tools | array | 否 | None | Anthropic 工具定义；跨协议时会映射到 OpenAI tools |

**非流式响应参数**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 消息响应 ID |
| type | string | 固定值 `message` |
| role | string | 固定值 `assistant` |
| model | string | 模型名称 |
| content | array | 内容块数组，当前主要返回 `text` 类型 |
| stop_reason | string/null | 停止原因 |
| stop_sequence | string/null | 命中的停止序列 |
| usage | object | 使用量信息，包含 `input_tokens` 与 `output_tokens` |

**流式响应说明**:

- 响应类型为 `text/event-stream`。
- 对原生 Anthropic 节点会透传其 SSE 事件。
- 对 OpenAI-only 节点会将 OpenAI SSE 片段转换为 Anthropic 事件流，包含 `content_block_start`、`content_block_delta` 等事件。

**错误响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| type | string | 固定值 `error` |
| error.type | string | 错误类型，如 `invalid_request_error`、`rate_limit_error`、`api_error` |
| error.message | string | 错误描述 |

### Count Tokens 接口

- **方法**: `POST`
- **路径**: `/v1/messages/count_tokens`
- **鉴权**: 应用 API Key
- **说明**: Anthropic 兼容 token 估算接口。若路由到 Anthropic 节点则原生透传；若路由到 OpenAI-only 节点，则代理层基于请求内容做本地估算。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model | string | 是 | - | 模型名称 |
| messages | array | 是 | - | Anthropic 消息数组 |
| system | string/array | 否 | None | 系统提示 |

**响应参数**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| input_tokens | int | 估算或后端返回的输入 token 数 |

### Message Batches 接口

- **创建批任务**: `POST /v1/messages/batches`
- **查询批列表**: `GET /v1/messages/batches`
- **查询批状态**: `GET /v1/messages/batches/{batch_id}`
- **取消批任务**: `POST /v1/messages/batches/{batch_id}/cancel`
- **查询批结果**: `GET /v1/messages/batches/{batch_id}/results`
- **鉴权**: 应用 API Key

**实现说明**:

- 若目标节点支持 Anthropic 协议，批处理接口会原生透传到下游 Anthropic 节点。
- 若目标节点仅支持 OpenAI 协议，代理层会将批请求拆解为多个非流式 OpenAI 请求，汇总后在本地内存中维护批状态与结果。
- 本地合成批任务返回的 `results_url` 同样位于 `/v1/messages/batches/{batch_id}/results`。

**创建批任务请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| requests | array | 是 | - | 批量请求数组 |
| requests[].custom_id | string | 否 | None | 客户端自定义请求 ID |
| requests[].params | object | 是 | - | 单条 Anthropic `messages` 请求参数，至少需要 `params.model` |

**批状态响应核心字段**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | string | 批任务 ID |
| type | string | 固定值 `message_batch` |
| processing_status | string | 批任务状态，如 `ended`、`canceled` |
| request_counts | object | 各状态请求数量统计 |
| created_at | int | 创建时间戳 |
| ended_at | int | 结束时间戳 |
| results_url | string | 批结果查询路径 |

**批结果响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| data | array | 结果数组 |
| data[].custom_id | string | 客户端自定义请求 ID |
| data[].result.type | string | `succeeded` 或 `errored` |
| data[].result.message | object | 单条 Anthropic 兼容响应 |

---

## 节点管理接口

### 遗留接口（deprecated）

#### 查询节点运行状态

- **方法**: `GET`
- **路径**: `/nodes/status`
- **鉴权**: 管理密钥
- **状态**: 已废弃
- **说明**: 查询节点运行状态。

#### 添加节点

- **方法**: `POST`
- **路径**: `/nodes/add`
- **鉴权**: 管理密钥
- **状态**: 已废弃
- **说明**: 添加节点。

#### 删除节点（遗留）

- **方法**: `POST`
- **路径**: `/nodes/remove`
- **鉴权**: 管理密钥
- **状态**: 已废弃
- **说明**: 删除节点。
- **参数**: `node_url` (string) - 节点URL

---

### 新版节点接口

#### 分页获取节点

- **方法**: `GET`
- **路径**: `/nodes`
- **鉴权**: 管理密钥
- **说明**: 分页获取OpenAI兼容服务节点列表。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| enabled | bool | 否 | None | 是否启用 |
| expired | bool | 否 | None | 是否过期 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

**响应参数** (PageResponse):

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| offset | int | 偏移量 |
| total | int | 总数 |
| data | array | 节点列表 |

**节点响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | 节点ID |
| name | string | 节点名称 |
| url | string | 节点URL |
| api_key | string | API密钥（解密后） |
| description | string | 描述 |
| created_at | datetime | 创建时间 |
| create_user | string | 创建用户 |
| updated_at | datetime | 更新时间 |
| modify_user | string | 修改用户 |
| health_check | bool | 是否健康检查 |
| trusted_without_models_endpoint | bool | 是否允许在节点不提供 `/v1/models` 时仍被永久信任 |
| enabled | bool | 是否启用 |

---

#### 创建节点

- **方法**: `POST`
- **路径**: `/nodes`
- **鉴权**: 管理密钥
- **说明**: 创建OpenAI兼容服务节点。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| id | UUID | 否 | 自动生成 | 节点ID |
| name | string | 否 | None | 节点名称 |
| url | string | 是 | - | 节点URL |
| api_key | string | 否 | None | API密钥 |
| description | string | 否 | None | 描述 |
| create_user | string | 否 | None | 创建用户 |
| modify_user | string | 否 | None | 修改用户 |
| health_check | bool | 否 | None | 是否健康检查 |
| trusted_without_models_endpoint | bool | 否 | False | 是否跳过 `/v1/models` 验证与探活依赖 |
| verify | bool | 否 | True | 是否验证节点可用性 |

**说明**:

- 如果URL已存在，返回已存在的节点
- 当 `verify=true` 时，会验证节点的 `/v1/models` 接口
- 当 `trusted_without_models_endpoint=true` 时，会跳过创建阶段的 `/v1/models` 校验，且运行时健康检查不再依赖该接口
- `trusted_without_models_endpoint` 不会自动发现模型能力，仍需通过节点模型管理接口手工维护模型列表，否则请求路由不会选中该节点

---

#### 通过URL查询节点

- **方法**: `POST`
- **路径**: `/nodes/query`
- **鉴权**: 管理密钥
- **说明**: 通过 URL 查询 OpenAI 兼容服务节点。当前实现将 `url` 作为简单参数接收，因此应按查询参数传递，而不是 JSON Body。

**查询参数**:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| url | string | 否 | 节点URL |

---

#### 查询节点模型

- **方法**: `POST`
- **路径**: `/nodes/models`
- **鉴权**: 管理密钥
- **说明**: 通过节点ID或URL拉取节点模型信息。

**请求参数** (Form):

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| node_id | UUID | 否 | 节点ID |
| url | string | 否 | 节点URL |
| api_key | string | 否 | API密钥 |

**说明**: 至少需要提供 `node_id` 或 `url` 之一。

---

#### 获取节点详情

- **方法**: `GET`
- **路径**: `/nodes/{node_id}`
- **鉴权**: 管理密钥
- **说明**: 获取指定ID的OpenAI兼容服务节点详情。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |

---

#### 更新节点

- **方法**: `POST`
- **路径**: `/nodes/{node_id}`
- **鉴权**: 管理密钥
- **说明**: 更新OpenAI兼容服务节点。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |

**请求参数**:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| name | string | 否 | 节点名称 |
| api_key | string | 否 | API密钥 |
| description | string | 否 | 描述 |
| modify_user | string | 否 | 修改用户 |
| enabled | bool | 否 | 是否启用 |
| trusted_without_models_endpoint | bool | 否 | 是否允许节点在缺少 `/v1/models` 时继续被信任 |
| verify | bool | 否 | 是否验证节点可用性 |

**说明**:

- 更新时若同时传入 `api_key` 与 `trusted_without_models_endpoint=true`，系统会跳过 `/v1/models` 连通性校验

---

#### 删除节点

- **方法**: `DELETE`
- **路径**: `/nodes/{node_id}`
- **鉴权**: 管理密钥
- **说明**: 删除OpenAI兼容服务节点（需先禁用）。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |

**说明**: 只有已禁用的节点才能删除。

---

#### 分页获取节点模型

- **方法**: `GET`
- **路径**: `/nodes/{node_id}/models`
- **鉴权**: 管理密钥
- **说明**: 分页获取节点模型列表。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model_type | string | 否 | None | 模型类型（chat/embedding/rerank） |
| enabled | bool | 否 | None | 是否启用 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

#### 创建节点模型

- **方法**: `POST`
- **路径**: `/nodes/{node_id}/models`
- **鉴权**: 管理密钥
- **说明**: 创建节点模型。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| id | UUID | 否 | 自动生成 | 模型ID |
| model_name | string | 是 | - | 模型名称 |
| model_type | string | 否 | chat | 模型类型 |
| enabled | bool | 否 | True | 是否启用 |

---

#### 通过节点与名称查询节点模型

- **方法**: `POST`
- **路径**: `/nodes/{node_id}/models/query`
- **鉴权**: 管理密钥
- **说明**: 通过节点与名称查询节点模型。当前实现将 `model_name` 和 `model_type` 作为简单参数接收，因此应按查询参数传递，而不是 JSON Body。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |

**查询参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| model_name | string | 是 | - | 模型名称 |
| model_type | string | 否 | chat | 模型类型 |

---

#### 获取节点模型详情

- **方法**: `GET`
- **路径**: `/nodes/{node_id}/models/{model_id}`
- **鉴权**: 管理密钥
- **说明**: 获取指定ID的节点模型详情。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |
| model_id | UUID | 模型ID |

---

#### 更新节点模型

- **方法**: `POST`
- **路径**: `/nodes/{node_id}/models/{model_id}`
- **鉴权**: 管理密钥
- **说明**: 更新节点模型。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |
| model_id | UUID | 模型ID |

**请求参数**:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| enabled | bool | 否 | 是否启用 |

---

#### 删除节点模型

- **方法**: `DELETE`
- **路径**: `/nodes/{node_id}/models/{model_id}`
- **鉴权**: 管理密钥
- **说明**: 删除节点模型（需先禁用）。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| node_id | UUID | 节点ID |
| model_id | UUID | 模型ID |

**说明**: 只有已禁用的节点模型才能删除。

---

## 节点模型配额接口

### 分页获取节点模型配额

- **方法**: `GET`
- **路径**: `/quotas`
- **鉴权**: 管理密钥
- **说明**: 分页获取节点模型配额列表。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| node_id | UUID | 否 | None | 节点ID |
| node_model_id | UUID | 否 | None | 节点模型ID |
| order_id | string | 否 | None | 订单ID |
| expired | bool | 否 | None | 是否过期 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

**配额响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | 配额ID |
| node_model_id | UUID | 节点模型ID |
| order_id | string | 订单ID |
| call_limit | int | 调用次数限制 |
| call_used | int | 已使用调用次数 |
| prompt_tokens_limit | int | Prompt Token限制 |
| prompt_tokens_used | int | 已使用Prompt Token |
| completion_tokens_limit | int | Completion Token限制 |
| completion_tokens_used | int | 已使用Completion Token |
| total_tokens_limit | int | 总Token限制 |
| total_tokens_used | int | 已使用总Token |
| last_reset_at | datetime | 上次重置时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |
| expired_at | datetime | 过期时间 |

---

### 创建节点模型配额

- **方法**: `POST`
- **路径**: `/quotas`
- **鉴权**: 管理密钥
- **说明**: 创建节点模型配额。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| node_model_id | UUID | 是 | - | 节点模型ID |
| order_id | string | 否 | None | 订单ID |
| call_limit | int | 否 | None | 调用次数限制 |
| call_used | int | 否 | 0 | 已使用调用次数 |
| prompt_tokens_limit | int | 否 | None | Prompt Token限制 |
| prompt_tokens_used | int | 否 | 0 | 已使用Prompt Token |
| completion_tokens_limit | int | 否 | None | Completion Token限制 |
| completion_tokens_used | int | 否 | 0 | 已使用Completion Token |
| total_tokens_limit | int | 否 | None | 总Token限制 |
| total_tokens_used | int | 否 | 0 | 已使用总Token |
| last_reset_at | datetime | 否 | None | 上次重置时间 |
| expired_at | datetime | 否 | None | 过期时间 |

---

### 查询节点模型配额使用记录

- **方法**: `GET`
- **路径**: `/quotas/usages`
- **鉴权**: 管理密钥
- **说明**: 查询节点模型配额使用记录。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| quota_id | UUID | 否 | None | 配额ID |
| node_id | UUID | 否 | None | 节点ID |
| node_model_id | UUID | 否 | None | 节点模型ID |
| ownerapp_id | string | 否 | None | 应用ID |
| request_action | string | 否 | None | 请求动作 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 获取配额详情

- **方法**: `GET`
- **路径**: `/quotas/{quota_id}`
- **鉴权**: 管理密钥
- **说明**: 获取节点模型配额详情。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

---

### 更新配额

- **方法**: `POST`
- **路径**: `/quotas/{quota_id}`
- **鉴权**: 管理密钥
- **说明**: 更新节点模型配额。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

**请求参数**: 参见创建节点模型配额，所有字段均为可选。

---

### 删除配额

- **方法**: `DELETE`
- **路径**: `/quotas/{quota_id}`
- **鉴权**: 管理密钥
- **说明**: 软删除节点模型配额（设置过期时间）。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

---

## API密钥配额接口

### 分页获取API密钥配额

- **方法**: `GET`
- **路径**: `/apikey-quotas`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 分页获取API密钥配额列表。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| api_key_id | UUID | 否 | None | API密钥ID |
| order_id | string | 否 | None | 订单ID |
| expired | bool | 否 | None | 是否过期 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

**配额响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | 配额ID |
| api_key_id | UUID | API密钥ID |
| order_id | string | 订单ID |
| call_limit | int | 调用次数限制 |
| call_used | int | 已使用调用次数 |
| total_tokens_limit | int | 总Token限制 |
| total_tokens_used | int | 已使用总Token |
| last_reset_at | datetime | 上次重置时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |
| expired_at | datetime | 过期时间 |

---

### 创建API密钥配额

- **方法**: `POST`
- **路径**: `/apikey-quotas`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 创建API密钥配额。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| api_key_id | UUID | 是 | - | API密钥ID |
| order_id | string | 否 | None | 订单ID |
| call_limit | int | 否 | None | 调用次数限制 |
| call_used | int | 否 | 0 | 已使用调用次数 |
| total_tokens_limit | int | 否 | None | 总Token限制 |
| total_tokens_used | int | 否 | 0 | 已使用总Token |
| last_reset_at | datetime | 否 | None | 上次重置时间 |
| expired_at | datetime | 否 | None | 过期时间 |

---

### 查询API密钥配额使用记录

- **方法**: `GET`
- **路径**: `/apikey-quotas/usages`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 查询API密钥配额使用记录。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| quota_id | UUID | 否 | None | 配额ID |
| api_key_id | UUID | 否 | None | API密钥ID |
| ownerapp_id | string | 否 | None | 应用ID |
| request_action | string | 否 | None | 请求动作 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 获取API密钥配额详情

- **方法**: `GET`
- **路径**: `/apikey-quotas/{quota_id}`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 获取API密钥配额详情。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

---

### 更新API密钥配额

- **方法**: `POST`
- **路径**: `/apikey-quotas/{quota_id}`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 更新API密钥配额。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

**请求参数**: 参见创建API密钥配额，所有字段均为可选。

---

### 删除API密钥配额

- **方法**: `DELETE`
- **路径**: `/apikey-quotas/{quota_id}`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 软删除API密钥配额（设置过期时间）。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

---

## 应用配额接口

### 分页获取应用配额

- **方法**: `GET`
- **路径**: `/app-quotas`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 分页获取应用配额列表。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| order_id | string | 否 | None | 订单ID |
| expired | bool | 否 | None | 是否过期 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

**配额响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | 配额ID |
| ownerapp_id | string | 应用ID |
| order_id | string | 订单ID |
| call_limit | int | 调用次数限制 |
| call_used | int | 已使用调用次数 |
| total_tokens_limit | int | 总Token限制 |
| total_tokens_used | int | 已使用总Token |
| last_reset_at | datetime | 上次重置时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |
| expired_at | datetime | 过期时间 |

---

### 创建应用配额

- **方法**: `POST`
- **路径**: `/app-quotas`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 创建应用配额。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 是 | - | 应用ID |
| order_id | string | 否 | None | 订单ID |
| call_limit | int | 否 | None | 调用次数限制 |
| call_used | int | 否 | 0 | 已使用调用次数 |
| total_tokens_limit | int | 否 | None | 总Token限制 |
| total_tokens_used | int | 否 | 0 | 已使用总Token |
| last_reset_at | datetime | 否 | None | 上次重置时间 |
| expired_at | datetime | 否 | None | 过期时间 |

---

### 查询应用配额使用记录

- **方法**: `GET`
- **路径**: `/app-quotas/usages`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 查询应用配额使用记录。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| quota_id | UUID | 否 | None | 配额ID |
| ownerapp_id | string | 否 | None | 应用ID |
| api_key_id | UUID | 否 | None | API密钥ID |
| request_action | string | 否 | None | 请求动作 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 获取应用配额详情

- **方法**: `GET`
- **路径**: `/app-quotas/{quota_id}`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 获取应用配额详情。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

---

### 更新应用配额

- **方法**: `POST`
- **路径**: `/app-quotas/{quota_id}`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 更新应用配额。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

**请求参数**: 参见创建应用配额，所有字段均为可选。

---

### 删除应用配额

- **方法**: `DELETE`
- **路径**: `/app-quotas/{quota_id}`
- **鉴权**: 管理密钥（严格模式）
- **说明**: 软删除应用配额（设置过期时间）。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| quota_id | UUID | 配额ID |

---

## 模型请求日志接口

### 分页查询模型服务接口请求日志

- **方法**: `GET`
- **路径**: `/request-logs`
- **鉴权**: 管理密钥
- **说明**: 分页查询模型服务接口请求记录。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| log_id | UUID | 否 | None | 日志ID |
| node_id | UUID | 否 | None | 节点ID |
| proxy_id | UUID | 否 | None | 代理ID |
| status_id | UUID | 否 | None | 状态ID |
| ownerapp_id | string | 否 | None | 应用ID |
| action | string | 否 | "completions,embeddings,rerankdocs" | 请求动作（逗号分隔） |
| model_name | string | 否 | None | 模型名称 |
| error | bool | 否 | None | 是否有错误 |
| abort | bool | 否 | None | 是否中止 |
| stream | bool | 否 | None | 是否流式 |
| processing | bool | 否 | None | 是否处理中 |
| start_time | datetime | 否 | None | 开始时间 |
| end_time | datetime | 否 | None | 结束时间 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

**日志响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | 日志ID |
| node_id | UUID | 节点ID |
| proxy_id | UUID | 代理ID |
| status_id | UUID | 状态ID |
| ownerapp_id | string | 应用ID |
| action | string | 请求动作 |
| model_name | string | 模型名称 |
| start_at | datetime | 开始时间 |
| end_at | datetime | 结束时间 |
| first_response_at | datetime | 首次响应时间 |
| latency | float | 延迟（秒） |
| stream | bool | 是否流式 |
| request_data | string | 请求数据 |
| request_tokens | int | 请求Token数 |
| response_data | string | 响应数据 |
| response_tokens | int | 响应Token数 |
| total_tokens | int | 总Token数 |
| error | bool | 是否有错误 |
| abort | bool | 是否中止 |
| error_message | string | 错误信息 |
| error_stack | string | 错误堆栈 |
| process_id | string | 进程ID |
| client_ip | string | 客户端IP |

---

### 按应用按天查询模型用量

- **方法**: `GET`
- **路径**: `/request-logs/daily-usage`
- **鉴权**: 管理密钥
- **说明**: 分页查询应用日度模型用量。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| day | string | 否 | None | 日期（YYYY-MM-DD） |
| models | string | 否 | None | 模型列表（逗号分隔） |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 按应用按周查询模型用量

- **方法**: `GET`
- **路径**: `/request-logs/weekly-usage`
- **鉴权**: 管理密钥
- **说明**: 分页查询应用周度模型用量。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| week_start | string | 否 | None | 周开始日期（YYYY-MM-DD，必须为周一） |
| models | string | 否 | None | 模型列表（逗号分隔） |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 按应用按月查询模型用量

- **方法**: `GET`
- **路径**: `/request-logs/monthly-usage`
- **鉴权**: 管理密钥
- **说明**: 分页查询应用月度模型用量。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| month | string | 否 | None | 月份（YYYY-MM） |
| models | string | 否 | None | 模型列表（逗号分隔） |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 按应用按月查询模型用量总计

- **方法**: `GET`
- **路径**: `/request-logs/monthly-usage-total`
- **鉴权**: 管理密钥
- **说明**: 分页查询应用月度模型用量总计（不分模型）。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| month | string | 否 | 当前月 | 月份（YYYY-MM） |
| models | string | 否 | None | 模型列表（逗号分隔） |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 按应用按年查询模型用量

- **方法**: `GET`
- **路径**: `/request-logs/yearly-usage`
- **鉴权**: 管理密钥
- **说明**: 分页查询应用年度模型用量。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| year | string | 否 | 当前年 | 年份（YYYY） |
| models | string | 否 | None | 模型列表（逗号分隔） |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

### 按应用按年查询模型用量总计

- **方法**: `GET`
- **路径**: `/request-logs/yearly-usage-total`
- **鉴权**: 管理密钥
- **说明**: 分页查询应用年度模型用量总计（不分模型）。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 否 | None | 应用ID |
| year | string | 否 | 当前年 | 年份（YYYY） |
| models | string | 否 | None | 模型列表（逗号分隔） |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

---

## 应用 API Key 管理接口

### 分页获取 API Key 列表

- **方法**: `GET`
- **路径**: `/apikeys`
- **鉴权**: 管理密钥
- **说明**: 分页获取 API Key 列表。

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| name | string | 否 | None | 名称 |
| ownerapp_id | string | 否 | None | 应用ID |
| enabled | bool | 否 | None | 是否启用 |
| expired | bool | 否 | None | 是否过期 |
| orderby | string | 否 | None | 排序字段 |
| offset | int | 否 | 0 | 偏移量 |
| limit | int | 否 | 20 | 每页数量 |

**API Key 响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | API Key ID |
| name | string | 名称 |
| enabled | bool | 是否启用 |
| ownerapp_id | string | 应用ID |
| description | string | 描述 |
| created_at | datetime | 创建时间 |
| expires_at | datetime | 过期时间 |

---

### 创建 API Key

- **方法**: `POST`
- **路径**: `/apikeys`
- **鉴权**: 管理密钥
- **说明**: 创建 API Key。

**请求参数**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| ownerapp_id | string | 是 | - | 应用ID |
| name | string | 是 | - | 名称 |
| description | string | 否 | None | 描述 |
| expires_at | datetime | 否 | None | 过期时间 |

**创建响应**:

除 API Key 响应结构字段外，还包括:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| token | string | API密钥令牌（仅创建时返回一次） |

**说明**: token 仅在创建时返回一次，请妥善保存。

---

### 获取 API Key 详情

- **方法**: `GET`
- **路径**: `/apikeys/{api_key_id}`
- **鉴权**: 管理密钥
- **说明**: 获取指定 ID 的 API Key 详情。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| api_key_id | UUID | API Key ID |

---

### 更新 API Key

- **方法**: `POST`
- **路径**: `/apikeys/{api_key_id}`
- **鉴权**: 管理密钥
- **说明**: 更新 API Key。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| api_key_id | UUID | API Key ID |

**请求参数**:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| name | string | 否 | 名称 |
| description | string | 否 | 描述 |
| expires_at | datetime | 否 | 过期时间 |
| enabled | bool | 否 | 是否启用 |

---

### 删除 API Key

- **方法**: `DELETE`
- **路径**: `/apikeys/{api_key_id}`
- **鉴权**: 管理密钥
- **说明**: 删除 API Key（需先禁用）。

**路径参数**:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| api_key_id | UUID | API Key ID |

**说明**: 只有已禁用的 API Key 才能删除。

---

## 健康检查与文档接口

### 基础健康检查

- **方法**: `GET`
- **路径**: `/health`
- **鉴权**: 无
- **说明**: 基础健康检查（兼容uvicorn）。

**响应**:

```json
{"status": "ok"}
```

**注意**: 此接口由uvicorn提供，不保证应用实例完全启动。

---

### 可靠健康检查

- **方法**: `GET`
- **路径**: `/health_check`
- **鉴权**: 无
- **说明**: 可靠健康检查，评估关键服务状态。

**响应结构**:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| status | string | 整体状态（"ok" 或 "nok"） |
| db | string | 数据库状态 |

**说明**:

- 检查数据库连接状态
- 如果任何服务异常，返回 HTTP 500

---

### Swagger UI

- **方法**: `GET`
- **路径**: `/docs`
- **鉴权**: 无
- **说明**: Swagger UI 文档界面。

---

### ReDoc

- **方法**: `GET`
- **路径**: `/redoc`
- **鉴权**: 无
- **说明**: ReDoc 文档界面。

---

## 通用响应结构

### 分页响应 (PageResponse)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| offset | int | 偏移量 |
| total | int | 总数 |
| data | array | 数据列表 |

### 使用量信息 (UsageInfo)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| prompt_tokens | int | Prompt Token数 |
| completion_tokens | int | Completion Token数 |
| total_tokens | int | 总Token数 |

### 错误响应

当请求出错时，返回如下格式的错误响应：

```json
{
  "detail": "错误信息描述"
}
```

常见HTTP状态码：

- `400 Bad Request`: 请求参数错误
- `404 Not Found`: 资源不存在
- `409 Conflict`: 资源冲突
- `422 Unprocessable Entity`: 参数验证失败
- `500 Internal Server Error`: 服务器内部错误
- `502 Bad Gateway`: 网关错误
