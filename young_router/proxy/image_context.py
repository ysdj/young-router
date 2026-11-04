"""Image context: which of a task's images go out with the next turn.

An agent client replays its whole transcript on every turn, images included, so
a task that accumulated a dozen screenshots uploads tens of megabytes per
request long after the model stopped working from them.  Measured on one
session, the images held 86% of the context window and a single request body
reached 8.34 MB while the tokens that mattered were 1,843.

This module decides that question the way Context Attachment Manager
(https://github.com/chipfighter/context-attachment-manager, MIT) decides it, and
speaks its placeholder wording and its selection state on the wire, so the two
can be used together: the current turn's images go out untouched, earlier ones
become short text placeholders that keep the image's identity, turn and size,
and the model asks for a specific one by id when it needs to look again.  The
request that carries the fetched copy is the only one that pays for it.

Three boundaries this module keeps:

* **Nothing inside the signed prefix moves.**  A replayed item that follows an
  ``encrypted_content`` item is part of a prefix the upstream signed; rewriting
  it changes the bytes the client will send forever after.  Only the mutable
  suffix is rewritten, so a task on a signed-history route keeps its cache.
* **A removed image stays recoverable.**  Every id this module omits can be
  served from the task's own replayed history (``image_for_turn``), and the
  scanned items carry the bytes, so the fetch tool never reads a transcript,
  never opens a path, and never needs the client's filesystem.
* **Selection is read, never written.**  The state file is Context Attachment
  Manager's own; this module only reads the keys it understands and falls back
  to automatic selection when the file, the plugin, or the reader is missing.

Input: request kwargs (Responses shape) and a :class:`ImageContextSettings` read
from the runtime settings.  Output: new kwargs, or ``None`` when nothing needs
rewriting -- the contract every ``_with_*`` request step in this package keeps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Imported lazily, inside the functions that need them: this module is also
# loaded by the fetch tool's own process, which must start without LiteLLM.

# Runtime settings that own this pass.  Read through the same environment the
# rest of the data plane reads, so a settings change reaches a running worker
# without a republish of this module.
IMAGE_CONTEXT_ENABLED_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_ENABLED"
IMAGE_CONTEXT_MODE_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MODE"
IMAGE_CONTEXT_KEEP_RECENT_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_KEEP_RECENT"
# How much of the turn being answered this pass may take: ``turn`` (the default)
# reaches the tool results inside it, ``history`` keeps the older rule that a
# turn is one unit.
IMAGE_CONTEXT_INSIDE_TURN_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_INSIDE_TURN"
IMAGE_CONTEXT_DATA_DIR_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR"
IMAGE_CONTEXT_MIN_BYTES_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MIN_BYTES"
# How the model fetches an omitted image back.  ``path`` writes each image to a
# file and lets the client's own viewer open it, so nothing in the client's
# configuration has to change; ``stdio`` adds an MCP server to a desktop client
# this app already configures; ``http`` is a gateway, where the client lives
# somewhere else and only a URL can reach the tool; ``off`` promises no tool at
# all; ``auto`` picks per host.
IMAGE_CONTEXT_FETCH_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH"
IMAGE_CONTEXT_FETCH_DEFAULT = "auto"
IMAGE_CONTEXT_FETCH_VALUES = ("auto", "path", "stdio", "http", "off")
IMAGE_CONTEXT_MODE_DEFAULT = "auto"
IMAGE_CONTEXT_MODE_VALUES = ("auto", "manual")
IMAGE_CONTEXT_INSIDE_TURN_DEFAULT = "turn"
IMAGE_CONTEXT_INSIDE_TURN_VALUES = ("turn", "history")
# A tool result that names the picture the model is working from.  Lengths are
# not a guard here: a turn whose newest picture is its only one is left alone by
# the loop's own floor, not by refusing to look at the newest result at all.
IMAGE_CONTEXT_KEEP_RECENT_DEFAULT = 0
# Images below this size are cheaper inline than as a placeholder plus a later
# fetch, so they are left alone even when they age out.
IMAGE_CONTEXT_MIN_BYTES_DEFAULT = 64 * 1024
# Data directory Context Attachment Manager writes on macOS, Windows and Linux,
# in its own order.  ``CAM_DATA_DIR`` wins over all of them, exactly as it does
# for the plugin.
_CAM_DATA_DIRS = (
    ("darwin", ("Library", "Application Support", "codex-attachment-manager")),
    ("win32", (".codex-attachment-manager",)),
    (None, (".local", "share", "codex-attachment-manager")),
)

# Placeholder wording, mirrored from Context Attachment Manager's
# ``plugin/src/rewrite.ts``.  The model was tuned against these exact sentences
# (``spike/src/auto-fetch-selftest.ts`` across the GPT-6 family, Chinese and
# English), so they are copied rather than paraphrased: a placeholder that says
# something else is a different prompt.
_LANG_ZH = "zh"
_LANG_EN = "en"

_WORDS: Dict[str, Dict[str, Any]] = {
    _LANG_ZH: {
        "separator": "｜",
        "list_separator": "，",
        "kind": {
            "upload": "用户上传",
            "view": "工具查看的图片",
            "generated": "生成的图片",
            "tool": "工具结果里的图片",
        },
        "unnamed": "未命名",
        "turn": lambda turn: f"第 {turn} 轮",
        "this_turn": "本轮",
        "size": lambda width, height: f"{width}×{height}",
        # ``[图片 IMG-004（[Image #4]） 已省略｜名字｜来源｜第 3 轮｜900×600]``
        "heading": (
            lambda image_id, label, state, fields: (
                f"[图片 {image_id}"
                + (f"（{label}）" if label else "")
                + f" {state}｜"
                + "｜".join(fields)
                + "]"
            )
        ),
        "omitted": "已省略",
        "included": "已提供",
        "not_repeated": "未重复提供",
        "plain": lambda image_id: f"需要重看时，在回复里写“需要 {image_id}”。",
        "auto_plain": (
            lambda image_id: f"需要原图时调用 view_image_by_id，传入“{image_id}”。"
        ),
        "file_plain": lambda name: f"需要原图时调用 view_image，传入“{name}”。",
        "duplicate": (
            lambda image_id, where: f"这张图和 {image_id}（{where}）内容完全相同，"
            "那一条在本次请求里照常提供，请直接参考那一条。"
        ),
        "fetched": "取回的原图",
        "copy": (
            lambda image_id: f"[{image_id} 的原图：模型当时取回，"
            "只在那一轮提供，现在已省略]"
        ),
        "auto_note": (
            "上下文管理说明（来自用户安装的上下文素材管理工具）：这个任务开着“自动选图”。"
            "为了节省上下文，之前各轮的图片默认不再随请求提供，它们原来的位置换成了"
            "“[图片 IMG-xxx 已省略…]”这样的占位文字。这个任务只有一轮时，"
            "本轮里的工具结果图片也按同样的规则省略，只留其中一张；"
            "用户在本轮附上的图从不动，照常提供。被省略的那张要看时，"
            "按占位文字里的编号取回即可。\n"
            "这些图片在它们出现的那一轮真实存在，你当时收到并看过；你之前对它们的描述，"
            "以及据此做出的判断，都是看着原图做出的，仍然有效。不要因为现在看不到，就认为"
            "之前的回答是猜测或错误，也不要收回或道歉。\n"
            "回答需要看某张被省略的图片、而你之前说过的内容不够用时，调用 view_image_by_id "
            "工具，传入它的编号（几张就一次传几个），原图会出现在这次的工具结果里。"
            "之前说过的内容够用时，不要调用。现在看不到的画面细节，不要猜测或补充；"
            "不要让用户重新上传图片，也不要用其他工具去读取图片文件。\n"
            "取回的图片只在本轮提供，下一轮又会换回占位文字；之后还需要时，再调用一次。"
        ),
        "auto_ask_note": (
            "上下文管理说明（来自用户安装的上下文素材管理工具）：这个任务开着“自动选图”。"
            "为了节省上下文，之前各轮的图片默认不再随请求提供，它们原来的位置换成了"
            "“[图片 IMG-xxx 已省略…]”这样的占位文字。这个任务只有一轮时，"
            "本轮里的工具结果图片也按同样的规则省略，只留其中一张；"
            "用户在本轮附上的图从不动，照常提供。被省略的那张要看时，"
            "按占位文字里的编号取回即可。\n"
            "这些图片在它们出现的那一轮真实存在，你当时收到并看过；你之前对它们的描述，"
            "以及据此做出的判断，都是看着原图做出的，仍然有效。不要因为现在看不到，就认为"
            "之前的回答是猜测或错误，也不要收回或道歉。\n"
            "回答需要看某张被省略的图片、而你之前说过的内容不够用时，在回复里单独写一行"
            "“需要 IMG-xxx”（几张就都写上）：用户看到这一行才会把那张图重新发给你。"
            "现在看不到的画面细节，不要猜测或补充；不要让用户重新上传，也不要用其他工具"
            "去读取图片文件。"
        ),
        "auto_file_note": (
            "上下文管理说明（来自用户安装的上下文素材管理工具）：这个任务开着“自动选图”。"
            "为了节省上下文，之前各轮的图片默认不再随请求提供，它们原来的位置换成了"
            "“[图片 IMG-xxx 已省略…]”这样的占位文字。这个任务只有一轮时，"
            "本轮里的工具结果图片也按同样的规则省略，只留其中一张；"
            "用户在本轮附上的图从不动，照常提供。被省略的那张要看时，"
            "按占位文字里的编号取回即可。\n"
            "这些图片在它们出现的那一轮真实存在，你当时收到并看过；你之前对它们的描述，"
            "以及据此做出的判断，都是看着原图做出的，仍然有效。不要因为现在看不到，就认为"
            "之前的回答是猜测或错误，也不要收回或道歉。\n"
            "回答需要看某张被省略的图片、而你之前说过的内容不够用时，调用 view_image 工具，"
            "传入那张图占位文字里给出的文件路径（几张就一次调几次）。"
            "之前说过的内容够用时，不要调用。现在看不到的画面细节，不要猜测或补充；"
            "不要让用户重新上传图片。\n"
            "取回的图片只在本轮提供，下一轮又会换回占位文字；之后还需要时，再调用一次。"
        ),
        "manual_note": (
            "上下文管理说明（来自用户安装的上下文素材管理工具）：为了节省上下文，"
            "用户取消勾选了这个任务里的部分图片。它们原来的位置换成了"
            "“[图片 IMG-xxx 已省略…]”这样的占位文字。\n"
            "这些图片在它们出现的那一轮真实存在，你当时收到并看过；你之前对它们的描述，"
            "以及据此做出的判断，都是看着原图做出的，仍然有效。不要因为现在看不到，就认为"
            "之前的回答是猜测或错误，也不要收回或道歉。\n"
            "被问到现在能不能看到某张被省略的图片时，如实说明现在看不到（被用户省略了），"
            "并说明之前的描述仍然有效。现在看不到画面，就不要补充之前没说过的细节。"
            "需要重新看某张图时，在回复里原样写出“需要 IMG-xxx”：用户看到这一行才知道"
            "该勾回哪张图；不要让用户重新上传或重新提供。"
        ),
    },
    _LANG_EN: {
        "separator": " | ",
        "list_separator": ", ",
        "kind": {
            "upload": "user upload",
            "view": "viewed image",
            "generated": "generated image",
            "tool": "tool result image",
        },
        "unnamed": "unnamed",
        "turn": lambda turn: f"turn {turn}",
        "this_turn": "this turn",
        "size": lambda width, height: f"{width}×{height}",
        "heading": (
            lambda image_id, label, state, fields: (
                f"[Image {image_id}"
                + (f" ({label})" if label else "")
                + f" {state} | "
                + " | ".join(fields)
                + "]"
            )
        ),
        "omitted": "omitted",
        "included": "provided",
        "not_repeated": "not repeated",
        "plain": lambda image_id: f'To look again, reply "need {image_id}".',
        "auto_plain": (
            lambda image_id: f'To see the original, call view_image_by_id with "{image_id}".'
        ),
        "file_plain": lambda name: f'To see the original, call view_image with "{name}".',
        "duplicate": (
            lambda image_id, where: f"This is the same content as {image_id} ({where}); "
            "that one is provided in this request, so read it there."
        ),
        "fetched": "fetched original",
        "copy": (
            lambda image_id: f"[original of {image_id}: fetched for that turn only, "
            "now omitted]"
        ),
        "auto_note": (
            "Context management note (from the context attachment tool the user "
            "installed): automatic image selection is on for this task. To save "
            "context, images from earlier turns are not provided by default and "
            "their place appear as text like \"[Image IMG-xxx omitted …]\". When "
            "this task has a single turn, the tool results inside that turn are "
            "omitted by the same rule and one of them is kept; an image the user "
            "attached to this turn is never touched and is provided as usual. "
            "Fetch an omitted one by the id its placeholder names.\n"
            "Those images did exist in the turn they appeared in and you did "
            "receive and see them; what you described, and the judgements you made "
            "from it, were made looking at the original and still hold. Do not treat "
            "earlier answers as guesses or mistakes because the image is not visible "
            "now, and do not take them back or apologise for them.\n"
            "When an answer needs an omitted image and what you already said is not "
            "enough, call the view_image_by_id tool with its id (several ids at "
            "once if you need several); the original appears in that tool result. "
            "Do not call it when what you already said is enough. Do not guess or "
            "add detail you cannot see now; do not ask the user to upload the image "
            "again, and do not read image files with another tool.\n"
            "A fetched image is provided for this turn only and becomes a "
            "placeholder again afterwards; call again when you need it again."
        ),
        "auto_ask_note": (
            "Context management note (from the context attachment tool the user "
            "installed): automatic image selection is on for this task. To save "
            "context, images from earlier turns are not provided by default and "
            "their place holds text like \"[Image IMG-xxx omitted …]\". When "
            "this task has a single turn, the tool results inside that turn are "
            "omitted by the same rule and one of them is kept; an image the user "
            "attached to this turn is never touched and is provided as usual. "
            "Fetch an omitted one by the id its placeholder names.\n"
            "Those images did exist in the turn they appeared in and you did "
            "receive and see them; what you described, and the judgements you made "
            "from it, were made looking at the original and still hold. Do not "
            "treat earlier answers as guesses or mistakes because the image is not "
            "visible now, and do not take them back or apologise for them.\n"
            "When an answer needs an omitted image and what you already said is "
            "not enough, write \"need IMG-xxx\" on its own line (several ids if you "
            "need several): that is how the user knows which one to send again. "
            "Do not guess or add detail you cannot see now; do not ask the user to "
            "upload it again, and do not read image files with another tool."
        ),
        "auto_file_note": (
            "Context management note (from the context attachment tool the user "
            "installed): automatic image selection is on for this task. To save "
            "context, images from earlier turns are not provided by default and "
            "their place appear as text like \"[Image IMG-xxx omitted …]\". When "
            "this task has a single turn, the tool results inside that turn are "
            "omitted by the same rule and one of them is kept; an image the user "
            "attached to this turn is never touched and is provided as usual. "
            "Fetch an omitted one by the id its placeholder names.\n"
            "Those images did exist in the turn they appeared in and you did "
            "receive and see them; what you described, and the judgements you made "
            "from it, were made looking at the original and still hold. Do not "
            "treat earlier answers as guesses or mistakes because the image is not "
            "visible now, and do not take them back or apologise for them.\n"
            "When an answer needs an omitted image and what you already said is not "
            "enough, call the view_image tool with the file path that image's "
            "placeholder gives (once per image); the original appears in that tool "
            "result. Do not call it when what you already said is enough. Do not "
            "guess or add detail you cannot see now, and do not ask the user to "
            "upload the image again.\n"
            "A fetched image is provided for this turn only and becomes a "
            "placeholder again afterwards; call again when you need it again."
        ),
        "manual_note": (
            "Context management note (from the context attachment tool the user "
            "installed): to save context, the user unchecked some of this task's "
            "images. Their place now holds text like \"[Image IMG-xxx omitted …]\".\n"
            "Those images did exist in the turn they appeared in and you did "
            "receive and see them; what you described, and the judgements you made "
            "from it, were made looking at the original and still hold. Do not treat "
            "earlier answers as guesses or mistakes because the image is not visible "
            "now, and do not take them back or apologise for them.\n"
            "If you are asked whether you can see an omitted image, say plainly that "
            "you cannot see it now (the user left it out) and that what you said "
            "earlier still holds. Do not add detail you never described. To look at "
            "one again, reply with \"need IMG-xxx\" on its own line: that is how the "
            "user knows which one to check back in. Do not ask the user to upload the "
            "image again."
        ),
    },
}

# An id numbers one occurrence, in the order the replay presents it.  It is not a
# content digest: two byte-identical images are two ids.
#
# The shape is the attachment manager's own, and it is the *lowest* one that
# maintained: it numbers ``IMG-%03d``, the panel and the notes reader accept
# ``IMG-\d{3,}``, and this pass used four digits.  Four was the one width that
# could never agree with it -- a panel selection made on ``IMG-001`` named an id
# this pass never wrote, and a placeholder it offered could not be parsed by a
# reader that only knows the panel's ids -- so the digits are the maintained
# three, and an old placeholder still parses because the readers here accept
# three or more.
_ID_PREFIX = "IMG-"
_ID_DIGITS = 3


def _as_bool(value: Any, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "off", "no", "disabled"}


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the pass runs at all.  Off unless the operator turns it on."""

    env = os.environ if environ is None else environ
    return _as_bool(env.get(IMAGE_CONTEXT_ENABLED_ENV), False)


