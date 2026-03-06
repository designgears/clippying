import json
import os
import shutil
import subprocess
import threading
import time
import weakref
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Adw, GLib

from loguru import logger as log

import globals as gl

from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.windows.Settings.PluginSettingsWindow.PluginSettingsWindow import PluginSettingsWindow


_ACTIVE_ACTIONS: "weakref.WeakSet[ClippyingClipButtonAction]" = weakref.WeakSet()
_ACTIVE_ACTIONS_LOCK = threading.Lock()


def _run_clippying(exe: str, args: list[str]) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if p.returncode == 0:
            return True, (p.stdout or "").strip()
        return False, ((p.stderr or p.stdout) or "").strip()
    except FileNotFoundError:
        return False, f"binary not found: {exe}"
    except Exception as e:
        return False, str(e)


def _ws_is_up(url: str) -> bool:
    try:
        import websocket  # type: ignore
    except Exception:
        return False

    try:
        ws = websocket.create_connection(url, timeout=1)
        try:
            ws.close()
        except Exception:
            pass
        return True
    except Exception:
        return False


_DAEMON_STOP_LOCK = threading.Lock()
_DAEMON_STOP_REQUESTED = False


def stop_daemon_best_effort(exe: str | None = None) -> None:
    global _DAEMON_STOP_REQUESTED
    with _DAEMON_STOP_LOCK:
        if _DAEMON_STOP_REQUESTED:
            return
        _DAEMON_STOP_REQUESTED = True

    try:
        if not exe:
            with _ACTIVE_ACTIONS_LOCK:
                actions = list(_ACTIVE_ACTIONS)

            for a in actions:
                try:
                    exe = a._clippying_exe()
                except Exception:
                    exe = None
                if exe:
                    break

        exe = (exe or "clippying").strip()
        ok, msg = _run_clippying(exe, ["stop"])
        if not ok:
            log.error(f"failed to stop daemon: {msg}")
    except Exception as e:
        log.error(f"failed to stop daemon: {e}")


