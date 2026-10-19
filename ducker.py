"""
Ducker
By Jacob Hammersley

Turns your music down, mutes it or pauses it whenever something else makes a
sound, and brings it back when the sound stops.

Sources are what the app listens to: the music you play, and the microphone.
Exceptions are apps and websites that never cause a duck. Anything else that
makes a sound counts as "other sound" and triggers the duck.

Run with --minimized to start hidden in the system tray.
"""

import asyncio
import ctypes
import json
import logging
import math
import os
import shutil
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

import customtkinter as ctk
import psutil
from comtypes import CoInitialize, CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

APP_NAME = "Ducker"
OLD_APP_NAMES = ("Music Ducker", "Auto Duck", "Spotify Auto Duck")

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "duck_state.json"
LOG_FILE = DATA_DIR / "ducker.log"
if not SETTINGS_FILE.exists():
    for old_name in OLD_APP_NAMES:
        old_settings = Path(os.environ.get("APPDATA", Path.home())) / old_name / "settings.json"
        if old_settings.exists():
            try:
                shutil.copy2(old_settings, SETTINGS_FILE)
                break
            except Exception:
                pass

TICK = 0.1

ALWAYS_IGNORE = {"explorer.exe", "shellexperiencehost.exe", "audiodg.exe",
                 "startmenuexperiencehost.exe", "searchhost.exe"}
OWN_PID = os.getpid()
TEST_MUSIC_PIDS = set()     # launcher pids of children playing the tutorial source
TEST_OTHER_PIDS = set()     # launcher pids of children playing the tutorial sound
_cmdline_cache = {}
SEEN_EXES = set()           # every program that has ever had an audio session while Ducker ran


def test_role(process):
    """'music', 'voice' or None, from what the process was started to play.
    The built app starts a launcher which starts the real program, so the
    process tree is unreliable. The command line is passed down unchanged."""
    pid = process.pid
    if pid in _cmdline_cache:
        return _cmdline_cache[pid]
    role = None
    try:
        line = " ".join(process.cmdline()).lower()
        if "--play-test-music" in line:
            role = "music"
        elif "--play-test-voice" in line:
            role = "voice"
    except Exception:
        role = None
    if role is not None:
        _cmdline_cache[pid] = role
    if len(_cmdline_cache) > 500:
        _cmdline_cache.clear()
    return role

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
            "opera_gx.exe", "vivaldi.exe", "arc.exe", "chromium.exe"}

KNOWN_PLAYERS = {
    "spotify.exe": "Spotify", "applemusic.exe": "Apple Music", "itunes.exe": "iTunes",
    "tidal.exe": "TIDAL", "amazon music.exe": "Amazon Music", "deezer.exe": "Deezer",
    "youtube music.exe": "YouTube Music", "musicbee.exe": "MusicBee",
    "foobar2000.exe": "foobar2000", "vlc.exe": "VLC", "winamp.exe": "Winamp",
    "aimp.exe": "AIMP", "qobuz.exe": "Qobuz", "soundcloud.exe": "SoundCloud",
    "wmplayer.exe": "Windows Media Player", "music.ui.exe": "Groove Music",
    "mediaplayer.exe": "Media Player", "clementine.exe": "Clementine",
    "strawberry.exe": "Strawberry", "dopamine.exe": "Dopamine", "plexamp.exe": "Plexamp",
    "roon.exe": "Roon", "audirvana.exe": "Audirvana", "mediamonkey.exe": "MediaMonkey",
    "napster.exe": "Napster", "pandora.exe": "Pandora", "iheartradio.exe": "iHeartRadio",
}

DEFAULT_SOURCES = [
    {"id": "spotify", "type": "app", "name": "Spotify", "exe": "spotify.exe", "path": "",
     "package": "spotifyab.spotifymusic", "enabled": True, "builtin": True},
    {"id": "applemusic", "type": "app", "name": "Apple Music", "exe": "applemusic.exe", "path": "",
     "package": "appleinc.applemusicwin", "enabled": True, "builtin": True},
    {"id": "ytmusic-app", "type": "site", "name": "YouTube Music app", "domain": "music.youtube.com",
     "keyword": "youtube music", "enabled": True, "builtin": True},
    {"id": "ytmusic-web", "type": "site", "name": "music.youtube.com", "domain": "music.youtube.com",
     "keyword": "youtube music", "enabled": True, "builtin": True},
]

# Sensitivity in plain words. The number is how loud a sound must be (0 to 1).
SENSITIVITY = {"quiet": 0.002, "normal": 0.01, "loud": 0.05}
MIC_SENSITIVITY = {"quiet": 0.02, "normal": 0.08, "loud": 0.2}

# The three built-in ducks. "action" is what happens to the music.
PRESETS = [
    {"id": "complete", "name": "Pause", "builtin": True, "action": "pause",
     "reduce": 100, "fade_down": 0.0, "fade_up": 0.0, "quiet": 1.0, "threshold": 0.01,
     "blurb": "The source pauses while another sound plays. When the sound stops, the source plays again from where it left off."},
    {"id": "muted", "name": "Mute", "builtin": True, "action": "mute",
     "reduce": 100, "fade_down": 0.0, "fade_up": 0.0, "quiet": 1.0, "threshold": 0.01,
     "blurb": "The source goes silent while another sound plays, but keeps running underneath. When the sound stops, you hear it again."},
    {"id": "smooth", "name": "Fade", "builtin": True, "action": "volume",
     "reduce": 80, "fade_down": 0.5, "fade_up": 1.5, "quiet": 1.0, "threshold": 0.01,
     "blurb": "The source fades down to 20% while another sound plays, then fades back up when the sound stops."},
]

NEW_DUCK = {"action": "volume", "reduce": 80, "fade_down": 0.5, "fade_up": 1.5,
            "quiet": 0.0, "threshold": 0.01}

DEFAULT_SETTINGS = {
    "enabled": True,
    "mode": "smooth",
    "sources": [dict(item) for item in DEFAULT_SOURCES],
    "mic": {"enabled": False, "devices": [], "device_settings": {}},   # devices: ids switched on; device_settings: id -> threshold
    "removed_sources": [],
    "seen_apps": [],
    "exceptions": [{"type": "app", "name": "Discord", "exe": "discord.exe", "path": ""}],
    "ducks": [],                 # the user's own ducks
    "seen_welcome": False,
    "seen_tray_hint": False,
}

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _sensitivity_word(value):
    """Turn an old numeric threshold into one of the three words."""
    if isinstance(value, str):
        return value if value in SENSITIVITY else "normal"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "normal"
    return min(SENSITIVITY, key=lambda k: abs(SENSITIVITY[k] - value))


def load_settings():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        for key, value in saved.items():
            if key in ("sources", "custom", "mic"):
                continue
            settings[key] = value
        settings["mic"].update(saved.get("mic", {}))

        # Older versions had one "custom" duck. Keep it as the first of the user's ducks.
        old_custom = saved.get("custom")
        if old_custom and not saved.get("ducks"):
            settings["ducks"] = [{
                "id": "custom", "name": "My Duck 1", "builtin": False,
                "action": old_custom.get("action", "volume"),
                "reduce": int(old_custom.get("reduce", 60)),
                "fade_down": float(old_custom.get("fade_down", 0.8)),
                "fade_up": float(old_custom.get("fade_up", 2.0)),
                "quiet": float(old_custom.get("quiet", 2.0)),
                "sensitivity": _sensitivity_word(old_custom.get("threshold", "normal")),
            }]
        for duck in settings["ducks"]:
            duck["sensitivity"] = _sensitivity_word(duck.get("sensitivity", "normal"))
            duck.setdefault("id", uuid.uuid4().hex)
            duck.setdefault("builtin", False)

        if "sources" in saved:
            merged = [dict(item) for item in saved["sources"]]
            have = {item.get("id") for item in merged}
            removed = set(saved.get("removed_sources", []))
            for default in DEFAULT_SOURCES:
                if default["id"] not in have and default["id"] not in removed:
                    merged.append(dict(default))
            # Earlier builds named this one with brackets
            for item in merged:
                if item.get("id") == "ytmusic-app":
                    item["name"] = "YouTube Music app"
            settings["sources"] = merged

        old = saved.get("ignore")
        if old and "exceptions" not in saved:
            settings["exceptions"] = [
                {"type": "app", "name": Path(exe).stem.title(), "exe": exe, "path": ""} for exe in old
            ]
    except Exception:
        pass

    # Make sure the chosen mode still exists
    if settings["mode"] not in {d["id"] for d in all_ducks(settings)}:
        settings["mode"] = "smooth"
    return settings


def save_settings(settings):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception as error:
        logging.warning("Could not save settings: %s", error)


def all_ducks(settings):
    return [dict(p) for p in PRESETS] + list(settings.get("ducks", []))


def current_duck(settings):
    for duck in all_ducks(settings):
        if duck["id"] == settings["mode"]:
            return duck
    return dict(PRESETS[2])


def site_keyword(domain):
    """youtube.com -> youtube, music.youtube.com -> youtube music"""
    domain = domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0]
    parts = [p for p in domain.split(".") if p]
    if not parts:
        return ""
    if len(parts) >= 3 and parts[0] not in ("m", "app", "web", "open", "play", "listen"):
        return f"{parts[1]} {parts[0]}"
    return parts[0]


def duck_threshold(duck):
    if "threshold" in duck:
        try:
            return float(duck["threshold"])
        except (TypeError, ValueError):
            pass
    return SENSITIVITY.get(duck.get("sensitivity", "normal"), 0.01)


def active_profile(settings, override=None):
    """Everything the engine needs for one check."""
    duck = override or current_duck(settings)
    profile = {
        "action": duck.get("action", "volume"),
        "reduce": float(duck.get("reduce", 60)),
        "fade_down": float(duck.get("fade_down", 0.5)),
        "fade_up": float(duck.get("fade_up", 1.5)),
        "quiet": float(duck.get("quiet", 1.0)),
        "threshold": duck_threshold(duck),
    }

    music_exes, music_paths, music_sites = set(), set(), set()
    for item in settings.get("sources", []):
        if not item.get("enabled", True):
            continue
        if item.get("type") == "site":
            keyword = item.get("keyword") or site_keyword(item.get("domain", ""))
            if keyword:
                music_sites.add(keyword)
        else:
            if item.get("exe"):
                music_exes.add(item["exe"].lower())
            if item.get("path"):
                music_paths.add(item["path"].lower())

    ignore_exes, ignore_sites = set(), set()
    for item in settings.get("exceptions", []):
        if item.get("type") == "site":
            keyword = site_keyword(item.get("domain", ""))
            if keyword:
                ignore_sites.add(keyword)
        elif item.get("exe"):
            ignore_exes.add(item["exe"].lower().strip())

    mic = settings.get("mic", {})
    profile.update(music_exes=music_exes, music_paths=music_paths, music_sites=music_sites,
                   ignore=ignore_exes, sites=ignore_sites,
                   mic_enabled=bool(mic.get("enabled")) and override is None,
                   mic_devices=list(mic.get("devices", [])),
                   mic_settings=dict(mic.get("device_settings", {})))
    return profile


# ---------------------------------------------------------------------------
# Windows helpers: windows, titles and media commands
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
WM_APPCOMMAND = 0x0319
APPCOMMAND_MEDIA_PLAY = 46
APPCOMMAND_MEDIA_PAUSE = 47

_pid_names = {}


def process_name(pid):
    name = _pid_names.get(pid)
    if name is None:
        try:
            name = psutil.Process(pid).name().lower()
        except Exception:
            name = ""
        _pid_names[pid] = name
        if len(_pid_names) > 2000:
            _pid_names.clear()
    return name


def window_titles_by_process_name():
    """Lower-case titles of every visible window, grouped by process name."""
    by_pid = {}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        by_pid.setdefault(pid.value, []).append(buffer.value.lower())
        return True

    user32.EnumWindows(callback, 0)
    by_name = {}
    for pid, titles in by_pid.items():
        by_name.setdefault(process_name(pid), []).extend(titles)
    return by_name


def send_window_command(process_names, command):
    """Post a media command to every window belonging to these processes."""
    names = {n.lower() for n in process_names}
    pids = set()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() in names:
                pids.add(proc.pid)
        except Exception:
            pass
    if not pids:
        return False
    sent = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            user32.PostMessageW(hwnd, WM_APPCOMMAND, 0, command << 16)
            sent.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return bool(sent)


class MediaControls:
    """Pause and resume through Windows' own media controls (the system behind
    the keyboard play/pause key). Works for any player that shows up there,
    including browsers. Falls back to sending a command to the player's window."""

    def __init__(self):
        self.available = None
        self.paused_ids = []

    def _load(self):
        if self.available is None:
            try:
                from winrt.windows.media.control import (
                    GlobalSystemMediaTransportControlsSessionManager as Manager)
                self.Manager = Manager
                self.available = True
            except Exception:
                self.available = False
        return self.available

    @staticmethod
    def _matches(session_id, keys):
        session_id = session_id.lower()
        return any(key and key in session_id for key in keys)

    def _run(self, coroutine):
        result = {}

        def worker():
            try:
                result["value"] = asyncio.run(coroutine)
            except Exception as error:
                result["error"] = error

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(3.0)
        return result.get("value")

    async def _pause_async(self, keys):
        manager = await self.Manager.request_async()
        paused = []
        for session in manager.get_sessions():
            try:
                source = session.source_app_user_model_id or ""
                if not self._matches(source, keys):
                    continue
                info = session.get_playback_info()
                if info.playback_status == 4:  # playing
                    if await session.try_pause_async():
                        paused.append(source)
            except Exception:
                pass
        return paused

    async def _play_async(self, ids):
        manager = await self.Manager.request_async()
        for session in manager.get_sessions():
            try:
                source = session.source_app_user_model_id or ""
                if source in ids:
                    await session.try_play_async()
            except Exception:
                pass
        return True

    def pause(self, keys, process_names):
        """keys are bits of app ids to match (e.g. 'spotify', 'chrome')."""
        self.paused_ids = []
        if self._load():
            paused = self._run(self._pause_async(keys)) or []
            if paused:
                self.paused_ids = paused
                return True
        return send_window_command(process_names, APPCOMMAND_MEDIA_PAUSE)

    def play(self, process_names):
        if self.paused_ids and self._load():
            self._run(self._play_async(set(self.paused_ids)))
            self.paused_ids = []
            return
        send_window_command(process_names, APPCOMMAND_MEDIA_PLAY)


# ---------------------------------------------------------------------------
# Audio sessions and the microphone
# ---------------------------------------------------------------------------

