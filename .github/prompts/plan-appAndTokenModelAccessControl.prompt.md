## Plan: 应用与令牌模型访问控制

在现有配额与鉴权链路之上增加“双层模型白名单”控制：应用级采用独立应用访问策略表，令牌级复用 ApiKey 扩展字段；空列表表示不限制；最终生效模型集合按“应用级 ∩ 令牌级”计算。实现重点是把有效白名单在鉴权阶段一次性解析进请求上下文，并在 /v1/models 与所有实际请求入口复用同一套校验逻辑，避免重复查库与接口遗漏。

**Steps**

1. Phase 1 - 数据模型与迁移
2. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/app/model.py 中新增应用访问策略模型，按现有 app 目录模式配套唯一键 ownerapp_id、allowed_models 字段、created_at/updated_at；该表只承载“应用级模型访问控制”，不复用 AppQuota，避免一个应用多配额单时的语义歧义。
3. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/apikey/model.py 的 ApiKey 增加 allowed_models 字段，用于令牌级进一步收紧访问范围。
4. 生成 Alembic 迁移脚本，覆盖新增表与 ApiKey 新列，并同步更新 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py 里的 last_version。该步骤阻塞后续联调。
5. Phase 2 - CRUD 与管理 API
6. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/app/crud.py 增加应用访问策略 CRUD（按 ownerapp_id 查询、创建/更新、列表/详情视需要最小化实现）；在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/apikey/crud.py 让 ApiKey 的 create/update/select 自然支持 allowed_models 字段。
7. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py 扩展 ApiKeyCreate、ApiKeyUpdate、ApiKeyRead，新增应用访问策略相关 schema，输入输出统一使用 List[str]；在落库前做标准化：去空白、去重、保持稳定顺序、空列表序列化为“不限制”。
8. 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/apikey_manager.py 以支持令牌级 allowed_models 的创建、查询、更新；新增一个独立的应用访问策略管理路由模块，挂到现有管理 API 中，而不是混入 /app-quotas，避免职责混杂。该新增路由模块与 ApiKey 管理改造可并行。
9. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/**init**.py 与 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/main.py 注册新的应用访问策略路由。
10. Phase 3 - 请求期生效逻辑
11. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/utils.py 扩展 AccessKeyContext，鉴权成功后同时装载 ApiKey.allowed_models 与应用访问策略的 allowed_models，并在这里计算 effective_allowed_models。规则：静态管理密钥直接绕过限制；应用级为空视为不限制；令牌级为空视为不限制；双层都存在时取交集；若交集为空则该请求无任何可访问模型。
12. 在 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 增加“按有效白名单过滤模型列表”和“判定单模型是否被白名单允许”的通用方法，并让 check_request_model 能同时处理“模型不存在/协议不支持”和“模型被访问策略拒绝”两类情况。这里是运行期控制的复用锚点。
13. 更新 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/models.py，让 /v1/models 在 list_models_for_protocol 结果上应用 effective_allowed_models 过滤。
14. 更新 OpenAI 与 Anthropic 全部模型入口，使它们统一复用有效白名单：/Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/completions.py、/Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/embeddings.py、/Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/rerank.py、/Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/anthropic.py。Anthropic 里不仅是 /messages，还包括 count_tokens 与 batches 相关入口，避免出现“列表不可见但仍可调用”的绕过。
15. Phase 4 - 测试与验证
16. 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_apikey_manager.py，覆盖 ApiKey allowed_models 的创建、回显、更新与空列表语义。
17. 在现有测试风格下新增应用访问策略管理 API 测试，覆盖创建/更新/查询与 ownerapp_id 唯一约束。
18. 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_quota_apis.py 或新增相邻 API 测试，验证应用策略与令牌策略的交集语义，以及 /v1/models 返回过滤后的模型列表。
19. 扩展 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_completions_responses.py 或新增相邻集成测试，覆盖 OpenAI/Anthropic 请求对未授权模型返回拒绝、授权模型正常放行、静态管理密钥绕过限制。
20. 如需更细粒度保障，再补数据库层测试：应用访问策略 CRUD、ApiKey 新字段持久化、空列表与 null 的标准化行为。

**Relevant files**

- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/utils.py — 现有 check_access_key 与 AccessKeyContext，是把双层策略折叠为 effective_allowed_models 的最佳位置。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py — 现有 list_models_for_protocol、supports_model、check_request_model，是统一做“模型存在性 + 白名单许可”校验的核心复用点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/models.py — 现有 /v1/models 入口，需要应用 effective_allowed_models 过滤。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/completions.py — OpenAI chat/completions 主入口，当前已调用 check_request_model，可直接接入增强后的统一校验。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/embeddings.py — embeddings 入口，当前已调用 check_request_model。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/rerank.py — rerank 入口，当前已调用 check_request_model。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/anthropic.py — 目前多处直接调用 supports_model/get_node_url，需要统一补白名单校验，避免 Anthropic 旁路。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/apikey/model.py — ApiKey 模型新增 allowed_models。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/apikey/crud.py — ApiKey create/update/select 与新字段兼容。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/app/model.py — 新增应用访问策略模型，延续 app 目录组织方式。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/app/crud.py — 新增应用访问策略 CRUD。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py — 增补 ApiKey 与应用访问策略 schema。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/apikey_manager.py — 令牌级 allowed_models 的管理入口。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/**init**.py — 导出新的应用访问策略路由。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/main.py — 注册新的应用访问策略路由。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py — 按仓库约定同步 last_version。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_apikey_manager.py — 令牌级管理接口测试模板。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_quota_apis.py — 管理 API 集成测试模板，可复用会话与清理方式。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_completions_responses.py — 现有 OpenAI 请求相关测试入口，可扩展拒绝场景。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_app_quota.py — app 目录数据库测试风格参考。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_apikey_crud.py — apikey 目录数据库测试风格参考。

**Verification**

1. 运行针对管理面的窄测试：API Key 管理测试 + 新增应用访问策略管理测试，确认 allowed_models 的创建、更新、回显与空列表语义。
2. 运行针对运行期控制的窄测试：/v1/models 过滤、OpenAI chat/completions、embeddings、rerank、Anthropic messages/count_tokens/batches 的未授权模型拒绝与授权模型放行。
3. 运行数据库相关窄测试，确认新表/新列迁移后 CRUD 正常，且不触发已知外键与唯一键陷阱。
4. 如环境允许，再执行一次聚焦回归：与北向配额并存时，请求被模型策略拒绝不应错误消耗配额；静态管理密钥仍能查看并访问所有模型。

**Decisions**

- 空 allowed_models 表示“不限制”，保持现有默认兼容行为。
- 双层控制按“应用级 ∩ 令牌级”计算，令牌只能进一步收紧，不得放宽应用级策略。
- 应用级控制使用独立策略表，不复用 AppQuota。
- 本次范围包含管理 API、/v1/models 可见性和运行期请求拦截；不包含前端控制台页面改造，除非后续另提需求。
- 静态管理密钥（MANAGER_KEY_ID）默认绕过模型访问控制，避免影响运维与排障。
