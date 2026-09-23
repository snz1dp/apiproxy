# 修复计划：节点多 Key 场景下的故障处置粒度错配

> 状态：**已确认**（用户确认后方可实施）
> 来源：2026-09-22 会话调研结论（节点状态/模型状态/密钥状态机制分析）
> 核心问题：密钥选择是 key 级的，但故障处置和可用性判定几乎都是节点级的，粒度错配，导致多 key 的容灾价值被大幅削弱。

## 一、缺陷清单（按严重程度排序）

| 编号 | 缺陷 | 严重程度 | 影响面 |
|------|------|---------|--------|
| D1 | 单个 key 限额触发 `mark_backend_node_unavailable`，连坐整个节点 | 高 | 多 key 容灾失效 |
| D2 | 容量耗尽重试只切节点（`exclude_node_urls`），不尝试同节点其他 key | 高 | 不必要的节点切换 |
| D3 | 无效 key（401/403）无任何熔断机制，加权随机持续选中坏 key | 高 | 随机性请求失败 |
| D4 | 健康检查固定用 priority 最高的单 key 探测，坏 key 误判节点死亡 | 中 | 误杀节点 |

## 二、修复批次与顺序

按"依赖关系 + 风险等级"分两个批次，批次内串行、批次间依赖递进：

```mermaid
graph LR
    B1["批次1: key级故障处置<br/>D1+D2+D3"] --> B2["批次2: 健康检查降级<br/>D4"]
```

**排序依据**：
- 批次 1 是收益最大、相互耦合最深的三个缺陷（D1/D2/D3 共用"key 级故障识别"这一基础设施），必须一起改，否则只修 D1 不修 D2，key 级故障仍会触发不必要的节点切换；
- 批次 2 依赖批次 1 的 key 级失败计数（D4 的降级重试需要知道哪些 key 不可用）。

---

## 批次 1：key 级故障处置（D1 + D2 + D3）

### 修复方案

**1. D1：限额时不再无条件连坐节点**

修改 `cleanup_backend_capacity_exhausted_attempt`（service.py L2356）：

- 当前：`mark_backend_node_unavailable(node_url, reason=message)` 无条件踢节点。
- 改为：踢节点前先检查该节点是否还有**健康 key**（进入 `select_node_api_key` 的候选集非空）。有 → 只冻结/禁用触发 key，节点保持可用；无 → 维持现状踢节点（此时节点确实无可用凭证）。
- 边界：`status.api_keys` 为空（未配置独立 key，回退 `Node.api_key` 的旧式节点）→ 维持现状踢节点（key 级处置无意义）。

**2. D2：重试层级改为"先换 key 后换节点"**

修改 `_retry_proxy_attempt_after_capacity_exhausted`（completions.py L626，同时存在于 responses.py L346 及各 API 端点的同构实现）：

- 当前：`get_node_url(exclude_node_urls=attempted_node_urls)` 直接换节点。
- 改为三级重试：
  1. **同节点换 key**：若该节点仍有健康 key 且未尝试过，重新 `select_node_api_key`（排除已失败的 key），重发请求；
  2. **换节点**：同节点 key 耗尽时，走现有 `get_node_url(exclude_node_urls=...)`；
  3. **返回错误**：无节点可用时返回现有 service_unavailable 响应。
- 需要给 `_RequestContext` 增加 `attempted_api_key_ids: set[UUID]` 字段记录本请求内已失败的 key，避免选路时重复选中。

**3. D3：无效 key 熔断**

- 在 `_RequestContext` 增加 key 级失败计数（内存态，随请求上下文生命周期）。
- 修改 `_post_process_api_key_usage`（L1394）或新增后处理钩子：识别 401/403/`invalid_api_key` 类响应（新增 `INVALID_API_KEY_HINTS` 常量集合，模式仿照 `BACKEND_CAPACITY_EXHAUSTED_HINTS`）。
- 触发条件：同一 key 在本实例内连续失败 N 次（建议 N=3，可配置）→ 调用 `_disable_node_api_key` 自动禁用（复用现有禁用链路，写 reason）。
- 失败计数存储：`_NodeMetadata` 或独立的 `_api_key_failure_counts: Dict[UUID, int]`，key 被禁用/刷新重建时清零。

