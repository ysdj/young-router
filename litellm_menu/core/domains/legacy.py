"""Compatibility exports for the split Core settings domains.

Production wiring imports the focused modules directly.  This module remains
only for callers that historically imported these adapters from
``domains.legacy``.
"""

from ._shared import LegacyDomainError
from .codex import CodexSettingsDomain
from .providers_models import ProvidersModelsDomain
from .runtime import RuntimeSettingsDomain
from .webdav import WebDAVSettingsDomain

__all__ = [
    "CodexSettingsDomain",
    "LegacyDomainError",
    "ProvidersModelsDomain",
    "RuntimeSettingsDomain",
    "WebDAVSettingsDomain",
]
