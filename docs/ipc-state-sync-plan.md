# Young Router IPC 状态同步改造：一次性实施规格

状态：待实施（本文档为规格，未改动任何代码）
交付形态：**一个批次、一次合并、一次发布**。不设灰度阶段、不设"先止血后架构"，所有改动作为同一个不变量系统同时落地。
范围：`young_router/core/`、`rn/packages/shared/`、`rn/apps/{macos,windows}/src/native/`、`rn/scripts/`、`tests/`

---

## 1. 目标与完成定义

### 1.1 用户可见目标

新增一个 model / provider / route 后：

- 界面在 **50 ms 内**出现该行（乐观更新），Core 确认后以权威数据校正；
- 无论后端正在进行 probe、WebDAV 同步、relay 登录还是服务重启，该操作都在 **200 ms 内**返回；
- 该操作**不触发整棵组件树重渲染**，滚动与输入不中断。

### 1.2 完成定义（Definition of Done）

以下各项**同时**成立才算完成：

1. 一次快照载荷 < 64 KB（空配置）、< 72 KB（60 条 route）——由测试断言。
2. 一次 `dispatch` 的响应时间不含投影成本——由注入延迟的测试断言。
3. 事件通道默认只推增量；全量仅在首连与断档时发生。
4. `add` 在慢操作（probe / sync / login / restart）并发进行时仍在 200 ms 内返回。
5. ≥ 3 个窗口同时在线时，一次 `add` 只产生 1 次实体级事件、0 次全量刷新。
6. `pnpm run check:macos` 与 `pytest tests/` 全绿；新增的护栏测试逐条生效。

### 1.3 不设阶段的原因

原来按"先止血、再架构"分阶段，问题在于：止血项（去掉重复载荷、emit 出锁）与架构项（增量事件、分域锁）共享同一批契约与同一条测试面。分两次做意味着**契约改两遍、原生 spec 生成两遍、契约测试维护两遍**，且中间态（有增量协议但仍是全量投影）本身就是一段没人会长期维护的死代码。因此一次做完，中间的依赖顺序只用于提交内部排序，不作为发布切分。

---

## 2. 现状实测

在本机用真实 `CoreStore.with_default_domains` + 临时目录测得：

| 指标 | 数值 |
| --- | --- |
| `snapshot` 载荷（空配置） | 106,220 B |
| `snapshot` 载荷（60 条 route） | 112,440 B |
| `domains.runtime` 占比 | 88–91% |
| `runtime.fields` / `runtime.settings` | 同一份 47,615 B 列表，JSON 里出现两次 |
| 冷投影（清 cache） | 6–9 ms（本机）；`service.py:2057-2059` 注释记录真实环境 270–375 ms |
| 缓存命中的 `snapshot` | 1.1 ms |
| 每订阅者 `deepcopy` | 0.66 ms |
| 每订阅者 JSON 编码 | 0.21 ms |
| 本机 loopback 一次 `snapshot` 往返 | 13.48 ms |
| `_persist_metadata()` | 0.35 ms |

**结论：传输只占 13 ms，用户感知的几百毫秒到数秒来自"每次 mutation 推全量 + 该投影发生在全局写锁内 + 客户端整树重渲染"。**

### 2.1 三个根因（代码位置）

**根因一：`_emit()` 在 `_mutation_scope()` 内。**
`self._emit()` 在 `service.py` 共 **27 处**，其中 **16 处**位于 `_mutation_scope()` 内：
`stage_secret:2803`、`import_relay_resources:2854`、`accept_relay_login:3098`、`refresh_relay_resources:3493`、`restore_relay_session:3692`、`_dispatch_codex_model_catalog_toggle:4012`、`_dispatch_webdav_sync:4624`、`dispatch:4716`、`dispatch:5078`、`_supervise_service_once:5823,5881`、`apply:6769`、`reload:6840`、`probe:6876,6907`、`import_package:7254`。
`_emit()` 会调 `self.snapshot()`，而 `_advance_revision()`（`service.py:1406`）刚清空 `_snapshot_cache`，所以必然是冷投影。于是写事务的响应时间 = 全量投影 + N×deepcopy + N×编码，全部在 `_mutation` 全局 RLock 内。

