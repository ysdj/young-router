"""Bounded, redacted log views owned by the Python Core."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from collections.abc import Mapping
from typing import Any

from ..security import REDACT_TEXT, redact


DOMAIN_NAME = "logs"
LOG_TABS = (
    "requests",
    "service",
    "menu",
    "route-trace",
    "recovery",
    "online-usage",
)
MAX_READ_BYTES = 1024 * 1024
MAX_LINES = 500
MAX_FILTER_BYTES = 256


class LogsDomainError(ValueError):
    """A source-safe log operation error."""


def _runtime_root(value: Path | str | None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _safe_scalar(value: object, limit: int = 160) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return REDACT_TEXT(value)[:limit]
    return None


def _safe_request_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "ts",
        "timestamp",
        "status",
        "model_group",
        "provider",
        "upstream_model",
        "duration_ms",
        "route_key",
    ):
        if key in raw:
            result[key] = _safe_scalar(raw[key])
    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        result["usage"] = {
            key: value
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance((value := usage.get(key)), (int, float)) and not isinstance(value, bool)
        }
    if raw.get("error") not in (None, ""):
        result["error"] = "Request failed"
    return result


def _safe_json_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    projected = redact(raw)
    if not isinstance(projected, Mapping):
        return {}
    # Log bodies and raw payloads are never useful enough to justify crossing
    # IPC. Keep summaries, counters and state while dropping high-risk blobs.
    forbidden = {
        "body",
        "request",
        "response",
        "messages",
        "input",
        "output",
        "raw",
        "payload",
        "config",
        "headers",
    }
    return {
        str(key): value
        for key, value in projected.items()
        if str(key).strip().lower().replace("-", "_") not in forbidden
    }


class LogsDomain:
    name = DOMAIN_NAME

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        config_path: Path | str | None = None,
        online_usage_reader: object | None = None,
        maximum_lines: int = MAX_LINES,
        maximum_read_bytes: int = MAX_READ_BYTES,
    ) -> None:
        self.root = _runtime_root(runtime_root)
        self.config_path = Path(config_path).expanduser() if config_path else self.root / "config.yaml"
        self._online_usage_reader = online_usage_reader
        self._online_usage_records: list[str] = []
        self._online_usage_refreshed = False
        self.maximum_lines = max(1, min(int(maximum_lines), MAX_LINES))
        self.maximum_read_bytes = max(4096, min(int(maximum_read_bytes), MAX_READ_BYTES))
        self.revision = 0
        self._paused: set[str] = set()
        self._cleared: set[str] = set()
        self._filters: dict[str, str] = {}
        self._limits: dict[str, int] = {}
        self._tabs: dict[str, dict[str, Any]] = {}
        self.reload()

    def _path(self, tab: str) -> Path | None:
        names = {
            "requests": "recent-requests.jsonl",
            "service": "menu-server.log",
            "menu": "menu-actions.log",
            "route-trace": "menu-server.log",
            "recovery": ".litellm-runtime/route-recovery-state.json",
        }
        name = names.get(tab)
        return self.root / name if name else None

    def _read_lines(self, path: Path) -> list[str]:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LogsDomainError("Log source is unavailable")
        try:
            with path.open("rb") as handle:
                if details.st_size > self.maximum_read_bytes:
                    handle.seek(-self.maximum_read_bytes, os.SEEK_END)
                    handle.readline()
                data = handle.read(self.maximum_read_bytes)
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None
        return data.decode("utf-8", errors="replace").splitlines()[-self.maximum_lines :]

    def _records(self, tab: str) -> tuple[bool, list[object]]:
        if tab == "online-usage":
            records: list[object] = list(self._online_usage_records)
            needle = self._filters.get(tab, "").casefold()
            if needle:
                records = [record for record in records if needle in str(record).casefold()]
            return self._online_usage_refreshed, records[-self._limits.get(tab, self.maximum_lines) :]
        path = self._path(tab)
        if path is None:
            return False, []
        lines = self._read_lines(path)
        if tab == "route-trace":
            lines = [line for line in lines if "litellm_route_trace" in line]
        elif tab == "service":
            lines = [line for line in lines if "litellm_route_trace" not in line]
        records: list[object] = []
        for line in lines:
            if not line.strip():
                continue
            parsed: object | None = None
            if tab in {"requests", "route-trace", "recovery"}:
                try:
                    parsed = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
            if isinstance(parsed, Mapping):
                record = _safe_request_record(parsed) if tab == "requests" else _safe_json_record(parsed)
                if record:
                    records.append(record)
            elif tab not in {"requests", "route-trace", "recovery"}:
                records.append(REDACT_TEXT(line)[:512])
        needle = self._filters.get(tab, "").casefold()
        if needle:
            records = [
                record
                for record in records
                if needle in json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
            ]
        return path.exists(), records[-self._limits.get(tab, self.maximum_lines) :]

    def _refresh(self, tab: str) -> None:
        if tab in self._paused:
            previous = dict(
                self._tabs.get(
                    tab,
                    {
                        "tab": tab,
                        "available": False,
                        "paused": True,
                        "line_count": 0,
                        "records": [],
                        "filter": self._filters.get(tab, ""),
                    },
                )
            )
            previous["paused"] = True
            previous["filter"] = self._filters.get(tab, "")
            if tab in self._cleared:
                previous["records"] = []
                previous["line_count"] = 0
            self._tabs[tab] = previous
            return
        available, records = self._records(tab)
        if tab in self._cleared:
            records = []
        self._tabs[tab] = {
            "tab": tab,
            "available": available,
            "paused": tab in self._paused,
            "line_count": len(records),
            "records": records,
            "filter": self._filters.get(tab, ""),
            "limit": self._limits.get(tab, self.maximum_lines),
        }

    def snapshot(self) -> dict[str, Any]:
        for tab in LOG_TABS:
            if tab not in self._paused:
                self._refresh(tab)
        return {
            "domain": self.name,
            "revision": self.revision,
            "tabs": {tab: dict(self._tabs[tab]) for tab in LOG_TABS},
        }

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        tab = data.get("tab")
        if tab not in LOG_TABS:
            raise LogsDomainError("Log tab is invalid")
        operation = action.removeprefix("logs.").replace("-", "_")
        if operation == "pause":
            self._paused.add(str(tab))
        elif operation == "resume":
            self._paused.discard(str(tab))
            self._cleared.discard(str(tab))
        elif operation == "clear":
            # Clear only the view. Core never deletes diagnostic files without
            # a separate, explicit destructive operation.
            self._cleared.add(str(tab))
        elif operation in {"set_filter", "filter"}:
            value = data.get("filter", data.get("query", ""))
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_FILTER_BYTES:
                raise LogsDomainError("Log filter is invalid")
            self._filters[str(tab)] = value.strip()
            self._cleared.discard(str(tab))
        elif operation in {"set_limit", "limit"}:
            value = data.get("limit")
            if type(value) is not int or not 1 <= value <= MAX_LINES:
                raise LogsDomainError("Log line limit is invalid")
            self._limits[str(tab)] = value
            self._cleared.discard(str(tab))
        elif operation in {"refresh", "reload", "refresh_online_usage"}:
            self._cleared.discard(str(tab))
            if tab == "online-usage":
                try:
                    reader = self._online_usage_reader
                    if reader is None:
                        from ..operations import OnlineUsageReader

                        reader = OnlineUsageReader(self.config_path)
                    refresh = getattr(reader, "refresh", None)
                    values = refresh() if callable(refresh) else []
                    self._online_usage_records = [
                        REDACT_TEXT(str(value))[:512]
                        for value in values
                        if isinstance(value, str) and value.strip()
                    ][-self.maximum_lines :]
                except Exception:
                    self._online_usage_records = ["Online usage logs are unavailable."]
                self._online_usage_refreshed = True
        else:
            raise LogsDomainError("Log action is unavailable")
        self.revision += 1
        self._refresh(str(tab))
        return self.snapshot()

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        return {"valid": True, "errors": []}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        return {"applied": True, **self.snapshot()}

    def reload(self) -> dict[str, Any]:
        self._cleared.clear()
        for tab in LOG_TABS:
            self._refresh(tab)
        self.revision += 1
        return self.snapshot()


__all__ = ["DOMAIN_NAME", "LOG_TABS", "LogsDomain", "LogsDomainError"]
