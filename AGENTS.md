# Young Router 仓库规则

本文件只记录跨文件、无法从源码和测试直接看出的约束。实现细节、事故时间线、尺寸、命令的完整说明和单点陷阱写在所属源码注释或测试里；不要在这里复制第二份规则。仓库自足，不向父目录寻找项目说明。

## 结构与边界

- 项目名是 `young-router`，Python 包是 `young_router`。`core/` 负责 IPC、域和持久化，`proxy/` 负责 LiteLLM 数据面，`adapters/` 负责 staged worker，`config/` 和 `webdav/` 负责文档与同步，`rn/packages/shared` 是共享 UI、路由、i18n 和类型化 IPC，`rn/apps/{macos,windows,web}` 只放平台叶子，`scripts/` 负责构建发布，`tests/` 放测试。根目录的 `sitecustomize.py` 由构建复制到 Core 根目录，不能漏掉。
- 一个功能只拥有一套共享 UI 和一个 Core 域；不要增加第二套存储、配置写入器、应用外壳或旧启动路径。React 只保存视图状态，不直接写配置、启动 proxy、访问任意路径或持有原始密钥；版本化编辑器正文是唯一例外。
- 工作树可能同时包含多个任务的改动。每个任务只修改自己负责的文件和行为；发现其他任务的未提交改动时，保留它们，不覆盖、不回退、不顺手整理。若同一文件混有不同任务的改动，只做带精确上下文的最小补丁，并在验证时分别说明各自范围。
- IPC 的 schema、校验器、类型化客户端、两个 host bridge 和聚焦测试一起修改。每个 host 都传 Core 私有的 `--metadata` 路径。
- 生成的模型目录和客户端 catalog 不手改；目录字段必须由实际装载的客户端二进制和 Core 使用的 registry 共同验证，`model_catalog_json` 不能指向不存在的文件。目录条目里**必须**写的只有 `model_messages.instructions_template`：客户端对缺失的模板返回空系统提示词（无 bundled 回退）。其余 message 家族（approvals、permissions、collaboration_modes、auto_review、multi_agent、persistent_instructions、token_budget、confirmation_policies、apps、plugins）都由客户端自带默认，写进目录等于把旧版本客户端的措辞钉死，因此不写。顶层 `base_instructions` 是已弃用的兼容镜像，永不写。
- 公开模型名是大小写敏感的身份。导入入口统一使用 `public_names` 归一化并保留上游 id；读取、分组、重命名、删除、fallback、catalog 和上下文索引都使用同一份名字，不在消费者侧重新推导或做不区分大小写的匹配。

## 单一来源

- 每个派生值只有一个产者；显示名、标签、分组、协议、状态和格式化文本都通过共享函数或域输出，消费者不得从原始字段再算一遍。
- 具有耦合语义的字段只能经一个 setter 一起更新，例如 provider 与 relay station、key 名与 key id。不要在别处只改其中一半。
- 新增派生值时同步更新 `tests/test_single_source_rules.py` 的注册表；这项测试用于禁止规则拷贝，不用于替代实现测试。

## UI、宿主与安全

