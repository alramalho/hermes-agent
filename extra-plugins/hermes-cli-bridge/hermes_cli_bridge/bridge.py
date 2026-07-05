"""tmux-backed Codex/Claude bridge for Hermes gateway chats."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .profiles import PROFILES, AgentProfile

logger = logging.getLogger("gateway.cli_bridge")

_OSC_RE = re.compile(r"\x1B\](?:[^\x07\x1B]|\x1B(?!\\))*?(?:\x07|\x1B\\)")
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NO_SELECTED_SESSION = "\x00none"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "session"


def _event_log_fields(event: Any) -> dict[str, str]:
    source = getattr(event, "source", None)
    return {
        "platform": _platform_value(getattr(source, "platform", "")),
        "chat": str(getattr(source, "chat_id", "") or ""),
        "user": str(getattr(source, "user_id", "") or ""),
    }


def _redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        return text


@dataclass
class BridgeSession:
    agent: str
    key: str
    base_key: str
    name: str
    session_name: str
    cwd: Path
    command: str
    log_path: Path
    backend: str = "tmux"
    thread_id: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    exec_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    approval_signature: str | None = None
    send_mutex: threading.Lock = field(default_factory=threading.Lock)
    queued_sends: deque = field(default_factory=deque)
    sender_active: bool = False
    restored: bool = False


class TmuxClient:
    """Small tmux wrapper kept separate for unit tests."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self._runner = runner or subprocess.run
        self._key_delay = _env_float("HERMES_CLI_BRIDGE_TMUX_KEY_DELAY", 0.15)

    def ensure_available(self) -> None:
        if shutil.which("tmux") is None:
            raise RuntimeError("tmux is not installed or not on PATH")

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            args,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=input,
        )

    def start(
        self,
        *,
        session_name: str,
        cwd: Path,
        command: str,
        log_path: Path,
        pipe_log: bool = False,
    ) -> None:
        self.ensure_available()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        self._run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-x",
                "140",
                "-y",
                "40",
                "-c",
                str(cwd),
                command,
            ]
        )
        if not pipe_log:
            return
        self._run(
            [
                "tmux",
                "pipe-pane",
                "-o",
                "-t",
                session_name,
                f"cat >> {shlex.quote(str(log_path))}",
            ]
        )

    def capture(self, session_name: str) -> str:
        result = self._run(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-J",
                "-t",
                session_name,
            ],
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def has_session(self, session_name: str) -> bool:
        result = self._run(
            ["tmux", "has-session", "-t", session_name],
            check=False,
        )
        return result.returncode == 0

    def send_input(
        self,
        session_name: str,
        text: str,
        *,
        submit_keys: list[str] | None = None,
    ) -> None:
        if "\n" in text:
            buffer_name = _safe_name(f"{session_name}-input-{uuid.uuid4().hex[:8]}")[:64]
            self._run(
                ["tmux", "load-buffer", "-b", buffer_name, "-"],
                input=text,
            )
            self._run(
                ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", session_name]
            )
            self.send_keys(session_name, submit_keys or ["Enter"])
            return

        if text:
            self._run(["tmux", "send-keys", "-t", session_name, "-l", text])
        self.send_keys(session_name, submit_keys or ["Enter"])

    def send_keys(self, session_name: str, keys: list[str]) -> None:
        for idx, key in enumerate(keys):
            if idx and self._key_delay > 0:
                time.sleep(self._key_delay)
            self._run(["tmux", "send-keys", "-t", session_name, key])

    def stop(self, session_name: str) -> None:
        self._run(["tmux", "kill-session", "-t", session_name], check=False)


