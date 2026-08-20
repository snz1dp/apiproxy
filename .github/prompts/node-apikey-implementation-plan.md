# 节点独立 APIKEY 记录 — 实施计划

> 状态：待实施
> 创建时间：2026-08-13
> 最后更新：2026-08-13

## 一、需求概述

为节点（Node）添加独立的 APIKEY 记录，实现：

1. 同一个节点可配置**多个 APIKEY 同时使用**（如 CodePlan 模型服务节点）
2. 每个 APIKEY 是独立记录，支持**到期时间**、**启用/禁用**、**优先级权重**、**最大使用 Tokens 数**
3. 通过管理接口设置 APIKEY 时采用 **upsert 语义**：不存在就新建，已存在但被禁用了就重新启用
4. **请求日志**中记录本次请求实际使用了哪个 APIKEY
5. APIKEY 选择采用**优先级权重百分比**策略：按各 APIKEY 的 priority 值占总和的比例进行加权随机选择
6. **自动禁用机制**：下游返回限额错误（429 等）或已用 Tokens 达到上限时，所有实例禁用该 APIKEY，记录禁用时间与原因
7. **重新设置清空**：通过管理接口重新启用/设置 APIKEY 时，清空禁用时间、禁用原因、已用 Tokens

## 二、APIKEY 选择策略（优先级加权）

### 2.1 核心算法

每个 NodeApiKey 记录有一个 `priority` 字段（正整数，默认 1）。选择时：

```
可用密钥列表 = [key for key in node_apikeys
                if key.enabled
                and (key.expires_at is None or key.expires_at > now)
                and (key.max_tokens is None or key.tokens_used < key.max_tokens)]
总权重 = sum(key.priority for key in 可用密钥列表)
每个密钥的选中概率 = key.priority / 总权重
```

### 2.2 示例

| APIKEY | priority | 选中概率 |
|--------|----------|---------|
| key-A  | 3        | 3/6 = 50% |
| key-B  | 2        | 2/6 = 33.3% |
| key-C  | 1        | 1/6 = 16.7% |

### 2.3 边界情况

- 所有密钥 priority 相同 → 退化为均匀随机
- 只有一个可用密钥 → 直接使用该密钥
- 所有密钥都过期/禁用/额度耗尽 → 回退使用 `Node.api_key`（向后兼容）
- priority 为 0 或负数 → 视为不可选（等效于禁用）
- `max_tokens` 为 NULL → 不限制用量
- `tokens_used >= max_tokens` → 该密钥不参与选择（等效于额度耗尽）

## 三、自动禁用与重新启用机制

### 3.1 触发自动禁用的条件

| 触发条件 | 检测时机 | disable_reason 示例 |
|---------|---------|-------------------|
| 下游返回 HTTP 429 / 限额错误 | 请求响应后 | `"下游限额错误: rate limit exceeded"` |
| 下游返回包含 quota/rate limit 关键词的错误 | 请求响应后 | `"下游限额错误: {error_message}"` |
| `tokens_used >= max_tokens` | 请求完成更新 tokens_used 后 | `"已用Tokens({tokens_used})达到上限({max_tokens})"` |

### 3.2 自动禁用流程

```
请求完成/收到限额错误
  ↓
1. 更新数据库：enabled=False, disabled_at=now, disable_reason="..."
  ↓
2. 更新本实例内存：从 Status.api_keys 中移除该条目
  ↓
3. 跨实例同步：通知所有代理实例刷新该节点的 APIKEY 列表
   （复用现有 restore_backend_node_availability() 的跨实例通知机制）
```

### 3.3 限额错误识别

在 `stream_generate()` / `generate()` 的响应处理中，检测以下情况：
- HTTP 状态码 429（Too Many Requests）
- HTTP 状态码 402（Payment Required）
- 响应体中包含 `rate_limit`、`quota`、`insufficient`、`exceeded` 等关键词
- Anthropic 协议的 `overloaded_error` 类型

识别为限额错误后，调用 `_disable_node_api_key()` 执行自动禁用。

### 3.4 Tokens 用量累计

