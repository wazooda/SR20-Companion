"""SR20 Companion's notification-area icon (docs/plans/companion_tray_installer.md §1, reviewed 2026-11-16).

The one visible handle on the Companion: right-click the SR20 icon by the clock.
    iPad setup...              opens the setup page (QR codes for the iPad)
    <feature>: <state>         five greyed rows, named for what the pilot sees, not the helper
    Sim: running / not running
    Start with Windows (tick)  the per-user Startup shortcut "SR20 Companion" (no admin)
    Restart all / Open log folder / Quit SR20 Companion

pystray runs its own Windows message loop on this module's thread (pystray handles the hidden window, Explorer
restarts and menu placement). Menu callbacks never touch the supervisor's Child objects: they put a command on the
supervisor's queue ("restart", "quit"), which only the main loop acts on.
"""
import os
import subprocess
import sys
import threading

FEATURES = [("taws", "Terrain alerts (TAWS)"), ("glide", "Glide ring"),
            ("surfacewatch", "Airport maps (SafeTaxi, SurfaceWatch)"), ("efb", "iPad and ForeFlight"),
            ("yoke", "RealSimGear yoke feel")]
LABEL = dict(FEATURES)
SHORTCUT_NAME = "SR20 Companion.lnk"
NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- Start with Windows

def startup_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def shortcut_path(folder=None):
    return os.path.join(folder or startup_dir(), SHORTCUT_NAME)


def launch_command(frozen, executable, script):
    """-> (target, arguments, working folder) that start this Companion quietly at log-on."""
    if frozen:
        return executable, "--background", os.path.dirname(executable)
    exe = executable
    if os.path.basename(exe).lower() == "python.exe":          # no console window at log-on
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.exists(w) else exe
    return exe, f'"{script}" --background', os.path.dirname(script)