def mode(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    value = str(env.get(IMAGE_CONTEXT_MODE_ENV) or "").strip().lower()
    return value if value in IMAGE_CONTEXT_MODE_VALUES else IMAGE_CONTEXT_MODE_DEFAULT


def keep_recent(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    return _as_int(
        env.get(IMAGE_CONTEXT_KEEP_RECENT_ENV), IMAGE_CONTEXT_KEEP_RECENT_DEFAULT
    )


def inside_turn(environ: Optional[Mapping[str, str]] = None) -> str:
    """Whether a task's own turn is reachable, or only the turns before it."""

    env = os.environ if environ is None else environ
    value = str(env.get(IMAGE_CONTEXT_INSIDE_TURN_ENV) or "").strip().lower()
    return (
        value
        if value in IMAGE_CONTEXT_INSIDE_TURN_VALUES
        else IMAGE_CONTEXT_INSIDE_TURN_DEFAULT
    )


def fetch_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    value = str(env.get(IMAGE_CONTEXT_FETCH_ENV) or "").strip().lower()
    return value if value in IMAGE_CONTEXT_FETCH_VALUES else IMAGE_CONTEXT_FETCH_DEFAULT


# The documents a real Codex client install owns.  The directory alone is not
# evidence: running the Codex CLI once on a server leaves ``~/.codex`` behind
# (this gateway had one holding nothing but ``generated_images``), and reading
# that as "the client is here" made a remote deployment answer every omitted
# image with a path from its own disk and switch its fetch endpoint off -- the
# model was told to open a file that exists on the server, not on the laptop
# reading the answer, and no client could ever call the tool meant to answer it.
_CLIENT_CONFIG_DOCUMENTS = ("config.toml", "auth.json")
# Headers a reverse proxy adds for the client it forwarded.  A desktop install
# receives its client directly; a gateway never does.
_FORWARDED_CLIENT_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _client_config_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    configured = str(env.get("CODEX_HOME") or "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(
        os.path.expanduser(str(env.get("HOME") or env.get("USERPROFILE") or "~")),
        ".codex",
    )


def _client_config_present(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this host is the one that owns the client's configuration."""

    home = _client_config_dir(environ)
    if not os.path.isdir(home):
        return False
    return any(
        os.path.isfile(os.path.join(home, document))
        for document in _CLIENT_CONFIG_DOCUMENTS
    )


def _request_headers(request_kwargs: Any) -> Mapping[str, Any]:
    """The downstream headers of the request being answered, lower-cased."""

    if not isinstance(request_kwargs, Mapping):
        return {}
    containers: List[Any] = []
    for container_key in ("proxy_server_request", "headers", "request_headers"):
        container = request_kwargs.get(container_key)
        if isinstance(container, Mapping):
            containers.append(
                container.get("headers")
                if container_key == "proxy_server_request"
                else container
            )
    for candidate in containers:
        if not isinstance(candidate, Mapping) or not candidate:
            continue
        return {
            str(name).strip().lower(): value
            for name, value in candidate.items()
            if isinstance(name, str)
        }
    return {}


def _request_is_remote_client(request_kwargs: Any) -> bool:
    """Whether this request reached us through a proxy in front of this host.

    The one fact a gateway has and a desktop install cannot: whoever asked is
    not sitting on this machine.  A placeholder may name a local path only when
    that path is the reader's own.
    """

    headers = _request_headers(request_kwargs)
    return any(
        str(headers.get(name) or "").strip()
        for name in _FORWARDED_CLIENT_HEADERS
    )


def endpoint_served(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the fetch endpoint has ever answered a client on this host.

    A gateway cannot see the client's configuration, so the only evidence that
    ``view_image_by_id`` exists out there is that someone actually called it.
    """

    from . import image_context_store as _store_module

    marker = os.path.join(_store_module.cache_dir(dict(environ) if environ else None), "endpoint-served")
    return os.path.isfile(marker)


def mark_endpoint_served(environ: Optional[Mapping[str, str]] = None) -> None:
    """Record that a client reached the fetch endpoint from now on."""

    from . import image_context_store as _store_module

    root = _store_module.cache_dir(dict(environ) if environ else None)
    try:
        os.makedirs(root, exist_ok=True)
        marker = os.path.join(root, "endpoint-served")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(int(__import__("time").time())))
    except OSError:
        pass


def fetch_kind(
    environ: Optional[Mapping[str, str]] = None,
    request_kwargs: Any = None,
) -> str:
    """How a placeholder may promise to bring an image back: ``file``, ``tool``, ``ask``.

    ``file`` names a file the client's own image viewer can open, which needs no
    change to the client's configuration at all; ``tool`` names
    ``view_image_by_id``; ``ask`` tells the model to ask the user.  A placeholder
    may only promise what will actually be there, and *where the reader is* is
    half of that: the same deployment serves its own window on a desktop and a
    laptop on another continent on a server, and only the first may be handed a
    path from this disk.
    """

    env = os.environ if environ is None else environ
    mode = fetch_mode(env)
    if mode == "off":
        return "ask"
    if mode == "path":
        return "file"
    if mode in {"stdio", "http"}:
        return "tool"
    # ``auto``: a host that owns the client's configuration *and* received this
    # request directly can hand it a file path and change nothing.  Everything
    # else is a gateway answering somebody else's client over its own endpoint.
    if _client_config_present(env) and not _request_is_remote_client(request_kwargs):
        return "file"
    from . import image_context_endpoint as _endpoint_module

    if _endpoint_module.enabled(env) or endpoint_served(env):
        return "tool"
    return "ask"


def fetch_available(
    environ: Optional[Mapping[str, str]] = None,
    request_kwargs: Any = None,
) -> bool:
    """Whether the model can actually fetch an omitted image on this host."""

    return fetch_kind(environ, request_kwargs) != "ask"


def min_bytes(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    return _as_int(
        env.get(IMAGE_CONTEXT_MIN_BYTES_ENV), IMAGE_CONTEXT_MIN_BYTES_DEFAULT
    )


def data_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    """Where the attachment manager keeps its state on this machine."""

    env = os.environ if environ is None else environ
    configured = str(env.get(IMAGE_CONTEXT_DATA_DIR_ENV) or "").strip()
    if configured:
        return os.path.expanduser(configured)
    cam = str(env.get("CAM_DATA_DIR") or "").strip()
    if cam:
        return os.path.expanduser(cam)
    home = os.path.expanduser(str(env.get("HOME") or env.get("USERPROFILE") or "~"))
    platform = str(env.get("YOUNG_ROUTER_PLATFORM") or sys.platform).strip().lower()
    for platform_name, parts in _CAM_DATA_DIRS:
        if platform_name is None or platform.startswith(platform_name):
            return os.path.join(home, *parts)
    return os.path.join(home, *_CAM_DATA_DIRS[-1][1])


# --------------------------------------------------------------------------
# Selection state (read-only)
# --------------------------------------------------------------------------


def _safe_thread_file_name(thread_id: Optional[str]) -> Optional[str]:
    """One task id as a file name, or ``None`` when it is not one.

    Codex ids are UUIDs, but a selection file is read from disk on every
    request: an id carrying a separator or a traversal piece would name a file
    outside the selection directory, so those are refused rather than cleaned.
    """

    if not isinstance(thread_id, str):
        return None
    cleaned = thread_id.strip()
    if not cleaned or len(cleaned) > 120:
        return None
    if any(piece in cleaned for piece in ("/", "\\", "\0", "..")):
        return None
    return cleaned


def _thread_state_path(thread_id: str, environ: Optional[Mapping[str, str]]) -> str:
    return os.path.join(data_dir(environ), "selection", f"{thread_id}.json")


def read_selection(
    thread_id: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Read Context Attachment Manager's selection for one task.

    Only the keys this pass understands are read: ``unchecked`` (ids the user
    turned off), ``pinned`` (ids the user wants every turn), and ``auto`` (the
    task's own automatic-selection switch, set in that project's panel).  A
    missing or unreadable file is not an error -- it means "let the mode
decide".
    """

    if not _safe_thread_file_name(thread_id):
        return {}
    path = _thread_state_path(thread_id, environ)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    def ids(key: str) -> set:
        raw = payload.get(key)
        if not isinstance(raw, dict):
            return set()
        return {str(item) for item in raw if isinstance(item, str)}

    unchecked = ids("unchecked")
    pinned = ids("pinned")
    auto = payload.get("auto")
    result: Dict[str, Any] = {"unchecked": unchecked, "pinned": pinned}
    if isinstance(auto, bool):
        result["auto"] = auto
    return result


# --------------------------------------------------------------------------
# Scan: every image a replay carries, with its identity
# --------------------------------------------------------------------------


def _image_size(raw: bytes) -> Tuple[Optional[int], Optional[int]]:
    """Width and height from the image header, without decoding it."""

    if len(raw) >= 24 and raw[:4] == b"\x89PNG":
        return (
            int.from_bytes(raw[16:20], "big"),
            int.from_bytes(raw[20:24], "big"),
        )
    if len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        if chunk == b"VP8X":
            return (
                int.from_bytes(raw[24:27], "little") + 1,
                int.from_bytes(raw[27:30], "little") + 1,
            )
        if chunk == b"VP8 " and len(raw) >= 30:
            return (
                int.from_bytes(raw[26:28], "little") & 0x3FFF,
                int.from_bytes(raw[28:30], "little") & 0x3FFF,
            )
    if len(raw) >= 10 and raw[:3] == b"GIF":
        return (
            int.from_bytes(raw[6:8], "little"),
            int.from_bytes(raw[8:10], "little"),
        )
    if len(raw) >= 4 and raw[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(raw):
            if raw[index] != 0xFF:
                index += 1
                continue
            marker = raw[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                return (
                    int.from_bytes(raw[index + 7 : index + 9], "big"),
                    int.from_bytes(raw[index + 5 : index + 7], "big"),
                )
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(raw[index + 2 : index + 4], "big")
            if length <= 0:
                break
            index += 2 + length
    return (None, None)


def _decode_data_url(value: Any) -> Optional[Tuple[str, bytes]]:
    if not isinstance(value, str) or not value.startswith("data:image/"):
        return None
    marker = ";base64,"
    split = value.find(marker)
    if split < 0:
        return None
    mime = value[5:split] or "image/png"
    try:
        return mime, base64.b64decode(value[split + len(marker) :], validate=False)
    except (ValueError, TypeError):
        return None


_IMAGE_PART_TYPES = ("input_image", "image_url")
# The line the fetch tool writes before an image it returns, in either language:
# ``[图片 IMG-004 取回的原图｜…]`` / ``[Image IMG-004 fetched original | …]``.
# An image behind it is a *copy* of an existing occurrence, not a new one, so it
# must not take a new id -- the model and the panel both address that image by
# the id it copies.
_FETCHED_LINE = re.compile(
    r"^\[(?:图片|Image) (IMG-\d{3,}) (?:取回的原图|fetched original)(?:｜| \||])"
)
_OPEN_TAG = re.compile(r'^<image name=(\[Image #\d+\]) path="(.*)">$', re.S)


def _same_path(value: object) -> str:
    """One file path in the form two callers can compare.

    A path arrives here as the client echoed it, so it is cleaned the same way
    on both sides of the comparison instead of trusted as text.
    """

    text = str(value or "").strip().strip('"')
    return os.path.normpath(os.path.expanduser(text)) if text else ""


# The client's own image viewer: one call per file, and its result is only the
# picture.  Both spellings exist in this client's replay (``function_call`` for
# the Responses wire, ``custom_tool_call`` for a freeform tool).
_VIEW_TOOL_NAME = "view_image"
_VIEW_CALL_ITEMS = frozenset({"function_call", "custom_tool_call"})


def _viewed_file_id(item: Mapping[str, Any], known_files: Mapping[str, str]) -> Optional[str]:
    """The stored image one ``view_image`` call opened, if it opened ours."""

    if str(item.get("type") or "") not in _VIEW_CALL_ITEMS:
        return None
    if str(item.get("name") or "") != _VIEW_TOOL_NAME:
        return None
    arguments = item.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, Mapping):
        return None
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return None
    return known_files.get(_same_path(path))


def _file_path_ids(file_paths: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """``{path: id}`` for the files one task's last replay wrote."""

    if not file_paths:
        return {}
    result: Dict[str, str] = {}
    for image_id, path in file_paths.items():
        cleaned = _same_path(path)
        if cleaned and isinstance(image_id, str) and image_id:
            result[cleaned] = image_id
    return result
_CLOSE_TAG = re.compile(r"^</image>$")


# Items the assistant produces.  A picture that comes after the last of these
# has not been in front of the model yet, in any request of this turn.
_ASSISTANT_ITEMS = frozenset(
    {"function_call", "custom_tool_call", "local_shell_call", "computer_call"}
)


class _Found:
    """One image occurrence in a replay."""

    __slots__ = (
        "image_id",
        "copy_of",
        "key",
        "item",
        "part",
        "open_tag",
        "close_tag",
        "mime",
        "raw",
        "width",
        "height",
        "content_id",
        "kind",
        "name",
        "turn",
        "unseen",
    )

    def __init__(self, **fields: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, fields.get(slot))


def _part_data_url(part: Any) -> Optional[str]:
    if not isinstance(part, dict):
        return None
    value = part.get("image_url")
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) else None


def _text_of(part: Any) -> Optional[str]:
    if not isinstance(part, dict):
        return None
    text = part.get("text")
    return text if isinstance(text, str) else None


def _kind_of(item_type: Optional[str], role: Any) -> str:
    """Where an image came from, in the reference implementation's four kinds."""

    if item_type == "image_generation_call":
        return "generated"
    if item_type == "message" and role == "user":
        return "upload"
    return "tool"


_VISIBLE_PICTURES_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_VISIBLE_PICTURES"
_VISIBLE_PICTURES_DEFAULT = 24
_VISIBLE_TOKENS_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_VISIBLE_TOKENS"
_VISIBLE_TOKENS_DEFAULT = 30_000
# The same pricing ``context_window`` uses to decide whether a request fits, so
# the two passes agree on what a picture costs instead of one trusting bytes and
# the other trusting pixels.
_PICTURE_PIXELS_PER_TOKEN = 750
_PICTURE_TILE_OVERHEAD_TOKENS = 85


def _picture_tokens(image: Any) -> int:
    """What one picture is expected to cost the route, in tokens."""

    width = getattr(image, "width", None)
    height = getattr(image, "height", None)
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return width * height // _PICTURE_PIXELS_PER_TOKEN + _PICTURE_TILE_OVERHEAD_TOKENS
    raw = getattr(image, "raw", b"") or b""
    return int(len(raw) / 2.5)


def _visible_tokens() -> int:
    """How many picture tokens of the turn stay visible at once."""

    try:
        value = int(os.environ.get(_VISIBLE_TOKENS_ENV, ""))
    except (TypeError, ValueError):
        return _VISIBLE_TOKENS_DEFAULT
    return value if value >= 0 else _VISIBLE_TOKENS_DEFAULT


def _visible_pictures() -> int:
    """How many pictures of the turn being answered stay visible at once."""

    try:
        value = int(os.environ.get(_VISIBLE_PICTURES_ENV, ""))
    except (TypeError, ValueError):
        return _VISIBLE_PICTURES_DEFAULT
    return value if value >= 0 else _VISIBLE_PICTURES_DEFAULT


def scan(
    input_items: Any,
    *,
    turn_of_item: Optional[Dict[int, int]] = None,
    file_paths: Optional[Mapping[str, str]] = None,
) -> List[_Found]:
    """Every image a replay carries, in wire order, with its identity.

    The turn of an image is the number of user messages that precede it, which
    is what a placeholder names ("第 3 轮").  It is derived here rather than read
    from the transcript so this pass stays a pure function of the request.

    ``file_paths`` is this task's ``{id: path}`` map from the replay the pass
    wrote before this one.  A picture the model opened with the client's own
    viewer comes back as a bare image: the result has no tag and no id, and the
    only thing that says which picture it is, is the ``view_image`` call beside
    it, whose arguments name the file this pass wrote.  That id is therefore
    read off the call -- exactly as the fetch tool's line is read off its own
    result -- and both readers exist for one rule: an id the model was given
    keeps meaning the one image it named, however the copy came back.
    """

    if not isinstance(input_items, list):
        return []
    known_files = _file_path_ids(file_paths)
    viewed_by_call: Dict[str, str] = {}
    found: List[_Found] = []
    # A picture is *unseen* when it sits after the last item the assistant
    # produced.  The assistant has not answered since, so no earlier request of
    # this turn can have carried those bytes to it: replacing such a picture
    # with a placeholder takes away the only view the model would ever get of
    # it, and the client then fetches the same picture again and again (a batch
    # of twelve views turned into twelve steps of one).  The flag is derived
    # from the request itself, so it stays a pure function of the replay.
    last_answer = -1
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            last_answer = index
        elif item.get("type") in _ASSISTANT_ITEMS:
            last_answer = index
    turn = 0
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        # A turn is numbered by the user message that opens it, so an item that
        # belongs to that message shares its number.
        if item.get("type") == "message" and item.get("role") == "user":
            turn += 1
        # A hosted image-generation result keeps its bytes in a required field
        # that cannot become text, so it is not a candidate for replacement.
        if item.get("type") == "image_generation_call":
            data = _decode_data_url(item.get("result"))
            if data is None:
                continue
            found.append(
                _found_image(
                    image_id=_numbered(len([item for item in found if item.copy_of is None]) + 1),
                    key=f"{index}:result",
                    item=index,
                    part=None,
                    open_tag=None,
                    close_tag=None,
                    data=data,
                    turn=turn,
                    kind="generated",
                    unseen=index > last_answer,
                )
            )
            continue
        if known_files:
            opened = _viewed_file_id(item, known_files)
            if opened is not None:
                viewed_by_call[str(item.get("call_id") or "")] = opened
        field = "content" if item.get("type") == "message" else "output"
        parts = item.get(field)
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts):
            if _part_data_url(part) is None:
                continue
            open_tag: Optional[int] = None
            close_tag: Optional[int] = None
            # A user upload arrives wrapped in Codex's own
            # ``<image name=[Image #N] path="…">`` parts; both tags belong to the
            # image and are replaced together with it.
            if part_index >= 1 and _text_of(parts[part_index - 1]):
                if _OPEN_TAG.match(_text_of(parts[part_index - 1]) or ""):
                    open_tag = part_index - 1
            if part_index + 1 < len(parts) and _text_of(parts[part_index + 1]):
                if _CLOSE_TAG.match(_text_of(parts[part_index + 1]) or ""):
                    close_tag = part_index + 1
            data = _decode_data_url(_part_data_url(part))
            if data is None:
                continue
            name = None
            if open_tag is not None:
                name = _OPEN_TAG.match(_text_of(parts[open_tag]) or "").group(1)  # type: ignore[union-attr]
            # A copy the fetch tool returned names the occurrence it copies in
            # the line right before it.  It keeps that id and does not advance
            # the numbering, so an id the model already knows still means the
            # same image after a fetch.
            copy_of = None
            for look_back in (part_index - 1, part_index - 2):
                if look_back < 0:
                    continue
                line = _FETCHED_LINE.match(_text_of(parts[look_back]) or "")
                if line is not None:
                    copy_of = line.group(1)
                    break
            if copy_of is None:
                # The client's own viewer, opened on a file this pass wrote.
                # Its result carries the image and nothing else -- no tag, no
                # id -- so the id comes from the call that asked for it, which
                # names the path.
                copy_of = viewed_by_call.get(str(item.get("call_id") or ""))
            if copy_of is not None:
                found.append(
                    _found_image(
                        image_id=copy_of,
                        copy_of=copy_of,
                        key=f"{index}:{part_index}",
                        item=index,
                        part=part_index,
                        open_tag=open_tag,
                        close_tag=close_tag,
                        data=data,
                        turn=turn,
                        kind="tool",
                        name=name,
                        unseen=index > last_answer,
                    )
                )
                continue
            found.append(
                _found_image(
                    image_id=_numbered(len([item for item in found if item.copy_of is None]) + 1),
                    key=f"{index}:{part_index}",
                    item=index,
                    part=part_index,
                    open_tag=open_tag,
                    close_tag=close_tag,
                    data=data,
                    turn=turn,
                    kind=_kind_of(item.get("type"), item.get("role")),
                    name=name,
                    unseen=index > last_answer,
                )
            )
    return found


def _turn_count(input_items: Any) -> int:
    """How many user turns a replay contains."""

    if not isinstance(input_items, list):
        return 0
    return sum(
        1
        for item in input_items
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "user"
    )


def _found_image(
    *,
    image_id: str,
    copy_of: Optional[str] = None,
    key: str,
    item: int,
    part: Optional[int],
    open_tag: Optional[int],
    close_tag: Optional[int],
    data: Tuple[str, bytes],
    turn: int,
    kind: str,
    name: Optional[str] = None,
    unseen: bool = False,
) -> _Found:
    mime, raw = data
    width, height = _image_size(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return _Found(
        image_id=image_id,
        copy_of=copy_of,
        key=key,
        item=item,
        part=part,
        open_tag=open_tag,
        close_tag=close_tag,
        mime=mime,
        raw=raw,
        width=width,
        height=height,
        content_id=digest,
        kind=kind,
        name=name,
        turn=turn,
        unseen=unseen,
    )


def _numbered(index: int) -> str:
    return f"{_ID_PREFIX}{index:0{_ID_DIGITS}d}"


def _heading(image: _Found, state: str, lang: str) -> str:
    """One placeholder heading, in the reference implementation's own shape.

    The model reads these fields to tell two images apart after a compaction,
    so the order and the wording are Context Attachment Manager's: name, kind,
    turn, size -- and its own label when Codex wrote one around the upload.
    """

    return _heading_text(
        image.image_id,
        state,
        lang,
        name=image.name,
        kind=image.kind,
        turn=image.turn,
        width=image.width,
        height=image.height,
        label=None,
    )


def _heading_text(
    image_id: str,
    state: str,
    lang: str,
    *,
    name: object = None,
    kind: object = "tool",
    turn: object = 0,
    width: object = None,
    height: object = None,
    label: object = None,
) -> str:
    """One heading from plain values, for the callers that hold no ``_Found``.

    The fetch tool is a separate process (an MCP server the client starts), and
    it has the stored record of an image rather than this pass's scan result.
    It still has to write the heading this pass writes -- same fields, same
    order, same separators -- so the line is built here once and both sides
    name an image the same way.
    """

    words = _WORDS[lang]
    fields: List[str] = [str(name or words["unnamed"])]
    fields.append(str(words["kind"].get(str(kind), words["kind"]["tool"])))
    fields.append(str(words["turn"](int(turn or 0))))
    if width and height:
        fields.append(str(words["size"](int(width), int(height))))
    return str(words["heading"](image_id, label, state, fields))


def _placeholder(image: _Found, lang: str, *, state: Optional[str] = None) -> str:
    words = _WORDS[lang]
    if state is None:
        state = words["omitted"]
    kind = _PLACEHOLDER_KIND[lang]
    path = _FILE_PATHS.get(image.image_id)
    if kind == "file" and path:
        body = words["file_plain"](path)
    elif kind == "tool":
        body = words["auto_plain"](image.image_id)
    else:
        # No tool and no file: the only way back is the user sending it again.
        body = words["plain"](image.image_id)
    return f"{_heading(image, str(state), lang)}\n{body}"


# Which final line a placeholder carries.  They are not interchangeable: one
# tells the model to open a file with the client's own viewer, one to call the
# fetch tool, one to write "need IMG-xxx" for the user.  The choice is per
# request, so it is set through these module-level maps inside the pass instead
# of being threaded through every placeholder call.
_PLACEHOLDER_KIND: Dict[str, str] = {_LANG_ZH: "ask", _LANG_EN: "ask"}
_FILE_PATHS: Dict[str, str] = {}


def _duplicate_placeholder(
    image: _Found, twin: _Found, lang: str
) -> str:
    words = _WORDS[lang]
    where = str(words["turn"](twin.turn))
    return (
        f"{_heading(image, str(words['not_repeated']), lang)}\n"
        f"{words['duplicate'](twin.image_id, where)}"
    )


def _included_label(image: _Found, lang: str) -> str:
    """The line put before an image that is still sent.

    Codex's own ``[Image #N]`` restarts in every message, so once a placeholder
    names an id the model has no way to tell which of the images in front of it
    that id is.  Every sent image therefore carries its own id, which is how the
    model asks for the right one later.
    """

    return _heading(image, str(_WORDS[lang]["included"]), lang)


def fetched_label(
    image_id: str,
    *,
    lang: str = _LANG_ZH,
    name: object = None,
    kind: object = "tool",
    turn: object = 0,
    width: object = None,
    height: object = None,
) -> str:
    """The line the fetch tool puts before an image it returns.

    This is the half of "fetch by id" that keeps an id meaning one picture.  The
    pass decides what is a *copy* by reading this line back
    (``_FETCHED_LINE``): an image behind it is the one its id already names, so
    it keeps that id instead of taking the next number.  A tool that answered a
    fetch with a line of its own -- one this reader does not match -- therefore
    renumbered the picture the model had just asked for, and the same image
    answered two ids for the rest of the task.

    It is deliberately callable with plain values: the fetch tool is an MCP
    server the client starts, so it holds a stored record rather than this
    pass's scan result.
    """

    return _heading_text(
        image_id,
        str(_WORDS[lang]["fetched"]),
        lang,
        name=name,
        kind=kind,
        turn=turn,
        width=width,
        height=height,
        label=None,
    )


# --------------------------------------------------------------------------
# Rewrite
# --------------------------------------------------------------------------


def _suffix_start(input_items: List[Any]) -> int:
    """First item this pass may rewrite: everything after the last signed one."""

    from .base import _value_has_encrypted_content

    last_signed = -1
    for index, item in enumerate(input_items):
        if _value_has_encrypted_content(item):
            last_signed = index
    return last_signed + 1


def _has_codex_client_evidence(request_kwargs: Mapping[str, Any]) -> bool:
    from . import responses_request as _responses_request_module

    return _responses_request_module._request_has_codex_client_evidence(
        dict(request_kwargs)
    )


def _language(request_kwargs: Mapping[str, Any]) -> str:
    """The task's language, from the client's own instruction block."""

    blob = json.dumps(
        {
            key: request_kwargs.get(key)
            for key in ("instructions", "input", "messages")
            if request_kwargs.get(key) is not None
        },
        ensure_ascii=False,
        default=str,
    )
    return _LANG_ZH if re.search(r"[\u4e00-\u9fff]", blob) else _LANG_EN


# Codex names its task inside ``x-codex-turn-metadata`` as JSON, and that is
# the only place a proxy sees the id: the plain ``thread_id`` keys
# ``_trace_session_context`` looks for never appear at the top level of a real
# request.  Without this the replay cache has no key to file anything under and
# the fetch tool can never answer.
_TURN_METADATA_HEADERS = (
    "x-codex-turn-metadata",
    "X-Codex-Turn-Metadata",
)
_THREAD_ID_KEYS = ("thread_id", "threadId", "session_id", "conversation_id")


def _thread_id(request_kwargs: Mapping[str, Any]) -> Optional[str]:
    """The task this request belongs to, from the client's own turn metadata."""

    candidates: List[Any] = []
    for container_key in ("proxy_server_request", "headers", "request_headers"):
        container = request_kwargs.get(container_key)
        if isinstance(container, Mapping):
            candidates.append(container.get("headers") if container_key == "proxy_server_request" else container)
    for metadata_key in ("metadata", "litellm_metadata", "client_metadata"):
        container = request_kwargs.get(metadata_key)
        if isinstance(container, Mapping):
            candidates.append(container)
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for header_name in _TURN_METADATA_HEADERS:
            value = candidate.get(header_name)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, Mapping):
                for id_key in _THREAD_ID_KEYS:
                    found = parsed.get(id_key)
                    if isinstance(found, str) and found.strip():
                        return found.strip()
    from . import routing as _routing_module

    try:
        session = _routing_module._trace_session_context(dict(request_kwargs))
    except Exception:
        return None
    if isinstance(session, Mapping):
        for key in ("id", "thread_id", "session_id", "conversation_id"):
            value = session.get(key)
            if isinstance(value, str) and value:
                return value
    return session if isinstance(session, str) and session else None


def _deployment_allows_images(request_kwargs: Mapping[str, Any]) -> bool:
    """A route that refuses image input gets its history unchanged."""

    for key in ("_young_router_selected_deployment", "litellm_params"):
        container = request_kwargs.get(key)
        if not isinstance(container, Mapping):
            continue
        model_info = container.get("model_info")
        if isinstance(model_info, Mapping):
            from .base import _RESPONSES_IMAGE_INPUT_SUPPORT_KEY

            if model_info.get(_RESPONSES_IMAGE_INPUT_SUPPORT_KEY) is False:
                return False
            if model_info.get("supports_vision") is False:
                return False
    return True


def rewrite_for_size(
    request_kwargs: dict,
    *,
    keep: int = 0,
) -> Optional[dict]:
    """Omit earlier images although the setting is off.

    This is the same rewrite the setting performs, reached a different way: the
    replay cannot be sent at all otherwise.  A thread that keeps calling
    ``view_image`` carries every image it ever looked at, and an upstream that
    prices those bytes refuses the prompt long before any token counter says
    the window is full -- so the choice is a placeholder or a dead turn.

    Unlike the setting, this pass also reaches the images of the turn being
    answered, all but the newest result's, because a task with one user message
    keeps every picture it ever viewed in that one turn (see
    ``newest_picture_item`` / ``keep_picture_item`` in ``_rewrite``).
    """

    return _rewrite(request_kwargs, forced=True, keep=keep)


def _with_codex_image_context(request_kwargs: dict) -> Optional[dict]:
    """Leave the current turn's images in place and omit the earlier ones.

    Returns ``None`` -- "nothing changed" -- for every request this pass does
    not own: a disabled setting, a non-Responses route, a history without the
    client evidence that marks a Codex replay, a route that refuses images, and
    a replay whose only images are all in the current turn.
    """

    return _rewrite(request_kwargs)


def _declined(request_kwargs: Any, reason: str) -> None:
    """Say why this request was left alone, once per decision.

    A pass that silently declines is indistinguishable from a pass that is off,
    and that ambiguity already cost a debugging session: the log said the
    rewrite did not change the request, and nothing said which of these six
    conditions had been true.
    """

    if not isinstance(request_kwargs, dict):
        return
    from . import trace as _trace_module

    try:
        _trace_module._route_trace(
            "codex_image_context_declined",
            reason=reason,
            images=_image_count_of(request_kwargs),
            enabled=enabled(),
            mode=mode(),
            keep_recent=keep_recent(),
            min_bytes=min_bytes(),
        )
    except Exception:
        return


def _image_count_of(request_kwargs: Mapping[str, Any]) -> Optional[int]:
    try:
        return len(scan(request_kwargs.get("input")))
    except Exception:
        return None


def _rewrite(
    request_kwargs: dict,
    *,
    forced: bool = False,
    keep: Optional[int] = None,
) -> Optional[dict]:
    if not forced and not enabled():
        _declined(request_kwargs, "setting_disabled")
        return None
    request_kwargs = request_kwargs if isinstance(request_kwargs, dict) else {}
    if not _deployment_allows_images(request_kwargs):
        _declined(request_kwargs, "route_refuses_images")
        return None
    from . import responses_request as _responses_request_module

    if not _responses_request_module._request_has_responses_shape(request_kwargs):
        _declined(request_kwargs, "not_a_responses_request")
        return None
    if not _has_codex_client_evidence(request_kwargs):
        _declined(request_kwargs, "no_codex_client_evidence")
        return None
    if _responses_request_module._request_has_structured_codex_compaction(
        request_kwargs
    ):
        # A compaction request replays its signed history byte-exact: the
        # upstream compacts exactly what it receives, and a placeholder baked
        # into the compacted history would be permanent.
        _declined(request_kwargs, "structured_compaction")
        return None
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list) or not input_items:
        _declined(request_kwargs, "empty_input")
        return None

    # Every item is rewritable, including the ones before a signed item: a
    # client that turns off server-side storage stamps an encrypted reasoning
    # item on *every* turn, so a boundary at the last such item would leave all
    # of history frozen and nothing would ever be omitted.  The invariant is
    # narrower -- the signed items themselves are never touched, because this
    # pass only ever replaces image parts, and no signed item carries one.
    mutable_from = 0
    # The task is read here because the scan needs it: a picture the model
    # opened with the client's own viewer is named by the file the *previous*
    # request wrote for it, and that map is this task's own.
    thread_id = _thread_id(request_kwargs)
    images = scan(input_items, file_paths=_stored_file_paths(thread_id))
    if not images:
        _declined(request_kwargs, "no_images")
        return None
    # The turn being answered is the last one in the replay, whether or not it
    # carried an image: an image from an earlier turn is older than it even
    # when the current turn is pure text.
    current_turn = _turn_count(input_items)
    keep = keep_recent() if keep is None else keep
    minimum = min_bytes()
    # A Codex task *is* one turn: a single user message followed by hundreds of
    # steps, and every ``view_image`` result of it stays inside that one turn for
    # ever.  A pass that only reached earlier turns was therefore inert on
    # exactly the thread that needed it -- one real task replayed 74 pictures
    # (50.4 MB of base64) on every step until the upstream refused the prompt as
    # too long.  Size mode always reached that far; the setting now does too, so
    # a turn that keeps calling ``view_image`` stops paying for the same
    # pictures on the request that follows.
    #
    # What the turn's own upload is exempt from, and why: it is the picture the
    # user just attached and the one the task is about, the note below promises
    # it is provided as usual, and a model shown a placeholder for it on the very
    # first request would answer about an image it never saw.
    #
    # Which tool result stays is decided by the *cache*, not by recency.  Keeping
    # "the newest picture" moves the kept picture forward on every step, so the
    # step that adds a result rewrites an item that was already on the wire and
    # the upstream re-reads everything after it: measured on a real 20-image
    # replay, a request shared only 85.8% of its bytes with the one before it.
    # Keeping the *oldest* result that is still inside the turn instead means an
    # item is written once and never touched again, so every later step shares a
    # prefix back to that item.  What the model still needs is never more than
    # one fetch away: a placeholder names its id and the fetch tool answers it,
    # and the newest picture is delivered as the result of the call that just
    # returned it, which no later request can take away.
    inside = inside_turn() == "turn"
    # Which picture inside the turn being answered survives.  It is the *oldest*
    # tool result of that turn, deliberately, and both passes use it:
    #
    # * The setting's own pass needs it because a kept picture that moves forward
    #   with every step rewrites an item already on the wire, and the upstream
    #   then re-reads every byte after it.  Measured on a real 20-image replay, a
    #   request shared only 85.8% of its prefix with the one before it.
    # * Size mode needs the same anchor for a sharper reason: the two passes run
    #   on the same request (this one fixes what it can, ``context_window`` then
    #   shrinks again if the route still refuses), and an anchor chosen
    #   differently by each means the second pass rewrites what the first just
    #   stabilised.  Measured on a live thread, that kept the hit rate at 89-96%
    #   when the image pass alone held it at 100%.
    #
    # ``None`` means every tool result of the turn is still sent (nothing to
    # anchor on); size mode then falls back to the newest picture overall, which
    # is what it did before and is still the best single survivor there.
    in_turn_tool_pictures = [
        image.item
        for image in images
        if image.copy_of is None
        and image.kind != "upload"
        and image.turn >= current_turn
    ]
    keep_picture_item = min(in_turn_tool_pictures) if in_turn_tool_pictures else None
    # Pictures of the turn being answered stay visible for a while, not for a
    # single step.  Retiring one as soon as the next request carries it makes the
    # model work from placeholders: a design task that had just looked at a dozen
    # pages found "many images as placeholders", started stitching contact sheets
    # of its own and asked for one page per step.  The window keeps the newest
    # ``_VISIBLE_PICTURES`` of the turn visible and retires the rest, which still
    # bounds the request: what a step adds is one picture, what it drops is the
    # one that fell out of the window.
    # A count is the wrong unit: ten thumbnails and ten 2240x3040 scans differ by
    # an order of magnitude in bytes and in the tokens the route charges for
    # them.  The window is therefore a budget of estimated picture tokens, taken
    # newest first, capped by a count so a step that renders fifty tiny icons
    # cannot fill the request either.
    visible_window = set()
    if in_turn_tool_pictures:
        by_item = {
            image.item: image
            for image in images
            if image.copy_of is None
            and image.kind != "upload"
            and image.turn >= current_turn
        }
        budget = _visible_tokens()
        spent = 0
        # A budget of zero is the setting with no window at all: every picture of
        # the turn but its first is retired, which is what the size repair wants.
        candidates = sorted(in_turn_tool_pictures, reverse=True) if budget > 0 else []
        for item in candidates:
            picture = by_item.get(item)
            cost = _picture_tokens(picture)
            if visible_window and spent + cost > budget:
                break
            visible_window.add(item)
            spent += cost
            if len(visible_window) >= _visible_pictures():
                break
    if not forced and keep_picture_item is not None and len(in_turn_tool_pictures) < 2:
        # Leaving the only picture of the turn alone is the setting's rule; size
        # mode has no such choice to make.
        keep_picture_item = None
    newest_picture_item = (
        max(image.item for image in images)
        if forced and images and keep_picture_item is None
        else None
    )
    selection = read_selection(thread_id)
    auto = selection.get("auto")
    automatic = True if forced else (auto if isinstance(auto, bool) else mode() == "auto")
    unchecked = set(selection.get("unchecked") or set())
    pinned = set(selection.get("pinned") or set())

    lang = _language(request_kwargs)
    replacements: Dict[int, Dict[int, str]] = {}
    omitted: List[_Found] = []
    sent_content: Dict[str, _Found] = {}
    omitted_keys: set = set()
    for image in images:
        image_id = image.image_id or ""
        in_mutable = image.item >= mutable_from
        aged = image.turn < current_turn and (current_turn - image.turn) > keep
        if (
            forced
            and newest_picture_item is not None
            and image.item < newest_picture_item
            # Size mode has the same duty as the setting: a picture no request
            # has carried yet must reach the model at least once, or "fetch by
            # id" becomes the only way to see the page it just rendered.
            and not image.unseen
        ):
            aged = True
        # The turn being answered: its tool results are omitted inside it, its
        # upload is not.  ``in_current_turn`` also decides which placeholder
        # wording is honest -- a picture this same turn produced was seen in this
        # turn, not an earlier one.
        in_current_turn = image.turn >= current_turn and current_turn > 0
        # A picture the model has not been shown yet is never replaced by a
        # placeholder -- unless a byte-identical copy earlier in this same
        # request already carries the same content, in which case the model is
        # looking straight at it.
        twin_already_sent = (image.content_id or "") in sent_content
        blindable = not image.unseen or twin_already_sent
        if (
            image.copy_of is None
            and image.kind != "upload"
            and keep_picture_item is not None
            and image.item != keep_picture_item
            # Size mode is the repair for a route that already refused, so it
            # retires freely; the window belongs to the setting's own pass, whose
            # job is to keep the model looking at what it just worked on.
            and (forced or image.item not in visible_window)
            and (forced or (inside and in_current_turn))
            and blindable
        ):
            aged = True
        big = len(image.raw) >= minimum
        if image_id in pinned:
            sent_content[image.content_id or ""] = image
            continue
        if image.copy_of is not None:
            # A copy the fetch tool returned belongs to the turn that fetched
            # it; every later turn leaves it out again, in either mode.  Its
            # original keeps its own place and its own state.
            if image.turn < current_turn:
                omitted_keys.add(image.key)
                omitted.append(image)
            else:
                sent_content[image.content_id or ""] = image
            continue
        drop = image_id in unchecked or (automatic and aged and big)
        if drop and not in_mutable:
            # A frozen image stays, but it still counts as sent: a placeholder
            # elsewhere may name it.
            sent_content[image.content_id or ""] = image
            continue
        if not drop:
            sent_content[image.content_id or ""] = image
            continue
        omitted_keys.add(image.key)
        omitted.append(image)

    # The replay is kept even when nothing is omitted -- the fetch tool has to
    # know every image the task carried, including one the client compacts
    # away later -- and it is the same copy the placeholders are built from, so
    # it is written before the placeholder decision.
    _store_replay(thread_id, request_kwargs)
    _FILE_PATHS.clear()
    if thread_id:
        from . import image_context_store as _store_module

        _FILE_PATHS.update(_store_module.paths(thread_id))

    # Byte-identical images are one content: when a content would be omitted
    # everywhere, one copy stays sent and the others point at it.  Which copy is
    # ``min`` and not ``max`` for the same reason the turn keeps its first
    # picture: the newest copy moves forward on every step, so its placeholder
    # elsewhere in the replay has to be rewritten to name it, and each rewrite
    # is a prefix the upstream re-reads.  The *oldest* copy never moves.
    for content_id in {image.content_id for image in omitted if image.copy_of is None}:
        if content_id in sent_content:
            continue
        copies = [
            image
            for image in omitted
            if image.content_id == content_id and image.copy_of is None
        ]
        if len(copies) < 2:
            continue
        anchor = min(copies, key=lambda image: image.item)
        omitted = [image for image in omitted if image is not anchor]
        sent_content[content_id or ""] = anchor
    if not omitted:
        # Every image was either in the turn being answered, pinned, or below
        # the byte floor.  This is the one decline that depends on the replay's
        # own shape, so it states what it saw.
        _declined(
            request_kwargs,
            "no_image_old_enough"
            if not any(
                (image.turn < current_turn and len(image.raw) >= minimum)
                or (
                    image.turn >= current_turn
                    and image.kind != "upload"
                    and keep_picture_item is not None
                    and image.item != keep_picture_item
                    and len(image.raw) >= minimum
                )
                for image in images
            )
            else "nothing_omitted",
        )
        return None

    # A duplicate placeholder points at a copy that is still being sent, so the
    # model keeps the content without the bytes being uploaded twice.
    # The final line may only name the fetch tool when that tool exists here;
    # otherwise the model is told to ask the user, which is the one action that
    # can still bring the image back on a gateway with no tool of its own.
    kind = fetch_kind(request_kwargs=request_kwargs) if automatic else "ask"
    if (
        forced
        and kind == "ask"
        and _client_config_present()
        and not _request_is_remote_client(request_kwargs)
    ):
        # The proxy and the client share this machine, and the replay cache has
        # already written every image to disk, so a placeholder can name a path
        # the client's own viewer opens -- the one avenue that needs no tool and
        # no change to the client's configuration.  A remote gateway has no such
        # path to offer, so there it keeps saying "ask the user".
        kind = "file"
    _PLACEHOLDER_KIND[lang] = kind
    for image in omitted:
        if image.copy_of is not None:
            replacements.setdefault(image.item, {})[image.part or 0] = str(
                _WORDS[lang]["copy"](image.copy_of)
            )
            if image.close_tag is not None:
                replacements[image.item][image.close_tag] = ""
            for position in range((image.part or 0) + 1, (image.close_tag or image.part or 0) + 1):
                replacements[image.item][position] = ""
            continue
        twin = sent_content.get(image.content_id or "")
        text = _placeholder(image, lang)
        if twin is not None and twin.key != image.key:
            text = _duplicate_placeholder(image, twin, lang)
        if image.part is None:
            continue
        first = image.open_tag if image.open_tag is not None else image.part
        last = image.close_tag if image.close_tag is not None else image.part
        bucket = replacements.setdefault(image.item, {})
        bucket[first] = text
        for position in range(first + 1, last + 1):
            bucket[position] = ""

    # Every image still sent states its id, so a placeholder that names one can
    # be matched to the picture in front of the model.  This is an *insertion*
    # before the image, never a replacement: an upload with no Codex tag around
    # it would otherwise be swapped for the label and the image would vanish.
    # Only the mutable suffix may change, so a frozen image stays byte-identical.
    omitted_keys_all = {image.key for image in omitted}
    labels: Dict[int, Dict[int, str]] = {}
    for image in images:
        if image.key in omitted_keys_all or image.item < mutable_from:
            continue
        if image.part is None:
            continue
        at = image.open_tag if image.open_tag is not None else image.part
        labels.setdefault(image.item, {}).setdefault(at, _included_label(image, lang))

    rewritten: List[Any] = []
    for index, item in enumerate(input_items):
        bucket = replacements.get(index)
        inserts = labels.get(index)
        if (not bucket and not inserts) or not isinstance(item, dict):
            rewritten.append(item)
            continue
        field = "content" if item.get("type") == "message" else "output"
        parts = item.get(field)
        if not isinstance(parts, list):
            rewritten.append(item)
            continue
        next_parts: List[Any] = []
        for position, part in enumerate(parts):
            label = (inserts or {}).get(position)
            if label is not None:
                next_parts.append({"type": "input_text", "text": label})
            if position not in (bucket or {}):
                next_parts.append(part)
                continue
            text = (bucket or {})[position]
            if not text:
                continue
            next_parts.append({"type": "input_text", "text": text})
        updated = item.copy()
        updated[field] = next_parts
        rewritten.append(updated)

    # The note goes before the user message that opens the first affected turn,
    # so the placeholder text never splits a tool call from its result.
    first_omitted = min(image.item for image in omitted)
    note_at = first_omitted
    while note_at > 0 and not (
        isinstance(rewritten[note_at], dict)
        and rewritten[note_at].get("type") == "message"
        and rewritten[note_at].get("role") == "user"
    ):
        note_at -= 1
    words = _WORDS[lang]
    kind = _PLACEHOLDER_KIND[lang]
    if automatic and kind == "file":
        note = words["auto_file_note"]
    elif automatic and kind == "tool":
        note = words["auto_note"]
    elif automatic:
        # Omitted by this pass with no tool to fetch them back: the model has to
        # ask the user, so the note must say that instead of naming a tool.
        note = words["auto_ask_note"]
    else:
        note = words["manual_note"]
    rewritten.insert(
        note_at,
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": note}],
        },
    )

    result = dict(request_kwargs)
    result["input"] = rewritten
    _trace(request_kwargs, omitted, automatic, len(images))
    return result


def _stored_file_paths(thread_id: Optional[str]) -> Dict[str, str]:
    """This task's ``{id: path}`` map from the replay it already has.

    Read before this request's own replay is written, so it is the map the
    previous request published -- which is the one the pictures in this replay
    were opened from.
    """

    if not thread_id:
        return {}
    from . import image_context_store as _store_module

    try:
        return dict(_store_module.paths(thread_id))
    except Exception:
        return {}


def _store_replay(thread_id: Optional[str], request_kwargs: Mapping[str, Any]) -> None:
    """Keep this request's images so the fetch tool can answer for them."""

    if not thread_id:
        return
    from . import image_context_store as _store_module

    try:
        _store_module.store(thread_id, dict(request_kwargs))
    except Exception:
        # A turn must not fail because its replay could not be cached; the
        # placeholders are still valid, and the model is told which ids the
        # fetch tool can no longer answer.
        pass


def _trace(
    request_kwargs: Mapping[str, Any],
    omitted: List[_Found],
    automatic: bool,
    total: int,
) -> None:
    from . import routing as _routing_module
    from . import trace as _trace_module

    try:
        _trace_module._route_trace(
            "codex_image_context",
            request_id=_routing_module._trace_request_id(dict(request_kwargs)),
            session=_routing_module._trace_session_context(dict(request_kwargs)),
            automatic=automatic,
            images=total,
            omitted=len(omitted),
            omitted_bytes=sum(len(image.raw) for image in omitted),
            ids=[image.image_id for image in omitted][:16],
        )
    except Exception:
        # A trace must never decide a request.
        pass


CODEX_IMAGE_CONTEXT_REQUEST_STEPS = (_with_codex_image_context,)


# --------------------------------------------------------------------------
# The fetch side: what the model's own tool answers with
# --------------------------------------------------------------------------


def image_for_turn(
    request_kwargs: Mapping[str, Any],
    ids: List[str],
) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """Resolve placeholder ids against the replay that is being served.

    Returns one ``(id, mime, data_url)`` per requested id, with ``None`` for a
    data URL the replay no longer carries -- which is what a session looks like
    after the client compacted the image away.  The bytes come from the request
    itself, so this never reads a transcript, a path, or the client's
    filesystem.
    """

    wanted = [value.strip().upper() for value in ids if isinstance(value, str)]
    index: Dict[str, _Found] = {}
    for image in scan(request_kwargs.get("input")):
        index[image.image_id or ""] = image
    resolved: List[Tuple[str, Optional[str], Optional[str]]] = []
    for value in wanted:
        image = index.get(value)
        if image is None or not isinstance(image.mime, str):
            resolved.append((value, None, None))
            continue
        encoded = base64.b64encode(image.raw).decode("ascii")
        resolved.append((value, image.mime, f"data:{image.mime};base64,{encoded}"))
    return resolved


def available_ids(request_kwargs: Mapping[str, Any]) -> List[str]:
    """Every id the current replay can answer for, oldest first."""

    seen: List[str] = []
    for image in scan(request_kwargs.get("input")):
        image_id = image.image_id or ""
        if image_id not in seen:
            seen.append(image_id)
    return seen
