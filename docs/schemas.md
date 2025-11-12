# 数据结构

> 帮助快速理解各张表的字段、约束与关联关系。

## 接口访问密钥

### 表 `openaiapi_apikeys`

> 用于保存应用访问接口的密钥。

| 字段 | 类型 | 约束 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | UUID | PK, NOT NULL | `uuid4()` | API Key 主键 |
| `name` | TEXT | NOT NULL, INDEX | - | API Key 名称 |
| `description` | TEXT | NULL | `None` | API Key 描述 |
| `key` | TEXT | NOT NULL, INDEX | - | 实际的密钥字符串 |
| `ownerapp_id` | VARCHAR(40) | NOT NULL, INDEX | - | 绑定的应用 ID |
| `created_at` | TIMESTAMP WITH TIME ZONE | NOT NULL | `current_timezone()` | 创建时间 |
| `enabled` | BOOLEAN | INDEX | `True` | 是否启用 |
| `expires_at` | TIMESTAMP WITH TIME ZONE | NULL | `None` | 密钥过期时间 |

**唯一约束**：`ownerapp_id + key` (`uix_openaiapi_apikeys_key`)

## 接口节点管理

### 表 `openaiapi_nodes`

> 用于记录大模型接口服务节点信息。

| 字段 | 类型 | 约束 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | UUID | PK, NOT NULL | `uuid4()` | 节点 ID |
| `url` | TEXT | NOT NULL, UNIQUE, INDEX | - | 节点地址 |
| `name` | TEXT | INDEX | `None` | 节点名称 |
| `description` | TEXT | NULL | `None` | 节点描述 |
| `api_key` | TEXT | NULL | `None` | 节点级访问密钥 |
| `health_check` | BOOLEAN | NOT NULL, INDEX | `True` | 是否启用健康检查 |
| `created_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 创建时间 |
| `create_user` | TEXT | NULL | `None` | 创建人 |
| `updated_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 最后修改时间 |
| `modify_user` | TEXT | NULL | `None` | 最后修改人 |
| `enabled` | BOOLEAN | INDEX | `True` | 是否启用 |

### 表 `openaiapi_models`

> 用于记录各节点支持的大模型信息。

| 字段 | 类型 | 约束 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | UUID | PK, NOT NULL | `uuid4()` | 映射 ID |
| `node_id` | UUID | NOT NULL, INDEX, FK → `openaiapi_nodes.id` | - | 关联节点 |
| `model_name` | TEXT | NOT NULL, INDEX | - | 模型名称 |
| `model_type` | ENUM(`chat`,`embeddings`,`rerank`) | NOT NULL, INDEX | `chat` | 模型类型 |
| `enabled` | BOOLEAN | INDEX | `True` | 是否启用 |

**唯一约束**：`node_id + model_name + model_type` (`uix_openaiapi_node_models_type`)

## 代理运行实例

### 表 `openaiapi_proxy`

> 用于记录代理服务的运行实例及其状态，只用于显示。

| 字段 | 类型 | 约束 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | UUID | PK, NOT NULL | `uuid4()` | 代理实例 ID |
| `instance_name` | TEXT | NOT NULL, INDEX | - | 代理实例名称 |
| `instance_ip` | TEXT | NOT NULL, INDEX | - | 实例 IP |
| `created_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 创建时间 |
| `updated_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 更新时间 |
| `process_id` | VARCHAR | INDEX | `pg_backend_pid()` (server default) | 处理进程 ID |

### 表 `openaiapi_status`

> 用于跟踪各节点在不同代理实例上的运行状态。

| 字段 | 类型 | 约束 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | UUID | PK, NOT NULL | `uuid4()` | 状态 ID |
| `node_id` | UUID | NOT NULL, INDEX, FK → `openaiapi_nodes.id` | - | 节点 ID |
| `proxy_id` | UUID | INDEX, FK → `openaiapi_proxy.id` | `None` | 代理实例 ID |
| `unfinished` | INTEGER | NOT NULL | `0` | 未完成请求数 |
| `latency` | FLOAT | NOT NULL | `0.0` | 最后耗时(秒) |
| `speed` | FLOAT | NOT NULL | `-1` | 处理速度(次/秒) |
| `avaiaible` | BOOLEAN | NOT NULL, INDEX | `True` | 节点可用状态 |
| `created_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 创建时间 |
| `updated_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 更新时间 |

**唯一约束**：`node_id + proxy_id` (`uix_openaiapi_status_node_proxy`)

### 表 `openaiapi_nodelogs`

> 用于记录节点模型接口请求日志。

| 字段 | 类型 | 约束 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | UUID | PK, NOT NULL | `uuid4()` | 日志 ID |
| `node_id` | UUID | NOT NULL, INDEX, FK → `openaiapi_nodes.id` | - | 节点 ID |
| `proxy_id` | UUID | NOT NULL, INDEX | - | 代理实例 ID |
| `status_id` | UUID | NOT NULL, INDEX | - | 状态记录 ID |
| `ownerapp_id` | TEXT | INDEX | `None` | 所属应用 ID |
| `action` | ENUM(`completions`,`embeddings`,`healthcheck`,`rerankdocs`) | NOT NULL, INDEX | `completions` | 请求类型 |
| `model_name` | TEXT | INDEX | `None` | 模型名称 |
| `start_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 请求开始时间 |
| `end_at` | TIMESTAMP WITH TIME ZONE | NULL | `datetime.now(current_timezone())` | 请求结束时间 |
| `latency` | FLOAT | NOT NULL | `0.0` | 延迟(秒) |
| `stream` | BOOLEAN | NOT NULL, INDEX | `False` | 是否为流式请求 |
| `request_tokens` | INTEGER | NOT NULL | `0` | 请求 Token 数 |
| `response_tokens` | INTEGER | NOT NULL | `0` | 响应 Token 数 |
| `error` | BOOLEAN | NOT NULL, INDEX | `False` | 是否发生错误 |
| `error_message` | TEXT | NULL | `None` | 错误信息 |
| `error_stack` | TEXT | NULL | `None` | 错误堆栈 |
| `request_data` | TEXT | NULL | `None` | 请求数据 |
| `response_data` | TEXT | NULL | `None` | 响应数据 |

## 表之间的关系概览

- `openaiapi_models.node_id` → `openaiapi_nodes.id`：节点与模型一对多。
- `openaiapi_status.node_id` → `openaiapi_nodes.id`，`openaiapi_status.proxy_id` → `openaiapi_proxy.id`：跟踪节点在各代理实例上的运行状态。
- `openaiapi_nodelogs.node_id` → `openaiapi_nodes.id`：记录节点层请求日志；`status_id` 与 `openaiapi_status.id` 对应；`proxy_id` 与 `openaiapi_proxy.id` 保持一致（代码未显式约束）。
- `openaiapi_apikeys.ownerapp_id` 可与日志中的 `ownerapp_id` 进行业务关联，用于追踪调用来源。

如需新增字段或表，请同步更新此文档以保持一致性。

