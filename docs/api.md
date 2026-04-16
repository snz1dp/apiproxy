# API 接口文档

## 鉴权说明

- 管理接口（节点、配额、请求日志、API Key）：使用管理密钥鉴权（check_api_key）。
- OpenAI 兼容接口（/v1/*）：使用应用 API Key 鉴权（check_access_key）。

## OpenAI 兼容接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /v1/models | 获取可用模型列表 |
| POST | /v1/chat/completions | Chat Completions 接口 |
| POST | /v1/completions | Completions 接口 |
| POST | /v1/embeddings | Embeddings 接口 |
| POST | /v1/rerank | Rerank 接口 |

## 节点管理接口

### 遗留接口（deprecated）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /nodes/status | 查询节点运行状态（遗留） |
| POST | /nodes/add | 添加节点（遗留） |
| POST | /nodes/remove | 删除节点（遗留） |

### 新版节点接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /nodes | 分页获取节点 |
| POST | /nodes | 创建节点 |
| POST | /nodes/query | 按 URL 查询节点 |
| POST | /nodes/models | 通过节点 ID/URL 拉取节点模型信息 |
| GET | /nodes/{node_id} | 获取节点详情 |
| POST | /nodes/{node_id} | 更新节点 |
| DELETE | /nodes/{node_id} | 删除节点 |
| GET | /nodes/{node_id}/models | 分页获取节点模型 |
| POST | /nodes/{node_id}/models | 创建节点模型 |
| POST | /nodes/{node_id}/models/query | 通过节点与模型名查询节点模型 |
| GET | /nodes/{node_id}/models/{model_id} | 获取节点模型详情 |
| POST | /nodes/{node_id}/models/{model_id} | 更新节点模型 |
| DELETE | /nodes/{node_id}/models/{model_id} | 删除节点模型 |

## 节点模型配额接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /quotas | 分页获取节点模型配额 |
| POST | /quotas | 创建节点模型配额 |
| GET | /quotas/usages | 查询节点模型配额使用记录 |
| GET | /quotas/{quota_id} | 获取配额详情 |
| POST | /quotas/{quota_id} | 更新配额 |
| DELETE | /quotas/{quota_id} | 删除配额（软删除） |

## 模型请求日志接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /request-logs | 分页查询模型服务接口请求日志 |
| GET | /request-logs/daily-usage | 按应用按天查询模型用量 |
| GET | /request-logs/weekly-usage | 按应用按周查询模型用量 |
| GET | /request-logs/monthly-usage | 按应用按月查询模型用量 |
| GET | /request-logs/monthly-usage-total | 按应用按月查询模型用量总计 |
| GET | /request-logs/yearly-usage | 按应用按年查询模型用量 |
| GET | /request-logs/yearly-usage-total | 按应用按年查询模型用量总计 |

支持过滤参数（query）：

- log_id
- node_id
- proxy_id
- status_id
- ownerapp_id
- action
- model_name
- error
- abort
- stream
- processing
- orderby
- offset
- limit

用量统计接口补充参数：

- daily-usage: day（YYYY-MM-DD）
- weekly-usage: week_start（YYYY-MM-DD，且必须为周一）
- monthly-usage / monthly-usage-total: month（YYYY-MM）
- yearly-usage / yearly-usage-total: year（YYYY）
- models（逗号分隔）

## 应用 API Key 管理接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /apikeys | 分页获取 API Key 列表 |
| POST | /apikeys | 创建 API Key |
| GET | /apikeys/{api_key_id} | 获取 API Key 详情 |
| POST | /apikeys/{api_key_id} | 更新 API Key |
| DELETE | /apikeys/{api_key_id} | 删除 API Key |

## 健康检查与文档接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /health | 基础健康检查（兼容） |
| GET | /health_check | 可靠健康检查 |
| GET | /docs | Swagger UI |
| GET | /redoc | ReDoc |
