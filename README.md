# 太乙智启接口代理

这是一个OpenAI兼容的服务接口代理，旨在提供一个统一的接口来访问不同的AI模型和服务。

## 环境变量

- TZ=Asia/Shanghai 默认时区
- APIPROXY_PORT=11434 服务端口
- APIPROXY_STRATEGY=min_expected_latency 代理策略
- APIPROXY_APIKEY=snz1dp9527 API密钥

## 部署方式

添加独立部署配置：

```bash
snz1dpctl profile add postgres
snz1dpctl profile add taiyiflow-apiproxy@1.1.0 \
  --env APIPROXY_APIKEY=snz1dp9527 \
  --overlay
```

然后启动代理服务：

```shell
snz1dpctl alone start taiyiflow-apiproxy
```

> 查看代理的模型列表（需要提供API密钥）

```bash
curl -X GET http://localhost:11434/v1/models -H "Authorization: Bearer snz1dp9527"
```
