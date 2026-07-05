"""Per-CLI dialect profiles for the tmux bridge.

Everything vendor-specific about a bridged CLI lives here: how its TUI
renders assistant output, how startup readiness and approval dialogs look
in a captured pane, which keys answer an approval, and how the optional
non-interactive exec backend is invoked and parsed. The bridge itself
stays agnostic and routes through a profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

_CODEX_BULLET_RE = re.compile(r"^[•·∙]\s+(.*)$")
_CODEX_PROGRESS_STATUS_RE = re.compile(
    r"^(?:Working|Thinking) "
    r"\(\d+(?:\.\d+)?s\s+[•·∙]\s+esc to interrupt\)$"
)
_CODEX_MCP_STATUS_RE = re.compile(
    r"^Starting MCP servers?"
    r"(?: \(\d+/\d+\): .+)? "
    r"\(\d+(?:\.\d+)?s\s+[•·∙]\s+esc to interrupt\)$"
)


class AgentProfile:
    """TUI and exec dialect for one bridged CLI."""

    name: str = ""
    default_command: str = ""
    command_env: str = ""
    exec_command_env: str = ""
    default_submit_keys: tuple[str, ...] = ("Enter",)
    supports_exec: bool = False

    bullet_re: re.Pattern[str] | None = None
    prompt_echo_prefixes: tuple[str, ...] = ()
    status_line_res: tuple[re.Pattern[str], ...] = ()
    approval_markers: tuple[str, ...] = ()
    approval_confirm_markers: tuple[str, ...] = ()

    def command(self) -> str:
        return os.environ.get(self.command_env, "").strip() or self.default_command

    def submit_keys(self) -> list[str]:
        raw = (
            os.environ.get(f"HERMES_CLI_BRIDGE_{self.name.upper()}_SUBMIT_KEYS")
            or os.environ.get("HERMES_CLI_BRIDGE_TMUX_SUBMIT_KEYS")
            or ",".join(self.default_submit_keys)
        )
        keys = [part.strip() for part in raw.replace(" ", ",").split(",") if part.strip()]
        return keys or list(self.default_submit_keys)

    def snapshot_ready(self, snapshot: str) -> bool:
        return bool(snapshot.strip())

    def is_chrome_line(self, line: str) -> bool:
        return line.startswith(("╭", "╰", "│"))

    def bullet_body(self, line: str) -> str | None:
        if self.bullet_re is None:
            return None
        match = self.bullet_re.match(line)
        if match is None:
            return None
        return match.group(1).strip()

    def is_prompt_echo(self, line: str) -> bool:
        return line.startswith(self.prompt_echo_prefixes) if self.prompt_echo_prefixes else False

    def is_status_line(self, line: str) -> bool:
        normalized = self.bullet_body(line) or line.strip()
        return any(pattern.fullmatch(normalized) for pattern in self.status_line_res)

    def strip_status_lines(self, text: str) -> str:
        lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and self.is_status_line(stripped):
                continue
            lines.append(line.rstrip())
        return "\n".join(lines).strip()

    def assistant_transcript(self, snapshot: str) -> str:
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

            if self.is_chrome_line(line):
                if in_assistant:
                    flush_current()
                in_assistant = False
                in_user_prompt = False
                continue

            if self.is_prompt_echo(line):
                if in_assistant:
                    flush_current()
                in_assistant = False
                in_user_prompt = True
                continue

            bullet_body = self.bullet_body(line)
            if bullet_body is not None:
                if in_assistant:
                    flush_current()
                in_user_prompt = False
                if self.is_status_line(line):
                    in_assistant = False
                    continue
                current = [bullet_body] if bullet_body else []
                in_assistant = True
                continue

            if in_user_prompt:
                continue

            if in_assistant:
                if self.is_status_line(line):
                    flush_current()
                    in_assistant = False
                    continue
                current.append(line)

        if in_assistant:
            flush_current()
        return "\n\n".join(blocks).strip()

    def approval_from_capture(self, snapshot: str) -> dict[str, str] | None:
        if not snapshot or not self.approval_markers:
            return None
        lines = [line.strip() for line in snapshot.splitlines() if line.strip()]
        if not lines:
            return None
        lowered = "\n".join(lines).lower()
        if not any(marker in lowered for marker in self.approval_markers):
            return None
        if self.approval_confirm_markers and not any(
            marker in lowered for marker in self.approval_confirm_markers
        ):
            return None

        marker_idx = 0
        for idx, line in enumerate(lines):
            if any(marker in line.lower() for marker in self.approval_markers):
                marker_idx = idx
                break
        start = max(0, marker_idx - 8)
        end = min(len(lines), marker_idx + 14)
        preview = "\n".join(lines[start:end]).strip()
        signature = hashlib.sha256(preview.encode("utf-8", errors="replace")).hexdigest()
        return {"signature": signature, "preview": preview}

    def approval_keys(self, choice: str, preview: str) -> list[str]:
        raise NotImplementedError

    def exec_command(self, resume_token: str | None, output_path: Path) -> list[str]:
        raise NotImplementedError

    def parse_exec_stdout(self, stdout: str) -> tuple[str | None, str]:
        raise NotImplementedError


class CodexProfile(AgentProfile):
    name = "codex"
    default_command = "codex"
    command_env = "HERMES_CLI_BRIDGE_CODEX_CMD"
    exec_command_env = "HERMES_CLI_BRIDGE_CODEX_EXEC_CMD"
    default_submit_keys = ("Escape", "Enter")
    supports_exec = True

    bullet_re = _CODEX_BULLET_RE
    prompt_echo_prefixes = ("›",)
    status_line_res = (_CODEX_PROGRESS_STATUS_RE, _CODEX_MCP_STATUS_RE)
    approval_markers = (
        "would you like to make the following edits",
        "would you like to run the following command",
        "command approval required",
        "requires approval",
        "press enter to confirm or esc to cancel",
        "do you trust the contents of this directory",
    )
    approval_confirm_markers = (
        "yes, proceed",
        "allow once",
        "press enter to confirm",
        "don't ask again",
        "yes, continue",
    )

    def snapshot_ready(self, snapshot: str) -> bool:
        if not snapshot.strip():
            return False
        lower = snapshot.lower()
        if "loading" in lower or "starting mcp servers" in lower:
            return False
        return "OpenAI Codex" in snapshot and "\n›" in f"\n{snapshot}"

    def is_chrome_line(self, line: str) -> bool:
        if super().is_chrome_line(line):
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

    def approval_keys(self, choice: str, preview: str) -> list[str]:
        normalized = (choice or "deny").strip().lower()
        trust_prompt = "do you trust the contents of this directory" in preview.lower()
        if normalized in {"once", "session", "always"} and trust_prompt:
            return ["Enter"]
        if normalized == "once":
            return ["y"]
        if normalized in {"session", "always"}:
            return [self._persistent_approval_key(preview)]
        return ["Escape"]

    def _persistent_approval_key(self, preview: str) -> str:
        for line in preview.splitlines():
            if "don't ask again" not in line.lower():
                continue
            match = re.search(r"\(([A-Za-z])\)", line)
            if match:
                return match.group(1)
        return "a"

    def exec_command(self, resume_token: str | None, output_path: Path) -> list[str]:
        base = self._exec_base_command()
        common = ["--json", "--output-last-message", str(output_path)]
        if resume_token:
            return [base[0], "exec", "resume", *base[2:], *common, resume_token, "-"]
        return [*base, *common, "-"]

    def _exec_base_command(self) -> list[str]:
        explicit = os.environ.get(self.exec_command_env, "").strip()
        if explicit:
            argv = shlex.split(explicit)
        else:
            argv = shlex.split(self.command())
            if not argv:
                argv = [self.default_command]
            argv = [argv[0], "exec", *self._exec_safe_options(argv[1:])]
        if len(argv) < 2 or argv[1] != "exec":
            argv = [argv[0], "exec", *argv[1:]]
        return argv

    def _exec_safe_options(self, options: list[str]) -> list[str]:
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

    def parse_exec_stdout(self, stdout: str) -> tuple[str | None, str]:
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


_CLAUDE_SPINNER_GLYPHS = "✻✽✢✳✶·∗*+"
_CLAUDE_SPINNER_RE = re.compile(
    rf"^[{_CLAUDE_SPINNER_GLYPHS}]\s+\S.*…(?:\s+\(.+\))?$"
)
_CLAUDE_TURN_SUMMARY_RE = re.compile(
    rf"^[{_CLAUDE_SPINNER_GLYPHS}]\s+\w+ for \d+(?:m )?\d*s?$"
)
_CLAUDE_WAITING_RE = re.compile(r"^⎿\s+Waiting…$")
_CLAUDE_STATUS_BAR_RE = re.compile(r"\bmodel:\S")


class ClaudeProfile(AgentProfile):
    name = "claude"
    default_command = "claude"
    command_env = "HERMES_CLI_BRIDGE_CLAUDE_CMD"
    exec_command_env = "HERMES_CLI_BRIDGE_CLAUDE_EXEC_CMD"
    default_submit_keys = ("Enter",)
    supports_exec = True

    bullet_re = re.compile(r"^[⏺●]\s*(.*)$")
    prompt_echo_prefixes = ("❯",)
    status_line_res = (_CLAUDE_SPINNER_RE, _CLAUDE_TURN_SUMMARY_RE, _CLAUDE_WAITING_RE)
    approval_markers = (
        "do you want to ",
        "this command requires approval",
        "quick safety check",
    )
    approval_confirm_markers = (
        "❯ 1.",
        "❯ 2.",
        "❯ 3.",
    )

    def snapshot_ready(self, snapshot: str) -> bool:
        if not snapshot.strip():
            return False
        if "claude code v" not in snapshot.lower():
            return False
        return any(
            line.strip().startswith("❯") for line in snapshot.splitlines()
        )

    def is_chrome_line(self, line: str) -> bool:
        if line.startswith(("╭", "╰", "│", "─", "╌", "▎", "⚠")):
            return True
        if _CLAUDE_STATUS_BAR_RE.search(line):
            return True
        if line.startswith(("← for agents", "? for shortcuts", "/rc")):
            return True
        if "tmux focus-events" in line:
            return True
        return line.startswith("Esc ") or "Esc again to clear" in line

    def approval_keys(self, choice: str, preview: str) -> list[str]:
        normalized = (choice or "deny").strip().lower()
        if normalized == "once":
            return ["1"]
        if normalized in {"session", "always"}:
            if re.search(r"(?:❯\s*)?2\.\s*yes", preview, re.IGNORECASE):
                return ["2"]
            return ["1"]
        return ["Escape"]

    def exec_command(self, resume_token: str | None, output_path: Path) -> list[str]:
        del output_path
        base = self._exec_base_command()
        common = ["--print", "--output-format", "json"]
        if resume_token:
            return [*base, *common, "--resume", resume_token]
        return [*base, *common]

    def _exec_base_command(self) -> list[str]:
        explicit = os.environ.get(self.exec_command_env, "").strip()
        if explicit:
            argv = shlex.split(explicit)
        else:
            argv = shlex.split(self.command()) or [self.default_command]
        return [
            option
            for option in argv
            if option not in {"--print", "-p", "--continue", "-c"}
        ]

    def parse_exec_stdout(self, stdout: str) -> tuple[str | None, str]:
        stripped = stdout.strip()
        candidates: list[Any] = []
        if stripped.startswith("{"):
            try:
                candidates.append(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        if not candidates:
            for line in stripped.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        session_id = None
        message = ""
        for event in candidates:
            if not isinstance(event, dict):
                continue
            session_id = str(event.get("session_id") or "") or session_id
            if event.get("type") == "result" or "result" in event:
                message = str(event.get("result") or "")
        return session_id, message


PROFILES: dict[str, AgentProfile] = {
    profile.name: profile for profile in (CodexProfile(), ClaudeProfile())
}