**根因二：单一全局写锁被慢操作占用。**
`_mutation_scope` 在 `service.py` 出现 **29 次**。其中持锁跨越网络/子进程的：`probe`（原生端 900 s 超时）、`accept_relay_login` / `refresh_relay_resources`（60 s）、`_dispatch_webdav_sync`（`4331` 起整段远端同步）、worker 状态读取（20–30 s）、`service.*`（换进程，分钟级）。

**根因三：一次动作三次全量。**
`dispatch` 返回前一次（根因一）→ `run()` 的 `if (refreshAfter) await refresh()`（`YoungRouterApp.tsx:2848`）→ 事件 `receiveSnapshot`（`1758`）→ `setSnapshot`。而根 `useState` 在 `YoungRouterApp.tsx:1725`，`ui/` 目录 `React.memo` 仅 1 处（`NativeControls.tsx`），`YoungRouterApp.tsx` 内 0 处。

### 2.2 放大器

- **全局 revision**：`_check_revision`（`service.py:3831`）比单一计数器；后台 bump（probe 写回、worker 状态、client model sync）让用户编辑吃 `revision_conflict` → 拉全量 → 重试。
- **周期性全量**：`SERVICE_STATE_RECONCILE_MS=1000`（`YoungRouterApp.tsx:104`，`starting` 期间每窗口每秒）、`SERVICE_SETTLED_RECONCILE_MS=15000`（`113`）、`SERVICE_UNHEALTHY_RECONCILE_MS=5000`（`122`）、`SETTINGS_DISK_POLL_MS=5000`（`757`/`3160`）。
- **事件队列**：`MAX_EVENTS=32`（`ipc.py:454`），`_Subscription.queue` maxsize 32（`ipc.py:557`），`_publish` 满时丢最旧（`ipc.py:2496`）。
- **Windows 无连接复用**：`CoreIPCBridge.cpp:1004-1020` 每次 `WinHttpOpen` + `WinHttpConnect`。
- **macOS 会话续期持锁**：`CoreIPCBridge.swift:682-690` `ensureSession()` 持 `NSLock` 跨越 `exchangeSession` 网络调用。

---

## 3. 目标架构

一次落地后的最终形态：

```
写路径：dispatch ──► 领域锁（单域） ──► 写内存 ──► 持久化 ──► 记账 changed 实体
                │                                              │
                └──► 立即返回 {revision, changed[]}             │
                                                                ▼
读路径：事件合并器（16–50 ms 去抖）──► 领域 diff ──► {event:"patch", base_revision, revision, ops[]}
                                                                │
订阅端：revision 连续 ──► apply ops（局部）                     │
        revision 断档 ──► 拉全量 snapshot（回退）  ◄────────────┘
慢操作：probe / webdav / login / service.* ──► job_id + 事件，不持写锁
客户端：按域 store + 选择器订阅 + 乐观更新；不再 dispatch 后 refresh
```

### 3.1 协议扩展（v1 内向后兼容，不升版本）

**事件两种形态**（`protocol.py:643` `make_event` 扩展，非替换）：

```jsonc
// 全量：首连、断档恢复
{ "protocol_version": 1, "event": "snapshot", "revision": 42, "snapshot": { … } }

// 增量：常规
{ "protocol_version": 1, "event": "patch",
  "base_revision": 41, "revision": 42,
  "domains": ["providers_models"],
  "ops": [
    { "domain": "providers_models", "kind": "upsert", "id": "model:abc", "value": { … } },
    { "domain": "providers_models", "kind": "remove", "id": "model:def" }
  ] }

// 明确要求重同步（队列溢出、diff 不可用）
{ "protocol_version": 1, "event": "resync", "revision": 42 }

// 作业进度与结果
{ "protocol_version": 1, "event": "job", "job_id": "…", "state": "running|done|failed",
  "result": { … } }
```

