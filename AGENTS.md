# Young Router 仓库规则

本文件只记录跨文件、无法从源码和测试直接看出的约束。实现细节、事故时间线、尺寸、命令的完整说明和单点陷阱写在所属源码注释或测试里；不要在这里复制第二份规则。仓库自足，不向父目录寻找项目说明。

## 结构与边界

- 项目名是 `young-router`，Python 包是 `young_router`。`core/` 负责 IPC、域和持久化，`proxy/` 负责 LiteLLM 数据面，`adapters/` 负责 staged worker，`config/` 和 `webdav/` 负责文档与同步，`rn/packages/shared` 是共享 UI、路由、i18n 和类型化 IPC，`rn/apps/{macos,windows,web}` 只放平台叶子，`scripts/` 负责构建发布，`tests/` 放测试。根目录的 `sitecustomize.py` 由构建复制到 Core 根目录，不能漏掉。
- 一个功能只拥有一套共享 UI 和一个 Core 域；不要增加第二套存储、配置写入器、应用外壳或旧启动路径。React 只保存视图状态，不直接写配置、启动 proxy、访问任意路径或持有原始密钥；版本化编辑器正文是唯一例外。
- 工作树可能同时包含多个任务的改动。每个任务只修改自己负责的文件和行为；发现其他任务的未提交改动时，保留它们，不覆盖、不回退、不顺手整理。若同一文件混有不同任务的改动，只做带精确上下文的最小补丁，并在验证时分别说明各自范围。
- IPC 的 schema、校验器、类型化客户端、两个 host bridge 和聚焦测试一起修改。每个 host 都传 Core 私有的 `--metadata` 路径。
- 生成的模型目录和客户端 catalog 不手改；目录字段必须由实际装载的客户端二进制和 Core 使用的 registry 共同验证，`model_catalog_json` 不能指向不存在的文件。
- 公开模型名是大小写敏感的身份。导入入口统一使用 `public_names` 归一化并保留上游 id；读取、分组、重命名、删除、fallback、catalog 和上下文索引都使用同一份名字，不在消费者侧重新推导或做不区分大小写的匹配。

## 单一来源

- 每个派生值只有一个产者；显示名、标签、分组、协议、状态和格式化文本都通过共享函数或域输出，消费者不得从原始字段再算一遍。
- 具有耦合语义的字段只能经一个 setter 一起更新，例如 provider 与 relay station、key 名与 key id。不要在别处只改其中一半。
- 新增派生值时同步更新 `tests/test_single_source_rules.py` 的注册表；这项测试用于禁止规则拷贝，不用于替代实现测试。

## UI、宿主与安全

- 语言只有 `常规` 设置中的一个来源；协议值先经过 i18n 映射再上屏，中文和英文界面不混排。共享布局常量（尤其设置 pane 的 inset、字段栅格和间距）是唯一来源，pane 不得重述数字或套一层只改名的 style 别名。
- 控件保持紧凑的原生密度；列表保留边界，侧栏可使用无框 `SettingsRail`。启用、禁用和次级显示是不同状态；字段帮助只补充控件本身没有表达的信息。
- 自动 apply、读取和后台写入不把整窗锁死；只有用户触发的动作显示 busy。每个 pane 使用自己的 pending 状态，常驻状态条是唯一全局结果面，失败标在所属行。
- 供应商向导、编辑器、分组、登录、日志、模型选择等复杂面都用可拖动的 child panel；任何关闭、失败或早退路径都释放父窗锁。确认、prompt、查看器和菜单由宿主的 `HostChrome`/原生决策面统一提供，不在共享 UI 另造遮罩或 DOM overlay。
- 路径、密钥和裸原生能力不进普通 props。密钥只经 Core 的 read/stage capability；编辑器走版本化 IPC 并遵守消息大小上限。每个 `@objc` 导出都要在 bridge 声明，新增 codegen view 同时登记到 macOS `codegenConfig`。
- 页面和 API 共用 proxy 的一个端口：`/ui`、`/ipc` 由 Core 处理，`/v1` 留给 LiteLLM。页面认证使用 Core 的 HttpOnly、SameSite cookie；首访凭据只在 URL fragment，绝不放进请求路径或普通页面状态。没有 Core 时仍明确返回“无页面/无 API”，不能落到 LiteLLM dashboard。
- 浏览器登录由 Core 驱动站点自己的 Chromium 页面和一次性 profile；页面不能代理别的源、读取别的标签页 cookie 或直接持有站点凭据。编辑器页面作为独立文档 iframe 提供，按其专用 CSP 和消息协议通信。
- relay 站点的登录表单、控制台、用量日志只来自 `relay_accounts.STATION_SURFACES`；三个宿主共享这张表。`remember_password` 与 `remember_session` 是独立开关，网络未知不能当作未登录，资源列表为空不能当作 key 不存在；key 标签始终保留账号/分组前缀。删除账号要同时清理本地 key、route 和同步墓碑。

