## Plan: 多实例报表任务幂等化

通过两层控制解决多实例报表任务冲突：第一层把日报、周报、月报的写入改为数据库原子 upsert，根除先查后插导致的唯一键冲突；第二层新增跨实例可见的数据库级任务锁，并用 TTL 租约语义处理实例异常退出，确保同一时间只有一个实例执行整段报表汇总，从而减少重复扫描与重复写入。若任务锁已被占用，则本轮任务直接取消，只记录一条“已有任务在执行，忽略本次调度”的信息日志。方案需要同时兼容 PostgreSQL 生产环境与 SQLite 测试环境，并补齐迁移与并发测试。

**Steps**

1. Phase 1: 固定改动边界。保持 APScheduler 调度入口与汇总时间窗口不变，只处理三条链路：main.py 中的调度注册，nodeproxy/service.py 中的三个 rollup task 与异步汇总方法，node/crud.py 中的三个报表 upsert。这个阶段仅整理复用点与约束，作为后续实现基线。
2. Phase 2: 原子化报表写入。重构 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/crud.py 中的 upsert_app_daily_model_usage、upsert_app_weekly_model_usage、upsert_app_monthly_model_usage，移除先 select 再 insert/update 的竞态窗口，改为单条语句的原子 upsert。建议抽一个共享 helper，按数据库方言选择 SQLite insert 或 PostgreSQL insert，并统一更新 call_count、request_tokens、response_tokens、total_tokens、updated_at。返回值模式复用 proxy/crud.py 中 upsert_proxy_node_status 的做法，先执行 upsert，再按主键或唯一键回读持久化对象。此步骤依赖步骤 1。
3. Phase 3: 新增跨实例数据库级任务锁。新增一个轻量任务锁模型与 CRUD，建议放在 proxy 域，因为它属于运行时实例协调而非报表结果本身。任务锁表至少包含 task_name 唯一键、owner_token、lease_until、created_at、updated_at。owner_token 建议使用独立字符串而不是外键关联 ProxyInstance，避免调度任务与代理实例注册顺序耦合。加锁语义应为：当任务不存在时创建锁记录；当锁已过期时抢占；当当前持有者续租时更新；否则返回未获取。实现上优先使用数据库原子更新加插入重试，保证 PostgreSQL 与 SQLite 都可运行。这里本质是“数据库级任务锁”，但带 TTL 租约语义，避免实例异常退出后永久死锁。此步骤可与步骤 2 并行设计，但代码落地时建议先完成步骤 2。
4. Phase 4: 在报表任务入口接入任务锁。修改 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py，在 monthly_usage_rollup_task、daily_usage_rollup_task、weekly_usage_rollup_task 对应的异步执行路径中先尝试获取各自任务锁。获取失败时直接取消本轮任务，并记录一条信息日志，文案明确为“已有任务在执行，忽略本次调度”；获取成功后执行聚合与 upsert；执行结束后主动释放任务锁或将 lease_until 缩短到当前时间，异常时依赖 TTL 自动过期避免死锁。任务锁 TTL 建议略长于单次任务最大预期耗时，并作为常量或配置集中管理。此步骤依赖步骤 3，并受益于步骤 2。
5. Phase 5: 数据模型注册与迁移。为新租约表补充模型导出与 Alembic 迁移，并按仓库约定同步更新 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py 中的 last_version。需要更新 models/**init**.py 与 proxy 包导出，确保初始化和迁移都能识别新表。此步骤依赖步骤 3。
6. Phase 6: 补测试与回归验证。先保留现有日报、周报、月报聚合测试作为回归基线，再新增多实例场景测试。数据库层至少覆盖一个会失败的并发场景：对相同 ownerapp_id、model_name、周期起点重复执行 upsert 时不会抛唯一键异常，且最终只保留一条记录。服务层补租约测试，验证同一任务在租约持有期间第二个执行者会跳过。若测试框架不方便做真并发，可用两个独立 session 顺序模拟冲突窗口，证明旧实现会撞唯一键而新实现不会。此步骤依赖步骤 2 和步骤 4，且可以与步骤 5 的迁移检查并行收尾。

**Relevant files**

- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/main.py — 现有 APScheduler 注册入口，确认调度点不变。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py — \_rollup_previous_day_usage、\_rollup_previous_week_usage、\_rollup_previous_month_usage 以及三个同步 task 包装器，是接入任务租约与 skip 日志的主入口。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/crud.py — 三个 aggregate 函数与三个报表 upsert 函数，原子 upsert 的核心改动点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/node/model.py — 现有日报、周报、月报表及唯一约束定义，作为 upsert 冲突键来源。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/model.py — 建议新增任务租约模型，归类到实例运行时协调域。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/crud.py — 现有 on_conflict_do_update 参考实现，可复用返回值与冲突处理模式。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/**init**.py — 新模型导出注册。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/alembic/versions — 新增任务租约表迁移脚本。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/service.py — 迁移版本号同步点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_daily_usage_rollup.py — 日报回归与冲突场景测试首选落点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_weekly_usage_rollup.py — 周报回归测试落点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/database/test_monthly_usage_rollup.py — 月报回归测试落点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/services/test_nodeproxy_service.py — 服务层任务租约与重复执行跳过逻辑测试落点。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests/conftest.py — 异步 session 基线，确认测试仍走当前数据库初始化路径。

**Verification**

1. 在 conda activate apiproxy 环境下，先执行聚焦回归：pytest src/apiproxy/tests/database/test_daily_usage_rollup.py src/apiproxy/tests/database/test_weekly_usage_rollup.py src/apiproxy/tests/database/test_monthly_usage_rollup.py。
2. 执行服务层验证：pytest src/apiproxy/tests/services/test_nodeproxy_service.py -k rollup，确认数据库级任务锁接入后，重复任务会被直接忽略并记录 info 日志，而不是报错。
3. 执行新增的并发冲突测试，使用两个独立 AsyncSession 复现同一报表唯一键的竞争写入，验证最终只有一条记录且聚合值正确。
4. 生成并检查 Alembic 迁移，确认新任务锁表被创建、回滚正常，并同步更新 last_version。
5. 如环境允许，在多实例共享同一数据库的测试环境手工触发同一 rollup task，确认一个实例执行，其他实例记录“已有任务在执行，忽略本次调度”的 info 日志，且无 IntegrityError。

**Decisions**

- 已确认部署形态是多机器或多 Pod 共享数据库，因此不采用现有文件锁作为主方案。
- 已确认目标不仅是消除插入冲突，还要尽量避免重复执行整段报表计算，因此数据库级任务锁纳入本次范围。
- 本次范围包含数据库原子 upsert、跨实例数据库级任务锁、对应迁移与测试；不包含重做 APScheduler 架构，也不引入外部 Redis 等新基础设施。
- 文件锁 KeyedWorkerLockManager 保持不动，可继续服务于同机 worker 场景，但不作为本问题的修复路径。
- 任务锁拿不到时不视为异常，不做重试风暴处理，直接取消本轮任务并输出一条忽略信息日志。

- 用户希望最终将该计划写入当前工程的 .github 目录下，作为后续 refinement 的工作文件。

**Further Considerations**

1. 如果后续确认生产环境永远只跑 PostgreSQL，可将数据库级任务锁进一步收敛为 PostgreSQL advisory lock，代码更短；但当前仓库测试依赖 SQLite，现阶段不建议走方言专用方案。
2. 若报表执行耗时明显超过预期，任务锁需要支持执行中续租，避免长任务被误抢占；第一版可先采用保守 TTL，再根据实际耗时补续租。