**硬约束**：
- `ops` **必须**支持 `remove`，否则被删的行在客户端残留——这是增量同步最典型的事故。
- `base_revision` 与客户端当前 revision 不连续时，客户端丢弃该 patch 并拉全量；`revision` 单调不变。
- 旧客户端只认 `snapshot`，因此首次订阅与断档仍走全量，**协议不破**。

### 3.2 五个不变量（写进 AGENTS.md）

1. **写事务不投影**：`_mutation_scope()` 内禁止调用 `snapshot()` 或 `_emit()`。
2. **事件只表达增量**：新增 domain 只需实现 `diff`，自动接入增量推送。
3. **慢操作不持写锁**：锁内等待 socket / 子进程一律视为缺陷，由 `CORE_LOCK_BUDGET_SECONDS` 与新增断言拦截。
4. **载荷有预算**：快照字节数与单次事件字节数进测试，超预算即失败。
5. **一次动作一次读**：UI 动作默认走事件回推，禁止"dispatch 后再 refresh"。

---

## 4. 实施清单（按依赖顺序，同批次提交）

> 编号只表示内部依赖顺序。全部改动作为同一个变更集提交与发布。

### 4.1 契约层（先改，后续所有代码依赖它）

**4.1.1** `young_router/core/ipc-v1.schema.json`

- `subscribeParams.topics.items.enum`：`["snapshot"]` → `["snapshot", "patch", "resync", "job"]`（当前在 `$defs.subscribeParams`）。
- `event` 定义（当前 `required: [protocol_version, event, revision, snapshot]`，`event.const: "snapshot"`）改为 `oneOf` 四分支，每分支 `additionalProperties: false`。
- `$defs.dispatchResult`：`properties` 增加 `changed: {"type":"array","items":{"type":"string"},"maxItems":256}`（保持 `required: ["revision"]`，旧客户端不受影响）。
- 若新增 `snapshot` 的域过滤参数，在 `methods.snapshot.params` 引入（否则保持 `emptyParams`）。

**4.1.2** `rn/packages/shared/src/types.ts`

- `IpcEvent`（`604-609`）改为可辨识联合：`SnapshotEvent | PatchEvent | ResyncEvent | JobEvent`；`IpcEventTopic`（`602`）自动跟随。
- `IpcResults["dispatch"]` 增加 `changed?: string[]`。
- `IpcParams.snapshot` 若加过滤参数则同步。

**4.1.3** `rn/scripts/check-contract.mjs` — **容易漏的一处**

- `186` 行断言 `coreSchema.event.properties.event.const === "snapshot"`，`187-188` 断言 `IpcEvent.event` 类型为字面量 `"snapshot"`。联合类型后这两处必须改成"联合成员集合相等"的断言（比较排序后的成员列表），否则 `contract-check` 必红。

**4.1.4** `rn/packages/shared/src/ipc.ts`

- `subscribe` 的 `topics` 透传不变；新增 patch 事件的分发不在此层（在 `YoungRouterApp` 的 `receiveSnapshot` 处，见 4.5）。

**验收**：`cd rn && pnpm run contract-check && pnpm run specs:check`

---

### 4.2 载荷瘦身（纯收益，无行为变化）

**4.2.1** `young_router/core/domains/runtime.py:447-454`

当前同时输出 `"fields": fields` 与 `"settings": fields`（同一 list 两次序列化）。

- 删除 `"fields"` 键，保留 `"settings"`。
- 事实依据：RN 侧只读 `.settings`（`YoungRouterApp.tsx:10779`、`11131`），`rg '\.fields' rn/packages/shared/src` 无 runtime 消费点。
- 同步修改 4 处直接读 adapter 的测试：`tests/test_core_operations.py:1082,1101,1574,1586` → `["settings"]`。

**收益**：106 KB → 58.6 KB（−45%）。若日后发现未预期消费者，改留 `fields` 删 `settings` 亦可（二者等价），但**不得两个都留**。

**4.2.2** `young_router/core/ipc.py` 订阅者编码复用

