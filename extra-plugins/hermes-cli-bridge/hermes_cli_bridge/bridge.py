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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("gateway.cli_bridge")

_OSC_RE = re.compile(r"\x1B\](?:[^\x07\x1B]|\x1B(?!\\))*?(?:\x07|\x1B\\)")
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


class TmuxClient:
    """Small tmux wrapper kept separate for unit tests."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self._runner = runner or subprocess.run

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

    def send_input(self, session_name: str, text: str) -> None:
        if "\n" in text:
            buffer_name = _safe_name(f"{session_name}-input")[:64]
            self._run(
                ["tmux", "load-buffer", "-b", buffer_name, "-"],
                input=text,
            )
            self._run(
                ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", session_name]
            )
            self._run(["tmux", "send-keys", "-t", session_name, "C-m"])
            return

        if text:
            self._run(["tmux", "send-keys", "-t", session_name, "-l", text])
        self._run(["tmux", "send-keys", "-t", session_name, "C-m"])

    def stop(self, session_name: str) -> None:
        self._run(["tmux", "kill-session", "-t", session_name], check=False)


class CliBridgePlugin:
    """Register and run chat-to-CLI bridge sessions."""

    _AGENT_COMMAND_ENV = {
        "codex": "HERMES_CLI_BRIDGE_CODEX_CMD",
        "claude": "HERMES_CLI_BRIDGE_CLAUDE_CMD",
    }
    _DEFAULT_COMMANDS = {
        "codex": "codex",
        "claude": "claude",
    }

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
        self._lock = threading.RLock()

    def register(self, ctx: Any) -> None:
        ctx.register_hook("pre_gateway_dispatch", self.handle_pre_gateway_dispatch)
        for agent in ("codex", "claude"):
            ctx.register_command(
                agent,
                handler=lambda raw_args, _agent=agent: self._command_stub(_agent, raw_args),
                description=f"Control a tmux-backed {agent.title()} CLI bridge.",
                args_hint="init|send|status|end",
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
                self.tmux.send_input(session.session_name, payload)
                routed = True
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
        if command not in self._DEFAULT_COMMANDS:
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
            cwd_arg = argv[1] if len(argv) > 1 else None
            return self._start_session(agent, cwd_arg, event, gateway)
        if subcommand == "send":
            payload = raw_args.strip().partition(" ")[2]
            return self._send_control(agent, payload, event, gateway)
        if subcommand == "status":
            return self._status(agent, event)
        if subcommand == "end":
            return self._end_session(agent, event)
        return f"[{agent}] unknown subcommand: {subcommand}\n\n{self._help(agent)}"

    def _start_session(
        self,
        agent: str,
        cwd_arg: str | None,
        event: Any,
        gateway: Any,
    ) -> str:
        key = self._session_key(agent, getattr(event, "source", None))
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
                return (
                    f"[{agent}] already active in {existing.cwd}\n"
                    f"{label}: {existing.session_name}"
                )
            if existing is not None:
                self._drop_session_locked(existing)

            cwd = self._resolve_cwd(cwd_arg)
            command = self._agent_command(agent)
            backend = self._agent_backend(agent)
            suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
            session_name = _safe_name(f"hermes-{agent}-{suffix}")[:64]
            if backend == "exec":
                session_name = _safe_name(f"hermes-{agent}-exec-{suffix}")[:64]
            log_path = self.state_dir / f"{session_name}.log"
            session = BridgeSession(
                agent=agent,
                key=key,
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
            self._sessions[key] = session

            if backend == "tmux" and self.enable_output_reader:
                reader = threading.Thread(
                    target=self._output_reader,
                    args=(session, gateway, event),
                    name=f"hermes-cli-bridge-{agent}",
                    daemon=True,
                )
                session.reader = reader
                reader.start()

        action = "attached to existing session" if attached else "started"
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge session %s: agent=%s backend=%s platform=%s chat=%s user=%s session=%s cwd=%s",
            "attached" if attached else "started",
            agent,
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
        )
        label = "tmux" if session.backend == "tmux" else "exec"
        return f"[{agent}] {action} in {cwd}\n{label}: {session_name}"

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
            self.tmux.send_input(session.session_name, payload)
            routed = True
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

    def _status(self, agent: str, event: Any) -> str:
        session = self._session_for_event(event, agent=agent, require_live=True)
        if session is None:
            return f"[{agent}] no active bridge for this chat."
        return (
            f"[{agent}] active\n"
            f"backend: {session.backend}\n"
            f"cwd: {session.cwd}\n"
            f"session: {session.session_name}"
            + (f"\nthread: {session.thread_id}" if session.thread_id else "")
        )

    def _end_session(self, agent: str, event: Any) -> str:
        key = self._session_key(agent, getattr(event, "source", None))
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return f"[{agent}] no active bridge for this chat."
        session.stop_event.set()
        if session.backend == "tmux":
            self.tmux.stop(session.session_name)
        fields = _event_log_fields(event)
        logger.info(
            "cli-bridge session ended: agent=%s platform=%s chat=%s user=%s session=%s",
            agent,
            fields["platform"],
            fields["chat"],
            fields["user"],
            session.session_name,
        )
        self._audit("session_ended", session, event)
        return f"[{agent}] ended {session.backend} session {session.session_name}."

    def _session_for_event(
        self,
        event: Any,
        *,
        agent: str | None = None,
        require_live: bool = False,
    ) -> BridgeSession | None:
        source = getattr(event, "source", None)
        with self._lock:
            if agent is not None:
                session = self._sessions.get(self._session_key(agent, source))
                return self._live_or_drop_locked(session) if require_live else session
            for candidate in ("codex", "claude"):
                session = self._sessions.get(self._session_key(candidate, source))
                if session is not None:
                    if not require_live:
                        return session
                    live = self._live_or_drop_locked(session)
                    if live is not None:
                        return live
        return None

    def _drop_session_locked(self, session: BridgeSession) -> None:
        session.stop_event.set()
        if session.backend == "tmux":
            self.tmux.stop(session.session_name)
        self._sessions.pop(session.key, None)

    def _live_or_drop_locked(self, session: BridgeSession | None) -> BridgeSession | None:
        if session is None:
            return None
        if session.backend == "exec":
            return session
        if self.tmux.has_session(session.session_name):
            return session
        session.stop_event.set()
        self._sessions.pop(session.key, None)
        return None

    def _session_key(self, agent: str, source: Any) -> str:
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

    def _agent_command(self, agent: str) -> str:
        env_name = self._AGENT_COMMAND_ENV[agent]
        return os.environ.get(env_name, "").strip() or self._DEFAULT_COMMANDS[agent]

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
        if backend == "exec" and agent != "codex":
            logger.warning("cli-bridge exec backend is only implemented for codex; using tmux")
            return "tmux"
        return backend

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
        worker.start()
        return True

    def _prepare_exec_payload(
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
            payload = self._prepare_exec_payload(session, payload, gateway, event)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.unlink(missing_ok=True)
            command = self._codex_exec_command(session, output_path)
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
            thread_id, stdout_message = self._parse_codex_exec_stdout(result.stdout)
            if thread_id:
                session.thread_id = thread_id
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
        base = self._codex_exec_base_command()
        common = ["--json", "--output-last-message", str(output_path)]
        if session.thread_id:
            return [
                base[0],
                "exec",
                "resume",
                *base[2:],
                *common,
                session.thread_id,
                "-",
            ]
        return [*base, *common, "-"]

    def _codex_exec_base_command(self) -> list[str]:
        explicit = os.environ.get("HERMES_CLI_BRIDGE_CODEX_EXEC_CMD", "").strip()
        if explicit:
            argv = shlex.split(explicit)
        else:
            argv = shlex.split(self._agent_command("codex"))
            if not argv:
                argv = ["codex"]
            argv = [argv[0], "exec", *self._codex_exec_safe_options(argv[1:])]
        if len(argv) < 2 or argv[1] != "exec":
            argv = [argv[0], "exec", *argv[1:]]
        return argv

    def _codex_exec_safe_options(self, options: list[str]) -> list[str]:
        safe: list[str] = []
        skip_next = False
        drop_with_value = {"--remote", "--remote-auth-token-env"}
        for option in options:
            if skip_next:
                skip_next = False
                continue
            if option == "--no-alt-screen":
                continue
            if option in drop_with_value:
                skip_next = True
                continue
            if any(option.startswith(f"{drop}=") for drop in drop_with_value):
                continue
            safe.append(option)
        return safe

    def _parse_codex_exec_stdout(self, stdout: str) -> tuple[str | None, str]:
        thread_id = None
        message = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = str(event.get("thread_id") or "") or thread_id
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = str(item.get("text") or "")
        return thread_id, message

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
                if snapshot and last_sent_transcript is None:
                    transcript = self._assistant_transcript_from_capture(snapshot)
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
                    transcript = self._assistant_transcript_from_capture(snapshot)
                    chat_output = self._transcript_delta(last_sent_transcript, transcript)
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
                    cleaned = self._clean_output(pending)
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

    def _chat_output_from_capture_delta(self, delta: str) -> str:
        return self._assistant_transcript_from_capture(delta)

    def _assistant_transcript_from_capture(self, snapshot: str) -> str:
        blocks: list[str] = []
        current: list[str] = []
        in_assistant = False
        in_user_prompt = False

        def flush_current() -> None:
            nonlocal current
            block = "\n".join(line for line in current if line).strip()
            if block:
                blocks.append(block)
            current = []

        for raw_line in snapshot.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if self._is_capture_chrome_line(line):
                if in_assistant:
                    flush_current()
                in_assistant = False
                in_user_prompt = False
                continue

            if line.startswith("›"):
                if in_assistant:
                    flush_current()
                in_assistant = False
                in_user_prompt = True
                continue

            if line.startswith("• "):
                if in_assistant:
                    flush_current()
                in_user_prompt = False
                if self._is_capture_status_line(line):
                    in_assistant = False
                    continue
                body = line[2:].strip()
                current = [body] if body else []
                in_assistant = True
                continue

            if in_user_prompt:
                continue

            if in_assistant:
                if self._is_capture_status_line(line):
                    flush_current()
                    in_assistant = False
                    continue
                current.append(line)

        if in_assistant:
            flush_current()
        return "\n\n".join(blocks).strip()

    def _transcript_delta(self, previous: str, current: str) -> str:
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
            if skipping_prompt and not line.startswith("•"):
                continue
            skipping_prompt = False

            if self._is_capture_status_line(line):
                continue
            if line.startswith("• "):
                line = line[2:].strip()
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

    def _is_capture_chrome_line(self, line: str) -> bool:
        if line.startswith(("╭", "╰", "│")):
            return True
        if line.startswith(("OpenAI Codex", ">_ OpenAI Codex", "model:", "directory:")):
            return True
        if "·" in line and "hermes-agent" in line:
            return True
        if re.match(r"^gpt-[\w.-]+(?:\s+\w+)?\s+·", line):
            return True
        if line.startswith("Tip:"):
            return True
        return "usage limit reset" in line or "usage limit resets" in line

    def _is_capture_status_line(self, line: str) -> bool:
        normalized = line[2:].strip() if line.startswith("• ") else line
        status_prefixes = (
            "Working",
            "Thinking",
            "Starting MCP servers",
            "Starting MCP server",
        )
        return normalized.startswith(status_prefixes)

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
            f"/{agent} init [cwd] - start a tmux-backed {agent} session\n"
            f"/{agent} send <text> - send exact text, including slash commands\n"
            f"/{agent} status - show this chat's bridge status\n"
            f"/{agent} end - kill this chat's tmux session\n\n"
            "While active, ordinary non-slash messages go to the CLI. "
            f"Use /{agent} send /command for CLI slash commands."
        )
