from __future__ import annotations

import sys
import subprocess
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


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.started: list[dict[str, object]] = []
        self.inputs: list[tuple[str, str]] = []
        self.submit_keys: list[tuple[str, list[str]]] = []
        self.keys: list[tuple[str, list[str]]] = []
        self.stopped: list[str] = []

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

    def stop(self, session_name: str) -> None:
        self.sessions.discard(session_name)
        self.stopped.append(session_name)


def _event(text: str, user_id: str = "u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat1",
            chat_type="dm",
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
    assert replies[-1].startswith("[codex] started in")


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
    assert replies[-1].startswith("[codex] attached to existing session in")


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
    event.media_urls = ["/tmp/hermes-voice.ogg"]
    event.media_types = ["audio/ogg"]

    result = plugin.handle_pre_gateway_dispatch(
        event=event,
        gateway=_gateway(),
    )

    assert result == {"action": "skip", "reason": "cli-bridge-input"}
    assert fake_tmux.inputs == [
        (
            session_name,
            "look at this\n\nUser attached file(s):\n- audio/ogg: /tmp/hermes-voice.ogg",
        )
    ]


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
    assert replies[-1] == "[codex] no active bridge for this chat."


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
    assert replies[-1].startswith("[claude] started in")


def test_claude_tmux_uses_plain_enter_submit(tmp_path: Path, monkeypatch) -> None:
    fake_tmux = FakeTmux()
    replies: list[str] = []
    plugin = _plugin(fake_tmux, replies, tmp_path)
    monkeypatch.chdir(tmp_path)
    plugin.handle_pre_gateway_dispatch(event=_event("/claude init"), gateway=_gateway())
    session_name = str(fake_tmux.started[0]["session_name"])

    plugin.handle_pre_gateway_dispatch(event=_event("hello"), gateway=_gateway())

    assert fake_tmux.inputs[-1] == (session_name, "hello")
    assert fake_tmux.submit_keys[-1] == (session_name, ["Enter"])


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
    assert replies[-1].startswith("[codex] started in")
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
    while time.time() < deadline and not received_inputs:
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
    plugin._send_tmux_approval_choice(session, "session")
    plugin._send_tmux_approval_choice(session, "always")
    plugin._send_tmux_approval_choice(session, "deny")

    assert fake_tmux.keys == [
        ("hermes-codex-test", ["y"]),
        ("hermes-codex-test", ["a"]),
        ("hermes-codex-test", ["a"]),
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
    assert replies[-1] == f"[codex] ended tmux session {session_name}."


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

    assert calls == [
        (
            ["tmux", "load-buffer", "-b", "hermes-codex-test-input", "-"],
            "look\n- image/jpeg: /tmp/a.jpg",
        ),
        (
            ["tmux", "paste-buffer", "-d", "-b", "hermes-codex-test-input", "-t", "hermes-codex-test"],
            None,
        ),
        (["tmux", "send-keys", "-t", "hermes-codex-test", "Escape"], None),
        (["tmux", "send-keys", "-t", "hermes-codex-test", "Enter"], None),
    ]


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


def test_log_snippet_caps_and_escapes_newlines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CLI_BRIDGE_LOG_SNIPPET_CHARS", "8")
    plugin = CliBridgePlugin(enable_output_reader=False, state_dir=tmp_path)

    assert plugin._log_snippet("abcdefghij\nnext") == "abcdefgh..."