def list_microphones():
    """Every microphone Windows knows about: [(id, friendly name, is_default)]."""
    found = []
    try:
        from pycaw.pycaw import AudioUtilities as AU
        from pycaw.constants import EDataFlow, ERole, DEVICE_STATE
        enumerator = AU.GetDeviceEnumerator()
        default_id = None
        try:
            default_id = enumerator.GetDefaultAudioEndpoint(
                EDataFlow.eCapture.value, ERole.eCommunications.value).GetId()
        except Exception:
            pass
        collection = enumerator.EnumAudioEndpoints(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
        for i in range(collection.GetCount()):
            device = collection.Item(i)
            try:
                dev_id = device.GetId()
                name = AU.CreateDevice(device).FriendlyName
            except Exception:
                continue
            found.append((dev_id, name, dev_id == default_id))
    except Exception:
        pass
    return found


class Microphone:
    """Reads how loud each chosen microphone is right now (0 to 1).
    Nothing is recorded; it's the same level meter Windows shows in Settings."""

    def __init__(self):
        self.meters = {}        # device id -> meter
        self.failed_at = 0.0
        self.wanted = None      # set of device ids, or None for the default

    def _connect(self):
        self.meters = {}
        try:
            from pycaw.pycaw import AudioUtilities as AU
            from pycaw.constants import EDataFlow, ERole, DEVICE_STATE
            enumerator = AU.GetDeviceEnumerator()
            devices = []
            if not self.wanted:
                devices.append(enumerator.GetDefaultAudioEndpoint(
                    EDataFlow.eCapture.value, ERole.eCommunications.value))
            else:
                collection = enumerator.EnumAudioEndpoints(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
                for i in range(collection.GetCount()):
                    device = collection.Item(i)
                    if device.GetId() in self.wanted:
                        devices.append(device)
            for device in devices:
                meter = device.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None).QueryInterface(
                    IAudioMeterInformation)
                self.meters[device.GetId()] = meter
        except Exception:
            self.meters = {}
        if not self.meters:
            self.failed_at = time.time()

    def levels(self, wanted=None):
        """{device id: level} for each chosen microphone."""
        wanted = set(wanted) if wanted else None
        if wanted != self.wanted:
            self.wanted = wanted
            self.meters = {}
        if not self.meters:
            if time.time() - self.failed_at > 5:
                self._connect()
            if not self.meters:
                return {}
        out = {}
        for dev_id, meter in list(self.meters.items()):
            try:
                out[dev_id] = float(meter.GetPeakValue())
            except Exception:
                self.meters = {}
                self.failed_at = time.time()
                return {}
        return out


def scan_sessions(profile):
    """Sort every audio session into music, exception or other.
    Returns (music_sessions, music_playing, other_apps_playing, music_names)."""
    music, others, music_names = [], [], set()
    music_playing = False
    titles = None

    for session in AudioUtilities.GetAllSessions():
        process = session.Process
        if process is None or process.pid == OWN_PID:
            continue
        try:
            name = process.name().lower()
        except Exception:
            continue
        try:
            peak = session._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue()
        except Exception:
            peak = 0.0

        SEEN_EXES.add(name)
        if (TEST_MUSIC_PIDS or TEST_OTHER_PIDS) and test_role(process):
            continue        # the tutorial recordings look after themselves
        is_music = name in profile["music_exes"]
        if not is_music and profile["music_paths"]:
            try:
                is_music = (process.exe() or "").lower() in profile["music_paths"]
            except Exception:
                pass
        if not is_music and profile["music_sites"] and name in BROWSERS:
            if titles is None:
                titles = window_titles_by_process_name()
            is_music = any(keyword in title
                           for title in titles.get(name, [])
                           for keyword in profile["music_sites"])

        if is_music:
            music.append(session)
            music_names.add(name)
            if peak > 0.001:
                music_playing = True
            continue

        if name in ALWAYS_IGNORE or name in profile["ignore"] or name == "ducker.exe":
            continue
        if peak <= profile["threshold"]:
            continue
        if profile["sites"] and name in BROWSERS:
            if titles is None:
                titles = window_titles_by_process_name()
            if any(keyword in title for title in titles.get(name, []) for keyword in profile["sites"]):
                continue
        others.append(name)

    return music, music_playing, others, music_names


def set_volume(sessions, level):
    for session in sessions:
        try:
            session.SimpleAudioVolume.SetMasterVolume(max(0.0, min(1.0, level)), None)
        except Exception:
            pass


def set_mute(sessions, muted):
    for session in sessions:
        try:
            session.SimpleAudioVolume.SetMute(1 if muted else 0, None)
        except Exception:
            pass


def session_key(session):
    try:
        return session.Process.pid
    except Exception:
        return id(session)


# ---------------------------------------------------------------------------
# The engine (runs in a background thread)
# ---------------------------------------------------------------------------

class DuckEngine(threading.Thread):
    def __init__(self, settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.lock = threading.Lock()
        self.status = "Starting"
        self.status_detail = ""
        self.last_reason = ""
        self.media = MediaControls()
        self.mic = Microphone()
        self.mic_level = 0.0
        self.mic_levels = {}
        self.override = None            # the tutorial sets this to force one behaviour
        self.test_players = {"music": None, "voice": None}
        self.ducked = False
        self.action = None
        self.saved_volumes = {}
        self.music_names = set()
        self.factor = 1.0
        self.fade_from = None
        self.fade_to = None
        self.fade_start = 0.0
        self.fade_length = 0.0
        self.last_noise = 0.0

    def _set_status(self, status, detail=""):
        with self.lock:
            self.status = status
            self.status_detail = detail

    # ---- remembering what we've done -------------------------------------

    def _write_state(self):
        try:
            if self.ducked:
                STATE_FILE.write_text(json.dumps({
                    "action": self.action,
                    "names": sorted(self.music_names),
                    "volumes": {str(k): v for k, v in self.saved_volumes.items()},
                }))
            elif STATE_FILE.exists():
                STATE_FILE.unlink()
        except Exception:
            pass

    def _undo_leftover_duck(self):
        try:
            if not STATE_FILE.exists():
                return
            state = json.loads(STATE_FILE.read_text())
            names = {n.lower() for n in state.get("names", [])}
            volumes = {int(k): v for k, v in state.get("volumes", {}).items()}
            for session in AudioUtilities.GetAllSessions():
                process = session.Process
                if process is None or process.name().lower() not in names:
                    continue
                if state.get("action") == "mute":
                    set_mute([session], False)
                elif state.get("action") == "volume":
                    set_volume([session], volumes.get(process.pid, 1.0))
            logging.info("Undid a duck left over from the last run")
            STATE_FILE.unlink()
        except Exception as error:
            logging.warning("Could not undo the leftover duck: %s", error)

    def reset_music_volume(self):
        try:
            CoInitialize()
            profile = active_profile(self.settings)
            sessions, _, _, _ = scan_sessions(profile)
            for session in sessions:
                set_mute([session], False)
                set_volume([session], 1.0)
            self.ducked = False
            self.action = None
            self.saved_volumes = {}
            self.fade_to = None
            self.factor = 1.0
            self._write_state()
            logging.info("Music volume reset to full")
        except Exception as error:
            logging.warning("Could not reset the music volume: %s", error)

    # ---- fading ------------------------------------------------------------

    def _begin_fade(self, target, seconds):
        self.fade_from = self.factor
        self.fade_to = target
        self.fade_start = time.time()
        self.fade_length = max(0.0, seconds)

    def _step_fade(self, sessions):
        if self.fade_to is None:
            return
        progress = 1.0 if self.fade_length <= 0 else min(1.0, (time.time() - self.fade_start) / self.fade_length)
        self.factor = self.fade_from + (self.fade_to - self.fade_from) * progress
        for session in sessions:
            saved = self.saved_volumes.get(session_key(session))
            if saved is not None:
                set_volume([session], saved * self.factor)
        if progress >= 1.0:
            self.fade_to = None
            if self.factor >= 0.999:
                self.saved_volumes = {}

    def _remember_volumes(self, sessions):
        if self.fade_to is not None and self.saved_volumes:
            return
        self.saved_volumes = {}
        for session in sessions:
            try:
                self.saved_volumes[session_key(session)] = session.SimpleAudioVolume.GetMasterVolume()
            except Exception:
                pass

    def _player_keys(self):
        keys = set()
        for name in self.music_names:
            stem = name[:-4] if name.endswith(".exe") else name
            keys.add(stem.replace(" ", ""))
            keys.add(stem)
            if name in BROWSERS:
                keys.add(stem.replace("msedge", "edge"))
        return keys

    # ---- ducking -------------------------------------------------------------

    def _duck(self, profile, sessions, music_playing, reason):
        if profile["action"] == "pause":
            if not music_playing:
                return
            if not self.media.pause(self._player_keys(), self.music_names):
                return
            self.action = "pause"
        elif profile["action"] == "mute":
            set_mute(sessions, True)
            self.action = "mute"
        else:
            self._remember_volumes(sessions)
            if not self.saved_volumes:
                return
            if self.fade_to is None:
                self.factor = 1.0
            self._begin_fade(1 - profile["reduce"] / 100.0, profile["fade_down"])
            self.action = "volume"
        self.ducked = True
        self.last_reason = reason
        self._set_status("Ducked", reason)
        self._write_state()
        logging.info("Ducked %s (%s) because of %s", ", ".join(sorted(self.music_names)), self.action, reason)

    def _restore(self, profile, sessions):
        if self.action == "pause":
            self.media.play(self.music_names)
        elif self.action == "mute":
            set_mute(sessions, False)
        elif self.action == "volume":
            self._begin_fade(1.0, profile["fade_up"])
        logging.info("Restored (%s)", self.action)
        self.ducked = False
        self.action = None
        self._write_state()
        self._set_status("Listening")

    # ---- the main loop ----------------------------------------------------------

    def run(self):
        CoInitialize()
        logging.info("%s started", APP_NAME)
        self._undo_leftover_duck()
        self._set_status("Listening")

        while True:
            try:
                profile = active_profile(self.settings, self.override)
                sessions, music_playing, others, names = scan_sessions(profile)
                now = time.time()
                if names:
                    self.music_names = names

                # The microphone: any chosen device louder than its own threshold counts
                if profile["mic_enabled"]:
                    self.mic_levels = self.mic.levels(profile["mic_devices"] or None)
                    for dev_id, level in self.mic_levels.items():
                        threshold = float(profile["mic_settings"].get(dev_id, 0.01))
                        if level > threshold:
                            others.append("your microphone")
                            break
                else:
                    self.mic_levels = {}

                if others:
                    self.last_noise = now

                if not sessions:
                    if self.ducked:
                        self.ducked = False
                        self.action = None
                        self.saved_volumes = {}
                        self.fade_to = None
                        self.factor = 1.0
                        self._write_state()
                    self._set_status("Waiting for a source")

                elif not self.settings["enabled"]:
                    if self.ducked:
                        self._restore(profile, sessions)
                    self._set_status("Switched off")

                else:
                    if others and not self.ducked:
                        reason = ", ".join(sorted(set(others)))
                        self._duck(profile, sessions, music_playing, reason)
                    elif self.ducked and (now - self.last_noise) >= profile["quiet"]:
                        self._restore(profile, sessions)
                    elif not self.ducked and self.fade_to is None:
                        self._set_status("Listening")

                if sessions:
                    self._step_fade(sessions)

            except Exception as error:
                logging.warning("Problem during check: %s", error)

            time.sleep(TICK)

    def restore_now(self):
        """Put the source back at once, for example when Ducker is closing."""
        try:
            profile = active_profile(self.settings, self.override)
            sessions, _, _, _ = scan_sessions(profile)
            if self.ducked and sessions:
                if self.action == "pause":
                    self.media.play(self.music_names)
                elif self.action == "mute":
                    set_mute(sessions, False)
                elif self.action == "volume":
                    for session in sessions:
                        saved = self.saved_volumes.get(session_key(session))
                        if saved is not None:
                            set_volume([session], saved)
            self.ducked = False
            self.fade_to = None
            self._write_state()
        except Exception:
            pass

    def start_test_player(self, which, loop=False):
        """Start a second copy of Ducker playing one of the two test recordings.
        The source always loops. The sound plays once unless loop is set."""
        import subprocess
        self.stop_test_player(which)
        try:
            flag = "--play-test-music" if which == "music" else "--play-test-voice"
            if getattr(sys, "frozen", False):
                command = [sys.executable, flag]
            else:
                command = [sys.executable, str(Path(__file__).resolve()), flag]
            if loop:
                command.append("--loop")
            proc = subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (TEST_MUSIC_PIDS if which == "music" else TEST_OTHER_PIDS).add(proc.pid)
            self.test_players[which] = proc
            return proc
        except Exception as error:
            logging.warning("Could not start the test player: %s", error)
            return None

    def stop_test_player(self, which):
        proc = self.test_players.get(which)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            (TEST_MUSIC_PIDS if which == "music" else TEST_OTHER_PIDS).discard(proc.pid)
            self.test_players[which] = None

    def test_player_running(self, which):
        proc = self.test_players.get(which)
        return proc is not None and proc.poll() is None

TUTORIAL_GAIN_FILE = DATA_DIR / "tutorial_gain.txt"


def play_test_file(name, loop):
    """Run in a second copy of Ducker. Plays sounds/<name>.wav through Windows.

    The source reads tutorial_gain.txt a few times a second and does what it
    says: a number from 0 to 1 is a volume to fade to, "mute" is silence at
    once, and "pause" stops it in place until the next instruction."""
    import winsound
    path = RESOURCE_DIR / "sounds" / f"{name}.wav"
    if not path.exists():
        return
    if name != "music":
        if loop:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            while True:
                time.sleep(1)
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return

    try:
        TUTORIAL_GAIN_FILE.write_text("1.0")
    except Exception:
        pass
    winmm = ctypes.windll.winmm

    def mci(command):
        buffer = ctypes.create_unicode_buffer(256)
        error = winmm.mciSendStringW(command, buffer, 255, None)
        return error, buffer.value

    error, _ = mci(f'open "{path}" type mpegvideo alias src')
    if error:
        # Fall back to the simpler player, which can only change volume
        def set_gain(value):
            level = int(max(0.0, min(1.0, value)) * 0xFFFF)
            winmm.waveOutSetVolume(0, (level << 16) | level)
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        gain = 1.0
        while True:
            try:
                token = TUTORIAL_GAIN_FILE.read_text().strip() or "1.0"
            except Exception:
                token = "1.0"
            target = 0.0 if token in ("mute", "pause") else float(token)
            if abs(target - gain) > 0.002:
                gain += (target - gain) * 0.18
                set_gain(gain)
            time.sleep(0.03)

    mci("setaudio src volume to 1000")
    mci("play src repeat")
    gain = 1.0
    paused = False
    while True:
        try:
            token = TUTORIAL_GAIN_FILE.read_text().strip() or "1.0"
        except Exception:
            token = "1.0"
        if token == "pause":
            if not paused:
                mci("pause src")
                paused = True
            time.sleep(0.03)
            continue
        if paused:
            mci("resume src")
            paused = False
        if token == "mute":
            target, instant = 0.0, True
        else:
            try:
                target, instant = float(token), False
            except ValueError:
                target, instant = 1.0, False
        if abs(target - gain) > 0.002:
            gain = target if instant else gain + (target - gain) * 0.18
            mci(f"setaudio src volume to {int(max(0.0, min(1.0, gain)) * 1000)}")
        time.sleep(0.03)


# ---------------------------------------------------------------------------
# Finding what's installed
# ---------------------------------------------------------------------------

def installed_packages():
    names = set()
    try:
        import winreg
        key_path = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
                    r"\CurrentVersion\AppModel\Repository\Packages")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                names.add(winreg.EnumKey(key, i).lower())
    except OSError:
        pass
    return names


def running_names():
    names = set()
    for proc in psutil.process_iter(["name"]):
        try:
            names.add((proc.info["name"] or "").lower())
        except Exception:
            pass
    return names


class AppIndex:
    """A quiet list of the programs on this PC, built once in the background,
    so typing part of a name offers the real app with its icon."""

    SKIP = ("\\windows\\", "uninstall", "unins0", "\\setup", "installer", "updater",
            "crashhandler", "crashpad", "helper", "vc_redist", "dxsetup")

    def __init__(self):
        self.apps = []
        self.ready = False
        threading.Thread(target=self._build, daemon=True).start()

    def _add(self, found, name, path):
        try:
            path = str(path).strip().strip('"')
            low = path.lower()
            if not low.endswith(".exe") or any(bad in low for bad in self.SKIP):
                return
            if low in found or not os.path.exists(path):
                return
            found[low] = {"type": "app", "name": name.strip(), "exe": os.path.basename(path), "path": path}
        except Exception:
            pass

    def _build(self):
        found = {}
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("WScript.Shell")
        except Exception:
            shell = None

        start_menus = [
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        for base in start_menus:
            if not base.is_dir():
                continue
            for link in base.rglob("*.lnk"):
                if shell is None:
                    break
                try:
                    self._add(found, link.stem, shell.CreateShortcut(str(link)).TargetPath)
                except Exception:
                    pass
            for link in base.rglob("*.url"):
                try:
                    for line in link.read_text(errors="ignore").splitlines():
                        if line.lower().startswith("iconfile="):
                            self._add(found, link.stem, line.split("=", 1)[1])
                except Exception:
                    pass

        import winreg
        roots = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
        for root, key_path in roots:
            try:
                with winreg.OpenKey(root, key_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                                name = winreg.QueryValueEx(sub, "DisplayName")[0]
                                icon = winreg.QueryValueEx(sub, "DisplayIcon")[0]
                                self._add(found, name, icon.split(",")[0])
                        except OSError:
                            pass
            except OSError:
                pass

        for proc in psutil.process_iter(["name", "exe"]):
            try:
                exe = proc.info["exe"]
                if exe and exe.lower() not in found:
                    self._add(found, Path(exe).stem.replace("_", " ").title(), exe)
            except Exception:
                pass

        self.apps = sorted(found.values(), key=lambda a: a["name"].lower())
        self.ready = True

    def search(self, text, limit=6):
        text = text.lower().strip()
        if not text:
            return []
        scored = []
        for app in self.apps:
            name, exe = app["name"].lower(), app["exe"].lower()
            if name.startswith(text):
                scored.append((0, app))
            elif text in name:
                scored.append((1, app))
            elif text in exe:
                scored.append((2, app))
        scored.sort(key=lambda pair: (pair[0], pair[1]["name"].lower()))
        return [app for _, app in scored[:limit]]

    def search_players(self, text, limit=6):
        """Only installed programs from the known music player list."""
        text = text.lower().strip()
        if not text:
            return []
        results = []
        for app in self.apps:
            exe = app["exe"].lower()
            if exe in KNOWN_PLAYERS and exe not in BROWSERS:
                label = KNOWN_PLAYERS[exe]
                if text in label.lower() or text in exe:
                    results.append({**app, "name": label})
        return results[:limit]

    def find_exe(self, exe):
        exe = exe.lower()
        for app in self.apps:
            if app["exe"].lower() == exe:
                return app
        return None


def looks_like_domain(text):
    text = text.strip().lower()
    return " " not in text and "." in text and not text.endswith(".exe") and len(text) > 3


def load_fonts():
    """Register bundled fonts for this process only. Returns the family to use."""
    fonts_dir = RESOURCE_DIR / "fonts"
    loaded = False
    if fonts_dir.is_dir():
        gdi32 = ctypes.windll.gdi32
        for ttf in fonts_dir.glob("*.ttf"):
            try:
                if gdi32.AddFontResourceExW(str(ttf), 0x10, 0) > 0:  # FR_PRIVATE
                    loaded = True
            except Exception:
                pass
    return "Poppins" if loaded else "Segoe UI"




# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

BG = "#1c1c1c"
SURFACE = "#202020"
CARD = "#2b2b2b"
CARD_HOVER = "#323232"
CARD_SELECTED = "#1f2c22"
BORDER = "#3a3a3a"
BORDER_HOVER = "#4a4a4a"
GREEN = "#1DB954"
GREEN_HOVER = "#21d05e"
RED = "#e5484d"
RED_HOVER = "#f2555a"
WHITE = "#ffffff"
GREY = "#c8c8c8"
DIM = "#8a8a8a"

WIN_W, WIN_H = 440, 640

KEY_WORDS = (
    ("Source", "What you are listening to. Usually music, but it can be a podcast, a video or a game. "
               "Ducker turns the source down."),
    ("Sound", "Any other noise on your PC. A video, a call, a game, a notification. Ducker hears it and "
              "keeps it clear. A sound is what makes the source duck."),
    ("Duck", "The dip. When a sound plays, the source gets quieter, pauses or mutes. When the sound stops, "
             "the source comes back."),
    ("Ducker mode", "What the duck does. Pause, Mute, Fade, or one you make yourself."),
    ("Exception", "A sound that is left alone. It never makes the source duck. Discord pings are a good one."),
    ("Input", "Your microphone. Switch it on and talking makes the source duck too."),
)


def work_area():
    try:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        monitor = user32.MonitorFromPoint(point, 2)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    rect = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


APP_KEY = r"Software\Ducker"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Ducker"


def install_location():
    """Where Ducker.exe lives, from the registry or from this very file."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
            location = Path(winreg.QueryValueEx(key, "InstallPath")[0])
        if location.is_dir():
            return location
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def delete_after_exit(paths):
    """Ask Windows to delete folders a few seconds from now, once this app has closed."""
    import subprocess
    commands = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            commands.append(f'rmdir /s /q "{path}"')
        elif path.exists():
            commands.append(f'del /f /q "{path}"')
    if not commands:
        return
    script = "ping 127.0.0.1 -n 5 >nul & " + " & ".join(commands)
    subprocess.Popen(f"cmd /c {script}",
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))


OLD_REGISTRY_KEYS = (
    r"Software\Music Ducker", r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicDucker",
    r"Software\Auto Duck", r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoDuck",
    r"Software\Spotify Auto Duck", r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SpotifyAutoDuck",
)


def uninstall_ducker(wipe, report):
    """Remove shortcuts and registry entries now, and the program folder once the
    app has closed. With wipe, settings, the log and every trace of older
    versions go too."""
    names = (APP_NAME,) + (OLD_APP_NAMES if wipe else ())
    report("Removing shortcuts...")
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("WScript.Shell")
        for folder in ("Programs", "Startup", "Desktop"):
            for name in names:
                (Path(shell.SpecialFolders(folder)) / f"{name}.lnk").unlink(missing_ok=True)
        # the Start Menu folder with Ducker and Ducker - Uninstall in it
        shutil.rmtree(Path(shell.SpecialFolders("Programs")) / APP_NAME, ignore_errors=True)
    except Exception as error:
        logging.warning("Could not remove a shortcut: %s", error)
    report("Removing registry entries...")
    try:
        import winreg
        keys = (UNINSTALL_KEY, APP_KEY) + (OLD_REGISTRY_KEYS if wipe else ())
        for key_path in keys:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as run_key:
                for name in names:
                    try:
                        winreg.DeleteValue(run_key, name)
                    except OSError:
                        pass
        except OSError:
            pass
    except Exception:
        pass
    report("Removing the program files...")
    leftovers = []
    if getattr(sys, "frozen", False):
        registered = None
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
                registered = Path(winreg.QueryValueEx(key, "InstallPath")[0])
        except OSError:
            pass
        exe = Path(sys.executable).resolve()
        if registered is not None and registered.is_dir() and exe.parent == registered:
            leftovers.append(registered)       # the install folder, as recorded by the installer
        else:
            leftovers.append(exe)              # running from somewhere else: remove just this file
        if wipe:
            programs = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs"
            for name in OLD_APP_NAMES:
                if (programs / name).is_dir():
                    leftovers.append(programs / name)
    if wipe:
        leftovers.append(DATA_DIR)
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        for name in OLD_APP_NAMES:
            if (appdata / name).is_dir():
                leftovers.append(appdata / name)
    delete_after_exit(leftovers)
    report("Done. Ducker will close in a moment.")


def scrollbar_when_needed(frame):
    """Show a scrollable frame's scrollbar only while its contents are taller than it."""
    def check():
        try:
            bar = frame._scrollbar
            needed = frame.winfo_reqheight() > frame._parent_canvas.winfo_height() + 2
            shown = bar.winfo_ismapped()
            if needed and not shown:
                bar.grid()
            elif not needed and shown:
                bar.grid_remove()
            frame.after(250, check)
        except Exception:
            return
    frame.after(250, check)


class Dimmer:
    """A dark see-through layer laid over a window while a more important window
    is open. It follows the window about, hides when the window hides, and blocks
    clicks so the important window gets dealt with first."""

    def __init__(self, window, priority=None, owner=None, alpha=0.55):
        self.window = window
        self.priority = priority
        self.owner = owner          # the important window; when it has gone, so has this layer
        self.alive = True
        self.last = None
        self.layer = tk.Toplevel(window)
        self.layer.overrideredirect(True)
        self.layer.transient(window)
        self.layer.configure(bg="black")
        self.layer.attributes("-alpha", alpha)
        self.layer.withdraw()
        window.bind("<FocusIn>", lambda e: self._lift(), add="+")
        self._follow()

    def _lift(self):
        try:
            self.layer.lift()
            if callable(self.priority):
                self.priority()
            elif self.priority is not None and self.priority.winfo_exists():
                self.priority.lift()
        except Exception:
            pass

    def _follow(self):
        if not self.alive:
            return
        try:
            if self.owner is not None and not self.owner.winfo_exists():
                self.close()
                return
            w = self.window
            if w.winfo_exists() and w.winfo_viewable():
                geo = f"{w.winfo_width()}x{w.winfo_height()}+{w.winfo_rootx()}+{w.winfo_rooty()}"
                if geo != self.last:
                    self.last = geo
                    self.layer.geometry(geo)
                if not self.layer.winfo_viewable():
                    self.layer.deiconify()
                    self._lift()
            elif self.layer.winfo_viewable():
                self.layer.withdraw()
            self.layer.after(60, self._follow)
        except Exception:
            self.close()

    def close(self):
        self.alive = False
        try:
            self.layer.destroy()
        except tk.TclError:
            pass


def centre_on_screen(window, w, h):
    """Put a window of w by h unscaled units in the exact centre of the screen.
    CustomTkinter scales the size itself but takes the position in real pixels."""
    scale = window._get_window_scaling()
    left, top_, right, bottom = work_area()
    x = int(left + (right - left - w * scale) / 2)
    y = int(top_ + (bottom - top_ - h * scale) / 2)
    window.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")


def ease_out(t):
    return 1 - (1 - t) ** 3


def reduce_motion():
    try:
        value = ctypes.c_int()
        user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
        return value.value == 0
    except Exception:
        return False


_icon_cache = {}


def icon(name, size=20):
    key = (name, size)
    if key not in _icon_cache:
        try:
            from PIL import Image
            image = Image.open(RESOURCE_DIR / "art" / f"{name}.png")
            _icon_cache[key] = ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))
        except Exception:
            _icon_cache[key] = None
    return _icon_cache[key]


def logo_image(size):
    try:
        from PIL import Image
        image = Image.open(RESOURCE_DIR / "icon.ico")
        image.size = (256, 256)
        image.load()
        return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))
    except Exception:
        return None


