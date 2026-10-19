"""
Ducker - Install Menu
By Jacob Hammersley

Installs or removes Ducker. The app's .exe is bundled inside this
installer by build.bat, so this one file is all anyone needs.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont

import customtkinter as ctk
import psutil

APP_NAME = "Ducker"
MENU_NAME = "Ducker - Installer"
UNINSTALLER_NAME = "Ducker - Uninstall.exe"
OLD_APP_NAMES = ("Music Ducker", "Auto Duck", "Spotify Auto Duck")
EXE_NAME = APP_NAME + ".exe"
VERSION = "1.0"
PUBLISHER = "Jacob Hammersley"

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BUNDLED_EXE = RESOURCE_DIR / EXE_NAME
DEFAULT_DEST = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / APP_NAME
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
FROZEN = bool(getattr(sys, "frozen", False))
THIS_EXE = Path(sys.executable) if FROZEN else None

APP_KEY = r"Software\Ducker"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Ducker"
OLD_INSTALLS = (
    ("Music Ducker", r"Software\Music Ducker",
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicDucker"),
    ("Auto Duck", r"Software\Auto Duck",
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoDuck"),
    ("Spotify Auto Duck", r"Software\Spotify Auto Duck",
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SpotifyAutoDuck"),
)

BG = "#0f0f10"
SURFACE = "#17181a"
CARD = "#1e1f22"
BORDER = "#2b2c30"
GREEN = "#1DB954"
GREEN_HOVER = "#1ed760"
RED = "#e5484d"
RED_HOVER = "#f2555a"
WHITE = "#ffffff"
GREY = "#a8a8ad"
DIM = "#5f6066"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wscript_shell():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()  # needed because this runs in a worker thread
    return win32com.client.Dispatch("WScript.Shell")


def special_folder(name):
    """Start Menu, Startup and Desktop folders via the WScript shell."""
    return Path(wscript_shell().SpecialFolders(name))


def make_shortcut(path, target, arguments="", icon=None):
    shortcut = wscript_shell().CreateShortCut(str(path))
    shortcut.TargetPath = str(target)
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = str(Path(target).parent)
    shortcut.IconLocation = str(icon or target)
    shortcut.Save()


def shortcut_paths():
    programs = special_folder("Programs")
    return {
        "start_menu": programs / APP_NAME / f"{APP_NAME}.lnk",
        "start_uninstall": programs / APP_NAME / "Ducker - Uninstall.lnk",
        "old_start_menu": programs / f"{APP_NAME}.lnk",
        "startup": special_folder("Startup") / f"{APP_NAME}.lnk",
        "desktop": special_folder("Desktop") / f"{APP_NAME}.lnk",
    }


def remove_shortcuts():
    paths = shortcut_paths()
    for path in paths.values():
        path.unlink(missing_ok=True)
    try:
        paths["start_menu"].parent.rmdir()
    except OSError:
        pass


def installed_location():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
            location = Path(winreg.QueryValueEx(key, "InstallPath")[0])
        if (location / EXE_NAME).exists():
            return location
    except OSError:
        pass
    return None


def installed_version():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
            return winreg.QueryValueEx(key, "Version")[0]
    except OSError:
        return "unknown"


def stop_running_app():
    """Close the app, the old Spotify-named one, and any script-based copy."""
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
            old_exes = {(old + ".exe").lower() for old in OLD_APP_NAMES}
            if name == EXE_NAME.lower() or name in old_exes or (
                name in ("pythonw.exe", "python.exe") and ("auto_duck.py" in cmdline or "music_ducker.py" in cmdline or "ducker.py" in cmdline)
            ):
                proc.kill()
        except Exception:
            pass
    time.sleep(0.5)


def remove_old_installs(report):
    """Tidy away any earlier install under an old name, carrying settings over."""
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    for old_name, old_key, old_uninstall in OLD_INSTALLS:
        try:
            old_location = None
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, old_key) as key:
                    old_location = Path(winreg.QueryValueEx(key, "InstallPath")[0])
            except OSError:
                pass
            for key_path in (old_uninstall, old_key):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
                except OSError:
                    pass
            for folder in ("Programs", "Startup", "Desktop"):
                (special_folder(folder) / f"{old_name}.lnk").unlink(missing_ok=True)
            old_settings = appdata / old_name / "settings.json"
            if old_settings.exists() and not (DATA_DIR / "settings.json").exists():
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_settings, DATA_DIR / "settings.json")
            if old_location and old_location.exists():
                report(f"Removing the old {old_name} install...", 0.25)
                shutil.rmtree(old_location, ignore_errors=True)
        except Exception as error:
            report(f"Couldn't fully remove the old {old_name} install ({error}), carrying on", 0.25)


def write_registry(dest):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
        winreg.SetValueEx(key, "InstallPath", 0, winreg.REG_SZ, str(dest))
        winreg.SetValueEx(key, "Version", 0, winreg.REG_SZ, VERSION)

    exe = dest / EXE_NAME
    uninstaller = dest / UNINSTALLER_NAME
    size_kb = int(sum(f.stat().st_size for f in dest.glob("*") if f.is_file()) / 1024)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        values = {
            "DisplayName": APP_NAME,
            "Publisher": PUBLISHER,
            "DisplayIcon": str(exe),
            "InstallLocation": str(dest),
            "UninstallString": f'"{uninstaller}"',
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)


def remove_registry():
    for key_path in (UNINSTALL_KEY, APP_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            pass


def refresh_icon_cache():
    """Ask Windows to redraw icons, so shortcuts don't show a stale one."""
    try:
        subprocess.run(["ie4uinit.exe", "-show"], creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    except Exception:
        pass


def delete_after_exit(paths):
    """Delete files or folders once this program has closed.

    Windows won't let a running .exe delete itself, so a hidden command
    window waits a few seconds and then removes everything listed.
    """
    commands = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            commands.append(f'rmdir /s /q "{path}"')
        else:
            commands.append(f'del /f /q "{path}"')
    if not commands:
        return
    script = "ping 127.0.0.1 -n 6 >nul & " + " & ".join(commands)
    subprocess.Popen(
        f"cmd /c {script}",
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )


def do_install(dest, start_with_windows, desktop_shortcut, report):
    report("Closing any running copy...", 0.1)
    stop_running_app()
    remove_old_installs(report)

    report("Copying files...", 0.35)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUNDLED_EXE, dest / EXE_NAME)
    (dest / "Uninstall.exe").unlink(missing_ok=True)       # the old name
    if FROZEN:
        shutil.copy2(THIS_EXE, dest / UNINSTALLER_NAME)

    report("Creating shortcuts...", 0.65)
    exe = dest / EXE_NAME
    paths = shortcut_paths()
    paths["old_start_menu"].unlink(missing_ok=True)
    paths["start_menu"].parent.mkdir(parents=True, exist_ok=True)
    make_shortcut(paths["start_menu"], exe)
    if FROZEN:
        make_shortcut(paths["start_uninstall"], dest / UNINSTALLER_NAME, icon=exe)
    if start_with_windows:
        make_shortcut(paths["startup"], exe, "--minimized")
    else:
        paths["startup"].unlink(missing_ok=True)
    if desktop_shortcut:
        make_shortcut(paths["desktop"], exe)

    report("Registering with Windows...", 0.9)
    write_registry(dest)
    refresh_icon_cache()
    report("Installed.", 1.0)


