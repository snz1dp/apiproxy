## Plan: 修复流式断连与状态刷新竞态

本次需要一起处理两类相关问题：其一，NodeProxyService.stream_generate 在生成器关闭时错误继续产出数据，导致 generator ignored GeneratorExit；其二，请求收尾后的节点状态刷新使用 ORM 对象“先查后改”模式，在流式断连、心跳刷新、请求收尾并发交错时，触发 openaiapi_status 的 StaleDataError。推荐做法是分两条主线并行设计、串行落地：先修正流式异常分类，确保断连不会走伪超时路径；再把 openaiapi_status 的持久化从脆弱的对象更新收敛成原子 upsert 或带重查的幂等更新，消除状态刷新竞态。

**Steps**

1. 锁定流式异常根因：以 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 中的 NodeProxyService.stream_generate 和 generate 为主，确认当前把 GeneratorExit 混入普通异常处理，属于 RuntimeError 的直接根因。
2. 第一阶段，修正流式关闭语义：调整 stream_generate 的异常分类，确保 GeneratorExit 直接重新抛出，不能再 yield 超时数据；同时把客户端断开、取消类异常和真实 requests 异常拆开，避免连接关闭时被记录成“接口调用超时”。该步骤依赖步骤 1。
3. 第一阶段，校对非流式路径：同步检查 generate 是否错误捕获 GeneratorExit 或取消类异常；按同样原则只在真实调用失败时返回 handle_api_timeout。该步骤可与步骤 2 同步设计、顺序实施。
4. 第二阶段，锁定状态刷新竞态入口：围绕 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 中 \_persist_health_check_result_async、\_record_request_start_async、\_finalize_request_log_async、\_refresh_node_metrics_async 和初始化阶段对 upsert_proxy_node_status 的调用，整理 openaiapi_status 的所有写路径，确认它们都依赖 get_or_create_proxy_node_status 返回 ORM 实体后再 session.add 持久化，存在并发窗口。该步骤可与步骤 2 并行研究，但实现依赖步骤 2 完成后再统一落地。
5. 第二阶段，确定状态持久化改造方案：优先把 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/crud.py 中的 upsert_proxy_node_status 扩展为真正的原子 upsert，并评估新增统一的“刷新状态” helper，供 healthcheck、request start/finalize、metrics refresh 复用，避免各处重复 get_or_create 后持有旧对象再更新。若现有代码结构不便一次性统一，则至少为关键写路径补“更新失败后按唯一键重查并重试一次”的幂等兜底。该步骤依赖步骤 4。
6. 第二阶段，收敛 meta.status_id 的使用方式：复核 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py 中 meta.status_id 作为快捷定位 ID 的使用边界，避免把它当成长期稳定主键假设。计划上应改为“status_id 仅作优化命中，不命中或更新失败时必须退回 node_id + proxy_id 唯一键重新定位”。该步骤依赖步骤 5。
7. 第二阶段，明确删除与刷新的并发边界：复核 remove_stale_nodes_by_expiration 与其它状态写路径的关系。当前代码不会删除当前 proxy_instance_id 的状态行，因此不能把根因简单归因于当前行被清理；计划应把关注点放在多写路径并发更新、实例切换或状态重建后的旧实体更新失败上。该步骤依赖步骤 4。
8. 第三阶段，复核上层流式包装兼容性：检查 /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/completions.py 中 stream_with_usage_logging、\_mark_client_disconnect、background task 收尾逻辑，确保底层异常分类修正后，request_ctx.abort、usage 汇总、post_call 结算链路仍然正确。该步骤依赖步骤 2 和步骤 3。
9. 第三阶段，补测试：新增或扩展 nodeproxy/service 与 proxy crud 相关测试。至少覆盖四类场景：关闭 stream_generate 生成器时不再抛出 generator ignored GeneratorExit；真实 requests 超时仍返回 API_TIMEOUT 错误体；并发/重入状态刷新时 openaiapi_status 不再抛出 StaleDataError；status_id 失效或旧实体失配时仍能通过唯一键恢复并完成写入。该步骤依赖步骤 5、步骤 6、步骤 8。
10. 第三阶段，回归验证：执行 nodeproxy、流式 completions、状态日志与 proxy status 相关定向测试，确认不再误报“接口调用超时”，同时请求日志、abort 标记、健康检查与状态指标刷新没有回退。该步骤依赖步骤 9。

