# 太乙智启大模型服务接口代理

## 系统简介

太乙智启OpenAI兼容的大模型服务接口代理，旨在提供一个统一的服务接口来访问不同的AI模型和服务。

> 目前支持以下几类接口：

- /v1/completions 文本生成接口
- /v1/chat/completions 聊天生成接口
- /v1/embeddings 向量生成接口
- /v1/rerank 重排序接口
- /v1/models 模型列表接口
- /nodes 节点管理接口
- /quotas 节点模型配额管理接口
- /request-logs 模型请求日志查询接口
- /apikeys 应用 API Key 管理接口

完整接口清单见：docs/api.md

针对不同的模型提供灵活的代理策略，包括最低延迟优先、轮询等，同时记录详细的请求日志和统计信息，方便用户进行分析和优化。

为每一次模型请求记录详细的时间戳，包括请求开始时间、首次响应时间和结束时间，帮助用户了解模型的响应性能。

## 环境变量

- `TZ=Asia/Shanghai` 默认时区
- `APIPROXY_PORT=11434` 服务端口
- `APIPROXY_STRATEGY=min_expected_latency` 代理策略
- `APIPROXY_APIKEY=changeme` API密钥
- `APIPROXY_DATABASE_URL=postgres://user:password@host:port/dbname` 数据库连接URL

## 部署方式

添加独立部署配置：

```bash
snz1dpctl profile add postgres@14.10
snz1dpctl profile add taiyiflow-apiproxy@1.1.0 \
  --env APIPROXY_APIKEY=changeme \
  --overlay
```

然后启动代理服务：

```shell
snz1dpctl alone start postgres taiyiflow-apiproxy
```

> 查看代理的模型列表（需要提供API密钥）

```bash
curl -X GET http://localhost:11434/v1/models -H "Authorization: Bearer changeme"
```
