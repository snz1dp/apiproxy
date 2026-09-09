# 节点 APIKEY 限额冻结与周期自动重置 — 实施计划

> 状态：待确认
> 创建时间：2026-09-08
> 关联计划：`.github/prompts/node-apikey-implementation-plan.md`（已实现）

## 一、需求背景

节点独立 APIKEY（`NodeApiKey`）已实现多密钥加权选择与自动禁用。但用于 TokenPlan 类订阅
（以千问 TokenPlan 周限额为例）时存在两个问题：

1. **限额即禁用，需人工恢复**：密钥触发周限额后 `enabled=False`，必须等到重置日手动重新
   启用；多个 key 时运维成本高。期望行为是**冻结**（不改 `enabled`），到重置时间**自动解冻**。
2. **跨周期状态不同步**：本周期没用完（未触发冻结），但厂商侧已到重置点、额度已恢复，
   本系统 `tokens_used` 仍是旧值继续累积，导致后续误触发上限、管理界面用量与厂商不符。

设计原则（经讨论收敛）：

- **不搞窗口起点推断/探测式冻结**：周期限额场景下，由用户**必填**"下一次重置时间"
  （`quota_next_reset_at`），这是厂商控制台可直接看到的值，比系统推断可靠。
- **错误消息解析纠偏**：下游限额错误若携带精确重置时间（千问会返回），解析后覆盖
  `quota_next_reset_at`，永久对齐厂商节奏。
- **自动解冻 + 跨周期例行重置**：统一挂在现有刷新循环（`refresh_interval`，默认 10s），
  不新增线程/定时任务。
- **完全向后兼容**：`quota_reset_cycle=none`（默认）的密钥保持现有"限额即禁用"行为。

## 二、千问 TokenPlan 实际错误样例

```json
{
  "error": {
    "message": "Your token-plan 1-week quota has been exhausted. The quota will reset at 09-14 09:13:00 UTC.",
    "id": "bc2ff79e-7cc9-4836-a26c-17364a6da489",
    "type": "insufficient_quota",
    "code": "insufficient_quota"
  }
}
```

- `code=insufficient_quota` 已在 `BACKEND_CAPACITY_EXHAUSTED_CODES` 中，限额识别无需改动。
- message 中 `reset at 09-14 09:13:00 UTC` 为厂商给出的精确重置时间，优先采用。

## 三、状态模型

### 3.1 四态区分

| 状态 | 字段表现 | 恢复方式 |
|------|---------|---------|
| 正常 | `enabled=True`，`frozen_until=NULL` | — |
| 冻结（新增） | `enabled=True` + `frozen_until/frozen_at/freeze_reason` | 到 `frozen_until` 自动解冻 |
| 手动禁用 | `enabled=False`，无 `disabled_at` | 管理接口手动启用 |
| 自动禁用（现状保留） | `enabled=False` + `disabled_at/disable_reason` | 管理接口手动启用 |

**可用性判定**（选择/健康检查/刷新构建统一使用）：

```
enabled = True
AND (expires_at IS NULL OR expires_at > now)
AND (max_tokens IS NULL OR tokens_used < max_tokens)
AND (frozen_until IS NULL OR frozen_until <= now)   ← 新增
```

### 3.2 核心状态机

```
                 tokens_used >= max_tokens
                 或下游限额错误
                        │
   正常 ────────────────┤
    ▲                   ▼
    │            cycle == none ──→ 自动禁用（现状逻辑，手动恢复）
    │            cycle != none ──→ 冻结:
    │                               frozen_until = 错误解析时间 ?? quota_next_reset_at
    │                               （解析到厂商时间时同步覆盖 quota_next_reset_at）
    │                               内存移除，enabled 保持 True
    │                   │
    │      now >= frozen_until（刷新循环，≤10s 感知）
    └───────────────────┘
              解冻: 清 frozen_* 三字段
                    tokens_used = 0
                    quota_next_reset_at = frozen_until 逐周期前推至 > now
```

### 3.3 冻结触发决策链

