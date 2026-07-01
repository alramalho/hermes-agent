"""Hermes plugin entry point for tmux-backed CLI bridges."""

from __future__ import annotations

from .bridge import CliBridgePlugin

_PLUGIN: CliBridgePlugin | None = None


def register(ctx) -> None:
    """Register Hermes slash commands and gateway interception hook."""
    global _PLUGIN
    _PLUGIN = CliBridgePlugin()
    _PLUGIN.register(ctx)


__all__ = ["CliBridgePlugin", "register"]
