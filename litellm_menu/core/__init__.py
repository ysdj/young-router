"""Shared Python Core/IPC implementation used by both native shells.

The top-level ``litellm_menu`` package remains the legacy LiteLLM hook.  This
submodule is deliberately side-effect free: importing it does not start a
service, inspect user files, or import LiteLLM.  Native hosts can construct a
``CoreStore`` and register only the domains they are ready to expose.
"""

from .ipc import CoreIPCClient, CoreIPCServer, IPCError, IpcEndpoint
from .protocol import (
    METHODS,
    PROTOCOL_VERSION,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
    decode_message,
    encode_message,
    load_protocol_schema,
)
from .service import (
    Core,
    CoreError,
    CoreService,
    CoreStore,
    ConfirmationNeeded,
    DomainAdapter,
    DomainNotFound,
    FileCapabilityRegistry,
    MemoryDomain,
    RevisionConflict,
)

__all__ = [
    "Core",
    "CoreError",
    "CoreIPCClient",
    "CoreIPCServer",
    "CoreService",
    "CoreStore",
    "ConfirmationNeeded",
    "DomainAdapter",
    "DomainNotFound",
    "FileCapabilityRegistry",
    "IPCError",
    "IpcEndpoint",
    "METHODS",
    "MemoryDomain",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "RequestEnvelope",
    "ResponseEnvelope",
    "RevisionConflict",
    "decode_message",
    "encode_message",
    "load_protocol_schema",
]
