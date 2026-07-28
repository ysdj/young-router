"""Versioned, JSON-safe settings domain adapters."""

from .claude import (
    ClaudeSettingsDomain,
    ClaudeSettingsError,
    ConfirmationRequired,
    RISK_CONFIRMATION_CODES,
    apply_litellm_deployment,
    risk_confirmation_codes,
)
from .language import (
    LANGUAGE_OPTIONS,
    LanguageSettingsDomain,
    LanguageSettingsError,
    create_translator,
    resolve_language,
)
from .legacy import (
    CodexSettingsDomain,
    LegacyDomainError,
    ProvidersModelsDomain,
    RuntimeSettingsDomain,
    WebDAVSettingsDomain,
)
from .logs import LogsDomain, LogsDomainError

__all__ = [
    "ClaudeSettingsDomain",
    "ClaudeSettingsError",
    "ConfirmationRequired",
    "RISK_CONFIRMATION_CODES",
    "apply_litellm_deployment",
    "risk_confirmation_codes",
    "LANGUAGE_OPTIONS",
    "LanguageSettingsDomain",
    "LanguageSettingsError",
    "create_translator",
    "resolve_language",
    "CodexSettingsDomain",
    "LegacyDomainError",
    "ProvidersModelsDomain",
    "RuntimeSettingsDomain",
    "WebDAVSettingsDomain",
    "LogsDomain",
    "LogsDomainError",
]
