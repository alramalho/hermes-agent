from __future__ import annotations

import asyncio
import json
import sys
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "extra-plugins" / "hermes-cli-bridge"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hermes_cli_bridge import CliBridgePlugin, register  # noqa: E402
from hermes_cli_bridge.bridge import TmuxClient  # noqa: E402

# Pane fixtures below are verbatim shapes captured from claude 2.1.201 and
# codex 0.142.5 running inside `tmux capture-pane -p -J` at 140x40.
CLAUDE_READY_PANE = """\
╭─── Claude Code v2.1.201 ──────────────────────────╮
│                 Welcome back Alex!                 │
│ Fable 5 with xhigh effort · Claude Max · Alexandre │
│         /…/scratchpad/claude-cap/freshdir          │
╰────────────────────────────────────────────────────╯
────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────
  freshdir  model:Fable 5 (effort:xhigh)
  ← for agents"""

CLAUDE_TRUST_DIALOG_PANE = """\
────────────────────────────────────────────────────
 Accessing workspace:

 /tmp/scratch/freshdir

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source project, or work from
 your team). If not, take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel"""

CLAUDE_WRITE_DIALOG_PANE = """\
❯ Create a file named hello.txt containing the word hi, using the Write tool.

⏺ Write(hello.txt)

────────────────────────────────────────────────────
 Create file
 hello.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hi
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create hello.txt?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend"""

CLAUDE_BASH_DIALOG_PANE = """\
⏺ Bash(node -e "console.log(1)")
  ⎿  Waiting…

────────────────────────────────────────────────────
 Bash command

   node -e "console.log(1)"
   Run Node one-liner printing 1

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: node *
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain"""

CODEX_TRUST_DIALOG_PANE = """\
> You are in /tmp/scratch/codex-cap

  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection.

› 1. Yes, continue
  2. No, quit

  Press enter to continue"""


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.started: list[dict[str, object]] = []
        self.inputs: list[tuple[str, str]] = []
        self.submit_keys: list[tuple[str, list[str]]] = []
        self.keys: list[tuple[str, list[str]]] = []
        self.stopped: list[str] = []
        self.capture_text = "OpenAI Codex\nmodel: gpt-5.5\n› Explain this codebase"
        self.captures: list[str] = []
        self.capture_calls: list[str] = []

    def start(self, **kwargs) -> None:
        session_name = str(kwargs["session_name"])
        self.sessions.add(session_name)
        self.started.append(kwargs)

    def has_session(self, session_name: str) -> bool:
        return session_name in self.sessions

    def send_input(
        self,
        session_name: str,
        text: str,
        *,
        submit_keys: list[str] | None = None,
    ) -> None:
        self.inputs.append((session_name, text))
        self.submit_keys.append((session_name, list(submit_keys or [])))

    def send_keys(self, session_name: str, keys: list[str]) -> None:
        self.keys.append((session_name, list(keys)))

    def capture(self, session_name: str) -> str:
        self.capture_calls.append(session_name)
        if self.captures:
            return self.captures.pop(0)
        return self.capture_text

    def stop(self, session_name: str) -> None:
        self.sessions.discard(session_name)
        self.stopped.append(session_name)


def _event(
    text: str,
    user_id: str = "u1",
    thread_id: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat1",
            chat_type="dm",
            thread_id=thread_id,
            user_id=user_id,
            user_name="Alex",
        ),
    )


def _gateway(*, authorized: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _is_user_authorized=lambda source: authorized,
        adapters={},
    )


def _plugin(
    fake_tmux: FakeTmux,
    replies: list[str],
    tmp_path: Path,
    **kwargs,
) -> CliBridgePlugin:
    return CliBridgePlugin(
        tmux=fake_tmux,  # type: ignore[arg-type]
        sender=lambda _gateway, _event, text: replies.append(text),
        enable_output_reader=False,
        state_dir=tmp_path,
        **kwargs,
    )


def test_register_adds_hook_and_commands() -> None:
    manager = PluginManager()
    manifest = PluginManifest(name="cli-bridge", source="entrypoint", key="cli-bridge")
    ctx = PluginContext(manifest, manager)

    register(ctx)

    assert "pre_gateway_dispatch" in manager._hooks
    assert "codex" in manager._plugin_commands
    assert "claude" in manager._plugin_commands


def test_unauthorized_control_falls_through(tmp_path: Path) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex init"),
        gateway=_gateway(authorized=False),
    )

    assert result == {"action": "allow"}
    assert fake_tmux.started == []
    assert replies == []