```
触发限额（下游 quota 错误 / tokens_used >= max_tokens）
  ↓
cycle == none → _disable_node_api_key()（现状，完全不变）
  ↓ cycle != none
① 错误消息解析出重置时间 → frozen_until = 解析值
     且 quota_next_reset_at = 解析值（厂商确认，永久纠偏）
     freeze_reason = "下游限额(厂商指定重置时间): {message}"
② 解析失败 → frozen_until = quota_next_reset_at
     （防御：若 quota_next_reset_at <= now 说明数据滞后，先按 cycle 前推到未来再冻结）
     freeze_reason = "下游限额错误: {message}" 或 "已用Tokens({used})达到上限({max})"
```

### 3.4 跨周期例行重置（未冻结场景，修复问题 2）

刷新循环中与到期解冻合并为同一任务 `roll_node_api_key_quotas(session, now)`：

```python
# 每轮 _refresh_nodes_from_database() 开头执行，两类记录一次处理：
#
# A. 到期解冻:
#    UPDATE ... SET frozen_until=NULL, frozen_at=NULL, freeze_reason=NULL,
#                   tokens_used=0,
#                   quota_next_reset_at=<frozen_until 逐周期前推至 > now>
#    WHERE frozen_until IS NOT NULL AND frozen_until <= :now
#
# B. 未冻结但已跨周期:
#    查询 cycle != none AND enabled=True AND frozen_until IS NULL
#         AND quota_next_reset_at <= now 的记录（数量小，Python 内循环推进）
#    UPDATE ... SET tokens_used=0, quota_next_reset_at=<逐周期前推至 > now>
#    WHERE id=:id AND quota_next_reset_at=:old_value   ← CAS 防多实例重复
```

- B 类保证：无论用没用完，每到厂商重置点，计数自动归零、重置时间自动前推。
- 服务停机跨多个周期：逐周期 `+= cycle` 循环推进直到未来，一次补齐。
- CAS 条件更新保证多实例并发安全，无需分布式锁。
- 解冻/重置延迟 ≤ 一个刷新周期（10s），对日/周/月级重置完全够用。

### 3.5 与手动操作的交互

| 场景 | 行为 |
|------|------|
| upsert / update `enabled=True`（现有清空逻辑扩展） | 同时清 `frozen_until/frozen_at/freeze_reason`，`tokens_used=0`；**`quota_next_reset_at` 保留**（描述厂商节奏，不因手动启用而变；若已过去由刷新循环自动前推） |
| 冻结期间手动 `enabled=False` | 允许；冻结字段保留但不生效（enabled 优先），重新启用时一并清空 |
| 冻结期间密钥 `expires_at` 到期 | 正常过期过滤，不解冻 |
| 冻结期间修改周期配置 | 允许；`frozen_until` 按新配置重算 |
| 节点所有密钥都被冻结 | `api_keys` 为空 → 按现有逻辑回退 `Node.api_key`（TokenPlan 节点通常不配兜底 key，节点自然不可用，符合预期） |
| 厂商提前恢复额度 | 管理接口 `POST .../unfreeze` 手动提前解冻 |

## 四、数据库模型变更

### 4.1 `NodeApiKey` 新增字段

**文件**：`src/apiproxy/openaiproxy/services/database/models/node/model.py`

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `quota_reset_cycle` | `SAEnum(QuotaResetCycle)` | default=`none`, nullable=False, index | 限额重置周期 |
| `quota_next_reset_at` | `DateTime(timezone=True)` | nullable, index | 下一次配额重置时间；cycle != none 时 API 层必填且必须 > now |
| `frozen_until` | `DateTime(timezone=True)` | nullable, index | 冻结截止时间，NULL=未冻结 |
| `frozen_at` | `DateTime(timezone=True)` | nullable | 冻结发生时间 |
| `freeze_reason` | `Text` | nullable | 冻结原因（含下游错误原文） |

新增枚举（参照现有 `ProtocolType` 的 `SAEnum` + `values_callable` 写法）：

```python
class QuotaResetCycle(Enum):
    """配额重置周期。"""

    none = "none"        # 无周期（默认，限额即禁用，现状行为）
    daily = "daily"      # 每日重置（+1天）
    weekly = "weekly"    # 每周重置（+7天）
    monthly = "monthly"  # 每月重置（日历月加法，31号短月钳制到月末）
```

### 4.2 Alembic 迁移

- 执行 `make alembic-revision message="节点APIKEY限额冻结与周期自动重置"` 生成迁移脚本
- 更新 `src/apiproxy/openaiproxy/services/database/service.py` 中
  `last_version = "df9c5f6f4c79"` → 新 revision ID