**Relevant files**

- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/nodeproxy/service.py — 核心改造点，涉及 stream_generate、generate、\_persist_health_check_result_async、\_record_request_start_async、\_finalize_request_log_async、\_refresh_node_metrics_async、post_call、remove_stale_nodes_by_expiration。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/crud.py — 需要重点复用或改造的持久化入口，尤其是 get_or_create_proxy_node_status 与 upsert_proxy_node_status。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/services/database/models/proxy/model.py — 确认 openaiapi_status 的唯一键、主键和字段语义，支撑原子 upsert 方案。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/v1/completions.py — 复核 stream_with_usage_logging、\_mark_client_disconnect 与 BackgroundTasks 收尾是否与底层修复兼容。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/openaiproxy/api/schemas.py — 参考 DisconnectHandlerStreamingResponse 的断连触发机制，确认不需要协议层改动。
- /Users/neeker/工作/技术开发平台/人工智能平台/太乙智启/apiproxy/src/apiproxy/tests — 新增或扩展单元测试的位置，优先沿用现有异步测试夹具。

**Verification**

1. 编写单元测试，直接消费并关闭 stream_generate 的生成器，验证不会再出现 RuntimeError: generator ignored GeneratorExit。
2. 模拟 requests.post 抛出 timeout 或 response.iter_lines 抛出读取超时，验证仍返回现有 API_TIMEOUT 错误体，且日志不把断连误记为超时。
3. 模拟客户端断开或显式关闭流，验证 request_ctx.abort 能通过上层包装逻辑被设置，且后台 post_call 不再引出二次异常。
4. 为 openaiapi_status 的写入补并发或重入测试，验证在 healthcheck、request finalize、metrics refresh 连续触发时不会出现 StaleDataError。
5. 模拟 status_id 失效、记录被重建或按唯一键重新定位的场景，验证状态更新仍能成功提交。
6. 运行与流式 completions、request logs、proxy status 相关的定向测试，确认 usage 统计、节点可用性、请求日志结算未退化。
7. 如环境允许，做一次人工回归：发起流式请求后中途断开，检查服务端日志仅记录断连或取消，不再出现 RuntimeError 和 openaiapi_status 的 StaleDataError。

**Decisions**

- 本次范围包含两部分：修复 GeneratorExit 违规 yield 的根因，以及修复 openaiapi_status 刷新路径的并发持久化竞态。
- 本次不扩展接口协议，不修改返回体结构，只纠正异常分类、日志语义、状态持久化方式与测试覆盖。
- 对 openaiapi_status 的修复优先级高于简单捕获并吞掉 StaleDataError；推荐从持久化方式上消除竞态，而不是只做日志降噪。
- remove_stale_nodes_by_expiration 不是当前最主要怀疑点，因为现有过滤条件默认不删除当前 proxy_instance_id 的状态行；实现时应重点关注多写路径共享同一状态记录的并发更新。

**Further Considerations**

1. 如果一次性把所有状态写路径都切到统一 upsert helper 代价过大，可以先修 \_refresh_node_metrics_async 和健康检查/请求收尾这几条高频路径，再评估是否继续收敛初始化路径。
2. 如果现网告警依赖“接口调用超时”文案，建议保留真实超时文案不变，仅新增断连/取消日志，避免影响外部告警规则。
3. 如果后续仍有零星 StaleDataError，可考虑为 openaiapi_status 的更新增加一次性重试，但这应作为幂等保护，不应替代原子 upsert 方案。