def do_uninstall(dest, report):
    """Remove the program. Settings are kept so a reinstall picks them up."""
    report("Closing the app...", 0.15)
    stop_running_app()

    report("Removing shortcuts...", 0.4)
    remove_shortcuts()

    report("Removing registry entries...", 0.6)
    remove_registry()

    report("Removing program files...", 0.8)
    running_from_dest = FROZEN and THIS_EXE.parent == dest
    if running_from_dest:
        # We're running from inside the folder, so leave this one file behind.
        # The "remove everything" step deletes the folder once we've closed.
        for item in dest.iterdir():
            if item != THIS_EXE:
                try:
                    item.unlink()
                except Exception:
                    pass
    else:
        shutil.rmtree(dest, ignore_errors=True)
    report("Uninstalled.", 1.0)


def do_wipe(dest, report):
    """Remove every last trace, including settings, older versions and this program."""
    report("Removing settings and log...", 0.3)
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    programs = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs"
    for old_name, old_key, old_uninstall in OLD_INSTALLS:
        shutil.rmtree(appdata / old_name, ignore_errors=True)
        shutil.rmtree(programs / old_name, ignore_errors=True)
        for key_path in (old_key, old_uninstall):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as run_key:
            for name in (APP_NAME,) + OLD_APP_NAMES:
                try:
                    winreg.DeleteValue(run_key, name)
                except OSError:
                    pass
    except OSError:
        pass

    report("Removing leftover shortcuts...", 0.55)
    remove_shortcuts()
    for old_name in OLD_APP_NAMES:
        for folder in ("Programs", "Startup", "Desktop"):
            try:
                (special_folder(folder) / f"{old_name}.lnk").unlink(missing_ok=True)
            except Exception:
                pass

    report("Scheduling removal of this installer...", 0.8)
    leftovers = [dest]
    if FROZEN:
        leftovers.append(THIS_EXE)
    delete_after_exit(leftovers)
    report("Everything has been removed. This window will close in a moment.", 1.0)


