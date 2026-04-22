## Plan: Anthropic 协议兼容

在不破坏现有 OpenAI 兼容链路的前提下，引入“协议识别 + 节点协议能力 + 协议适配层”三层增量改造：节点新增 protocol_type（openai / anthropic / both，默认 openai），南向新增 Anthropic 常用接口，NodeProxyService 在选节点后按请求协议与节点协议决定是否转换，并把请求协议写入请求日志。对现有 OpenAI 路由、Bearer 鉴权、配额和日志主链保持默认不变，仅在识别到 Anthropic 协议或命中跨协议节点时进入新增分支。

**Steps**

1. 阶段一：补齐数据契约与默认值。
   1.1 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/model.py 为 Node 新增 ProtocolType 枚举与 protocol_type 字段，默认值设为 openai，保证存量节点行为不变。
   1.2 在同一 Node 数据模型中新增南向请求代理配置字段（建议命名为 request_proxy_url 或 proxy_url），用于描述该节点下游调用时应走的 HTTP/HTTPS 代理地址；默认值为空，保证存量节点仍按直连行为工作。
   1.3 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/model.py 为 ProxyNodeStatusLog 新增 request_protocol 字段，仅记录北向入站协议，不改变现有聚合维度。
   1.4 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py 同步扩展 CreateOpenAINode、OpenAINodeUpdate、OpenAINodeReponse、ModelServiceRequestLogResponse，使节点管理接口可维护 request_proxy_url。
   1.5 生成 Alembic 迁移脚本，补历史数据默认值 openai，并为新增代理字段回填空值；同时更新 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py 中 last_version。以上步骤阻塞后续实现。
2. 阶段二：建立协议识别与节点能力判定层。_depends on 1_
   2.1 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/utils.py 扩展 /v1 鉴权入口：继续支持 Authorization: Bearer，同时兼容 Anthropic 风格的 x-api-key，并基于 anthropic-version / x-api-key / 路由特征解析“请求协议类型”。保持 AccessKeyContext、配额身份信息与现有 Bearer 路径完全兼容。
   2.2 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/schemas.py 与 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 的运行时状态中加入 protocol_type 与 request_proxy_url，使 get_node_url 和后续下游调用都能拿到节点级代理配置。
   2.3 同步改造 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/node_manager.py 的创建、更新与在线校验逻辑：
   - openai：沿用 Bearer + /v1/models。
   - anthropic：使用 Anthropic 所需请求头校验 /v1/models。
   - both：在非 trusted 场景下同时校验两种协议能力，确保字段语义真实。
   2.4 节点管理入口同时为 request_proxy_url 增加格式校验与安全约束：仅接受显式 http/https 代理地址，并在节点校验、/v1/models 探测与健康检查请求中复用该代理配置，避免“配置可保存但实际请求不走代理”的行为偏差。
3. 阶段三：抽出协议适配层，并把转换约束集中到单点。_depends on 2_
   3.1 在现有 v1 API 目录与 nodeproxy 服务目录新增 Anthropic 适配模块，定义统一的“协议上下文”对象，至少包含 request_protocol、target_protocol、target_endpoint、request_headers、stream 标记，以及节点级 request_proxy_url。
   3.2 将 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/completions.py 中现有 OpenAI chat/completions 主流程下沉为“OpenAI 入站适配器”，把请求体解析、usage 回填、SSE 片段提取保留在协议适配层，而不是直接散落在路由函数里。
   3.3 为 Anthropic messages 建立对等的入站适配器，覆盖普通响应与流式 SSE 事件转换，并定义双向转换矩阵：
   - OpenAI 入站 -> openai / both 节点：原样透传。
   - OpenAI 入站 -> anthropic 节点：将 chat/completions 或 completions 转为 messages，再把响应转回 OpenAI。
   - Anthropic 入站 -> anthropic / both 节点：原样透传。
   - Anthropic 入站 -> openai 节点：将 messages 转为 chat/completions，再把响应转回 Anthropic。
     3.4 对没有稳定语义映射的接口单独约束：
   - OpenAI embeddings、rerank 继续只服务 openai / both 节点，不对 anthropic-only 节点做伪转换。
   - Anthropic message batches 接受 OpenAI 伪转换：对 anthropic / both 节点优先原生透传；若落到 openai-only 节点，则拆分批任务并映射为 OpenAI 可执行的单请求作业，代理侧负责批次状态聚合、结果回填与错误整形。
   - Anthropic count_tokens 对 anthropic / both 节点优先原生透传；若落到 openai-only 节点，则基于转换后的 OpenAI 请求做本地估算，避免凭空新增后端依赖。