- 每次请求完成后，将该请求的 `total_tokens`（来自响应中的 usage 字段）累加到 `NodeApiKey.tokens_used`
- 累加后检查是否 `tokens_used >= max_tokens`，若达到则触发自动禁用
- 累加操作使用数据库原子更新（`UPDATE ... SET tokens_used = tokens_used + :delta`），避免并发竞争

### 3.5 重新设置清空

通过管理接口（upsert 创建 / update 更新）重新启用 APIKEY 时：
- `enabled` → `True`
- `disabled_at` → `NULL`
- `disable_reason` → `NULL`
- `tokens_used` → `0`（重置用量计数）
- 触发跨实例同步，让所有实例重新加载该 APIKEY

## 四、数据库模型变更

### 4.1 新增 `NodeApiKey` 模型

**文件**：`src/apiproxy/openaiproxy/services/database/models/node/model.py`
**表名**：`openaiapi_node_apikeys`

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | UUID | PK, default=uuid4 | 记录ID |
| `node_id` | UUID | FK→openaiapi_nodes.id, index | 关联节点 |
| `name` | Text | nullable | 密钥名称/备注 |
| `api_key` | Text | nullable=False | 加密存储的API密钥 |
| `api_key_hash` | String(64) | index | SHA256哈希，用于 upsert 查找 |
| `priority` | Integer | default=1, nullable=False | 优先级权重（正整数，越大越优先） |
| `max_tokens` | BigInteger | nullable | 最大使用Tokens数，NULL表示不限制 |
| `tokens_used` | BigInteger | default=0, nullable=False | 已使用Tokens数 |
| `enabled` | Boolean | default=True | 是否启用 |
| `expires_at` | DateTime(timezone=True) | nullable | 到期时间，NULL表示永不过期 |
| `disabled_at` | DateTime(timezone=True) | nullable | 自动禁用时间（手动禁用不记录） |
| `disable_reason` | Text | nullable | 自动禁用原因 |
| `created_at` | DateTime(timezone=True) | default=now | 创建时间 |
| `updated_at` | DateTime(timezone=True) | default=now | 更新时间 |

**唯一约束**：`(node_id, api_key_hash)` — 同一节点下同一密钥不重复

### 4.2 `ProxyNodeStatusLog` 新增字段

**文件**：`src/apiproxy/openaiproxy/services/database/models/proxy/model.py`

| 新增字段 | 类型 | 约束 | 说明 |
|---------|------|------|------|
| `node_api_key_id` | UUID | nullable, index | 本次请求使用的节点API密钥记录ID |

### 4.3 Alembic 迁移

- 执行 `make alembic-revision message="节点独立APIKEY记录与优先级权重"` 生成迁移脚本
- 更新 `src/apiproxy/openaiproxy/services/database/service.py` 中 `last_version` 为新 revision ID
- 当前 last_version = `"cb04f8f0f5a2"`

## 五、CRUD 层

### 5.1 新增 NodeApiKey CRUD 函数

**文件**：`src/apiproxy/openaiproxy/services/database/models/node/crud.py`

| 函数 | 说明 |
|------|------|
| `create_node_api_key_record(*, session, payload: dict) -> NodeApiKey` | 创建节点API密钥记录 |
| `select_node_api_key_by_id(*, session, api_key_id: UUID) -> Optional[NodeApiKey]` | 按ID查询 |
| `select_node_api_keys_by_node_id(*, session, node_id: UUID, enabled: Optional[bool], offset, limit) -> list[NodeApiKey]` | 按节点ID查询列表（分页、enabled过滤） |
| `count_node_api_keys_by_node_id(*, session, node_id: UUID, enabled: Optional[bool]) -> int` | 计数 |
| `select_node_api_key_by_hash(*, session, node_id: UUID, api_key_hash: str) -> Optional[NodeApiKey]` | 按 node_id + hash 查找（upsert 用） |
| `update_node_api_key_record(*, session, record: NodeApiKey, update_payload: dict, updated_at: datetime) -> NodeApiKey` | 更新记录 |
| `delete_node_api_key_record(*, session, record: NodeApiKey) -> None` | 删除记录 |
| `select_active_node_api_keys(*, session, node_id: UUID) -> list[NodeApiKey]` | 查询节点下所有 enabled=True 且未过期且未超额的密钥（转发用） |
| `increment_node_api_key_tokens_used(*, session, api_key_id: UUID, delta: int) -> None` | 原子累加 tokens_used（`SET tokens_used = tokens_used + :delta`） |
| `disable_node_api_key(*, session, api_key_id: UUID, reason: str, disabled_at: datetime) -> None` | 自动禁用：enabled=False + disabled_at + disable_reason |
| `enable_node_api_key(*, session, api_key_id: UUID) -> None` | 重新启用：enabled=True + 清空 disabled_at/disable_reason/tokens_used |

