# 太乙智启接口代理

这是一个OpenAI兼容的API代理，旨在提供一个统一的接口来访问不同的AI模型和服务。


## 配置

- TZ=Asia/Shanghai 默认时区
- CONFIG_FILE=/etc/lmdeloy/proxy_config.yml 配置文件
- APIPROXY_PORT=11434 服务端口
- SERVER_STRATEGY=min_expected_latency 代理策略
- SERVER_APIKEY=snz1dp9527 API密钥

## 部署

在当前目录准备好配置文件`proxy_config.yml`：

```shell
cat > proxy_config.yml <<EOF
nodes:
  http://deepseek-r1:8003:
    avaiaible: true
    latency: []
    models:
    - DeepSeek-R1-Distill-Qwen-1.5B
    speed: -1
    unfinished: 0
  http://internvl2_5:8002:
    avaiaible: true
    latency: []
    models:
    - InternVL2_5-8B
    speed: -1
    unfinished: 0
EOF
```

添加独立部署配置：

```bash
snz1dpctl profile add taiyiflow-apiproxy@1.0.0 \
  --file proxy_config.yml=$PWD/proxy_config.yml \
  --env SERVER_APIKEY=snz1dp9527 \
  --overlay
```

然后启动代理服务：

```shell
snz1dpctl alone start taiyiflow-apiproxy
```

> 查看代理的模型列表（需要提供API密钥）

```bash
curl -X GET http://localhost:8008/v1/models -H "Authorization: Bearer snz1dp9527"
```
