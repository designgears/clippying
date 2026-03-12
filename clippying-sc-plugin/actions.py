import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GLib", "2.0")
from gi.repository import Adw, Gio, GLib, Gtk

from loguru import logger as log

import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionBase import ActionBase
from src.windows.Settings.PluginSettingsWindow.PluginSettingsWindow import PluginSettingsWindow

try:
    from clippying_native import api_is_up as _native_api_is_up
    from clippying_native import ensure_api as _native_ensure_api
    from clippying_native import resolve_exe as _native_resolve_exe
    from clippying_native import stop_api as _native_stop_api

    _NATIVE_BINDINGS_AVAILABLE = True
except Exception:
    _NATIVE_BINDINGS_AVAILABLE = False


_ACTIVE_ACTIONS: "weakref.WeakSet[ClippyingActionBase]" = weakref.WeakSet()
_ACTIVE_ACTIONS_LOCK = threading.Lock()
_DEFAULT_WS_URL = "ws://127.0.0.1:17373"
_DEFAULT_CLIPPYING_EXE = "__embedded__"
_DEFAULT_CLIPS_DIR = os.path.join(os.path.expanduser("~"), "clips")
_AUDIO_FILE_PATTERNS = [
    "*.wav",
    "*.mp3",
    "*.flac",
    "*.ogg",
    "*.oga",
    "*.opus",
    "*.m4a",
    "*.aac",
]
_AUDIO_FILE_EXTENSIONS = {os.path.splitext(pattern)[1].lower() for pattern in _AUDIO_FILE_PATTERNS}


def _run_clippying(exe: str, args: list[str]) -> tuple[bool, str]:
    if not exe:
        return False, "empty executable path"
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


def _resolve_clippying_exe(exe: str) -> str:
    candidate = (exe or "").strip() or _DEFAULT_CLIPPYING_EXE
    if _NATIVE_BINDINGS_AVAILABLE:
        try:
            return _native_resolve_exe(candidate)
        except Exception:
            pass
    return candidate


def _ws_is_up(url: str) -> bool:
    if _NATIVE_BINDINGS_AVAILABLE:
        try:
            return bool(_native_api_is_up(url))
        except Exception:
            return False

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


def _normalize_clips_dir(path: str | None) -> str:
    candidate = (path or "").strip()
    if not candidate:
        candidate = _DEFAULT_CLIPS_DIR
    return os.path.abspath(os.path.expanduser(candidate))


def _sanitize_source_name(source: str | None) -> str:
    candidate = (source or "").strip() or "default-source"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._") or "default-source"


def _predictable_latest_clip_path(source: str | None, clips_dir: str | None) -> str:
    return os.path.join(_normalize_clips_dir(clips_dir), _sanitize_source_name(source), "latest.wav")


def _audio_file_filter_store() -> Gio.ListStore:
    filter_store = Gio.ListStore.new(Gtk.FileFilter)

    audio_filter = Gtk.FileFilter()
    audio_filter.set_name("Audio files")
    for pattern in _AUDIO_FILE_PATTERNS:
        audio_filter.add_pattern(pattern)
    filter_store.append(audio_filter)

    any_filter = Gtk.FileFilter()
    any_filter.set_name("All files")
    any_filter.add_pattern("*")
    filter_store.append(any_filter)

    return filter_store


def _is_audio_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in _AUDIO_FILE_EXTENSIONS


def _is_wav_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".wav"


def _list_audio_files(folder: str) -> list[str]:
    if not folder or not os.path.isdir(folder):
        return []
    entries: list[str] = []
    try:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if _is_audio_file(path):
                entries.append(path)
    except Exception:
        return []
    return entries


def _plugin_settings(plugin_base) -> dict[str, Any]:
    try:
        return plugin_base.get_settings() or {}
    except Exception:
        return {}


def _plugin_clips_dir(plugin_base) -> str:
    return _normalize_clips_dir(_plugin_settings(plugin_base).get("clips_dir"))