### 5.2 修改 ProxyNodeStatusLog CRUD

**文件**：`src/apiproxy/openaiproxy/services/database/models/proxy/crud.py`

- `create_proxy_node_status_log_entry()` 增加 `node_api_key_id: Optional[UUID]` 参数
- `update_proxy_node_status_log_entry()` 支持更新 `node_api_key_id`

## 六、请求转发服务改造

### 6.1 运行时 Schema 变更

**文件**：`src/apiproxy/openaiproxy/services/nodeproxy/schemas.py`

`Status` 模型新增：

```python
class NodeApiKeyEntry(BaseModel):
    """节点API密钥运行时条目"""
    api_key_id: UUID          # 记录ID（写入日志用）
    api_key: str              # 解密后的明文密钥
    priority: int = 1         # 优先级权重
    max_tokens: Optional[int] = None   # 最大使用Tokens数
    tokens_used: int = 0      # 已使用Tokens数

# Status 中新增字段
api_keys: List[NodeApiKeyEntry] = []  # 可用API密钥列表
```

保留原有 `api_key: Optional[str]` 作为默认密钥（向后兼容）。

### 6.2 节点刷新逻辑改造

**文件**：`src/apiproxy/openaiproxy/services/nodeproxy/service.py`

`_refresh_nodes_from_database()` 改造：
- 加载节点时，同时查询该节点的 `NodeApiKey` 记录（enabled=True 且 expires_at 未过期或为 NULL 且 tokens_used < max_tokens 或 max_tokens 为 NULL）
- 解密后构建 `List[NodeApiKeyEntry]` 填入 `Status.api_keys`
- 如果 `NodeApiKey` 无可用记录，回退使用 `Node.api_key`（向后兼容）

### 6.3 APIKEY 加权选择

新增方法 `_select_api_key(status: Status) -> Optional[NodeApiKeyEntry]`：

```python
def _select_api_key(self, status: Status) -> Optional[NodeApiKeyEntry]:
    """按优先级权重加权随机选择一个API密钥"""
    candidates = [
        entry for entry in status.api_keys
        if entry.priority > 0
        and (entry.max_tokens is None or entry.tokens_used < entry.max_tokens)
    ]
    if not candidates:
        return None
    total_weight = sum(entry.priority for entry in candidates)
    random_value = random.uniform(0, total_weight)
    cumulative = 0
    for entry in candidates:
        cumulative += entry.priority
        if random_value <= cumulative:
            return entry
    return candidates[-1]  # 浮点精度兜底
```

### 6.4 请求转发链路改造

`get_node_url()` 返回值扩展：
- 当前返回 `Optional[str]`（节点URL）
- 改为同时返回选中的 `(api_key, node_api_key_id)`，可通过返回 NamedTuple 或修改调用方

`stream_generate()` / `generate()` 调用处：
- 传入选中的 api_key 构建 Header
- 请求完成后将 `node_api_key_id` 写入 `ProxyNodeStatusLog`

### 6.5 请求后处理（Tokens 累计 + 限额检测）

请求完成后新增后处理逻辑：

