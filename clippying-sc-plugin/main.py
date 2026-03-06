import gi
from loguru import logger as log

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

import os
import sys

sys.path.append(os.path.dirname(__file__))

import globals as gl

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

from src.Signals.Signals import AppQuit

from actions import (
    ClippyingClipButtonAction,
    ClippyingWsClient,
    start_host_manager,
    stop_daemon_best_effort,
    stop_host_manager,
)


class Clippying(PluginBase):
    def __init__(self):
        super().__init__()

        self.has_plugin_settings = True

        try:
            gl.signal_manager.connect_signal(AppQuit, self._on_app_quit)
        except Exception as e:
            log.error(f"Failed to hook AppQuit: {e}")

        self.lm = self.locale_manager

        self.clip_button_holder = ActionHolder(
            plugin_base=self,
            action_base=ClippyingClipButtonAction,
            action_id_suffix="ClipButton",
            action_name=self.lm.get("actions.clip_button.name"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNSUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
            },
        )
        self.add_action_holder(self.clip_button_holder)

        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://localhost/",
            plugin_version="0.1.0",
            app_version="1.5.0-beta.12",
        )

    def on_enable(self):
        start_host_manager()

    def on_disable(self):
        stop_host_manager()
        if self._stop_daemon_on_quit_enabled():
            stop_daemon_best_effort()

    def _stop_daemon_on_quit_enabled(self) -> bool:
        try:
            settings = self.get_settings() or {}
        except Exception:
            settings = {}
        return bool(settings.get("stop_daemon_on_quit", True))

    def _set_stop_daemon_on_quit_enabled(self, enabled: bool) -> None:
        try:
            settings = self.get_settings() or {}
        except Exception:
            settings = {}
        settings["stop_daemon_on_quit"] = bool(enabled)
        self.set_settings(settings)

    def _on_app_quit(self, *_args, **_kwargs) -> None:
        stop_host_manager()
        if self._stop_daemon_on_quit_enabled():
            stop_daemon_best_effort()

    def _preview_sink(self) -> str:
        try:
            settings = self.get_settings() or {}
        except Exception:
            settings = {}
        return (settings.get("preview_sink") or "").strip()

    def _set_preview_sink(self, sink: str) -> None:
        try:
            settings = self.get_settings() or {}
        except Exception:
            settings = {}
        settings["preview_sink"] = (sink or "").strip()
        self.set_settings(settings)

    def get_settings_area(self):
        group = Adw.PreferencesGroup(title="General")

        row = Adw.ActionRow(title="Stop daemon on StreamController quit")
        row.set_subtitle("Stops the clippying background daemon when StreamController exits")

        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        sw.set_active(self._stop_daemon_on_quit_enabled())

        def on_toggled(*_a):
            self._set_stop_daemon_on_quit_enabled(sw.get_active())

        sw.connect("notify::active", on_toggled)
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        group.add(row)

        sinks: list[dict[str, str]] = []
        try:
            sinks = ClippyingWsClient("ws://127.0.0.1:17373").sinks()
        except Exception:
            sinks = []

        sink_model = Gtk.StringList()
        display_to_sink: list[str] = []

        sink_model.append("Default")
        display_to_sink.append("")

        for entry in sinks:
            name = (entry.get("name") or "").strip()
            desc = (entry.get("description") or "").strip()
            if not name:
                continue
            label = f"{name} - {desc}" if desc else name
            sink_model.append(label)
            display_to_sink.append(name)

        # Show full text without truncation
        def setup_label(factory, list_item):
            label = Gtk.Label(xalign=0, wrap=True, wrap_mode=2)  # WORD_CHAR
            list_item.set_child(label)

        def bind_label(factory, list_item):
            label = list_item.get_child()
            item = list_item.get_item()
            label.set_text(item.get_string())

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", setup_label)
        factory.connect("bind", bind_label)

        preview_sink_row = Adw.ComboRow(model=sink_model, title="Preview output")
        preview_sink_row.set_factory(factory)
        preview_sink_row.set_list_factory(factory)
        preview_sink_row.set_use_subtitle(True)
        preview_sink_row.set_subtitle("Device used by the trimmer preview")

        current_sink = self._preview_sink()
        if current_sink in display_to_sink:
            preview_sink_row.set_selected(display_to_sink.index(current_sink))
        else:
            preview_sink_row.set_selected(0)
            self._set_preview_sink("")

        def on_preview_sink_selected(*_args):
            idx = preview_sink_row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(display_to_sink):
                self._set_preview_sink(display_to_sink[idx])

        preview_sink_row.connect("notify::selected", on_preview_sink_selected)
        group.add(preview_sink_row)

        return group

    def get_selector_icon(self) -> Gtk.Widget:
        return Gtk.Image(icon_name="audio-x-generic-symbolic")