4. 阶段四：接入南向路由并最小化变更现有入口。_depends on 3_
   4.1 在现有 v1 路由体系中新增 Anthropic 常用接口，至少覆盖 messages、messages 流式响应、messages/count_tokens、message batches 相关端点，以及协议化的 models 返回。
   4.2 更新 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/**init**.py 与 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/router.py，按现有 v1 聚合方式注册新路由，保证现有 OpenAI 路由路径不变。
   4.3 将 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/models.py 改造成协议感知入口：同一路径 /v1/models 根据协议识别返回 OpenAI 形状或 Anthropic 形状，避免重复路径冲突。
   4.4 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 的 generate / stream_generate 及健康检查相关 HTTP 请求中接入节点级 request_proxy_url，让协议转换后的真实南向请求、模型探测和健康检查都走相同代理链路。
5. 阶段五：把协议信息贯穿日志与后处理。_parallel with 4 after 3_
   5.1 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 的 \_RequestContext、pre_call、\_record_request_start_async、\_finalize_request_log_async 中补 request_protocol 透传。
   5.2 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/crud.py 的 create_proxy_node_status_log_entry / update_proxy_node_status_log_entry 中写入新字段，确保流式、非流式、异常和客户端断开场景都能记录协议类型。
   5.3 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/model_request_logs.py 维持现有查询逻辑，仅在响应中透出 request_protocol；除非后续有明确需求，本次不扩展筛选维度，避免无关行为变化。
6. 阶段六：测试与回归保护。_depends on 1-5_
   6.1 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_node_manager.py 与 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_node_crud.py，覆盖 protocol_type 默认值、request_proxy_url 创建更新与格式校验、trusted / verify 与双协议校验分支。
   6.2 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/services/test_nodeproxy_service.py，覆盖同协议直通、跨协议转换、节点优先级选择、count_tokens 本地估算、message batches 的 OpenAI 伪转换分支、节点级 request_proxy_url 在真实下游请求与健康检查中的传递、日志 request_protocol 落库。
   6.3 在现有 API 测试目录中新增 Anthropic 协议用例，参照 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_completions_responses.py 的组织方式，分别覆盖非流式、流式 SSE、/v1/models 协议分发、鉴权头兼容、错误响应形状。
   6.4 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_model_request_logs.py，确保 request_protocol 可查询可展示，且不影响既有分页/过滤断言。

**Relevant files**

- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/model.py — Node / NodeModel 数据契约；新增 ProtocolType 与节点字段。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/schemas.py — 运行时节点状态；新增 request_proxy_url，承接节点级下游代理配置。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/model.py — ProxyNodeStatusLog 日志表；新增 request_protocol。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/crud.py — 日志写入入口，适合单点落库协议字段。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py — Alembic 版本同步。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py — 节点与请求日志响应 schema 扩展。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/utils.py — Bearer / x-api-key 鉴权兼容与请求协议识别。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/node_manager.py — 节点管理、协议字段维护、双协议校验。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/completions.py — 现有 OpenAI 入口，需抽离为协议适配可复用实现。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/models.py — 处理 OpenAI / Anthropic 同路径模型列表分发。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/**init**.py — 注册新增 Anthropic 路由。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/router.py — 聚合 v1 路由。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py — 节点选择、请求上下文、转发与日志收口的核心控制面。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_node_manager.py — 节点协议字段与校验分支回归。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_model_request_logs.py — 日志协议字段展示回归。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/services/test_nodeproxy_service.py — 协议选择、转换与日志链路核心回归。

**Verification**

1. 在仓库根目录执行 conda activate apiproxy 后，先跑节点与数据库契约相关测试：pytest src/apiproxy/tests/database/test_node_crud.py src/apiproxy/tests/api/test_node_manager.py。
2. 跑服务层协议选择与转换测试：pytest src/apiproxy/tests/services/test_nodeproxy_service.py。
3. 跑 API 回归：pytest src/apiproxy/tests/api/test_completions_responses.py src/apiproxy/tests/api/test_model_request_logs.py，以及新增的 Anthropic 协议 API 用例。
4. 做一次迁移与启动烟测：make alembic-revision（生成后检查脚本）、应用迁移、启动服务后分别用 OpenAI 风格与 Anthropic 风格请求验证 /v1/models、消息生成、流式返回、count_tokens、message batches。
5. 人工确认回归基线：存量未配置 protocol_type 与 request_proxy_url 的节点默认按 openai + 直连行为工作；原有 Bearer 调用路径、节点管理接口、OpenAI embeddings / rerank 结果不变。

**Decisions**

- 为保证“不影响任何原有业务逻辑”，存量节点统一回填为 openai，且节点选择优先同协议节点，只有同协议不可用时才尝试可转换节点。
- request_protocol 记录的是客户端入站协议，而不是最终节点协议；这样最贴合审计需求，也不会改动现有聚合口径。是否需要额外记录 target_protocol / converted 标记，可作为后续增强，不纳入本次最小闭环。
- /v1/models 因路径冲突必须做协议感知分发，不能简单新增第二个同路径路由。
- message batches 纳入兼容范围：允许代理侧做 OpenAI 伪转换，但要在计划中额外控制批次拆分、异步状态机、结果聚合与失败回填，避免影响现有实时请求链路。
- OpenAI 现有 completions / chat completions / embeddings / rerank 路由与返回体保持向后兼容；Anthropic 仅在其对应入口或协议识别命中时生效。
- both 节点采用严格语义：仅当 OpenAI 与 Anthropic 两种协议校验都成功时才允许保存为 both；否则要求显式保存为单协议节点。
- 南向节点代理配置采用节点粒度而不是全局粒度：request_proxy_url 仅影响该节点的模型探测、健康检查与真实转发请求，不应污染其他节点或整个进程的网络行为。

**Further Considerations**

1. 如果后续需要更强审计能力，建议追加 target_protocol 与 converted 布尔标记，但这会扩大日志表与查询 API 改动面，本次先不做。
2. 如果后端节点大量混跑两种协议，建议在节点选择中增加“协议转换成本”权重，而不仅是同协议优先；当前计划先以不改变既有调度策略为先。
