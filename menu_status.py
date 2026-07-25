#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

MAX_STATUS_TEXT = 2_000
WEBDAV_ACTIONS = {"configure", "disable", "probe", "pull", "push", "sync", "sync-pull", "sync-push"}
RECOVERY_HEARTBEAT_GRACE_SECONDS = 45.0


def _short_text(value: Any, *, allowed: set[str] | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_STATUS_TEXT or any(ord(character) < 32 and character not in "\n\t" for character in text):
        return None
    return text if allowed is None or text in allowed else None


def _recovery_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _recovery_timestamp(value: Any) -> float | None:
    numeric = _recovery_number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _load_object(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _summary_text(value: Any, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split())[:240] or fallback


def recovery_summary_payload(
    *, recovery_state_path: str, cooldown_state_path: str
) -> dict[str, Any]:
    now = time.time()
    raw_recoveries = _load_object(recovery_state_path).get("recoveries")
    raw_cooldowns = _load_object(cooldown_state_path).get("cooldowns")
    recoveries: list[dict[str, Any]] = []
    if isinstance(raw_recoveries, dict):
        for key, raw in raw_recoveries.items():
            if not isinstance(raw, dict):
                continue
            pid = raw.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                try:
                    os.kill(pid, 0)
                except OSError:
                    continue
            row = dict(raw)
            row.setdefault("key", str(key))
            heartbeat = _recovery_timestamp(row.get("heartbeat_at"))
            if heartbeat is None:
                row["activity"] = "heartbeat unavailable"
                row["heartbeat_age_seconds"] = None
            else:
                age = round(max(0.0, now - heartbeat), 3)
                row["activity"] = "needs attention" if age > RECOVERY_HEARTBEAT_GRACE_SECONDS else "active"
                row["heartbeat_age_seconds"] = age
            until = _recovery_number(row.get("cooldown_until"))
            if until is not None:
                row["cooldown_remaining_seconds"] = round(max(0.0, until - now), 3)
            recoveries.append(row)
    recoveries.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)

    cooldown = 0
    if isinstance(raw_cooldowns, dict):
        cooldown = sum(
            1
            for raw in raw_cooldowns.values()
            if isinstance(raw, dict) and (_recovery_number(raw.get("cooldown_until")) or 0) > now
        )

    current = recoveries[0] if recoveries else None
    current_summary: dict[str, Any] | None = None
    if current is not None:
        diagnostic = current.get("diagnostic") if isinstance(current.get("diagnostic"), dict) else {}
        status = _summary_text(current.get("status"), "polling").lower()
        current_summary = {
            "status": "retry scheduled" if status == "waiting" else "recovering" if status == "polling" else status,
            "activity": current["activity"],
            "kind": _summary_text(diagnostic.get("kind"), "unknown"),
            "title": _summary_text(diagnostic.get("title"), "Recovery is retrying"),
            "detail": _summary_text(diagnostic.get("detail")),
            "attempt": current.get("attempt") if isinstance(current.get("attempt"), int) and not isinstance(current.get("attempt"), bool) and 0 <= current["attempt"] <= 1_000_000 else None,
            "heartbeat_age_seconds": current.get("heartbeat_age_seconds"),
            "cooldown_remaining_seconds": current.get("cooldown_remaining_seconds") if isinstance(current.get("cooldown_remaining_seconds"), (int, float)) and not isinstance(current.get("cooldown_remaining_seconds"), bool) else None,
        }
    return {
        "summary": f"{len(recoveries)} recovering / {cooldown} cooldown",
        "recovering": len(recoveries),
        "cooldown": cooldown,
        "overdue": sum(1 for row in recoveries if row.get("activity") == "needs attention"),
        "current": current_summary,
    }


def load_webdav_status(path: str, enabled: bool) -> dict[str, Any]:
    payload: Any = None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    source = payload if isinstance(payload, dict) else {}
    ok = source.get("ok") if isinstance(source.get("ok"), bool) else None
    exit_code = source.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not -255 <= exit_code <= 255:
        exit_code = None
    return {
        "action": _short_text(source.get("action"), allowed=WEBDAV_ACTIONS),
        "ok": ok,
        "exit_code": exit_code,
        "checked_at": _short_text(source.get("checked_at")),
        "enabled": enabled,
        "output": _short_text(source.get("output")) or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-state", required=True)
    parser.add_argument("--auto-start-state", required=True)
    parser.add_argument("--webdav-enabled", choices=("true", "false"), required=True)
    parser.add_argument("--webdav-status-file", required=True)
    parser.add_argument("--recovery-state-file", required=True)
    parser.add_argument("--cooldown-state-file", required=True)
    args = parser.parse_args()

    recovery = recovery_summary_payload(
        recovery_state_path=args.recovery_state_file,
        cooldown_state_path=args.cooldown_state_file,
    )
    payload = {
        "service_state": args.service_state,
        "auto_start_state": args.auto_start_state,
        "route_recovery_summary": recovery["summary"],
        "route_recovery": recovery,
        "webdav_sync_enabled": args.webdav_enabled == "true",
        "webdav_last_status": load_webdav_status(
            args.webdav_status_file,
            args.webdav_enabled == "true",
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
