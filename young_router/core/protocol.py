"""The versioned local Core IPC contract.

The React Native client and the Python Core deliberately exchange plain JSON,
but the JSON is not an unversioned bag of fields.  This module is the Python
runtime validator for the checked-in ``ipc-v1.schema.json`` v1 contract. Keep
the wire shape boring and strict: a mismatched protocol is much safer to
diagnose than a partially applied configuration action.

No application state or credentials are imported here.  Importing the module
is consequently cheap and safe for a native host's startup path.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Any

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_ID_BYTES = 256
MAX_METHOD_BYTES = 64
MAX_JSON_DEPTH = 32


class ProtocolError(ValueError):
    """An error that is safe to send to a local UI client."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = str(code)
        self.retryable = bool(retryable)
        # Protocol error messages are authored by this module/Core.  Do not
        # accept arbitrary parser text here: JSON/YAML diagnostics can contain
        # source lines with keys, tokens, or private paths.
        self.message = str(message)
        super().__init__(self.message)


_SCHEMA_PATH = Path(__file__).with_name("ipc-v1.schema.json")


def _read_protocol_schema() -> dict[str, Any]:
    try:
        loaded = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable") from exc
    if not isinstance(loaded, dict):
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
    version = loaded.get("protocol_version")
    methods = loaded.get("methods")
    if type(version) is not int or version < 1:
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
    if (
        not isinstance(methods, list)
        or not methods
        or any(not isinstance(method, str) or not method for method in methods)
        or len(set(methods)) != len(methods)
    ):
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
    return loaded


# The checked-in schema is the durable wire-contract source.  Both the method
# set and the runtime validators below are derived from this loaded document.
_PROTOCOL_SCHEMA = _read_protocol_schema()
PROTOCOL_VERSION = _PROTOCOL_SCHEMA["protocol_version"]
"""The current IPC protocol version."""
SUPPORTED_METHODS = tuple(_PROTOCOL_SCHEMA["methods"])
METHODS = frozenset(SUPPORTED_METHODS)


def _safe_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_request", f"{label} must be a string")
    encoded = value.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > maximum:
        raise ProtocolError("invalid_request", f"{label} is invalid")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ProtocolError("invalid_request", f"{label} is invalid")
    return value


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    """Reject values that cannot be represented safely on the JSON wire."""

    if depth > MAX_JSON_DEPTH:
        raise ProtocolError("invalid_request", "IPC payload is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("invalid_request", "IPC payload contains an invalid number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key:
                raise ProtocolError("invalid_request", "IPC object keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    raise ProtocolError("invalid_request", "IPC payload contains an unsupported value")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError("invalid_request", f"{label} must be an object")
    result = dict(value)
    _validate_json_value(result)
    return result


class _SchemaMismatch(ValueError):
    pass


def _resolve_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if reference is None:
        return schema
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
    name = reference.removeprefix("#/$defs/")
    definition = _PROTOCOL_SCHEMA.get("$defs", {}).get(name)
    if not isinstance(definition, Mapping):
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
    return definition


def _schema_type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return (type(value) is int) or isinstance(value, float)
    if expected == "boolean":
        return type(value) is bool
    if expected == "null":
        return value is None
    raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")


def _json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    return left == right


def _validate_schema_value(value: object, raw_schema: Mapping[str, Any]) -> None:
    """Validate the JSON Schema subset used by the checked-in IPC contract."""

    schema = _resolve_schema(raw_schema)
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed_types = [expected_type] if isinstance(expected_type, str) else expected_type
        if (
            not isinstance(allowed_types, list)
            or not allowed_types
            or any(not isinstance(item, str) for item in allowed_types)
        ):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        if not any(_schema_type_matches(value, item) for item in allowed_types):
            raise _SchemaMismatch

    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _SchemaMismatch
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        if not any(_json_equal(value, item) for item in enum):
            raise _SchemaMismatch

    variants = schema.get("oneOf")
    if variants is not None:
        if not isinstance(variants, list) or not variants:
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        matches = 0
        for variant in variants:
            if not isinstance(variant, Mapping):
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            try:
                _validate_schema_value(value, variant)
            except _SchemaMismatch:
                continue
            matches += 1
        if matches != 1:
            raise _SchemaMismatch

    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        if any(not isinstance(name, str) for name in required):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        if any(name not in value for name in required):
            raise _SchemaMismatch
        maximum_properties = schema.get("maxProperties")
        if maximum_properties is not None:
            if type(maximum_properties) is not int:
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            if len(value) > maximum_properties:
                raise _SchemaMismatch
        if schema.get("additionalProperties") is False and any(name not in properties for name in value):
            raise _SchemaMismatch
        for name, item in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                if not isinstance(child_schema, Mapping):
                    raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
                _validate_schema_value(item, child_schema)

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if minimum_items is not None and (type(minimum_items) is not int or len(value) < minimum_items):
            if type(minimum_items) is not int:
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            raise _SchemaMismatch
        if maximum_items is not None and (type(maximum_items) is not int or len(value) > maximum_items):
            if type(maximum_items) is not int:
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            raise _SchemaMismatch
        if schema.get("uniqueItems") is True:
            encoded_items = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in value]
            if len(set(encoded_items)) != len(encoded_items):
                raise _SchemaMismatch
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, Mapping):
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            for item in value:
                _validate_schema_value(item, item_schema)

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if minimum_length is not None and (type(minimum_length) is not int or len(value) < minimum_length):
            if type(minimum_length) is not int:
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            raise _SchemaMismatch
        if maximum_length is not None and (type(maximum_length) is not int or len(value) > maximum_length):
            if type(maximum_length) is not int:
                raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
            raise _SchemaMismatch

    minimum = schema.get("minimum")
    if minimum is not None:
        if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
            raise _SchemaMismatch