## 数据面

- 下游 header 默认逐字节透传，不改写、不伪造、不剥离；例外必须有聚焦回归测试。只有在切换 Responses/Chat/Anthropic wire 时，才按共享 helper 移除不被目标 wire 接受的 `extra_body` 字段，不要把整个容器误删。
- 保留 LiteLLM 的路由、Responses 流语义、工具顺序、id 和明确的终止错误；不要为某个请求或 provider 加临时路由 hack。路径错误、客户端 body 错误、上游拒绝、网络失败和本地重启要使用各自的状态与文案，不能用含糊的通用重试句掩盖原因。
- fallback 只在仍有可用兄弟部署时移除失败路线；只有一条路线时不能把同一份字节无界重放。派发、首响应和首帧是不同计时阶段；head/first-frame hedge 不用于生图请求，任何已经交付给客户端的输出都不能被重复重放。
- 对 `response.incomplete`、工具调用、reasoning、搜索和流结束按 wire 的真实事件处理，不按 HTTP 是否正常关闭猜结局。跨 worker 的冷却、终止、重放缩短和 hedge 记账必须放在共享状态中。
- reasoning/image 的 presence 和块顺序是协议的一部分；图片上下文的 Codex/Claude 开关和 wire 形状分别处理，模型上下文统一从 `ModelContextRegistry` 读取。历史压缩或 reasoning 修复第一次只能由上游拒绝触发，后续可依据共享的拒绝记录，且必须保持工具、图片、签名和消息顺序。
- 协议默认值由上游模型名和已写入的用户字段决定；生图模型走 Images 探测，其他模型按实际 wire 探测。客户端自带的 hosted tools 要保持声明形状；本路由不执行不支持的 computer-use，也不把 hosted 工具静默桥成普通 chat。
- 1M 上下文是模型 id 的一部分（如 `<id>[1m]`），不能依赖客户端本机偏好；带后缀和裸 id 的路由、上下文记录和客户端 picker 必须由同一套解析规则关联。推理等级和图像模型过滤也要使用共享解析器，不能在 listing、catalog 和请求入口各写一套。

## 运行、同步与发布

- 运行时从 `.litellm-runtime/config.yaml` 和当前 `runtime-root` 解出；配置变更必须经过 Core 的校验、staging 和 apply/reload，不能靠文件改动静默重启。主密钥更换需要替换 proxy 进程。
- WebDAV 的 settings、state、status、conflict 和 relay 文档跟随同一 runtime root，不跟随全局 `$HOME`。双端都改时以 state 中的祖先做语义三路合并；同一行冲突或校验失败时整轮不写并保留冲突文档。删除用 tombstone 传播，余额、读取时间等观测字段不参与编辑冲突。
- 页面、Core、proxy 的构建和安装使用仓库现有脚本；不要手改生成的 bundle、staged 包或安装目录。版本号由 `scripts/version.py` 管理，平台只在对应主机上构建。
- 不在本规则文件或新脚本中增加 workflow、manifest、指纹、审批或其他自定义状态门禁；现有工具的必要校验保持原样。

## 验证与交付

- 测试源放 `tests/`；一次性探针放系统临时目录并清理，工作树不留 `work/`、`tmp/`、预览 bundle、日志、真实数据截图、trace、运行目录、虚拟环境或包安装。
- Python 改动运行相关聚焦测试；共享 UI 至少运行 `cd rn && pnpm exec tsc --noEmit` 和对应 RN 测试。UI 行为必须在运行中的安装包或隔离 Core 页面上用真实事件验证；截图、AX/DOM 结果比源码断言更有证明力。无法运行时明确写“未在运行中验证”。
- macOS 使用 `scripts/dev-loop-macos.sh`，Windows 使用 `scripts/dev-loop-windows.ps1`；Windows 共享目录只作源码树，依赖和构建输出放 VM 本地树，禁止在共享目录安装 `rn/node_modules`。Windows 架构、Metro、RNW wheel 和 native stale 的边界以脚本当前行为为准。
- 提交前人工检查未跟踪文件和 diff 中的凭据、私有端点、provider/model 名、请求或任务 id、机器路径和抄入的配置。不要用 `--no-verify` 绕过已有检查。
- 排查 Codex 任务时先用精确 task/thread id 查看最近状态，再查对应 rollout 和窄时间窗日志；不要扫整个日志目录或 `~/.codex/sessions`。