class AudioPlayer:
    """Manages audio playback with stop capability."""
    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def is_playing(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            return self._process.poll() is None

    def stop(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None

    def play(self, path: str, sink: str | None = None) -> bool:
        if not path or not os.path.exists(path):
            return False

        paplay = shutil.which("paplay")
        aplay = shutil.which("aplay")
        player = paplay or aplay
        if not player:
            return False

        self.stop()

        with self._lock:
            try:
                args: list[str] = [player]

                # Prefer paplay if a sink is specified (Pulse/PipeWire routing)
                if sink and paplay:
                    args.extend(["-d", sink])

                args.append(path)
                self._process = subprocess.Popen(args)
                return True
            except Exception:
                return False


_audio_player = AudioPlayer()


class ClippyingWsClient:
    def __init__(self, url: str):
        self.url = url

    def _connect(self):
        try:
            import websocket  # type: ignore
        except Exception as e:
            raise RuntimeError("python package 'websocket-client' is required") from e

        return websocket.create_connection(self.url, timeout=2)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        ws = self._connect()
        try:
            ws.send(json.dumps(payload))
            raw = ws.recv()
            if not raw:
                return {"type": "error", "message": "empty response"}
            return json.loads(raw)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def sources(self) -> list[dict[str, str]]:
        resp = self.request({"cmd": "sources"})
        if resp.get("type") == "sources":
            return list(resp.get("sources", []))
        return []

    def sinks(self) -> list[dict[str, str]]:
        resp = self.request({"cmd": "sinks"})
        if resp.get("type") == "sinks":
            return list(resp.get("sinks", []))
        return []


class ClippyingEventListener:
    def __init__(self, url: str, on_event: Callable[[dict[str, Any]], None]):
        self.url = url
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            import websocket  # type: ignore
        except Exception:
            return

        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(self.url, timeout=2)
                try:
                    ws.sock.settimeout(1.0)
                except Exception:
                    pass

                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception:
                        break

                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue

                    try:
                        self.on_event(data)
                    except Exception:
                        pass
            except Exception:
                time.sleep(0.5)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass


class ClippyingClipButtonAction(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self.settings: dict[str, Any] = {}
        self._listener: ClippyingEventListener | None = None
        self._waiting_for_source: str | None = None
        self._waiting_event = threading.Event()
        self._waiting_clip: dict[str, Any] | None = None
        self._clip_lock = threading.Lock()

        self._press_started_at: float | None = None
        self._long_press_seconds: float = 0.6

        # If the user performed a hold, StreamController will also emit an UP on release.
        # We suppress that UP so it doesn't also play the clip.
        self._suppress_next_up: bool = False
        self._player = _audio_player

    def on_ready(self):
        self.settings = self.get_settings() or {}
        with _ACTIVE_ACTIONS_LOCK:
            _ACTIVE_ACTIONS.add(self)
        self._ensure_listener()
        self._ensure_monitoring()
        self._refresh_labels_from_settings()

    def on_remove(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        with _ACTIVE_ACTIONS_LOCK:
            try:
                _ACTIVE_ACTIONS.remove(self)
            except KeyError:
                pass
        self._ensure_monitoring()

    def event_callback(self, event, data=None):
        event_str = str(event)
        log.error(f"ClippyingClipButtonAction event_callback: event={event_str} data={data}")

        if event_str == "Key Short Up":
            mode = self.settings.get("playback_mode", "play-stop")
            if self._player.is_playing():
                if mode == "play-stop":
                    log.error("ClippyingClipButtonAction -> STOP PLAYBACK")
                    self._player.stop()
                elif mode == "play-restart":
                    log.error("ClippyingClipButtonAction -> RESTART PLAYBACK")
                    self._play_last_clip()
                elif mode == "play-overlap":
                    log.error("ClippyingClipButtonAction -> OVERLAP PLAYBACK")
                    self._play_last_clip_overlap()
            else:
                log.error("ClippyingClipButtonAction -> PLAY CLIP")
                self._play_last_clip()
        elif event_str == "Key Hold Start":
            log.error("ClippyingClipButtonAction -> TRIGGER CLIPPING")
            self._trigger_clip()


    def _ws_url(self) -> str:
        return (self.settings.get("ws_url") or "ws://127.0.0.1:17373").strip() or "ws://127.0.0.1:17373"

    def _clippying_exe(self) -> str:
        return (
            self.settings.get("clippying_exe")
            or "clippying"
        ).strip() or "clippying"

    def _ws(self) -> ClippyingWsClient:
        return ClippyingWsClient(self._ws_url())

    def _error_row(self, title: str, subtitle: str) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.add_css_class("warning")
        return row

    def _entry_row(self, title: str, key: str, default: str) -> Adw.EntryRow:
        row = Adw.EntryRow()
        row.set_title(title)
        row.set_text((self.settings.get(key) or default).strip())

        def on_changed(*_args):
            self.settings[key] = row.get_text()
            self.set_settings(self.settings)

        row.connect("notify::text", on_changed)
        return row

    def _selected_source(self) -> str | None:
        s = (self.settings.get("source") or "").strip()
        return s or None

    def _selected_sink(self) -> str | None:
        s = (self.settings.get("playback_sink") or "").strip()
        return s or None

    def _ensure_daemon_running(self) -> bool:
        url = self._ws_url()
        if _ws_is_up(url):
            return True

        ok, msg = _run_clippying(self._clippying_exe(), ["start"])
        if not ok:
            log.error(f"failed to start daemon: {msg}")
            return False

        for _ in range(30):
            if _ws_is_up(url):
                return True
            time.sleep(0.1)
        return False

    def _ensure_listener(self):
        if self._listener is not None:
            return

        def on_event(data: dict[str, Any]):
            if data.get("type") != "clip_saved":
                return
            source = data.get("source")
            if not source:
                return

            with self._clip_lock:
                if self._waiting_for_source and source == self._waiting_for_source:
                    self._waiting_clip = data
                    self._waiting_event.set()

        self._listener = ClippyingEventListener(self._ws_url(), on_event)
        self._listener.start()

    def _ensure_monitoring(self):
        if not self._ensure_daemon_running():
            return

        with _ACTIVE_ACTIONS_LOCK:
            desired_sources = {a._selected_source() for a in list(_ACTIVE_ACTIONS)}

        desired_sources.discard(None)

        try:
            if not desired_sources:
                self._ws().request({"cmd": "stop_all"})
                return

            resp = self._ws().request({"cmd": "status"})
            statuses = list(resp.get("statuses", [])) if resp.get("type") == "status" else []
            current_sources = {s.get("source") for s in statuses if isinstance(s, dict)}
            current_sources.discard(None)

            for src in sorted(current_sources - desired_sources):
                self._ws().request({"cmd": "stop", "source": src})

            for src in sorted(desired_sources - current_sources):
                self._ws().request({"cmd": "monitor", "source": src})
        except Exception as e:
            log.error(f"monitor sync failed: {e}")

    def _refresh_labels_from_settings(self):
        source = self._selected_source() or "No source"
        clip_path = (self.settings.get("last_clip_path") or "").strip()

        def apply():
            if hasattr(self, "set_top_label"):
                try:
                    self.set_top_label(source[:30], font_size=14)
                except Exception:
                    pass
            if hasattr(self, "set_bottom_label"):
                try:
                    self.set_bottom_label(os.path.basename(clip_path)[:30] if clip_path else "", font_size=12)
                except Exception:
                    pass

        GLib.idle_add(apply)

    def get_config_rows(self) -> list:
        self.settings = self.get_settings() or {}

        rows: list = []

        open_settings_row = Adw.ActionRow(title="Global settings")
        open_settings_row.set_subtitle("Open Clippying plugin settings")
        open_settings_button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)

        def on_open_settings_clicked(*_args):
            try:
                settings_window = PluginSettingsWindow(self.plugin_base)
                settings_window.present(gl.app.get_active_window())
            except Exception as e:
                log.error(f"Failed to open plugin settings: {e}")

        open_settings_button.connect("clicked", on_open_settings_clicked)
        open_settings_row.add_suffix(open_settings_button)
        open_settings_row.set_activatable_widget(open_settings_button)
        rows.append(open_settings_row)

        if not self._ensure_daemon_running():
            rows.append(self._error_row("Daemon not reachable", "Unable to connect or start the daemon."))
            return rows

        sources: list[dict[str, str]] = []
        try:
            sources = self._ws().sources()
        except Exception as e:
            rows.append(self._error_row("Daemon not reachable", str(e)))
            return rows

        if not sources:
            rows.append(self._error_row("No sources", "Daemon returned no sources."))
            return rows

        model = Gtk.StringList()
        display_to_source: list[str] = []
        for entry in sources:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            label = f"{name}"
            model.append(label)
            display_to_source.append(name)

        # Custom factory to show full text without truncation
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

        source_row = Adw.ComboRow(model=model, title="Monitor source")
        source_row.set_factory(factory)
        source_row.set_list_factory(factory)
        source_row.set_use_subtitle(True)
        current = self._selected_source()
        if current and current in display_to_source:
            source_row.set_selected(display_to_source.index(current))
        else:
            source_row.set_selected(0)
            self.settings["source"] = display_to_source[0]
            self.set_settings(self.settings)

        def on_selected(*_args):
            idx = source_row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(display_to_source):
                self.settings["source"] = display_to_source[idx]
                self.set_settings(self.settings)
                self._ensure_monitoring()
                self._refresh_labels_from_settings()

        source_row.connect("notify::selected", on_selected)
        rows.append(source_row)

        # Playback output device (PulseAudio/PipeWire sink)
        sinks: list[dict[str, str]] = []
        try:
            sinks = self._ws().sinks()
        except Exception:
            sinks = []

        sink_model = Gtk.StringList()
        display_to_sink: list[str] = []

        # Always include default
        sink_model.append("Default")
        display_to_sink.append("")

        for entry in sinks:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            label = f"{name}"
            sink_model.append(label)
            display_to_sink.append(name)

        sink_row = Adw.ComboRow(model=sink_model, title="Playback output")
        sink_row.set_factory(factory)
        sink_row.set_list_factory(factory)
        sink_row.set_use_subtitle(True)

        current_sink = self._selected_sink() or ""
        if current_sink in display_to_sink:
            sink_row.set_selected(display_to_sink.index(current_sink))
        else:
            sink_row.set_selected(0)
            self.settings["playback_sink"] = ""
            self.set_settings(self.settings)

        def on_sink_selected(*_args):
            idx = sink_row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(display_to_sink):
                self.settings["playback_sink"] = display_to_sink[idx]
                self.set_settings(self.settings)

        sink_row.connect("notify::selected", on_sink_selected)
        rows.append(sink_row)

        # Playback mode setting
        playback_modes = ["play-stop", "play-restart", "play-overlap"]
        playback_labels = ["Play / Stop", "Play / Restart", "Play / Overlap"]
        playback_model = Gtk.StringList()
        for label in playback_labels:
            playback_model.append(label)

        playback_row = Adw.ComboRow(model=playback_model, title="Playback mode")
        current_mode = self.settings.get("playback_mode", "play-stop")
        if current_mode in playback_modes:
            playback_row.set_selected(playback_modes.index(current_mode))
        else:
            playback_row.set_selected(0)

        def on_playback_selected(*_args):
            idx = playback_row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(playback_modes):
                self.settings["playback_mode"] = playback_modes[idx]
                self.set_settings(self.settings)

        playback_row.connect("notify::selected", on_playback_selected)
        rows.append(playback_row)

        return rows


    def _trigger_clip(self):
        def work():
            source = self._selected_source()
            if not source:
                return

            if not self._ensure_daemon_running():
                return

            self._ensure_listener()
            self._ensure_monitoring()

            def set_status(text: str):
                if hasattr(self, "set_center_label"):
                    try:
                        self.set_center_label(text[:20], font_size=14)
                    except Exception:
                        pass

            GLib.idle_add(lambda: set_status("Clipping"))

            with self._clip_lock:
                self._waiting_for_source = source
                self._waiting_clip = None
                self._waiting_event.clear()

            try:
                preview_sink = ""
                try:
                    preview_sink = (self.plugin_base.get_settings() or {}).get("preview_sink", "")
                except Exception:
                    preview_sink = ""
                preview_sink = (preview_sink or "").strip()

                payload: dict[str, Any] = {"cmd": "clip", "source": source}
                if preview_sink:
                    payload["preview_sink"] = preview_sink

                resp = self._ws().request(payload)
            except Exception as e:
                log.error(f"clip failed: {e}")
                GLib.idle_add(lambda: set_status("Clip failed"))
                return

            if resp.get("type") != "ok":
                log.error(f"clip error: {resp}")
                GLib.idle_add(lambda: set_status("Clip error"))
                return

            if not self._waiting_event.wait(timeout=60):
                GLib.idle_add(lambda: set_status("Timeout"))
                return

            with self._clip_lock:
                clip = dict(self._waiting_clip or {})
                self._waiting_for_source = None

            if clip.get("source") != source:
                return

            path = (clip.get("path") or "").strip()
            if not path:
                if clip.get("canceled") is True:
                    GLib.idle_add(lambda: set_status("Canceled"))
                else:
                    GLib.idle_add(lambda: set_status("No clip"))
                self._refresh_labels_from_settings()
                return

            self.settings["last_clip_path"] = path
            self.set_settings(self.settings)

            GLib.idle_add(lambda: set_status("Saved"))
            self._refresh_labels_from_settings()

        threading.Thread(target=work, daemon=True).start()

    def _play_last_clip(self):
        path = (self.settings.get("last_clip_path") or "").strip()
        if not path:
            return
        self._player.play(path, sink=self._selected_sink())

    def _play_last_clip_overlap(self):
        """Play clip without stopping current playback (fire and forget)."""
        path = (self.settings.get("last_clip_path") or "").strip()
        if not path or not os.path.exists(path):
            return
        sink = self._selected_sink()
        paplay = shutil.which("paplay")
        aplay = shutil.which("aplay")
        player = paplay or aplay
        if not player:
            return

        args: list[str] = [player]
        if sink and paplay:
            args.extend(["-d", sink])
        args.append(path)
        subprocess.Popen(args)