```python
async def _post_process_api_key_usage(self, *, api_key_entry, response_tokens, http_status, error_message):
    """请求后处理：累计Tokens、检测限额、触发自动禁用"""
    # 1. 原子累加 tokens_used
    await increment_node_api_key_tokens_used(session, api_key_entry.api_key_id, response_tokens)

    # 2. 检测限额错误（HTTP 429/402 或错误信息包含限额关键词）
    if _is_rate_limit_error(http_status, error_message):
        await self._disable_node_api_key(
            api_key_id=api_key_entry.api_key_id,
            reason=f"下游限额错误: {error_message or f'HTTP {http_status}'}"
        )
        return

    # 3. 检测 Tokens 用量是否达到上限
    if api_key_entry.max_tokens is not None:
        new_used = api_key_entry.tokens_used + response_tokens
        if new_used >= api_key_entry.max_tokens:
            await self._disable_node_api_key(
                api_key_id=api_key_entry.api_key_id,
                reason=f"已用Tokens({new_used})达到上限({api_key_entry.max_tokens})"
            )
```

### 6.6 自动禁用方法

```python
async def _disable_node_api_key(self, *, api_key_id: UUID, reason: str):
    """自动禁用APIKEY并跨实例同步"""
    # 1. 数据库更新：enabled=False, disabled_at=now, disable_reason=reason
    await disable_node_api_key(session, api_key_id, reason=reason, disabled_at=datetime.now(timezone.utc))

    # 2. 本实例内存：从所有节点的 Status.api_keys 中移除该条目
    self._remove_api_key_from_memory(api_key_id)

    # 3. 跨实例同步：通知其他代理实例刷新
    #    复用现有 restore_backend_node_availability() 的通知机制
    self._notify_instances_refresh_api_keys(node_url)

    logger.warning(f"节点API密钥 {api_key_id} 已自动禁用: {reason}")
```

### 6.7 限额错误识别

```python
_RATE_LIMIT_KEYWORDS = frozenset({
    'rate_limit', 'rate limit', 'ratelimit',
    'quota', 'insufficient', 'exceeded',
    'too many requests', 'overloaded',
    'billing', 'payment', 'limit reached',
})

def _is_rate_limit_error(http_status: Optional[int], error_message: Optional[str]) -> bool:
    """判断是否为限额/配额错误"""
    if http_status in (429, 402):
        return True
    if error_message:
        lower_message = error_message.lower()
        return any(keyword in lower_message for keyword in _RATE_LIMIT_KEYWORDS)
    return False
```

### 6.8 健康检查

`_check_single_node()` 中：
- 如果节点有多个 APIKEY，使用 priority 最高的一个进行检查
- 健康检查不累计 tokens_used

## 七、管理 API 接口

### 7.1 新增 Schema

**文件**：`src/apiproxy/openaiproxy/api/schemas.py`

```python
class CreateNodeApiKey(BaseModel):
    """创建/Upsert节点API密钥"""
    api_key: str                          # 必填，明文密钥
    name: Optional[str] = None            # 密钥名称/备注
    priority: int = 1                     # 优先级权重，正整数
    max_tokens: Optional[int] = None      # 最大使用Tokens数，NULL不限制
    expires_at: Optional[datetime] = None # 到期时间
    enabled: Optional[bool] = True        # 是否启用

class UpdateNodeApiKey(BaseModel):
    """更新节点API密钥"""
    api_key: Optional[str] = None         # 新密钥值（可选）
    name: Optional[str] = None            # 名称
    priority: Optional[int] = None        # 优先级权重
    max_tokens: Optional[int] = None      # 最大使用Tokens数
    expires_at: Optional[datetime] = None # 到期时间
    enabled: Optional[bool] = None        # 启用状态

class NodeApiKeyResponse(BaseModel):
    """节点API密钥响应"""
    id: UUID
    node_id: UUID
    name: Optional[str]
    api_key: Optional[str] = None         # 解密后明文（管理接口返回）
    api_key_hash: str
    priority: int
    max_tokens: Optional[int]             # 最大使用Tokens数
    tokens_used: int                      # 已使用Tokens数
    enabled: bool
    expires_at: Optional[datetime]
    disabled_at: Optional[datetime]       # 自动禁用时间
    disable_reason: Optional[str]         # 自动禁用原因
    created_at: datetime
    updated_at: datetime
```