### 测试方案

- **单元测试**（`tests/services/test_nodeproxy_service.py` 扩展）：
  - 节点配置 3 个 key，key A 触发限额 → 断言节点仍在 `nodes` 池、key A 被冻结、key B/C 仍可被 `select_node_api_key` 选中；
  - 节点仅 1 个 key 且触发限额 → 断言节点被踢出（维持现状）；
  - 无独立 key（回退 `Node.api_key`）触发限额 → 断言节点被踢出（维持现状）；
  - key 连续 3 次 401 → 断言 `disable_node_api_key` 被调用且 reason 包含无效密钥描述；
  - 401 失败 2 次后成功 1 次 → 断言计数清零不禁用。
- **API 集成测试**（`tests/api/test_completions_runtime.py` 扩展）：
  - mock 下游：第一次响应限额错误、第二次（换 key 后）成功 → 断言请求最终成功且只调用了一个节点；
  - mock 下游：所有 key 均限额 → 断言响应为 service_unavailable 且尝试节点数正确。

### 验收标准

1. 多 key 节点单 key 限额后，节点不离开 `nodes` 可用池（`snode[url].avaiaible` 保持 True）；
2. 同节点换 key 重试成功时，不发生节点切换（请求日志中 `node_url` 不变）；
3. 无效 key 连续失败达阈值后 30 秒内（下一轮刷新）不再被选中；
4. 全部现有测试通过（`uv run pytest src/apiproxy/tests -x`），无回归。

---

## 批次 2：健康检查 key 降级重试（D4）

### 修复方案

修改 `_check_single_node`（L1608）：

- 当前：固定用 `_select_health_check_api_key`（priority 最高）探测一次。
- 改为：探测失败（非 200 或异常）时，按 priority 降序取下一个未尝试的 key 重试一次（最多 2 个 key，避免心跳线程阻塞过长）。
- 若所有 key 均失败才判节点不可用。
- 注意：健康检查不累计 `tokens_used`、不触发禁用（现有语义保持）。

### 测试方案

- **单元测试**：
  - 节点 3 个 key，priority 最高的 key 探测返回 401，次优 key 返回 200 → 断言节点可用性为 True；
  - 所有 key 探测均失败 → 断言节点不可用；
  - 无独立 key（回退 `Node.api_key`）→ 断言行为与现状一致（单次探测）。

### 验收标准

1. 坏 key 在最高优先级时，节点不再被误判死亡（心跳日志中无对应失败记录）；
2. 现有健康检查相关测试全部通过，无回归。

---

## 三、全局约束与回退方案

### 代码规范约束（来自项目 CLAUDE.md）

- Python 3.12 / FastAPI / pathlib / SQLAlchemy ORM / Alembic 迁移 / uv 依赖管理 / PEP 8 + Black；
- 函数/类必须有 docstring，禁止 `except Exception` 宽泛捕获（现有代码有大量历史遗留，新代码严格遵守）；
- 所有数据库操作通过 ORM，禁止裸 SQL。

### 回退方案

- 全部为内存逻辑修改，回退 = git revert 对应 commit。

### 实施节奏

- 每批次完成后运行全量测试 + 本批次验收标准逐项确认，通过后进入下一批次；
- 批次内每个缺陷独立 commit，便于定点回退；
- 缺陷修复不改变对外 API 语义（管理 API 的请求/响应 schema 不变，仅行为变化）。

## 四、风险提示

1. **D1+D2 的重试层级变化会改变请求日志的分布**：同节点换 key 的重试会在同一 `node_url` 下产生多条日志（每次尝试一条），监控/报表若按日志条数统计请求数会翻倍——需确认现有报表（daily/weekly/monthly rollup）按 `request_id` 或日志条数统计，若是后者需同步调整。
2. **D3 的失败计数是实例内存态**，多实例下每个实例独立计数，key 真正被禁用前可能累计被选中更多次（N×实例数），可接受（禁用是最终一致的）。