- 迁移脚本使用 `batch_alter_table` 保持 SQLite 兼容（与现有迁移一致）

## 五、CRUD 层

**文件**：`src/apiproxy/openaiproxy/services/database/models/node/crud.py`

| 函数 | 说明 |
|------|------|
| `freeze_node_api_key(*, session, api_key_id, reason, frozen_at, frozen_until, next_reset_at=None)` | 新增。条件更新：仅当 `enabled=True` 时写入冻结字段（防止覆盖手动禁用）；`next_reset_at` 非空时同步覆盖 `quota_next_reset_at`（解析到厂商时间时） |
| `roll_node_api_key_quotas(*, session, now) -> int` | 新增。合并任务：A 到期解冻 + B 跨周期例行重置（CAS），返回处理行数 |
| `unfreeze_node_api_key(*, session, api_key_id)` | 新增。单个手动解冻：清冻结三字段 + `tokens_used=0` + `quota_next_reset_at` 前推（unfreeze 接口用） |
| `enable_node_api_key()`（修改） | 清空字段追加 `frozen_until/frozen_at/freeze_reason` |
| `select_active_node_api_keys()`（修改） | WHERE 追加 `(frozen_until IS NULL OR frozen_until <= now)` |

`models/node/__init__.py` 同步导出新函数与枚举。

## 六、转发服务改造

**文件**：`src/apiproxy/openaiproxy/services/nodeproxy/schemas.py`、`service.py`

1. `NodeApiKeyEntry` 新增字段：`quota_reset_cycle`、`quota_next_reset_at`
   （`frozen_until` 不需要——冻结条目已从内存移除）
2. `_build_node_api_key_entries()`：过滤条件追加 `frozen_until > now` 的记录跳过
   （防御性，正常情况下 roll 任务已在前一步处理）
3. 新增静态方法 `_parse_reset_time_from_error(payload, error_message) -> Optional[datetime]`，
   按顺序匹配，命中即返回（统一转为 `current_timezone()` 的 aware datetime）：

   | 优先级 | 模式 | 正则要点 |
   |-------|------|---------|
   | 1 | 千问格式 `reset at MM-DD HH:MM:SS UTC` | `reset\s+at\s+(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s*UTC` |
   | 2 | 通用 ISO `reset(s) at/on YYYY-MM-DDTHH:MM[:SS][Z\|±HH:MM]` | `reset(?:s|ting)?\s+(?:at|on)\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)` |
   | 3 | `Retry-After` 响应头（若上下文可拿到 headers） | 秒数或 HTTP-date，`frozen_until = now + seconds` |

   - 千问格式无年份：以当前年份构造候选；若候选 < now − 1 天（说明是明年），年份 +1
   - 合法性校验（任一不满足视为解析失败，走决策链②）：
     - 解析结果必须 > now（≤ now 说明额度应已恢复，错误可能是滞后状态）
     - 解析结果 ≤ now + 40 天（防脏数据把 key 冻死过久）
4. 新增静态方法 `_advance_reset_time(current, cycle, now) -> datetime`：
   逐周期前推直到 > now（daily +1天 / weekly +7天 / monthly 日历月加法 + 月末钳制）
5. 新增 `_freeze_node_api_key(*, entry, reason, payload, error_message)`：
   按 3.3 决策链计算 `frozen_until` → DB 落盘（`freeze_node_api_key`）→
   `_remove_api_key_from_memory()`（复用现有方法）→ 日志，结构与 `_disable_node_api_key()` 对称
6. `_post_process_api_key_usage()`：两处 `_disable_node_api_key` 调用点改为按
   `entry.quota_reset_cycle` 分支（none → 禁用；其他 → 冻结）
7. `_refresh_nodes_from_database()` 开头调用 `roll_node_api_key_quotas()`，
   有变更时使相关节点 `config_version` 失效
   （参照 `restore_node_api_key_availability` 中 `meta.config_version = ''` 的做法，确保条目重建）
8. `BACKEND_CAPACITY_EXHAUSTED_HINTS` 追加 `'quota has been exhausted'`、`'token-plan'`，
   覆盖仅拿到 message 文本（无结构化 code）时的识别
9. 健康检查 `_select_health_check_api_key()`：无需改动（冻结密钥已被过滤出 `api_keys`）

## 七、管理 API 接口