def _load_method_contracts() -> dict[str, dict[str, Mapping[str, Any]]]:
    raw_contracts = _PROTOCOL_SCHEMA.get("x-method-contracts")
    clauses = _PROTOCOL_SCHEMA.get("request", {}).get("allOf")
    if not isinstance(raw_contracts, Mapping) or list(raw_contracts) != list(SUPPORTED_METHODS):
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
    if not isinstance(clauses, list) or len(clauses) != len(SUPPORTED_METHODS):
        raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")

    request_refs: dict[str, str] = {}
    for clause in clauses:
        if not isinstance(clause, Mapping):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        method = clause.get("if", {}).get("properties", {}).get("method", {}).get("const")
        reference = clause.get("then", {}).get("properties", {}).get("params", {}).get("$ref")
        if not isinstance(method, str) or not isinstance(reference, str) or method in request_refs:
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        request_refs[method] = reference

    contracts: dict[str, dict[str, Mapping[str, Any]]] = {}
    for method in SUPPORTED_METHODS:
        contract = raw_contracts.get(method)
        if not isinstance(contract, Mapping) or set(contract) != {"params", "result"}:
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        params = contract.get("params")
        result = contract.get("result")
        if not isinstance(params, Mapping) or not isinstance(result, Mapping):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        if params.get("$ref") != request_refs.get(method):
            raise ProtocolError("schema_unavailable", "Core IPC schema is unavailable")
        _resolve_schema(params)
        _resolve_schema(result)
        contracts[method] = {"params": params, "result": result}
    return contracts


_METHOD_CONTRACTS = _load_method_contracts()


def _validate_method_params(method: str, params: Mapping[str, Any]) -> None:
    try:
        _validate_schema_value(params, _METHOD_CONTRACTS[method]["params"])
    except _SchemaMismatch:
        raise ProtocolError("invalid_request", f"{method} params do not match the Core IPC contract") from None


