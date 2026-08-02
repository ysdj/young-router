import type { Translator } from "./types";

export type AssistantSettingOption = { value: string; label: string };

const featureLabels: Record<string, string> = {
  fast_mode: "快速模式",
  goals: "目标",
  apps: "应用",
  plugins: "插件",
  plugin_sharing: "插件共享",
  hooks: "钩子",
  collab: "协作",
  collaboration_modes: "协作模式",
  computer_use: "计算机操作",
  browser_use: "浏览器操作",
  in_app_browser: "内置浏览器",
  image_generation: "图像生成",
  multi_agent: "多智能体",
  multi_agent_mode: "多智能体模式",
  connectors: "连接器",
  memories: "记忆",
  request_permissions: "请求权限",
  web_search: "网页搜索",
  network_proxy: "网络代理",
  prevent_idle_sleep: "防止闲置休眠",
  remote_models: "远程模型",
  remote_plugin: "远程插件",
  code_mode: "代码模式",
  js_repl: "JavaScript REPL",
  experimental_use_unified_exec_tool: "统一执行工具（实验性）",
  shell_snapshot: "Shell 快照",
  shell_tool: "Shell 工具",
  skill_mcp_dependency_install: "技能 / MCP 依赖安装",
  personality: "个性",
};

const optionLabels: Record<string, string> = {
  "": "（空）",
  auto: "自动",
  file: "文件",
  keyring: "钥匙串",
  ephemeral: "临时",
  chatgpt: "ChatGPT",
  api: "API",
  minimal: "最低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  none: "无",
  concise: "简洁",
  detailed: "详细",
  friendly: "友好",
  pragmatic: "务实",
  fast: "快速",
  flex: "弹性",
  disabled: "关闭",
  cached: "缓存",
  indexed: "索引",
  live: "实时",
  legacy: "兼容模式",
  profile: "配置档案",
  unset: "未设置",
  "read-only": "只读",
  "workspace-write": "工作区可写",
  "danger-full-access": "完全访问",
  untrusted: "不受信任",
  "on-request": "按请求",
  never: "从不",
  user: "用户",
  auto_review: "自动审查",
  responses: "OpenAI Responses",
  env_key: "环境变量密钥",
  openai_auth: "OpenAI 登录",
  command: "命令",
  bearer: "Bearer 令牌",
  stdio: "标准输入输出",
  http: "HTTP",
  all: "全部",
  core: "核心",
  "save-all": "保存全部",
  openai: "OpenAI",
  "amazon-bedrock": "Amazon Bedrock",
  ollama: "Ollama",
  lmstudio: "LM Studio",
  vscode: "VS Code",
  "vscode-insiders": "VS Code Insiders",
  windsurf: "Windsurf",
  cursor: "Cursor",
  latest: "最新",
  stable: "稳定",
  normal: "普通",
  vim: "Vim",
  bash: "Bash",
  powershell: "PowerShell",
  dark: "深色",
  light: "浅色",
  "dark-daltonized": "深色（色觉辅助）",
  "light-daltonized": "浅色（色觉辅助）",
  "dark-ansi": "深色 ANSI",
  "light-ansi": "浅色 ANSI",
  default: "默认",
  verbose: "详细",
  focus: "专注",
  fullscreen: "全屏",
  "in-process": "进程内",
  tmux: "tmux",
  iterm2: "iTerm2",
  terminal_bell: "终端铃声",
  iterm2_with_bell: "iTerm2（带铃声）",
  kitty: "kitty",
  ghostty: "Ghostty",
  terminal: "终端",
  notifications_disabled: "关闭通知",
  "60s": "60 秒",
  "5m": "5 分钟",
  "10m": "10 分钟",
  unrestricted: "不限制",
  small: "小",
  large: "大",
  append: "追加",
  replace: "替换",
  fresh: "新建",
  head: "当前 HEAD",
  worktree: "工作树",
};

const validationLabels: Record<string, string> = {
  "sandbox_mode must be read-only, workspace-write, or danger-full-access": "沙箱模式只能是“只读”“工作区可写”或“完全访问”。",
  "features must be a TOML table": "功能开关必须是 TOML 表。",
  "model_providers must be a TOML table": "模型供应商必须是 TOML 表。",
  "mcp_servers must be a TOML table": "MCP 服务器必须是 TOML 表。",
  "auth.json must be a JSON object": "auth.json 必须是 JSON 对象。",
  "A custom provider uses a reserved built-in provider id": "自定义供应商使用了保留的内置供应商 ID。",
  "Each custom provider must be a TOML table": "每个自定义供应商必须是 TOML 表。",
  "A custom provider wire_api must be responses": "自定义供应商的 wire_api 必须为 OpenAI Responses。",
  "Each MCP server must be a TOML table": "每个 MCP 服务器必须是 TOML 表。",
  "An MCP server cannot define both command and url": "MCP 服务器不能同时设置 command 和 url。",
};

function isChinese(translate: Translator): boolean {
  return translate("common.empty") === "（空）";
}

export function assistantSettingOptions(values: Array<string | AssistantSettingOption>, translate: Translator): AssistantSettingOption[] {
  return values.map((option) => {
    if (typeof option !== "string") return option;
    return { value: option, label: isChinese(translate) ? optionLabels[option] ?? option : option };
  });
}

export function codexFeatureLabel(key: string, translate: Translator): string {
  if (!isChinese(translate)) return key;
  return featureLabels[key] ?? "其他功能（请在原始配置中编辑）";
}

export function localizeCodexValidationMessage(message: string, translate: Translator): string {
  if (!isChinese(translate)) return message;
  if (validationLabels[message]) return validationLabels[message];
  const featureType = /^features\.([^ ]+) must be true or false$/.exec(message);
  if (featureType) return `功能“${codexFeatureLabel(featureType[1], translate)}”只能设为启用或关闭。`;
  const unknownFeature = /^features\.([^ ]+) is not editable in the structured UI$/.exec(message);
  if (unknownFeature) return `功能“${codexFeatureLabel(unknownFeature[1], translate)}”只能在原始配置中编辑。`;
  return "Codex 配置校验失败；请在右侧原始文件中修复后再应用。";
}