**文件**：`src/apiproxy/openaiproxy/api/schemas.py`、`api/node_apikey.py`

1. `CreateNodeApiKey` / `UpdateNodeApiKey` 新增：
   - `quota_reset_cycle: Optional[QuotaResetCycle] = None`
   - `quota_next_reset_at: Optional[datetime] = None`
2. **校验规则**（API 层，违反返回 400）：
   - 创建时 `cycle != none` → `quota_next_reset_at` 必填且必须 > now
   - 更新时若结果状态 `cycle != none` 且库中 `quota_next_reset_at` 为 NULL 或已过去
     → 要求本次传入新值
   - `cycle == none` 时忽略 `quota_next_reset_at`
3. `NodeApiKeyResponse` 新增：`quota_reset_cycle`、`quota_next_reset_at`、
   `frozen_until`、`frozen_at`、`freeze_reason`
4. upsert / update 的"重新启用清空"逻辑扩展：清冻结三字段 + `tokens_used=0`；
   `quota_next_reset_at` 保留（见 3.5）
5. 新增接口 `POST /nodes/{node_id}/apikeys/{key_id}/unfreeze`：手动提前解冻
   （`unfreeze_node_api_key` + `restore_node_api_key_availability()` 触发刷新），
   用于厂商提前恢复额度等场景
6. 列表接口 `GET /nodes/{node_id}/apikeys` 新增可选参数 `frozen: Optional[bool]`
   过滤冻结中/未冻结

## 八、前端（可选批次）

`html/dash.html` 密钥列表：

- 状态列区分：正常 / 冻结中（显示 `frozen_until` 倒计时）/ 已禁用
- 编辑表单增加：重置周期（none/daily/weekly/monthly）+ 下一次重置时间（周期非 none 时必填）
- 冻结行提供"立即解冻"按钮（调 unfreeze 接口）
- 显示 `quota_next_reset_at`，便于核对厂商节奏

## 九、向后兼容

| 场景 | 行为 |
|------|------|
| 存量密钥（`quota_reset_cycle=none`） | 限额后照旧禁用，行为零变化 |
| 各厂商普通 API key | 不配置周期即可，不受影响 |
| `frozen_until` / `quota_next_reset_at` 旧数据 | 全部为 NULL，视为未冻结、无周期 |
| 旧接口消费方 | Response 仅增字段，无破坏 |
| `Node.api_key` 兜底 | 保留不变 |

## 十、执行批次

```
批次一（模型 + 枚举 + Alembic 迁移 + last_version）
  ↓
批次二（CRUD：freeze / roll / unfreeze / enable 扩展 / active 查询扩展）
  ↓
批次三（转发服务：Entry 字段、解析器、冻结分支、roll 挂刷新循环）
  +  批次四（管理 API：schemas、校验、清空逻辑、unfreeze 接口）  ← 可并行
  ↓
批次五（单元测试 + 集成测试）
  ↓
批次六（前端 dash.html，可选）
```

### 批次一：数据库模型层
- [ ] `models/node/model.py` 新增 `QuotaResetCycle` 枚举
- [ ] `NodeApiKey` 新增 5 字段（quota_reset_cycle / quota_next_reset_at / frozen_until / frozen_at / freeze_reason）
- [ ] 执行 `make alembic-revision message="节点APIKEY限额冻结与周期自动重置"`
- [ ] 更新 `services/database/service.py` 中 `last_version`

### 批次二：CRUD 层
- [ ] 新增 `freeze_node_api_key()`（条件更新，仅 enabled=True 生效）
- [ ] 新增 `roll_node_api_key_quotas()`（A 到期解冻 + B 跨周期例行重置，CAS）
- [ ] 新增 `unfreeze_node_api_key()`（单个手动解冻）
- [ ] 修改 `enable_node_api_key()`（清空字段追加冻结三字段）
- [ ] 修改 `select_active_node_api_keys()`（过滤条件追加冻结判断）
- [ ] `models/node/__init__.py` 导出新函数与枚举