### 7.2 新增路由文件

**文件**：`src/apiproxy/openaiproxy/api/node_apikey_manager.py`（新建）
**Tag**：`节点API密钥管理`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/nodes/{node_id}/apikeys` | 获取节点的API密钥列表（分页，支持 enabled 过滤） |
| POST | `/nodes/{node_id}/apikeys` | **Upsert**：按 api_key 查找，不存在则新建，存在且被禁用则启用 |
| GET | `/nodes/{node_id}/apikeys/{apikey_id}` | 获取指定API密钥详情 |
| POST | `/nodes/{node_id}/apikeys/{apikey_id}` | 更新API密钥（名称、优先级、额度、到期时间、启用状态、密钥值） |
| DELETE | `/nodes/{node_id}/apikeys/{apikey_id}` | 删除API密钥（需先禁用） |

### 7.3 Upsert 核心逻辑（POST 创建接口）

```python
async def create_or_enable_node_api_key(node_id, input: CreateNodeApiKey, *, session):
    """
    Upsert 语义：
    1. 计算 api_key_hash = sha256(input.api_key.strip())
    2. 按 (node_id, api_key_hash) 查找记录
    3. 不存在 → 加密 api_key → 创建新记录 → 返回
    4. 已存在且 enabled=True → 更新 priority/max_tokens/expires_at/name（如果传了）→ 返回（幂等）
    5. 已存在且 enabled=False → 重新启用：
       - enabled=True
       - disabled_at=None（清空禁用时间）
       - disable_reason=None（清空禁用原因）
       - tokens_used=0（重置已用Tokens）
       - 更新 priority/max_tokens/expires_at/name（如果传了）
       → 触发跨实例同步 → 返回
    """
```

### 7.4 更新接口中的重新启用逻辑

```python
async def update_node_api_key(node_id, apikey_id, update: UpdateNodeApiKey, *, session):
    """
    更新逻辑：
    1. 查找记录，不存在则 404
    2. 如果 update.enabled == True 且当前 enabled == False：
       - 清空 disabled_at、disable_reason
       - 重置 tokens_used = 0
       - 触发跨实例同步
    3. 如果 update.enabled == False 且当前 enabled == True：
       - 手动禁用（不记录 disabled_at 和 disable_reason，区别于自动禁用）
    4. 更新其他传入的字段
    """
```

### 7.5 注册路由

在 API 路由注册处（`api/__init__.py` 或主 app 文件）引入新路由。

## 八、向后兼容策略

| 场景 | 处理方式 |
|------|---------|
| 节点只有 `Node.api_key`，无 `NodeApiKey` 记录 | 回退使用 `Node.api_key`，行为不变 |
| 节点有 `NodeApiKey` 记录 | 优先使用 `NodeApiKey` 中 enabled 且未过期且未超额的密钥，按 priority 加权选择 |
| `Node.api_key` 字段 | 保留不删除，作为默认/兜底密钥 |
| 请求日志 `node_api_key_id` | nullable，旧日志为 NULL |
| 已有节点创建/更新接口中的 `api_key` 参数 | 保持不变，写入 `Node.api_key`（默认密钥） |
| `max_tokens` 为 NULL | 不限制用量，tokens_used 仍累计但不触发禁用 |

## 九、执行批次与顺序

```
批次一（数据库模型 + Alembic迁移）
  ↓
批次二（CRUD 层）
  ↓
批次三（转发服务改造） + 批次四（管理API接口）  ← 可并行
  ↓
