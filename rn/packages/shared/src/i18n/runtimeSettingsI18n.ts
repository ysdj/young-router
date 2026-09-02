import type { Translator } from "./types";

type RuntimeCopy = { label: string; help: string };

const categories: Record<string, string> = {
  "Relay": "中转站",
  "Timeouts": "超时",
  "Recovery": "恢复",
  "Web Search": "网页搜索",
  "Vision Router": "视觉路由",
  "Model Context": "模型上下文",
  "Fallback": "故障转移",
  "Computer Facade": "计算机操作",
  "MCP": "MCP",
  "Logs": "日志",
  "Network": "网络",
  "Service": "服务",
};

const units: Record<string, string> = {
  seconds: "秒",
  retries: "次重试",
  results: "条结果",
  pages: "页",
  chars: "字符",
  rounds: "轮",
  queries: "条查询",
  actions: "次操作",
  failures: "次失败",
  attempts: "次尝试",
  minutes: "分钟",
  steps: "步",
  MB: "MB",
  workers: "个工作进程",
  requests: "个请求",
  rows: "行",
  tokens: "标记",
  hours: "小时",
};

const zh: Record<string, RuntimeCopy> = {
  LITELLM_MENU_RELAY_AUTO_GROUP_INTERVAL_MINUTES: { label: "自动分组间隔", help: "服务商管理界面刷新并暂存自动 API 密钥分组的间隔。修改会在点击“应用”后生效。" },
  LITELLM_MENU_REQUEST_TIMEOUT_SECONDS: { label: "请求超时", help: "上游模型请求、续写合成和每次恢复探测的总超时。设为 0 可取消本地请求上限。" },
  LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS: { label: "首事件超时", help: "普通请求等待首个上游流事件的最长时间。设为 0 时回退到请求超时。" },
  LITELLM_MENU_CODEX_COMPACTION_START_TIMEOUT_SECONDS: { label: "压缩首事件超时", help: "结构化 Codex 压缩请求等待首个事件的最长时间。设为 0 时回退到请求超时。" },
  LITELLM_MENU_STALL_TIMEOUT_SECONDS: { label: "流空闲超时", help: "首个事件到达后，相邻流事件之间允许的最长间隔。设为 0 可取消本地流空闲上限。" },
  LITELLM_MENU_CODEX_DESCENDANT_CLEANUP: { label: "Codex 父线程屏障", help: "根任务结束前必须取得最新的完整后代线程快照。仍承担必要实现或测试的后代会被等待；已不影响交付的后代按最深层优先中断。关闭后仅使用 Codex 原生生命周期。" },
  LITELLM_MENU_RECOVERY_MAX_SECONDS: { label: "恢复最长时间", help: "路由恢复轮询的最长时间。所有路由冷却时保持连接和进度心跳，首个冷却结束后重试。设为 0 可关闭轮询。" },
  LITELLM_MENU_RECOVERY_INTERVAL_SECONDS: { label: "恢复探测间隔", help: "两次实际路由恢复探测之间的等待时间。" },
  LITELLM_MENU_SAME_DEPLOYMENT_RETRIES: { label: "同路由重试", help: "失败部署切换到下一个同级路由或顺序前的额外尝试次数。默认 0 会立即推进，也会覆盖 LiteLLM Router 的重试计数。" },
  LITELLM_MENU_RECOVERY_POLICY_BALANCE: { label: "余额 / 配额", help: "余额不足、配额或计费失败的处理方式。" },
  LITELLM_MENU_RECOVERY_POLICY_RATE_LIMIT: { label: "限流 / 过载", help: "HTTP 429、上游过载或容量不足的处理方式。默认恢复，不让部署进入冷却。" },
  LITELLM_MENU_RECOVERY_POLICY_SERVER: { label: "服务器 / 网关", help: "临时服务器故障、没有健康路由和网关超时的处理方式。" },
  LITELLM_MENU_RECOVERY_POLICY_NETWORK: { label: "网络", help: "断连、DNS 故障和连接错误的处理方式。默认恢复不会让部署进入冷却。" },
  LITELLM_MENU_RECOVERY_POLICY_STREAM_START_TIMEOUT: { label: "首事件超时", help: "上游首个流事件到达前发生本地超时时的处理方式。" },
  LITELLM_MENU_RECOVERY_POLICY_STREAM_IDLE_TIMEOUT: { label: "流空闲超时", help: "流已开始后发生本地超时时的处理方式。默认恢复不会让部署进入冷却。" },
  LITELLM_MENU_RECOVERY_POLICY_REQUEST_ERROR: { label: "请求 / 格式错误", help: "确定性的请求、格式、模型、策略和上下文错误的处理方式。默认错误会直接返回失败，以便修复代理配置而不是继续等待。" },
  LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS: { label: "网页抓取超时", help: "pi-web-access 搜索和页面抓取的超时，不限制模型生成。" },
  LITELLM_MENU_WEB_SEARCH_MAX_RESULTS: { label: "搜索结果数", help: "每次搜索操作返回的最大 pi-web-access 结果数。" },
  LITELLM_MENU_WEB_SEARCH_READ_CHARS: { label: "可读页面字符数", help: "模型明确打开结果页面后保留的最大 pi-web-access 页面字符数；模型发起页内查找时会扫描最多 12000 字符，以避免漏检。" },
  LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS: { label: "操作轮数", help: "一次回复允许模型主导网页搜索操作的最大轮数。" },
  LITELLM_MENU_WEB_SEARCH_MAX_QUERIES: { label: "查询总数", help: "所有网页搜索轮次合计允许的最大唯一查询数。" },
  LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES: { label: "打开页面数", help: "所有网页搜索轮次合计允许的最大显式打开页面次数。设为 0 可关闭打开页面。" },
  LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE: { label: "页内查找次数", help: "所有网页搜索轮次合计允许的最大页内查找次数。设为 0 可关闭页内查找。" },
  LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES: { label: "模型重试", help: "模型规划或综合桥接网页搜索时，对临时限流错误的重试次数。" },
  LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS: { label: "模型重试间隔", help: "临时网页搜索模型重试之间的基础等待时间。" },
  LITELLM_MENU_PI_WEB_ACCESS_CONFIG_JSON: { label: "pi-web-access 配置 JSON", help: "传给 pi-web-access 的 JSON 配置。配置会私密保存，并在代理启动前写入托管的 web-search.json 文件。" },
  LITELLM_MENU_DSH_VISION_ROUTER_ENABLED: { label: "启用视觉回退", help: "视觉回退总开关；此控件与高级 JSON 中的 enabled 会相互更新。" },
  LITELLM_MENU_DSH_VISION_ROUTER_BACKEND: { label: "视觉回退后端", help: "此控件与高级 JSON 中的 backend 会相互更新；自动会尝试本地和 HTTP 提供方，本地仅使用本地提供方 / OCR，API 仅使用配置的 HTTP 提供方。" },
  LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK: { label: "使用免费回退链", help: "在允许的后端中追加内置免密 HTTP 回退链；此控件与高级 JSON 中的 freeFallback 会相互更新。" },
  LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS: { label: "视觉回退超时", help: "每个视觉回退提供方请求允许的最长时间；此控件与高级 JSON 中的 timeoutSeconds 会相互更新。" },
  LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS: { label: "视觉回退标记数", help: "视觉回退请求最多生成的标记数；此控件与高级 JSON 中的 maxTokens 会相互更新。" },
  LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED: { label: "启用本地 Ollama", help: "在配置的 HTTP 提供方前尝试本地 Ollama；此控件与高级 JSON 中的 localOllama.enabled 会相互更新，端点和模型仍在高级 JSON 中设置。" },
  LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED: { label: "启用本地 LM Studio", help: "在配置的 HTTP 提供方前尝试本地 LM Studio；此控件与高级 JSON 中的 localLmStudio.enabled 会相互更新，端点和模型仍在高级 JSON 中设置。" },
  LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON: { label: "dsh-vision-router 配置", help: "可直接粘贴 JSON 的多行配置框，设置 backend（auto/local/api/off）、localOllama、localLmStudio、按顺序排列的 httpProviders（支持 apiKeyEnv），或 providers 中的 {provider,model,fallbacks}（vision-http/ovh 会映射到内置 HTTP 链；未知适配器会跳过），以及 freeFallback、prompt、timeoutSeconds 和 maxTokens。上方快捷控件与对应 JSON 字段会相互更新。仅当所选模型拒绝图像输入时运行；明确支持 vision 的模型仍直接收到原图。" },
  LITELLM_MENU_UNKNOWN_MODEL_CONTEXT_WINDOW: { label: "未知模型原始上下文", help: "当内置配置、实际路由、LiteLLM 与上游元数据都无法识别模型时，供 Codex 使用的原始上下文窗口。默认 272000，按 Codex 95% 的有效窗口策略会显示约 258k。" },
  LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS: { label: "模型上下文刷新间隔", help: "托管 Codex 模型目录刷新公开上下文元数据的间隔。设为 0 后仅使用内置配置与已有缓存，不访问上游。" },
  LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES: { label: "冷却失败阈值", help: "同一部署 / 协议对连续失败多少次后临时跳过。该部署的其他已配置协议仍可使用。设为 0 可关闭冷却。" },
  LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS: { label: "冷却时长", help: "失败部署 / 协议对达到阈值后被跳过的时长。仅当所有已配置协议都在冷却时，才会排除该部署。设为 0 可关闭冷却。" },
  LITELLM_MENU_PROTOCOL_FALLBACK_TTL_SECONDS: { label: "备用协议记忆", help: "某部署的备用协议成功后，在此时长内，同一种客户端协议直接复用该备用协议，不再重试原协议。设为 0 可关闭记忆。" },
  LITELLM_MENU_CODEX_COMPACTION_CAPABILITY_TTL_SECONDS: { label: "压缩能力记忆", help: "第三方 Responses 路由是否能返回加密 Codex 压缩结果的记忆时长。首次未验证的压缩会先发送不带历史的小探测；明确不支持时停止转发签名历史，并走 LiteLLM 模型选择所启用的 Codex 本地摘要检查点。设为 0 时每次压缩都重新探测。" },
  LITELLM_MENU_WEB_SEARCH_UNSUPPORTED_TTL_SECONDS: { label: "网页搜索不支持记忆", help: "某部署明确拒绝原生 web_search 后，在此时长内使用本机 pi-web-access 桥接，再到期重新探测原生搜索。瞬时网络、配额和提供方搜索失败不会写入该记忆；设为 0 可关闭跨请求记忆。" },
  LITELLM_MENU_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED: { label: "普通请求写入冷却", help: "普通请求失败时是否向共享部署冷却池累计失败。所有请求类型仍会读取已有冷却状态。" },
  LITELLM_MENU_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED: { label: "压缩请求写入冷却", help: "结构化 Codex 压缩失败时是否向共享部署冷却池累计失败。默认关闭，避免仅压缩时的上游失败让普通工作路由进入冷却。" },
  LITELLM_MENU_IMAGE_TOOL_UNSUPPORTED_TTL_SECONDS: { label: "图像工具不支持记忆", help: "某部署明确拒绝 image_generation 工具后，在此时长内跳过它。参数错误、超时、策略拒绝和其他上游错误不会写入该记忆；设为 0 可关闭跨请求记忆。" },
  LITELLM_MENU_COMPUTER_FACADE_BACKEND: { label: "后端", help: "执行器后端。明确选择后不会静默回退到其他真实后端。" },
  LITELLM_MENU_COMPUTER_FACADE_MODEL: { label: "规划模型", help: "内部 JSON 规划器可选的模型组或路由。留空时使用请求模型。" },
  LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS: { label: "最大步骤数", help: "在安全失败前，允许计算机观察 / 操作的最大轮数。" },
  LITELLM_MENU_COMPUTER_FACADE_TRACE: { label: "记录跟踪", help: "将操作摘要和后端选择记录到路由跟踪。" },
  LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS: { label: "记录截图", help: "涉及隐私：启用后截图以 0600 权限写入本地，而不是内嵌记录到日志。" },
  LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST: { label: "操作拒绝列表", help: "以逗号分隔要阻止的操作，例如 click、type、drag。" },
  LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION: { label: "要求观察结果", help: "要求执行器在规划完成或操作成功前提供观察结果。" },
  LITELLM_MENU_MCP_AUTO_APPROVE: { label: "自动同意 MCP 工具", help: "启用后将 Responses API 的 MCP 工具设置为 require_approval=never，使上游服务不再暂停等待交互式批准。默认关闭，因为这会绕过 MCP 安全审核。" },
  LITELLM_MENU_LOG_MAX_BYTES: { label: "本地日志文件上限", help: "本地日志单个文件的容量上限，包括最近请求、服务标准输出 / 错误和菜单操作。每份日志保留一个包含此前末尾内容的 .1 备份。" },
  LITELLM_MENU_LOG_VIEW_LIMIT: { label: "日志视图行数", help: "每个日志标签最多显示的行数。此设置只改变视图，不会删除本地日志数据。" },
  LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS: { label: "跟踪预览字符数", help: "始终开启的本地路由跟踪中保留的最大请求预览字符数。" },
  LITELLM_USE_SYSTEM_PROXIES: { label: "使用系统代理", help: "允许上游 HTTP 客户端使用 macOS 系统代理设置。关闭会让 LiteLLM 与系统代理自动发现隔离。" },
  LITELLM_PORT: { label: "本地端口", help: "LiteLLM 代理的本地 HTTP 端口。修改后会更新健康检查，且需要重启服务。" },
  LITELLM_NUM_WORKERS: { label: "工作进程数", help: "macOS 使用 Uvicorn 工作进程以保证本地 Responses 流稳定；其他主机可使用设定的进程数。" },
  LITELLM_MAX_REQUESTS_BEFORE_RESTART: { label: "工作进程请求回收", help: "每个 Gunicorn 工作进程处理到此请求数后重启，以限制长期内存增长。" },
  LITELLM_STATE_TTL_SECONDS: { label: "状态有效期", help: "启动 / 停止的临时状态被视为有效的时长。" },
  LITELLM_HEALTH_WAIT_SECONDS: { label: "健康检查等待", help: "启动或重启时等待健康检查端点的最长时间。" },
  LITELLM_RUNTIME_VERIFY_WAIT_SECONDS: { label: "运行时验证等待", help: "运行时配置验证允许等待的最长时间。" },
  LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS: { label: "生命周期锁等待", help: "并发启动、重启或应用配置操作允许等待的最长时间。" },
};

const options: Record<string, Record<string, string>> = {
  "*": { on: "开启", auto: "自动", local: "本地", api: "API", off: "关闭", error: "直接报错", recovery: "恢复", recovery_cooldown: "恢复并冷却", compact: "紧凑", detailed: "详细", mcp: "MCP", browser: "内置浏览器", chrome: "Chrome", playwright: "Playwright", mock: "模拟" },
};
const englishOptions: Record<string, string> = {
  on: "On",
  off: "Off",
  auto: "Auto",
  local: "Local",
  api: "API",
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

export const runtimeLocalizedKeys = Object.freeze(Object.keys(zh));