- 语言只有 `常规` 设置中的一个来源；协议值先经过 i18n 映射再上屏，中文和英文界面不混排。共享布局常量（尤其设置 pane 的 inset、字段栅格和间距）是唯一来源，pane 不得重述数字或套一层只改名的 style 别名。
- 控件保持紧凑的原生密度；列表保留边界，侧栏可使用无框 `SettingsRail`。启用、禁用和次级显示是不同状态；字段帮助只补充控件本身没有表达的信息。按钮上方不留空白带：子面板按表单自身高度定窗（`CHILD_SURFACE_*`），不用固定高度把按钮压到底。
- 按钮不写「取消」：子面板底部只有「关闭」和「保存并关闭」，关闭不写盘；确认类问题用其结果命名（如「继续编辑」「放弃」），登录中断用「停止登录」。新增或修改按钮时按这条审查。
- 自动 apply、读取和后台写入不把整窗锁死；只有用户触发的动作显示 busy。每个 pane 使用自己的 pending 状态，常驻状态条是唯一全局结果面，失败标在所属行。
- 供应商向导、编辑器、分组、登录、日志、模型选择等复杂面都用可拖动的 child panel；任何关闭、失败或早退路径都释放父窗锁。确认、prompt、查看器和菜单由宿主的 `HostChrome`/原生决策面统一提供，不在共享 UI 另造遮罩或 DOM overlay。
- 路径、密钥和裸原生能力不进普通 props。密钥只经 Core 的 read/stage capability；编辑器走版本化 IPC 并遵守消息大小上限。每个 `@objc` 导出都要在 bridge 声明，新增 codegen view 同时登记到 macOS `codegenConfig`。
- 页面和 API 共用 proxy 的一个端口：`/ui`、`/ipc` 由 Core 处理，`/v1` 留给 LiteLLM；`/codex/models` 是 Core 提供给 Codex 自取的模型目录（与 OpenAI 自家 `.../backend-api/codex/models` 同拼写），不算 LiteLLM 的产者：数据面 `?client_version=` 的翻译会丢 `model_messages` 与上下文/推理档，不能当目录源。页面认证使用 Core 的 HttpOnly、SameSite cookie；首访凭据只在 URL fragment，绝不放进请求路径或普通页面状态。没有 Core 时仍明确返回“无页面/无 API”，不能落到 LiteLLM dashboard。模型列表是本机可读的公开数据：`GET`/`HEAD` 的 `/v1/models`、`/models`、`/codex/models` 在**对端为 loopback 且 `Host` 为 loopback 名**时由代理补上主密钥（`proxy/local_listing_key.py`），其余路径、其余方法、其余对端仍然要密钥；已自带凭据的请求一律不覆盖。
- 浏览器登录由 Core 驱动站点自己的 Chromium 页面和一次性 profile；页面不能代理别的源、读取别的标签页 cookie 或直接持有站点凭据。编辑器页面作为独立文档 iframe 提供，按其专用 CSP 和消息协议通信。
- relay 站点的登录表单、控制台、用量日志只来自 `relay_accounts.STATION_SURFACES`；三个宿主共享这张表。`remember_password` 与 `remember_session` 是独立开关，网络未知不能当作未登录，资源列表为空不能当作 key 不存在；key 标签始终保留账号/分组前缀。删除账号要同时清理本地 key、route 和同步墓碑。
- 中转站自动登录和续期只由 Core 在同一账号写入流程内执行，面板、资源和用量读取及后台任务共用。先复用当前凭据，再尝试站点的续期协议；只有站点明确拒绝且不能续期，或 Core 确认已退出且无可用会话时，才允许一次密码 POST。超时、普通 403、畸形响应、账号头错误和宿主缓存缺失均不构成拒绝。宿主只预填手动表单；站点的会话上限拒绝须跨 Core 重启继续退避。
- 记住登录态时，先持久化已验证会话和轮换的刷新凭据，再报告完成。刚签发的会话未被站点接受时，仍属于同一次恢复，即使重启也不得再次自动提交密码。旧请求、资源刷新、无关草稿及其回滚不得覆盖新会话；写盘失败只重试保存原会话。改动须验证明确拒绝、未知探测、并发、写盘失败和重启，按实际请求分别统计密码 POST 和续期次数，并读回会话文件。
- 会话数上限是中转站上次答复；没有新请求时不得显示为当前实时状态。用户明确重试可以跳过该次拒绝的退避，仍应先探测和续期；同一个旧拒绝不能授权重复提交，后台和页面重挂载不能跳过退避。

## 数据面