批次五（单元测试 + 集成测试）
```

### 批次一：数据库模型层
- [ ] `models/node/model.py` 新增 `NodeApiKey` 模型（含 max_tokens/tokens_used/disabled_at/disable_reason）
- [ ] `models/proxy/model.py` `ProxyNodeStatusLog` 新增 `node_api_key_id` 字段
- [ ] `models/__init__.py` 导出新模型
- [ ] 执行 `make alembic-revision message="节点独立APIKEY记录与优先级权重"`
- [ ] 更新 `service.py` 中 `last_version`

### 批次二：CRUD 层
- [ ] `models/node/crud.py` 新增 NodeApiKey CRUD 函数（含 increment/disable/enable）
- [ ] `models/proxy/crud.py` 修改日志创建/更新函数支持 `node_api_key_id`
- [ ] `models/node/__init__.py` 导出新函数

### 批次三：转发服务改造
- [ ] `nodeproxy/schemas.py` 新增 `NodeApiKeyEntry`（含 max_tokens/tokens_used），`Status` 新增 `api_keys` 字段
- [ ] `nodeproxy/service.py` 改造 `_refresh_nodes_from_database()` 加载多密钥
- [ ] `nodeproxy/service.py` 新增 `_select_api_key()` 加权选择方法（含 max_tokens 过滤）
- [ ] `nodeproxy/service.py` 改造 `get_node_url()` 返回选中的密钥信息
- [ ] `nodeproxy/service.py` 改造 `stream_generate()` / `generate()` 传递 `node_api_key_id`
- [ ] `nodeproxy/service.py` 新增 `_post_process_api_key_usage()` 请求后处理
- [ ] `nodeproxy/service.py` 新增 `_disable_node_api_key()` 自动禁用 + 跨实例同步
- [ ] `nodeproxy/service.py` 新增 `_is_rate_limit_error()` 限额错误识别
- [ ] 请求日志写入 `node_api_key_id`

### 批次四：管理 API 接口
- [ ] `api/schemas.py` 新增 CreateNodeApiKey / UpdateNodeApiKey / NodeApiKeyResponse
- [ ] 新建 `api/node_apikey_manager.py` 实现 5 个接口
- [ ] upsert 逻辑含重新启用清空（disabled_at/disable_reason/tokens_used）
- [ ] 注册路由到主 app

### 批次五：测试
- [ ] 单元测试：NodeApiKey CRUD、upsert 逻辑、过期判断
- [ ] 单元测试：加权选择算法（priority 分布验证、max_tokens 过滤）
- [ ] 单元测试：限额错误识别（_is_rate_limit_error）
- [ ] 单元测试：自动禁用流程（禁用→记录原因→跨实例通知）
- [ ] 单元测试：重新启用清空（disabled_at/disable_reason/tokens_used 归零）
- [ ] 集成测试：管理 API 全流程（创建→查询→更新→删除）
- [ ] 集成测试：请求转发时 APIKEY 加权选择 + Tokens 累计 + 日志记录验证
- [ ] 集成测试：限额错误触发自动禁用 → 重新设置后恢复

## 十、注意事项

1. **加密一致性**：NodeApiKey 的 api_key 加密/解密复用现有 `encrypt_api_key()` / `decrypt_api_key()` 工具函数
2. **哈希算法**：`api_key_hash = hashlib.sha256(api_key.strip().encode()).hexdigest()`，与北向 ApiKey v2 方案一致
3. **priority 校验**：API 层校验 priority >= 0，0 表示不参与选择（等效禁用但保留记录）
4. **max_tokens 校验**：API 层校验 max_tokens > 0 或为 NULL，不允许 0 或负数
5. **并发安全**：加权选择在内存中完成，无锁竞争；tokens_used 累加使用数据库原子操作；upsert 依赖唯一约束防并发重复
6. **日志脱敏**：请求日志只记录 `node_api_key_id`（UUID），不记录密钥明文
7. **拼写注意**：项目中 `avaiaible`（available 的拼写错误）已广泛使用，不要修正，保持一致
8. **手动禁用 vs 自动禁用**：手动禁用（管理接口设 enabled=False）不记录 disabled_at/disable_reason；自动禁用才记录，便于区分禁用来源
9. **跨实例同步**：自动禁用和重新启用都需要触发跨实例通知，确保所有代理实例及时感知状态变化
10. **tokens_used 精度**：tokens_used 为累计值，重新启用时归零；不做周期性重置，仅通过重新设置来清空