`_emit` 对每个订阅者各做一次 `deepcopy` + 编码。快照是不可变投影，改为：**一次编码成 bytes，多订阅者复用**（`_send_encoded` 已存在，`ipc.py:743`）。仅当事件带订阅者私有数据时才逐订阅者编码。

**验收**：新增 `test_emit_encodes_once`——3 个订阅者时编码调用计数为 1。

---

### 4.3 投影出锁与事件合并

**4.3.1** 把 16 处锁内 `_emit()` 改为"锁内标记、锁外发送"

对每处（清单见 2.1）改为：

```python
should_emit = False
with self._mutation_scope():
    …
    should_emit = self._revision != previous_revision
if should_emit:
    self._emit()
```

`_emit()` 内部本就会自行获取 `_mutation` 做投影，移出后语义不变，只是把投影从写事务里挪出来。

**注意点**：
- `_supervise_service_once:5823,5881` 与 `_reload_service_after_apply:5557,5589,5601` 在后台线程，本就不持锁——保持原样，只确认没有间接持锁。
- `apply:6769`、`import_package:7254` 的 `_emit` 若依赖事务内的中间态，需把该中间态提前落定再移出。
- `tests/test_core_disk_conflicts.py:141` 直接调 `core._emit()`，语义保持同步，不需改。

**4.3.2** 事件合并器（新增 `young_router/core/emitter.py`）

- 单飞线程 + `threading.Event`；窗口内（默认 16 ms，上限 50 ms）只发一次事件；
- 事件携带**合并后的最终 revision**；
- 无订阅者时直接跳过投影（现有行为保留）；
- `stop()` 时 flush 未发送事件。

**验收**：
- `test_emit_outside_mutation_lane`：在 `_emit` 内注入 50 ms 延迟，`dispatch` 返回 < 100 ms。
- `test_emit_coalesces_burst`：连续 3 次 dispatch，订阅者只收到 1 个事件且 revision 为最后一个。

---

### 4.4 增量事件

**4.4.1** 领域 diff（选择"领域级 diff"，不用写入点记账）

给 `DomainAdapter` 协议加可选方法：

```python
def diff(self, previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[EntityOp]:
    """Return entity-level upsert/remove ops between two projections."""
```

- 有实现的域用它；没有实现的域**回退为整域 upsert**（`{"domain": X, "kind": "replace", "value": current}`）。
- 9 个域的快照方法位置：`providers_models:1165`、`relay_accounts:1750`、`codex:1352`、`claude:2367`、`runtime:436`、`webdav:160`、`logs:2751`、`language:237`、`clients:232`。
- 首轮先实现 `providers_models`（唯一有 entity id 语义、也是 `add` 的目标域），其余回退 `replace`。**这不影响正确性**，只影响收益——`runtime` 域虽然最大但极少变更。

**4.4.2** `CoreStore` 记账与产出

- `dispatch` / `apply` / `reload` / `probe` commit 处记录本次 revision 改动了哪些域（现成的 `before`/`after` 比较在 `service.py:4729`、`5053` 附近）。
- 事件合并器在发送时，对每个改动域调用 `diff(last_published_projection[domain], current[domain])`，产出 `ops`。
- 若 `diff` 抛异常或域集合过大（> 阈值），发 `resync` 事件。

**4.4.3** 队列溢出语义（`ipc.py:454/557/2496`）

- 满时丢弃最旧的 patch，并给该订阅打 `needs_resync`；
- 该订阅下一次取事件时先收到 `{"event":"resync","revision":N}`。

**验收**：
- `test_patch_roundtrip`：`add` → 事件 `ops` 含 1 个 upsert，载荷 < 4 KB。
- `test_patch_remove`：`delete` → 事件含 `remove` op，客户端应用后该行消失。
- `test_overflow_resync`：slow consumer 压测后客户端最终 revision 与服务端一致。

---

### 4.5 客户端应用增量与状态分层

**4.5.1** `rn/packages/shared/src/ui/YoungRouterApp.tsx`