def _build_text_factory() -> Gtk.SignalListItemFactory:
    def setup_label(_factory, list_item):
        label = Gtk.Label(xalign=0, wrap=True, wrap_mode=2)
        list_item.set_child(label)

    def bind_label(_factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        label.set_text(item.get_string())

    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", setup_label)
    factory.connect("bind", bind_label)
    return factory


@dataclass(slots=True)
class SharedClipInfo:
    path: str
    saved_path: str
    updated_at: float


class SharedClipRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._clips: dict[tuple[str, str], SharedClipInfo] = {}
        self._subscribers: dict[tuple[str, str], weakref.WeakSet] = {}

    def get(self, ws_url: str, source: str) -> SharedClipInfo | None:
        key = ((ws_url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL, (source or "").strip())
        with self._lock:
            return self._clips.get(key)

    def subscribe(self, action: "ClippyingActionBase", ws_url: str, source: str) -> None:
        source_name = (source or "").strip()
        if not source_name:
            return
        key = ((ws_url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL, source_name)
        with self._lock:
            self._subscribers.setdefault(key, weakref.WeakSet()).add(action)

    def unsubscribe(self, action: "ClippyingActionBase", ws_url: str | None, source: str | None) -> None:
        source_name = (source or "").strip()
        if not source_name:
            return
        key = ((ws_url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL, source_name)
        with self._lock:
            subscribers = self._subscribers.get(key)
            if not subscribers:
                return
            subscribers.discard(action)
            if not subscribers:
                self._subscribers.pop(key, None)

    def update(self, ws_url: str, source: str, path: str, saved_path: str = "") -> bool:
        ws_url = (ws_url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL
        source = (source or "").strip()
        path = (path or "").strip()
        saved_path = (saved_path or "").strip()
        if not source or not path:
            return False

        info = SharedClipInfo(path=path, saved_path=saved_path, updated_at=time.time())
        key = (ws_url, source)

        with self._lock:
            previous = self._clips.get(key)
            changed = previous is None or previous.path != info.path or previous.saved_path != info.saved_path
            self._clips[key] = info
            subscribers = list(self._subscribers.get(key, weakref.WeakSet()))

        if changed:
            for action in subscribers:
                if action is None:
                    continue
                try:
                    action.on_shared_clip_updated(info)
                except Exception as e:
                    log.debug(f"shared clip notification failed: {e}")
        return changed


_SHARED_CLIPS = SharedClipRegistry()


_DAEMON_STOP_LOCK = threading.Lock()
_DAEMON_STOP_REQUESTED = False


class _DaemonHostManager:
    """Keeps the local API hosted and fails over when it disappears."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_failure_log_at: dict[str, float] = {}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="clippying-host-manager")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = None
        with self._lock:
            t = self._thread
            self._thread = None
        if t:
            t.join(timeout=1.0)

    def ensure_now(self, url: str, exe: str) -> bool:
        self.start()
        return self._ensure_running(url, exe, log_on_failure=True)

    def _targets(self) -> list[tuple[str, str]]:
        with _ACTIVE_ACTIONS_LOCK:
            actions = list(_ACTIVE_ACTIONS)

        targets: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for action in actions:
            try:
                url = action._ws_url()
            except Exception:
                url = _DEFAULT_WS_URL
            try:
                exe = action._clippying_exe()
            except Exception:
                exe = _DEFAULT_CLIPPYING_EXE

            key = (
                (url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL,
                (exe or _DEFAULT_CLIPPYING_EXE).strip() or _DEFAULT_CLIPPYING_EXE,
            )
            if key in seen:
                continue
            seen.add(key)
            targets.append(key)

        if not targets:
            targets.append((_DEFAULT_WS_URL, _DEFAULT_CLIPPYING_EXE))
        return targets

    def _run(self) -> None:
        while not self._stop.is_set():
            for url, exe in self._targets():
                if self._stop.is_set():
                    return
                self._ensure_running(url, exe, log_on_failure=False)
            self._stop.wait(2.0)

    def _ensure_running(self, url: str, exe: str, log_on_failure: bool) -> bool:
        ws_url = (url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL
        binary = (exe or _DEFAULT_CLIPPYING_EXE).strip() or _DEFAULT_CLIPPYING_EXE

        ok = False
        msg = ""
        if _NATIVE_BINDINGS_AVAILABLE:
            try:
                ok = bool(_native_ensure_api(ws_url, binary, 3000))
            except Exception as e:
                msg = str(e)
        else:
            if not log_on_failure and _ws_is_up(ws_url):
                return True
            ok, msg = _run_clippying(binary, ["start"])
            if ok:
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if self._stop.is_set():
                        return False
                    if _ws_is_up(ws_url):
                        return True
                    time.sleep(0.1)

        if ok:
            return True

        if log_on_failure:
            log.error(f"daemon not reachable at {ws_url}")
        else:
            now = time.time()
            last = self._last_failure_log_at.get(ws_url, 0.0)
            if now - last >= 10.0:
                detail = msg if msg else "no response"
                log.warning(f"daemon heartbeat failed at {ws_url}; host attempt result: {detail}")
                self._last_failure_log_at[ws_url] = now
        return False


_HOST_MANAGER = _DaemonHostManager()


def start_host_manager() -> None:
    _HOST_MANAGER.start()


def stop_host_manager() -> None:
    _HOST_MANAGER.stop()


def stop_daemon_best_effort(exe: str | None = None) -> None:
    global _DAEMON_STOP_REQUESTED
    with _DAEMON_STOP_LOCK:
        if _DAEMON_STOP_REQUESTED:
            return
        _DAEMON_STOP_REQUESTED = True

    _HOST_MANAGER.stop()

    try:
        if not exe:
            with _ACTIVE_ACTIONS_LOCK:
                actions = list(_ACTIVE_ACTIONS)

            for action in actions:
                try:
                    exe = action._clippying_exe()
                except Exception:
                    exe = None
                if exe:
                    break

        exe = (exe or _DEFAULT_CLIPPYING_EXE).strip()
        if _NATIVE_BINDINGS_AVAILABLE:
            ok, msg = _native_stop_api(exe)
        else:
            ok, msg = _run_clippying(exe, ["stop"])
        if not ok:
            log.error(f"failed to stop daemon: {msg}")
    except Exception as e:
        log.error(f"failed to stop daemon: {e}")


class AudioPlayer:
    """Manages preview playback with stop, loop, and overlap support."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._session_id = 0

    def is_playing(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        with self._lock:
            self._session_id += 1
            process = self._process
            self._process = None
        self._terminate_process(process)

    def play(
        self,
        path: str,
        sink: str | None = None,
        loop: bool = False,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> bool:
        path = (path or "").strip()
        if not path or not os.path.exists(path):
            return False

        self.stop()
        with self._lock:
            self._session_id += 1
            session_id = self._session_id

        spawned = self._spawn_process(path, sink=sink, start_sec=start_sec, end_sec=end_sec)
        if spawned is None:
            return False
        process, cleanup_path = spawned

        with self._lock:
            if self._session_id != session_id:
                self._terminate_process(process)
                self._cleanup_path(cleanup_path)
                return False
            self._process = process

        thread = threading.Thread(
            target=self._watch_process,
            args=(session_id, path, sink, loop, start_sec, end_sec, process, cleanup_path),
            daemon=True,
            name="clippying-audio-player",
        )
        thread.start()
        return True

    def play_overlap(
        self,
        path: str,
        sink: str | None = None,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> bool:
        path = (path or "").strip()
        if not path or not os.path.exists(path):
            return False
        spawned = self._spawn_process(path, sink=sink, start_sec=start_sec, end_sec=end_sec)
        if spawned is None:
            return False
        process, cleanup_path = spawned

        def wait_and_cleanup():
            try:
                process.wait()
            finally:
                self._cleanup_path(cleanup_path)

        threading.Thread(target=wait_and_cleanup, daemon=True, name="clippying-audio-overlap").start()
        return True

    def _watch_process(
        self,
        session_id: int,
        path: str,
        sink: str | None,
        loop: bool,
        start_sec: float | None,
        end_sec: float | None,
        process: subprocess.Popen,
        cleanup_path: str | None,
    ) -> None:
        current = process
        current_cleanup = cleanup_path
        while True:
            current.wait()
            self._cleanup_path(current_cleanup)
            with self._lock:
                if self._session_id != session_id:
                    if self._process is current:
                        self._process = None
                    return
                if not loop:
                    if self._process is current:
                        self._process = None
                    return

            next_spawned = self._spawn_process(path, sink=sink, start_sec=start_sec, end_sec=end_sec)
            if next_spawned is None:
                with self._lock:
                    if self._session_id == session_id and self._process is current:
                        self._process = None
                return
            next_process, next_cleanup = next_spawned

            with self._lock:
                if self._session_id != session_id:
                    self._terminate_process(next_process)
                    self._cleanup_path(next_cleanup)
                    if self._process is current:
                        self._process = None
                    return
                self._process = next_process
            current = next_process
            current_cleanup = next_cleanup

    def _spawn_process(
        self,
        path: str,
        sink: str | None = None,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> tuple[subprocess.Popen, str | None] | None:
        sink = (sink or "").strip() or None
        if start_sec is not None or end_sec is not None:
            builders = [
                self._build_ffmpeg_paplay_command,
                self._build_ffmpeg_aplay_command,
                self._build_ffplay_command,
                self._build_paplay_command,
                self._build_aplay_command,
            ]
        else:
            builders = [
                self._build_paplay_command,
                self._build_aplay_command,
                self._build_ffmpeg_paplay_command,
                self._build_ffmpeg_aplay_command,
                self._build_ffplay_command,
            ]
        for builder in builders:
            command = builder(path, sink, start_sec, end_sec)
            if command is None:
                continue
            args, env, cleanup_path = command
            try:
                return subprocess.Popen(args, env=env), cleanup_path
            except Exception as e:
                log.debug(f"player launch failed for {args[0]}: {e}")
                self._cleanup_path(cleanup_path)
        return None

    def _build_ffplay_command(
        self,
        path: str,
        sink: str | None,
        start_sec: float | None,
        end_sec: float | None,
    ) -> tuple[list[str], dict[str, str], str | None] | None:
        ffplay = shutil.which("ffplay")
        if not ffplay:
            return None
        env = os.environ.copy()
        if sink:
            env["PULSE_SINK"] = sink
        args = [ffplay, "-v", "error", "-nodisp", "-autoexit"]
        if start_sec is not None and start_sec > 0:
            args.extend(["-ss", f"{start_sec:.6f}"])
        if end_sec is not None and start_sec is not None and end_sec > start_sec:
            args.extend(["-t", f"{(end_sec - start_sec):.6f}"])
        elif end_sec is not None and end_sec > 0:
            args.extend(["-t", f"{end_sec:.6f}"])
        args.append(path)
        return args, env, None

    def _build_paplay_command(
        self,
        path: str,
        sink: str | None,
        start_sec: float | None,
        end_sec: float | None,
    ) -> tuple[list[str], dict[str, str] | None, str | None] | None:
        paplay = shutil.which("paplay")
        if not paplay:
            return None
        if start_sec is not None or end_sec is not None or not _is_wav_file(path):
            return None
        args = [paplay]
        if sink:
            args.extend(["-d", sink])
        args.append(path)
        return args, None, None

    def _build_ffmpeg_paplay_command(
        self,
        path: str,
        sink: str | None,
        start_sec: float | None,
        end_sec: float | None,
    ) -> tuple[list[str], dict[str, str] | None, str | None] | None:
        paplay = shutil.which("paplay")
        ffmpeg = shutil.which("ffmpeg")
        if not paplay or not ffmpeg:
            return None
        temp_path = self._render_temp_wav(path, start_sec, end_sec, ffmpeg)
        if temp_path is None:
            return None
        args = [paplay]
        if sink:
            args.extend(["-d", sink])
        args.append(temp_path)
        return args, None, temp_path

    def _build_aplay_command(
        self,
        path: str,
        sink: str | None,
        start_sec: float | None,
        end_sec: float | None,
    ) -> tuple[list[str], dict[str, str] | None, str | None] | None:
        aplay = shutil.which("aplay")
        if not aplay:
            return None
        if sink or start_sec is not None or end_sec is not None or not _is_wav_file(path):
            return None
        return [aplay, path], None, None

    def _build_ffmpeg_aplay_command(
        self,
        path: str,
        sink: str | None,
        start_sec: float | None,
        end_sec: float | None,
    ) -> tuple[list[str], dict[str, str] | None, str | None] | None:
        aplay = shutil.which("aplay")
        ffmpeg = shutil.which("ffmpeg")
        if sink or not aplay or not ffmpeg:
            return None
        temp_path = self._render_temp_wav(path, start_sec, end_sec, ffmpeg)
        if temp_path is None:
            return None
        return [aplay, temp_path], None, temp_path

    def _render_temp_wav(
        self,
        path: str,
        start_sec: float | None,
        end_sec: float | None,
        ffmpeg: str,
    ) -> str | None:
        fd, temp_path = tempfile.mkstemp(prefix="clippying-playback-", suffix=".wav")
        os.close(fd)

        args = [ffmpeg, "-v", "error"]
        if start_sec is not None and start_sec > 0:
            args.extend(["-ss", f"{start_sec:.6f}"])
        args.extend(["-i", path])
        if end_sec is not None and start_sec is not None and end_sec > start_sec:
            args.extend(["-t", f"{(end_sec - start_sec):.6f}"])
        elif end_sec is not None and end_sec > 0:
            args.extend(["-t", f"{end_sec:.6f}"])
        args.extend(["-vn", "-acodec", "pcm_s16le", "-y", temp_path])

        try:
            result = subprocess.run(args, capture_output=True, text=True, check=False)
        except Exception as e:
            self._cleanup_path(temp_path)
            log.debug(f"ffmpeg render failed: {e}")
            return None

        if result.returncode != 0:
            self._cleanup_path(temp_path)
            log.debug(f"ffmpeg render failed: {(result.stderr or result.stdout).strip()}")
            return None
        return temp_path

    def _cleanup_path(self, path: str | None) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _terminate_process(self, process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
        except Exception:
            pass


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

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="clippying-event-listener")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
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


class ClippyingActionBase(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self.settings: dict[str, Any] = {}
        self._listener: ClippyingEventListener | None = None
        self._player = _audio_player
        self._subscribed_source: str | None = None
        self._flash_step = 0

    def on_ready(self):
        self.settings = self.get_settings() or {}
        with _ACTIVE_ACTIONS_LOCK:
            _ACTIVE_ACTIONS.add(self)
        start_host_manager()
        if self._needs_listener():
            self._ensure_listener()
        self._refresh_shared_subscription()
        self._refresh_labels()

    def on_remove(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        self._unsubscribe_from_shared_clip()
        with _ACTIVE_ACTIONS_LOCK:
            try:
                _ACTIVE_ACTIONS.remove(self)
            except KeyError:
                pass

    def on_plugin_settings_changed(self) -> None:
        self.settings = self.get_settings() or {}
        self._refresh_shared_subscription()
        self._refresh_labels()

    def on_shared_clip_updated(self, clip_info: SharedClipInfo) -> None:
        GLib.idle_add(self._apply_shared_clip_update, clip_info)

    def _apply_shared_clip_update(self, clip_info: SharedClipInfo) -> bool:
        self._handle_shared_clip_update(clip_info)
        return False

    def _handle_shared_clip_update(self, _clip_info: SharedClipInfo) -> None:
        self._refresh_labels()

    def _needs_listener(self) -> bool:
        return False

    def _ws_url(self) -> str:
        return (self.settings.get("ws_url") or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL

    def _clippying_exe(self) -> str:
        return (self.settings.get("clippying_exe") or _DEFAULT_CLIPPYING_EXE).strip() or _DEFAULT_CLIPPYING_EXE

    def _ws(self) -> ClippyingWsClient:
        return ClippyingWsClient(self._ws_url())

    def _clips_dir(self) -> str:
        return _plugin_clips_dir(self.plugin_base)

    def _preview_sink(self) -> str | None:
        sink = (_plugin_settings(self.plugin_base).get("preview_sink") or "").strip()
        return sink or None

    def _selected_source(self) -> str | None:
        selected = (self.settings.get("source") or "").strip()
        return selected or None

    def _selected_sink(self) -> str | None:
        selected = (self.settings.get("playback_sink") or "").strip()
        return selected or None

    def _shared_source(self) -> str | None:
        return None

    def _ensure_daemon_running(self) -> bool:
        return _HOST_MANAGER.ensure_now(self._ws_url(), self._clippying_exe())

    def _ensure_listener(self) -> None:
        if self._listener is not None:
            return

        def on_event(data: dict[str, Any]):
            self._handle_ws_event(data)

        self._listener = ClippyingEventListener(self._ws_url(), on_event)
        self._listener.start()

    def _handle_ws_event(self, data: dict[str, Any]) -> None:
        if data.get("type") == "clip_saved":
            source = (data.get("source") or "").strip()
            path = (data.get("path") or "").strip()
            saved_path = (data.get("saved_path") or "").strip()
            if source and path:
                _SHARED_CLIPS.update(self._ws_url(), source, path, saved_path=saved_path)

    def _refresh_shared_subscription(self) -> None:
        source = self._shared_source()
        if source == self._subscribed_source:
            return
        self._unsubscribe_from_shared_clip()
        if source:
            _SHARED_CLIPS.subscribe(self, self._ws_url(), source)
            self._subscribed_source = source

    def _unsubscribe_from_shared_clip(self) -> None:
        if self._subscribed_source:
            _SHARED_CLIPS.unsubscribe(self, self._ws_url(), self._subscribed_source)
        self._subscribed_source = None

    def _shared_clip_info(self) -> SharedClipInfo | None:
        source = self._shared_source()
        if not source:
            return None

        clip_info = _SHARED_CLIPS.get(self._ws_url(), source)
        if clip_info is not None:
            return clip_info

        predictable_path = _predictable_latest_clip_path(source, self._clips_dir())
        if os.path.exists(predictable_path):
            updated = 0.0
            try:
                updated = os.path.getmtime(predictable_path)
            except Exception:
                pass
            return SharedClipInfo(path=predictable_path, saved_path="", updated_at=updated)
        return None

    def _clip_label_name(self, clip_info: SharedClipInfo | None) -> str:
        if clip_info is None:
            return ""
        return os.path.basename(clip_info.saved_path or clip_info.path)

    def _flash_button(self, color: list[int], blinks: int = 4, interval_ms: int = 180) -> None:
        self._flash_step += 1
        flash_id = self._flash_step
        total_steps = max(1, blinks * 2)
        step_state = {"step": 0}

        def tick():
            if flash_id != self._flash_step:
                return False
            if step_state["step"] >= total_steps:
                try:
                    self.set_background_color([0, 0, 0, 0])
                except Exception:
                    pass
                return False
            try:
                self.set_background_color(color if step_state["step"] % 2 == 0 else [0, 0, 0, 0])
            except Exception:
                return False
            step_state["step"] += 1
            return True

        GLib.timeout_add(interval_ms, tick)

    def _open_plugin_settings_row(self) -> Adw.ActionRow:
        row = Adw.ActionRow(title="Global settings")
        row.set_subtitle("Open Clippying plugin settings")
        button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)

        def on_clicked(*_args):
            try:
                settings_window = PluginSettingsWindow(self.plugin_base)
                settings_window.present(gl.app.get_active_window())
            except Exception as e:
                log.error(f"Failed to open plugin settings: {e}")

        button.connect("clicked", on_clicked)
        row.add_suffix(button)
        row.set_activatable_widget(button)
        return row

    def _error_row(self, title: str, subtitle: str) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.add_css_class("warning")
        return row

    def _source_combo_row(self) -> Adw.PreferencesRow:
        if not self._ensure_daemon_running():
            return self._error_row("Daemon not reachable", "Unable to connect or start the daemon.")

        try:
            sources = self._ws().sources()
        except Exception as e:
            return self._error_row("Daemon not reachable", str(e))

        if not sources:
            return self._error_row("No sources", "Daemon returned no monitor sources.")

        model = Gtk.StringList()
        display_to_source: list[str] = []
        for entry in sources:
            name = (entry.get("name") or "").strip()
            description = (entry.get("description") or "").strip()
            if not name:
                continue
            label = f"{name} - {description}" if description else name
            model.append(label)
            display_to_source.append(name)

        if not display_to_source:
            return self._error_row("No sources", "Daemon returned no usable monitor sources.")

        row = Adw.ComboRow(model=model, title="Monitor source")
        factory = _build_text_factory()
        row.set_factory(factory)
        row.set_list_factory(factory)
        row.set_use_subtitle(True)
        row.set_subtitle("Source whose rolling buffer will be clipped")

        current = self._selected_source()
        if current in display_to_source:
            row.set_selected(display_to_source.index(current))
        else:
            row.set_selected(0)
            self.settings["source"] = display_to_source[0]
            self.set_settings(self.settings)

        def on_selected(*_args):
            idx = row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(display_to_source):
                self.settings["source"] = display_to_source[idx]
                self.set_settings(self.settings)
                self._refresh_shared_subscription()
                self._refresh_labels()
                self._after_source_changed()

        row.connect("notify::selected", on_selected)
        return row

    def _after_source_changed(self) -> None:
        pass

    def _sink_combo_row(self) -> Adw.ComboRow:
        sinks: list[dict[str, str]] = []
        try:
            if self._ensure_daemon_running():
                sinks = self._ws().sinks()
        except Exception:
            sinks = []

        model = Gtk.StringList()
        display_to_sink: list[str] = []
        model.append("Default")
        display_to_sink.append("")

        for entry in sinks:
            name = (entry.get("name") or "").strip()
            description = (entry.get("description") or "").strip()
            if not name:
                continue
            label = f"{name} - {description}" if description else name
            model.append(label)
            display_to_sink.append(name)

        row = Adw.ComboRow(model=model, title="Playback output")
        factory = _build_text_factory()
        row.set_factory(factory)
        row.set_list_factory(factory)
        row.set_use_subtitle(True)
        row.set_subtitle("Output device used for preview playback")

        current = self._selected_sink() or ""
        if current in display_to_sink:
            row.set_selected(display_to_sink.index(current))
        else:
            row.set_selected(0)
            self.settings["playback_sink"] = ""
            self.set_settings(self.settings)

        def on_selected(*_args):
            idx = row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(display_to_sink):
                self.settings["playback_sink"] = display_to_sink[idx]
                self.set_settings(self.settings)

        row.connect("notify::selected", on_selected)
        return row

    def _mode_combo_row(
        self,
        title: str,
        subtitle: str,
        key: str,
        options: list[tuple[str, str]],
        default_value: str,
    ) -> Adw.ComboRow:
        model = Gtk.StringList()
        values: list[str] = []
        for value, label in options:
            values.append(value)
            model.append(label)

        row = Adw.ComboRow(model=model, title=title)
        row.set_subtitle(subtitle)

        current = (self.settings.get(key) or default_value).strip() or default_value
        if current in values:
            row.set_selected(values.index(current))
        else:
            row.set_selected(values.index(default_value))
            self.settings[key] = default_value
            self.set_settings(self.settings)

        def on_selected(*_args):
            idx = row.get_selected()
            if idx is None or idx == Gtk.INVALID_LIST_POSITION:
                return
            if 0 <= idx < len(values):
                self.settings[key] = values[idx]
                self.set_settings(self.settings)
                self._refresh_labels()

        row.connect("notify::selected", on_selected)
        return row

    def _playback_mode_row(self) -> Adw.ComboRow:
        return self._mode_combo_row(
            title="Playback mode",
            subtitle="How the action behaves when you press the button",
            key="playback_mode",
            default_value="play-stop",
            options=[
                ("play-stop", "Play / Stop"),
                ("play-restart", "Play / Restart"),
                ("play-overlap", "Play / Overlap"),
                ("loop-stop", "Loop / Stop"),
                ("hold-to-play", "Hold To Play"),
            ],
        )

    def _setting_path_row(
        self,
        title: str,
        subtitle: str,
        path_getter: Callable[[], str],
        on_browse: Callable[[], None],
        on_clear: Callable[[], None],
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        current = (path_getter() or "").strip()
        row.set_subtitle(current or subtitle)

        browse_button = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        clear_button = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)

        browse_button.connect("clicked", lambda *_args: on_browse())
        clear_button.connect("clicked", lambda *_args: on_clear())

        row.add_suffix(clear_button)
        row.add_suffix(browse_button)
        return row

    def _open_audio_file_dialog(self, current_path: str | None, callback: Callable[[str], None]) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select audio file")
        dialog.set_modal(True)
        dialog.set_filters(_audio_file_filter_store())

        current = (current_path or "").strip()
        if current:
            try:
                dialog.set_initial_file(Gio.File.new_for_path(current))
            except Exception:
                pass

        def on_response(file_dialog: Gtk.FileDialog, result):
            try:
                selected = file_dialog.open_finish(result)
            except GLib.Error:
                return
            if not selected:
                return
            callback(selected.get_path() or "")

        dialog.open(gl.app.get_active_window(), None, on_response)

    def _open_folder_dialog(self, current_path: str | None, callback: Callable[[str], None]) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select folder")
        dialog.set_modal(True)

        current = (current_path or "").strip()
        if current:
            initial_folder = current if os.path.isdir(current) else os.path.dirname(current)
            if initial_folder:
                try:
                    dialog.set_initial_folder(Gio.File.new_for_path(initial_folder))
                except Exception:
                    pass

        def on_response(file_dialog: Gtk.FileDialog, result):
            try:
                selected = file_dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if not selected:
                return
            callback(selected.get_path() or "")

        dialog.select_folder(gl.app.get_active_window(), None, on_response)

    def _refresh_labels(self) -> None:
        pass


class ClippyingCaptureAction(ClippyingActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._waiting_for_source: str | None = None
        self._waiting_event = threading.Event()
        self._waiting_clip: dict[str, Any] | None = None
        self._clip_lock = threading.Lock()

    def _needs_listener(self) -> bool:
        return True

    def _shared_source(self) -> str | None:
        return self._selected_source()

    def on_ready(self):
        super().on_ready()
        self._ensure_monitoring()

    def on_remove(self) -> None:
        super().on_remove()
        self._ensure_monitoring()

    def _after_source_changed(self) -> None:
        self._ensure_monitoring()

    def event_callback(self, event, data=None):
        event_str = str(event)
        if event_str == "Key Short Up":
            self._trigger_clip()

    def _handle_ws_event(self, data: dict[str, Any]) -> None:
        super()._handle_ws_event(data)
        if data.get("type") != "clip_saved":
            return
        source = (data.get("source") or "").strip()
        if not source:
            return
        with self._clip_lock:
            if self._waiting_for_source and source == self._waiting_for_source:
                self._waiting_clip = dict(data)
                self._waiting_event.set()

    def _handle_shared_clip_update(self, clip_info: SharedClipInfo) -> None:
        self.settings["last_clip_path"] = clip_info.path
        if clip_info.saved_path:
            self.settings["last_saved_clip_path"] = clip_info.saved_path
        self.set_settings(self.settings)
        self._flash_button([28, 138, 84, 255], blinks=3)
        self._refresh_labels()

    def _ensure_monitoring(self) -> None:
        if not self._ensure_daemon_running():
            return

        with _ACTIVE_ACTIONS_LOCK:
            desired_sources = {
                action._selected_source()
                for action in list(_ACTIVE_ACTIONS)
                if isinstance(action, ClippyingCaptureAction)
            }

        desired_sources.discard(None)

        try:
            if not desired_sources:
                self._ws().request({"cmd": "stop_all"})
                return

            resp = self._ws().request({"cmd": "status"})
            statuses = list(resp.get("statuses", [])) if resp.get("type") == "status" else []
            current_sources = {status.get("source") for status in statuses if isinstance(status, dict)}
            current_sources.discard(None)

            for source in sorted(current_sources - desired_sources):
                self._ws().request({"cmd": "stop", "source": source})

            for source in sorted(desired_sources - current_sources):
                self._ws().request({"cmd": "monitor", "source": source})
        except Exception as e:
            log.error(f"monitor sync failed: {e}")

    def get_config_rows(self) -> list:
        self.settings = self.get_settings() or {}
        rows: list = [self._open_plugin_settings_row(), self._source_combo_row()]

        source = self._selected_source()
        predictable = _predictable_latest_clip_path(source, self._clips_dir()) if source else ""
        info_row = Adw.ActionRow(title="Latest clip path")
        info_row.set_subtitle(predictable or "Select a source to enable deterministic latest.wav output")
        rows.append(info_row)
        return rows

    def _trigger_clip(self) -> None:
        def work():
            source = self._selected_source()
            if not source:
                return

            if not self._ensure_daemon_running():
                return

            self._ensure_listener()
            self._ensure_monitoring()

            GLib.idle_add(lambda: self.set_center_label("Clipping", font_size=14))

            with self._clip_lock:
                self._waiting_for_source = source
                self._waiting_clip = None
                self._waiting_event.clear()

            try:
                preview_sink = (_plugin_settings(self.plugin_base).get("preview_sink") or "").strip()
                payload: dict[str, Any] = {
                    "cmd": "clip",
                    "source": source,
                    "clips_dir": self._clips_dir(),
                }
                if preview_sink:
                    payload["preview_sink"] = preview_sink
                resp = self._ws().request(payload)
            except Exception as e:
                log.error(f"clip failed: {e}")
                GLib.idle_add(lambda: self.set_center_label("Clip failed", font_size=14))
                return

            if resp.get("type") != "ok":
                log.error(f"clip error: {resp}")
                GLib.idle_add(lambda: self.set_center_label("Clip error", font_size=14))
                return

            if not self._waiting_event.wait(timeout=60):
                GLib.idle_add(lambda: self.set_center_label("Timeout", font_size=14))
                return

            with self._clip_lock:
                clip = dict(self._waiting_clip or {})
                self._waiting_for_source = None

            path = (clip.get("path") or "").strip()
            saved_path = (clip.get("saved_path") or "").strip()
            if not path:
                if clip.get("canceled") is True:
                    GLib.idle_add(lambda: self.set_center_label("Canceled", font_size=14))
                else:
                    GLib.idle_add(lambda: self.set_center_label("No clip", font_size=14))
                self._refresh_labels()
                return

            self.settings["last_clip_path"] = path
            if saved_path:
                self.settings["last_saved_clip_path"] = saved_path
            self.set_settings(self.settings)

            GLib.idle_add(lambda: self.set_center_label("Saved", font_size=14))
            self._refresh_labels()

        threading.Thread(target=work, daemon=True, name="clippying-capture").start()

    def _refresh_labels(self) -> None:
        source = self._selected_source() or "No source"
        clip_info = self._shared_clip_info()
        bottom = self._clip_label_name(clip_info)

        def apply():
            self.set_top_label(source[:30], font_size=14)
            self.set_center_label("Capture", font_size=14)
            self.set_bottom_label(bottom[:30], font_size=12)
            return False

        GLib.idle_add(apply)


class ClippyingLastClipPlaybackAction(ClippyingActionBase):
    def _needs_listener(self) -> bool:
        return True

    def _shared_source(self) -> str | None:
        return self._selected_source()

    def event_callback(self, event, data=None):
        mode = (self.settings.get("playback_mode") or "play-stop").strip() or "play-stop"
        event_str = str(event)

        if mode == "hold-to-play":
            if event_str == "Key Down":
                self._play_latest(loop=False)
            elif event_str in ("Key Up", "Key Hold Stop"):
                self._player.stop()
            return

        if event_str != "Key Short Up":
            return

        if self._player.is_playing():
            if mode in ("play-stop", "loop-stop"):
                self._player.stop()
            elif mode == "play-restart":
                self._play_latest(loop=False)
            elif mode == "play-overlap":
                self._play_latest_overlap()
            return

        self._play_latest(loop=mode == "loop-stop")

    def _handle_shared_clip_update(self, clip_info: SharedClipInfo) -> None:
        self.settings["last_clip_path"] = clip_info.path
        if clip_info.saved_path:
            self.settings["last_saved_clip_path"] = clip_info.saved_path
        self.set_settings(self.settings)
        self._flash_button([240, 173, 78, 255], blinks=4)
        self._refresh_labels()

    def get_config_rows(self) -> list:
        self.settings = self.get_settings() or {}
        return [
            self._open_plugin_settings_row(),
            self._source_combo_row(),
            self._sink_combo_row(),
            self._playback_mode_row(),
        ]

    def _resolved_latest_path(self) -> str:
        clip_info = self._shared_clip_info()
        if clip_info is not None:
            return clip_info.path

        configured = (self.settings.get("last_clip_path") or "").strip()
        if configured and os.path.exists(configured):
            return configured

        source = self._selected_source()
        if not source:
            return ""
        predictable = _predictable_latest_clip_path(source, self._clips_dir())
        if os.path.exists(predictable):
            return predictable
        return ""

    def _play_latest(self, loop: bool) -> None:
        path = self._resolved_latest_path()
        if not path:
            log.debug("No latest clip available for playback")
            return
        if not self._player.play(path, sink=self._selected_sink(), loop=loop):
            log.warning("Failed to start latest-clip playback")

    def _play_latest_overlap(self) -> None:
        path = self._resolved_latest_path()
        if not path:
            return
        if not self._player.play_overlap(path, sink=self._selected_sink()):
            log.warning("Failed to overlap latest-clip playback")

    def _refresh_labels(self) -> None:
        source = self._selected_source() or "No source"
        clip_info = self._shared_clip_info()
        bottom = self._clip_label_name(clip_info)

        def apply():
            self.set_top_label(source[:30], font_size=14)
            self.set_center_label("Latest", font_size=14)
            self.set_bottom_label(bottom[:30], font_size=12)
            return False

        GLib.idle_add(apply)


class ClippyingFilePlayerAction(ClippyingActionBase):
    def event_callback(self, event, data=None):
        mode = (self.settings.get("playback_mode") or "play-stop").strip() or "play-stop"
        event_str = str(event)

        if mode == "hold-to-play":
            if event_str == "Key Down":
                self._play_selected(loop=False)
            elif event_str in ("Key Up", "Key Hold Stop"):
                self._player.stop()
            return

        if event_str != "Key Short Up":
            return

        if self._player.is_playing():
            if mode in ("play-stop", "loop-stop"):
                self._player.stop()
            elif mode == "play-restart":
                self._play_selected(loop=False)
            elif mode == "play-overlap":
                self._play_selected_overlap()
            return

        self._play_selected(loop=mode == "loop-stop")

    def get_config_rows(self) -> list:
        self.settings = self.get_settings() or {}
        rows: list = [self._open_plugin_settings_row()]

        rows.append(
            self._mode_combo_row(
                title="Source mode",
                subtitle="Play a single file or pick a random file from a folder",
                key="file_source_mode",
                default_value="single-file",
                options=[
                    ("single-file", "Single File"),
                    ("random-folder", "Random From Folder"),
                ],
            )
        )

        rows.append(
            self._setting_path_row(
                title="Audio file",
                subtitle="Choose the file to play",
                path_getter=lambda: (self.settings.get("audio_file_path") or "").strip(),
                on_browse=lambda: self._open_audio_file_dialog(
                    self.settings.get("audio_file_path"),
                    self._set_audio_file_path,
                ),
                on_clear=lambda: self._set_audio_file_path(""),
            )
        )

        range_row = Adw.ActionRow(title="Playback range")
        range_row.set_subtitle(self._playback_range_subtitle())
        edit_range_button = Gtk.Button(label="Edit", valign=Gtk.Align.CENTER)
        clear_range_button = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)

        edit_range_button.connect("clicked", lambda *_args: self._open_range_editor())
        clear_range_button.connect("clicked", lambda *_args: self._clear_playback_range())

        range_row.add_suffix(clear_range_button)
        range_row.add_suffix(edit_range_button)
        rows.append(range_row)

        rows.append(
            self._setting_path_row(
                title="Random folder",
                subtitle="Choose the folder used for random playback",
                path_getter=lambda: (self.settings.get("audio_folder_path") or "").strip(),
                on_browse=lambda: self._open_folder_dialog(
                    self.settings.get("audio_folder_path"),
                    self._set_audio_folder_path,
                ),
                on_clear=lambda: self._set_audio_folder_path(""),
            )
        )

        rows.append(self._sink_combo_row())
        rows.append(self._playback_mode_row())
        return rows

    def _set_audio_file_path(self, path: str) -> None:
        self.settings["audio_file_path"] = (path or "").strip()
        self.settings.pop("playback_range_start", None)
        self.settings.pop("playback_range_end", None)
        self.set_settings(self.settings)
        self._refresh_labels()

    def _set_audio_folder_path(self, path: str) -> None:
        self.settings["audio_folder_path"] = (path or "").strip()
        self.set_settings(self.settings)
        self._refresh_labels()

    def _resolve_selected_path(self) -> str:
        source_mode = (self.settings.get("file_source_mode") or "single-file").strip() or "single-file"
        if source_mode == "random-folder":
            files = _list_audio_files((self.settings.get("audio_folder_path") or "").strip())
            if not files:
                return ""
            return random.choice(files)
        return (self.settings.get("audio_file_path") or "").strip()

    def _playback_range(self) -> tuple[float | None, float | None]:
        source_mode = (self.settings.get("file_source_mode") or "single-file").strip() or "single-file"
        if source_mode != "single-file":
            return None, None

        start = self.settings.get("playback_range_start")
        end = self.settings.get("playback_range_end")
        try:
            start_value = float(start) if start is not None else None
        except Exception:
            start_value = None
        try:
            end_value = float(end) if end is not None else None
        except Exception:
            end_value = None

        if start_value is None or end_value is None or end_value <= start_value:
            return None, None
        return start_value, end_value

    def _playback_range_subtitle(self) -> str:
        source_mode = (self.settings.get("file_source_mode") or "single-file").strip() or "single-file"
        if source_mode != "single-file":
            return "Available for single-file playback"

        path = (self.settings.get("audio_file_path") or "").strip()
        if not path:
            return "Select an audio file first"

        start, end = self._playback_range()
        if start is None or end is None:
            return "Play the full file"
        return f"Play {start:.2f}s → {end:.2f}s"

    def _set_playback_range(self, start_sec: float | None, end_sec: float | None) -> None:
        if start_sec is None or end_sec is None or end_sec <= start_sec:
            self.settings.pop("playback_range_start", None)
            self.settings.pop("playback_range_end", None)
        else:
            self.settings["playback_range_start"] = round(float(start_sec), 4)
            self.settings["playback_range_end"] = round(float(end_sec), 4)
        self.set_settings(self.settings)
        self._refresh_labels()

    def _clear_playback_range(self) -> None:
        self._set_playback_range(None, None)

    def _open_range_editor(self) -> None:
        path = (self.settings.get("audio_file_path") or "").strip()
        if not path or not os.path.isfile(path):
            log.warning("Range editor requires a selected audio file")
            return

        preview_sink = self._preview_sink() or ""
        exe = _resolve_clippying_exe(self._clippying_exe())
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log.warning("Range editor requires ffmpeg")
            return

        def work():
            try:
                start_sec, end_sec = self._playback_range()
                trimmer_args = [
                    exe,
                    "--stdin-pcm",
                    "48000",
                    "1",
                    "--emit-selection",
                ]
                if preview_sink:
                    trimmer_args.extend(["--preview-sink", preview_sink])
                if start_sec is not None and end_sec is not None and end_sec > start_sec:
                    trimmer_args.extend(
                        [
                            "--selection-start",
                            f"{start_sec:.6f}",
                            "--selection-end",
                            f"{end_sec:.6f}",
                        ]
                    )

                ffmpeg_proc = subprocess.Popen(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-i",
                        path,
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "-ac",
                        "1",
                        "-ar",
                        "48000",
                        "-",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                trimmer_proc = subprocess.Popen(
                    trimmer_args,
                    stdin=ffmpeg_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                if ffmpeg_proc.stdout is not None:
                    ffmpeg_proc.stdout.close()

                trimmer_stdout, trimmer_stderr = trimmer_proc.communicate()
                ffmpeg_stderr = ""
                if ffmpeg_proc.stderr is not None:
                    ffmpeg_stderr = ffmpeg_proc.stderr.read().decode("utf-8", errors="replace")
                ffmpeg_rc = ffmpeg_proc.wait()

                if trimmer_proc.returncode != 0:
                    log.error(f"Range editor failed: {trimmer_stderr.strip()}")
                    return
                if ffmpeg_rc != 0:
                    log.error(f"ffmpeg decode failed: {ffmpeg_stderr.strip()}")
                    return

                for line in trimmer_stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if payload.get("type") != "selection_saved":
                        continue
                    start_sec = payload.get("start")
                    end_sec = payload.get("end")
                    GLib.idle_add(self._apply_playback_range, start_sec, end_sec)
                    return
            except Exception as e:
                log.error(f"Failed to open range editor: {e}")

        threading.Thread(target=work, daemon=True, name="clippying-range-editor").start()

    def _apply_playback_range(self, start_sec: float, end_sec: float) -> bool:
        self._set_playback_range(start_sec, end_sec)
        return False

    def _play_selected(self, loop: bool) -> None:
        path = self._resolve_selected_path()
        if not path:
            log.debug("No file available for file-player action")
            return
        start_sec, end_sec = self._playback_range()
        if not self._player.play(
            path,
            sink=self._selected_sink(),
            loop=loop,
            start_sec=start_sec,
            end_sec=end_sec,
        ):
            log.warning(f"Failed to start file playback for {path}")

    def _play_selected_overlap(self) -> None:
        path = self._resolve_selected_path()
        if not path:
            return
        start_sec, end_sec = self._playback_range()
        if not self._player.play_overlap(
            path,
            sink=self._selected_sink(),
            start_sec=start_sec,
            end_sec=end_sec,
        ):
            log.warning(f"Failed to overlap file playback for {path}")

    def _refresh_labels(self) -> None:
        source_mode = (self.settings.get("file_source_mode") or "single-file").strip() or "single-file"
        if source_mode == "random-folder":
            folder = (self.settings.get("audio_folder_path") or "").strip()
            top = "Random Folder"
            bottom = os.path.basename(folder) if folder else ""
        else:
            path = (self.settings.get("audio_file_path") or "").strip()
            top = os.path.basename(path) if path else "No file"
            start, end = self._playback_range()
            if start is not None and end is not None:
                bottom = f"{start:.1f}-{end:.1f}s"
            else:
                bottom = os.path.basename(os.path.dirname(path)) if path else ""

        def apply():
            self.set_top_label(top[:30], font_size=14)
            self.set_center_label("File", font_size=14)
            self.set_bottom_label(bottom[:30], font_size=12)
            return False

        GLib.idle_add(apply)


def notify_plugin_settings_changed() -> None:
    with _ACTIVE_ACTIONS_LOCK:
        actions = list(_ACTIVE_ACTIONS)
    for action in actions:
        try:
            action.on_plugin_settings_changed()
        except Exception as e:
            log.debug(f"plugin settings refresh failed: {e}")
