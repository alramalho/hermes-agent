"""Hermes terminal-relay plugin.

Routes messages from a gateway chat (Telegram, Discord, etc.) into a
persistent tmux session running an interactive coding CLI such as Codex or
Claude Code. This is intentionally implemented as a user plugin instead of a
core Hermes patch: it uses the supported plugin command + pre_gateway_dispatch
hook surfaces and can be versioned/published independently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_NAME = "terminal-relay"
STATE_DIR = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "state" / PLUGIN_NAME
STATE_FILE = STATE_DIR / "sessions.json"
DEFAULT_CAPTURE_LINES = int(os.environ.get("HERMES_RELAY_CAPTURE_LINES", "90"))
DEFAULT_CAPTURE_DELAY = float(os.environ.get("HERMES_RELAY_CAPTURE_DELAY", "1.5"))
DEFAULT_MAX_REPLY_CHARS = int(os.environ.get("HERMES_RELAY_MAX_REPLY_CHARS", "3500"))
DEFAULT_CWD = Path(os.environ.get("HERMES_RELAY_DEFAULT_CWD", str(Path.home()))).expanduser()

CONTROL_COMMANDS = {
    "relay_start", "relay-start", "relay",
    "relay_end", "relay-end",
    "relay_status", "relay-status",
    "relay_capture", "relay-capture",
    "codex_init", "codex-init",
    "codex_end", "codex-end",
    "codex_status", "codex-status",
    "codex_capture", "codex-capture",
    "claude_init", "claude-init",
    "claude_end", "claude-end",
    "claude_status", "claude-status",
    "claude_capture", "claude-capture",
}

BACKENDS = {"codex", "claude"}


@dataclass
class Command:
    name: str
    args: str


def _load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _platform_value(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "")


def _route_key(source: Any) -> str:
    """Scope relay sessions to a platform chat/thread.

    In Telegram topic mode, thread_id keeps independent topic lanes separate.
    In plain DMs, chat_id is already unique to the user.
    """
    parts = [
        _platform_value(source) or "unknown",
        str(getattr(source, "chat_id", "") or "unknown-chat"),
    ]
    thread_id = getattr(source, "thread_id", None)
    if thread_id not in (None, "", "1"):
        parts.append(str(thread_id))
    return ":".join(parts)


def _parse_command(text: str | None) -> Command | None:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    first, _, rest = raw[1:].partition(" ")
    # Telegram bot commands may arrive as /cmd@BotName.
    first = first.split("@", 1)[0]
    return Command(first.strip().lower(), rest.strip())


def _usage() -> str:
    return (
        "**Terminal relay commands**\n\n"
        "Start a session:\n"
        "`/relay_start codex [cwd]`\n"
        "`/relay_start claude [cwd]`\n"
        "`/codex_init [cwd]`\n"
        "`/claude_init [cwd]`\n\n"
        "While active, normal messages in this chat are sent to the tmux session.\n\n"
        "Manage it:\n"
        "`/relay_status`, `/relay_capture`, `/relay_end`\n"
        "or backend aliases: `/codex_status`, `/codex_capture`, `/codex_end`."
    )


def _tmux_name_for(source: Any, backend: str) -> str:
    key = _route_key(source)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-")[:80]
    return f"hermes-{backend}-{safe}"


def _run(cmd: list[str], *, timeout: float = 10.0, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _tmux_exists(session: str) -> bool:
    return _run(["tmux", "has-session", "-t", session], timeout=3).returncode == 0


def _resolve_cwd(raw: str | None) -> tuple[Path | None, str | None]:
    text = (raw or "").strip()
    if not text:
        path = DEFAULT_CWD
    else:
        try:
            # Only use the first shell-style argument as cwd. The /relay_start
            # command reserves the first arg for backend and the second for cwd.
            path = Path(shlex.split(text)[0]).expanduser()
        except Exception as exc:
            return None, f"Could not parse cwd: {exc}"
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists() or not path.is_dir():
        return None, f"CWD does not exist or is not a directory: `{path}`"
    return path, None


def _backend_command(backend: str) -> str:
    if backend == "codex":
        return os.environ.get("HERMES_RELAY_CODEX_CMD", "codex")
    if backend == "claude":
        return os.environ.get("HERMES_RELAY_CLAUDE_CMD", "claude")
    raise ValueError(f"Unsupported backend: {backend}")


def _start_tmux(session: str, backend: str, cwd: Path) -> tuple[bool, str]:
    if shutil.which("tmux") is None:
        return False, "`tmux` is not installed or not on PATH."

    cmd = _backend_command(backend)
    executable = shlex.split(cmd)[0] if cmd.strip() else backend
    if shutil.which(executable) is None:
        return False, f"`{executable}` is not installed or not on PATH."

    if _tmux_exists(session):
        return True, "already-running"

    # Run via sh -lc so command overrides may include flags, while cwd is passed
    # separately to tmux instead of shell-concatenated.
    proc = _run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "140", "-y", "45", "-c", str(cwd), "sh", "-lc", cmd],
        timeout=10,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tmux new-session failed").strip()
    return True, "started"


def _capture(session: str, lines: int = DEFAULT_CAPTURE_LINES, max_chars: int = DEFAULT_MAX_REPLY_CHARS) -> str:
    if not _tmux_exists(session):
        return ""
    start = f"-{max(1, int(lines))}"
    proc = _run(["tmux", "capture-pane", "-t", session, "-p", "-S", start], timeout=5)
    text = (proc.stdout or proc.stderr or "").strip()
    # Strip common ANSI escapes if any leaked through.
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    # Collapse huge blank runs.
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if len(text) > max_chars:
        text = "…\n" + text[-max_chars:]
    return text


def _capture_delta(before: str, after: str, max_chars: int = DEFAULT_MAX_REPLY_CHARS) -> str:
    """Return the part of a tmux capture that changed after a prompt.

    This is still terminal screen scraping (Codex/Claude do not provide a
    structured "latest reply" protocol in interactive TUI mode), but trimming
    common prefix/suffix avoids reposting the startup banner and old prompts on
    every Telegram message.
    """
    if not after:
        return ""
    if not before:
        delta = after
    else:
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        prefix = 0
        max_prefix = min(len(before_lines), len(after_lines))
        while prefix < max_prefix and before_lines[prefix] == after_lines[prefix]:
            prefix += 1

        suffix = 0
        max_suffix = min(len(before_lines) - prefix, len(after_lines) - prefix)
        while (
            suffix < max_suffix
            and before_lines[len(before_lines) - 1 - suffix]
            == after_lines[len(after_lines) - 1 - suffix]
        ):
            suffix += 1

        changed = after_lines[prefix: len(after_lines) - suffix if suffix else len(after_lines)]
        delta = "\n".join(changed).strip()
        if not delta:
            # TUI redraws can defeat line-level diffing. Prefer a short tail
            # over an empty response so users still get actionable context.
            delta = "\n".join(after_lines[-25:]).strip()

    if len(delta) > max_chars:
        delta = "…\n" + delta[-max_chars:]
    return delta


def _send_keys(session: str, text: str) -> tuple[bool, str]:
    if not _tmux_exists(session):
        return False, "tmux session is no longer running"
    # Put the exact user message in a tmux paste buffer, paste it into the TUI,
    # then submit with C-m. This is more reliable than passing arbitrary text as
    # tmux key names and avoids Enter/newline ambiguity in prompt_toolkit TUIs.
    buffer_name = f"{PLUGIN_NAME}-{int(time.time() * 1000)}"
    proc = _run(["tmux", "set-buffer", "-b", buffer_name, "--", text], timeout=5)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tmux set-buffer failed").strip()
    proc = _run(["tmux", "paste-buffer", "-b", buffer_name, "-t", session], timeout=5)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tmux paste-buffer failed").strip()
    proc = _run(["tmux", "send-keys", "-t", session, "C-m"], timeout=5)
    _run(["tmux", "delete-buffer", "-b", buffer_name], timeout=2)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tmux send-keys failed").strip()
    return True, "sent"


def _kill(session: str) -> tuple[bool, str]:
    if not _tmux_exists(session):
        return True, "not-running"
    proc = _run(["tmux", "kill-session", "-t", session], timeout=5)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tmux kill-session failed").strip()
    return True, "killed"


def _metadata_for(event: Any) -> dict[str, Any]:
    source = getattr(event, "source", None)
    metadata: dict[str, Any] = {}
    thread_id = getattr(source, "thread_id", None)
    if thread_id not in (None, ""):
        metadata["thread_id"] = thread_id
        if _platform_value(source) == "telegram" and getattr(source, "chat_type", None) == "dm":
            metadata["telegram_dm_topic_reply_fallback"] = True
            if str(thread_id) not in {"", "1"}:
                metadata["direct_messages_topic_id"] = str(thread_id)
    msg_id = getattr(event, "message_id", None) or getattr(event, "reply_to_message_id", None)
    if msg_id is not None:
        metadata["telegram_reply_to_message_id"] = str(msg_id)
    return metadata


def _schedule_send(gateway: Any, event: Any, content: str) -> None:
    source = getattr(event, "source", None)
    adapter = None
    try:
        adapter = gateway.adapters.get(getattr(source, "platform", None))
    except Exception:
        adapter = None
    if adapter is None:
        return

    async def _send() -> None:
        try:
            await adapter.send(getattr(source, "chat_id", ""), content, metadata=_metadata_for(event))
        except TypeError:
            # Some adapters do not accept metadata.
            await adapter.send(getattr(source, "chat_id", ""), content)
        except Exception as exc:
            print(f"[{PLUGIN_NAME}] send failed: {exc}", flush=True)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        # Should not happen inside the gateway, but keep CLI tests harmless.
        try:
            asyncio.run(_send())
        except Exception:
            pass


def _format_capture(session: str, prefix: str = "", body: str | None = None) -> str:
    cap = _capture(session) if body is None else body.strip()
    if not cap:
        return (prefix + "\n" if prefix else "") + "No captured output yet."
    header = (prefix + "\n\n" if prefix else "") + "```text\n"
    return header + cap + "\n```"


def _state_entry_for(source: Any) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    state = _load_state()
    key = _route_key(source)
    entry = state.get(key)
    return state, key, entry if isinstance(entry, dict) else None


def _handle_start(source: Any, backend: str, cwd_text: str) -> str:
    if backend not in BACKENDS:
        return f"Unsupported backend `{backend}`. Use `codex` or `claude`."
    cwd, err = _resolve_cwd(cwd_text)
    if err:
        return err
    assert cwd is not None

    session = _tmux_name_for(source, backend)
    ok, msg = _start_tmux(session, backend, cwd)
    if not ok:
        return f"❌ Failed to start {backend}: {msg}"

    state = _load_state()
    key = _route_key(source)
    state[key] = {
        "active": True,
        "backend": backend,
        "tmux_session": session,
        "cwd": str(cwd),
        "route_key": key,
        "platform": _platform_value(source),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "thread_id": str(getattr(source, "thread_id", "") or ""),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _save_state(state)
    verb = "already running" if msg == "already-running" else "started"
    return (
        f"✅ **{backend.title()} relay {verb}.**\n"
        f"tmux: `{session}`\n"
        f"cwd: `{cwd}`\n\n"
        f"Normal messages in this chat now go to {backend}. Use `/relay_end` to return to Hermes."
    )


def _handle_end(source: Any, expected_backend: str | None = None) -> str:
    state, key, entry = _state_entry_for(source)
    if not entry:
        return "No active terminal relay for this chat."
    backend = str(entry.get("backend") or "")
    if expected_backend and backend and backend != expected_backend:
        return f"Active relay is `{backend}`, not `{expected_backend}`. Use `/relay_end` to end it."
    session = str(entry.get("tmux_session") or "")
    ok, msg = _kill(session) if session else (True, "no-session")
    state.pop(key, None)
    _save_state(state)
    if not ok:
        return f"⚠️ Removed relay state, but failed to kill tmux `{session}`: {msg}"
    return f"✅ Ended {backend or 'terminal'} relay. Normal Hermes chat is restored."


def _handle_status(source: Any) -> str:
    _state, _key, entry = _state_entry_for(source)
    if not entry:
        return "No active terminal relay for this chat."
    session = str(entry.get("tmux_session") or "")
    running = _tmux_exists(session) if session else False
    return (
        "**Active terminal relay**\n"
        f"backend: `{entry.get('backend')}`\n"
        f"tmux: `{session}`\n"
        f"running: `{running}`\n"
        f"cwd: `{entry.get('cwd')}`\n"
        f"route: `{entry.get('route_key')}`"
    )


def _handle_capture(source: Any) -> str:
    _state, _key, entry = _state_entry_for(source)
    if not entry:
        return "No active terminal relay for this chat."
    session = str(entry.get("tmux_session") or "")
    return _format_capture(session)


def _command_reply(cmd: Command, source: Any) -> str | None:
    name = cmd.name
    args = cmd.args

    if name in {"relay", "relay_start", "relay-start"}:
        # Allow `/relay start codex ~/repo` and `/relay_start codex ~/repo`.
        parts = shlex.split(args) if args else []
        if name == "relay" and parts and parts[0] == "start":
            parts = parts[1:]
        if name == "relay" and parts and parts[0] in {"end", "stop"}:
            return _handle_end(source)
        if name == "relay" and parts and parts[0] == "status":
            return _handle_status(source)
        if name == "relay" and parts and parts[0] == "capture":
            return _handle_capture(source)
        if not parts:
            return _usage()
        backend = parts[0].lower()
        cwd_text = " ".join(shlex.quote(p) for p in parts[1:])
        return _handle_start(source, backend, cwd_text)

    if name in {"codex_init", "codex-init"}:
        return _handle_start(source, "codex", args)
    if name in {"claude_init", "claude-init"}:
        return _handle_start(source, "claude", args)
    if name in {"relay_end", "relay-end"}:
        return _handle_end(source)
    if name in {"codex_end", "codex-end"}:
        return _handle_end(source, "codex")
    if name in {"claude_end", "claude-end"}:
        return _handle_end(source, "claude")
    if name in {"relay_status", "relay-status", "codex_status", "codex-status", "claude_status", "claude-status"}:
        return _handle_status(source)
    if name in {"relay_capture", "relay-capture", "codex_capture", "codex-capture", "claude_capture", "claude-capture"}:
        return _handle_capture(source)
    return None


def _relay_message(source: Any, text: str) -> str:
    state, key, entry = _state_entry_for(source)
    if not entry or not entry.get("active"):
        return ""
    session = str(entry.get("tmux_session") or "")
    if not session or not _tmux_exists(session):
        state.pop(key, None)
        _save_state(state)
        return "⚠️ Relay tmux session is gone; relay state was cleared. Normal Hermes chat is restored."

    before = _capture(session)
    ok, msg = _send_keys(session, text)
    if not ok:
        return f"❌ Failed to send to tmux `{session}`: {msg}"

    entry["updated_at"] = time.time()
    state[key] = entry
    _save_state(state)

    if DEFAULT_CAPTURE_DELAY > 0:
        time.sleep(min(DEFAULT_CAPTURE_DELAY, 5.0))
    after = _capture(session)
    delta = _capture_delta(before, after)
    return _format_capture(
        session,
        prefix=f"↪️ Sent to `{entry.get('backend')}` (`{session}`). Showing changed output only.",
        body=delta,
    )


def _pre_gateway_dispatch(event: Any = None, gateway: Any = None, **_: Any) -> dict[str, Any] | None:
    if event is None or gateway is None:
        return None
    text = getattr(event, "text", None) or ""
    source = getattr(event, "source", None)
    if source is None:
        return None

    cmd = _parse_command(text)
    if cmd and cmd.name in CONTROL_COMMANDS:
        reply = _command_reply(cmd, source) or _usage()
        _schedule_send(gateway, event, reply)
        return {"action": "skip", "reason": f"{PLUGIN_NAME}:control-command"}

    # Never hijack unknown slash commands. They should continue through Hermes'
    # normal command dispatcher even if a relay is active.
    if cmd:
        return None

    state, _key, entry = _state_entry_for(source)
    if not entry or not entry.get("active"):
        return None

    reply = _relay_message(source, text)
    if reply:
        _schedule_send(gateway, event, reply)
    return {"action": "skip", "reason": f"{PLUGIN_NAME}:active-relay"}


def _slash_placeholder(raw_args: str = "") -> str:
    del raw_args
    return (
        "Terminal relay commands are handled in the gateway before Hermes' normal "
        "slash-command dispatcher. If you are seeing this, use the command from "
        "Telegram/gateway or restart the gateway after enabling the plugin.\n\n" + _usage()
    )


def register(ctx: Any) -> None:
    # Register slash commands so they appear in command menus/help. The actual
    # gateway implementation is in pre_gateway_dispatch because plugin slash
    # handlers receive only raw_args, not the MessageEvent needed for routing.
    for name in [
        "relay_start", "relay_end", "relay_status", "relay_capture",
        "codex_init", "codex_end", "codex_status", "codex_capture",
        "claude_init", "claude_end", "claude_status", "claude_capture",
    ]:
        ctx.register_command(name, _slash_placeholder, description=f"Terminal relay: /{name}")
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
