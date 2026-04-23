# 太乙智启大模型服务接口代理

[长沙慧码至一](https://snz1.cn)太乙智启大模型服务接口代理提供统一的北向接入层，用来代理多种后端模型节点，并在代理层完成鉴权、路由选择、协议转换、配额控制、请求日志、健康检查和用量汇总。

当前项目同时支持 OpenAI 兼容接口和 Anthropic 兼容接口，并且通过统一的 `/v1` 前缀对外暴露。

## 核心能力

- OpenAI 兼容接口：`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/rerank`、`/v1/models`
- Anthropic 兼容接口：`/v1/messages`、`/v1/messages/count_tokens`、`/v1/messages/batches` 及其查询、取消、结果子接口
- 协议感知模型列表：`GET /v1/models` 会根据请求协议返回 OpenAI 或 Anthropic 兼容格式
- 多节点代理与调度：支持最低期望延迟、轮询等节点选择策略
- 三层配额控制：应用、API Key、节点模型配额
- 请求日志与用量汇总：记录开始时间、首次响应时间、结束时间、协议类型与 token 使用量
- 节点管理与健康检查：支持节点配置、模型映射、运行状态与定时清理任务

## 目录结构

- [src/apiproxy](src/apiproxy)：主 Python 项目目录
- [src/apiproxy/openaiproxy/api](src/apiproxy/openaiproxy/api)：API 路由与请求处理
- [src/apiproxy/openaiproxy/services](src/apiproxy/openaiproxy/services)：服务层与节点代理核心逻辑
- [src/apiproxy/tests](src/apiproxy/tests)：API、数据库、服务与协议适配测试
- [docs/api/api.md](docs/api/api.md)：当前对外接口清单与参数说明
- [docs/wiki/content/API参考文档](docs/wiki/content/API参考文档)：按主题拆分的 wiki 文档

## 运行环境

- Python：3.12
- Web 框架：FastAPI
- ORM：SQLModel / SQLAlchemy
- 依赖管理：uv
- 推荐环境：`conda activate apiproxy`

## 关键环境变量

- `TZ`：默认时区，推荐设置为 `Asia/Shanghai`
- `APIPROXY_HOST`：监听地址，默认 `0.0.0.0`
- `APIPROXY_PORT`：服务端口，代码默认值为 `8008`
- `APIPROXY_WORKERS`：Uvicorn workers 数量
- `APIPROXY_LOG_LEVEL`：日志级别
- `APIPROXY_STRATEGY`：节点调度策略
- `APIPROXY_APIKEYS`：管理密钥与静态访问密钥，多个值用英文逗号分隔
- `APIPROXY_DATABASE_URL`：数据库连接串；未配置时会按 settings 逻辑生成本地 SQLite 路径

说明：

- 管理接口依赖 `APIPROXY_APIKEYS`
- `/v1/*` 推理接口复用应用 API Key 鉴权，同时兼容静态密钥
- Anthropic 风格调用支持 `x-api-key`

## 快速启动

### 1. 安装依赖

```bash
conda activate apiproxy
cd src/apiproxy
uv sync --frozen
```

### 2. 启动服务

推荐直接使用 Uvicorn factory 启动：

```bash
conda activate apiproxy
cd src/apiproxy
export TZ=Asia/Shanghai
export APIPROXY_APIKEYS=changeme
uvicorn --host 0.0.0.0 --port 8008 --factory openaiproxy.main:setup_app
```

也可以使用项目自带脚本：

```bash
conda activate apiproxy
cd /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy
bash scripts/start.sh
```

## 调用示例

### OpenAI 兼容模型列表

```bash
curl -X GET http://localhost:8008/v1/models \
  -H "Authorization: Bearer changeme"
```

### OpenAI 兼容聊天生成

```bash
curl -X POST http://localhost:8008/v1/chat/completions \
  -H "Authorization: Bearer changeme" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "你好，介绍一下你自己"}
    ]
  }'
```

### Anthropic 兼容消息生成

```bash
curl -X POST http://localhost:8008/v1/messages \
  -H "x-api-key: changeme" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet",
    "max_tokens": 256,
    "messages": [
      {"role": "user", "content": [{"type": "text", "text": "你好，介绍一下你自己"}]}
    ]
  }'
```

### Anthropic 兼容 token 估算

```bash
curl -X POST http://localhost:8008/v1/messages/count_tokens \
  -H "x-api-key: changeme" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet",
    "messages": [
      {"role": "user", "content": [{"type": "text", "text": "帮我估算 token"}]}
    ]
  }'
```

## 管理接口概览

- `/nodes`：节点管理
- `/node-model-quotas`：节点模型配额管理
- `/apikey-quotas`：API Key 配额管理
- `/app-quotas`：应用配额管理
- `/request-logs`：请求日志与用量查询
- `/apikeys`：应用 API Key 管理
- `/health`、`/health_check`：健康检查

管理接口统一使用 `Authorization: Bearer <APIPROXY_APIKEYS>` 方式访问。

## 测试

在本项目里，pytest 配置位于 [src/apiproxy/pyproject.toml](src/apiproxy/pyproject.toml)，测试应从 `src/apiproxy` 目录执行。

```bash
conda activate apiproxy
cd src/apiproxy
pytest -q
```

如果只跑 NodeProxyService 定向测试：

```bash
conda activate apiproxy
cd src/apiproxy
pytest tests/services/test_nodeproxy_service.py -q
```

## 文档导航

- [docs/api/api.md](docs/api/api.md)：接口总览与参数说明
- [docs/wiki/content/API参考文档/API参考文档.md](docs/wiki/content/API参考文档/API参考文档.md)：API 参考总览
- [docs/wiki/content/API参考文档/OpenAI兼容API/OpenAI兼容API.md](docs/wiki/content/API参考文档/OpenAI兼容API/OpenAI兼容API.md)：OpenAI 兼容接口说明
- [docs/wiki/content/API参考文档/OpenAI兼容API/Anthropic兼容API.md](docs/wiki/content/API参考文档/OpenAI兼容API/Anthropic兼容API.md)：Anthropic 兼容接口说明
- [docs/wiki/content/API参考文档/节点管理API.md](docs/wiki/content/API参考文档/节点管理API.md)：节点管理说明
- [docs/wiki/content/API参考文档/配额管理API.md](docs/wiki/content/API参考文档/配额管理API.md)：配额管理说明

## 部署说明

如果仍沿用内部部署体系，可以继续使用已有的 `snz1dpctl` 配置方式。README 这里保留最小结论：

- 先准备数据库与 `APIPROXY_APIKEYS`
- 再启动代理服务镜像或 Uvicorn 进程
- 启动后优先验证 `GET /health_check` 和 `GET /v1/models`

更细的部署、迁移、监控、排障说明请看 [docs/wiki/content/部署指南.md](docs/wiki/content/部署指南.md) 和 [docs/wiki/content/故障排除.md](docs/wiki/content/故障排除.md)