def set_startup(on, frozen, executable, script, folder=None, run=subprocess.run):
    """Create or remove the Startup shortcut. The values travel as environment variables, so no path is ever pasted
    into PowerShell code. -> True when the shortcut now matches `on`."""
    path = shortcut_path(folder)
    if not on:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return not os.path.exists(path)
    target, args, wd = launch_command(frozen, executable, script)
    env = dict(os.environ, SR20_LNK=path, SR20_TARGET=target, SR20_ARGS=args, SR20_WD=wd)
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SR20_LNK); $s.TargetPath = $env:SR20_TARGET; "
          "$s.Arguments = $env:SR20_ARGS; $s.WorkingDirectory = $env:SR20_WD; "
          "$s.Description = 'SR20 Companion: TAWS, glide ring, airport maps, iPad link, yoke feel'; $s.Save()")
    pwsh = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        run([pwsh, "-NoProfile", "-NonInteractive", "-Command", ps], env=env, capture_output=True, timeout=20,
            creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return os.path.exists(path)


def startup_on(folder=None):
    return os.path.exists(shortcut_path(folder))


# ---------------------------------------------------------------- status text

def summary(states, sim):
    """states: {helper: "on" | "starting" | "restarting" | "stopped: why" | "no yoke connected"}
    -> (tooltip, first problem or None)."""
    problems = [f"{LABEL[n]}: {s[len('stopped: '):] if s.startswith('stopped: ') else s}" for n, s in states.items()
                if s.startswith("stopped")]
    if problems:
        return "SR20 Companion: " + problems[0], problems[0]
    if any(s in ("starting", "restarting") for s in states.values()):
        return "SR20 Companion: starting", None
    return "SR20 Companion: everything is working" + ("" if sim else " (sim not running)"), None


def light(states):
    """The colour of the dot on the icon: green all working, amber starting, red something stopped."""
    if any(s.startswith("stopped") for s in states.values()):
        return "red"
    if any(s in ("starting", "restarting") for s in states.values()):
        return "amber"
    return "green"


def status_row(name, state):
    """One menu line that reads as a status, never as a switch (the author 2026-11-16: "on makes me think I can turn it off")."""
    label = LABEL[name]
    if state == "on":
        return f"\u2713  {label}"                                  # check mark
    if state == "no yoke connected":
        return f"\u2013  {label} (no yoke connected)"
    if state.startswith("off"):
        return f"\u2013  {label} (off)"                             # turned off in setup: a choice, not a fault
    if state in ("starting", "restarting"):
        return f"\u2026  {label} (starting)"
    why = state[len("stopped: "):] if state.startswith("stopped: ") else state
    return f"\u2717  {label}: {why}"                               # cross


def headline(states):
    c = light(states)
    return {"green": "Everything is working", "amber": "Starting up\u2026", "red": "Something needs attention"}[c]


# ---------------------------------------------------------------- the icon

LIGHTS = {"green": (46, 204, 64, 255), "amber": (255, 176, 0, 255), "red": (230, 40, 40, 255)}


def make_image(size=64, dot=None):
    """The SR20 icon; dot = "green" | "amber" | "red" adds the status light in the lower right corner."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    d.ellipse([2 * s, 2 * s, 62 * s, 62 * s], fill=(0, 120, 143, 255))       # avionics cyan disc
    plane = [(32, 9), (35, 22), (56, 30), (56, 35), (35, 32), (34, 46), (41, 51), (41, 55), (32, 52), (23, 55), (23, 51),
             (30, 46), (29, 32), (8, 35), (8, 30), (29, 22)]                    # a generic low-wing single, top view
    d.polygon([(x * s, y * s) for x, y in plane], fill=(255, 255, 255, 255))
    if dot in LIGHTS:
        d.ellipse([36 * s, 36 * s, 64 * s - 1, 64 * s - 1], fill=(20, 20, 20, 255))           # dark ring so it shows on any taskbar
        d.ellipse([39 * s, 39 * s, 61 * s - 1, 61 * s - 1], fill=LIGHTS[dot])
    return img


class Tray:
    WM_QUERYENDSESSION, WM_ENDSESSION, WM_REFRESH = 0x0011, 0x0016, 0x8000 + 1   # WM_APP + 1

    def __init__(self, commands, get_states, get_sim, frozen, executable, script, logdir, open_setup):
        self.commands, self.get_states, self.get_sim = commands, get_states, get_sim
        self.frozen, self.executable, self.script, self.logdir = frozen, executable, script, logdir
        self.open_setup = open_setup
        self.icon = None
        self.thread = None
        self.stopped = threading.Event()     # set by the supervisor once the helpers are stopped

    def _menu(self):
        import pystray
        M = pystray.MenuItem
        items = [M("iPad setup…", lambda i, it: self.open_setup(), default=True), pystray.Menu.SEPARATOR,
                 M(lambda it: headline(self.get_states()), None, enabled=False)]
        for name, label in FEATURES:
            items.append(M((lambda n: lambda it: status_row(n, self.get_states().get(n, "starting")))(name), None, enabled=False))
        items.append(M(lambda it: "\u2713  Sim running" if self.get_sim() else "\u2013  Sim not running", None, enabled=False))
        items += [pystray.Menu.SEPARATOR,
                  M("Start with Windows", self._toggle_startup, checked=lambda it: startup_on()),
                  M("Restart all", lambda i, it: self.commands.put("restart")),
                  M("Open log folder", lambda i, it: os.startfile(self.logdir) if os.path.isdir(self.logdir) else None),
                  M("Quit SR20 Companion", lambda i, it: self.commands.put("quit"))]
        return pystray.Menu(*items)

    def _toggle_startup(self, icon, item):
        set_startup(not startup_on(), self.frozen, self.executable, self.script)

    def start(self):
        """-> True when the icon is up (False without pystray / Pillow: the Companion runs on without it)."""
        try:
            import pystray
            self.icon = pystray.Icon("SR20Companion", make_image(dot="amber"), "SR20 Companion: starting", self._menu())
            self.shown_light = "amber"
            h = getattr(self.icon, "_message_handlers", None)
            if isinstance(h, dict):
                # Windows log-off / shutdown: pystray answers unknown messages with 0, which vetoes WM_QUERYENDSESSION
                # and skips the helpers' clean-up (the yoke left stiff). Say yes, then stop the helpers before Windows
                # ends the process.
                h[self.WM_QUERYENDSESSION] = lambda w, l: 1
                h[self.WM_ENDSESSION] = self._end_session
                h[self.WM_REFRESH] = self._do_refresh     # menu rebuilt on the tray's own thread
                for k, fn in list(h.items()):              # TrackPopupMenuEx runs inside pystray's notify handler
                    if getattr(fn, "__name__", "") == "_on_notify":
                        h[k] = self._wrap_notify(fn)
        except Exception:  # noqa: BLE001
            self.icon = None
            return False
        self.thread = threading.Thread(target=self.icon.run, daemon=True)
        self.thread.start()
        return True

    def _end_session(self, wparam, lparam):
        if wparam:
            self.commands.put("quit")
            self.stopped.wait(8)
        return 0

    menu_open = False
    pending = False
    shown_light = None

    def _wrap_notify(self, fn):
        def wrapped(wparam, lparam):
            self.menu_open = True
            try:
                return fn(wparam, lparam)
            finally:
                self.menu_open = False
                if self.pending:
                    self.pending = False
                    self._do_refresh(0, 0)
        return wrapped

    def _do_refresh(self, wparam, lparam):
        if self.menu_open:            # a posted refresh can run inside the open menu's own message loop: later
            self.pending = True
            return 0
        try:
            st = self.get_states()
            tip, _ = summary(st, self.get_sim())
            self.icon.title = tip[:127]
            c = light(st)
            if c != self.shown_light:
                self.shown_light = c
                self.icon.icon = make_image(dot=c)
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            pass
        return 0

    def refresh(self):
        """From the supervisor thread: ask the tray thread to rebuild (an open menu is never destroyed under it)."""
        hwnd = getattr(self.icon, "_hwnd", None) if self.icon else None
        if not hwnd:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), self.WM_REFRESH, 0, 0)
        except Exception:  # noqa: BLE001
            pass

    def notify(self, text):
        if self.icon:
            try:
                self.icon.notify(text, "SR20 Companion")
            except Exception:  # noqa: BLE001
                pass

    def stop(self):
        """Remove the icon and wait for it, so no ghost icon is left when a new Companion starts straight after."""
        self.stopped.set()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
            if self.thread:
                self.thread.join(3)