def friendly(name):
    if name.lower().endswith(".exe"):
        return KNOWN_PLAYERS.get(name.lower(), name[:-4].replace("_", " ").title())
    return name


def join_names(names):
    names = list(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def threshold_words(value):
    """At the very bottom any noise counts. Above that, show the level as a number."""
    if value <= 0.012:
        return "Any noise"
    return f"Over {value * 100:.0f}%"


def slider_to_threshold(v):
    """0 to 100 on the slider becomes 0.01 to 0.3, growing steadily."""
    return 0.01 * (30 ** (float(v) / 100))


def threshold_to_slider(t):
    t = max(0.01, min(0.3, float(t)))
    return 100 * math.log(t / 0.01) / math.log(30)


def waveform(path, columns=400):
    """One number per column, 0 to 1, from a WAV file. Empty list if missing."""
    try:
        import wave as wave_module
        with wave_module.open(str(path), "rb") as f:
            frames = f.getnframes()
            raw = f.readframes(frames)
            width = f.getsampwidth()
            channels = f.getnchannels()
            rate = f.getframerate()
        count = len(raw) // (width * channels)
        step = max(1, count // columns)
        out = []
        for c in range(columns):
            start = c * step
            peak = 0
            for i in range(start, min(count, start + step), max(1, step // 40)):
                offset = i * width * channels
                sample = int.from_bytes(raw[offset:offset + width], "little", signed=True)
                peak = max(peak, abs(sample))
            out.append(peak / (2 ** (8 * width - 1)))
        top = max(out) or 1
        return [v / top for v in out], count / rate
    except Exception:
        return [], 0.0


class Theme:
    def __init__(self, root):
        family = load_fonts()
        if family not in tkfont.families():
            family = "Segoe UI Variable" if "Segoe UI Variable" in tkfont.families() else "Segoe UI"
        self.family = family
        self.root = root
        self.f = {
            "title": ctk.CTkFont(family=family, size=20, weight="bold"),
            "h2": ctk.CTkFont(family=family, size=15, weight="bold"),
            "h3": ctk.CTkFont(family=family, size=13, weight="bold"),
            "body": ctk.CTkFont(family=family, size=13),
            "small": ctk.CTkFont(family=family, size=12),
            "tiny": ctk.CTkFont(family=family, size=11),
            "label": ctk.CTkFont(family=family, size=11, weight="bold"),
            "button": ctk.CTkFont(family=family, size=13, weight="bold"),
        }

    def label(self, parent, text, font="body", colour=GREY, **kw):
        return ctk.CTkLabel(parent, text=text, font=self.f[font], text_color=colour, **kw)

    def button(self, parent, text, command, style="primary", image=None, **kw):
        styles = {
            "primary": dict(fg_color=GREEN, hover_color=GREEN_HOVER, text_color=BG),
            "secondary": dict(fg_color=CARD, hover_color=CARD_HOVER, text_color=WHITE,
                              border_width=1, border_color=BORDER),
            "ghost": dict(fg_color="transparent", hover_color=CARD, text_color=GREY,
                          border_width=1, border_color=BORDER),
            "red": dict(fg_color=RED, hover_color=RED_HOVER, text_color=WHITE),
            "danger": dict(fg_color="transparent", hover_color="#3a2426", text_color=RED,
                           border_width=1, border_color="#5a2a2d"),
        }
        options = dict(font=self.f["button"], corner_radius=4, height=34, command=command)
        options.update(styles[style])
        options.update(kw)
        if image is not None:
            options["image"] = image
            options["compound"] = "left"
        return ctk.CTkButton(parent, text=text, **options)

    def card(self, parent, **kw):
        options = dict(fg_color=CARD, corner_radius=8, border_width=1, border_color=BORDER)
        options.update(kw)
        return ctk.CTkFrame(parent, **options)

    def hover(self, widgets, on_enter, on_leave):
        state = {"inside": False, "count": 0}

        def entered(_event=None):
            state["count"] += 1
            if not state["inside"]:
                state["inside"] = True
                on_enter()

        def left(_event=None):
            count = state["count"]

            def maybe():
                if state["count"] == count and state["inside"]:
                    state["inside"] = False
                    on_leave()

            self.root.after(40, maybe)

        for widget in widgets:
            widget.bind("<Enter>", entered, add="+")
            widget.bind("<Leave>", left, add="+")


class Wrapper:
    """Keeps paragraph labels wrapped to the width of their window."""

    def __init__(self, window, margin_default=60):
        self.window = window
        self.items = []
        window.bind("<Configure>", self._resize, add="+")

    def add(self, label, margin=60):
        self.items.append((label, margin))
        self._apply(label, margin)
        return label

    def _width(self):
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(self.window)
            return self.window.winfo_width() / scale
        except Exception:
            return WIN_W

    def _apply(self, label, margin):
        try:
            label.configure(wraplength=max(160, int(self._width() - margin)))
        except tk.TclError:
            pass

    def _resize(self, event):
        if event.widget is not self.window:
            return
        self.items = [(l, m) for l, m in self.items if l.winfo_exists()]
        for label, margin in self.items:
            self._apply(label, margin)


class App:
    def __init__(self, settings, engine, start_hidden=False):
        self.settings = settings
        self.engine = engine
        self.tray = None
        self.index = AppIndex()
        self.packages = installed_packages()
        self.running = running_names()
        self.visible = False
        self.page = None
        self.pages = {}
        self.animating = False
        self.welcome = None
        self.picker = None
        self.tutorial = None
        self.maker = None

        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(APP_NAME)
        self.root.minsize(WIN_W, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Unmap>", self._unmapped, add="+")
        self.root.attributes("-alpha", 0.0)
        self.root.bind("<Escape>", lambda e: self._escape())
        ico = RESOURCE_DIR / "icon.ico"
        if ico.exists():
            try:
                self.root.iconbitmap(str(ico))
            except Exception:
                pass
        self.t = Theme(self.root)
        self.wrap = Wrapper(self.root)
        self.enabled_var = tk.BooleanVar(value=settings["enabled"])

        self._build_nav()
        self.stage = ctk.CTkFrame(self.root, fg_color="transparent")
        self.stage.pack(fill="both", expand=True)
        for key, build in (("home", self._build_home), ("settings", self._build_settings),
                           ("picker", self._build_picker), ("uninstall", self._build_uninstall),
                           ("help", self._build_help)):
            frame = ctk.CTkFrame(self.stage, fg_color="transparent")
            build(frame)
            self.pages[key] = frame
        self._show_page("home", animate=False)

        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.update_idletasks()
        self.root.withdraw()
        self._refresh_status()
        self._start_tray()
        if not start_hidden:
            self.root.after(50, self.show)

    # ---- nav bar: name on the left, Active switch on the right ----------------------

    def _build_nav(self):
        """Two versions of the top bar. The front page has the logo and the Active
        switch. Every other page has Done on the left and its name on the right.
        They slide in and out together with the pages."""
        self.nav = ctk.CTkFrame(self.root, fg_color="transparent", height=36)
        self.nav.pack(fill="x", padx=20, pady=(14, 10))
        self.nav.pack_propagate(False)

        home = ctk.CTkFrame(self.nav, fg_color="transparent")
        logo = logo_image(26)
        if logo is not None:
            ctk.CTkLabel(home, text="", image=logo).pack(side="left", padx=(0, 8))
        self.t.label(home, APP_NAME, "h2", WHITE).pack(side="left")
        self.switch = ctk.CTkSwitch(home, text="Active", font=self.t.f["small"], text_color=GREY,
                                    variable=self.enabled_var, command=self.toggle_enabled,
                                    width=90, switch_width=40, switch_height=20, progress_color=GREEN,
                                    button_color="#e6e6e6", button_hover_color=WHITE, fg_color=BORDER)
        self.switch.pack(side="right")

        page = ctk.CTkFrame(self.nav, fg_color="transparent")
        self.t.button(page, "Done", lambda: self._show_page("home"), "secondary", height=28, width=70).pack(side="left")
        self.nav_title = self.t.label(page, "", "h2", WHITE)
        self.nav_title.pack(side="right")

        self.navs = {"home": home, "page": page}
        self.nav_shown = "home"
        home.place(relx=0, rely=0, relwidth=1, relheight=1)

    # ---- pages -------------------------------------------------------------------------

    def _show_page(self, key, animate=True):
        if key == self.page:
            return
        if key == "home":
            self._render_home()
        else:
            self.nav_title.configure(text={"settings": "Settings", "picker": self.picker_label,
                                           "help": "Help", "uninstall": "Uninstall"}[key])
        if key == "settings":
            self._render_settings()
        if key == "uninstall":
            self.uninstall_from = self.page or "home"
            self._render_uninstall()
        old_key, self.page = self.page, key
        new = self.pages[key]
        old = self.pages.get(old_key) if old_key else None
        nav_key = "home" if key == "home" else "page"
        new_nav = self.navs[nav_key]
        old_nav = self.navs[self.nav_shown] if self.nav_shown != nav_key else None
        self.nav_shown = nav_key
        order = ["home", "settings", "picker", "help", "uninstall"]
        if not animate or old is None or self.animating or reduce_motion():
            for frame in list(self.pages.values()) + list(self.navs.values()):
                frame.place_forget()
            new.place(relx=0, rely=0, relwidth=1, relheight=1)
            new_nav.place(relx=0, rely=0, relwidth=1, relheight=1)
            return
        direction = 1 if order.index(key) > order.index(old_key) else -1
        self.animating = True
        new.place(relx=direction, rely=0, relwidth=1, relheight=1)
        if old_nav is not None:
            new_nav.place(relx=direction, rely=0, relwidth=1, relheight=1)
        begun = time.time()

        def step():
            progress = min(1.0, (time.time() - begun) / 0.26)
            e = ease_out(progress)
            old.place_configure(relx=-direction * e)
            new.place_configure(relx=direction * (1 - e))
            if old_nav is not None:
                old_nav.place_configure(relx=-direction * e)
                new_nav.place_configure(relx=direction * (1 - e))
            if progress < 1.0:
                self.root.after(16, step)
            else:
                old.place_forget()
                new.place_configure(relx=0)
                if old_nav is not None:
                    old_nav.place_forget()
                    new_nav.place_configure(relx=0)
                self.animating = False

        step()

    def _escape(self):
        if self.page == "home":
            self.hide()
        elif self.page == "picker":
            self._show_page("settings")
        else:
            self._show_page("home")

    def _scroll(self, page):
        frame = ctk.CTkScrollableFrame(page, fg_color="transparent", scrollbar_button_color=BORDER,
                                       scrollbar_button_hover_color=DIM)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        scrollbar_when_needed(frame)
        return frame

    # ---- HOME --------------------------------------------------------------------------

    def _build_home(self, page):
        inner = ctk.CTkFrame(page, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20, pady=(6, 16))
        grid = ctk.CTkFrame(inner, fg_color="transparent")
        grid.pack(side="bottom", fill="x", pady=(12, 0))
        grid.columnconfigure((0, 1), weight=1, uniform="b")
        self.t.label(inner, "Ducker mode", "h3", WHITE).pack(anchor="w", pady=(4, 6))
        self.tiles = ctk.CTkFrame(inner, fg_color="transparent")
        self.tiles.pack(fill="x")
        self.tiles.columnconfigure((0, 1), weight=1, uniform="t")

        for i, (text, name, action) in enumerate((("Create a Ducker", "down", lambda: self.open_maker(None)),
                                                    ("Tutorial", "speaker", self.open_tutorial),
                                                    ("Settings", "cog", lambda: self._show_page("settings")),
                                                    ("Help", "help", lambda: self._show_page("help")))):
            self.t.button(grid, text, action, "secondary", image=icon(name, 16), height=38).grid(
                row=i // 2, column=i % 2, sticky="ew",
                padx=(0 if i % 2 == 0 else 4, 4 if i % 2 == 0 else 0), pady=(0, 6))

    def _render_home(self):
        for child in self.tiles.winfo_children():
            child.destroy()
        items = [dict(p) for p in PRESETS]
        if self.settings["ducks"]:
            items.extend(self.settings["ducks"])
        else:
            items.append({"id": "__new__", "name": "Create a Ducker", "blurb": "Make your own mode."})
        for i, duck in enumerate(items):
            self._tile(duck, i)
        self._paint_tiles()

    def _tile(self, duck, i):
        key = duck["id"]
        if key == "__new__":
            self._new_tile(duck, i)
            return
        tile = self.t.card(self.tiles, cursor="hand2")
        tile.grid(row=i // 2, column=i % 2, sticky="nsew",
                  padx=(0 if i % 2 == 0 else 4, 4 if i % 2 == 0 else 0), pady=(0, 8))
        top = ctk.CTkFrame(tile, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 0))
        name = self.t.label(top, duck["name"], "h3", WHITE)
        name.pack(side="left")
        if key != "__new__" and not duck.get("builtin", False):
            self.t.button(top, "Edit", lambda dd=duck: self.open_maker(dd), "ghost",
                          height=22, width=46, font=self.t.f["tiny"]).pack(side="right")
        badge = ctk.CTkLabel(top, text="Selected", font=self.t.f["tiny"], text_color=BG, fg_color=GREEN,
                             corner_radius=4, padx=6, pady=0)
        line = self.t.label(tile, duck.get("blurb") or duck_long(duck), "tiny", DIM, justify="left", wraplength=165)
        line.pack(anchor="w", padx=12, pady=(2, 10))
        scale = ctk.ScalingTracker.get_widget_scaling(tile)
        tile.bind("<Configure>", lambda e, lb=line: lb.configure(wraplength=max(100, int(e.width / scale) - 28)))
        for w in (tile, top, name, line):
            w.bind("<Button-1>", lambda e, k=key: self._tile_clicked(k))
        self.t.hover([tile, top, name, line], lambda: tile.configure(border_color=BORDER_HOVER),
                     lambda k=key: self._paint_tile(k))
        tile.badge = badge
        tile.duck = duck
        self.tiles.grid_slaves()
        setattr(tile, "key", key)

    def _new_tile(self, duck, i):
        """The Create a Ducker tile: a dashed outline and a quieter fill than the real modes."""
        tile = tk.Canvas(self.tiles, bg=BG, highlightthickness=0, bd=0, cursor="hand2", width=1, height=1)
        tile.grid(row=i // 2, column=i % 2, sticky="nsew",
                  padx=(0 if i % 2 == 0 else 4, 4 if i % 2 == 0 else 0), pady=(0, 8))
        name = self.t.label(tile, duck["name"], "h3", GREY, fg_color=SURFACE)
        line = self.t.label(tile, duck["blurb"], "tiny", DIM, justify="left", wraplength=165, fg_color=SURFACE)
        scale = ctk.ScalingTracker.get_widget_scaling(tile)
        tile.create_window(12 * scale, 10 * scale, anchor="nw", window=name)
        tile.create_window(12 * scale, 36 * scale, anchor="nw", window=line)
        tile.colour = DIM

        def draw(_event=None):
            w, h = tile.winfo_width(), tile.winfo_height()
            if w < 10:
                return
            line.configure(wraplength=max(100, int(w / scale) - 28))
            tile.delete("frame")
            r = 8 * scale
            points = []
            for cx, cy, a0 in ((w - r - 1, r + 1, -90), (w - r - 1, h - r - 1, 0), (r + 1, h - r - 1, 90), (r + 1, r + 1, 180)):
                for k in range(9):
                    a = math.radians(a0 + k * 90 / 8)
                    points.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
            tile.create_polygon(points, fill=SURFACE, outline=tile.colour, dash=(5, 4), width=1, tags="frame")
            tile.tag_lower("frame")

        tile.bind("<Configure>", draw)
        for w in (tile, name, line):
            w.bind("<Button-1>", lambda e: self.open_maker(None))

        def hover(on):
            tile.colour = GREY if on else DIM
            draw()

        self.t.hover([tile, name, line], lambda: hover(True), lambda: hover(False))
        setattr(tile, "key", "__new__")

    def _paint_tile(self, key):
        for tile in self.tiles.winfo_children():
            if isinstance(tile, tk.Canvas):
                continue
            if getattr(tile, "key", None) == key:
                chosen = self.settings["mode"] == key
                tile.configure(fg_color=CARD_SELECTED if chosen else CARD, border_color=GREEN if chosen else BORDER)
                if chosen:
                    tile.badge.pack(side="right")
                else:
                    tile.badge.pack_forget()

    def _paint_tiles(self):
        for tile in self.tiles.winfo_children():
            self._paint_tile(getattr(tile, "key", None))
        rows = max(1, (len(self.tiles.winfo_children()) + 1) // 2)
        self.tiles.rowconfigure(tuple(range(rows)), uniform="r")

    def _tile_clicked(self, key):
        if key == "__new__":
            self.open_maker(None)
            return
        self.choose_mode(key)

    def paint_now(self, status, detail):
        """The front page no longer shows a status line; the tray tooltip still does."""
        return

    # ---- SETTINGS -------------------------------------------------------------------------

    def _build_settings(self, page):
        self.settings_inner = self._scroll(page)
        self.root.bind("<Configure>", self._settings_resized, add="+")
        self._resize_job = None

    def _settings_resized(self, event):
        if event.widget is not self.root or self.page != "settings":
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(200, self._render_settings)

    def _chips(self, parent, items, on_remove, empty_text):
        """Small boxes with a name and a cross, flowing onto new rows as needed."""
        if not items:
            self.wrap.add(self.t.label(parent, empty_text, "small", DIM, justify="left"), 90).pack(anchor="w", pady=(4, 0))
            return
        available = self.wrap._width() - 100
        row = None
        used = 0
        for item in items:
            text = item.get("name", "")
            width = 8 * len(text) + 44
            if row is None or used + width > available:
                row = ctk.CTkFrame(parent, fg_color="transparent")
                row.pack(fill="x", pady=(4, 0))
                used = 0
            chip = ctk.CTkFrame(row, fg_color=SURFACE, corner_radius=4, border_width=1, border_color=BORDER)
            chip.pack(side="left", padx=(0, 6))
            self.t.label(chip, text, "small", GREY).pack(side="left", padx=(8, 4), pady=3)
            x = ctk.CTkLabel(chip, text="✕", font=self.t.f["tiny"], text_color=DIM, cursor="hand2", width=14)
            x.pack(side="left", padx=(0, 6))
            x.bind("<Button-1>", lambda e, i=item: on_remove(i))
            self.t.hover([x], lambda x=x: x.configure(text_color=WHITE), lambda x=x: x.configure(text_color=DIM))
            used += width

    def _setting_card(self, parent, icon_name, title, button_text, on_click, intro):
        card = self.t.card(parent)
        card.pack(fill="x", padx=8, pady=(0, 8))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(top, text="", image=icon(icon_name, 18), width=18).pack(side="left", padx=(0, 8))
        self.t.label(top, title, "h3", WHITE).pack(side="left")
        if button_text:
            self.t.button(top, button_text, on_click, "secondary", height=26, width=110).pack(side="right")
        if intro:
            self.wrap.add(self.t.label(card, intro, "tiny", DIM, justify="left"), 90).pack(anchor="w", padx=12, pady=(2, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=12, pady=(2, 10))
        return body

    def _installed_sources(self):
        return [x for x in self.settings["sources"] if self._source_installed(x)]

    def _render_settings(self):
        self._resize_job = None
        for child in self.settings_inner.winfo_children():
            child.destroy()
        s = self.settings
        inner = self.settings_inner

        body = self._setting_card(inner, "headphones", "Sources", "Add source", lambda: self._open_picker("sources"),
                                  "What Ducker turns down when a sound plays.")
        self._chips(body, self._installed_sources(), self._remove_source, "Add a source which will be ducked.")

        body = self._setting_card(inner, "ignore", "Exceptions", "Add exception", lambda: self._open_picker("exceptions"),
                                  "Sounds that Ducker leaves alone.")
        self._chips(body, s["exceptions"], self._remove_exception,
                    "Add a source that should be unaffected by Ducker. When audio from any source is playing, "
                    "audio from the exception stays unchanged.")

        # Input
        mic_on = bool(s["mic"].get("enabled"))
        card = self.t.card(inner)
        card.pack(fill="x", padx=8, pady=(0, 8))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(top, text="", image=icon("mic", 18), width=18).pack(side="left", padx=(0, 8))
        self.t.label(top, "Input", "h3", WHITE).pack(side="left")
        self.mic_var = tk.BooleanVar(value=mic_on)
        ctk.CTkSwitch(top, text="", variable=self.mic_var, width=40, switch_width=36, switch_height=18,
                      progress_color=GREEN, button_color="#e6e6e6", fg_color=BORDER,
                      command=self._mic_toggled).pack(side="right")
        self.wrap.add(self.t.label(card, "You may want to duck audio when you talk, for example over music or "
                                         "gameplay. Turn this on and tick the input devices that should cause a duck.",
                                   "tiny", DIM, justify="left"), 90).pack(anchor="w", padx=12, pady=(2, 0))
        if mic_on:
            self.mic_detail = ctk.CTkFrame(card, fg_color="transparent")
            self.mic_detail.pack(fill="x", padx=12, pady=(6, 10))
            self._render_mic_detail()
        else:
            ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(6, 10))
        self.t.button(row, "Reset to defaults", self._reset_defaults, "ghost", height=30).pack(side="left")
        self.t.button(inner, "Uninstall Ducker", lambda: self._show_page("uninstall"), "red", height=34).pack(fill="x", padx=8, pady=(4, 10))

    def _reset_defaults(self):
        s = self.settings
        s["sources"] = [dict(item) for item in DEFAULT_SOURCES]
        s["removed_sources"] = []
        s["exceptions"] = [dict(item) for item in DEFAULT_SETTINGS["exceptions"]]
        s["mic"] = {"enabled": False, "devices": [], "device_settings": {}}
        s["mode"] = "smooth"
        save_settings(s)
        self._render_settings()

    def _render_mic_detail(self):
        for child in self.mic_detail.winfo_children():
            child.destroy()
        devices = list_microphones()
        chosen = set(self.settings["mic"].get("devices", []))
        device_settings = self.settings["mic"].setdefault("device_settings", {})
        self.mic_meters = {}
        if not devices:
            self.t.label(self.mic_detail, "No input device found.", "small", DIM).pack(anchor="w")
            return
        for dev_id, name, is_default in devices:
            box = ctk.CTkFrame(self.mic_detail, fg_color=SURFACE, corner_radius=4)
            box.pack(fill="x", pady=(0, 4))
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=6)
            var = tk.BooleanVar(value=(dev_id in chosen) if chosen else is_default)
            ctk.CTkCheckBox(row, text=name, variable=var, font=self.t.f["small"], text_color=WHITE,
                            fg_color=GREEN, hover_color=GREEN_HOVER, border_color=DIM, corner_radius=4,
                            command=lambda d=dev_id, v=var: self._mic_device_toggled(d, v)).pack(side="left")
            gear = ctk.CTkButton(row, text="Settings", image=icon("cog", 12), compound="left",
                                 font=self.t.f["tiny"], fg_color="transparent", hover_color=CARD_HOVER,
                                 text_color=GREY, height=24, width=80, corner_radius=4)
            gear.pack(side="right")
            detail = ctk.CTkFrame(box, fg_color="transparent")
            gear.configure(command=lambda d=detail, b=box: self._toggle_mic_settings(d))
            # inside the settings: a level bar and the sensitivity slider
            meter_row = ctk.CTkFrame(detail, fg_color="transparent")
            meter_row.pack(fill="x", padx=8)
            self.t.label(meter_row, "Level now", "tiny", DIM).pack(side="left", padx=(0, 8))
            meter = ctk.CTkProgressBar(meter_row, progress_color=GREEN, fg_color=BORDER, height=8, corner_radius=4)
            meter.set(0)
            meter.pack(side="left", fill="x", expand=True)
            self.mic_meters[dev_id] = meter
            head = ctk.CTkFrame(detail, fg_color="transparent")
            head.pack(fill="x", padx=8, pady=(8, 0))
            self.t.label(head, "Sensitivity", "tiny", DIM).pack(side="left")
            value_label = self.t.label(head, "", "tiny", WHITE)
            value_label.pack(side="right")
            current = float(device_settings.get(dev_id, 0.01))
            slider = ctk.CTkSlider(detail, from_=0, to=100, number_of_steps=100, progress_color=GREEN,
                                   button_color="#e6e6e6", button_hover_color=WHITE, fg_color=BORDER, height=14,
                                   command=lambda v, d=dev_id, l=value_label: self._mic_sensitivity_changed(d, v, l))
            slider.set(threshold_to_slider(current))
            slider.pack(fill="x", padx=8, pady=(2, 0))
            value_label.configure(text=threshold_words(current))
            self.wrap.add(self.t.label(detail, "How loud you must be before the source ducks. At the left, "
                                               "any noise counts, even breathing. Slide right so only speaking counts. "
                                               "Talk and watch the level bar to find the right spot.",
                                       "tiny", DIM, justify="left"), 130).pack(anchor="w", padx=8, pady=(4, 6))

    def _toggle_mic_settings(self, detail):
        if detail.winfo_ismapped():
            detail.pack_forget()
        else:
            detail.pack(fill="x", pady=(0, 4))

    def _mic_sensitivity_changed(self, dev_id, v, label):
        value = slider_to_threshold(v)
        self.settings["mic"].setdefault("device_settings", {})[dev_id] = round(value, 4)
        label.configure(text=threshold_words(value))
        save_settings(self.settings)

    def _mic_toggled(self):
        self.settings["mic"]["enabled"] = bool(self.mic_var.get())
        save_settings(self.settings)
        self._render_settings()

    def _mic_device_toggled(self, dev_id, var):
        chosen = set(self.settings["mic"].get("devices", []))
        if not chosen:
            chosen = {d for d, _, is_default in list_microphones() if is_default}
        if var.get():
            chosen.add(dev_id)
        else:
            chosen.discard(dev_id)
        self.settings["mic"]["devices"] = sorted(chosen)
        save_settings(self.settings)

    def _remove_source(self, item, rerender=True):
        self.settings["sources"] = [x for x in self.settings["sources"] if x is not item]
        if item.get("builtin"):
            removed = set(self.settings.setdefault("removed_sources", []))
            removed.add(item["id"])
            self.settings["removed_sources"] = sorted(removed)
        save_settings(self.settings)
        if rerender:
            self._render_settings()

    def _remove_exception(self, item):
        self.settings["exceptions"] = [x for x in self.settings["exceptions"] if x is not item]
        save_settings(self.settings)
        self._render_settings()

    def _source_installed(self, item):
        if item.get("type") == "site":
            return True
        exe = item.get("exe", "").lower()
        if item.get("path") and os.path.exists(item["path"]):
            return True
        if exe in self.running or exe in SEEN_EXES or exe in self.settings.get("seen_apps", []):
            return True
        if self.index.ready and self.index.find_exe(exe):
            return True
        package = item.get("package", "")
        if package:
            if any(name.startswith(package) for name in self.packages):
                return True
            try:
                base = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
                if any(p.name.lower().startswith(package) for p in base.iterdir()):
                    return True
            except Exception:
                pass
        return False

    # ---- PICKER: add a source or an exception ----------------------------------------------

    def _build_picker(self, page):
        self.picker_label = "Add"
        self.picker_intro = self.wrap.add(self.t.label(page, "", "small", DIM, justify="left"), 40)
        self.picker_intro.pack(anchor="w", padx=20, pady=(2, 8))
        self.search_var = tk.StringVar()
        self.search_entry = ctk.CTkEntry(page, textvariable=self.search_var, font=self.t.f["body"],
                                         fg_color=CARD, border_color=BORDER, text_color=WHITE, corner_radius=4,
                                         height=34, placeholder_text="Type a name")
        self.search_entry.pack(fill="x", padx=20)
        self.search_entry.bind("<KeyRelease>", self._search_changed, add="+")
        self.search_entry.bind("<Return>", lambda e: self._add_first_result(), add="+")
        self.results_box = self._scroll(page)

    def _open_picker(self, which):
        self.picker = which
        if which == "sources":
            self.picker_label = "Add source"
            self.picker_intro.configure(text="Type the name of a music app on this PC, or a website address such as soundcloud.com. "
                                             "Pick it from the list and it is added.")
        else:
            self.picker_label = "Add exception"
            self.picker_intro.configure(text="Type the name of any app on this PC, or a website address such as youtube.com. "
                                             "Pick it from the list and it is added.")
        self.search_var.set("")
        for child in self.results_box.winfo_children():
            child.destroy()
        self._show_page("picker")
        self.root.after(250, self.search_entry.focus_set)

    def _results_for(self, text):
        text = text.strip()
        if not text:
            return []
        results = []
        if looks_like_domain(text):
            domain = text.lower().replace("https://", "").replace("http://", "").strip("/")
            results.append({"type": "site", "name": domain, "domain": domain})
        if self.picker == "sources":
            found = self.index.search_players(text)
            seen = {r["exe"].lower() for r in found}
            # Built-in players that are on this PC but not in the index (Store apps, for example)
            for default in DEFAULT_SOURCES:
                if default["type"] == "app" and default["exe"] not in seen and text.lower() in default["name"].lower():
                    if self._source_installed(default):
                        found.append(dict(default))
                        seen.add(default["exe"])
            for r in self.index.search(text):
                if r["exe"].lower() not in seen:
                    found.append(r)
                    seen.add(r["exe"].lower())
            results.extend(found[:8])
        else:
            results.extend(self.index.search(text))
        return results

    def _search_changed(self, event=None):
        if event is not None and event.keysym in ("Return", "Escape", "Up", "Down"):
            return
        for child in self.results_box.winfo_children():
            child.destroy()
        text = self.search_var.get().strip()
        results = self._results_for(text)
        if not results:
            if text and not self.index.ready:
                self.t.label(self.results_box, "Still reading the apps on this PC. Try again in a moment.", "small", DIM).pack(anchor="w", padx=8, pady=6)
            elif text:
                self.wrap.add(self.t.label(self.results_box, "Nothing on this PC matches that. Check the spelling, or type a website address instead.", "small", DIM, justify="left"), 60).pack(anchor="w", padx=8, pady=6)
            return
        for item in results:
            is_site = item.get("type") == "site"
            row = self.t.card(self.results_box, corner_radius=4, cursor="hand2")
            row.pack(fill="x", padx=8, pady=(0, 4))
            pic = ctk.CTkLabel(row, text="", image=icon("web" if is_site else "app", 16), width=16)
            pic.pack(side="left", padx=(10, 8), pady=8)
            text_box = ctk.CTkFrame(row, fg_color="transparent")
            text_box.pack(side="left", fill="x", expand=True, pady=4)
            a = self.t.label(text_box, item["domain"] if is_site else item["name"], "body", WHITE, anchor="w", height=16)
            a.pack(anchor="w")
            b = self.t.label(text_box, "Website, counts when the tab is open" if is_site else "App", "tiny", DIM, anchor="w", height=13)
            b.pack(anchor="w")
            add = self.t.button(row, "Add", lambda i=item: self._add_item(i), "primary", height=26, width=60)
            add.pack(side="right", padx=(0, 8))
            for w in (row, pic, text_box, a, b):
                w.bind("<Button-1>", lambda e, i=item: self._add_item(i))
            self.t.hover([row, pic, text_box, a, b], lambda r=row: r.configure(fg_color=CARD_HOVER),
                         lambda r=row: r.configure(fg_color=CARD))

    def _add_first_result(self):
        results = self._results_for(self.search_var.get())
        if results:
            self._add_item(results[0])

    def _add_item(self, item, go_back=True):
        target = self.settings["sources"] if self.picker == "sources" else self.settings["exceptions"]
        duplicate = False
        for existing in target:
            if item.get("type") == "site":
                duplicate = existing.get("type") == "site" and existing.get("domain") == item.get("domain")
            else:
                duplicate = existing.get("type") != "site" and existing.get("exe", "").lower() == item.get("exe", "").lower()
            if duplicate:
                break
        if not duplicate:
            new = dict(item)
            if self.picker == "sources":
                new["enabled"] = True
                new.setdefault("id", uuid.uuid4().hex)
                new.setdefault("builtin", False)
                removed = [r for r in self.settings.get("removed_sources", []) if r != new["id"]]
                self.settings["removed_sources"] = removed
            target.append(new)
            save_settings(self.settings)
        if go_back:
            self._show_page("settings")

    # ---- CREATE A DUCKER --------------------------------------------------------------------

    def _delete_duck(self, duck):
        self.settings["ducks"] = [d for d in self.settings["ducks"] if d["id"] != duck["id"]]
        if self.settings["mode"] == duck["id"]:
            self.settings["mode"] = "smooth"
        save_settings(self.settings)
        if self.page == "home":
            self._render_home()
        if self.tutorial is not None:
            self.tutorial.refresh_panel()

    def next_duck_name(self):
        names = {d["name"] for d in self.settings["ducks"]}
        if "My first Ducker" not in names and not self.settings["ducks"]:
            return "My first Ducker"
        n = len(self.settings["ducks"]) + 1
        while f"Ducker {n}" in names:
            n += 1
        return f"Ducker {n}"

    def open_maker(self, duck, over=None):
        if self.maker is not None and self.maker.win is not None and self.maker.win.winfo_exists():
            self.maker.win.lift()
            return
        try:
            self.maker = MakerWindow(self, duck, over)
        except Exception:
            self.maker = None

    def duck_saved(self):
        self.maker = None
        if self.page == "home":
            self._render_home()
        if self.tutorial is not None:
            self.tutorial.refresh_panel()

    # ---- UNINSTALL ----------------------------------------------------------------------------

    def _build_uninstall(self, page):
        inner = self._scroll(page)
        self.uninstall_inner = inner
        self.uninstall_from = "home"
        self._render_uninstall()

    def _render_uninstall(self):
        inner = self.uninstall_inner
        for child in inner.winfo_children():
            child.destroy()
        self.t.label(inner, "Uninstall Ducker", "h2", WHITE).pack(anchor="w", padx=8, pady=(4, 4))
        self._para(inner, "Ducker is safe to remove at any time. It only adjusts volume, the same sliders you can "
                          "move yourself in the Volume Mixer. Uninstalling puts everything back exactly as it was.",
                   margin=60)
        self._para(inner, "Ducker will close, its shortcuts and its entry in Windows Settings will be removed, "
                          "and the program folder will be deleted a few seconds later.", margin=60)
        box = self.t.card(inner, fg_color=SURFACE)
        box.pack(fill="x", padx=8, pady=(4, 12))
        self.wipe_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(box, text="Completely wipe Ducker", variable=self.wipe_var,
                        font=self.t.f["body"], text_color=WHITE, fg_color=RED, hover_color=RED_HOVER,
                        border_color=BORDER, checkbox_width=22, checkbox_height=22, corner_radius=4).pack(anchor="w", padx=12, pady=(10, 4))
        self._para(box, "Everything associated with Ducker is removed from this PC: the program and its folder, "
                        "your settings, the log, every shortcut, the startup entry, its registry entries and "
                        "anything left by older versions. Nothing stays behind.", margin=90, pady=(0, 10)).pack_configure(padx=12)
        self.t.button(inner, "Uninstall Ducker", self._uninstall_now, "red", height=36).pack(fill="x", padx=8)
        self.t.button(inner, "Keep Ducker", lambda: self._show_page(self.uninstall_from), "ghost", height=32).pack(fill="x", padx=8, pady=(6, 0))

    def _uninstall_now(self):
        inner = self.uninstall_inner
        for child in inner.winfo_children():
            child.destroy()
        self.t.label(inner, "Uninstalling", "h2", WHITE).pack(anchor="w", padx=8, pady=(4, 8))
        status = self._para(inner, "Starting...", "body", GREY, margin=60)
        bar = ctk.CTkProgressBar(inner, progress_color=RED, fg_color=BORDER, height=8)
        bar.pack(fill="x", padx=8, pady=(4, 0))
        bar.set(0.05)
        steps = {"Removing shortcuts...": 0.3, "Removing registry entries...": 0.55,
                 "Removing the program files...": 0.8, "Done. Ducker will close in a moment.": 1.0}

        def report(text):
            self.root.after(0, lambda: (status.configure(text=text), bar.set(steps.get(text, 0.5))))

        def work():
            try:
                uninstall_ducker(bool(self.wipe_var.get()), report)
            except Exception as error:
                logging.exception("Uninstall failed")
                report(f"Something went wrong: {error}")
                return
            time.sleep(1.6)
            self.root.after(0, self.quit)

        threading.Thread(target=work, daemon=True).start()

    # ---- HELP ----------------------------------------------------------------------------------

    def _build_help(self, page):
        inner = self._scroll(page)
        self.help_sections = []
        self._help_section(inner, "h_keywords", "Keywords", self._help_words)
        self._help_section(inner, "h_logo", "What is Ducker?", self._help_what)
        self._help_section(inner, "h_pin", "Where is everything?", self._help_where)
        self._help_section(inner, "h_search", "Where is Ducker stored and found?", self._help_stored)
        self._help_section(inner, "h_check", "How can I test this works?", self._help_test)
        self._help_section(inner, "h_shield", "How do I prove this is safe?", self._help_safe)
        self._help_section(inner, "h_cross", "I want to uninstall Ducker", self._help_uninstall)
        self._help_section(inner, "h_person", "Who made this?", self._help_who)

    def _help_section(self, parent, icon_name, title, build_body):
        card = self.t.card(parent, fg_color=SURFACE)
        card.pack(fill="x", padx=8, pady=(0, 8))
        head = ctk.CTkFrame(card, fg_color="transparent", cursor="hand2")
        head.pack(fill="x", padx=12, pady=10)
        pic = ctk.CTkLabel(head, text="", image=icon(icon_name, 28), width=28)
        pic.pack(side="left", padx=(0, 10))
        text = self.t.label(head, title, "h3", WHITE)
        text.pack(side="left")
        arrow = self.t.label(head, "▾", "body", DIM)
        arrow.pack(side="right")
        body = ctk.CTkFrame(card, fg_color="transparent")
        build_body(body)

        def toggle(_event=None):
            if body.winfo_ismapped():
                body.pack_forget()
                arrow.configure(text="▾")
            else:
                body.pack(fill="x", padx=12, pady=(0, 12))
                arrow.configure(text="▴")

        for w in (head, pic, text, arrow):
            w.bind("<Button-1>", toggle)
        self.t.hover([head, pic, text, arrow], lambda: text.configure(text_color=GREEN_HOVER),
                     lambda: text.configure(text_color=WHITE))
        self.help_sections.append((title, card, toggle))   # kept so other parts can open a section

    def _para(self, parent, text, font="small", colour=GREY, margin=90, pady=(0, 8)):
        label = self.wrap.add(self.t.label(parent, text, font, colour, justify="left"), margin)
        label.pack(anchor="w", pady=pady)
        return label

    def _help_what(self, body):
        self._para(body, "Ducker listens to the sounds on your PC, and focuses on the sounds that matter most "
                         "to you, right when you need them.")
        self._para(body, "You tell it what your source is. Usually that is your music, like Spotify. "
                         "When anything else makes a sound, a video, a call, a game, Ducker turns the source down "
                         "so you can hear the other thing clearly. When that sound stops, the source comes back.")
        self._para(body, "That is all it does. The Tutorial shows it happening with two recordings.")
        self.t.button(body, "Open the Tutorial", self.open_tutorial, "primary", height=30).pack(anchor="w")

    def _help_where(self, body):
        self._para(body, "The Tutorial walks through the whole app in about a minute, with two recordings you can play.")
        self.t.button(body, "Open the Tutorial", self.open_tutorial, "primary", height=30).pack(anchor="w", pady=(0, 8))
        for name, where in (("Ducker mode", "the front page. Choose Pause, Mute or Fade."),
                            ("Sources and Exceptions", "Settings."),
                            ("Input", "Settings, at the bottom."),
                            ("Your own modes", "the front page. Press Create a Ducker."),
                            ("Turning it off", "the Active switch at the top right, or the icon by the clock.")):
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=(0, 4))
            self.t.label(row, name, "small", WHITE).pack(side="left")
            self.t.label(row, "  is on  " + where, "small", GREY).pack(side="left")

    def _help_words(self, body):
        for word, meaning in KEY_WORDS:
            self.t.label(body, word, "h3", WHITE).pack(anchor="w", pady=(4, 0))
            self._para(body, meaning)

    def _help_safe(self, body):
        self._para(body, "Ducker does one thing: it moves the volume sliders you can see in Windows' own Volume Mixer. "
                         "Here is everything it touches, and everything it does not.")
        self.t.label(body, "What it does", "h3", WHITE).pack(anchor="w", pady=(4, 2))
        for line in ("Reads how loud each app is, from the Volume Mixer.",
                     "Moves the volume slider of your source apps, and moves it back.",
                     "Reads window titles, so it can tell which website a browser tab is on.",
                     "If you switch Input on, reads how loud your microphone is. Nothing is recorded.",
                     "Keeps its settings in one small text file in your AppData folder."):
            self._para(body, "•  " + line, pady=(0, 2))
        self.t.label(body, "What it never does", "h3", WHITE).pack(anchor="w", pady=(8, 2))
        for line in ("Connect to the internet. There is no network code in it at all.",
                     "Record audio, take screenshots, or read your files.",
                     "Run as administrator, or change anything outside its own folder.",
                     "Start anything you did not ask for. It only runs itself at sign-in, and you can switch that off."):
            self._para(body, "•  " + line, pady=(0, 2))
        self.t.label(body, "Check for yourself", "h3", WHITE).pack(anchor="w", pady=(8, 2))
        self._para(body, "Open Task Manager and watch Ducker. It uses almost no processor time and has no network activity. "
                         "Upload Ducker.exe to VirusTotal, which scans it with seventy security products. "
                         "Right-click the file and choose Scan with Microsoft Defender.")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        self.t.button(row, "Open VirusTotal", lambda: self._open_url("https://www.virustotal.com/gui/home/upload"), "secondary", height=28).pack(side="left", padx=(0, 6))
        self.t.button(row, "Open Ducker's folder", self._open_data_folder, "secondary", height=28).pack(side="left")

    def _help_uninstall(self, body):
        self._para(body, "Ducker is safe to remove at any time. It only adjusts volume, the same sliders you can move "
                         "yourself in the Volume Mixer. It does not change or reach anything else on your PC, so "
                         "uninstalling it puts everything back exactly as it was.")
        self.t.label(body, "How to uninstall", "h3", WHITE).pack(anchor="w", pady=(4, 2))
        for line in ("Press the button below, or the one at the bottom of Settings. There is also "
                     "\"Ducker - Uninstall\" in the Start Menu, next to Ducker.",
                     "Choose whether to keep your settings, then press Uninstall Ducker.",
                     "Ducker closes and removes itself, its shortcuts and its entry in Windows Settings. "
                     "Nothing is left behind."):
            self._para(body, "•  " + line, pady=(0, 2))
        self.t.button(body, "Uninstall Ducker", lambda: self._show_page("uninstall"), "red", height=30).pack(anchor="w", pady=(6, 0))

    def _help_test(self, body):
        self._para(body, "Add a music source, like Spotify, and then play a song. As the song plays, play a video. "
                         "You will hear Spotify automatically \"duck\" in volume.")
        self._para(body, "Ducker is customisable, so you can add any source, exception, input or setting you wish.")

    def _help_who(self, body):
        self._para(body, "Hello, I'm Jacob Hammersley, a creative strategist and media producer. I created Ducker "
                         "as I found music was necessary to the background of my creative life... until it got "
                         "in the way!")
        self._para(body, "When creating content, I'd have to faff with opening the relevant music apps, stopping it, "
                         "edit videos, and then hearing the eerie silence of no music when I was done. This isn't "
                         "system-breaking, but it is enough to disrupt my creative flow. I wanted something that "
                         "could do this seamlessly so I can get on with what I wanted to, and music could continue "
                         "being the background of my creativity!")
        self._para(body, "After realising this could be useful beyond video editing... I made Ducker.")

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _open_data_folder(self):
        try:
            os.startfile(str(DATA_DIR))
        except Exception:
            pass

    def _help_stored(self, body):
        self._para(body, "Ducker lives in the system tray, the little row of icons by the clock at the bottom right "
                         "of the screen. Closing or minimising the window sends it there. Click the green circle to "
                         "open it again, or right-click it for a menu.")
        try:
            from PIL import Image
            image = Image.open(RESOURCE_DIR / "art" / "tray.png")
            picture = ctk.CTkImage(light_image=image, dark_image=image, size=(342, 135))
            ctk.CTkLabel(body, text="", image=picture).pack(anchor="w", pady=(0, 10))
        except Exception:
            pass
        self._para(body, "Here is where Ducker is installed on your system:")
        location = install_location()
        self.t.label(body, str(location), "small", WHITE, justify="left", wraplength=330).pack(anchor="w", pady=(0, 4))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 10))
        self.t.button(row, "Open that folder", lambda: self._open_folder(location), "secondary", height=28).pack(side="left", padx=(0, 6))
        self.t.button(row, "Open the settings folder", self._open_data_folder, "secondary", height=28).pack(side="left")
        self.t.button(body, "Uninstall Ducker", lambda: self._show_page("uninstall"), "red", height=30).pack(anchor="w")

    def _open_folder(self, path):
        try:
            os.startfile(str(path))
        except Exception:
            pass

    # ---- TUTORIAL ----------------------------------------------------------------------------------

    def open_tutorial(self):
        if self.tutorial is not None and self.tutorial.win is not None and self.tutorial.win.winfo_exists():
            self.tutorial.win.lift()
            return
        try:
            self.tutorial = TutorialWindow(self)
        except Exception:
            self.tutorial = None

    def tutorial_closed(self):
        self.tutorial = None

    def after_tutorial(self):
        """The small pop-up that appears when the tutorial is finished or closed."""
        self.show()
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        self.after_win = win
        win.title("What next?")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        dim = Dimmer(self.root, priority=win, owner=win)
        ico = RESOURCE_DIR / "icon.ico"
        if ico.exists():
            win.after(200, lambda: win.iconbitmap(str(ico)))
        inner = ctk.CTkFrame(win, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=22, pady=18)
        self.t.label(inner, "What next?", "h2", WHITE).pack(anchor="w")
        self.t.label(inner, "Now you have seen the tutorial, try it for yourself and enjoy. "
                            "If anything is still unclear, head to the \"Help\" section.",
                     "body", GREY, justify="left", wraplength=350).pack(anchor="w", pady=(6, 14))
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x")

        def go(page):
            dim.close()
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass
            self.show()
            self._show_page(page)

        self.t.button(row, "Ok", lambda: go("home"), "primary", width=90).pack(side="left")
        self.t.button(row, "Help", lambda: go("help"), "secondary", height=34, width=90).pack(side="left", padx=8)
        win.protocol("WM_DELETE_WINDOW", lambda: go("home"))
        win.update_idletasks()
        scale = win._get_window_scaling()
        centre_on_screen(win, 400, int(win.winfo_reqheight() / scale + 10))
        win.after(100, win.grab_set)

    # ---- showing and hiding ---------------------------------------------------------------------------

    def _corner(self):
        """Where the window opens: the exact centre of the screen, in real pixels."""
        scale = self.root._get_window_scaling()
        left, top, right, bottom = work_area()
        w = self.root.winfo_width() if self.root.winfo_width() > 10 else WIN_W * scale
        h = self.root.winfo_height() if self.root.winfo_height() > 10 else WIN_H * scale
        return int(left + (right - left - w) / 2), int(top + (bottom - top - h) / 2)

    def show(self):
        if self.visible:
            self.root.lift()
            self.root.focus_force()
            return
        self._show_page("home", animate=False)
        self._render_home()
        self.root.update_idletasks()
        x, y = self._corner()
        rise = 0 if reduce_motion() else 16
        self.root.geometry(f"+{x}+{y + rise}")
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.visible = True
        begun = time.time()
        duration = 0.0 if reduce_motion() else 0.2

        def step():
            progress = 1.0 if duration == 0 else min(1.0, (time.time() - begun) / duration)
            e = ease_out(progress)
            self.root.attributes("-alpha", e)
            self.root.geometry(f"+{x}+{int(y + rise * (1 - e))}")
            if progress < 1.0:
                self.root.after(16, step)
            else:
                self.root.attributes("-topmost", False)
                if not self.settings.get("seen_welcome"):
                    self._show_welcome()

        step()

    def _unmapped(self, event):
        """Minimising to the taskbar is treated the same as closing: into the tray."""
        if event.widget is not self.root or not self.visible or self.tray is None:
            return
        self.root.after(80, self._check_minimised)

    def _check_minimised(self):
        try:
            if self.visible and self.root.state() == "iconic":
                self.root.withdraw()
                self.visible = False
                if not self.settings.get("seen_tray_hint"):
                    self.settings["seen_tray_hint"] = True
                    save_settings(self.settings)
                    try:
                        self.tray.notify("Ducker is still running here. Click the icon to open it.", APP_NAME)
                    except Exception:
                        pass
        except tk.TclError:
            pass

    def hide(self):
        if not self.visible:
            return
        if self.tutorial is not None and self.tutorial.win is not None and self.tutorial.win.winfo_exists():
            self.tutorial.close()
        self.visible = False
        if self.tray is None:
            self.root.iconify()
            return
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        except tk.TclError:
            x, y = self._corner()
        begun = time.time()
        duration = 0.0 if reduce_motion() else 0.14
        drop = 0 if reduce_motion() else 12

        def step():
            progress = 1.0 if duration == 0 else min(1.0, (time.time() - begun) / duration)
            e = ease_out(progress)
            self.root.attributes("-alpha", 1 - e)
            self.root.geometry(f"+{x}+{int(y + drop * e)}")
            if progress < 1.0:
                self.root.after(16, step)
            else:
                self.root.withdraw()
                if not self.settings.get("seen_tray_hint"):
                    self.settings["seen_tray_hint"] = True
                    save_settings(self.settings)
                    try:
                        self.tray.notify("Ducker is still running here. Click the icon to open it.", APP_NAME)
                    except Exception:
                        pass

        step()

    def _show_welcome(self):
        if self.welcome is not None:
            return
        card = self.t.card(self.root, border_color=GREEN)
        self.welcome = card
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(padx=18, pady=14)
        self.t.label(inner, "Hello. This is Ducker.", "h2", WHITE).pack(anchor="w")
        for text in ("It is on and listening.",
                     "When another sound plays, your source gets quieter.",
                     "When the sound stops, the source comes back."):
            self.t.label(inner, text, "body", WHITE, wraplength=300, justify="left").pack(anchor="w", pady=(5, 0))
        self.t.label(inner, "The Tutorial shows it happening in about a minute.",
                     "small", DIM, wraplength=300, justify="left").pack(anchor="w", pady=(10, 10))
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x")
        self.t.button(row, "Show me", lambda: (self._dismiss_welcome(), self.open_tutorial()), "primary", width=100).pack(side="left")
        self.t.button(row, "Got it", self._dismiss_welcome, "ghost", height=32, width=80).pack(side="left", padx=8)
        card.place(relx=0.5, rely=0.5, anchor="center")

    def _dismiss_welcome(self):
        if self.welcome is not None:
            self.welcome.destroy()
            self.welcome = None
        self.settings["seen_welcome"] = True
        save_settings(self.settings)

    # ---- shared ----------------------------------------------------------------------------------------

    def toggle_enabled(self):
        self.settings["enabled"] = bool(self.enabled_var.get())
        save_settings(self.settings)
        self._shown = None

    def set_enabled(self, value):
        self.enabled_var.set(value)
        self.toggle_enabled()

    def choose_mode(self, key):
        self.settings["mode"] = key
        save_settings(self.settings)
        if self.page == "home":
            self._paint_tiles()
        logging.info("Mode changed to %s", key)

    def reset_music(self):
        threading.Thread(target=self.engine.reset_music_volume, daemon=True).start()

    def _refresh_status(self):
        with self.engine.lock:
            status, detail = self.engine.status, self.engine.status_detail
        shown = (status, detail, tuple(sorted(self.engine.music_names)), self.settings["mode"],
                 self.settings["enabled"], self.engine.last_reason)
        if shown != getattr(self, "_shown", None):
            self._shown = shown
            self.paint_now(status, detail)
        if self.page == "settings" and self.settings["mic"].get("enabled") and hasattr(self, "mic_meters"):
            for dev_id, meter in self.mic_meters.items():
                try:
                    meter.set(min(1.0, self.engine.mic_levels.get(dev_id, 0.0) * 2.5))
                except Exception:
                    pass
        if self.index.ready and not getattr(self, "_index_rendered", False):
            self._index_rendered = True
            if self.page == "settings":
                self._render_settings()
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 40 == 0:      # every six seconds or so
            self.running = running_names()
            known = set(self.settings.get("seen_apps", []))
            fresh = {n for n in SEEN_EXES if n.endswith(".exe")}
            if not fresh <= known:
                self.settings["seen_apps"] = sorted(known | fresh)
                save_settings(self.settings)
        self.root.after(150, self._refresh_status)

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        try:
            image = Image.open(RESOURCE_DIR / "icon.ico")
            image.size = (256, 256)
            image.load()
        except Exception:
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(image).ellipse((4, 4, 60, 60), fill=GREEN)

        def pick(duck_id):
            return lambda: self.root.after(0, self.choose_mode, duck_id)

        def duck_items():
            return [pystray.MenuItem(d["name"], pick(d["id"]),
                                     checked=lambda item, i=d["id"]: self.settings["mode"] == i, radio=True)
                    for d in all_ducks(self.settings)]

        menu = pystray.Menu(
            pystray.MenuItem("Open " + APP_NAME, lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem(lambda item: "Turn Ducker off" if self.settings["enabled"] else "Turn Ducker on",
                             lambda: self.root.after(0, self.set_enabled, not self.settings["enabled"])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ducker mode", pystray.Menu(lambda: duck_items())),
            pystray.MenuItem("Settings", lambda: self.root.after(0, lambda: (self.show(), self._show_page("settings")))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Put everything back to full volume", lambda: self.root.after(0, self.reset_music)),
            pystray.MenuItem("Quit", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon("ducker", image, APP_NAME, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def quit(self):
        self.engine.stop_test_player("music")
        self.engine.stop_test_player("voice")
        self.engine.restore_now()
        if self.tray is not None:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def duck_short(duck):
    """One line about a mode, for a tile."""
    action = duck.get("action", "volume")
    if duck.get("id") == "__new__":
        return "Make your own mode."
    if action == "pause":
        return "Pauses the source."
    if action == "mute":
        return "Silences the source."
    return f"Fades the source to {100 - int(duck.get('reduce', 80))}%."


def duck_long(duck):
    action = duck.get("action", "volume")
    quiet = float(duck.get("quiet", 0))
    wait = f" It waits {quiet:g} seconds of silence before the source comes back." if quiet > 0 else ""
    if action == "pause":
        return "The source pauses while another sound plays, then plays again from where it left off." + wait
    if action == "mute":
        return "The source goes silent while another sound plays, but keeps running underneath." + wait
    return (f"The source fades to {100 - int(duck.get('reduce', 80))}% while another sound plays, "
            f"then fades back up when the sound stops." + wait)


# ---------------------------------------------------------------------------
# Create a Ducker: its own window
# ---------------------------------------------------------------------------

class MakerWindow:
    EFFECTS = (
        ("pause", "Pause", "The source stops. When the sound ends, it carries on from the same place."),
        ("mute", "Mute", "The source goes silent but keeps running. When the sound ends, you hear it again."),
        ("volume", "Fade", "The source gets quieter, then fades back up when the sound ends."),
    )

    def __init__(self, app, duck, over=None):
        self.app = app
        self.t = app.t
        self.duck = duck
        self.over = over or app.root
        self.win = None
        self.dimmers = []
        try:
            self._build(app, duck)
        except Exception:
            logging.exception("The Create a Ducker window could not open")
            self.close()
            raise

    def _build(self, app, duck):
        self.win = ctk.CTkToplevel(app.root, fg_color=BG)
        self.dimmers = [Dimmer(app.root, priority=self.win, owner=self.win)]
        if app.tutorial is not None and app.tutorial.win is not None and app.tutorial.win.winfo_exists():
            self.dimmers.append(Dimmer(app.tutorial.win, priority=self.win, owner=self.win))
        self.win.title("Create a Ducker" if duck is None else duck["name"])
        self.win.resizable(False, False)
        self.win.after(200, lambda: self.win.attributes("-topmost", False))
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        ico = RESOURCE_DIR / "icon.ico"
        if ico.exists():
            self.win.after(200, lambda: self.win.iconbitmap(str(ico)))
        self.wrap = Wrapper(self.win)
        outer = ctk.CTkFrame(self.win, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=16)
        self.t.label(outer, "Create a Ducker" if duck is None else "Edit " + duck["name"], "h2", WHITE).pack(anchor="w")
        self.wrap.add(self.t.label(outer, "A Ducker is what happens to the source when another sound plays.",
                                   "small", DIM, justify="left"), 70).pack(anchor="w", pady=(2, 10))
        buttons = ctk.CTkFrame(outer, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        self.body = ctk.CTkScrollableFrame(outer, fg_color="transparent", scrollbar_button_color=BORDER,
                                           scrollbar_button_hover_color=DIM)
        self.body.pack(fill="both", expand=True)
        scrollbar_when_needed(self.body)
        inner = self.body
        self.inner = inner

        # Name
        self.t.label(inner, "NAME", "label", DIM).pack(anchor="w", pady=(0, 4))
        self.name_var = tk.StringVar(value=duck["name"] if duck else "")
        self.name_entry = ctk.CTkEntry(inner, textvariable=self.name_var, font=self.t.f["body"], fg_color=CARD,
                                       border_color=BORDER, text_color=WHITE, corner_radius=4, height=34,
                                       placeholder_text=app.next_duck_name())
        self.name_entry.pack(fill="x")

        # Effect
        self.t.label(inner, "EFFECT", "label", DIM).pack(anchor="w", pady=(14, 4))
        self.wrap.add(self.t.label(inner, "What should this Ducker do to the source?", "small", GREY), 70).pack(anchor="w", pady=(0, 6))
        self.action_var = tk.StringVar(value=(duck or NEW_DUCK).get("action", "volume"))
        effects = ctk.CTkFrame(inner, fg_color="transparent")
        effects.pack(fill="x")
        effects.columnconfigure((0, 1, 2), weight=1, uniform="e")
        self.effect_tiles = {}
        for i, (value, title, blurb) in enumerate(self.EFFECTS):
            tile = self.t.card(effects, cursor="hand2")
            tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0 if i == 2 else 4))
            name = self.t.label(tile, title, "h3", WHITE)
            name.pack(anchor="w", padx=10, pady=(8, 0))
            text = self.t.label(tile, blurb, "tiny", DIM, justify="left", wraplength=130)
            text.pack(anchor="w", padx=10, pady=(2, 8))
            for w in (tile, name, text):
                w.bind("<Button-1>", lambda e, v=value: self._pick_effect(v))
            self.effect_tiles[value] = tile

        # Options
        self.options = ctk.CTkFrame(inner, fg_color="transparent")
        self.options.pack(fill="x", pady=(12, 0))
        source = duck or NEW_DUCK
        self.v_target = 100 - int(source.get("reduce", 80))
        self.v_fade_down = float(source.get("fade_down", 0.5))
        self.v_fade_up = float(source.get("fade_up", 1.5))
        self.v_quiet = float(source.get("quiet", 0.0))
        self.v_threshold = duck_threshold(source)
        self._pick_effect(self.action_var.get(), first=True)

        self.t.button(buttons, "Save", self.save, "primary", width=100).pack(side="left")
        self.t.button(buttons, "Cancel", self.close, "ghost", height=32, width=90).pack(side="left", padx=8)
        if duck is not None:
            self.t.button(buttons, "Delete mode", self.delete, "danger", height=32, width=110).pack(side="right")

        self._fit(place=True)
        self.name_entry.focus_set()

    def _fit(self, place=False):
        """Size the window to its contents, but never taller than the screen.
        Everything here is worked out in unscaled units, which is what
        CustomTkinter expects in geometry()."""
        scale = self.win._get_window_scaling()
        self.win.update_idletasks()
        content = self.body.winfo_reqheight() / scale + 4
        self.body.configure(height=content)
        self.win.update_idletasks()
        total = self.win.winfo_reqheight() / scale + 10
        _, top_, _, bottom = work_area()
        available = (bottom - top_) / scale - 60
        if total > available:
            self.body.configure(height=max(120, content - (total - available)))
            total = available
        w, h = 520, int(total)
        if place:
            centre_on_screen(self.win, w, h)
        else:
            self.win.geometry(f"{w}x{h}")

    def _pick_effect(self, value, first=False):
        self.action_var.set(value)
        for v, tile in self.effect_tiles.items():
            tile.configure(fg_color=CARD_SELECTED if v == value else CARD, border_color=GREEN if v == value else BORDER)
        self._render_options()
        if not first:
            self._fit()

    def _slider(self, parent, title, explain, lo, hi, value, fmt, on_change, steps=100):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        head = ctk.CTkFrame(row, fg_color="transparent")
        head.pack(fill="x")
        self.t.label(head, title, "small", WHITE).pack(side="left")
        shown = self.t.label(head, fmt(value), "small", GREEN)
        shown.pack(side="right")
        slider = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, progress_color=GREEN,
                               button_color="#e6e6e6", button_hover_color=WHITE, fg_color=BORDER, height=14,
                               command=lambda v: (shown.configure(text=fmt(float(v))), on_change(float(v))))
        slider.set(value)
        slider.pack(fill="x", pady=(4, 0))
        self.wrap.add(self.t.label(row, explain, "tiny", DIM, justify="left"), 70).pack(anchor="w", pady=(2, 0))
        return slider

    def _render_options(self):
        for child in self.options.winfo_children():
            child.destroy()
        action = self.action_var.get()
        if action == "volume":
            def set_target(v): self.v_target = int(round(v))
            def set_down(v): self.v_fade_down = v
            def set_up(v): self.v_fade_up = v
            def set_thr(v): self.v_threshold = slider_to_threshold(v)
            self._slider(self.options, "Adjust volume to", "How loud the source is while the other sound plays, "
                         "as a share of its normal volume.", 0, 90, self.v_target, lambda v: f"{int(round(v))}%", set_target, 90)
            self._slider(self.options, "Fade speed in", "How long the source takes to drop. Instant at the left.",
                         0, 3, self.v_fade_down, lambda v: "Instant" if v < 0.05 else f"{v:.1f} s", set_down, 30)
            self._slider(self.options, "Fade speed out", "How long the source takes to come back up.",
                         0, 4, self.v_fade_up, lambda v: "Instant" if v < 0.05 else f"{v:.1f} s", set_up, 40)
            self._slider(self.options, "Sound sensitivity", "How loud another sound must be before the source ducks. "
                         "At the left, any noise counts, even a quiet click. Slide right so only louder sounds count.",
                         0, 100, threshold_to_slider(self.v_threshold), lambda v: threshold_words(slider_to_threshold(v)), set_thr)
        else:
            def set_quiet(v): self.v_quiet = round(v, 1)
            self._slider(self.options, "Delay before the source comes back",
                         "After the other sound stops, wait this long before the source resumes. "
                         "A short delay stops it jumping back during a brief pause.",
                         0, 5, self.v_quiet, lambda v: "No delay" if v < 0.05 else f"{v:.1f} s", set_quiet, 50)

    def save(self):
        name = self.name_var.get().strip() or self.app.next_duck_name()
        action = self.action_var.get()
        values = {"action": action, "reduce": 100 - self.v_target if action == "volume" else 100,
                  "fade_down": round(self.v_fade_down, 1) if action == "volume" else 0.0,
                  "fade_up": round(self.v_fade_up, 1) if action == "volume" else 0.0,
                  "quiet": 1.0 if action == "volume" else self.v_quiet,
                  "threshold": round(self.v_threshold, 4) if action == "volume" else 0.01}
        if self.duck:
            self.duck.update(values)
            self.duck["name"] = name
            chosen = self.duck["id"]
        else:
            duck = {"id": uuid.uuid4().hex, "name": name, "builtin": False}
            duck.update(values)
            self.app.settings["ducks"].append(duck)
            chosen = duck["id"]
        self.app.settings["mode"] = chosen
        save_settings(self.app.settings)
        self.close()
        self.app.duck_saved()

    def delete(self):
        duck = self.duck
        self.close()
        self.app._delete_duck(duck)

    def close(self):
        for dim in self.dimmers:
            dim.close()
        try:
            if self.win is not None:
                self.win.destroy()
        except tk.TclError:
            pass
        self.app.maker = None


# ---------------------------------------------------------------------------
# Tutorial: a wide window with two tracks and a guided walk-through
# ---------------------------------------------------------------------------

class TutorialWindow:
    """A fixed-size window with the two tracks. Over it sits a dark see-through
    layer with a hole cut out around whatever the current step is about, and a
    guide card along the bottom. Steps 3 to 5 fill the space at the top with
    real controls, so what the user does here is saved in Ducker."""

    KEY = "#010101"      # pixels this colour in the overlay are see-through and click-through
    STEPS = [
        ("1 / 5", "Press Play on the Source track",
         "This could be music, podcasts, anything you listen to. This is what Ducker turns down (or ducks!).",
         "music"),
        ("2 / 5", "Now press Play on the Sound track",
         "These are noises Ducker hears which stay clear to you. This could be a YouTube video, a Discord call "
         "or a Premiere Pro edit. Whatever you need to stay at normal volume.",
         "voice"),
        ("3 / 5", "Choose a Ducker mode",
         "A Ducker mode decides how Ducker behaves. Click one and the Sound plays again, "
         "so you can hear what that mode does to the Source.",
         "modes"),
        ("4 / 5", "Create a Ducker",
         "You can make new Ducker modes to change how Ducker behaves, to suit you. "
         "Anything you make here is saved and waiting on the front page.",
         "maker"),
        ("5 / 5", "Head to Settings",
         "There are more options to make Ducker fit you. Press the cog to open Settings.",
         "settings"),
    ]
    PANEL_STEPS = ("modes",)

    def __init__(self, app):
        self.app = app
        self.t = app.t
        self.engine = app.engine
        self.win = self.overlay = self.card = None
        self.hid_main = False
        self.token = "1.0"
        self.gain = self.shown_gain = 1.0
        self.replay_at = None
        try:
            self._build()
        except Exception:
            logging.exception("The tutorial could not open")
            self.close()
            raise

    def _build(self):
        app = self.app
        self.step = -1
        self.ducked_seen = False
        self.restored_at = None
        self.hole = [0, 0, 0, 0]        # current hole, in overlay coordinates
        self.hole_target = None
        self.hole_start = None
        self.hole_begun = 0.0
        self.last_geometry = None
        self.mode_tiles = {}

        self.engine.override = None
        self.token = "1.0"              # the last instruction given to the source
        self.gain = 1.0                 # what that means as a level, for the waveform
        self.shown_gain = 1.0
        self.voice_stopped_at = None
        self._write_token("1.0", force=True)

        # The tutorial window itself. Its size comes from the screen, so that
        # nothing ever has to be squeezed.
        self.win = ctk.CTkToplevel(app.root, fg_color=BG)
        self.win.title("Ducker tutorial")
        self.win.protocol("WM_DELETE_WINDOW", lambda: self.close(ask=True))
        ico = RESOURCE_DIR / "icon.ico"
        if ico.exists():
            self.win.after(200, lambda: self.win.iconbitmap(str(ico)))
        scale = self.win._get_window_scaling()
        left, top_, right, bottom = work_area()
        avail_w = (right - left) / scale
        avail_h = (bottom - top_) / scale
        w = int(min(860, avail_w - 40))
        h = int(min(720, avail_h - 30))
        compact = h < 690
        self.panel_height = 170 if compact else 210
        self.card_height = 150 if compact else 164
        self.canvas_height = 80
        self.win.resizable(False, False)

        outer = ctk.CTkFrame(self.win, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=24, pady=(14, 16))
        head = ctk.CTkFrame(outer, fg_color="transparent")
        head.pack(side="top", fill="x")
        self.t.label(head, "Tutorial", "h2", WHITE).pack(side="left")
        self.close_button = self.t.button(head, "Close", lambda: self.close(ask=True), "secondary", height=28, width=70)
        self.close_button.pack(side="right")
        # The guide card's space is claimed first, so it can never be squeezed out
        self.card_area = ctk.CTkFrame(outer, fg_color="transparent", height=self.card_height)
        self.card_area.pack(side="bottom", fill="x", pady=(12, 0))
        self.card_area.pack_propagate(False)
        # Space at the top for steps 3 to 5. It stays empty until a step needs it.
        self.panel = ctk.CTkFrame(outer, fg_color="transparent", height=self.panel_height)
        self.panel.pack(side="top", fill="x", pady=(10, 0))
        self.panel.pack_propagate(False)
        self.panel_box = self.t.card(self.panel, fg_color=SURFACE)
        self.panel_shown = None
        self.tracks = {}
        for which, title, sub in (
            ("music", "Source", "Music, podcasts, anything you listen to. This is what Ducker turns down."),
            ("voice", "Sound", "Noises Ducker hears. These stay clear to you."),
        ):
            self._track(outer, which, title, sub)

        # Measure what the contents really need. If the screen is too short, the
        # empty space at the top gives way first, so the tracks and the guide card
        # are never squeezed.
        self.win.update_idletasks()
        needed = self.win.winfo_reqheight() / scale + 4
        if needed > h:
            self.panel_height = int(max(120, self.panel_height - (needed - h)))
            self.panel.configure(height=self.panel_height)
            self.win.update_idletasks()
            needed = self.win.winfo_reqheight() / scale + 4
            h = int(min(max(h, needed), avail_h - 20))
        centre_on_screen(self.win, w, h)

        # The dark layer with a hole in it
        self.overlay = tk.Toplevel(self.win)
        self.overlay.transient(self.win)
        self.overlay.overrideredirect(True)
        self.overlay.configure(bg="black")
        self.overlay.attributes("-alpha", 0.62)
        self.overlay.attributes("-transparentcolor", self.KEY)
        self.canvas = tk.Canvas(self.overlay, bg="black", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.overlay.withdraw()

        # The guide card, fully solid, above the dark layer
        self.card = tk.Toplevel(self.win)
        self.card.transient(self.win)
        self.card.overrideredirect(True)
        self.card.configure(bg=self.KEY)
        self.card.attributes("-transparentcolor", self.KEY)
        self.card.attributes("-alpha", 0.0)
        box = ctk.CTkFrame(self.card, fg_color=CARD, corner_radius=8, border_width=1, border_color=GREEN)
        box.pack(fill="both", expand=True)
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=18, pady=12)
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        self.step_num = self.t.label(top, "", "label", GREEN)
        self.step_num.pack(side="left")
        self.step_title = self.t.label(top, "", "h3", WHITE)
        self.step_title.pack(side="left", padx=(10, 0))
        self.next_button = self.t.button(top, "Next", self.next, "primary", height=28, width=80)
        self.next_button.pack(side="right")
        self.create_button = self.t.button(top, "+  Create a Ducker", lambda: self.app.open_maker(None, over=self.win),
                                           "primary", height=28)
        bottom = ctk.CTkFrame(inner, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")
        self.back_button = self.t.button(bottom, "‹  Previous step", self.back, "ghost", height=24, width=110,
                                         border_width=0, text_color=DIM, hover_color=CARD, font=self.t.f["small"])
        self.back_button.bind("<Enter>", lambda e: self.back_button.configure(text_color=GREY), add="+")
        self.back_button.bind("<Leave>", lambda e: self.back_button.configure(text_color=DIM), add="+")
        self.settings_button = self.t.button(top, "", self._to_settings, "secondary", height=28, width=34,
                                             image=icon("cog", 16))
        self.step_body = self.t.label(inner, "", "body", GREY, justify="left", anchor="w", wraplength=600)
        self.step_body.pack(fill="x", pady=(4, 0))
        self.card.withdraw()

        # The main window steps aside while the tutorial is open
        self.hid_main = app.visible
        if self.hid_main:
            app.root.withdraw()
        self.win.after(250, self._start)

    def _start(self):
        self.win.update_idletasks()
        self.win.lift()
        self._follow(force=True)
        self.overlay.deiconify()
        self.card.deiconify()
        self.overlay.lift(self.win)
        self.card.lift(self.overlay)
        self.win.bind("<FocusIn>", lambda e: self._restack(), add="+")
        self.win.bind("<Button-1>", lambda e: self._restack(), add="+")
        self._show_step(0)
        self._tick()

    def _restack(self):
        try:
            if self.win is not None:
                self.win.lift()
            if self.overlay is not None and self.overlay.winfo_viewable():
                self.overlay.lift(self.win)
            if self.card is not None and self.card.winfo_viewable():
                self.card.lift(self.overlay)
        except Exception:
            pass

    # ---- the two tracks -------------------------------------------------------------

    def _track(self, parent, which, title, sub):
        card = self.t.card(parent)
        card.pack(side="top", fill="x", pady=(10, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=8)
        left = ctk.CTkFrame(inner, fg_color="transparent")
        left.pack(side="left", anchor="n")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(anchor="w")
        ctk.CTkLabel(row, text="", image=icon("headphones" if which == "music" else "speaker", 18), width=18).pack(side="left", padx=(0, 6))
        self.t.label(row, title, "h3", WHITE).pack(side="left")
        self.t.label(left, sub, "tiny", DIM, justify="left", wraplength=180).pack(anchor="w", pady=(2, 4))
        button = self.t.button(left, "Play", lambda w=which: self.toggle(w), "primary", width=90, height=26)
        button.pack(anchor="w")
        canvas = tk.Canvas(inner, height=self.canvas_height, bg=SURFACE, highlightthickness=0, bd=0)
        canvas.pack(side="left", fill="x", expand=True, padx=(12, 0))
        shape, duration = waveform(RESOURCE_DIR / "sounds" / f"{which}.wav", 360)
        self.tracks[which] = {"card": card, "button": button, "canvas": canvas, "shape": shape,
                              "duration": duration, "started": None, "loop": which == "music"}
        canvas.bind("<Configure>", lambda e, w=which: self._draw(w))

    def _draw(self, which):
        track = self.tracks[which]
        canvas = track["canvas"]
        shape = track["shape"]
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 10 or not shape:
            canvas.create_text(width // 2, height // 2, text="Recording missing", fill=DIM, font=(self.t.family, 10))
            return
        mid = height / 2
        gain = 1.0
        paused = False
        if which == "music":
            if self.token in ("pause", "mute"):
                self.shown_gain = 0.0
                paused = self.token == "pause"
            else:
                self.shown_gain += (self.gain - self.shown_gain) * 0.18
            gain = max(0.05, self.shown_gain)
        running = self._running(which)
        position = 0.0
        if running and track["started"] and track["duration"]:
            elapsed = time.time() - track["started"]
            if paused:
                elapsed = track.get("paused_elapsed", elapsed)
                track["paused_elapsed"] = elapsed
                track["started"] = time.time() - elapsed
            else:
                track.pop("paused_elapsed", None)
            position = (elapsed % track["duration"]) / track["duration"] if track["loop"] else min(1.0, elapsed / track["duration"])
        columns = len(shape)
        step = width / columns
        for i, v in enumerate(shape):
            x = i * step
            h = max(1, v * gain * (height - 10) / 2)
            played = running and (i / columns) <= position
            colour = (GREEN if which == "music" else WHITE) if played else ("#2f6b44" if which == "music" else "#6a6a6a")
            canvas.create_line(x, mid - h, x, mid + h, fill=colour, width=max(1, step - 1))
        if running:
            px = position * width
            canvas.create_line(px, 4, px, height - 4, fill=WHITE, width=2)
        if which == "music" and running:
            if self.token == "pause":
                label = "Paused"
            elif self.token == "mute":
                label = "Muted"
            elif gain < 0.99:
                label = f"{int(gain * 100)}%"
            else:
                label = ""
            if label:
                canvas.create_text(width - 8, 12, text=label, fill=GREEN, anchor="e", font=(self.t.family, 10, "bold"))

    def _running(self, which):
        return self.engine.test_player_running(which)

    def _write_token(self, token, force=False):
        if token == self.token and not force:
            return
        self.token = token
        self.gain = 0.0 if token in ("pause", "mute") else float(token)
        try:
            TUTORIAL_GAIN_FILE.write_text(token)
        except Exception:
            pass

    def _update_duck(self):
        """While the Sound plays, do to the Source whatever the chosen mode says.
        One second after the Sound stops, bring the Source back."""
        if self._running("voice"):
            self.voice_stopped_at = None
            duck = current_duck(self.app.settings)
            action = duck.get("action", "volume")
            if action == "pause":
                self._write_token("pause")
            elif action == "mute":
                self._write_token("mute")
            else:
                level = 0.15 if duck.get("builtin") else max(0.0, (100 - int(duck.get("reduce", 80))) / 100)
                self._write_token(f"{level:.3f}")
        elif self.token != "1.0":
            if self.voice_stopped_at is None:
                self.voice_stopped_at = time.time()
            elif time.time() - self.voice_stopped_at >= 1.0:
                self._write_token("1.0")

    def toggle(self, which):
        track = self.tracks[which]
        if self._running(which):
            self.engine.stop_test_player(which)
            track["started"] = None
        else:
            self._play(which)
            if which == "music" and self.step == 0:
                self._show_step(1)
            if which == "voice" and self.step == 1:
                self.ducked_seen = False
                self.restored_at = None
        self._paint_buttons()

    def _play(self, which):
        track = self.tracks[which]
        self.engine.start_test_player(which)
        track["started"] = time.time()
        track["loop"] = which == "music"

    def _paint_buttons(self):
        for which, track in self.tracks.items():
            running = self._running(which)
            track["button"].configure(text="Stop" if running else "Play",
                                      fg_color=CARD if running else GREEN,
                                      text_color=WHITE if running else BG,
                                      hover_color=CARD_HOVER if running else GREEN_HOVER)

    # ---- the space at the top: real controls for steps 3 to 5 ------------------------------

    def _open_panel(self, what):
        for child in self.panel_box.winfo_children():
            child.destroy()
        {"modes": self._panel_modes}[what](self.panel_box)
        if self.panel_shown is None:
            self.panel_box.pack(fill="x", anchor="n")
        self.panel_shown = what
        self.panel_box.update_idletasks()

    def refresh_panel(self):
        """Called by the app when a mode is saved or deleted while the tutorial is open."""
        if self.panel_shown == "modes":
            try:
                self._open_panel(self.panel_shown)
            except tk.TclError:
                pass

    def _panel_inner(self, parent, title, intro=None):
        inner = ctk.CTkFrame(parent, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=10)
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        self.t.label(head, title, "h3", WHITE).pack(side="left")
        if intro:
            self.t.label(head, intro, "tiny", DIM).pack(side="left", padx=(12, 0))
        return inner

    def _mode_tiles(self, parent, ducks, with_edit=False):
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x", pady=(6, 0))
        columns = 4 if len(ducks) > 3 else 3
        grid.columnconfigure(tuple(range(columns)), weight=1, uniform="m")
        self.mode_tiles = {}
        for i, duck in enumerate(ducks[:8]):
            tile = self.t.card(grid, cursor="hand2")
            tile.grid(row=i // columns, column=i % columns, sticky="nsew",
                      padx=(0 if i % columns == 0 else 4, 0 if i % columns == columns - 1 else 4), pady=(0, 4))
            top = ctk.CTkFrame(tile, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(6, 0))
            name = self.t.label(top, duck["name"], "small", WHITE)
            name.pack(side="left")
            badge = ctk.CTkLabel(top, text="Selected", font=self.t.f["tiny"], text_color=BG, fg_color=GREEN,
                                 corner_radius=4, padx=6, pady=0)
            if with_edit and not duck.get("builtin"):
                self.t.button(top, "Edit", lambda dd=duck: self.app.open_maker(dd, over=self.win), "ghost",
                              height=20, width=40, font=self.t.f["tiny"]).pack(side="right", padx=(4, 0))
            text = self.t.label(tile, duck.get("blurb") or duck_short(duck), "tiny", DIM, justify="left",
                                wraplength=170 if columns == 4 else 215)
            text.pack(anchor="w", padx=10, pady=(0, 6))
            for w in (tile, top, name, text):
                w.bind("<Button-1>", lambda e, k=duck["id"]: self._pick_mode(k))
            self.mode_tiles[duck["id"]] = (tile, badge)
        self._paint_modes()

    def _panel_modes(self, parent):
        inner = self._panel_inner(parent, "Ducker mode", "Click one to hear it.")
        self._mode_tiles(inner, all_ducks(self.app.settings))

    def _pick_mode(self, key):
        self.app.choose_mode(key)
        self._paint_modes()
        # Bring the Source back to full volume first, so the new mode is heard
        # from the top, then start the Sound again a moment later
        self.engine.stop_test_player("voice")
        self.tracks["voice"]["started"] = None
        self.voice_stopped_at = None
        self._write_token("1.0")
        if not self._running("music"):
            self._play("music")
        self._paint_buttons()
        self.replay_at = time.time() + 1.2

    def _paint_modes(self):
        chosen = self.app.settings["mode"]
        for key, (tile, badge) in self.mode_tiles.items():
            try:
                tile.configure(fg_color=CARD_SELECTED if key == chosen else CARD, border_color=GREEN if key == chosen else BORDER)
                if key == chosen:
                    badge.pack(side="right")
                else:
                    badge.pack_forget()
            except tk.TclError:
                pass

    # ---- the overlay, the hole and the card -------------------------------------------

    def _win_box(self):
        """The tutorial window's inside area, in screen pixels."""
        return (self.win.winfo_rootx(), self.win.winfo_rooty(), self.win.winfo_width(), self.win.winfo_height())

    def _widget_box(self, widget, pad=6):
        """A widget's box in overlay coordinates."""
        ox, oy, _, _ = self._win_box()
        return [widget.winfo_rootx() - ox - pad, widget.winfo_rooty() - oy - pad,
                widget.winfo_rootx() - ox + widget.winfo_width() + pad,
                widget.winfo_rooty() - oy + widget.winfo_height() + pad]

    def _card_box(self):
        return self._widget_box(self.card_area, pad=0)

    def _follow(self, force=False):
        """Keep the dark layer and the card sitting exactly over the tutorial window."""
        geometry = self._win_box()
        if geometry != self.last_geometry or force:
            self.last_geometry = geometry
            x, y, w, h = geometry
            self.overlay.geometry(f"{w}x{h}+{x}+{y}")
            self.canvas.configure(width=w, height=h)
            cx1, cy1, cx2, cy2 = self._card_box()
            self.card.geometry(f"{cx2 - cx1}x{cy2 - cy1}+{x + cx1}+{y + cy1}")
            scale = ctk.ScalingTracker.get_widget_scaling(self.card)
            self.step_body.configure(wraplength=max(300, int((cx2 - cx1 - 56) / scale)))
            self._paint_overlay()

    def _paint_overlay(self):
        self.canvas.delete("all")
        x1, y1, x2, y2 = self.hole
        if x2 > x1 and y2 > y1:
            self.canvas.create_rectangle(x1, y1, x2, y2, fill=self.KEY, outline=self.KEY)
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=GREEN, width=2)
        cx1, cy1, cx2, cy2 = self._card_box()
        self.canvas.create_rectangle(cx1, cy1, cx2, cy2, fill=self.KEY, outline=self.KEY)
        # the Close button stays reachable
        try:
            bx1, by1, bx2, by2 = self._widget_box(self.close_button, pad=4)
            self.canvas.create_rectangle(bx1, by1, bx2, by2, fill=self.KEY, outline=self.KEY)
        except Exception:
            pass

    def _move_hole_to(self, widgets):
        """Open the hole around one widget, or around several at once."""
        if not widgets:
            target = [0, 0, 0, 0]
        else:
            boxes = [self._widget_box(w) for w in widgets]
            target = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes)]
        if reduce_motion():
            self.hole = target
            self._paint_overlay()
            return
        self.hole_start = list(self.hole) if self.hole[2] > self.hole[0] else [target[0], target[1] + (target[3] - target[1]) / 2,
                                                                                target[2], target[1] + (target[3] - target[1]) / 2]
        self.hole_target = target
        self.hole_begun = time.time()

    def _animate_hole(self):
        if self.hole_target is None:
            return
        progress = min(1.0, (time.time() - self.hole_begun) / 0.35)
        e = ease_out(progress)
        self.hole = [s + (t - s) * e for s, t in zip(self.hole_start, self.hole_target)]
        self._paint_overlay()
        if progress >= 1.0:
            self.hole_target = None

    def _fade_card(self, to, then=None, duration=0.18):
        begun = time.time()
        try:
            start = float(self.card.attributes("-alpha"))
        except Exception:
            start = 1.0
        if reduce_motion():
            duration = 0.0

        def step():
            progress = 1.0 if duration == 0 else min(1.0, (time.time() - begun) / duration)
            e = ease_out(progress)
            try:
                self.card.attributes("-alpha", start + (to - start) * e)
            except tk.TclError:
                return
            if progress < 1.0:
                self.card.after(16, step)
            elif then:
                then()

        step()

    # ---- steps -------------------------------------------------------------------------------

    def _show_step(self, index):
        self.step = index
        num, title, body, highlight = self.STEPS[index]

        def apply():
            self.step_num.configure(text=num)
            self.step_title.configure(text=title)
            self.step_body.configure(text=body)
            last = index == len(self.STEPS) - 1
            self.next_button.configure(text="Close Tutorial" if last else "Next", width=120 if last else 80)
            if last:
                self.settings_button.pack(side="right", padx=(0, 8))
            else:
                self.settings_button.pack_forget()
            if index > 0:
                self.back_button.pack(side="left")
            else:
                self.back_button.pack_forget()
            if highlight == "maker":
                # Create a Ducker sits beside Next, and Next steps back to an outline
                self.create_button.pack(side="right", padx=(0, 8))
                self.next_button.configure(fg_color=BG, hover_color=CARD_HOVER, text_color=GREEN,
                                           border_width=1, border_color=GREEN)
            else:
                self.create_button.pack_forget()
                self.next_button.configure(fg_color=GREEN, hover_color=GREEN_HOVER, text_color=BG, border_width=0)
            if highlight in self.PANEL_STEPS:
                self._open_panel(highlight)
                self._move_hole_to([self.panel, self.tracks["music"]["card"], self.tracks["voice"]["card"]])
            else:
                if self.panel_shown is not None:
                    self.panel_box.pack_forget()
                    self.panel_shown = None
                if highlight in self.tracks:
                    self._move_hole_to([self.tracks[highlight]["card"]])
                else:
                    self._move_hole_to([])
            self._fade_card(1.0)

        self._fade_card(0.0, then=apply)

    def next(self):
        if self.step < len(self.STEPS) - 1:
            self._show_step(self.step + 1)
        else:
            self.close(ask=True)

    def back(self):
        if self.step > 0:
            self._show_step(self.step - 1)

    def _to_settings(self):
        app = self.app
        self.close()
        app.show()
        app._show_page("settings")

    def _tick(self):
        if not self.win.winfo_exists():
            return
        try:
            self._follow()
            self._animate_hole()
            for which in self.tracks:
                if not self._running(which) and self.tracks[which]["started"] is not None:
                    self.tracks[which]["started"] = None
                self._draw(which)
            self._paint_buttons()
            if self.replay_at is not None and time.time() >= self.replay_at:
                self.replay_at = None
                if not self._running("voice"):
                    self._play("voice")
            self._update_duck()
            if self.step == 1:
                if self.token != "1.0":
                    self.ducked_seen = True
                    self.restored_at = None
                elif self.ducked_seen and self.restored_at is None:
                    self.restored_at = time.time()
                if self.restored_at is not None and time.time() - self.restored_at >= 2.0:
                    self._show_step(2)
        except tk.TclError:
            return
        self.win.after(40, self._tick)

    def close(self, ask=False):
        self._write_token("1.0")
        self.engine.stop_test_player("music")
        self.engine.stop_test_player("voice")
        self.engine.override = None
        for w in (self.card, self.overlay, self.win):
            try:
                if w is not None:
                    w.destroy()
            except tk.TclError:
                pass
        if getattr(self, "hid_main", False):
            try:
                self.app.root.deiconify()
                self.app.root.lift()
            except tk.TclError:
                pass
        self.app.tutorial_closed()
        if ask:
            self.app.root.after(150, self.app.after_tutorial)


# ---------------------------------------------------------------------------

MUTEX_NAME = "Ducker_JacobHammersley"
SHOW_EVENT_NAME = "Ducker_Show"
kernel32 = ctypes.windll.kernel32


def already_running():
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() == 183


def ask_running_copy_to_show():
    event = kernel32.OpenEventW(0x0002, False, SHOW_EVENT_NAME)
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)


def watch_for_show_requests(app):
    event = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    while True:
        kernel32.WaitForSingleObject(event, 0xFFFFFFFF)
        try:
            app.root.after(0, app.show)
        except Exception:
            pass


def main():
    if "--play-test-music" in sys.argv:
        play_test_file("music", loop=True)
        return
    if "--play-test-voice" in sys.argv:
        play_test_file("voice", loop="--loop" in sys.argv)
        return
    if already_running():
        ask_running_copy_to_show()
        return
    settings = load_settings()
    engine = DuckEngine(settings)
    engine.start()
    app = App(settings, engine, start_hidden="--minimized" in sys.argv)
    threading.Thread(target=watch_for_show_requests, args=(app,), daemon=True).start()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        details = traceback.format_exc()
        logging.error("Crashed:\n%s", details)
        try:
            ctypes.windll.user32.MessageBoxW(
                0, f"{APP_NAME} hit a problem and had to close.\n\n{details[-1200:]}",
                APP_NAME, 0x10)
        except Exception:
            pass