- 新增 `applyPatch(snapshot, event)`：按 `ops` 对 `snapshot.domains[domain]` 做局部替换；`replace` 语义整体替换该域。
- `receiveSnapshot`（`1758-1775`）拆为 `receiveSnapshot` 与 `receiveEvent`：`base_revision` 不连续 → 丢弃并调用一次 `refreshSnapshot()`。
- 单调 revision 检查保留，patch 走同一道门。

**4.5.2** 按域 store（`useSyncExternalStore`）

- 这已经是本仓库既有模式：`HostChrome.tsx:86,357`、`RelayGroupSheet.tsx:136` 都在用，因此不引入新依赖、也不需要新约定。React 19.2.3 / RN 0.85 原生支持。
- 把根 `useState<CoreSnapshot>`（`1725`）拆为按域的订阅单元：`service`、`providers_models`、`codex`、`claude`、`runtime`、`relay_accounts`、`logs`、`webdav`、`disk`、`drafts`。
- 组件用选择器订阅。**不引入 Redux**：这里的模型是"服务端投影 + 增量"，不是本地 reducer 图。
- 若团队倾向现成库，选 `zustand`（58.9k star，2026-11 活跃），但选择器语义必须一致。

**4.5.3** 去掉多余刷新

- `run()` 的 `if (refreshAfter) await refresh()`（`2848`）：事件通道已连接且 revision 连续时**跳过**；保留为通道异常时的兜底。
- `SERVICE_STATE_RECONCILE_MS` / `SETTLED` / `UNHEALTHY` / `SETTINGS_DISK_POLL_MS` 轮询改为**只拉轻量字段**（新增 `snapshot` 的域过滤参数，或复用 `disk_state` 模式），不再拉全量。

**4.5.4** 乐观更新

- `runMutationOnce`（`6322`）路径：`add` 按下即本地插入临时行（`pending: true`），Core 确认后以权威数据覆盖，失败回滚并提示。
- 约束：临时行**不参与** draft 比较，否则会触发伪 dirty 与误报"设置已变更"。

**验收**：
- 新增 `tests/test_rn_patch_apply.mjs`：upsert / remove / 断档三类事件，结果与"直接拉全量"逐字段一致。
- React Profiler：一次 `add` 后只有 `providers_models` 相关组件 commit。

---

### 4.6 分域锁与作业模型

**4.6.1** 分域锁

- 现状：`service.py:1234` 一把 `self._mutation = threading.RLock()`。
- 目标：每域一把锁 + 一把跨域事务锁；组合动作必须先声明"涉及域集合"，按固定顺序获取以避免死锁。
- 跨域依赖清单（必须显式声明）：`relay_accounts` 删除 → 同时改 `providers_models`（`service.py:4811` 起的 `relay_dependency_action` 分支）；`provider_select_relay_station` 的 `binding_source` 解析。

**4.6.2** 慢操作作业化

按此顺序迁移（同一批次，内部顺序）：

1. `probe` — 立即返回 `{revision, job_id}`，结果走 `job` 事件；`action_summary` 保留为兼容字段。
2. `_dispatch_webdav_sync` — 同上，远端同步不再持写锁。
3. `service.*` — 生命周期操作返回 job，状态经 `job` 事件与 `service` 行推送。
4. worker 状态读取（workbuddy/qoder/our_free_model）— 同上。

**注意**：`probe` 的原生超时是 900 s（`CoreIPCBridge.swift` 的 `responseTimeoutInterval`），作业化后该超时只对"取作业结果"生效，不再是 `dispatch` 的超时——这正是当前最可能的卡死来源。

**4.6.3** 分域 revision

- `revision` 保持全局单调（协议兼容），**冲突检查改为比较本域版本**。
- `dispatch` 增加可选 `expected_domain_revision`；`_check_revision`（`3831`）保留给显式全局校验场景。
- 效果：worker 状态刷新不再让用户编辑 rebase；真正的并发语义冲突仍触发 `revision_conflict`。