- 下游 header 默认逐字节透传，不改写、不伪造、不剥离；例外必须有聚焦回归测试。只有在切换 Responses/Chat/Anthropic wire 时，才按共享 helper 移除不被目标 wire 接受的 `extra_body` 字段，不要把整个容器误删。
- 保留 LiteLLM 的路由、Responses 流语义、工具顺序、id 和明确的终止错误；不要为某个请求或 provider 加临时路由 hack。路径错误、客户端 body 错误、上游拒绝、网络失败和本地重启要使用各自的状态与文案，不能用含糊的通用重试句掩盖原因。
- fallback 只在仍有可用兄弟部署时移除失败路线；只有一条路线时不能把同一份字节无界重放。派发、首响应和首帧是不同计时阶段；head/first-frame hedge 不用于生图请求，任何已经交付给客户端的输出都不能被重复重放。
- 对 `response.incomplete`、工具调用、reasoning、搜索和流结束按 wire 的真实事件处理，不按 HTTP 是否正常关闭猜结局。跨 worker 的冷却、终止、重放缩短和 hedge 记账必须放在共享状态中。
- Codex 流遇到 `response.incomplete`（`max_messages`）时，只结束本次采样。保留已生成的进度和完整工具调用，让 Codex 自动续跑原任务；不重放已交付内容。只有无生成输出且历史过重时才尝试缩短历史；短请求即使空截断也要续跑，不能因此冷却唯一可用的部署。
- `incomplete`/`max_messages` 不能被适配器边界误判为完成：重复工具循环的证据须跨请求适配和流式路径传递；比较调用与结果时忽略 `Wall time` 等易变包装字段，并统计本轮历史中的最大重复段。只有确认无进展后才注入 `tool_choice: none` 收口，不能因元数据丢失或后续一次不同调用而放弃保护。
- reasoning/image 的 presence 和块顺序是协议的一部分；图片上下文的 Codex/Claude 开关和 wire 形状分别处理，模型上下文统一从 `ModelContextRegistry` 读取。历史压缩或 reasoning 修复第一次只能由上游拒绝触发，后续可依据共享的拒绝记录，且必须保持工具、图片、签名和消息顺序。
- 协议默认值由上游模型名和已写入的用户字段决定；生图模型走 Images 探测，其他模型按实际 wire 探测。客户端自带的 hosted tools 要保持声明形状；本路由不执行不支持的 computer-use，也不把 hosted 工具静默桥成普通 chat。
- 1M 上下文是模型 id 的一部分（如 `<id>[1m]`），不能依赖客户端本机偏好；带后缀和裸 id 的路由、上下文记录和客户端 picker 必须由同一套解析规则关联。推理等级和图像模型过滤也要使用共享解析器，不能在 listing、catalog 和请求入口各写一套。

## 运行、同步与发布

- 运行时从 `.litellm-runtime/config.yaml` 和当前 `runtime-root` 解出；配置变更必须经过 Core 的校验、staging 和 apply/reload，不能靠文件改动静默重启。主密钥更换需要替换 proxy 进程。
- WebDAV 的 settings、state、status、conflict 和 relay 文档跟随同一 runtime root，不跟随全局 `$HOME`。双端都改时以 state 中的祖先做语义三路合并；同一行冲突或校验失败时整轮不写并保留冲突文档。删除用 tombstone 传播，余额、读取时间等观测字段不参与编辑冲突。
- 共享账号续期和自动登录使用同一个账号级 WebDAV 排他锁。先读取其他设备已保存的会话，凭据写入前记录不含密钥的恢复声明；另一台即使重启或锁已释放，仍须等到较新的会话同步或明确可重试的站点结果。锁忙、协调失败或服务不支持锁均不授权密码 POST。新会话尽快同步；旧凭据更新的探测时间不能覆盖新会话。验证须覆盖两台独立 Core 在同步前后及重启时的实际请求次数。
- 页面、Core、proxy 的构建和安装使用仓库现有脚本；不要手改生成的 bundle、staged 包或安装目录。版本号由 `scripts/version.py` 管理，平台只在对应主机上构建。
- 不在本规则文件或新脚本中增加 workflow、manifest、指纹、审批或其他自定义状态门禁；现有工具的必要校验保持原样。

## 验证与交付