def validate_method_result(method: str, result: object) -> None:
    """Validate one successful Core result against its method contract.

    A result is produced by Core, but it still crosses a process boundary and
    must be treated as untrusted at that boundary.  Keep the failure message
    contract-level so an adapter cannot expose a raw configuration value or
    exception through an invalid result shape.
    """

    if not isinstance(method, str) or method not in METHODS:
        raise ProtocolError("unsupported_method", "Unsupported Core operation")
    try:
        _validate_json_value(result)
        _validate_schema_value(result, _METHOD_CONTRACTS[method]["result"])
    except _SchemaMismatch:
        raise ProtocolError("invalid_response", f"{method} result does not match the Core IPC contract") from None
    except ProtocolError as exc:
        if exc.code == "schema_unavailable":
            raise
        raise ProtocolError("invalid_response", f"{method} result does not match the Core IPC contract") from None


@dataclass(frozen=True)
class RequestEnvelope:
    """A validated request sent from a native/RN client to Core."""

    request_id: str
    method: str
    params: dict[str, Any]
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_version", "Unsupported IPC protocol version")
        _safe_text(self.request_id, "request_id", maximum=MAX_REQUEST_ID_BYTES)
        method = _safe_text(self.method, "method", maximum=MAX_METHOD_BYTES)
        if method not in METHODS:
            raise ProtocolError("unsupported_method", "Unsupported Core operation")
        if not isinstance(self.params, dict):
            raise ProtocolError("invalid_request", "params must be an object")
        _validate_json_value(self.params)
        _validate_method_params(method, self.params)

    @classmethod
    def from_mapping(cls, raw: object) -> "RequestEnvelope":
        data = _mapping(raw, "request")
        if set(data) != {"protocol_version", "request_id", "method", "params"}:
            raise ProtocolError("invalid_request", "IPC request has an unsupported shape")
        version = data.get("protocol_version")
        if type(version) is not int or version != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_version", "Unsupported IPC protocol version")
        return cls(
            request_id=_safe_text(data.get("request_id"), "request_id", maximum=MAX_REQUEST_ID_BYTES),
            method=_safe_text(data.get("method"), "method", maximum=MAX_METHOD_BYTES),
            params=_mapping(data.get("params"), "params"),
            protocol_version=version,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "method": self.method,
            "params": self.params,
        }