**验收**：
- `test_domain_lock_parallel`：一个域内 5 s 长操作进行时，另一个域 `add` < 200 ms 返回。
- `test_probe_does_not_block_dispatch`：probe 运行期间连续 `add` 5 次，全部 < 200 ms。
- `test_job_events`：probe 作业从 running 到 done 的事件序列完整。

---

### 4.7 原生传输修正

**4.7.1** Windows 连接复用 — `rn/apps/windows/src/native/windows/CoreIPCBridge.cpp:1004-1020`

- `WinHttpOpen` / `WinHttpConnect` 句柄上移到桥接实例，按 endpoint 缓存；每次请求只新建 `WinHttpOpenRequest`。
- 注意 `WinHttpSetTimeouts` 是每 session 设置，缓存后需按请求类型分别设置（长轮询 vs 普通请求）。

**4.7.2** macOS 会话续期不持锁 — `rn/apps/macos/src/native/macos/CoreIPCBridge.swift:682-690`

- `ensureSession()` 改为：锁内只读取凭据快照，锁外做 `exchangeSession` 网络交换，锁内双检写入（generation 不变才写）。
- 同时确认 `performCoreRequest`（`604` 起）的失败路径不会在锁内等待。

**4.7.3** 事件推送通道

- 保持现有 long-poll（macOS `poll()` `960-995`；Windows `PollEvents` `1103-1114`；Web 宿主 `browserLeaf.ts:291-336` 三处实现语义一致）。
- 唯一修改：处理新增的 `patch` / `resync` / `job` 事件——三者对传输层都是"不透明 JSON 字符串"，**无需改动原生代码**，只在 RN 侧解析（4.5.1）。这是本批次选择保留 long-poll 的关键理由：增量协议让单次事件变小十倍，长轮询的延迟上限（≤ 一个轮询周期，事件到达即返回）不再是主要矛盾。

**不做的**：不换 WebSocket，不改 UDS。理由：实测 loopback 往返 13.5 ms，换传输只能省几毫秒；且原生侧 `URLSessionWebSocketTask` / `WinHttpWebSocket*` 会牵动现有三处 poll 实现与三宿主同源设计（AGENTS.md 第 8 行）。若日后需要，作为独立变更评估。

**验收**：Windows 上连续 200 次 `dispatch`，`Get-NetTCPConnection` 观察不存在每次请求新建连接；macOS 会话续期期间并发 10 个请求全部完成且总耗时 < 1 s。

---

## 5. 一次性落地后的行为对照

| 场景 | 现在 | 之后 |
| --- | --- | --- |
| 一次 `add` 的载荷 | 106 KB × 3 次 | patch 1–3 KB × 1 次 |
| `add` 的响应时间 | 含冷投影（真实环境 270–375 ms） | 仅领域写入 + 持久化（< 10 ms） |
| probe 进行中 `add` | 排队至 probe 结束（最长 900 s） | < 200 ms |
| 客户端重渲染范围 | 整棵树 | 单个域 |
| 事件丢失 | 丢最旧全量，静默 | 发 `resync`，可恢复 |
| 3 窗口同时在线 | 3 × 106 KB / 事件 | 1 × 编码 + 3 × 小 patch |

---

## 6. 测试与验证

### 6.1 新增测试

| 文件 | 用例 |
| --- | --- |
| `tests/test_core_ipc_latency.py` | `test_snapshot_payload_budget`（< 64 KB / < 72 KB）、`test_emit_outside_mutation_lane`、`test_dispatch_response_does_not_project` |
| `tests/test_core_emit.py`（新） | `test_emit_coalesces_burst`、`test_emit_encodes_once`、`test_patch_remove`、`test_overflow_resync` |
| `tests/test_core_locking.py`（新） | `test_domain_lock_parallel`、`test_probe_does_not_block_dispatch`、`test_job_events` |
| `tests/test_rn_patch_apply.mjs`（新） | upsert / remove / 断档三类的客户端应用一致性 |

### 6.2 必须同时更新的既有测试