### 批次三：转发服务改造
- [ ] `nodeproxy/schemas.py` `NodeApiKeyEntry` 新增 quota_reset_cycle / quota_next_reset_at
- [ ] `_build_node_api_key_entries()` 过滤条件追加冻结判断
- [ ] 新增 `_parse_reset_time_from_error()`（千问格式 / 通用 ISO / Retry-After + 合法性校验）
- [ ] 新增 `_advance_reset_time()`（逐周期前推，monthly 月末钳制）
- [ ] 新增 `_freeze_node_api_key()`（决策链 + DB 落盘 + 内存移除）
- [ ] `_post_process_api_key_usage()` 两处禁用调用点改为周期分支
- [ ] `_refresh_nodes_from_database()` 开头挂 `roll_node_api_key_quotas()` + config_version 失效
- [ ] `BACKEND_CAPACITY_EXHAUSTED_HINTS` 追加千问关键词

### 批次四：管理 API 接口
- [ ] `api/schemas.py` Create/Update/Response 新增字段
- [ ] API 层校验（cycle != none 时 next_reset_at 必填且 > now）
- [ ] upsert / update 重新启用清空逻辑扩展（清冻结三字段）
- [ ] 新增 `POST /nodes/{node_id}/apikeys/{key_id}/unfreeze` 接口
- [ ] 列表接口新增 `frozen` 过滤参数

### 批次五：测试
- [ ] 单元：`_parse_reset_time_from_error`（千问格式、年份推断、UTC→本地时区、跨年 12月收到01月重置时间、非法值回退：≤now / >40天 / 畸形消息）
- [ ] 单元：`_advance_reset_time`（daily/weekly/monthly、跨多周期一次补齐、31号月末钳制）
- [ ] 单元：冻结决策链（解析值优先 / next_reset_at 兜底 / next_reset_at 已过去先前推 / cycle=none 走禁用）
- [ ] 单元：`freeze_node_api_key` 条件更新（enabled=False 时不覆盖）
- [ ] 单元：`roll_node_api_key_quotas` A 类（解冻 + tokens 归零 + next_reset_at 前推）
- [ ] 单元：`roll_node_api_key_quotas` B 类（未冻结未用完、next_reset_at 已过 → tokens 归零、时间前推；CAS 并发只命中一次）
- [ ] 集成：模拟千问 429 响应 → 冻结至解析时间 → 时间越过 → 刷新循环自动解冻 → tokens=0 → 重新可选
- [ ] 集成：手动禁用与冻结互不干扰；冻结期间手动禁用 → 到期不自动启用
- [ ] 集成：upsert 重新启用清空冻结字段、保留 next_reset_at
- [ ] 集成：unfreeze 接口全流程
- [ ] 集成：cycle=none 密钥限额 → 仍走禁用（回归验证）

### 批次六：前端（可选）
- [ ] `html/dash.html` 状态列区分 正常/冻结中(倒计时)/已禁用
- [ ] 编辑表单增加 重置周期 + 下一次重置时间（联动必填校验）
- [ ] 冻结行"立即解冻"按钮

## 十一、注意事项

1. **时区**：`frozen_until`、`quota_next_reset_at` 全部 timezone-aware，统一
   `current_timezone()`；naive 时间入库前补时区
2. **解冻与 increment 并发**：解冻/重置用条件 UPDATE（CAS），与
   `increment_node_api_key_tokens_used` 的原子累加不冲突；极端交错下最多多计一次请求的
   tokens，可接受
3. **freeze_reason 脱敏**：只存下游错误消息，不存密钥明文（与现有 `disable_reason` 一致）
4. **拼写**：项目中 `avaiaible` 等既有拼写错误保持不动
5. **monthly 前推**：使用日历月加法（1月31日 → 2月28/29日），不用固定 30 天
6. **千问 TokenPlan 配置示例**：

   | 字段 | 值 |
   |------|-----|
   | `max_tokens` | 周额度 tokens 数（可选，配了更早触发本地冻结） |
   | `quota_reset_cycle` | `weekly` |
   | `quota_next_reset_at` | 厂商控制台显示的下次重置时间（必填） |

   运行效果：耗尽时优先用厂商返回的 `reset at 09-14 09:13:00 UTC` 精确冻结并纠偏
   `next_reset_at`；错误消息没带时间时按用户填写的 `next_reset_at` 冻结；到点自动解冻、
   tokens 归零、重置时间自动 +7 天前推；本周期没用完也会在重置点自动归零计数。
   多 key 各自配置，到期各自自动恢复，全程无人工干预。
7. **后续扩展（本次不做）**：冻结前 N 小时预告警；管理界面批量设置多 key 的重置时间