- 测试源放 `tests/`；一次性探针放系统临时目录并清理，工作树不留 `work/`、`tmp/`、预览 bundle、日志、真实数据截图、trace、运行目录、虚拟环境或包安装。
- Python 改动运行相关聚焦测试；共享 UI 至少运行 `cd rn && pnpm exec tsc --noEmit` 和对应 RN 测试。UI 行为必须在运行中的安装包或隔离 Core 页面上用真实事件验证；截图、AX/DOM 结果比源码断言更有证明力。无法运行时明确写“未在运行中验证”。
- macOS 使用 `scripts/dev-loop-macos.sh`，Windows 使用 `scripts/dev-loop-windows.ps1`；Windows 共享目录只作源码树，依赖和构建输出放 VM 本地树，禁止在共享目录安装 `rn/node_modules`。Windows 架构、Metro、RNW wheel 和 native stale 的边界以脚本当前行为为准。
- 交付前必须用 devloop 在真实安装包里实测目标路径：原生源码改动先 `scripts/build-and-install-macos.sh`，再 `scripts/dev-loop-macos.sh --check` 确认无 stale，并读回写出的文档。只跑单测、网页宿主、源码断言或布局推算不算实测；未实测不得结束。会写客户端文件的测试用隔离的临时 home，不碰用户真实配置。
- devloop 不能承载改动时必须构建重启，不得降级成交付：工作树里另一任务的 native 源比安装包新，或混合改动无法只同步一半时，直接运行 `scripts/build-and-install-macos.sh`，在重启后的安装包上实测目标路径。手工拷贝单个文件、只重启 worker、单测或源码断言都不能作为“已修复”的交付证据；这些只可用于临时排查。构建会把同一工作树里其他任务的未提交改动一并编入时，在交付说明中点名，不能当作没发生。
- 代理、协议和路由修复要用实际客户端或等价请求复现原场景，并通过本次修改后的运行实例重测。流式请求要读到终止事件，按请求核对命中的部署、实际尝试次数和响应结果。供应商专属改动要命中该上游实测，涉及重启时再核对新进程的健康和请求结果。
- 严禁为验证抢占前台：不激活或前置窗口、不移动或点击鼠标、不切换用户焦点、不用 AppleScript/`open` 把应用带到前台。构建、同步、重启都在后台完成；必须前台交互才能验证的部分先征得同意，否则用隔离实例或无头方式验证，并在交付说明里写明哪部分未在真实界面验证。
- 后台重启实测是默认动作，不是待批准事项：改动能由 devloop 承载时（`scripts/dev-loop-macos.sh`、`--check`、`scripts/restart-app.sh`、后台启动应用、读回客户端文档、curl 本机端点）直接执行，不逐次征询；停在那里等一句「要不要重启」只等于交付未实测。仍然不碰前台，也不再单独把「未在运行中验证」当作合格交付。同样，交付说明不以「要不要我做 X / 是否需要 Y」收尾：能由源码、上游实现或本次实测裁决的，直接做或直接否掉，并写明依据；只有真正需要人做产品取舍且无法从既有约定推出的事项，才留作待定项。
- 提交前人工检查未跟踪文件和 diff 中的凭据、私有端点、provider/model 名、请求或任务 id、机器路径和抄入的配置。不要用 `--no-verify` 绕过已有检查。
- 上传前确认没有只存在于本地的残留工作：`git stash list` 无条目、`git branch -vv` 只有要推的分支、`git worktree list` 只有当前 checkout、`git remote prune origin` 后无过期的 remote-tracking ref。逐个判断残留是否还值得留下，值得的并进它该在的提交历史。`git fsck --lost-found` 里的 dangling 是已 drop 的 stash：仍需要的先捞回再 prune——不可达提交形式的 stash 仍是那份工作的第二份副本。
- 公开远端只用预期的公开或 noreply Git 身份：它记在每个克隆里，后来的提交改不了，所以要在对象存在之前检查。
- 排查 Codex 任务时先用精确 task/thread id 查看最近状态，再查对应 rollout 和窄时间窗日志；不要扫整个日志目录或 `~/.codex/sessions`。
