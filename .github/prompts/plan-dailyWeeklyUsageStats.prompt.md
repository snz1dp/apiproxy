## Plan: 新增日周统计接口与归档

补齐按天、按周（周一到周日）两类“按模型统计”接口，采用新增日/周汇总表 + 定时归档的方式，保持与现有月/年统计同一架构：日志表 -> 汇总表 -> API 查询，优先复用月度聚合与API分页模式，降低行为差异和回归风险。

**Steps**
1. 对齐口径与参数协议（阻塞后续步骤）
- 明确日维度参数为 day（YYYY-MM-DD），周维度参数为 week_start（YYYY-MM-DD 且必须是周一）。
- 约定仅新增“按模型统计”接口，不新增 daily/weekly total 接口（根据当前决策）。
- 保持分页、models 过滤、ownerapp_id 过滤、422 参数校验行为与月/年接口一致。

2. 数据模型与导出扩展（依赖步骤1）
- 在 node model 中新增 AppDailyModelUsage、AppWeeklyModelUsage（字段与唯一键风格对齐 AppMonthlyModelUsage）。
- 更新模型导出与聚合映射：node 包导出、database models 顶层导出、DatabaseService 的 model mapping。

3. Alembic 迁移与版本同步（依赖步骤2）
- 生成迁移：新增 openaiapi_app_daily_usage、openaiapi_app_weekly_usage 两张表与必要索引/唯一约束。
- 同步 database service 内 last_version 为新 revision，满足项目迁移约束。

4. CRUD 聚合与查询能力（依赖步骤2，可与步骤3并行开发，最终联调需等待步骤3）
- 在 node/crud.py 新增 Daily/Weekly 聚合 dataclass 与函数：aggregate_*, upsert_*, select_*, count_*。
- 聚合来源为 ProxyNodeStatusLog，筛选已结束请求、ownerapp_id/model_name 非空，按 ownerapp_id + model_name 分组。

5. 归档任务与调度（依赖步骤4，联调依赖步骤3）
- 在 NodeProxyService 新增 _rollup_previous_day_usage、_rollup_previous_week_usage 及对应 task 方法。
- 周起始计算采用周一 00:00，任务归档上一个完整自然周。
- 在 main lifespan 注册 daily/weekly cron 任务；新增配置项（hour/minute）放到 settings base，沿用 monthly 配置风格。

6. API 与 Schema 扩展（依赖步骤4）
- 在 api/schemas.py 新增 AppDailyModelUsageResponse、AppWeeklyModelUsageResponse。
- 在 api/model_request_logs.py 新增路由：/request-logs/daily-usage、/request-logs/weekly-usage。
- 同时更新 html/dash.html，补齐日/周维度的统计展示与对应交互入口。
- 增加 _parse_day_start、_parse_week_start 参数解析与422错误文案，复用现有分页与过滤处理流程。

7. 测试补齐（依赖步骤3/4/5/6）
- 数据库测试：新增 test_daily_usage_rollup.py、test_weekly_usage_rollup.py，覆盖聚合范围、幂等 upsert、跨边界排除。
- API测试：扩展 test_model_request_logs.py，覆盖日/周正常查询、models 过滤、非法参数422、默认参数行为。
- 任务行为：至少覆盖 service 层 rollup 内部方法（无需真实调度器启动），验证提交与回滚路径。

8. 文档与回归验证（依赖步骤7）
- 更新 API 文档与字段说明，确保新增接口被开发者文档覆盖。
- 执行定向与全量测试，确认不影响既有月/年接口。

**Relevant files**
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/model_request_logs.py — 参考现有 monthly/yearly 接口模板并新增 daily/weekly 路由与参数解析
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py — 新增 daily/weekly 响应模型
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/html/dash.html — 同步补齐日/周统计维度的展示与交互
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/model.py — 新增 AppDailyModelUsage/AppWeeklyModelUsage ORM 模型
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/crud.py — 新增日/周聚合、upsert、查询、计数函数
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py — 新增日/周归档任务实现
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/main.py — 注册 daily/weekly 定时任务
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/settings/base.py — 增加 daily/weekly 任务调度配置
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/__init__.py — 导出新增 usage 模型
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/__init__.py — 导出新增 usage 模型
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py — 同步 last_version 与模型映射
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/alembic/versions/ — 新增迁移脚本
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_monthly_usage_rollup.py — 参考月度测试模板扩展日/周用量测试
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/api/test_model_request_logs.py — 扩展日/周接口测试
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/docs/api.md — 增补接口说明（如当前文档用于管理API）

**Verification**
1. 激活环境并运行迁移：conda activate apiproxy -> make alembic-revision -> alembic upgrade head。
2. 运行聚合相关测试：pytest src/apiproxy/tests/database/test_monthly_usage_rollup.py src/apiproxy/tests/database/test_daily_usage_rollup.py src/apiproxy/tests/database/test_weekly_usage_rollup.py。
3. 运行接口测试：pytest src/apiproxy/tests/api/test_model_request_logs.py。
4. 回归校验：请求 monthly/yearly 现有接口，确认返回结构与统计值未变化。
5. 页面联调验证：确认 dash.html 已暴露日/周统计入口，且能正确展示新增接口返回结果。
6. 时区边界验证：构造周日23:59、周一00:01样本，验证 week_start 归属正确。

**Decisions**
- 周口径：自然周（周一至周日）。
- 接口范围：仅新增按模型统计接口，不包含 total 接口（注意以应用为单位）。
- 数据来源：新增日/周汇总表并通过定时归档生成，不采用实时直接扫日志。
- 兼容策略：保持与月/年接口相同的分页、过滤、校验与错误语义。

**Further Considerations**
1. 参数协议建议固定为 day + week_start（周一），避免使用 YYYY-Www 在跨年周场景下增加解析复杂度。
2. 若后续出现“总计”诉求，可在当前日/周汇总表上增补 daily/weekly total 接口，成本较低。