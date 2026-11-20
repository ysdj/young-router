import type { Translator } from "./types";

type RuntimeCopy = { label: string; help: string };

const categories: Record<string, string> = {
  "Timeouts": "超时",
  "Recovery": "恢复与续写",
  "Web Search": "网页搜索",
  "Vision Router": "视觉回退",
  "Model Context": "模型上下文",
  "Image Context": "图片上下文",
  "Fallback": "路由与故障转移",
  "MCP": "MCP",
  "Logs": "日志",
  "Network": "网络",
  "Service": "服务",
  "Our Free Model": "Our Free Model",
};

const units: Record<string, string> = {
  seconds: "秒",
  retries: "次重试",
  chars: "字符",
  rounds: "轮",
  turns: "轮",
  failures: "次失败",
  requests: "个并发",
  MB: "MB",
  workers: "个工作进程",
  rows: "行",
  tokens: "令牌",
  hours: "小时",
  minutes: "分钟",
  bytes: "字节",
  items: "条",
  segments: "段",
};

const zh: Record<string, RuntimeCopy> = {
  YOUNG_ROUTER_REQUEST_TIMEOUT_SECONDS: { label: "请求超时", help: "上游模型请求、续写合成和每次恢复探测的总超时。设为 0 可取消本地请求上限。" },
  YOUNG_ROUTER_STREAM_START_TIMEOUT_SECONDS: { label: "首事件超时", help: "普通流式请求最后的静默预算：先等响应头，拿到头后再等第一个事件。组里有兄弟线路、且兄弟抢跑开启时，路由器会在默认约 11.5–20 秒并行问一条，健康回合通常等不到这个值；它约束的是只有一条线路、或兄弟全部失败的组。整段静默达到这个值会记 1 次失败。设为 0 时回退到「请求超时」。" },
  YOUNG_ROUTER_HEAVY_REPLAY_STREAM_START_TIMEOUT_SECONDS: { label: "重前缀首事件超时", help: "重放前缀序列化后超过重前缀阈值的 Responses 请求，改用此更长的首事件预算；多 MB 的签名历史会让所有路由同时推迟首个事件。设为 0 时仅使用首事件超时。" },
  YOUNG_ROUTER_HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES: { label: "重前缀阈值", help: "重放负载（input 加 instructions）序列化后超过该字节数时启用重前缀首事件超时。设为 0 可完全关闭加长预算。" },
  YOUNG_ROUTER_WEB_PASSWORD: { label: "Web 密钥", help: "浏览器打开本地端口上的页面时用来登录的密钥。留空时自动生成。在「常规」中设置。" },
  YOUNG_ROUTER_WEBSOCKET_MAX_FRAME_BYTES: { label: "WebSocket 帧上限", help: "本地代理接受的单个 WebSocket 消息帧的最大字节数。Codex 会把整轮请求（含签名历史）作为单帧发送，上限过小会在进入路由前被 1009 关闭并触发重连循环。修改后需重启服务生效。" },
  YOUNG_ROUTER_CODEX_COMPACTION_START_TIMEOUT_SECONDS: { label: "压缩首事件超时", help: "结构化 Codex 压缩请求等待首个事件的最长时间。设为 0 时回退到请求超时。" },
  YOUNG_ROUTER_STALL_TIMEOUT_SECONDS: { label: "流空闲超时", help: "首个事件到达后，相邻流事件之间允许的最长间隔。设为 0 可取消本地流空闲上限。" },
  YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS: { label: "流保活间隔", help: "交付流静默期间按此间隔发送一个客户端可解析的保活事件，避免客户端字节空闲计时器早于代理预算触发。设为 0 可关闭保活。" },
  YOUNG_ROUTER_STREAM_HEDGE_ENABLED: { label: "兄弟抢跑", help: "第一个事件迟迟不来时，用同一份请求在兄弟线路再发一次，谁先回答就用谁。首个尝试不会被取消；输掉竞速的线路记 1 次普通失败。关闭后连响应头竞速也一起停用。生图、组里只有一条线路、以及重试轮次都不补发。" },
  YOUNG_ROUTER_STREAM_HEDGE_DELAY_SECONDS: { label: "兄弟抢跑等待", help: "第一个事件等多久后让兄弟线路加入。更早的响应头竞速受此值封顶，体积小的请求体会提前（10 秒加上传时间）。设为 0 即停用，关闭上面的开关也一样。" },
  YOUNG_ROUTER_STREAM_CONTINUATION_ENABLED: { label: "续写客户端上限的回答", help: "路由在自己的输出上限处截断回答时，路由器总会向同一路由索取剩余内容并接进正在阅读的答案。这个开关还会续写停在客户端自己声明上限处的回答——那会超出客户端选择的上限，所以默认关闭。仅适用于纯文本 Chat Completions 与 Anthropic 回答；带工具的轮次不受影响。" },
  YOUNG_ROUTER_STREAM_CONTINUATION_MAX_ROUNDS: { label: "续写轮数上限", help: "一条回答因路由撞到自身输出上限而最多可被续写的次数。每轮都是一次完整的上游调用，从已交付内容继续。设为 0 时保留路由留下的截断结果。" },
  YOUNG_ROUTER_REPLAY_REASONING_ELISION_ENABLED: { label: "裁剪过大的重放", help: "上游以「重放太大」拒绝后，删掉最老的推理项再试一次。消息、工具调用与结果、图片和托管工具都按原序保留；只裁推理，且只在被拒绝之后。关闭后重放保持原样。" },
  YOUNG_ROUTER_REPLAY_REASONING_ELISION_KEEP: { label: "保留推理条数", help: "裁剪重放时保留最新多少条推理项。设为 0 表示重试时删掉全部推理项。" },
  YOUNG_ROUTER_CODEX_DESCENDANT_CLEANUP: { label: "Codex 父线程屏障", help: "根任务结束前必须取得最新的完整后代线程快照。仍承担必要实现或测试的后代会被等待；已不影响交付的后代按最深层优先中断。关闭后仅使用 Codex 原生生命周期。" },
  YOUNG_ROUTER_RECOVERY_MAX_SECONDS: { label: "恢复最长时间", help: "路由恢复轮询的最长时间。所有路由冷却时保持连接和进度心跳，首个冷却结束后重试。设为 0 可关闭轮询。" },
  YOUNG_ROUTER_RECOVERY_INTERVAL_SECONDS: { label: "恢复探测间隔", help: "两次实际路由恢复探测之间的等待时间。" },
  YOUNG_ROUTER_SAME_DEPLOYMENT_RETRIES: { label: "同路由重试", help: "失败部署切换到下一个同级路由或 order 层之前的额外尝试次数。默认 0 会立即推进，也会覆盖 LiteLLM Router 的重试计数。" },
  YOUNG_ROUTER_RECOVERY_POLICY_BALANCE: { label: "余额 / 配额处理", help: "余额不足、配额或计费失败的处理方式。" },
  YOUNG_ROUTER_RECOVERY_POLICY_RATE_LIMIT: { label: "限流 / 过载处理", help: "HTTP 429、上游过载或容量不足的处理方式。默认冷却：连续失败达到「冷却失败次数」后暂停该部署，后续请求直接交给能服务的路由，不再每次都先吃同一个拒绝。设为「恢复」则每次都在原路由重试。" },
  YOUNG_ROUTER_RECOVERY_POLICY_SERVER: { label: "服务器 / 网关处理", help: "临时服务器故障、没有健康路由和网关超时的处理方式。" },
  YOUNG_ROUTER_RECOVERY_POLICY_NETWORK: { label: "网络处理", help: "断连、DNS 故障和连接错误的处理方式。默认恢复不会让部署进入冷却。" },
  YOUNG_ROUTER_RECOVERY_POLICY_UNREACHABLE: { label: "上游不可达处理", help: "一次根本没到达主机的尝试如何处理：域名解析失败、握手被拒或超时、拿不到响应头。与「网络」不同，这不是答完之后又断掉的路由——一个字节都没交换过，兄弟路由能服务同一份字节，所以这条路由会被停用，下一个请求不必再付一次同样的沉默。" },
  YOUNG_ROUTER_RECOVERY_POLICY_STREAM_START_TIMEOUT: { label: "首事件超时处理", help: "上游首个流事件到达前发生本地超时时的处理方式。" },
  YOUNG_ROUTER_RECOVERY_POLICY_STREAM_IDLE_TIMEOUT: { label: "流空闲超时处理", help: "流已开始后发生本地超时时的处理方式。默认恢复不会让部署进入冷却。" },
  YOUNG_ROUTER_RECOVERY_POLICY_REQUEST_ERROR: { label: "请求 / 格式错误处理", help: "确定性的请求、格式、模型、策略和上下文错误的处理方式。默认错误会直接返回失败，以便修复代理配置而不是继续等待。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED: { label: "启用视觉回退", help: "视觉回退总开关。快捷控件与高级 JSON 的 enabled 双向同步。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND: { label: "视觉回退后端", help: "视觉回退后端。自动会尝试本地与 HTTP 提供方，本地仅用本地提供方 / OCR，API 仅用配置的 HTTP 提供方，关闭则停用视觉回退。快捷控件与高级 JSON 的 backend 双向同步。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK: { label: "使用免费回退链", help: "在允许的后端中追加内置免密 HTTP 回退链。快捷控件与高级 JSON 的 freeFallback 双向同步。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS: { label: "视觉回退超时", help: "每个视觉回退提供方请求的最长时间。留空则使用高级 JSON 中的 timeoutSeconds。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS: { label: "视觉回退令牌数", help: "视觉回退请求最多生成的令牌数。留空则使用高级 JSON 中的 maxTokens。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED: { label: "启用本地 Ollama", help: "在配置的 HTTP 提供方之前尝试本地 Ollama。快捷控件与高级 JSON 的 localOllama.enabled 双向同步；地址和模型仍在 JSON 中设置。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED: { label: "启用本地 LM Studio", help: "在配置的 HTTP 提供方之前尝试本地 LM Studio。快捷控件与高级 JSON 的 localLmStudio.enabled 双向同步；地址和模型仍在 JSON 中设置。" },
  YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON: { label: "视觉回退配置", help: "视觉回退链的高级 JSON：backend、providers / httpProviders、apiKeyEnv、prompt、顺序、超时与令牌上限、本地提供方。上方快捷控件与对应字段双向同步。仅在所选模型拒绝图像输入时运行；声明支持视觉的模型直接收到原图。" },
  YOUNG_ROUTER_UNKNOWN_MODEL_CONTEXT_WINDOW: { label: "未知模型原始上下文", help: "当内置配置、实际路由、漾路由与上游元数据都无法识别模型时，供 Codex 使用的原始上下文窗口。默认 272000，按 Codex 95% 的有效窗口策略会显示约 258k。" },
  YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS: { label: "模型上下文刷新间隔", help: "托管 Codex 模型目录刷新公开上下文元数据的间隔。设为 0 后仅使用内置配置与已有缓存，不访问上游。" },
  YOUNG_ROUTER_CATALOG_BASE_PROFILE: { label: "模型目录基础配置", help: "内部记录：第三方 Codex 模型别名继承的原生配置。由 Core 写入，仅当已安装客户端不再提供该配置时才会更新；请勿手动修改。" },
  YOUNG_ROUTER_MODEL_CONCURRENCY_LIMIT: { label: "模型并发上限", help: "同一条路由最多同时服务多少个请求，超过后由同一公开模型的下一条路由接手，和路由冷却一样被跳过。某个模型的每条路由都满时，这一轮会排队等待（客户端流保持开启），名额空出来后给等待最久的那一轮，而不是改用别的模型或直接报错。模型详情里可以给单条路由单独设置数字；没有单独设置的路由都会跟随这里的值，所以修改它会改变所有没单独设置的路由。设为 0 可完全关闭该限制。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FAILURES: { label: "冷却失败阈值", help: "同一部署连续失败多少次后临时跳过。设为 0 可关闭冷却。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_SECONDS: { label: "冷却时长", help: "失败的部署达到阈值后被跳过的时长。设为 0 可关闭冷却。" },
  YOUNG_ROUTER_STREAM_START_TIMEOUT_COOLDOWN_SECONDS: { label: "重前缀超时冷却", help: "重前缀线路花完整个重前缀首事件预算、始终没有产出事件时，被停用的时长。其他首事件超时使用「冷却时长」。" },
  YOUNG_ROUTER_QUOTA_RESET_COOLDOWN_HORIZON_SECONDS: { label: "429 恢复阈值", help: "当 429 给出恢复时间时，等待长于该值就按「冷却时长」停用该路由，不再优先重试；更短、或未说明恢复时间的 429 仍按「限流 / 过载」处理。设为 0 表示只要说明恢复时间就停用。" },
  YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY: { label: "会话部署亲和", help: "让同一会话在最低可用 order 层内继续使用首次成功服务的部署，既能复用提示缓存，也不会让旧部署越过更优且健康的线路。关闭后每轮完全按路由策略选择。" },
  YOUNG_ROUTER_PROTOCOL_FALLBACK_TTL_SECONDS: { label: "备用协议记忆", help: "某部署的备用协议成功后，在此时长内，同一种客户端协议直接复用该备用协议，不再重试原协议。设为 0 可关闭记忆。" },
  YOUNG_ROUTER_CODEX_COMPACTION_CAPABILITY_TTL_SECONDS: { label: "压缩能力记忆", help: "Responses 路由的加密 Codex 压缩能力记忆时长。新建任务默认使用本地检查点摘要；只有显式声明 supports_responses_compaction opt-in 的模型才会公布精确的 OpenAI 名，其路由在转发签名历史前仍会用不带历史的小探测验证（官方 OpenAI 主机跳过探测）。明确拒绝的路由会切换到其他候选；所有候选都拒绝时返回明确的协议错误。已在运行的任务保留创建时缓存的能力。设为 0 时每次压缩都重新探测。" },
  YOUNG_ROUTER_WEB_SEARCH_UNSUPPORTED_TTL_SECONDS: { label: "托管搜索不支持记忆", help: "某部署明确拒绝托管的 web_search 后，在此时长内不再附加该声明，改由 Chat 协议桥接完成这一轮，避免每次请求都重复同一拒绝。瞬时网络、配额和提供方搜索失败不会写入该记忆；设为 0 可关闭跨请求记忆。" },
  YOUNG_ROUTER_ALPHA_SEARCH_UNSUPPORTED_TTL_SECONDS: { label: "独立搜索不支持记忆", help: "Codex 的 Responses Lite 通道不会下发托管 web_search，只调用 provider 的 /alpha/search。某条路由明确回答没有该端点后，在此时长内直接跳过它，避免每轮都重复同一次拒绝；超时、5xx 等瞬时失败不会写入该记忆。设为 0 则每次搜索都重新探测。" },
  YOUNG_ROUTER_REASONING_COMPAT_TTL_SECONDS: { label: "推理参数兼容记忆", help: "某部署记住「用哪种请求形态携带客户端推理参数」的时长。Gemini 系上游对别名模型拒绝 OpenAI 推理字段时，先改用原生 Google 思考配置重放，保留客户端请求的档位；其他路由则改用不带推理字段的重放，以上游默认思考方式运行。只有上游明确拒绝该模型的思考配置时才会写入该记忆；设为 0 可关闭跨请求记忆（每次请求重新重放一次）。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED: { label: "普通请求写入冷却", help: "普通请求失败时是否向共享部署冷却池累计失败。所有请求类型仍会读取已有冷却状态。" },
  YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED: { label: "压缩请求写入冷却", help: "结构化 Codex 压缩失败时是否向共享部署冷却池累计失败。默认关闭，避免仅压缩时的上游失败让普通工作路由进入冷却。" },
  YOUNG_ROUTER_IMAGE_TOOL_UNSUPPORTED_TTL_SECONDS: { label: "图像工具不支持记忆", help: "某部署明确拒绝 image_generation 工具后，在此时长内跳过它。参数错误、超时、策略拒绝和其他上游错误不会写入该记忆；设为 0 可关闭跨请求记忆。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_ENABLED: { label: "Codex 图片上下文", help: "之前各轮的图换成保留编号、轮次、尺寸的占位符；本轮附上的图不动。模型用 view_image_by_id 按编号取回。编号与选图沿用 Context Attachment Manager；默认关闭。" },
  YOUNG_ROUTER_CLAUDE_IMAGE_CONTEXT_ENABLED: { label: "Claude 图片上下文", help: "之前各轮的图换成保留编号、轮次、尺寸的占位符；本轮附上的图不动。模型写“需要 IMG-xxx”，下一轮由代理把原图放回。默认关闭。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MODE: { label: "Codex 图片选择模式", help: "自动：省略之前各轮的图。手动：只省略在 Context Attachment Manager 里取消勾选的图。Claude 始终自动选择。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH: { label: "取图方式", help: "Codex 被省略的图如何取回：自动（文件或端点）、文件、本地 MCP、HTTP 端点、或提示用户重发。Claude 始终使用“需要 IMG-xxx”。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_INSIDE_TURN: { label: "本轮内的图片", help: "「也处理本轮」还会省略本轮内较早的工具结果图，只保留一个可见窗口；「只处理历史」则本轮原样发送。用户在本轮附上的图始终不动。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_KEEP_RECENT: { label: "保留最近轮数", help: "最近多少轮的图保持内联；0 表示省略之前所有轮。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MIN_BYTES: { label: "省略大于", help: "小于该字节数的图保持内联；占位符加之后取回的开销比省下的字节更大。0 表示所有图都参与省略。" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR: { label: "选图目录", help: "Context Attachment Manager 的选图目录；留空使用 CAM_DATA_DIR 或平台默认目录。仅用于 Codex。" },
  YOUNG_ROUTER_MCP_AUTO_APPROVE: { label: "自动同意 MCP 工具", help: "启用后将 Responses API 的 MCP 工具设置为 require_approval=never，使上游服务不再暂停等待交互式批准。默认关闭，因为这会绕过 MCP 安全审核。" },
  YOUNG_ROUTER_LOG_MAX_BYTES: { label: "本地日志文件上限", help: "本地日志单个文件的容量上限，包括最近请求、服务标准输出 / 错误和状态项操作。轮转时按设定的备份段数保留 .1..N 旧末尾内容。" },
  YOUNG_ROUTER_LOG_BACKUP_SEGMENTS: { label: "日志备份段数", help: "本地日志轮转时保留的历史末尾段数（.1..N）。设为 1 时只保留紧接着的上一段；当前文件始终保留。" },
  YOUNG_ROUTER_LOG_VIEW_LIMIT: { label: "日志视图行数", help: "每个日志标签最多显示的行数。此设置只改变视图，不会删除本地日志数据。" },
  YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS: { label: "请求行中断窗口", help: "请求行在多长时间内没有进展后会被标记为已中断。客户端断开、故障转移拆连或服务重启都可能让上游尝试没有终态回调；此窗口会结束这些行，而不是一直显示“发送中”。设为 0 则保持未完成行不结束。" },
  YOUNG_ROUTER_ROUTE_TRACE_PREVIEW_CHARS: { label: "跟踪预览字符数", help: "始终开启的本地路由跟踪中保留的最大请求预览字符数。" },
  LITELLM_USE_SYSTEM_PROXIES: { label: "使用系统代理", help: "允许上游 HTTP 客户端使用系统代理设置。关闭会让漾路由与系统代理自动发现隔离。" },
  LITELLM_PORT: { label: "本地端口", help: "本地代理的 HTTP 端口。端口被占用时服务会顺延到下一个空闲端口。请到「常规」中设置。" },
  LITELLM_NUM_WORKERS: { label: "工作进程数", help: "本地代理的 Uvicorn 工作进程数。进程越多可承担的并发越高，内存占用也越大。修改后需重启服务。" },
  LITELLM_HEALTH_WAIT_SECONDS: { label: "健康检查等待", help: "启动或重启时等待健康检查地址的最长时间；超过后刚启动的进程会被回收。" },
  YOUNG_ROUTER_RELAY_SESSION_RECHECK_SECONDS: { label: "中转站登录复核窗口", help: "同一次登录观察在该时间内会直接回答后续的登录检查。每次进入供应商详情都会复核账号的站点会话，而每次复核都是一次站点往返；在此窗口内会沿用刚验证过的会话，因此重新进入面板不会重新登录、不重读站点，也不会刷新登录状态。设为 0 则每次进入都重新复核。" },
  YOUNG_ROUTER_RELAY_NEGATIVE_BALANCE_REFRESH_INTERVAL_MINUTES: { label: "负余额账号查询间隔", help: "按此间隔复查上次读到负余额的中转站账号。只有成功读到且仍在有效期内的负余额才会让路由跳过该账号的模型。" },
  YOUNG_ROUTER_RELAY_SIGNED_IN_BALANCE_REFRESH_INTERVAL_MINUTES: { label: "已登录账号余额查询间隔", help: "按此间隔查询已登录中转站账号的余额。上次读到负余额的账号使用负余额查询间隔。" },
  YOUNG_ROUTER_SERVICE_ACCOUNT_RECHECK_SECONDS: { label: "服务商账号复核窗口", help: "同一次账号读取在该时间内会直接回答后续的账号读取。选中 WorkBuddy 或 Qoder 供应商时会读取其账号与目录，而每次进入面板都会重读一次；在此窗口内会沿用刚读到的账号、积分与倍率，不会启动 worker、不重读凭据，也不请求上游。该区块自身的刷新按钮始终真实读取。设为 0 则每次进入都重新读取。" },
  YOUNG_ROUTER_RELAY_LOGIN_BACKOFF_SECONDS: { label: "中转站登录重试等待", help: "被中转站拒绝登录后，本应用等待多久才再次尝试。每次尝试都会让中转站签发一个新会话，而登录被拒最常见的原因就是该账号的同时在线会话数已达上限；因此立刻重试只会再次被拒，并让会话数更糟。已保存的会话始终先被探测，仍然有效就直接复用，所以这个等待只作用于真正要发出的登录。在账号面板中手动登录会立即尝试并清除等待。设为 0 则一旦被拒就立即重试。" },
  YOUNG_ROUTER_RELAY_SESSION_LIMIT_BACKOFF_SECONDS: { label: "中转站会话上限等待", help: "中转站回答“同时在线会话数已达上限”后，本应用多久不再尝试登录。这个拒绝与密码无关，也不会随上面的重试等待到期而消失：它持续到占满上限的那些会话失效为止，而站点签发这类会话大约是按天计算的。把这个等待设得比上限更长，未被人盯着的面板才不会反复请求中转站签发新会话——那正是账号被判定为异常登录的原因。请在其它设备退出登录后，从账号面板手动登录；该次尝试会立即发出并清除等待。设为 0 则回退到上面的重试等待。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_MAX_TOKENS: { label: "默认输出上限", help: "请求未指定输出上限时，集成运行时提供的最大输出令牌数。上游目录可以给出更低的上限。保存后会重启该 worker，下一次请求生效。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_EXPOSE_REGION_MODELS: { label: "显示地区受限模型", help: "在上游目录中保留当前网络位置不可用的模型。关闭后，这些模型会从供应商及其路由选择器中隐藏。保存后会重启该 worker，目录随之更新。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_REFRESH_INTERVAL_MINUTES: { label: "目录刷新间隔", help: "集成运行时刷新可用模型目录的间隔。保存后会重启该 worker，新的间隔生效。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_PROBE_ON_REFRESH: { label: "探测模型可用性", help: "刷新目录时同时探测各模型的可用性。默认关闭，以免例行刷新消耗上游免费额度。保存后会重启该 worker，新的行为生效。" },
  YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR: { label: "上游源码目录", help: "开发与验证时显式指定 Our Free Model 源码目录。留空使用随 Young Router 打包的副本。" },
};

const options: Record<string, Record<string, string>> = {
  "*": { on: "开启", auto: "自动", manual: "手动", path: "文件路径（不改 Codex 配置）", stdio: "本地 MCP（会改 Codex 配置）", http: "HTTP 端点", local: "本地", api: "API", off: "关闭", error: "直接报错", recovery: "恢复", recovery_cooldown: "恢复并冷却", inherit: "跟随（高级 JSON）", turn: "也处理本轮", history: "只处理历史" },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH: { off: "提示用户重发" },
};
const englishOptions: Record<string, Record<string, string>> = {
  "*": {
    on: "On",
    off: "Off",
    auto: "Auto",
    manual: "Manual",
    path: "File path (no config change)",
    stdio: "Local MCP (edits Codex config)",
    http: "HTTP endpoint",
    local: "Local",
    api: "API",
    error: "Error",
    recovery: "Recover",
    recovery_cooldown: "Recover + cooldown",
    inherit: "Inherit (advanced JSON)",
    turn: "Also this turn",
    history: "History only",
  },
  YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH: { off: "Ask the user" },
};

function isChinese(translate: Translator): boolean {
  // A CJK code point in any translated value, not a copy of one entry: this
  // probe compared against the exact Chinese spelling of `runtime.categories`,
  // so rewording that one heading — or fixing its punctuation — silently made
  // the whole Runtime pane answer in English while the rest of the window
  // stayed Chinese.  A code point cannot collide with an English dictionary
  // value, which is what makes this a language test rather than a copy.
  return /[一-鿿]/u.test(translate("common.empty"));
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
  return isChinese(translate)
    ? options[key]?.[value] ?? options["*"][value] ?? value
    : englishOptions[key]?.[value] ?? englishOptions["*"][value] ?? value;
}