@dataclass(frozen=True)
class ResponseEnvelope:
    """A validated response.  Exactly one of ``result`` and ``error`` exists."""

    request_id: str
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_version", "Unsupported IPC protocol version")
        _safe_text(self.request_id, "request_id", maximum=MAX_REQUEST_ID_BYTES)
        if type(self.ok) is not bool:
            raise ProtocolError("invalid_response", "IPC response has an invalid status")
        if self.ok:
            if self.error is not None:
                raise ProtocolError("invalid_response", "Successful IPC response cannot contain an error")
            _validate_json_value(self.result)
        else:
            if self.error is None or not isinstance(self.error, dict):
                raise ProtocolError("invalid_response", "Failed IPC response must contain an error")
            _validate_error(self.error)
            if self.result is not None:
                raise ProtocolError("invalid_response", "Failed IPC response cannot contain a result")

    @classmethod
    def success(cls, request_id: str, result: Any) -> "ResponseEnvelope":
        return cls(request_id=request_id, ok=True, result=result)

    @classmethod
    def failure(
        cls,
        request_id: str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> "ResponseEnvelope":
        # Keep failure fields deliberately small.  Details/cause/traceback are
        # not part of v1 and therefore cannot accidentally cross IPC.
        safe_code = _safe_error_code(code)
        safe_message = _safe_error_message(message)
        return cls(
            request_id=request_id,
            ok=False,
            error={"code": safe_code, "message": safe_message, "retryable": bool(retryable)},
        )

    @classmethod
    def from_mapping(cls, raw: object) -> "ResponseEnvelope":
        data = _mapping(raw, "response")
        required = {"protocol_version", "request_id", "ok"}
        if not required.issubset(data):
            raise ProtocolError("invalid_response", "IPC response has an unsupported shape")
        if type(data.get("protocol_version")) is not int or data["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_version", "Unsupported IPC protocol version")
        ok = data.get("ok")
        if type(ok) is not bool:
            raise ProtocolError("invalid_response", "IPC response has an invalid status")
        allowed = required | ({"result"} if ok else {"error"})
        if set(data) != allowed:
            raise ProtocolError("invalid_response", "IPC response has an unsupported shape")
        return cls(
            request_id=_safe_text(data.get("request_id"), "request_id", maximum=MAX_REQUEST_ID_BYTES),
            ok=ok,
            result=data.get("result") if ok else None,
            error=data.get("error") if not ok else None,
            protocol_version=data["protocol_version"],
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "ok": self.ok,
        }
        if self.ok:
            result["result"] = self.result
        else:
            result["error"] = self.error
        return result


def _validate_error(error: Mapping[str, Any]) -> None:
    if set(error) != {"code", "message", "retryable"}:
        raise ProtocolError("invalid_response", "IPC error has an unsupported shape")
    _safe_error_code(error.get("code"))
    _safe_error_message(error.get("message"))
    if type(error.get("retryable")) is not bool:
        raise ProtocolError("invalid_response", "IPC error has an invalid retryable flag")


_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_error_code(value: object) -> str:
    if not isinstance(value, str) or not _ERROR_CODE_RE.fullmatch(value):
        raise ProtocolError("invalid_response", "IPC error has an invalid code")
    return value


def _safe_error_message(value: object) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_response", "IPC error has an invalid message")
    # Newlines make it too easy for a caller to turn an error into a fake log
    # record.  Length is bounded so a parser cannot send a source document.
    if not value or len(value.encode("utf-8")) > 512 or "\x00" in value or "\r" in value or "\n" in value:
        raise ProtocolError("invalid_response", "IPC error has an invalid message")
    return value


def encode_message(value: Mapping[str, Any] | RequestEnvelope | ResponseEnvelope) -> bytes:
    """Serialize one envelope for the HTTP/JSON-lines transports."""

    if isinstance(value, (RequestEnvelope, ResponseEnvelope)):
        payload = value.to_mapping()
    else:
        payload = _mapping(value, "message")
        _validate_json_value(payload)
    try:
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("invalid_message", "IPC message cannot be encoded") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_too_large", "IPC message exceeds the size limit")
    return encoded


def decode_message(raw: bytes | str) -> dict[str, Any]:
    """Decode one JSON message without echoing parser diagnostics."""

    if isinstance(raw, bytes):
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "IPC message exceeds the size limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid_message", "IPC message must be UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "IPC message exceeds the size limit")
        text = raw
    else:
        raise ProtocolError("invalid_message", "IPC message is invalid")
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_json_constant)
    except (TypeError, json.JSONDecodeError, ProtocolError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_message", "IPC message is not valid JSON") from exc
    return _mapping(payload, "message")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("invalid_message", "IPC message contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ProtocolError("invalid_message", "IPC message contains an unsupported value")


def make_request(method: str, params: Mapping[str, Any] | None = None, *, request_id: str) -> RequestEnvelope:
    """Build a request for direct Python callers and contract tests."""

    return RequestEnvelope(request_id=request_id, method=method, params=dict(params or {}))


def make_event(event: str, revision: int, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build the state event delivered to subscriptions."""

    name = _safe_text(event, "event", maximum=64)
    if type(revision) is not int or revision < 0:
        raise ProtocolError("invalid_event", "event revision is invalid")
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "event": name,
        "revision": revision,
        "snapshot": dict(snapshot),
    }
    _validate_json_value(payload)
    return payload


def load_protocol_schema() -> dict[str, Any]:
    """Load the shared JSON Schema used by Python and TypeScript checks.

    The runtime validator above intentionally remains dependency-free.  This
    loader gives build/contract tests one durable source to compare with the
    generated TypeScript union without requiring a JSON Schema package at app
    startup.
    """

    return deepcopy(_PROTOCOL_SCHEMA)


__all__ = [
    "METHODS",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "RequestEnvelope",
    "ResponseEnvelope",
    "SUPPORTED_METHODS",
    "decode_message",
    "encode_message",
    "make_event",
    "make_request",
    "load_protocol_schema",
    "validate_method_result",
]
