"""Language preference and translation domain shared by both native shells."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
from collections.abc import Mapping
from typing import Any, Callable


DOMAIN_NAME = "language"
LANGUAGE_OPTIONS = ("system", "en", "zh-Hans")
RESOLVED_LANGUAGES = ("en", "zh-Hans")


class LanguageSettingsError(ValueError):
    """An error safe to send over IPC."""


def normalize_system_language(system_locale: str | None) -> str:
    value = str(system_locale or "").strip().lower().replace("_", "-")
    return "zh-Hans" if value.startswith("zh-") or value == "zh" else "en"


def resolve_language(choice: str | None, system_locale: str | None = None) -> str:
    value = str(choice or "system").strip()
    if value == "system":
        return normalize_system_language(system_locale)
    if value in {"en", "English", "english", "en-US", "en-GB"}:
        return "en"
    if value in {"zh-Hans", "zh", "zh-CN", "zh-SG", "简体中文", "Chinese"}:
        return "zh-Hans"
    raise LanguageSettingsError("Unsupported language")


MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "app.title": "LiteLLM Menu",
        "menu.providers": "Providers & Models",
        "menu.codex": "Codex / Claude Settings",
        "menu.claude": "Claude Settings",
        "menu.runtime": "Runtime Settings",
        "menu.webdav": "WebDAV Sync Settings",
        "menu.logs": "Logs",
        "menu.logsSummary": "Logs (route recovery {recovering}, cooldown {cooldown})",
        "menu.language": "Language Settings",
        "menu.reload": "Reload",
        "menu.close": "Close",
        "menu.apply": "Apply",
        "menu.cancel": "Cancel",
        "menu.autoStart": "Auto Start at Login",
        "menu.quit": "Quit LiteLLM Menu",
        "language.title": "Language Settings",
        "language.system": "System",
        "language.english": "English",
        "language.simplified_chinese": "简体中文",
        "claude.title": "Claude Settings",
        "claude.model": "Model",
        "claude.gateway": "LiteLLM gateway",
        "claude.permissions": "Permissions",
        "claude.sandbox": "Bash sandbox",
        "claude.filesystem": "File system access",
        "codex.network": "Network access",
        "claude.confirmation.required": "Explicit confirmation is required for the selected Claude permissions",
        "validation.invalid_settings": "Claude Settings contains invalid values",
        "validation.invalid_language": "Unsupported language",
        "logs.requests": "Requests",
        "logs.service": "Service",
        "logs.menu": "Menu",
        "logs.route_trace": "Route Trace",
        "logs.recovery": "Recovery / Cooldown",
        "logs.online_usage": "Online Usage",
        "error.generic": "Something went wrong",
        "common.secureEditorLoading": "Loading document...",
        "common.secureEditorReadFailed": "The document could not be loaded.",
        "common.secureEditorStageFailed": "The latest edits could not be staged. Your text remains in this editor.",
        "service.starting": "Starting",
        "service.running": "Running",
        "service.runningOnPort": "Running (port {port})",
        "service.unhealthy": "Unhealthy",
        "service.stopped": "Stopped",
        "service.unknown": "Unknown",
        "service.status": "Status: {status}",
        "error.coreUnavailable": "The local core is unavailable.",
        "route.home": "Home",
        "home.heading": "LiteLLM Menu",
        "home.subtitle": "Manage local routing, settings, and diagnostics.",
        "home.service": "Service",
        "home.revision": "Core revision {revision}",
        "home.open": "Open",
        "route.loadingFromCore": "This view is connected to the Python Core.",
        "card.providersModels": "Providers & Models",
        "card.codexSettings": "Codex Settings",
        "card.claudeSettings": "Claude Settings",
        "card.runtimeSettings": "Runtime Settings",
        "card.webdavSettings": "WebDAV Sync Settings",
        "card.logs": "Logs",
        "card.languageSettings": "Language Settings",
    },
    "zh-Hans": {
        "app.title": "LiteLLM 菜单",
        "menu.providers": "供应商与模型",
        "menu.codex": "Codex / Claude 设置",
        "menu.claude": "Claude 设置",
        "menu.runtime": "运行时设置",
        "menu.webdav": "WebDAV 同步设置",
        "menu.logs": "日志",
        "menu.logsSummary": "日志 (路由恢复 {recovering}, 冷却 {cooldown})",
        "menu.language": "语言设置",
        "menu.reload": "重新加载",
        "menu.close": "关闭",
        "menu.apply": "应用",
        "menu.cancel": "取消",
        "menu.autoStart": "登录时自动启动",
        "menu.quit": "退出 LiteLLM 菜单",
        "language.title": "语言设置",
        "language.system": "系统",
        "language.english": "English",
        "language.simplified_chinese": "简体中文",
        "claude.title": "Claude 设置",
        "claude.model": "模型",
        "claude.gateway": "LiteLLM 网关",
        "claude.permissions": "权限",
        "claude.sandbox": "Bash 沙箱",
        "claude.filesystem": "文件系统访问",
        "codex.network": "网络访问",
        "claude.confirmation.required": "所选 Claude 权限需要明确确认",
        "validation.invalid_settings": "Claude 设置包含无效值",
        "validation.invalid_language": "不支持的语言",
        "logs.requests": "请求",
        "logs.service": "服务",
        "logs.menu": "菜单",
        "logs.route_trace": "路由跟踪",
        "logs.recovery": "恢复 / 冷却",
        "logs.online_usage": "在线用量",
        "error.generic": "发生错误",
        "common.secureEditorLoading": "正在加载文档...",
        "common.secureEditorReadFailed": "无法加载文档。",
        "common.secureEditorStageFailed": "无法暂存最新修改。当前文本仍保留在此编辑器中。",
        "service.starting": "启动中",
        "service.running": "运行中",
        "service.runningOnPort": "运行中 (端口 {port})",
        "service.unhealthy": "不健康",
        "service.stopped": "已停止",
        "service.unknown": "未知",
        "service.status": "状态: {status}",
        "error.coreUnavailable": "本地 Core 不可用。",
        "route.home": "首页",
        "home.heading": "LiteLLM 菜单",
        "home.subtitle": "管理本地路由、设置和诊断。",
        "home.service": "服务",
        "home.revision": "Core 修订版本 {revision}",
        "home.open": "打开",
        "route.loadingFromCore": "此页面已连接到 Python Core。",
        "card.providersModels": "供应商与模型",
        "card.codexSettings": "Codex 设置",
        "card.claudeSettings": "Claude 设置",
        "card.runtimeSettings": "运行时设置",
        "card.webdavSettings": "WebDAV 同步设置",
        "card.logs": "日志",
        "card.languageSettings": "语言设置",
    },
}


def create_translator(choice: str | None = "system", system_locale: str | None = None) -> Callable[..., str]:
    language = resolve_language(choice, system_locale)
    messages = MESSAGES[language]
    fallback = MESSAGES["en"]

    def translate(key: str, values: Mapping[str, object] | None = None) -> str:
        text = messages.get(key, fallback.get(key, key))
        for name, value in (values or {}).items():
            text = text.replace("{" + str(name) + "}", str(value))
        return text

    return translate


def _atomic_write(path: pathlib.Path, value: str) -> None:
    try:
        try:
            details = path.lstat()
        except FileNotFoundError:
            details = None
        if details is not None and (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)):
            raise LanguageSettingsError("Language preference could not be saved")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError:
        raise LanguageSettingsError("Language preference could not be saved") from None


class LanguageSettingsDomain:
    name = DOMAIN_NAME

    def __init__(self, preference_path: pathlib.Path | str | None = None, *, system_locale: str | None = None):
        configured = os.environ.get("LITELLM_MENU_LANGUAGE_FILE", "").strip()
        self.preference_path = (
            pathlib.Path(preference_path).expanduser()
            if preference_path
            else pathlib.Path(configured).expanduser()
            if configured
            else pathlib.Path.home() / ".litellm-menu" / "language.json"
        )
        self.system_locale = system_locale if system_locale is not None else os.environ.get("LANG", "")
        self.choice = "system"
        self._baseline_bytes: bytes | None = None
        self.revision = 0
        self.reload()

    def reload(self) -> dict[str, Any]:
        try:
            details = self.preference_path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise LanguageSettingsError("Language preference could not be loaded")
            text = self.preference_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.choice = "system"
            self._baseline_bytes = None
        except LanguageSettingsError:
            raise
        except (OSError, UnicodeError):
            raise LanguageSettingsError("Language preference could not be loaded") from None
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise LanguageSettingsError("Language preference could not be loaded") from None
            if not isinstance(data, Mapping) or data.get("language", "system") not in LANGUAGE_OPTIONS:
                raise LanguageSettingsError("Unsupported language")
            self.choice = str(data.get("language", "system"))
            self._baseline_bytes = text.encode("utf-8")
        self.revision += 1
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "domain": self.name,
            "revision": self.revision,
            "choice": self.choice,
            "resolved": resolve_language(self.choice, self.system_locale),
            "options": list(LANGUAGE_OPTIONS),
        }

    def draft_state(self) -> object:
        return self.choice

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        value = self.choice if payload is None else (payload.get("language") if isinstance(payload, Mapping) else payload)
        try:
            if value not in LANGUAGE_OPTIONS:
                raise LanguageSettingsError("Unsupported language")
        except LanguageSettingsError as exc:
            return {"valid": False, "errors": [str(exc)]}
        return {"valid": True, "errors": []}

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        if action in {"set", "select", "set_language", "setLanguage"}:
            value = payload.get("language") if isinstance(payload, Mapping) else payload
            if value not in LANGUAGE_OPTIONS:
                raise LanguageSettingsError("Unsupported language")
            self.choice = str(value)
            self.revision += 1
            return self.snapshot()
        if action == "reload":
            return self.reload()
        raise LanguageSettingsError("Unknown language action")

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        if payload is not None:
            value = payload.get("language") if isinstance(payload, Mapping) else payload
            if value not in LANGUAGE_OPTIONS:
                raise LanguageSettingsError("Unsupported language")
            self.choice = str(value)
        try:
            details = self.preference_path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise LanguageSettingsError("Language preference changed on disk; reload before applying")
            current = self.preference_path.read_bytes()
        except FileNotFoundError:
            current = None
        except LanguageSettingsError:
            raise
        except OSError:
            raise LanguageSettingsError("Language preference could not be saved") from None
        if current != self._baseline_bytes:
            raise LanguageSettingsError("Language preference changed on disk; reload before applying")
        text = json.dumps({"language": self.choice}, ensure_ascii=False) + "\n"
        _atomic_write(self.preference_path, text)
        self._baseline_bytes = text.encode("utf-8")
        self.revision += 1
        return {"applied": True, **self.snapshot()}

    def translate(self, key: str, values: Mapping[str, object] | None = None) -> str:
        return create_translator(self.choice, self.system_locale)(key, values)


def create_domain(*args: Any, **kwargs: Any) -> LanguageSettingsDomain:
    return LanguageSettingsDomain(*args, **kwargs)


__all__ = [
    "DOMAIN_NAME",
    "LANGUAGE_OPTIONS",
    "LanguageSettingsDomain",
    "LanguageSettingsError",
    "MESSAGES",
    "create_domain",
    "create_translator",
    "normalize_system_language",
    "resolve_language",
]
