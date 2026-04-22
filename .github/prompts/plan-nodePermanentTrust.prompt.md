## Plan: 节点永久信任标记

为 OpenAI 兼容节点增加一个持久化的“永久信任/跳过模型探测”标记，用来解除当前对 /v1/models 的硬依赖。推荐方案是新增显式布尔字段（建议命名为 `trusted_without_models_endpoint` 或语义接近的名称），并将其影响限定在三处：创建/更新节点时跳过 /v1/models 验证、周期健康检查不再用 /v1/models 打掉节点、节点状态刷新时不因缺少远端探测结果而自动置为不可用。模型路由仍以数据库中的手工节点模型列表为准，不把“永久信任”扩展成“无模型也可按任意模型路由”。

**Steps**
1. 阶段一：定义字段与数据结构。为节点数据库模型新增显式布尔字段，默认值保持现有行为不变；同步更新创建/更新/读取 schema，让管理 API 可以持久化和返回该标记。这个阶段阻塞后续所有步骤。
2. 阶段一：生成 Alembic 迁移并同步数据库初始化版本号。新增节点字段的迁移脚本后，更新数据库服务中的 `last_version`，保证新库初始化与迁移链一致。这个步骤依赖步骤 1。
3. 阶段二：调整节点管理 API 的验证入口。在创建节点与更新节点 API 中，将“是否请求 /v1/models”判断改为 `verify` 与新标记联合决定：默认仍校验；当新标记为真时，跳过 _verify_models_endpoint。这个步骤依赖步骤 1，可与步骤 2 并行开发，但最终一起验证。
4. 阶段二：调整运行时健康检查逻辑。修改 NodeProxyService 中 `_should_probe_status` 与心跳结果应用路径，使带有新标记的节点不再触发基于 /v1/models 的探测，也不会因为缺少该探测而被自动移出运行时状态。关键点是把“是否探测”和“是否可路由”分开处理。这个步骤依赖步骤 1。
5. 阶段二：保留模型路由门槛。维持 `get_node_url` / `_status_supports_model` 基于数据库 NodeModel 列表做匹配；只允许“永久信任”节点在已有手工模型配置时参与路由，不建议放开成空模型节点可承接任意模型请求。若仍希望在状态页里展示该节点为存活，可让其保留在 `snode/status`，但是否进入 `nodes` 应继续受 `status.models` 约束。这个步骤依赖步骤 4。
6. 阶段三：补充测试。API 测试覆盖创建/更新节点时新字段的收发、开启该标记后即使 verify 默认开启也不请求 /v1/models；服务层测试覆盖带该标记的节点在刷新状态与心跳流程中不会因 /v1/models 缺失被标记为不可用；数据库层测试覆盖新字段的默认值和持久化行为。这个步骤依赖步骤 2 至步骤 5。
7. 阶段三：补充文档与操作约束。更新节点管理相关文档，明确该标记只表示“跳过 /v1/models 探测并保留节点可用资格”，不自动发现模型能力；启用后必须手工维护节点模型列表，否则请求调度仍匹配不到节点。这个步骤可与步骤 6 并行。

**Relevant files**
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/model.py — `Node` 模型；新增持久化布尔字段。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py — `CreateOpenAINode`、`OpenAINodeUpdate`、`OpenAINodeReponse`；暴露和返回新字段。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/node_manager.py — `_verify_models_endpoint` 的调用点、`create_openaiapi_node`、`update_openaiapi_node`；把新字段纳入验证分支。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py — `_refresh_nodes_from_database`、`_should_probe_status`、`perform_node_health_checks`、`_apply_health_check_result`、`get_node_url`、`_status_supports_model`；拆开“探测存活”和“模型可路由”逻辑。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py — `create_db_and_tables` 中的 `last_version`；迁移后必须同步。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_node_manager.py — API 行为测试模板，适合补创建/更新/跳过验证用例。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_node_crud.py — 新字段持久化的基础数据库测试入口。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/services/test_nodeproxy_service.py — NodeProxyService 行为测试入口，适合补心跳与节点选择相关测试。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/alembic/versions/ — 新增迁移脚本目录。

**Verification**
1. 运行节点管理 API 相关测试，重点验证创建/更新节点在新标记为真时不会触发 /v1/models 请求，且默认行为不回归。
2. 运行 NodeProxyService 相关测试，验证带新标记的节点在健康检查周期后不会因 /v1/models 不可达而变成不可用；同时验证未配置 NodeModel 的节点仍不会被 `get_node_url` 选中。
3. 运行数据库相关测试，验证新字段默认值、读写与迁移后的模式健康检查通过。
4. 对新增迁移做一次数据库初始化验证，确认新库初始化使用更新后的 `last_version` 不会漏列。
5. 如测试环境允许，再做一次手工链路验证：创建一个不提供 /v1/models 的节点并打开新标记，手工添加节点模型，确认节点可保存、状态保留、并能按已配置模型路由。

**Decisions**
- 已确认：需要新增显式字段，不复用现有 `health_check`，也不把 `verify` 当成持久化能力开关。
- 已确认：新标记覆盖创建/更新节点时跳过 /v1/models 验证，以及周期健康检查不再依赖 /v1/models。
- 已确认：模型来源仍是手工维护的节点模型列表，运行时路由继续按模型匹配，不把“永久信任”扩展成通配能力。
- 计划建议：即使状态展示允许“被信任节点”保持存活，也不要放开 `get_node_url` 对 `status.models` 的要求，否则会破坏当前按模型和配额路由的边界。

**Further Considerations**
1. 字段命名建议优先选表达业务语义而不是实现细节，例如 `trusted` 过宽，`skip_model_discovery` 只覆盖发现阶段，`trusted_without_models_endpoint` 更贴近当前需求，但名字会偏长；执行时可在可读性与准确性之间做一次压缩。
2. 如果后续还要支持非 /v1/models 的探活方式，建议把这次字段设计成“信任策略/探测策略”的前向兼容入口，而不是把所有语义都塞进 `health_check`。