- `tests/test_core_operations.py:1082,1101,1574,1586` — `["fields"]` → `["settings"]`
- `tests/test_core_ipc.py:2668,2697` — 断言 `event == "snapshot"`，改为"首连为 snapshot，后续为 patch"
- `tests/test_core_disk_conflicts.py:141` — 直接调 `_emit()`，保持同步语义
- `rn/scripts/check-contract.mjs:186-188` — 事件契约断言改联合类型

### 6.3 验证命令

```bash
pytest tests/test_core_ipc.py tests/test_core_ipc_latency.py tests/test_core_ipc_work.py tests/test_core_operations.py tests/test_core_emit.py tests/test_core_locking.py
```

```bash
cd rn && pnpm run contract-check
```

```bash
cd rn && pnpm run specs:check
```

```bash
cd rn && pnpm run typecheck
```

```bash
cd rn && pnpm run check:macos
```

### 6.4 人工验证（发布前必做）

1. 三窗口同时打开（主窗 + 路由窗 + 设置窗），在一窗 `add` 一条 route：另外两窗 100 ms 内出现，且不过度重渲染（Profiler 确认）。
2. 触发一次 probe，同时连续 `add` 5 条：全部即时出现。
3. WebDAV 同步进行中执行 `add`：不排队。
4. 断开再恢复 Core（`service.restart`）：客户端自动 resync，不出现残留行。
5. Windows 与 macOS 各跑一遍 1–4。

---

## 7. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 增量应用出错导致状态不一致 | patch 带 `base_revision`；不连续即拉全量。任何 apply 异常 → 主动 `resync` |
| `remove` 语义遗漏导致残留行 | 专项测试 `test_patch_remove`；`replace` 回退路径兜底 |
| 投影出锁后事件顺序错乱 | 事件带 revision，客户端单调丢弃（已有逻辑）；合并器保证单线程发送 |
| 分域锁死锁 | 跨域动作必须先声明域集合、按固定顺序获取；新增死锁检测测试（超时即失败） |
| 契约改动破坏旧客户端 | 协议版本不变；`snapshot` 事件保留；`dispatchResult.changed` 为可选字段 |
| 作业化改变 probe 的调用语义 | 保留 `action_summary` 兼容字段；调用方仍可从 `job` 事件读结果 |

**回滚**：整个变更集作为一个 commit，回滚即 revert。由于协议向后兼容，回滚不需要数据迁移（`_persist_metadata` 的记录格式未变）。

---

## 8. 明确不做的事

- **不升 `protocol_version`**：旧客户端仍能连，只是收不到 patch 的收益。
- **不换传输层**（WebSocket / UDS）：理由见 4.7.3，作为独立变更评估。
- **不引入 CRDT**（Yjs 23k / Automerge 6.7k / RxDB 23k）：Core 是单写者，版本号 + 补丁足够，CRDT 只会增加调试难度。
- **不引入 gRPC / Cap'n Proto**（45k / 13k）：解决的是跨机调用与 schema 演进，不解决载荷问题。
- **不引入 Redux**：状态来源是服务端投影，不是本地 reducer 图。

---

## 9. 参考实现（开源，2026-11 核对）

| 用途 | 项目 | Stars | 说明 |
| --- | --- | --- | --- |
| 增量补丁应用（JS） | `immerjs/immer` | 29,004 | `produceWithPatches` / `applyPatches`，可直接用于 `applyPatch` |
| 增量补丁（Python） | `stefankoegl/python-json-patch` | 499 | RFC 6902；若服务端改用标准 patch 格式 |
| 细粒度订阅 | `pmndrs/zustand` | 58,910 | 选择器订阅；备选 `pmndrs/valtio` 10,244 |
| 乐观更新参考 | `TanStack/query` | 50,543 | mutation + rollback 的成熟模式 |
| 帧协议参考 | `microsoft/vscode-languageserver-node` | 1,796 | LSP 的 Content-Length 分帧，若日后走 UDS |
| 无需引入 | React 19 / RN 0.85 原生 `useSyncExternalStore` | — | 默认方案，零依赖 |