def test_codex_init_starts_tmux_and_skips_gateway(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert len(fake_tmux.started) == 1
    assert fake_tmux.started[0]["cwd"] == tmp_path
    assert fake_tmux.started[0]["command"] == "codex"
    assert replies[-1].startswith("[codex:default] started in")


def test_codex_init_waits_for_tmux_ready(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.captures = [
        "",
        "OpenAI Codex\nmodel: loading\n› Explain this codebase",
        "OpenAI Codex\n• Starting MCP servers (2/6)\n› Explain this codebase",
        "OpenAI Codex\nmodel: gpt-5.5\n› Explain this codebase",
    ]
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermes_cli_bridge.bridge.time.sleep", lambda _seconds: None)

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert len(fake_tmux.capture_calls) == 4
    assert replies[-1].startswith("[codex:default] started in")


def test_codex_init_reattaches_existing_tmux_after_gateway_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    monkeypatch.chdir(tmp_path)

    first = _plugin(fake_tmux, replies, tmp_path)
    first.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())

    restarted = _plugin(fake_tmux, replies, tmp_path)
    result = restarted.handle_pre_gateway_dispatch(
        event=_event("/codex init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert len(fake_tmux.started) == 1
    assert replies[-1].startswith("[codex:default] attached to existing session in")


def test_sessions_restore_from_registry_and_route_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    monkeypatch.chdir(tmp_path)

    first = _plugin(fake_tmux, replies, tmp_path)
    first.handle_pre_gateway_dispatch(event=_event("/codex init api"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    restarted = _plugin(fake_tmux, replies, tmp_path)
    restarted.handle_pre_gateway_dispatch(event=_event("/codex list"), gateway=_gateway())
    assert "- api (tmux)" in replies[-1]

    result = restarted.handle_pre_gateway_dispatch(
        event=_event("after restart"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs[-1] == (session_name, "after restart")


def test_stale_registry_session_is_pruned_from_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    monkeypatch.chdir(tmp_path)

    first = _plugin(fake_tmux, replies, tmp_path)
    first.handle_pre_gateway_dispatch(event=_event("/codex init api"), gateway=_gateway())
    fake_tmux.sessions.clear()

    restarted = _plugin(fake_tmux, replies, tmp_path)
    restarted.handle_pre_gateway_dispatch(event=_event("/codex list"), gateway=_gateway())

    assert replies[-1] == "[codex] no bridge sessions for this chat."
    registry = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert registry["sessions"] == []


def test_list_all_shows_codex_and_claude_sessions_across_topics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)

    plugin.handle_pre_gateway_dispatch(
        event=_event("/codex init api", thread_id="topic-a"),
        gateway=_gateway(),
    )
    fake_tmux.capture_text = CLAUDE_READY_PANE
    plugin.handle_pre_gateway_dispatch(
        event=_event("/claude init docs", thread_id="topic-b"),
        gateway=_gateway(),
    )

    plugin.handle_pre_gateway_dispatch(event=_event("/codex list --all"), gateway=_gateway())
    codex_reply = replies[-1]
    assert codex_reply.startswith("[codex] all bridge sessions:")
    assert "codex:api (tmux)" in codex_reply
    assert "topic=topic-a" in codex_reply
    assert "claude:docs (tmux)" in codex_reply
    assert "topic=topic-b" in codex_reply

    plugin.handle_pre_gateway_dispatch(event=_event("/claude list --all"), gateway=_gateway())
    claude_reply = replies[-1]
    assert claude_reply.startswith("[claude] all bridge sessions:")
    assert "codex:api (tmux)" in claude_reply
    assert "claude:docs (tmux)" in claude_reply


def test_named_sessions_can_be_listed_selected_and_cleared(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    work_api = tmp_path / "api"
    work_web = tmp_path / "web"
    work_api.mkdir()
    work_web.mkdir()
    monkeypatch.chdir(tmp_path)

    plugin.handle_pre_gateway_dispatch(
        event=_event(f"/codex init api --cwd {work_api}"),
        gateway=_gateway(),
    )
    plugin.handle_pre_gateway_dispatch(
        event=_event(f"/codex init web --cwd {work_web}"),
        gateway=_gateway(),
    )
    web_session = str(fake_tmux.started[-1]["session_name"])

    plugin.handle_pre_gateway_dispatch(event=_event("/codex list"), gateway=_gateway())
    assert "- api (tmux)" in replies[-1]
    assert "* web (tmux)" in replies[-1]

    plugin.handle_pre_gateway_dispatch(event=_event("hello web"), gateway=_gateway())
    assert fake_tmux.inputs[-1] == (web_session, "hello web")

    plugin.handle_pre_gateway_dispatch(event=_event("/codex select api"), gateway=_gateway())
    api_session = str(fake_tmux.started[0]["session_name"])
    plugin.handle_pre_gateway_dispatch(event=_event("hello api"), gateway=_gateway())
    assert fake_tmux.inputs[-1] == (api_session, "hello api")

    plugin.handle_pre_gateway_dispatch(event=_event("/codex select none"), gateway=_gateway())
    result = plugin.handle_pre_gateway_dispatch(
        event=_event("this should go to Hermes"),
        gateway=_gateway(),
    )
    assert result is None
    assert fake_tmux.inputs[-1] == (api_session, "hello api")


def test_named_sessions_can_be_killed_by_name_or_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)

    plugin.handle_pre_gateway_dispatch(event=_event("/codex init api"), gateway=_gateway())
    api_session = str(fake_tmux.started[-1]["session_name"])
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init web"), gateway=_gateway())
    web_session = str(fake_tmux.started[-1]["session_name"])

    plugin.handle_pre_gateway_dispatch(event=_event("/codex kill api"), gateway=_gateway())
    assert fake_tmux.stopped[-1] == api_session
    assert replies[-1] == f"[codex:api] killed tmux session {api_session}."

    plugin.handle_pre_gateway_dispatch(event=_event("/codex kill current"), gateway=_gateway())
    assert fake_tmux.stopped[-1] == web_session
    assert replies[-1] == f"[codex:web] killed tmux session {web_session}."


def test_named_session_can_be_renamed_without_restarting_tmux(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)

    plugin.handle_pre_gateway_dispatch(event=_event("/codex init api"), gateway=_gateway())
    session_name = str(fake_tmux.started[-1]["session_name"])

    plugin.handle_pre_gateway_dispatch(
        event=_event("/codex rename backend"),
        gateway=_gateway(),
    )

    assert len(fake_tmux.started) == 1
    assert replies[-1] == "[codex:backend] renamed api -> backend."

    plugin.handle_pre_gateway_dispatch(event=_event("/codex list"), gateway=_gateway())
    assert "* backend (tmux)" in replies[-1]
    assert " api (tmux)" not in replies[-1]

    plugin.handle_pre_gateway_dispatch(event=_event("hello backend"), gateway=_gateway())
    assert fake_tmux.inputs[-1] == (session_name, "hello backend")

    plugin.handle_pre_gateway_dispatch(
        event=_event("/codex kill backend"),
        gateway=_gateway(),
    )
    assert fake_tmux.stopped[-1] == session_name


def test_named_session_rename_rejects_live_name_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)

    plugin.handle_pre_gateway_dispatch(event=_event("/codex init api"), gateway=_gateway())
    api_session = str(fake_tmux.started[-1]["session_name"])
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init web"), gateway=_gateway())

    plugin.handle_pre_gateway_dispatch(event=_event("/codex select api"), gateway=_gateway())
    plugin.handle_pre_gateway_dispatch(event=_event("/codex rename web"), gateway=_gateway())

    assert replies[-1] == "[codex] session named 'web' already exists."
    plugin.handle_pre_gateway_dispatch(event=_event("still api"), gateway=_gateway())
    assert fake_tmux.inputs[-1] == (api_session, "still api")


def test_active_bridge_routes_plain_message_to_tmux(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    event = _event("/codex init")
    plugin.handle_pre_gateway_dispatch(event=event, gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("fix the tests"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == [(session_name, "fix the tests")]
    assert fake_tmux.submit_keys == [(session_name, ["Escape", "Enter"])]


def test_exit_detaches_current_session_without_stopping_tmux(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex exit"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.stopped == []
    assert replies[-1] == "[codex:default] exited bridge; session is still running."

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("this should go to Hermes"),
        gateway=_gateway(),
    )
    assert result is None
    assert fake_tmux.inputs == []

    plugin.handle_pre_gateway_dispatch(event=_event("/codex select default"), gateway=_gateway())
    plugin.handle_pre_gateway_dispatch(event=_event("back to codex"), gateway=_gateway())
    assert fake_tmux.inputs == [(session_name, "back to codex")]


def test_select_none_detaches_single_session_without_stopping_tmux(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())

    plugin.handle_pre_gateway_dispatch(event=_event("/codex select none"), gateway=_gateway())
    result = plugin.handle_pre_gateway_dispatch(
        event=_event("this should also go to Hermes"),
        gateway=_gateway(),
    )

    assert result is None
    assert fake_tmux.inputs == []
    assert fake_tmux.stopped == []


def test_claude_exit_detaches_current_session_without_stopping_tmux(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.capture_text = CLAUDE_READY_PANE
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=_gateway())

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/claude exit"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.stopped == []
    assert replies[-1] == "[claude:default] exited bridge; session is still running."


def test_active_bridge_routes_media_only_message_to_tmux(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])
    event = _event("")
    event.media_urls = ["/tmp/hermes-image.jpg"]
    event.media_types = ["image/jpeg"]

    result = plugin.handle_pre_gateway_dispatch(
        event=event,
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == [
        (session_name, "User attached file(s):\n- image/jpeg: /tmp/hermes-image.jpg")
    ]


def test_active_bridge_routes_caption_with_media_to_tmux(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])
    event = _event("look at this")
    event.media_urls = ["/tmp/hermes-report.pdf"]
    event.media_types = ["application/pdf"]

    result = plugin.handle_pre_gateway_dispatch(
        event=event,
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == [
        (
            session_name,
            "look at this\n\nUser attached file(s):\n- application/pdf: /tmp/hermes-report.pdf",
        )
    ]


def test_tmux_backend_transcribes_voice_memo_before_send(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.capture_text = CLAUDE_READY_PANE
    replies: list[str] = []

    class Gateway:
        adapters = {}

        def _is_user_authorized(self, _source) -> bool:
            return True

        async def _enrich_message_with_transcription(self, text, audio_paths):
            assert text == "caption"
            assert audio_paths == ["/tmp/hermes-voice.ogg"]
            return '"hello from voice"\n\ncaption', ["hello from voice"]

    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=Gateway())
    session_name = str(fake_tmux.started[0]["session_name"])
    event = _event("caption")
    event.message_type = MessageType.VOICE
    event.media_urls = ["/tmp/hermes-voice.ogg"]
    event.media_types = ["audio/ogg"]

    result = plugin.handle_pre_gateway_dispatch(event=event, gateway=Gateway())

    deadline = time.time() + 2
    while time.time() < deadline and not fake_tmux.inputs:
        time.sleep(0.01)

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == [(session_name, '"hello from voice"\n\ncaption')]
    assert fake_tmux.submit_keys[-1] == (session_name, ["Enter"])


def test_tmux_text_after_voice_memo_keeps_arrival_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.capture_text = CLAUDE_READY_PANE
    replies: list[str] = []

    class Gateway:
        adapters = {}

        def _is_user_authorized(self, _source) -> bool:
            return True

        async def _enrich_message_with_transcription(self, text, audio_paths):
            await asyncio.sleep(0.2)
            return '"voice first"', ["voice first"]

    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=Gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    voice_event = _event("")
    voice_event.message_type = MessageType.VOICE
    voice_event.media_urls = ["/tmp/hermes-voice.ogg"]
    voice_event.media_types = ["audio/ogg"]
    plugin.handle_pre_gateway_dispatch(event=voice_event, gateway=Gateway())
    plugin.handle_pre_gateway_dispatch(event=_event("text second"), gateway=Gateway())

    deadline = time.time() + 3
    while time.time() < deadline and len(fake_tmux.inputs) < 2:
        time.sleep(0.01)

    assert fake_tmux.inputs == [
        (session_name, '"voice first"'),
        (session_name, "text second"),
    ]


def test_tmux_send_deferred_while_approval_pending(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.capture_text = CLAUDE_READY_PANE
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=_gateway())
    session = next(iter(plugin._sessions.values()))
    session.approval_signature = "pending-dialog"

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("do not type into the dialog"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    time.sleep(0.1)
    assert fake_tmux.inputs == []

    session.approval_signature = None
    deadline = time.time() + 3
    while time.time() < deadline and not fake_tmux.inputs:
        time.sleep(0.01)
    assert fake_tmux.inputs == [
        (str(fake_tmux.started[0]["session_name"]), "do not type into the dialog")
    ]


def test_exec_lock_released_when_worker_thread_fails_to_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_BACKEND", "exec")
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session = next(iter(plugin._sessions.values()))

    def _boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr("hermes_cli_bridge.bridge.threading.Thread.start", _boom)
    plugin.handle_pre_gateway_dispatch(event=_event("hello"), gateway=_gateway())
    monkeypatch.undo()

    assert session.exec_lock.acquire(blocking=False)
    session.exec_lock.release()


def test_exec_reply_discarded_after_kill(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    release = threading.Event()

    def _runner(args, **kwargs):
        release.wait(timeout=5)
        stdout = '{"type":"result","result":"GHOST","session_id":"sess-ghost"}'
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    plugin = _plugin(fake_tmux, replies, tmp_path, exec_runner=_runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CLAUDE_BACKEND", "exec")
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=_gateway())
    plugin.handle_pre_gateway_dispatch(event=_event("hello"), gateway=_gateway())

    plugin.handle_pre_gateway_dispatch(event=_event("/claude kill"), gateway=_gateway())
    release.set()

    time.sleep(0.3)
    assert not any("GHOST" in reply for reply in replies)


def test_active_bridge_sends_typing_indicator(tmp_path: Path, monkeypatch) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.typing: list[tuple[str, object]] = []

        def send_typing(self, chat_id, metadata=None) -> None:
            self.typing.append((chat_id, metadata))

    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    adapter = Adapter()
    gateway = SimpleNamespace(
        _is_user_authorized=lambda source: True,
        adapters={Platform.TELEGRAM: adapter},
    )

    plugin.handle_pre_gateway_dispatch(
        event=_event("fix the tests"),
        gateway=gateway,
    )

    assert adapter.typing == [("chat1", None)]


def test_unauthorized_active_bridge_input_falls_through(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("try to hijack"),
        gateway=_gateway(authorized=False),
    )

    assert result == {"action": "allow"}
    assert fake_tmux.inputs == []


def test_active_bridge_leaves_other_slash_commands_alone(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/help"),
        gateway=_gateway(),
    )

    assert result is None
    assert fake_tmux.inputs == []


def test_stale_bridge_no_longer_intercepts_plain_messages(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])
    fake_tmux.sessions.remove(session_name)

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("this should go to normal Hermes"),
        gateway=_gateway(),
    )

    assert result is None
    assert fake_tmux.inputs == []
    assert plugin.handle_pre_gateway_dispatch(
        event=_event("/codex status"),
        gateway=_gateway(),
    ) == {"action": "skip", "reason": "cli-bridge-control"}
    assert replies[-1] == "[codex] no bridge sessions for this chat."


def test_send_subcommand_can_forward_cli_slash_command(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex send /compact"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.inputs[-1] == (session_name, "/compact")
    assert fake_tmux.submit_keys[-1] == (session_name, ["Escape", "Enter"])
    assert replies[-1] == "[codex] sent."


def test_claude_init_starts_tmux_with_claude_command(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.capture_text = CLAUDE_READY_PANE
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/claude init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert len(fake_tmux.started) == 1
    assert fake_tmux.started[0]["cwd"] == tmp_path
    assert fake_tmux.started[0]["command"] == "claude"
    assert replies[-1].startswith("[claude:default] started in")


def test_claude_tmux_uses_plain_enter_submit(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.capture_text = CLAUDE_READY_PANE
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    plugin.handle_pre_gateway_dispatch(event=_event("hello"), gateway=_gateway())

    assert fake_tmux.inputs[-1] == (session_name, "hello")
    assert fake_tmux.submit_keys[-1] == (session_name, ["Enter"])


def test_claude_init_waits_for_claude_prompt(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    fake_tmux.captures = [
        "",
        CLAUDE_READY_PANE,
    ]
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermes_cli_bridge.bridge.time.sleep", lambda _seconds: None)

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/claude init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert len(fake_tmux.capture_calls) == 2
    assert replies[-1].startswith("[claude:default] started in")


def test_claude_tmux_ready_detection_rejects_foreign_panes(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    assert not plugin._tmux_snapshot_ready("claude", "")
    assert not plugin._tmux_snapshot_ready(
        "claude",
        "OpenAI Codex\nmodel: gpt-5.5\n› Explain this codebase",
    )
    assert not plugin._tmux_snapshot_ready("claude", CLAUDE_TRUST_DIALOG_PANE)
    assert plugin._tmux_snapshot_ready("claude", CLAUDE_READY_PANE)


def test_init_treats_startup_trust_dialog_as_ready(tmp_path: Path, monkeypatch) -> None:
    for agent, pane in (
        ("claude", CLAUDE_TRUST_DIALOG_PANE),
        ("codex", CODEX_TRUST_DIALOG_PANE),
    ):
        fake_tmux = FakeTmux()
        fake_tmux.captures = [pane]
        fake_tmux.capture_text = pane
        replies: list[str] = []
        plugin = _plugin(fake_tmux, replies, tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("hermes_cli_bridge.bridge.time.sleep", lambda _seconds: None)

        result = plugin.handle_pre_gateway_dispatch(
            event=_event(f"/{agent} init"),
            gateway=_gateway(),
        )

        assert result == {"action": "skip", "reason": "cli-bridge-control"}
        assert len(fake_tmux.capture_calls) == 1
        assert replies[-1].startswith(f"[{agent}:default] started in")


def test_codex_submit_keys_can_be_overridden(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_SUBMIT_KEYS", "C-m")
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    plugin.handle_pre_gateway_dispatch(event=_event("hello"), gateway=_gateway())

    assert fake_tmux.submit_keys[-1] == (session_name, ["C-m"])


def test_codex_tmux_ready_detection_rejects_startup_states(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    assert not plugin._tmux_snapshot_ready(
        "codex",
        "OpenAI Codex\nmodel: loading\n› Explain this codebase",
    )
    assert not plugin._tmux_snapshot_ready(
        "codex",
        "OpenAI Codex\n• Starting MCP servers (2/6)\n› Explain this codebase",
    )
    assert plugin._tmux_snapshot_ready(
        "codex",
        "OpenAI Codex\nmodel: gpt-5.5\n› Explain this codebase",
    )


def test_codex_init_uses_configured_command(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_CMD", "codex --model gpt-5.5")

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.started[0]["command"] == "codex --model gpt-5.5"


def test_codex_exec_backend_init_does_not_start_tmux(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_BACKEND", "exec")

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.started == []
    assert replies[-1].startswith("[codex:default] started in")
    assert "\nexec: hermes-codex-exec-" in replies[-1]


def test_codex_exec_backend_routes_subprocess_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []

    def _runner(args, **_kwargs):
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text("BRIDGE_OK", encoding="utf-8")
        stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"BRIDGE_OK"}}\n'
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    plugin = _plugin(fake_tmux, replies, tmp_path, exec_runner=_runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_BACKEND", "exec")
    monkeypatch.setenv(
        "HERMES_CLI_BRIDGE_CODEX_CMD",
        "codex --model gpt-5.5 --no-alt-screen -c check_for_update_on_startup=false",
    )
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("hello"),
        gateway=_gateway(),
    )

    deadline = time.time() + 1
    while time.time() < deadline and "[codex]\nBRIDGE_OK" not in replies:
        time.sleep(0.01)

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == []
    assert "[codex]\nBRIDGE_OK" in replies
    session = next(iter(plugin._sessions.values()))
    assert session.thread_id == "thread-1"


def test_codex_exec_backend_transcribes_voice_before_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    received_inputs: list[str] = []

    def _runner(args, **kwargs):
        received_inputs.append(kwargs["input"])
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text("VOICE_OK", encoding="utf-8")
        stdout = '{"type":"thread.started","thread_id":"thread-voice"}\n'
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    class Gateway:
        adapters = {}

        def _is_user_authorized(self, _source) -> bool:
            return True

        async def _enrich_message_with_transcription(self, text, audio_paths):
            assert text == "caption"
            assert audio_paths == ["/tmp/hermes-voice.ogg"]
            return '"hello from voice"\n\ncaption', ["hello from voice"]

    plugin = _plugin(fake_tmux, replies, tmp_path, exec_runner=_runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_BACKEND", "exec")
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=Gateway())
    event = _event("caption")
    event.message_type = MessageType.VOICE
    event.media_urls = ["/tmp/hermes-voice.ogg"]
    event.media_types = ["audio/ogg"]

    result = plugin.handle_pre_gateway_dispatch(event=event, gateway=Gateway())

    deadline = time.time() + 1
    while time.time() < deadline and not received_inputs:
        time.sleep(0.01)

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert received_inputs == ['"hello from voice"\n\ncaption']
    assert "[codex]\nVOICE_OK" in replies


def test_codex_exec_backend_keeps_uploaded_audio_as_attachment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    received_inputs: list[str] = []

    def _runner(args, **kwargs):
        received_inputs.append(kwargs["input"])
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text("AUDIO_OK", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    class Gateway:
        adapters = {}

        def _is_user_authorized(self, _source) -> bool:
            return True

        async def _enrich_message_with_transcription(self, _text, _audio_paths):
            raise AssertionError("uploaded audio files should not be auto-transcribed")

    plugin = _plugin(fake_tmux, replies, tmp_path, exec_runner=_runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_BACKEND", "exec")
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=Gateway())
    event = _event("")
    event.message_type = MessageType.AUDIO
    event.media_urls = ["/tmp/song.mp3"]
    event.media_types = ["audio/mpeg"]

    result = plugin.handle_pre_gateway_dispatch(event=event, gateway=Gateway())

    deadline = time.time() + 1
    while time.time() < deadline and "[codex]\nAUDIO_OK" not in replies:
        time.sleep(0.01)

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert received_inputs == ["User attached file(s):\n- audio/mpeg: /tmp/song.mp3"]
    assert "[codex]\nAUDIO_OK" in replies


def test_codex_exec_command_derives_from_tui_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "HERMES_CLI_BRIDGE_CODEX_CMD",
        "codex --model gpt-5.5 --no-alt-screen -c check_for_update_on_startup=false",
    )
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(thread_id=None)

    command = plugin._codex_exec_command(session, tmp_path / "last.txt")

    assert command == [
        "codex",
        "exec",
        "--model",
        "gpt-5.5",
        "-c",
        "check_for_update_on_startup=false",
        "--json",
        "--output-last-message",
        str(tmp_path / "last.txt"),
        "-",
    ]


def test_codex_exec_resume_command_uses_thread_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CODEX_EXEC_CMD", "codex exec --model gpt-5.5")
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(thread_id="thread-1")

    command = plugin._codex_exec_command(session, tmp_path / "last.txt")

    assert command == [
        "codex",
        "exec",
        "resume",
        "--model",
        "gpt-5.5",
        "--json",
        "--output-last-message",
        str(tmp_path / "last.txt"),
        "thread-1",
        "-",
    ]


def test_parse_codex_exec_stdout(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    thread_id, message = plugin._parse_codex_exec_stdout(
        '{"type":"thread.started","thread_id":"abc"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
    )

    assert thread_id == "abc"
    assert message == "done"


def test_codex_tmux_detects_edit_approval_prompt(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(agent="codex")

    approval = plugin._codex_approval_from_capture(
        session,
        """
• Added hermes-permission-e2e.txt (+1 -0)
    1 +ok

  Would you like to make the following edits?

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for these files (a)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
""",
    )

    assert approval is not None
    assert "Would you like to make the following edits?" in approval["preview"]
    assert approval["signature"]


def test_codex_tmux_approval_uses_existing_gateway_buttons(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Adapter:
        typed_command_prefix = "/"

        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def send_exec_approval(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(success=True)

    def _await_gateway_decision(session_key, notify_cb, approval_data, *, surface):
        notify_cb(approval_data)
        assert session_key == "telegram:chat1:u1"
        assert surface == "cli_bridge_tmux"
        return {"resolved": True, "choice": "once"}

    import tools.approval as approval_mod

    monkeypatch.setattr(approval_mod, "_await_gateway_decision", _await_gateway_decision)
    adapter = Adapter()
    gateway = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _session_key_for_source=lambda _source: "telegram:chat1:u1",
    )
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(
        key="fallback-key",
        cwd=tmp_path,
        session_name="hermes-codex-test",
    )

    choice = plugin._request_tmux_approval_decision(
        session,
        gateway,
        _event(""),
        "Would you like to make the following edits?",
    )

    assert choice == "once"
    assert adapter.requests[0]["chat_id"] == "chat1"
    assert adapter.requests[0]["session_key"] == "telegram:chat1:u1"
    assert "Would you like to make the following edits?" in adapter.requests[0]["command"]


def test_codex_tmux_approval_choice_maps_to_tmux_keys(tmp_path: Path) -> None:
    fake_tmux = FakeTmux()
    plugin = CliBridgePlugin(
        tmux=fake_tmux,  # type: ignore[arg-type]
        enable_output_reader=False,
        state_dir=tmp_path,
    )
    session = SimpleNamespace(session_name="hermes-codex-test")

    plugin._send_tmux_approval_choice(session, "once")
    plugin._send_tmux_approval_choice(
        session,
        "session",
        preview="2. Yes, and don't ask again for these files (a)",
    )
    plugin._send_tmux_approval_choice(
        session,
        "always",
        preview=(
            "2. Yes, and don't ask again for commands that start with "
            "`mkdir -p /tmp/x && printf ok > /tmp/x/file` (p)"
        ),
    )
    plugin._send_tmux_approval_choice(session, "deny")

    assert fake_tmux.keys == [
        ("hermes-codex-test", ["y"]),
        ("hermes-codex-test", ["a"]),
        ("hermes-codex-test", ["p"]),
        ("hermes-codex-test", ["Escape"]),
    ]


def test_end_stops_session(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/codex init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/codex end"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.stopped == [session_name]
    assert replies[-1] == f"[codex:default] killed tmux session {session_name}."


def test_tmux_start_truncates_reused_log_file(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def _runner(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hermes_cli_bridge.bridge.shutil.which", lambda name: "/opt/homebrew/bin/tmux")
    log_path = tmp_path / "session.log"
    log_path.write_text("stale output\n", encoding="utf-8")

    client = TmuxClient(runner=_runner)
    client.start(
        session_name="hermes-codex-test",
        cwd=tmp_path,
        command="codex",
        log_path=log_path,
    )

    assert log_path.read_text(encoding="utf-8") == ""
    assert calls[0][:4] == ["tmux", "new-session", "-d", "-s"]
    assert len(calls) == 1


def test_tmux_start_can_pipe_raw_log_file(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def _runner(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hermes_cli_bridge.bridge.shutil.which", lambda name: "/opt/homebrew/bin/tmux")
    log_path = tmp_path / "session.log"

    client = TmuxClient(runner=_runner)
    client.start(
        session_name="hermes-codex-test",
        cwd=tmp_path,
        command="codex",
        log_path=log_path,
        pipe_log=True,
    )

    assert calls[1][:4] == ["tmux", "pipe-pane", "-o", "-t"]


def test_tmux_send_input_submits_with_configured_keys() -> None:
    calls: list[list[str]] = []

    def _runner(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    client = TmuxClient(runner=_runner)

    client.send_input("hermes-codex-test", "hey", submit_keys=["Escape", "Enter"])

    assert calls == [
        ["tmux", "send-keys", "-t", "hermes-codex-test", "-l", "hey"],
        ["tmux", "send-keys", "-t", "hermes-codex-test", "Escape"],
        ["tmux", "send-keys", "-t", "hermes-codex-test", "Enter"],
    ]


def test_tmux_send_input_pastes_multiline_payload_once() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def _runner(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    client = TmuxClient(runner=_runner)

    client.send_input(
        "hermes-codex-test",
        "look\n- image/jpeg: /tmp/a.jpg",
        submit_keys=["Escape", "Enter"],
    )

    buffer_name = calls[0][0][3]
    assert buffer_name.startswith("hermes-codex-test-input-")
    assert calls == [
        (
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            "look\n- image/jpeg: /tmp/a.jpg",
        ),
        (
            ["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", "hermes-codex-test"],
            None,
        ),
        (["tmux", "send-keys", "-t", "hermes-codex-test", "Escape"], None),
        (["tmux", "send-keys", "-t", "hermes-codex-test", "Enter"], None),
    ]


def test_tmux_send_input_uses_unique_buffer_per_call() -> None:
    calls: list[list[str]] = []

    def _runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    client = TmuxClient(runner=_runner)
    client.send_input("hermes-codex-test", "a\nb")
    client.send_input("hermes-codex-test", "c\nd")

    buffer_names = [args[3] for args in calls if args[1] == "load-buffer"]
    assert len(buffer_names) == 2
    assert buffer_names[0] != buffer_names[1]


def test_clean_output_strips_osc_ansi_and_control_sequences(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    text = "\x1b]8;;https://example.test\x07link\x1b]8;;\x07 \x1b[31mred\x1b[0m \x00done"

    assert plugin._clean_output(text) == "link red done"


def test_clean_output_collapses_carriage_return_redraws(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    text = "Downloading 10%\rDownloading 50%\rDownloaded\nnext"

    assert plugin._clean_output(text) == "Downloaded\nnext"


def test_clean_output_caps_large_flushes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CLI_BRIDGE_MAX_OUTPUT_CHARS", "10")
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    cleaned = plugin._clean_output("x" * 50)

    assert cleaned == "[output truncated to last 10 chars]\nxxxxxxxxxx"


def test_snapshot_delta_returns_changed_rendered_lines(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    delta = plugin._snapshot_delta(
        "status: thinking\nanswer line",
        "status: done\nanswer line\nnew line",
    )

    assert delta == "status: done\nnew line"


def test_capture_filter_drops_codex_chrome_prompts_and_status(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    delta = """
╭─────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.142.5)                  │
│ model:     gpt-5.5 xhigh   /model to change │
• You have 3 usage limit resets available. Run /usage to use one.
› hey
• Working (4s • esc to interrupt)
• Hey. What do you want to work on?
gpt-5.5 xhigh · ~/workspace/hermes2/hermes-agent · hermes-agent · main
"""

    assert plugin._chat_output_from_capture_delta(delta) == "Hey. What do you want to work on?"


def test_capture_transcript_drops_user_prompt_echo(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    before = """
• Yes, I can see it.
› Find and fix a bug in @filename
gpt-5.5 xhigh · ~/workspace/hermes2/hermes-agent · hermes-agent · main
"""
    after = """
• Yes, I can see it.
› hey there
gpt-5.5 xhigh · ~/workspace/hermes2/hermes-agent · hermes-agent · main
"""

    before_transcript = plugin._assistant_transcript_from_capture(before)
    after_transcript = plugin._assistant_transcript_from_capture(after)

    assert before_transcript == "Yes, I can see it."
    assert after_transcript == "Yes, I can see it."
    assert plugin._transcript_delta(before_transcript, after_transcript) == ""


def test_capture_transcript_routes_new_assistant_block(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    before = """
• Yes, I can see it.
› hey there
"""
    after = """
• Yes, I can see it.
› hey there
• I can see your message now.
  What should I inspect first?
"""

    before_transcript = plugin._assistant_transcript_from_capture(before)
    after_transcript = plugin._assistant_transcript_from_capture(after)

    assert plugin._transcript_delta(before_transcript, after_transcript) == (
        "I can see your message now.\nWhat should I inspect first?"
    )


def test_capture_transcript_drops_status_only_updates(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    snapshot = """
• Starting MCP servers (4/6): betterstack (2s • esc to interrupt)
• Working (4s • esc to interrupt)
"""

    assert plugin._assistant_transcript_from_capture(snapshot) == ""


def test_capture_transcript_suppresses_repeated_working_timer_updates(
    tmp_path: Path,
) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    before = """
• Ran hermes config set browser.cloud_provider browserbase
  └─ ✔ Set browser.cloud_provider = browserbase in /home/alex/.hermes/config.yaml
• Working (49s • esc to interrupt)
"""
    after = """
• Ran hermes config set browser.cloud_provider browserbase
  └─ ✔ Set browser.cloud_provider = browserbase in /home/alex/.hermes/config.yaml
• Working (54s • esc to interrupt)
"""

    before_transcript = plugin._assistant_transcript_from_capture(before)
    after_transcript = plugin._assistant_transcript_from_capture(after)

    assert before_transcript == (
        "Ran hermes config set browser.cloud_provider browserbase\n"
        "└─ ✔ Set browser.cloud_provider = browserbase in /home/alex/.hermes/config.yaml"
    )
    assert after_transcript == before_transcript
    assert plugin._transcript_delta(before_transcript, after_transcript) == ""


def test_capture_status_filter_does_not_drop_normal_working_prose(
    tmp_path: Path,
) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    snapshot = """
• Working with Hermes slash commands requires explicit forwarding.
• Starting MCP servers is not necessary for this explanation.
• Thinking through the edge case is useful.
"""

    assert plugin._assistant_transcript_from_capture(snapshot) == (
        "Working with Hermes slash commands requires explicit forwarding.\n\n"
        "Starting MCP servers is not necessary for this explanation.\n\n"
        "Thinking through the edge case is useful."
    )


def test_transcript_delta_strips_leaked_codex_status_lines(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    previous = "Ran command\n└─ ✔ ok\n• Working (51s • esc to interrupt)"
    current = "Ran command\n└─ ✔ ok\n• Working (52s • esc to interrupt)"

    assert plugin._transcript_delta(previous, current) == ""


def test_log_snippet_caps_and_escapes_newlines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CLI_BRIDGE_LOG_SNIPPET_CHARS", "8")
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    assert plugin._log_snippet("abcdefghij\nnext") == "abcdefgh..."


def test_claude_transcript_extracts_assistant_reply(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    snapshot = """\
╭─── Claude Code v2.1.201 ──────────────────────────╮
│                 Welcome back Alex!                 │
╰────────────────────────────────────────────────────╯
❯ What is 2+2? Answer with only the number.

⏺ 4

✻ Cogitated for 2s

────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────
  freshdir  dur:5s  tok/s:8072.6  ctx:4%  model:Fable 5 (effort:xhigh)
  ← for agents"""

    assert plugin._assistant_transcript_from_capture(snapshot, agent="claude") == "4"


def test_claude_transcript_includes_tool_calls_and_results(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    snapshot = """\
❯ Run the shell command: node -e "console.log(1)" and tell me the output

⏺ Bash(node -e "console.log(1)")
  ⎿  1

⏺ The output is 1.

✻ Baked for 5s

────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────
  freshdir  model:Fable 5 (effort:xhigh)"""

    assert plugin._assistant_transcript_from_capture(snapshot, agent="claude") == (
        'Bash(node -e "console.log(1)")\n⎿  1\n\nThe output is 1.'
    )


def test_claude_transcript_ignores_spinner_and_ghost_text(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    snapshot = """\
❯ Write a 200 word essay about clouds.

✢ Garnishing… (2s · ↓ 65 tokens)

────────────────────────────────────────────────────
❯ cat hello.txt
────────────────────────────────────────────────────
  freshdir  model:Fable 5 (effort:xhigh)
                                      tmux focus-events off · add 'set -g focus-events on' to ~/.tmux.conf and reattach for focus tracking"""

    assert plugin._assistant_transcript_from_capture(snapshot, agent="claude") == ""


def test_claude_status_line_detection(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    for status in (
        "✻ Puzzling…",
        "✢ Ideating…",
        "✢ Garnishing… (2s · ↓ 65 tokens)",
        "✽ Ideating… (running stop hooks… 0/2 · 9s · ↓ 174 tokens)",
        "✻ Cogitated for 2s",
        "⎿  Waiting…",
    ):
        assert plugin._is_capture_status_line(status, agent="claude"), status

    for prose in (
        "⏺ Puzzling as it sounds, yes.",
        "⏺ Working with Hermes slash commands requires explicit forwarding.",
        "The essay took 2s to write.",
    ):
        assert not plugin._is_capture_status_line(prose, agent="claude"), prose


def test_claude_transcript_delta_suppresses_turn_summary_updates(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    before = "Bash(touch hello.txt)\n⎿  (No content)\n✻ Worked for 6s"
    after = "Bash(touch hello.txt)\n⎿  (No content)\n✻ Worked for 9s"

    assert plugin._transcript_delta(before, after, agent="claude") == ""


def test_claude_tmux_detects_permission_dialogs(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(agent="claude")

    write_approval = plugin._approval_from_capture(session, CLAUDE_WRITE_DIALOG_PANE)
    assert write_approval is not None
    assert "Do you want to create hello.txt?" in write_approval["preview"]
    assert write_approval["signature"]

    bash_approval = plugin._approval_from_capture(session, CLAUDE_BASH_DIALOG_PANE)
    assert bash_approval is not None
    assert "Do you want to proceed?" in bash_approval["preview"]

    trust_approval = plugin._approval_from_capture(session, CLAUDE_TRUST_DIALOG_PANE)
    assert trust_approval is not None
    assert "Yes, I trust this folder" in trust_approval["preview"]


def test_claude_prose_question_is_not_an_approval(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(agent="claude")

    snapshot = """\
⏺ Do you want to proceed with plan A or plan B?
  Reply with 1. yes to plan A or 2. yes to plan B.

────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────"""

    assert plugin._approval_from_capture(session, snapshot) is None


def test_claude_tmux_approval_choice_maps_to_number_keys(tmp_path: Path) -> None:
    fake_tmux = FakeTmux()
    plugin = CliBridgePlugin(
        tmux=fake_tmux,  # type: ignore[arg-type]
        enable_output_reader=False,
        state_dir=tmp_path,
    )
    session = SimpleNamespace(agent="claude", session_name="hermes-claude-test")

    plugin._send_tmux_approval_choice(session, "once")
    plugin._send_tmux_approval_choice(
        session,
        "session",
        preview="2. Yes, allow all edits during this session (shift+tab)",
    )
    plugin._send_tmux_approval_choice(
        session,
        "always",
        preview="2. Yes, and don’t ask again for: node *",
    )
    plugin._send_tmux_approval_choice(
        session,
        "session",
        preview="❯ 1. Yes, I trust this folder\n  2. No, exit",
    )
    plugin._send_tmux_approval_choice(session, "deny")

    assert fake_tmux.keys == [
        ("hermes-claude-test", ["1"]),
        ("hermes-claude-test", ["2"]),
        ("hermes-claude-test", ["2"]),
        ("hermes-claude-test", ["1"]),
        ("hermes-claude-test", ["Escape"]),
    ]


def test_codex_trust_prompt_choice_maps_to_enter_and_escape(tmp_path: Path) -> None:
    fake_tmux = FakeTmux()
    plugin = CliBridgePlugin(
        tmux=fake_tmux,  # type: ignore[arg-type]
        enable_output_reader=False,
        state_dir=tmp_path,
    )
    session = SimpleNamespace(agent="codex", session_name="hermes-codex-test")

    plugin._send_tmux_approval_choice(session, "once", preview=CODEX_TRUST_DIALOG_PANE)
    plugin._send_tmux_approval_choice(session, "deny", preview=CODEX_TRUST_DIALOG_PANE)

    assert fake_tmux.keys == [
        ("hermes-codex-test", ["Enter"]),
        ("hermes-codex-test", ["Escape"]),
    ]


def test_claude_tmux_approval_uses_claude_labels(tmp_path: Path, monkeypatch) -> None:
    class Adapter:
        typed_command_prefix = "/"

        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def send_exec_approval(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(success=True)

    def _await_gateway_decision(session_key, notify_cb, approval_data, *, surface):
        notify_cb(approval_data)
        assert approval_data["pattern_key"] == "claude tmux approval"
        assert surface == "cli_bridge_tmux"
        return {"resolved": True, "choice": "once"}

    import tools.approval as approval_mod

    monkeypatch.setattr(approval_mod, "_await_gateway_decision", _await_gateway_decision)
    adapter = Adapter()
    gateway = SimpleNamespace(
        adapters={Platform.TELEGRAM: adapter},
        _session_key_for_source=lambda _source: "telegram:chat1:u1",
    )
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(
        agent="claude",
        key="fallback-key",
        cwd=tmp_path,
        session_name="hermes-claude-test",
    )

    choice = plugin._request_tmux_approval_decision(
        session,
        gateway,
        _event(""),
        "Do you want to create hello.txt?",
    )

    assert choice == "once"
    assert adapter.requests[0]["command"].startswith("Claude tmux approval in")
    assert "Do you want to create hello.txt?" in adapter.requests[0]["command"]
    assert adapter.requests[0]["description"] == (
        "Claude is waiting for permission in the tmux bridge."
    )


def test_claude_exec_backend_init_does_not_start_tmux(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CLAUDE_BACKEND", "exec")

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("/claude init"),
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-control"}
    assert fake_tmux.started == []
    assert replies[-1].startswith("[claude:default] started in")
    assert "\nexec: hermes-claude-exec-" in replies[-1]


def test_claude_exec_backend_routes_subprocess_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    commands: list[list[str]] = []

    def _runner(args, **kwargs):
        commands.append(list(args))
        stdout = (
            '{"type":"result","subtype":"success","is_error":false,'
            '"result":"CLAUDE_OK","session_id":"sess-1","total_cost_usd":0.01}'
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    plugin = _plugin(fake_tmux, replies, tmp_path, exec_runner=_runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CLAUDE_BACKEND", "exec")
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=_gateway())

    result = plugin.handle_pre_gateway_dispatch(
        event=_event("hello"),
        gateway=_gateway(),
    )

    deadline = time.time() + 1
    while time.time() < deadline and "[claude]\nCLAUDE_OK" not in replies:
        time.sleep(0.01)

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == []
    assert "[claude]\nCLAUDE_OK" in replies
    assert commands[0] == ["claude", "--print", "--output-format", "json"]
    session = next(iter(plugin._sessions.values()))
    assert session.thread_id == "sess-1"

    plugin.handle_pre_gateway_dispatch(event=_event("again"), gateway=_gateway())
    deadline = time.time() + 1
    while time.time() < deadline and len(commands) < 2:
        time.sleep(0.01)

    assert commands[1] == [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--resume",
        "sess-1",
    ]


def test_claude_exec_command_derives_from_tui_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "HERMES_CLI_BRIDGE_CLAUDE_CMD",
        "claude --model claude-opus-4-8 -p",
    )
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)
    session = SimpleNamespace(thread_id=None)

    command = plugin._claude_exec_command(session, tmp_path / "last.txt")

    assert command == [
        "claude",
        "--model",
        "claude-opus-4-8",
        "--print",
        "--output-format",
        "json",
    ]


def test_parse_claude_exec_stdout(tmp_path: Path) -> None:
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    session_id, message = plugin._parse_claude_exec_stdout(
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"done","session_id":"abc"}'
    )
    assert session_id == "abc"
    assert message == "done"

    session_id, message = plugin._parse_claude_exec_stdout(
        'npm warn something\n{"type":"result","result":"ok","session_id":"xyz"}\n'
    )
    assert session_id == "xyz"
    assert message == "ok"


def test_claude_exec_backend_transcribes_voice_before_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    received_inputs: list[str] = []

    def _runner(args, **kwargs):
        received_inputs.append(kwargs["input"])
        stdout = '{"type":"result","result":"VOICE_OK","session_id":"sess-voice"}'
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    class Gateway:
        adapters = {}

        def _is_user_authorized(self, _source) -> bool:
            return True

        async def _enrich_message_with_transcription(self, text, audio_paths):
            assert text == "caption"
            assert audio_paths == ["/tmp/hermes-voice.ogg"]
            return '"hello from voice"\n\ncaption', ["hello from voice"]

    plugin = _plugin(fake_tmux, replies, tmp_path, exec_runner=_runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_CLI_BRIDGE_CLAUDE_BACKEND", "exec")
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=Gateway())
    event = _event("caption")
    event.message_type = MessageType.VOICE
    event.media_urls = ["/tmp/hermes-voice.ogg"]
    event.media_types = ["audio/ogg"]

    result = plugin.handle_pre_gateway_dispatch(event=event, gateway=Gateway())

    deadline = time.time() + 1
    while time.time() < deadline and not received_inputs:
        time.sleep(0.01)

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert received_inputs == ['"hello from voice"\n\ncaption']
    assert "[claude]\nVOICE_OK" in replies