class CliBridgePlugin:
    """Register and run chat-to-CLI bridge sessions."""

    def __init__(
        self,
        *,
        tmux: TmuxClient | None = None,
        sender: Callable[[Any, Any, str], None] | None = None,
        enable_output_reader: bool = True,
        state_dir: Path | None = None,
        exec_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.tmux = tmux or TmuxClient()
        self.exec_runner = exec_runner or subprocess.run
        self.sender = sender
        self.enable_output_reader = enable_output_reader
        self.state_dir = state_dir or Path.home() / ".hermes" / "cli-bridge"
        self.output_interval = _env_float("HERMES_CLI_BRIDGE_OUTPUT_INTERVAL", 1.0)
        self.chunk_chars = _env_int("HERMES_CLI_BRIDGE_CHUNK_CHARS", 3500)
        self.max_output_chars = _env_int("HERMES_CLI_BRIDGE_MAX_OUTPUT_CHARS", 12000)
        self.exec_timeout = _env_float("HERMES_CLI_BRIDGE_EXEC_TIMEOUT", 1800.0)
        self.startup_ready_timeout = _env_float(
            "HERMES_CLI_BRIDGE_STARTUP_READY_TIMEOUT",
            20.0,
        )
        self.voice_transcription_enabled = _env_bool(
            "HERMES_CLI_BRIDGE_TRANSCRIBE_VOICE",
            True,
        )
        self.voice_transcription_timeout = _env_float(
            "HERMES_CLI_BRIDGE_TRANSCRIBE_TIMEOUT",
            120.0,
        )
        self.log_snippet_chars = _env_int("HERMES_CLI_BRIDGE_LOG_SNIPPET_CHARS", 50)
        output_source = os.environ.get("HERMES_CLI_BRIDGE_OUTPUT_SOURCE", "capture")
        self.output_source = output_source.strip().lower() or "capture"
        if self.output_source not in {"capture", "pipe"}:
            self.output_source = "capture"
        self.raw_log_enabled = _env_bool("HERMES_CLI_BRIDGE_RAW_LOG", False)
        self.audit_enabled = _env_bool("HERMES_CLI_BRIDGE_AUDIT_LOG", True)
        audit_log_path = os.environ.get("HERMES_CLI_BRIDGE_AUDIT_LOG_PATH", "").strip()
        self.audit_log_path = (
            Path(audit_log_path).expanduser()
            if audit_log_path
            else self.state_dir / "events.jsonl"
        )
        self._sessions: dict[str, BridgeSession] = {}
        self._selected_sessions: dict[str, str] = {}
        self._lock = threading.RLock()
        self._load_session_registry()

    def register(self, ctx: Any) -> None:
        ctx.register_hook("pre_gateway_dispatch", self.handle_pre_gateway_dispatch)
        for agent in PROFILES:
            ctx.register_command(
                agent,
                handler=lambda raw_args, _agent=agent: self._command_stub(_agent, raw_args),
                description=f"Control a tmux-backed {agent.title()} CLI bridge.",
                args_hint="init|list|select|rename|send|status|exit|kill",
            )

    def _command_stub(self, agent: str, raw_args: str) -> str:
        del raw_args
        return self._help(agent)

    def handle_pre_gateway_dispatch(
        self,
        event: Any = None,
        gateway: Any = None,
        session_store: Any = None,
        **_: Any,
    ) -> dict[str, str] | None:
        del session_store
        if event is None or gateway is None or getattr(event, "internal", False):
            return None

        raw_text = getattr(event, "text", "") or ""
        text = raw_text if isinstance(raw_text, str) else ""
        payload = self._event_payload(event)
        if not text.strip() and not payload:
            return None

        control = self._parse_control_command(text) if text.strip() else None
        if control is not None:
            agent, raw_args = control
            if not self._authorized(gateway, event):
                return {"action": "allow"}
            try:
                reply = self._handle_control(agent, raw_args, event, gateway)
            except Exception as exc:
                logger.warning("cli-bridge control failed: %s", exc)
                reply = f"[{agent}] failed: {exc}"
            if reply:
                self._reply(gateway, event, reply)
            return {"action": "skip", "reason": "cli-bridge-control"}

        if text.lstrip().startswith("/"):
            return None

        session = self._session_for_event(event, require_live=True)
        if session is None:
            return None

        if not self._authorized(gateway, event):
            return {"action": "allow"}

        try:
            started = time.perf_counter()
            if session.backend == "exec":
                routed = self._route_exec_input(session, payload, gateway, event)
            else:
                routed = self._route_tmux_input(session, payload, gateway, event)
            session.last_activity = time.time()
            self._send_typing(gateway, event)
            elapsed_ms = (time.perf_counter() - started) * 1000
            fields = _event_log_fields(event)
            logger.info(
                "cli-bridge input routed: agent=%s platform=%s chat=%s user=%s "
                "session=%s chars=%d media=%d input=%r send_ms=%.1f",
                session.agent,
                fields["platform"],
                fields["chat"],
                fields["user"],
                session.session_name,
                len(payload),
                len(getattr(event, "media_urls", []) or []),
                self._log_snippet(payload),
                elapsed_ms,
            )
            self._audit(
                "input_routed",
                session,
                event,
                chars=len(payload),
                media=len(getattr(event, "media_urls", []) or []),
                send_ms=round(elapsed_ms, 1),
                input=self._audit_snippet(payload),
            )
            if not routed:
                self._reply(
                    gateway,
                    event,
                    f"[{session.agent}] busy; previous prompt is still running.",
                )
        except Exception as exc:
            logger.warning("cli-bridge send failed: %s", exc)
            self._reply(gateway, event, f"[{session.agent}] send failed: {exc}")
        return {"action": "skip", "reason": "cli-bridge-input"}

    def _parse_control_command(self, text: str) -> tuple[str, str] | None:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        head, _, raw_args = stripped[1:].partition(" ")
        command = head.split("@", 1)[0].replace("_", "-").lower()
        if command not in PROFILES:
            return None
        return command, raw_args.strip()

    def _handle_control(
        self,
        agent: str,
        raw_args: str,
        event: Any,
        gateway: Any,
    ) -> str:
        try:
            argv = shlex.split(raw_args)
        except ValueError as exc:
            return f"[{agent}] could not parse command: {exc}"

        if not argv or argv[0] in {"help", "-h", "--help"}:
            return self._help(agent)

        subcommand = argv[0].lower()
        if subcommand == "init":
            try:
                name, cwd_arg = self._parse_init_args(argv[1:])
            except ValueError as exc:
                return f"[{agent}] {exc}"
            return self._start_session(agent, name, cwd_arg, event, gateway)
        if subcommand == "list":
            if len(argv) > 2 or (len(argv) == 2 and not self._is_all_option(argv[1])):
                return f"[{agent}] usage: /{agent} list [--all]"
            return self._list_sessions(agent, event, include_all=len(argv) == 2)
        if subcommand == "select":
            target = argv[1] if len(argv) > 1 else None
            return self._select_session(agent, target, event)
        if subcommand == "rename":
            if len(argv) != 2:
                return f"[{agent}] usage: /{agent} rename <new-name>"
            return self._rename_session(agent, event, argv[1])
        if subcommand == "send":
            payload = raw_args.strip().partition(" ")[2]
            return self._send_control(agent, payload, event, gateway)
        if subcommand == "status":
            target = argv[1] if len(argv) > 1 else None
            return self._status(agent, event, target=target)
        if subcommand == "exit":
            return self._exit_session(agent, event)
        if subcommand in {"kill", "end"}:
            target = argv[1] if len(argv) > 1 else "current"
            return self._kill_session(agent, event, target=target)
        return f"[{agent}] unknown subcommand: {subcommand}\n\n{self._help(agent)}"

    def _parse_init_args(self, argv: list[str]) -> tuple[str, str | None]:
        name = "default"
        cwd_arg: str | None = None
        positional: list[str] = []
        idx = 0
        while idx < len(argv):
            item = argv[idx]
            if item == "--cwd":
                if idx + 1 >= len(argv):
                    raise ValueError("usage: init [name] [--cwd <cwd>]")
                cwd_arg = argv[idx + 1]
                idx += 2
                continue
            if item.startswith("--cwd="):
                cwd_arg = item.split("=", 1)[1]
                if not cwd_arg:
                    raise ValueError("usage: init [name] [--cwd <cwd>]")
                idx += 1
                continue
            if item.startswith("-"):
                raise ValueError(f"unknown init option: {item}")
            positional.append(item)
            idx += 1

        if len(positional) > 1:
            raise ValueError("usage: init [name] [--cwd <cwd>]")
        if positional:
            candidate = positional[0]
            if cwd_arg is None and self._looks_like_path(candidate):
                cwd_arg = candidate
            else:
                name = self._normalize_bridge_name(candidate)
        return name, cwd_arg

    def _looks_like_path(self, value: str) -> bool:
        return (
            value.startswith(("/", "./", "../", "~"))
            or "/" in value
            or "\\" in value
        )

    def _is_all_option(self, value: str) -> bool:
        return value == "--all" or value in {"—all", "–all", "−all"}

    def _normalize_bridge_name(self, name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValueError("session name cannot be empty")
        lower = normalized.lower()
        if lower == "current":
            raise ValueError("'current' is reserved; choose another session name")
        if lower in {"none", "all"}:
            raise ValueError(f"'{normalized}' is reserved; choose another session name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", normalized):
            raise ValueError(
                "session name must use letters, numbers, dot, underscore, or dash"
            )
        return normalized

    def _start_session(
        self,
        agent: str,
        name: str,
        cwd_arg: str | None,
        event: Any,
        gateway: Any,
    ) -> str:
        source = getattr(event, "source", None)
        base_key = self._base_session_key(agent, source)
        key = self._session_key(agent, source, name)
        with self._lock:
            existing = self._sessions.get(key)
            if (
                existing is not None
                and (
                    existing.backend == "exec"
                    or self.tmux.has_session(existing.session_name)
                )
            ):
                label = "exec" if existing.backend == "exec" else "tmux"
                action = (
                    "attached to existing session"
                    if existing.restored
                    else "already active"
                )
                existing.restored = False
                self._selected_sessions[base_key] = name
                self._ensure_tmux_reader(existing, gateway, event)
                existing.last_activity = time.time()
                self._persist_session_registry_locked()
                return (
                    f"[{agent}:{name}] {action} in {existing.cwd}\n"
                    f"{label}: {existing.session_name}"
                )
            if existing is not None:
                self._drop_session_locked(existing)

            cwd = self._resolve_cwd(cwd_arg)
            command = self._agent_command(agent)
            backend = self._agent_backend(agent)
            self._ensure_agent_command_available(agent, command, backend=backend)
            suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
            session_name = _safe_name(f"hermes-{agent}-{name}-{suffix}")[:64]
            if backend == "exec":
                session_name = _safe_name(f"hermes-{agent}-exec-{name}-{suffix}")[:64]
            log_path = self.state_dir / f"{session_name}.log"
            session = BridgeSession(
                agent=agent,
                key=key,
                base_key=base_key,
                name=name,
                session_name=session_name,
                cwd=cwd,
                command=command,
                log_path=log_path,
                backend=backend,
            )
            attached = backend == "tmux" and self.tmux.has_session(session_name)
            if backend == "tmux" and not attached:
                self.tmux.start(
                    session_name=session_name,
                    cwd=cwd,
                    command=command,
                    log_path=log_path,
                    pipe_log=self.raw_log_enabled or self.output_source == "pipe",
                )
                ready = self._wait_for_tmux_ready(session, event)
                if not ready and not self.tmux.has_session(session.session_name):
                    raise RuntimeError(
                        f"{agent} command exited before becoming ready: {command!r}. "
                        f"Check that the CLI is installed and authenticated on this host."
                    )
            self._sessions[key] = session
            self._ensure_tmux_reader(session, gateway, event)
            self._selected_sessions[base_key] = name
            self._persist_session_registry_locked()

        action = "attached to existing session" if attached else "started"
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge session %s: agent=%s bridge=%s backend=%s platform=%s chat=%s user=%s session=%s cwd=%s",
            "attached" if attached else "started",
            agent,
            name,
            session.backend,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session_name,
            cwd,
        )
        self._audit(
            "session_attached" if attached else "session_started",
            session,
            event,
            cwd=str(cwd),
            command=command,
            backend=session.backend,
            name=name,
        )
        label = "tmux" if session.backend == "tmux" else "exec"
        return f"[{agent}:{name}] {action} in {cwd}\n{label}: {session_name}"

    def _send_control(self, agent: str, payload: str, event: Any, gateway: Any) -> str:
        if not payload:
            return f"[{agent}] usage: /{agent} send <text>"
        session = self._session_for_event(event, agent=agent, require_live=True)
        if session is None:
            return f"[{agent}] no active bridge for this chat. Run /{agent} init first."
        started = time.perf_counter()
        if session.backend == "exec":
            routed = self._route_exec_input(session, payload, gateway, event)
        else:
            routed = self._route_tmux_input(
                session, payload, gateway, event, allow_voice=False
            )
        session.last_activity = time.time()
        self._send_typing(gateway, event)
        elapsed_ms = (time.perf_counter() - started) * 1000
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge command send routed: agent=%s platform=%s chat=%s user=%s "
            "session=%s chars=%d input=%r send_ms=%.1f",
            agent,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
            len(payload),
            self._log_snippet(payload),
            elapsed_ms,
        )
        self._audit(
            "command_send_routed",
            session,
            event,
            chars=len(payload),
            send_ms=round(elapsed_ms, 1),
            input=self._audit_snippet(payload),
        )
        if not routed:
            return f"[{agent}] busy; previous prompt is still running."
        return f"[{agent}] sent."

    def _list_sessions(
        self,
        agent: str,
        event: Any,
        *,
        include_all: bool = False,
    ) -> str:
        source = getattr(event, "source", None)
        base_key = self._base_session_key(agent, source)
        with self._lock:
            if include_all:
                sessions = self._sessions_for_source_scope_locked(
                    source,
                    require_live=True,
                )
            else:
                sessions = self._sessions_for_base_locked(
                    agent,
                    source,
                    require_live=True,
                )
            selected = self._selected_sessions.get(base_key)
            selected_by_base = dict(self._selected_sessions)
        if not sessions:
            return f"[{agent}] no bridge sessions for this chat."
        lines = [f"[{agent}] {'all bridge sessions' if include_all else 'sessions'}:"]
        sort_key = (
            (lambda item: (item.agent, item.name))
            if include_all
            else (lambda item: item.name)
        )
        for session in sorted(sessions, key=sort_key):
            if include_all:
                session_selected = selected_by_base.get(session.base_key)
                marker = "*" if session.name == session_selected else "-"
                thread_id = self._base_key_fields(session.base_key)["thread_id"]
                thread_label = f" topic={thread_id}" if thread_id else ""
                lines.append(
                    f"{marker} {session.agent}:{session.name} ({session.backend}) "
                    f"{session.cwd} [{session.session_name}]{thread_label}"
                )
                continue

            marker = "*" if session.name == selected else "-"
            lines.append(
                f"{marker} {session.name} ({session.backend}) {session.cwd} "
                f"[{session.session_name}]"
            )
        if not include_all and (selected is None or selected == _NO_SELECTED_SESSION):
            lines.append("No current session selected.")
        return "\n".join(lines)

    def _select_session(self, agent: str, target: str | None, event: Any) -> str:
        if target is None:
            return self._list_sessions(agent, event)
        source = getattr(event, "source", None)
        base_key = self._base_session_key(agent, source)
        if target == "none":
            with self._lock:
                self._selected_sessions[base_key] = _NO_SELECTED_SESSION
            return f"[{agent}] no current session selected."
        if target == "current":
            session = self._session_for_event(event, agent=agent, require_live=True)
            if session is None:
                return self._list_sessions(agent, event)
            return f"[{agent}] current: {session.name}"
        try:
            name = self._normalize_bridge_name(target)
        except ValueError as exc:
            return f"[{agent}] {exc}"
        with self._lock:
            session = self._sessions.get(self._session_key(agent, source, name))
            session = self._live_or_drop_locked(session) if session is not None else None
            if session is None:
                return f"[{agent}] no session named {name!r}.\n\n{self._list_sessions(agent, event)}"
            self._selected_sessions[base_key] = name
        return f"[{agent}] selected {name}."

    def _rename_session(self, agent: str, event: Any, new_name_raw: str) -> str:
        try:
            new_name = self._normalize_bridge_name(new_name_raw)
        except ValueError as exc:
            return f"[{agent}] {exc}"
        source = getattr(event, "source", None)
        with self._lock:
            session = self._session_for_event_locked(
                event,
                agent=agent,
                require_live=True,
                target="current",
            )
            if session is None:
                return self._list_sessions(agent, event)
            old_name = session.name
            if old_name == new_name:
                return f"[{agent}:{new_name}] already named {new_name}."

            new_key = self._session_key(agent, source, new_name)
            existing = self._sessions.get(new_key)
            existing = self._live_or_drop_locked(existing) if existing is not None else None
            if existing is not None and existing is not session:
                return f"[{agent}] session named {new_name!r} already exists."

            self._sessions.pop(session.key, None)
            session.key = new_key
            session.name = new_name
            self._sessions[new_key] = session
            self._selected_sessions[session.base_key] = new_name
            self._persist_session_registry_locked()

        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge session renamed: agent=%s old_bridge=%s new_bridge=%s platform=%s chat=%s user=%s session=%s",
            agent,
            old_name,
            new_name,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
        )
        self._audit("session_renamed", session, event, old_name=old_name, new_name=new_name)
        return f"[{agent}:{new_name}] renamed {old_name} -> {new_name}."

    def _status(self, agent: str, event: Any, *, target: str | None = None) -> str:
        session = self._session_for_event(
            event,
            agent=agent,
            require_live=True,
            target=target,
        )
        if session is None:
            return self._list_sessions(agent, event)
        return (
            f"[{agent}:{session.name}] active\n"
            f"backend: {session.backend}\n"
            f"cwd: {session.cwd}\n"
            f"session: {session.session_name}"
            + (f"\nthread: {session.thread_id}" if session.thread_id else "")
        )

    def _exit_session(self, agent: str, event: Any) -> str:
        source = getattr(event, "source", None)
        base_key = self._base_session_key(agent, source)
        with self._lock:
            session = self._session_for_event_locked(
                event,
                agent=agent,
                require_live=True,
                target="current",
            )
            self._selected_sessions[base_key] = _NO_SELECTED_SESSION
        if session is None:
            return f"[{agent}] no current session selected."
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge session exited: agent=%s bridge=%s platform=%s chat=%s user=%s session=%s",
            agent,
            session.name,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
        )
        self._audit("session_exited", session, event)
        return f"[{agent}:{session.name}] exited bridge; session is still running."

    def _kill_session(self, agent: str, event: Any, *, target: str = "current") -> str:
        source = getattr(event, "source", None)
        with self._lock:
            session = self._session_for_event_locked(
                event,
                agent=agent,
                require_live=True,
                target=target,
            )
            if session is not None:
                self._sessions.pop(session.key, None)
                self._persist_session_registry_locked()
        if session is None:
            return self._list_sessions(agent, event)
        session.stop_event.set()
        if session.backend == "tmux":
            self.tmux.stop(session.session_name)
        if self._selected_sessions.get(session.base_key) == session.name:
            self._selected_sessions.pop(session.base_key, None)
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge session ended: agent=%s bridge=%s platform=%s chat=%s user=%s session=%s",
            agent,
            session.name,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
        )
        self._audit("session_ended", session, event)
        return f"[{agent}:{session.name}] killed {session.backend} session {session.session_name}."

    def _session_for_event(
        self,
        event: Any,
        *,
        agent: str | None = None,
        require_live: bool = False,
        target: str | None = None,
    ) -> BridgeSession | None:
        with self._lock:
            return self._session_for_event_locked(
                event,
                agent=agent,
                require_live=require_live,
                target=target,
            )

    def _session_for_event_locked(
        self,
        event: Any,
        *,
        agent: str | None = None,
        require_live: bool = False,
        target: str | None = None,
    ) -> BridgeSession | None:
        source = getattr(event, "source", None)
        if agent is not None:
            return self._selected_or_named_session_locked(
                agent,
                source,
                require_live=require_live,
                target=target,
            )
        for candidate in PROFILES:
            session = self._selected_or_named_session_locked(
                candidate,
                source,
                require_live=require_live,
                target=target,
            )
            if session is not None:
                return session
        return None

    def _selected_or_named_session_locked(
        self,
        agent: str,
        source: Any,
        *,
        require_live: bool,
        target: str | None = None,
    ) -> BridgeSession | None:
        base_key = self._base_session_key(agent, source)
        name: str | None
        if target and target != "current":
            try:
                name = self._normalize_bridge_name(target)
            except ValueError:
                return None
        else:
            name = self._selected_sessions.get(base_key)

        if name == _NO_SELECTED_SESSION:
            return None
        if name is not None:
            session = self._sessions.get(self._session_key(agent, source, name))
            return self._live_or_drop_locked(session) if require_live else session

        sessions = self._sessions_for_base_locked(
            agent,
            source,
            require_live=require_live,
        )
        if len(sessions) == 1:
            return sessions[0]
        return None

    def _sessions_for_base_locked(
        self,
        agent: str,
        source: Any,
        *,
        require_live: bool,
    ) -> list[BridgeSession]:
        base_key = self._base_session_key(agent, source)
        sessions = [
            session
            for session in list(self._sessions.values())
            if session.agent == agent and session.base_key == base_key
        ]
        if not require_live:
            return sessions
        live: list[BridgeSession] = []
        for session in sessions:
            candidate = self._live_or_drop_locked(session)
            if candidate is not None:
                live.append(candidate)
        return live

    def _sessions_for_source_scope_locked(
        self,
        source: Any,
        *,
        require_live: bool,
    ) -> list[BridgeSession]:
        scope = self._source_scope_fields(source)
        sessions = [
            session
            for session in list(self._sessions.values())
            if self._session_matches_source_scope(session, scope)
        ]
        if not require_live:
            return sessions
        live: list[BridgeSession] = []
        for session in sessions:
            candidate = self._live_or_drop_locked(session)
            if candidate is not None:
                live.append(candidate)
        return live

    def _drop_session_locked(self, session: BridgeSession) -> None:
        session.stop_event.set()
        if session.backend == "tmux":
            self.tmux.stop(session.session_name)
        self._sessions.pop(session.key, None)
        if self._selected_sessions.get(session.base_key) == session.name:
            self._selected_sessions.pop(session.base_key, None)
        self._persist_session_registry_locked()

    def _live_or_drop_locked(self, session: BridgeSession | None) -> BridgeSession | None:
        if session is None:
            return None
        if session.backend == "exec":
            return session
        if self.tmux.has_session(session.session_name):
            return session
        session.stop_event.set()
        self._sessions.pop(session.key, None)
        if self._selected_sessions.get(session.base_key) == session.name:
            self._selected_sessions.pop(session.base_key, None)
        self._persist_session_registry_locked()
        return None

    def _base_session_key(self, agent: str, source: Any) -> str:
        platform = _platform_value(getattr(source, "platform", ""))
        parts = [
            agent,
            platform,
            str(getattr(source, "profile", "") or ""),
            str(getattr(source, "scope_id", "") or ""),
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "thread_id", "") or ""),
            str(getattr(source, "user_id", "") or ""),
        ]
        return "\x1f".join(parts)

    def _session_key(self, agent: str, source: Any, name: str = "default") -> str:
        return f"{self._base_session_key(agent, source)}\x1f{name}"

    def _base_key_fields(self, base_key: str) -> dict[str, str]:
        parts = (base_key.split("\x1f") + [""] * 7)[:7]
        return {
            "agent": parts[0],
            "platform": parts[1],
            "profile": parts[2],
            "scope_id": parts[3],
            "chat_id": parts[4],
            "thread_id": parts[5],
            "user_id": parts[6],
        }

    def _source_scope_fields(self, source: Any) -> dict[str, str]:
        return {
            "platform": _platform_value(getattr(source, "platform", "")),
            "profile": str(getattr(source, "profile", "") or ""),
            "scope_id": str(getattr(source, "scope_id", "") or ""),
            "chat_id": str(getattr(source, "chat_id", "") or ""),
            "user_id": str(getattr(source, "user_id", "") or ""),
        }

    def _session_matches_source_scope(
        self,
        session: BridgeSession,
        scope: dict[str, str],
    ) -> bool:
        fields = self._base_key_fields(session.base_key)
        return (
            fields["agent"] in PROFILES
            and fields["platform"] == scope["platform"]
            and fields["profile"] == scope["profile"]
            and fields["scope_id"] == scope["scope_id"]
            and fields["chat_id"] == scope["chat_id"]
            and fields["user_id"] == scope["user_id"]
        )

    @property
    def _registry_path(self) -> Path:
        return self.state_dir / "sessions.json"

    def _load_session_registry(self) -> None:
        path = self._registry_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("cli-bridge session registry could not be read: %s", exc)
            return
        if not isinstance(data, dict):
            return

        loaded: dict[str, BridgeSession] = {}
        for record in data.get("sessions", []):
            if not isinstance(record, dict):
                continue
            session = self._session_from_record(record)
            if session is not None:
                loaded[session.key] = session
        self._sessions.update(loaded)

    def _session_from_record(self, record: dict[str, Any]) -> BridgeSession | None:
        agent = str(record.get("agent") or "")
        if agent not in PROFILES:
            return None

        key = str(record.get("key") or "")
        base_key = str(record.get("base_key") or "")
        name = str(record.get("name") or "")
        session_name = str(record.get("session_name") or "")
        cwd_raw = str(record.get("cwd") or "")
        if not key or not base_key or not name or not session_name or not cwd_raw:
            return None
        try:
            self._normalize_bridge_name(name)
        except ValueError:
            return None

        backend = str(record.get("backend") or "tmux").lower()
        if backend not in {"tmux", "exec"}:
            backend = "tmux"

        log_raw = str(record.get("log_path") or "")
        created_at = self._record_float(record.get("created_at"), time.time())
        last_activity = self._record_float(record.get("last_activity"), created_at)
        return BridgeSession(
            agent=agent,
            key=key,
            base_key=base_key,
            name=name,
            session_name=session_name,
            cwd=Path(cwd_raw).expanduser(),
            command=str(record.get("command") or self._agent_command(agent)),
            log_path=Path(log_raw).expanduser()
            if log_raw
            else self.state_dir / f"{session_name}.log",
            backend=backend,
            thread_id=str(record.get("thread_id") or "") or None,
            created_at=created_at,
            last_activity=last_activity,
            restored=True,
        )

    def _record_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _session_record(self, session: BridgeSession) -> dict[str, Any]:
        return {
            "agent": session.agent,
            "key": session.key,
            "base_key": session.base_key,
            "name": session.name,
            "session_name": session.session_name,
            "cwd": str(session.cwd),
            "command": session.command,
            "log_path": str(session.log_path),
            "backend": session.backend,
            "thread_id": session.thread_id,
            "created_at": session.created_at,
            "last_activity": session.last_activity,
            "source": self._base_key_fields(session.base_key),
        }

    def _persist_session_registry_locked(self) -> None:
        try:
            records = [
                self._session_record(session)
                for session in sorted(
                    self._sessions.values(),
                    key=lambda item: (item.agent, item.base_key, item.name),
                )
                if not session.stop_event.is_set()
            ]
            data = {"version": 1, "sessions": records}
            path = self._registry_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp_path.write_text(
                    json.dumps(data, ensure_ascii=True, indent=2, default=str) + "\n",
                    encoding="utf-8",
                )
                tmp_path.replace(path)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("cli-bridge session registry write failed: %s", exc)

    def _ensure_tmux_reader(
        self,
        session: BridgeSession,
        gateway: Any,
        event: Any,
    ) -> None:
        if session.backend != "tmux" or not self.enable_output_reader:
            return
        with self._lock:
            if session.stop_event.is_set():
                return
            if session.reader is not None and session.reader.is_alive():
                return
            reader = threading.Thread(
                target=self._output_reader,
                args=(session, gateway, event),
                name=f"hermes-cli-bridge-{session.agent}",
                daemon=True,
            )
            session.reader = reader
            reader.start()

    def _resolve_cwd(self, cwd_arg: str | None) -> Path:
        cwd_raw = (
            cwd_arg
            or os.environ.get("HERMES_CLI_BRIDGE_DEFAULT_CWD")
            or os.getcwd()
        )
        cwd = Path(cwd_raw).expanduser().resolve()
        if not cwd.exists() or not cwd.is_dir():
            raise RuntimeError(f"cwd does not exist or is not a directory: {cwd}")
        return cwd

    def _profile(self, agent: str) -> AgentProfile:
        return PROFILES[agent]

    def _agent_command(self, agent: str) -> str:
        return self._profile(agent).command()

    def _agent_backend(self, agent: str) -> str:
        raw = (
            os.environ.get(f"HERMES_CLI_BRIDGE_{agent.upper()}_BACKEND")
            or os.environ.get("HERMES_CLI_BRIDGE_BACKEND")
            or "tmux"
        )
        backend = raw.strip().lower()
        if backend not in {"tmux", "exec"}:
            logger.warning("cli-bridge unknown backend %r for %s; using tmux", raw, agent)
            return "tmux"
        if backend == "exec" and not self._profile(agent).supports_exec:
            logger.warning(
                "cli-bridge exec backend is not implemented for %s; using tmux", agent
            )
            return "tmux"
        return backend

    def _ensure_agent_command_available(
        self,
        agent: str,
        command: str,
        *,
        backend: str,
    ) -> None:
        executable = self._command_executable(command)
        if not executable:
            return
        if Path(executable).expanduser().exists() or shutil.which(executable):
            return
        raise RuntimeError(
            f"{agent} {backend} command not found on PATH: {executable!r}. "
            f"Install it or set HERMES_CLI_BRIDGE_{agent.upper()}_CMD."
        )

    def _command_executable(self, command: str) -> str | None:
        if not command.strip() or re.search(r"[|&;<>()$`]", command):
            return None
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        while parts and "=" in parts[0] and not parts[0].startswith(("/", "./", "../")):
            parts.pop(0)
        if parts and parts[0] == "env":
            parts.pop(0)
            while parts and "=" in parts[0]:
                parts.pop(0)
        return parts[0] if parts else None

    def _wait_for_tmux_ready(self, session: BridgeSession, event: Any) -> bool:
        capture = getattr(self.tmux, "capture", None)
        if not callable(capture) or self.startup_ready_timeout <= 0:
            return True

        deadline = time.monotonic() + self.startup_ready_timeout
        last_snapshot = ""
        while time.monotonic() < deadline:
            if session.stop_event.is_set():
                return False
            if not self.tmux.has_session(session.session_name):
                logger.warning(
                    "cli-bridge tmux session exited before ready: agent=%s session=%s command=%r",
                    session.agent,
                    session.session_name,
                    session.command,
                )
                self._audit(
                    "session_exited_before_ready",
                    session,
                    event,
                    command=session.command,
                    snapshot=self._audit_snippet(last_snapshot),
                )
                return False
            try:
                snapshot = self._clean_output(capture(session.session_name))
            except Exception:
                snapshot = ""
            last_snapshot = snapshot or last_snapshot
            if self._tmux_snapshot_ready(session.agent, snapshot):
                self._audit(
                    "session_ready",
                    session,
                    event,
                    snapshot=self._audit_snippet(snapshot),
                )
                return True
            if self._approval_from_capture(session, snapshot) is not None:
                self._audit(
                    "session_ready_dialog",
                    session,
                    event,
                    snapshot=self._audit_snippet(snapshot),
                )
                return True
            time.sleep(0.25)

        logger.warning(
            "cli-bridge tmux session did not become ready before timeout: "
            "agent=%s session=%s timeout=%.1f snapshot=%r",
            session.agent,
            session.session_name,
            self.startup_ready_timeout,
            self._log_snippet(last_snapshot),
        )
        self._audit(
            "session_ready_timeout",
            session,
            event,
            timeout=self.startup_ready_timeout,
            snapshot=self._audit_snippet(last_snapshot),
        )
        return False

    def _tmux_snapshot_ready(self, agent: str, snapshot: str) -> bool:
        return self._profile(agent).snapshot_ready(snapshot)

    def _tmux_submit_keys(self, agent: str) -> list[str]:
        return self._profile(agent).submit_keys()

    def _event_payload(self, event: Any) -> str:
        parts: list[str] = []
        text = getattr(event, "text", "") or ""
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())

        media_note = self._media_payload(event)
        if media_note:
            parts.append(media_note)
        return "\n\n".join(parts).strip()

    def _media_payload(
        self,
        event: Any,
        *,
        skip_paths: set[str] | None = None,
    ) -> str:
        media_urls = list(getattr(event, "media_urls", []) or [])
        media_types = list(getattr(event, "media_types", []) or [])
        skip_paths = skip_paths or set()
        attachment_lines = []
        for idx, path in enumerate(media_urls):
            if str(path) in skip_paths:
                continue
            media_type = media_types[idx] if idx < len(media_types) else "file"
            attachment_lines.append(f"- {media_type}: {path}")
        if not attachment_lines:
            return ""
        return "User attached file(s):\n" + "\n".join(attachment_lines)

    def _authorized(self, gateway: Any, event: Any) -> bool:
        checker = getattr(gateway, "_is_user_authorized", None)
        if not callable(checker):
            logger.warning("cli-bridge refusing message: gateway has no auth checker")
            return False
        try:
            return bool(checker(getattr(event, "source", None)))
        except Exception as exc:
            logger.warning("cli-bridge auth check failed: %s", exc)
            return False

    def _route_exec_input(
        self,
        session: BridgeSession,
        payload: str,
        gateway: Any,
        event: Any,
    ) -> bool:
        if not session.exec_lock.acquire(blocking=False):
            self._audit(
                "exec_busy",
                session,
                event,
                input=self._audit_snippet(payload),
            )
            return False
        worker = threading.Thread(
            target=self._exec_prompt_worker,
            args=(session, payload, gateway, event),
            name=f"hermes-cli-bridge-exec-{session.agent}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            session.exec_lock.release()
            raise
        return True

    def _route_tmux_input(
        self,
        session: BridgeSession,
        payload: str,
        gateway: Any,
        event: Any,
        *,
        allow_voice: bool = True,
    ) -> bool:
        self._ensure_tmux_reader(session, gateway, event)
        needs_voice = bool(
            allow_voice
            and self.voice_transcription_enabled
            and self._voice_audio_paths(event)
        )
        with session.send_mutex:
            deferred = (
                needs_voice
                or session.sender_active
                or bool(session.queued_sends)
                or session.approval_signature is not None
            )
            if deferred:
                session.queued_sends.append((payload, needs_voice, gateway, event))
                if not session.sender_active:
                    worker = threading.Thread(
                        target=self._tmux_send_worker,
                        args=(session,),
                        name=f"hermes-cli-bridge-send-{session.agent}",
                        daemon=True,
                    )
                    session.sender_active = True
                    try:
                        worker.start()
                    except Exception:
                        session.sender_active = False
                        raise
                return True
        self.tmux.send_input(
            session.session_name,
            payload,
            submit_keys=self._tmux_submit_keys(session.agent),
        )
        return True

    def _tmux_send_worker(self, session: BridgeSession) -> None:
        while True:
            with session.send_mutex:
                if session.stop_event.is_set() or not session.queued_sends:
                    session.sender_active = False
                    return
                payload, needs_voice, gateway, event = session.queued_sends.popleft()
            try:
                if needs_voice:
                    payload = self._prepare_outbound_payload(session, payload, gateway, event)
                while (
                    session.approval_signature is not None
                    and not session.stop_event.is_set()
                ):
                    session.stop_event.wait(0.25)
                if session.stop_event.is_set():
                    continue
                self.tmux.send_input(
                    session.session_name,
                    payload,
                    submit_keys=self._tmux_submit_keys(session.agent),
                )
                session.last_activity = time.time()
            except Exception as exc:
                logger.warning("cli-bridge tmux send worker failed: %s", exc)
                self._audit("tmux_send_worker_failed", session, event, error=str(exc))
                self._reply(gateway, event, f"[{session.agent}] send failed: {exc}")

    def _prepare_outbound_payload(
        self,
        session: BridgeSession,
        payload: str,
        gateway: Any,
        event: Any,
    ) -> str:
        voice_paths = self._voice_audio_paths(event)
        if not self.voice_transcription_enabled or not voice_paths:
            return payload

        text = getattr(event, "text", "") or ""
        text = text.strip() if isinstance(text, str) else ""
        try:
            enriched_text, transcripts = self._transcribe_voice_paths(
                gateway,
                text,
                voice_paths,
            )
        except Exception as exc:
            logger.warning(
                "cli-bridge voice transcription failed: agent=%s session=%s "
                "audio=%d error=%s",
                session.agent,
                session.session_name,
                len(voice_paths),
                exc,
            )
            self._audit(
                "voice_transcription_failed",
                session,
                event,
                audio=len(voice_paths),
                error=str(exc),
            )
            return payload

        remaining_media = self._media_payload(event, skip_paths=set(voice_paths))
        parts = []
        if enriched_text.strip():
            parts.append(enriched_text.strip())
        if remaining_media:
            parts.append(remaining_media)
        prepared = "\n\n".join(parts).strip() or payload
        logger.info(
            "cli-bridge voice transcription prepared: agent=%s session=%s "
            "audio=%d transcripts=%d input=%r",
            session.agent,
            session.session_name,
            len(voice_paths),
            len(transcripts),
            self._log_snippet(prepared),
        )
        self._audit(
            "voice_transcription_prepared",
            session,
            event,
            audio=len(voice_paths),
            transcripts=len(transcripts),
            input=self._audit_snippet(prepared),
        )
        return prepared

    def _voice_audio_paths(self, event: Any) -> list[str]:
        message_type = self._event_message_type(event)
        paths: list[str] = []
        media_urls = list(getattr(event, "media_urls", []) or [])
        media_types = list(getattr(event, "media_types", []) or [])
        for idx, path in enumerate(media_urls):
            media_type = media_types[idx] if idx < len(media_types) else ""
            if message_type == "voice" or (
                str(media_type).lower().startswith("audio/")
                and message_type not in {"audio", "document"}
            ):
                paths.append(str(path))
        return paths

    def _event_message_type(self, event: Any) -> str:
        message_type = getattr(event, "message_type", None)
        return str(getattr(message_type, "value", message_type) or "").lower()

    def _transcribe_voice_paths(
        self,
        gateway: Any,
        text: str,
        voice_paths: list[str],
    ) -> tuple[str, list[str]]:
        transcriber = getattr(gateway, "_enrich_message_with_transcription", None)
        if callable(transcriber):
            result = transcriber(text, voice_paths)
            if inspect.isawaitable(result):
                return self._run_awaitable(
                    gateway,
                    result,
                    timeout=self.voice_transcription_timeout,
                )
            return result

        from tools.transcription_tools import transcribe_audio

        enriched_parts: list[str] = []
        transcripts: list[str] = []
        for path in voice_paths:
            result = transcribe_audio(path)
            if result.get("success"):
                transcript = str(result.get("transcript") or "")
                transcripts.append(transcript)
                enriched_parts.append(f'"{transcript}"')
            else:
                enriched_parts.append("[voice message could not be transcribed]")

        prefix = "\n\n".join(part for part in enriched_parts if part).strip()
        if prefix and text:
            return f"{prefix}\n\n{text}", transcripts
        return prefix or text, transcripts

    def _run_awaitable(self, gateway: Any, awaitable: Any, *, timeout: float) -> Any:
        loop = getattr(gateway, "_gateway_loop", None)
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(awaitable, loop).result(timeout=timeout)
        return asyncio.run(awaitable)

    def _exec_prompt_worker(
        self,
        session: BridgeSession,
        payload: str,
        gateway: Any,
        event: Any,
    ) -> None:
        started = time.perf_counter()
        output_path = self.state_dir / f"{session.session_name}-last.txt"
        try:
            payload = self._prepare_outbound_payload(session, payload, gateway, event)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.unlink(missing_ok=True)
            command = self._profile(session.agent).exec_command(
                session.thread_id, output_path
            )
            fields = _event_log_fields(event)
            logger.info(
                "cli-bridge exec started: agent=%s platform=%s chat=%s user=%s "
                "session=%s thread=%s input=%r",
                session.agent,
                fields["platform"],
                fields["chat"],
                fields["user"],
                session.session_name,
                session.thread_id or "",
                self._log_snippet(payload),
            )
            self._audit(
                "exec_started",
                session,
                event,
                thread=session.thread_id,
                command=" ".join(shlex.quote(part) for part in command),
                input=self._audit_snippet(payload),
            )
            result = self.exec_runner(
                command,
                cwd=session.cwd,
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.exec_timeout,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            if session.stop_event.is_set():
                self._audit("exec_discarded_after_kill", session, event)
                return
            thread_id, stdout_message = self._profile(session.agent).parse_exec_stdout(
                result.stdout
            )
            if thread_id:
                with self._lock:
                    session.thread_id = thread_id
                    self._persist_session_registry_locked()
            output = ""
            if output_path.exists():
                output = output_path.read_text(encoding="utf-8", errors="replace").strip()
            if not output:
                output = stdout_message.strip()

            if result.returncode != 0:
                error_text = (result.stderr or result.stdout or "").strip()
                logger.warning(
                    "cli-bridge exec failed: agent=%s platform=%s chat=%s user=%s "
                    "session=%s returncode=%d stderr=%r",
                    session.agent,
                    fields["platform"],
                    fields["chat"],
                    fields["user"],
                    session.session_name,
                    result.returncode,
                    self._log_snippet(error_text),
                )
                self._audit(
                    "exec_failed",
                    session,
                    event,
                    returncode=result.returncode,
                    stderr=self._audit_snippet(error_text),
                    elapsed_ms=round(elapsed_ms, 1),
                )
                self._reply(
                    gateway,
                    event,
                    f"[{session.agent}] exec failed: {self._log_snippet(error_text, 300)}",
                )
                return

            if output:
                chunks = self._chunks(f"[{session.agent}]\n{output}")
                logger.info(
                    "cli-bridge exec output routed: agent=%s platform=%s chat=%s user=%s "
                    "session=%s thread=%s chars=%d chunks=%d output=%r elapsed_ms=%.1f",
                    session.agent,
                    fields["platform"],
                    fields["chat"],
                    fields["user"],
                    session.session_name,
                    session.thread_id or "",
                    len(output),
                    len(chunks),
                    self._log_snippet(output),
                    elapsed_ms,
                )
                self._audit(
                    "exec_output_routed",
                    session,
                    event,
                    thread=session.thread_id,
                    chars=len(output),
                    chunks=len(chunks),
                    output=self._audit_snippet(output),
                    elapsed_ms=round(elapsed_ms, 1),
                )
                for chunk in chunks:
                    self._reply(gateway, event, chunk)
                return

            logger.info(
                "cli-bridge exec output empty: agent=%s platform=%s chat=%s user=%s "
                "session=%s thread=%s elapsed_ms=%.1f stdout=%r stderr=%r",
                session.agent,
                fields["platform"],
                fields["chat"],
                fields["user"],
                session.session_name,
                session.thread_id or "",
                elapsed_ms,
                self._log_snippet(result.stdout),
                self._log_snippet(result.stderr),
            )
            self._audit(
                "exec_output_empty",
                session,
                event,
                thread=session.thread_id,
                stdout=self._audit_snippet(result.stdout),
                stderr=self._audit_snippet(result.stderr),
                elapsed_ms=round(elapsed_ms, 1),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "cli-bridge exec timed out: agent=%s session=%s timeout=%.1f",
                session.agent,
                session.session_name,
                self.exec_timeout,
            )
            self._audit("exec_timeout", session, event, timeout=self.exec_timeout)
            self._reply(gateway, event, f"[{session.agent}] exec timed out.")
        except Exception as exc:
            logger.warning("cli-bridge exec worker failed: %s", exc)
            self._audit("exec_worker_failed", session, event, error=str(exc))
            self._reply(gateway, event, f"[{session.agent}] exec failed: {exc}")
        finally:
            session.exec_lock.release()

    def _codex_exec_command(self, session: BridgeSession, output_path: Path) -> list[str]:
        return self._profile("codex").exec_command(session.thread_id, output_path)

    def _claude_exec_command(self, session: BridgeSession, output_path: Path) -> list[str]:
        return self._profile("claude").exec_command(session.thread_id, output_path)

    def _parse_codex_exec_stdout(self, stdout: str) -> tuple[str | None, str]:
        return self._profile("codex").parse_exec_stdout(stdout)

    def _parse_claude_exec_stdout(self, stdout: str) -> tuple[str | None, str]:
        return self._profile("claude").parse_exec_stdout(stdout)

    def _reply(self, gateway: Any, event: Any, text: str) -> None:
        if self.sender is not None:
            self.sender(gateway, event, text)
            return
        self._send_gateway_reply(gateway, event, text)

    def _adapter_and_metadata(self, gateway: Any, event: Any) -> tuple[Any | None, dict | None]:
        source = getattr(event, "source", None)
        adapters = getattr(gateway, "adapters", {}) or {}
        platform = getattr(source, "platform", None)
        adapter = adapters.get(platform) or adapters.get(_platform_value(platform))
        if adapter is None:
            return None, None

        metadata = None
        meta_fn = getattr(gateway, "_thread_metadata_for_source", None)
        if callable(meta_fn):
            try:
                metadata = meta_fn(source, getattr(event, "message_id", None))
            except TypeError:
                metadata = meta_fn(source)
            except Exception:
                metadata = None
        return adapter, metadata

    def _send_gateway_reply(self, gateway: Any, event: Any, text: str) -> None:
        adapter, metadata = self._adapter_and_metadata(gateway, event)
        if adapter is None:
            source = getattr(event, "source", None)
            logger.warning(
                "cli-bridge could not find adapter for platform=%s",
                getattr(source, "platform", None),
            )
            return
        source = getattr(event, "source", None)
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge telegram send queued: platform=%s chat=%s user=%s chars=%d output=%r",
            fields["platform"],
            fields["chat"],
            fields["user"],
            len(text),
            self._log_snippet(text),
        )
        result = adapter.send(str(getattr(source, "chat_id", "")), text, metadata=metadata)
        if inspect.isawaitable(result):
            self._schedule_awaitable(gateway, result, label="telegram send")
        else:
            logger.info(
                "cli-bridge telegram send completed synchronously: platform=%s chat=%s user=%s",
                fields["platform"],
                fields["chat"],
                fields["user"],
            )

    def _send_typing(self, gateway: Any, event: Any) -> None:
        if gateway is None:
            return
        adapter, metadata = self._adapter_and_metadata(gateway, event)
        if adapter is None:
            return
        send_typing = getattr(adapter, "send_typing", None)
        if not callable(send_typing):
            return
        source = getattr(event, "source", None)
        try:
            result = send_typing(str(getattr(source, "chat_id", "")), metadata=metadata)
            if inspect.isawaitable(result):
                self._schedule_awaitable(gateway, result, label="typing")
        except Exception as exc:
            logger.debug("cli-bridge typing indicator failed: %s", exc)

    def _schedule_awaitable(self, gateway: Any, awaitable: Any, *, label: str) -> None:
        def _done(future: Any) -> None:
            try:
                future.result()
            except Exception as exc:
                level = logging.WARNING if label == "telegram send" else logging.DEBUG
                logger.log(level, "cli-bridge %s failed: %s", label, exc)
                return
            if label == "telegram send":
                logger.info("cli-bridge %s completed", label)

        loop = getattr(gateway, "_gateway_loop", None)
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(awaitable, loop)
            future.add_done_callback(_done)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(awaitable)
            except Exception as exc:
                level = logging.WARNING if label == "telegram send" else logging.DEBUG
                logger.log(level, "cli-bridge %s failed: %s", label, exc)
                return
            if label == "telegram send":
                logger.info("cli-bridge %s completed", label)
        else:
            task = running.create_task(awaitable)
            task.add_done_callback(_done)

    def _output_reader(self, session: BridgeSession, gateway: Any, event: Any) -> None:
        if self.output_source == "pipe":
            self._pipe_output_reader(session, gateway, event)
            return
        self._capture_output_reader(session, gateway, event)

    def _capture_output_reader(self, session: BridgeSession, gateway: Any, event: Any) -> None:
        last_snapshot = ""
        last_sent_transcript: str | None = None
        last_flush = time.monotonic()
        while not session.stop_event.is_set():
            try:
                if not self.tmux.has_session(session.session_name):
                    session.stop_event.set()
                    break
                snapshot = self._clean_output(self.tmux.capture(session.session_name))
                now = time.monotonic()
                approval = self._approval_from_capture(session, snapshot)
                if approval is not None:
                    signature = str(approval["signature"])
                    if signature != session.approval_signature:
                        session.approval_signature = signature
                        self._handle_tmux_approval(session, gateway, event, approval)
                        last_snapshot = snapshot
                        last_flush = time.monotonic()
                    session.stop_event.wait(0.25)
                    continue
                session.approval_signature = None

                if snapshot and last_sent_transcript is None:
                    transcript = self._assistant_transcript_from_capture(
                        snapshot, agent=session.agent
                    )
                    last_sent_transcript = transcript
                    last_snapshot = snapshot
                    last_flush = now
                    fields = _event_log_fields(event)
                    logger.info(
                        "cli-bridge capture baseline: agent=%s platform=%s chat=%s "
                        "user=%s session=%s snapshot=%r assistant=%r",
                        session.agent,
                        fields["platform"],
                        fields["chat"],
                        fields["user"],
                        session.session_name,
                        self._log_snippet(snapshot),
                        self._log_snippet(transcript),
                    )
                    self._audit(
                        "capture_baseline",
                        session,
                        event,
                        snapshot=self._audit_snippet(snapshot),
                        assistant=self._audit_snippet(transcript),
                    )
                    continue

                if (
                    snapshot
                    and snapshot != last_snapshot
                    and now - last_flush >= self.output_interval
                ):
                    raw_delta = self._snapshot_delta(last_snapshot, snapshot)
                    transcript = self._assistant_transcript_from_capture(
                        snapshot, agent=session.agent
                    )
                    chat_output = self._transcript_delta(
                        last_sent_transcript, transcript, agent=session.agent
                    )
                    last_sent_transcript = transcript
                    last_snapshot = snapshot
                    last_flush = now
                    if chat_output:
                        chunks = self._chunks(f"[{session.agent}]\n{chat_output}")
                        fields = _event_log_fields(event)
                        logger.info(
                            "cli-bridge output routed: agent=%s platform=%s chat=%s user=%s "
                            "session=%s chars=%d chunks=%d output=%r",
                            session.agent,
                            fields["platform"],
                            fields["chat"],
                            fields["user"],
                            session.session_name,
                            len(chat_output),
                            len(chunks),
                            self._log_snippet(chat_output),
                        )
                        self._audit(
                            "output_routed",
                            session,
                            event,
                            chars=len(chat_output),
                            chunks=len(chunks),
                            output=self._audit_snippet(chat_output),
                            snapshot_delta=self._audit_snippet(raw_delta),
                        )
                        for chunk in chunks:
                            self._reply(gateway, event, chunk)
                    elif raw_delta:
                        fields = _event_log_fields(event)
                        logger.info(
                            "cli-bridge output suppressed: agent=%s platform=%s chat=%s "
                            "user=%s session=%s snapshot=%r assistant=%r",
                            session.agent,
                            fields["platform"],
                            fields["chat"],
                            fields["user"],
                            session.session_name,
                            self._log_snippet(raw_delta),
                            self._log_snippet(transcript),
                        )
                        self._audit(
                            "output_suppressed",
                            session,
                            event,
                            snapshot_delta=self._audit_snippet(raw_delta),
                            assistant=self._audit_snippet(transcript),
                        )
            except Exception as exc:
                logger.debug("cli-bridge capture reader failed: %s", exc)
                self._audit("capture_reader_error", session, event, error=str(exc))
            session.stop_event.wait(0.25)

    def _approval_from_capture(
        self,
        session: Any,
        snapshot: str,
    ) -> dict[str, str] | None:
        agent = getattr(session, "agent", "codex")
        if agent not in PROFILES:
            return None
        return self._profile(agent).approval_from_capture(snapshot)

    def _codex_approval_from_capture(
        self,
        session: Any,
        snapshot: str,
    ) -> dict[str, str] | None:
        return self._approval_from_capture(session, snapshot)

    def _handle_tmux_approval(
        self,
        session: BridgeSession,
        gateway: Any,
        event: Any,
        approval: dict[str, str],
    ) -> None:
        preview = approval.get("preview", "")
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge tmux approval requested: agent=%s platform=%s chat=%s "
            "user=%s session=%s prompt=%r",
            session.agent,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
            self._log_snippet(preview),
        )
        self._audit(
            "tmux_approval_requested",
            session,
            event,
            prompt=self._audit_snippet(preview),
        )
        choice = self._request_tmux_approval_decision(session, gateway, event, preview)
        logger.info(
            "cli-bridge tmux approval resolved: agent=%s platform=%s chat=%s "
            "user=%s session=%s choice=%s",
            session.agent,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
            choice,
        )
        self._audit("tmux_approval_resolved", session, event, choice=choice)
        self._send_tmux_approval_choice(session, choice, preview=preview)

    def _request_tmux_approval_decision(
        self,
        session: BridgeSession,
        gateway: Any,
        event: Any,
        preview: str,
    ) -> str:
        session_key = self._gateway_session_key(gateway, event) or session.key
        agent = getattr(session, "agent", "codex")
        approval_data = {
            "command": f"{agent.title()} tmux approval in {session.cwd}\n\n{preview}",
            "description": f"{agent.title()} is waiting for permission in the tmux bridge.",
            "pattern_key": f"{agent} tmux approval",
            "pattern_keys": [f"{agent} tmux approval"],
        }

        def _notify(data: dict[str, Any]) -> None:
            self._send_tmux_approval_request(gateway, event, session_key, data, agent=agent)

        try:
            from tools.approval import _await_gateway_decision

            result = _await_gateway_decision(
                session_key,
                _notify,
                approval_data,
                surface="cli_bridge_tmux",
            )
        except Exception as exc:
            logger.warning("cli-bridge tmux approval flow failed: %s", exc)
            self._reply(
                gateway,
                event,
                f"[{agent}] approval flow failed; cancelling {agent.title()} request: {exc}",
            )
            return "deny"

        if not result.get("resolved"):
            self._reply(
                gateway,
                event,
                f"[{agent}] approval timed out or could not be delivered; cancelling.",
            )
            return "deny"
        return str(result.get("choice") or "deny")

    def _gateway_session_key(self, gateway: Any, event: Any) -> str:
        session_key_fn = getattr(gateway, "_session_key_for_source", None)
        if callable(session_key_fn):
            try:
                return str(session_key_fn(getattr(event, "source", None)) or "")
            except Exception:
                return ""
        return ""

    def _send_tmux_approval_request(
        self,
        gateway: Any,
        event: Any,
        session_key: str,
        approval_data: dict[str, Any],
        *,
        agent: str = "codex",
    ) -> None:
        adapter, metadata = self._adapter_and_metadata(gateway, event)
        if adapter is None:
            raise RuntimeError("no adapter available for approval request")

        source = getattr(event, "source", None)
        chat_id = str(getattr(source, "chat_id", "") or "")
        command = str(approval_data.get("command") or "")
        description = str(approval_data.get("description") or "permission request")
        send_exec_approval = getattr(adapter, "send_exec_approval", None)
        if callable(send_exec_approval):
            result = send_exec_approval(
                chat_id=chat_id,
                command=command,
                session_key=session_key,
                description=description,
                metadata=metadata,
            )
            if inspect.isawaitable(result):
                result = self._run_awaitable(gateway, result, timeout=15)
            if getattr(result, "success", False):
                return
            logger.warning(
                "cli-bridge tmux approval button send failed: %s",
                getattr(result, "error", "unknown error"),
            )

        prefix = getattr(adapter, "typed_command_prefix", "/")
        self._reply(
            gateway,
            event,
            (
                f"[{agent}] Permission requested in tmux.\n"
                f"Reply `{prefix}approve`, `{prefix}approve session`, "
                f"`{prefix}approve always`, or `{prefix}deny`.\n\n"
                f"{self._snippet(command, 1200)}"
            ),
        )

    def _send_tmux_approval_choice(
        self,
        session: Any,
        choice: str,
        *,
        preview: str = "",
    ) -> None:
        agent = getattr(session, "agent", "codex")
        keys = self._profile(agent).approval_keys(choice, preview)
        self.tmux.send_keys(session.session_name, keys)

    def _pipe_output_reader(self, session: BridgeSession, gateway: Any, event: Any) -> None:
        offset = 0
        pending = ""
        last_flush = time.monotonic()
        while not session.stop_event.is_set():
            try:
                if session.log_path.exists():
                    with session.log_path.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        data = fh.read()
                        offset = fh.tell()
                    if data:
                        pending += data
                now = time.monotonic()
                if pending and now - last_flush >= self.output_interval:
                    cleaned = self._strip_capture_status_lines(
                        self._clean_output(pending), agent=session.agent
                    )
                    pending = ""
                    last_flush = now
                    if cleaned:
                        chunks = self._chunks(f"[{session.agent}]\n{cleaned}")
                        fields = _event_log_fields(event)
                        logger.info(
                            "cli-bridge pipe output routed: agent=%s platform=%s chat=%s "
                            "user=%s session=%s chars=%d chunks=%d output=%r",
                            session.agent,
                            fields["platform"],
                            fields["chat"],
                            fields["user"],
                            session.session_name,
                            len(cleaned),
                            len(chunks),
                            self._log_snippet(cleaned),
                        )
                        self._audit(
                            "pipe_output_routed",
                            session,
                            event,
                            chars=len(cleaned),
                            chunks=len(chunks),
                            output=self._audit_snippet(cleaned),
                        )
                        for chunk in chunks:
                            self._reply(gateway, event, chunk)
            except Exception as exc:
                logger.debug("cli-bridge output reader failed: %s", exc)
                self._audit("pipe_reader_error", session, event, error=str(exc))
            session.stop_event.wait(0.25)

    def _snapshot_delta(self, previous: str, current: str) -> str:
        if not previous:
            return current
        if previous == current:
            return ""

        previous_lines = previous.splitlines()
        current_lines = current.splitlines()
        matcher = difflib.SequenceMatcher(
            a=previous_lines,
            b=current_lines,
            autojunk=False,
        )
        changed_lines: list[str] = []
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag in {"insert", "replace"}:
                changed_lines.extend(current_lines[j1:j2])
        return "\n".join(line for line in changed_lines if line.strip()).strip()

    def _chat_output_from_capture_delta(self, delta: str, agent: str = "codex") -> str:
        return self._assistant_transcript_from_capture(delta, agent=agent)

    def _assistant_transcript_from_capture(self, snapshot: str, agent: str = "codex") -> str:
        return self._profile(agent).assistant_transcript(snapshot)

    def _transcript_delta(self, previous: str, current: str, agent: str = "codex") -> str:
        previous = self._strip_capture_status_lines(previous, agent=agent)
        current = self._strip_capture_status_lines(current, agent=agent)
        if previous == current or not current:
            return ""
        if not previous:
            return current
        if current.startswith(previous):
            return current[len(previous) :].strip()

        previous_blocks = [block for block in previous.split("\n\n") if block.strip()]
        current_blocks = [block for block in current.split("\n\n") if block.strip()]
        matcher = difflib.SequenceMatcher(
            a=previous_blocks,
            b=current_blocks,
            autojunk=False,
        )
        changed_blocks: list[str] = []
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag in {"insert", "replace"}:
                changed_blocks.extend(current_blocks[j1:j2])
        return "\n\n".join(changed_blocks).strip()

    def _legacy_chat_output_from_capture_delta(self, delta: str) -> str:
        lines: list[str] = []
        skipping_prompt = False
        for raw_line in delta.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if self._is_capture_chrome_line(line):
                skipping_prompt = False
                continue
            if line.startswith("›"):
                skipping_prompt = True
                continue
            if skipping_prompt and self._capture_bullet_body(line) is None:
                continue
            skipping_prompt = False

            if self._is_capture_status_line(line):
                continue
            bullet_body = self._capture_bullet_body(line)
            if bullet_body is not None:
                line = bullet_body
            if line:
                lines.append(line)
        return "\n".join(lines).strip()

    def _log_snippet(self, text: str, limit: int | None = None) -> str:
        return self._snippet(text, limit or self.log_snippet_chars)

    def _audit_snippet(self, text: str) -> str:
        return self._snippet(text, _env_int("HERMES_CLI_BRIDGE_AUDIT_SNIPPET_CHARS", 500))

    def _snippet(self, text: str, limit: int) -> str:
        cleaned = _redact(str(text or "")).replace("\n", "\\n")
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + "..."

    def _audit(
        self,
        event_type: str,
        session: BridgeSession,
        event: Any,
        **fields: Any,
    ) -> None:
        if not self.audit_enabled:
            return
        try:
            event_fields = _event_log_fields(event)
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event_type,
                "agent": session.agent,
                "session": session.session_name,
                "platform": event_fields["platform"],
                "chat": event_fields["chat"],
                "user": event_fields["user"],
                **fields,
            }
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
        except Exception as exc:
            logger.debug("cli-bridge audit write failed: %s", exc)

    def _is_capture_chrome_line(self, line: str, agent: str = "codex") -> bool:
        return self._profile(agent).is_chrome_line(line)

    def _capture_bullet_body(self, line: str, agent: str = "codex") -> str | None:
        return self._profile(agent).bullet_body(line)

    def _is_capture_status_line(self, line: str, agent: str = "codex") -> bool:
        return self._profile(agent).is_status_line(line)

    def _strip_capture_status_lines(self, text: str, agent: str = "codex") -> str:
        return self._profile(agent).strip_status_lines(text)

    def _clean_output(self, text: str) -> str:
        cleaned = _OSC_RE.sub("", text)
        cleaned = _ANSI_RE.sub("", cleaned)
        cleaned = self._collapse_terminal_redraws(cleaned)
        cleaned = _CONTROL_RE.sub("", cleaned)
        cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = _redact(cleaned)
        cleaned = cleaned.strip()
        if len(cleaned) > self.max_output_chars:
            cleaned = (
                f"[output truncated to last {self.max_output_chars} chars]\n"
                f"{cleaned[-self.max_output_chars:]}"
            )
        return cleaned

    def _collapse_terminal_redraws(self, text: str) -> str:
        normalized = text.replace("\r\n", "\n")
        lines: list[str] = []
        for line in normalized.split("\n"):
            if "\r" in line:
                frames = line.split("\r")
                line = next((frame for frame in reversed(frames) if frame.strip()), frames[-1])
            lines.append(line)
        return "\n".join(lines)

    def _chunks(self, text: str) -> list[str]:
        if len(text) <= self.chunk_chars:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            chunks.append(remaining[: self.chunk_chars])
            remaining = remaining[self.chunk_chars :]
        return chunks

    def _help(self, agent: str) -> str:
        return (
            f"/{agent} init [name] [--cwd <cwd>] - start or attach a named session\n"
            f"/{agent} list [--all] - show this chat's sessions\n"
            f"/{agent} select <name|none> - choose the current session\n"
            f"/{agent} rename <new-name> - rename the current bridge session\n"
            f"/{agent} send <text> - send exact text, including slash commands\n"
            f"/{agent} status [name|current] - show bridge status\n"
            f"/{agent} exit - exit the current bridge without killing it\n"
            f"/{agent} kill [name|current] - kill a session\n\n"
            f"`/{agent} end` is an alias for `/{agent} kill current`. "
            "When a current session is selected, ordinary non-slash messages "
            f"go to the CLI. Use /{agent} select none to return ordinary "
            f"messages to Hermes, and /{agent} send /command for CLI slash commands."
        )
