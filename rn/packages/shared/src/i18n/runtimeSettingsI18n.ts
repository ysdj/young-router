import type { Translator } from "./types";

type RuntimeCopy = { label: string; help: string };

const categories: Record<string, string> = {
  "Timeouts": "超时",
  "Recovery": "恢复",
  "Web Search": "网页搜索",
  "Vision Router": "视觉路由",
  "Model Context": "模型上下文",
  "Codex Images": "Codex 图片上下文",
  "Fallback": "故障转移",
  "MCP": "MCP",
  "Logs": "日志",
  "Network": "网络",
  "Service": "服务",
  "Our Free Model": "Our Free Model",
};

const units: Record<string, string> = {
  seconds: "秒",
  retries: "次重试",
  results: "条结果",
  pages: "页",
  chars: "字符",
  rounds: "轮",
  turns: "轮",
  queries: "条查询",
  actions: "次操作",
  failures: "次失败",
  MB: "MB",
  workers: "个工作进程",
  rows: "行",
  tokens: "标记",
  hours: "小时",
  bytes: "字节",
  images: "张图",
  segments: "段",
};

const zh: Record<string, RuntimeCopy> = {
  YOUNG_ROUTER_REQUEST_TIMEOUT_SECONDS: { label: "请求超时", help: "上游模型请求、续写合成和每次恢复探测的总超时。设为 0 可取消本地请求上限。" },
  YOUNG_ROUTER_STREAM_START_TIMEOUT_SECONDS: { label: "首事件超时", help: "普通请求等待首个上游流事件的最长时间。设为 0 时回退到请求超时。" },
  YOUNG_ROUTER_HEAVY_REPLAY_STREAM_START_TIMEOUT_SECONDS: { label: "重前缀首事件超时", help: "重放前缀序列化后超过重前缀阈值的 Responses 请求，改用此更长的首事件预算；多 MB 的签名历史会让所有路由同时推迟首个事件。设为 0 时仅使用首事件超时。" },
  YOUNG_ROUTER_HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES: { label: "重前缀阈值", help: "重放负载（input 加 instructions）序列化后超过该字节数时启用重前缀首事件超时。设为 0 可完全关闭加长预算。" },
  YOUNG_ROUTER_WEBSOCKET_MAX_FRAME_BYTES: { label: "WebSocket 帧上限", help: "本地代理接受的单个 WebSocket 消息帧的最大字节数。Codex 会把整轮请求（含签名历史）作为单帧发送，上限过小会在进入路由前被 1009 关闭并触发重连循环。修改后需重启服务生效。" },
  YOUNG_ROUTER_CODEX_COMPACTION_START_TIMEOUT_SECONDS: { label: "压缩首事件超时", help: "结构化 Codex 压缩请求等待首个事件的最长时间。设为 0 时回退到请求超时。" },
  YOUNG_ROUTER_STALL_TIMEOUT_SECONDS: { label: "流空闲超时", help: "首个事件到达后，相邻流事件之间允许的最长间隔。设为 0 可取消本地流空闲上限。" },
  YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS: { label: "流保活间隔", help: "交付流静默期间按此间隔发送一个客户端可解析的保活事件，避免客户端字节空闲计时器早于代理预算触发。设为 0 可关闭保活。" },
  YOUNG_ROUTER_STREAM_CONTINUATION_ENABLED: { label: "截断回答自动续写", help: "路由撞到自己的输出上限时，会以 length 终止帧正常结束本轮，而不是报错，所以整篇翻译可能在中途停住。开启后路由器会向同一路由索取剩余内容，并把续写接进客户端正在阅读的同一条回答。默认关闭：客户端自己发送了输出上限时，应优先在客户端修正，只有无法修正时才由这里兜底。仅适用于纯文本的 Chat Completions 与 Anthropic 回答；带工具的轮次不受影响。" },
  YOUNG_ROUTER_STREAM_CONTINUATION_MAX_ROUNDS: { label: "续写轮数上限", help: "一条回答因路由撞到自身输出上限而最多可被续写的次数。每轮都是一次完整的上游调用，从已交付内容继续。设为 0 时保留路由留下的截断结果。" },
  YOUNG_ROUTER_CODEX_DESCENDANT_CLEANUP: { label: "Codex 父线程屏障", help: "根任务结束前必须取得最新的完整后代线程快照。仍承担必要实现或测试的后代会被等待；已不影响交付的后代按最深层优先中断。关闭后仅使用 Codex 原生生命周期。" },
  YOUNG_ROUTER_RECOVERY_MAX_SECONDS: { label: "恢复最长时间", help: "路由恢复轮询的最长时间。所有路由冷却时保持连接和进度心跳，首个冷却结束后重试。设为 0 可关闭轮询。" },
  YOUNG_ROUTER_RECOVERY_INTERVAL_SECONDS: { label: "恢复探测间隔", help: "两次实际路由恢复探测之间的等待时间。" },
  YOUNG_ROUTER_SAME_DEPLOYMENT_RETRIES: { label: "同路由重试", help: "失败部署切换到下一个同级路由或顺序前的额外尝试次数。默认 0 会立即推进，也会覆盖漾路由的重试计数。" },
  YOUNG_ROUTER_RECOVERY_POLICY_BALANCE: { label: "余额 / 配额", help: "余额不足、配额或计费失败的处理方式。" },
  YOUNG_ROUTER_RECOVERY_POLICY_RATE_LIMIT: { label: "限流 / 过载", help: "HTTP 429、上游过载或容量不足的处理方式。默认恢复，不让部署进入冷却。" },
  YOUNG_ROUTER_RECOVERY_POLICY_SERVER: { label: "服务器 / 网关", help: "临时服务器故障、没有健康路由和网关超时的处理方式。" },
  YOUNG_ROUTER_RECOVERY_POLICY_NETWORK: { label: "网络", help: "断连、DNS 故障和连接错误的处理方式。默认恢复不会让部署进入冷却。" },
  YOUNG_ROUTER_RECOVERY_POLICY_STREAM_START_TIMEOUT: { label: "首事件超时", help: "上游首个流事件到达前发生本地超时时的处理方式。" },
  YOUNG_ROUTER_RECOVERY_POLICY_STREAM_IDLE_TIMEOUT: { label: "流空闲超时", help: "流已开始后发生本地超时时的处理方式。默认恢复不会让部署进入冷却。" },
  YOUNG_ROUTER_RECOVERY_POLICY_REQUEST_ERROR: { label: "请求 / 格式错误", help: "确定性的请求、格式、模型、策略和上下文错误的处理方式。默认错误会直接返回失败，以便修复代理配置而不是继续等待。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED: { label: "启用视觉回退", help: "视觉回退总开关；此控件与高级 JSON 中的 enabled 会相互更新。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND: { label: "视觉回退后端", help: "此控件与高级 JSON 中的 backend 会相互更新；自动会尝试本地和 HTTP 提供方，本地仅使用本地提供方 / OCR，API 仅使用配置的 HTTP 提供方。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK: { label: "使用免费回退链", help: "在允许的后端中追加内置免密 HTTP 回退链；此控件与高级 JSON 中的 freeFallback 会相互更新。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS: { label: "视觉回退超时", help: "每个视觉回退提供方请求允许的最长时间；此控件与高级 JSON 中的 timeoutSeconds 会相互更新。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS: { label: "视觉回退标记数", help: "视觉回退请求最多生成的标记数；此控件与高级 JSON 中的 maxTokens 会相互更新。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED: { label: "启用本地 Ollama", help: "在配置的 HTTP 提供方前尝试本地 Ollama；此控件与高级 JSON 中的 localOllama.enabled 会相互更新，地址和模型仍在高级 JSON 中设置。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED: { label: "启用本地 LM Studio", help: "在配置的 HTTP 提供方前尝试本地 LM Studio；此控件与高级 JSON 中的 localLmStudio.enabled 会相互更新，地址和模型仍在高级 JSON 中设置。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON: { label: "dsh-vision-router 配置", help: "可直接粘贴 JSON 的多行配置框，设置 backend（auto/local/api/off）、localOllama、localLmStudio、按顺序排列的 httpProviders（支持 apiKeyEnv），或 providers 中的 {provider,model,fallbacks}（vision-http/ovh 会映射到内置 HTTP 链；未知适配器会跳过），以及 freeFallback、prompt、timeoutSeconds 和 maxTokens。上方快捷控件与对应 JSON 字段会相互更新。仅当所选模型拒绝图像输入时运行；明确支持 vision 的模型仍直接收到原图。" },
  YOUNG_ROUTER_UNKNOWN_MODEL_CONTEXT_WINDOW: { label: "未知模型原始上下文", help: "当内置配置、实际路由、漾路由与上游元数据都无法识别模型时，供 Codex 使用的原始上下文窗口。默认 272000，按 Codex 95% 的有效窗口策略会显示约 258k。" },
  YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS: { label: "模型上下文刷新间隔", help: "托管 Codex 模型目录刷新公开上下文元数据的间隔。设为 0 后仅使用内置配置与已有缓存，不访问上游。" },
  YOUNG_ROUTER_CATALOG_BASE_PROFILE: { label: "模型目录基础配置", help: "内部记录：第三方 Codex 模型别名继承的原生配置。由 Core 写入，仅当已安装客户端不再提供该配置时才会更新；请勿手动修改。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FAILURES: { label: "冷却失败阈值", help: "同一部署 / 协议对连续失败多少次后临时跳过。该部署的其他已配置协议仍可使用。设为 0 可关闭冷却。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_SECONDS: { label: "冷却时长", help: "失败部署 / 协议对达到阈值后被跳过的时长。仅当所有已配置协议都在冷却时，才会排除该部署。设为 0 可关闭冷却。" },
  YOUNG_ROUTER_QUOTA_RESET_COOLDOWN_HORIZON_SECONDS: { label: "限流恢复时长阈值", help: "当 429 说明了自己多久后恢复时，等待超过这个时长就直接停用（冷却）该路由，不再每次都先试它。说明的恢复时间比它长，说明该路由暂时用不了，会按「冷却时长」跳过它，由健康路由接手；更短、或完全没说明恢复时间的 429，仍按「限流 / 过载」处理。例：设为 60 时，说“30 分钟后恢复”的 429 会停用该路由，说“10 秒后再试”的不会。设为 0 表示只要说明了恢复时间就停用。" },
  YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY: { label: "会话部署亲和", help: "让同一会话在最低可用 order 层内继续使用首次成功服务的部署，既能复用提示缓存，也不会让旧部署越过更优且健康的线路。关闭后每轮完全按路由策略选择。" },
  YOUNG_ROUTER_PROTOCOL_FALLBACK_TTL_SECONDS: { label: "备用协议记忆", help: "某部署的备用协议成功后，在此时长内，同一种客户端协议直接复用该备用协议，不再重试原协议。设为 0 可关闭记忆。" },
  YOUNG_ROUTER_CODEX_COMPACTION_CAPABILITY_TTL_SECONDS: { label: "压缩能力记忆", help: "Responses 路由能否返回加密 Codex 压缩结果的记忆时长。新建任务默认使用 Codex 本地检查点摘要（漾路由保持中性 provider 名）；只有 model_info 显式声明 supports_responses_compaction opt-in 的模型才会公布精确的 OpenAI 名，其路由在转发签名历史前仍会用不带历史的小探测验证（官方 OpenAI 主机跳过探测）。明确不支持的 deployment 会让请求切换到其他候选；当所有候选都拒绝加密压缩时返回明确的协议错误。已在运行的任务保留任务创建时缓存的能力，无法在协议中途切换。设为 0 时每次压缩都重新探测。" },
  YOUNG_ROUTER_WEB_SEARCH_UNSUPPORTED_TTL_SECONDS: { label: "网页搜索不支持记忆", help: "某部署明确拒绝托管的 web_search 后，在此时长内不再附加该声明，改由 Chat 协议桥接完成这一轮，避免每次请求都重复同一拒绝。瞬时网络、配额和提供方搜索失败不会写入该记忆；设为 0 可关闭跨请求记忆。" },
  YOUNG_ROUTER_ALPHA_SEARCH_UNSUPPORTED_TTL_SECONDS: { label: "独立搜索不支持记忆", help: "Codex 的 Responses Lite 通道不会下发托管 web_search，只调用 provider 的 /alpha/search。某条路由明确回答没有该端点后，在此时长内直接跳过它，避免每轮都重复同一次拒绝；超时、5xx 等瞬时失败不会写入该记忆。设为 0 则每次搜索都重新探测。" },
  YOUNG_ROUTER_REASONING_COMPAT_TTL_SECONDS: { label: "推理参数兼容记忆", help: "某部署携带客户端推理参数的兼容形态记忆时长。Gemini 系上游对别名模型拒绝 OpenAI 推理字段时，先改用原生 Google 思考配置重放，保留客户端请求的档位；其他路由则改用不带推理字段的重放，以上游默认思考方式运行。只有上游明确拒绝该模型的思考配置时才会写入该记忆；设为 0 可关闭跨请求记忆（每次请求重新重放一次）。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED: { label: "普通请求写入冷却", help: "普通请求失败时是否向共享部署冷却池累计失败。所有请求类型仍会读取已有冷却状态。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED: { label: "压缩请求写入冷却", help: "结构化 Codex 压缩失败时是否向共享部署冷却池累计失败。默认关闭，避免仅压缩时的上游失败让普通工作路由进入冷却。" },
  YOUNG_ROUTER_IMAGE_TOOL_UNSUPPORTED_TTL_SECONDS: { label: "图像工具不支持记忆", help: "某部署明确拒绝 image_generation 工具后，在此时长内跳过它。参数错误、超时、策略拒绝和其他上游错误不会写入该记忆；设为 0 可关闭跨请求记忆。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_ENABLED: { label: "Codex 图片上下文", help: "只让真正还有用的图进入请求：这一轮的图照常发送，之前各轮的图换成一行占位符（编号、轮次、尺寸），模型需要重看时用 view_image_by_id 按编号取回。占位符措辞、IMG-xxxx 编号和选图文件都沿用 Context Attachment Manager，因此在那个面板里做的勾选在这里同样生效；但不会使用它的代理，Codex 始终指向本代理。只改写重放中可变的尾部，签名历史不动，缓存不受影响。默认关闭：关闭时请求原样送达上游。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MODE: { label: "图片上下文模式", help: "自动：默认省略之前各轮的图，这是长图片任务保持流畅的关键。手动：只有你在 Context Attachment Manager 面板里取消勾选的图才被替换，其余照常发送。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH: { label: "取图方式", help: "模型怎么取回被省略的图。自动：在本应用配置的客户端里登记工具；在没有客户端配置的机器上（服务器上的网关）改为自己提供 HTTP 端点。本地：只登记客户端条目。HTTP：只在 POST /mcp 提供端点，用代理密钥认证。关闭：不承诺任何工具，占位符改为请用户重发。占位符绝不能提到一个不存在的工具。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_KEEP_RECENT: { label: "保留最近轮数", help: "自动模式下，最近多少轮的图仍以内联原图发送。0 表示之前各轮全部省略，这是 Context Attachment Manager 的默认做法，也是反复修改同一张图的任务想要的。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MIN_BYTES: { label: "忽略小于", help: "之前各轮中，小于该字节数的图保持内联：占位符加之后一次取回的开销比省下的字节更大。0 表示所有历史图都参与处理。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR: { label: "选图目录", help: "Context Attachment Manager 存放选图文件的目录。留空依次读取 CAM_DATA_DIR 和该平台下该插件的默认目录。如果你改过 CAM_DATA_DIR，把这里指到同一个目录。" },
  YOUNG_ROUTER_MCP_AUTO_APPROVE: { label: "自动同意 MCP 工具", help: "启用后将 Responses API 的 MCP 工具设置为 require_approval=never，使上游服务不再暂停等待交互式批准。默认关闭，因为这会绕过 MCP 安全审核。" },
  YOUNG_ROUTER_LOG_MAX_BYTES: { label: "本地日志文件上限", help: "本地日志单个文件的容量上限，包括最近请求、服务标准输出 / 错误和状态项操作。轮转时按设定的备份段数保留 .1..N 旧末尾内容。" },
  YOUNG_ROUTER_LOG_BACKUP_SEGMENTS: { label: "日志备份段数", help: "本地日志轮转时保留的历史末尾段数（.1..N）。设为 1 时只保留紧接着的上一段；当前文件始终保留。" },
  YOUNG_ROUTER_LOG_VIEW_LIMIT: { label: "日志视图行数", help: "每个日志标签最多显示的行数。此设置只改变视图，不会删除本地日志数据。" },
  YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS: { label: "请求行中断窗口", help: "请求行在多长时间内没有进展后会被标记为已中断。客户端断开、故障转移拆连或服务重启都可能让上游尝试没有终态回调；此窗口会结束这些行，而不是一直显示“发送中”。设为 0 则保持未完成行不结束。" },
  YOUNG_ROUTER_ROUTE_TRACE_PREVIEW_CHARS: { label: "跟踪预览字符数", help: "始终开启的本地路由跟踪中保留的最大请求预览字符数。" },
  LITELLM_USE_SYSTEM_PROXIES: { label: "使用系统代理", help: "允许上游 HTTP 客户端使用 macOS 系统代理设置。关闭会让漾路由与系统代理自动发现隔离。" },
  LITELLM_PORT: { label: "本地端口", help: "漾路由代理的本地 HTTP 端口。修改后会更新健康检查，且需要重启服务。" },
  LITELLM_NUM_WORKERS: { label: "工作进程数", help: "macOS 使用 Uvicorn 工作进程以保证本地 Responses 流稳定；其他主机可使用设定的进程数。" },
  LITELLM_HEALTH_WAIT_SECONDS: { label: "健康检查等待", help: "启动或重启时等待健康检查地址的最长时间；超过后刚启动的进程会被回收。" },
  YOUNG_ROUTER_RELAY_SESSION_RECHECK_SECONDS: { label: "中转站登录复核窗口", help: "同一次登录观察在该时间内会直接回答后续的登录检查。每次进入供应商详情都会复核账号的站点会话，而每次复核都是一次站点往返；在此窗口内会沿用刚验证过的会话，因此重新进入面板不会重新登录、不重读站点，也不会刷新登录状态。设为 0 则每次进入都重新复核。" },
  YOUNG_ROUTER_SERVICE_ACCOUNT_RECHECK_SECONDS: { label: "服务商账号复核窗口", help: "同一次账号读取在该时间内会直接回答后续的账号读取。选中 WorkBuddy 供应商时会读取其桌面 App 的账号与目录，而每次进入面板都会重读一次；在此窗口内会沿用刚读到的账号、积分与倍率，不会启动 worker、不重读凭据，也不请求上游。该区块自身的刷新按钮始终真实读取。设为 0 则每次进入都重新读取。" },
  YOUNG_ROUTER_RELAY_LOGIN_BACKOFF_SECONDS: { label: "中转站登录重试等待", help: "被中转站拒绝登录后，本应用等待多久才再次尝试。每次尝试都会让中转站签发一个新会话，而登录被拒最常见的原因就是该账号的同时在线会话数已达上限；因此立刻重试只会再次被拒，并让会话数更糟。已保存的会话始终先被探测，仍然有效就直接复用，所以这个等待只作用于真正要发出的登录。在账号面板中手动登录会立即尝试并清除等待。设为 0 则一旦被拒就立即重试。" },
  YOUNG_ROUTER_RELAY_SESSION_LIMIT_BACKOFF_SECONDS: { label: "中转站会话上限等待", help: "中转站回答“同时在线会话数已达上限”后，本应用多久不再尝试登录。这个拒绝与密码无关，也不会随上面的重试等待到期而消失：它持续到占满上限的那些会话失效为止，而站点签发这类会话大约是按天计算的。把这个等待设得比上限更长，未被人盯着的面板才不会反复请求中转站签发新会话——那正是账号被判定为异常登录的原因。请在其它设备退出登录后，从账号面板手动登录；该次尝试会立即发出并清除等待。设为 0 则回退到上面的重试等待。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_MAX_TOKENS: { label: "默认输出上限", help: "请求未指定输出上限时，集成运行时提供的最大输出标记数。上游模型目录可以给出更低的上限。修改后需重启漾路由。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_EXPOSE_REGION_MODELS: { label: "显示地区受限模型", help: "在上游目录中保留当前网络位置不可用的模型。关闭后，这些模型会从供应商及其路由选择器中隐藏。修改后需重启漾路由。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_REFRESH_INTERVAL_MINUTES: { label: "目录刷新间隔", help: "集成运行时刷新可用模型目录的间隔。修改后需重启漾路由。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_PROBE_ON_REFRESH: { label: "探测模型可用性", help: "刷新目录时同时探测各模型的可用性。默认关闭，以免例行刷新消耗上游免费额度。修改后需重启漾路由。" },
};

const options: Record<string, Record<string, string>> = {
  "*": { on: "开启", auto: "自动", manual: "手动", stdio: "本地客户端", http: "HTTP 端点", local: "本地", api: "API", off: "关闭", error: "直接报错", recovery: "恢复", recovery_cooldown: "恢复并冷却" },
};
const englishOptions: Record<string, string> = {
  on: "On",
  off: "Off",
  auto: "Auto",
  manual: "Manual",
  stdio: "Local client",
  http: "HTTP endpoint",
  local: "Local",
  api: "API",
  error: "Error",
  recovery: "Recover",
  recovery_cooldown: "Recover + cooldown",
};

function isChinese(translate: Translator): boolean {
  return translate("runtime.categories") === "运行时分类";
}

export function runtimeCategoryLabel(value: string, translate: Translator): string {
  return isChinese(translate) ? categories[value] ?? value : value;
}

export function runtimeFieldLabel(key: string, fallback: string, translate: Translator): string {
  return isChinese(translate) ? zh[key]?.label ?? fallback : fallback;
}

export function runtimeFieldHelp(key: string, fallback: string, translate: Translator): string {
  return isChinese(translate) ? zh[key]?.help ?? fallback : fallback;
}

export function runtimeUnitLabel(value: string, translate: Translator): string {
  return isChinese(translate) ? units[value] ?? value : value;
}

export function runtimeOptionLabel(key: string, value: string, translate: Translator): string {
  return isChinese(translate) ? options[key]?.[value] ?? options["*"][value] ?? value : englishOptions[value] ?? value;
}