def load_fonts():
    fonts_dir = RESOURCE_DIR / "fonts"
    loaded = False
    if fonts_dir.is_dir():
        for ttf in fonts_dir.glob("*.ttf"):
            try:
                if ctypes.windll.gdi32.AddFontResourceExW(str(ttf), 0x10, 0) > 0:
                    loaded = True
            except Exception:
                pass
    return "Poppins" if loaded else "Segoe UI"


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class SetupApp:
    def __init__(self):
        ctk.set_appearance_mode("dark")
        family = load_fonts()
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(MENU_NAME)
        self.root.resizable(False, False)
        # Centre on the screen the mouse is on
        width, height = 520, 540
        try:
            scale = self.root._get_window_scaling()
            point = wintypes.POINT()
            user32 = ctypes.windll.user32
            user32.GetCursorPos(ctypes.byref(point))
            monitor = user32.MonitorFromPoint(point, 2)

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            user32.GetMonitorInfoW(monitor, ctypes.byref(info))
            r = info.rcWork
            x = (r.left + (r.right - r.left - width * scale) / 2) / scale
            y = (r.top + (r.bottom - r.top - height * scale) / 2) / scale
            self.root.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
        except Exception:
            self.root.geometry(f"{width}x{height}")
        icon = RESOURCE_DIR / "icon.ico"
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass
        if family not in tkfont.families():
            family = "Segoe UI"
        self.f = {
            "title": ctk.CTkFont(family=family, size=24, weight="bold"),
            "sub": ctk.CTkFont(family=family, size=12),
            "h2": ctk.CTkFont(family=family, size=14, weight="bold"),
            "body": ctk.CTkFont(family=family, size=12),
            "small": ctk.CTkFont(family=family, size=11),
            "button": ctk.CTkFont(family=family, size=13, weight="bold"),
        }
        self.busy = False
        self._build()
        self.refresh()

    # ---- frame ----------------------------------------------------------------

    def _build(self):
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=32, pady=(26, 14))
        logo = self._logo_image(44)
        if logo is not None:
            ctk.CTkLabel(header, text="", image=logo).pack(side="left", padx=(0, 14))
        text = ctk.CTkFrame(header, fg_color="transparent")
        text.pack(side="left")
        ctk.CTkLabel(text, text=MENU_NAME, font=self.f["title"],
                     text_color=WHITE).pack(anchor="w")
        ctk.CTkLabel(text, text=f"By {PUBLISHER}", font=self.f["sub"],
                     text_color=GREY).pack(anchor="w")

        self.body = ctk.CTkFrame(self.root, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=32)

        footer = ctk.CTkFrame(self.root, fg_color=SURFACE, corner_radius=0, height=48)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        self.status = ctk.CTkLabel(footer, text="", font=self.f["body"], text_color=GREY)
        self.status.pack(side="left", padx=32)

    def _logo_image(self, size):
        try:
            from PIL import Image
            image = Image.open(RESOURCE_DIR / "icon.ico")
            image.size = (256, 256)
            image.load()
            return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))
        except Exception:
            return None

    def _link(self, widget, pad=4, radius=22):
        return widget

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def refresh(self):
        self.clear()
        location = installed_location()
        if location:
            self._installed_view(location)
        else:
            self._not_installed_view()

    def _card(self):
        card = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=14,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 14))
        return card

    def _button(self, text, command, style="primary"):
        styles = {
            "primary": dict(fg_color=GREEN, hover_color=GREEN_HOVER, text_color=BG),
            "secondary": dict(fg_color=CARD, hover_color=BORDER, text_color=WHITE,
                              border_width=1, border_color=BORDER),
            "danger": dict(fg_color="transparent", hover_color=CARD, text_color=RED,
                           border_width=1, border_color=RED),
            "danger_solid": dict(fg_color=RED, hover_color=RED_HOVER, text_color=WHITE),
        }
        return self._link(ctk.CTkButton(self.body, text=text, font=self.f["button"], corner_radius=22,
                                        height=42, command=command, **styles[style]))

    # ---- views ----------------------------------------------------------------

    def _installed_view(self, location):
        have = installed_version()
        newer = BUNDLED_EXE.exists() and have != VERSION

        card = self._card()
        ctk.CTkLabel(card, text="●  Installed", font=self.f["h2"],
                     text_color=GREEN).pack(anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(card, text=str(location), font=self.f["body"], text_color=GREY,
                     wraplength=420, justify="left").pack(anchor="w", padx=18, pady=(0, 14))

        if newer:
            self._button("Update available, install it now",
                         lambda: self.run_install(location, True, False)).pack(fill="x", pady=(4, 8))
            self._button("Open " + APP_NAME, lambda: self.launch(location), "secondary").pack(
                fill="x", pady=(0, 8))
        else:
            self._button("Open " + APP_NAME, lambda: self.launch(location)).pack(fill="x", pady=(4, 8))
            if BUNDLED_EXE.exists():
                self._button("Reinstall", lambda: self.run_install(location, True, False),
                             "secondary").pack(fill="x", pady=(0, 8))
        self._button("Uninstall", lambda: self.run_uninstall(location), "danger").pack(
            fill="x", pady=(16, 0))

    def _not_installed_view(self):
        card = self._card()
        ctk.CTkLabel(card, text="●  Not installed", font=self.f["h2"], text_color=GREY).pack(
            anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(card, text="When another sound plays, your music gets quieter. When it stops, "
                                "your music comes back. Works with Spotify, Apple Music, YouTube Music and more.",
                     font=self.f["body"], text_color=GREY, wraplength=420, justify="left").pack(
            anchor="w", padx=18, pady=(0, 14))

        if not BUNDLED_EXE.exists():
            ctk.CTkLabel(self.body, text="This copy of Setup has no app inside it. Run build.bat first.",
                         font=self.f["body"], text_color=RED).pack(anchor="w")
            return

        ctk.CTkLabel(self.body, text="INSTALL TO", font=self.f["small"], text_color=GREY).pack(
            anchor="w", pady=(4, 4))
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        self.dest_var = tk.StringVar(value=str(DEFAULT_DEST))
        ctk.CTkEntry(row, textvariable=self.dest_var, font=self.f["body"], fg_color=CARD,
                     border_color=BORDER, text_color=WHITE, corner_radius=10, height=38).pack(
            side="left", fill="x", expand=True)
        self._link(ctk.CTkButton(row, text="Browse", font=self.f["body"], fg_color=CARD, hover_color=BORDER,
                                 text_color=WHITE, border_width=1, border_color=BORDER, corner_radius=10,
                                 height=38, width=90, command=self.browse), 3, 12).pack(side="left", padx=(8, 0))

        self.start_var = tk.BooleanVar(value=True)
        self.desktop_var = tk.BooleanVar(value=False)
        for text, var in (("Start automatically when I log in to Windows", self.start_var),
                          ("Put a shortcut on my desktop", self.desktop_var)):
            self._link(ctk.CTkCheckBox(self.body, text=text, variable=var, font=self.f["body"],
                                       text_color=WHITE, fg_color=GREEN, hover_color=GREEN_HOVER,
                                       border_color=DIM), 6, 16).pack(anchor="w", pady=(12, 0))

        self._button("Install", self.install_clicked).pack(fill="x", pady=(24, 0))

    def _progress_view(self, title, colour):
        """A card with a progress bar and a running list of steps."""
        self.clear()
        card = self._card()
        ctk.CTkLabel(card, text=title, font=self.f["h2"], text_color=colour).pack(
            anchor="w", padx=18, pady=(14, 8))
        self.progress = ctk.CTkProgressBar(card, progress_color=colour, fg_color=BORDER,
                                           height=10, corner_radius=5)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=18, pady=(0, 10))
        self.steps = ctk.CTkLabel(card, text="", font=self.f["body"], text_color=GREY,
                                  justify="left", anchor="w")
        self.steps.pack(fill="x", padx=18, pady=(0, 14))
        self.step_lines = []

    def _wipe_question_view(self, location):
        self.clear()
        card = self._card()
        ctk.CTkLabel(card, text="Uninstalled", font=self.f["h2"], text_color=GREEN).pack(
            anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(card, text="The program and its shortcuts have been removed.",
                     font=self.f["body"], text_color=GREY, wraplength=420, justify="left").pack(
            anchor="w", padx=18, pady=(0, 14))

        ctk.CTkLabel(self.body, text="Do you want to completely remove all associated files?",
                     font=self.f["h2"], text_color=WHITE, wraplength=440, justify="left").pack(
            anchor="w", pady=(6, 4))
        ctk.CTkLabel(self.body, text="This includes this installer, your saved settings and log, and "
                                     "any outlying files. It will completely wipe everything of this "
                                     "program from your system.",
                     font=self.f["body"], text_color=GREY, wraplength=440, justify="left").pack(
            anchor="w", pady=(0, 18))

        self._button("Yes, remove everything", lambda: self.run_wipe(location), "danger_solid").pack(
            fill="x", pady=(0, 8))
        self._button("No, keep my settings and this installer", self.refresh, "secondary").pack(fill="x")

    # ---- actions ------------------------------------------------------------

    def browse(self):
        chosen = filedialog.askdirectory(title="Choose where to install", initialdir=str(DEFAULT_DEST.parent))
        if chosen:
            chosen = Path(chosen)
            if chosen.name.lower() != APP_NAME.lower():
                chosen = chosen / APP_NAME
            self.dest_var.set(str(chosen))

    def report(self, text, fraction=None):
        def update():
            self.status.configure(text=text)
            if fraction is not None and hasattr(self, "progress"):
                try:
                    self.progress.set(fraction)
                    self.step_lines.append(("✓  " if fraction >= 1 else "•  ") + text)
                    self.steps.configure(text="\n".join(self.step_lines))
                except Exception:
                    pass
        self.root.after(0, update)

    def _run(self, work):
        if self.busy:
            return
        self.busy = True

        def wrapped():
            try:
                work()
            except Exception as error:
                self.report(f"Something went wrong: {error}")
            finally:
                self.busy = False

        threading.Thread(target=wrapped, daemon=True).start()

    def install_clicked(self):
        self.run_install(Path(self.dest_var.get().strip()), self.start_var.get(), self.desktop_var.get())

    def run_install(self, dest, start_with_windows, desktop_shortcut):
        self._progress_view("Installing", GREEN)

        def work():
            do_install(dest, start_with_windows, desktop_shortcut, self.report)
            time.sleep(0.6)
            self.launch(dest)
            # The app takes over from here, so the Install Menu closes
            self.root.after(400, self.root.destroy)

        self._run(work)

    def run_uninstall(self, dest):
        self._progress_view("Uninstalling", RED)

        def work():
            do_uninstall(dest, self.report)
            time.sleep(0.8)
            self.root.after(0, lambda: self._wipe_question_view(dest))

        self._run(work)

    def run_wipe(self, dest):
        self._progress_view("Removing everything", RED)

        def work():
            do_wipe(dest, self.report)
            time.sleep(2.0)
            self.root.after(0, self.root.destroy)

        self._run(work)

    def launch(self, location):
        try:
            subprocess.Popen([str(location / EXE_NAME)], cwd=str(location))
            self.root.after(400, self.root.destroy)
        except Exception as error:
            self.report(f"Could not open the app: {error}")

    def run(self):
        self.root.mainloop()


class UninstallDialog:
    """What runs when the installed copy is opened as "Ducker - Uninstall".
    One message, one red button, and everything goes."""

    def __init__(self):
        ctk.set_appearance_mode("dark")
        family = load_fonts()
        self.root = ctk.CTk(fg_color=BG)
        self.root.title("Uninstall " + APP_NAME)
        self.root.resizable(False, False)
        icon = RESOURCE_DIR / "icon.ico"
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass
        if family not in tkfont.families():
            family = "Segoe UI"
        self.f = {
            "title": ctk.CTkFont(family=family, size=20, weight="bold"),
            "body": ctk.CTkFont(family=family, size=12),
            "button": ctk.CTkFont(family=family, size=13, weight="bold"),
        }
        self.busy = False
        inner = ctk.CTkFrame(self.root, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=28, pady=22)
        head = ctk.CTkFrame(inner, fg_color="transparent")
        head.pack(fill="x")
        logo = SetupApp._logo_image(self, 36)
        if logo is not None:
            ctk.CTkLabel(head, text="", image=logo).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(head, text="Uninstall " + APP_NAME, font=self.f["title"], text_color=WHITE).pack(side="left")
        ctk.CTkLabel(inner, text="Ducker is safe to remove at any time. It only adjusts volume, the same sliders "
                                 "you can move yourself in the Volume Mixer. Uninstalling puts everything back "
                                 "exactly as it was.",
                     font=self.f["body"], text_color=GREY, wraplength=430, justify="left").pack(anchor="w", pady=(12, 8))
        ctk.CTkLabel(inner, text="Everything associated with Ducker is removed from this PC: the program and its "
                                 "folder, your settings, the log, every shortcut, the startup entry, its registry "
                                 "entries and anything left by older versions. Nothing stays behind.",
                     font=self.f["body"], text_color=GREY, wraplength=430, justify="left").pack(anchor="w", pady=(0, 16))
        self.status = ctk.CTkLabel(inner, text="", font=self.f["body"], text_color=GREY)
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x")
        self.go = ctk.CTkButton(row, text="Uninstall " + APP_NAME, font=self.f["button"], corner_radius=22, height=40,
                                fg_color=RED, hover_color=RED_HOVER, text_color=WHITE, command=self.start)
        self.go.pack(side="left")
        self.keep = ctk.CTkButton(row, text="Keep " + APP_NAME, font=self.f["button"], corner_radius=22, height=40,
                                  fg_color=CARD, hover_color=BORDER, text_color=WHITE, border_width=1,
                                  border_color=BORDER, command=self.root.destroy)
        self.keep.pack(side="left", padx=(10, 0))
        self.root.update_idletasks()
        scale = self.root._get_window_scaling()
        width, height = 500, int(self.root.winfo_reqheight() / scale + 10)
        try:
            user32 = ctypes.windll.user32
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            self.root.geometry(f"{width}x{height}+{int((sw - width * scale) / 2)}+{int((sh - height * scale) / 2)}")
        except Exception:
            self.root.geometry(f"{width}x{height}")

    def report(self, text, fraction=None):
        self.root.after(0, lambda: self.status.configure(text=text))

    def start(self):
        if self.busy:
            return
        self.busy = True
        self.go.configure(state="disabled")
        self.keep.configure(state="disabled")
        self.status.pack(anchor="w", pady=(0, 10), before=self.go.master)
        location = installed_location() or (THIS_EXE.parent if FROZEN else None)

        def work():
            try:
                if location is not None:
                    do_uninstall(location, self.report)
                    do_wipe(location, self.report)
                else:
                    do_wipe(DEFAULT_DEST, self.report)
            except Exception as error:
                self.report(f"Something went wrong: {error}")
                self.busy = False
                return
            self.root.after(0, self.finished)

        threading.Thread(target=work, daemon=True).start()

    def finished(self):
        try:
            self.root.withdraw()
        except Exception:
            pass
        ctypes.windll.user32.MessageBoxW(0, f"{APP_NAME} is uninstalled.", APP_NAME, 0x40 | 0x40000)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if FROZEN and THIS_EXE.name.lower() == UNINSTALLER_NAME.lower():
        UninstallDialog().run()
    else:
        SetupApp().run()
