"""Offline checks for the Companion's tray icon pieces (tray.py) and supervisor status (sr20_companion.helper_states).

    python -I tools/companion/test_tray.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("SR20_LOGDIR", tempfile.mkdtemp())
import tray as T  # noqa: E402
import sr20_companion as C  # noqa: E402

n_ok = n_bad = 0


def ok(cond, what):
    global n_ok, n_bad
    if cond:
        n_ok += 1
    else:
        n_bad += 1
        print("FAIL", what)


# launch command for Start with Windows
t, a, w = T.launch_command(True, r"C:\Apps\SR20 Companion\SR20Companion.exe", "ignored")
ok(t.endswith("SR20Companion.exe") and a == "--background" and w == r"C:\Apps\SR20 Companion", "frozen: the exe, quietly")
t, a, w = T.launch_command(False, sys.executable, r"C:\repo\tools\companion\sr20_companion.py")
ok(a == '"C:\\repo\\tools\\companion\\sr20_companion.py" --background' and w == r"C:\repo\tools\companion",
   "from the repo: python with the script, quietly")
ok(os.path.basename(t).lower() in ("pythonw.exe", "python.exe"), "from the repo: pythonw when it exists " + t)

# create / remove against a temporary Startup folder (real PowerShell COM, no admin)
d = tempfile.mkdtemp()
ok(not T.startup_on(d), "no shortcut at first")
ok(T.set_startup(True, True, r"C:\Apps\SR20 & Co\SR20Companion.exe", "x", folder=d) and T.startup_on(d),
   "Start with Windows on creates the shortcut (a path with & in it)")
ok(T.set_startup(False, True, "x", "x", folder=d) and not T.startup_on(d), "off removes it")
ok(T.set_startup(False, True, "x", "x", folder=d), "off again is fine")

# status text
st = {"taws": "on", "glide": "on", "surfacewatch": "on", "efb": "on", "yoke": "no yoke connected"}
tip, prob = T.summary(st, True)
ok(prob is None and tip == "SR20 Companion: everything is working", "all good: " + tip)
ok(T.light(st) == "green" and T.headline(st) == "Everything is working", "green light when all work")
ok(T.status_row("taws", "on") == "\u2713  Terrain alerts (TAWS)", "a working feature reads as a status, not a switch")
ok(T.status_row("yoke", "no yoke connected").endswith("(no yoke connected)") and "on" not in T.status_row("glide", "on").split(),
   "no bare 'on' anywhere")
ok(T.light(dict(st, glide="starting")) == "amber", "amber while starting")
tip, prob = T.summary(st, False)
ok(tip.endswith("(sim not running)"), "sim off noted, not a problem")
st["surfacewatch"] = "stopped: the SR20 aircraft isn't in the Community2024 folder"
tip, prob = T.summary(st, True)
ok(prob and "Airport maps" in prob and "Community2024" in tip, "a stopped feature, named for what the pilot sees: " + tip)
ok(T.light(st) == "red" and T.headline(st) == "Something needs attention", "red light when something stopped")
ok(T.status_row("surfacewatch", st["surfacewatch"]).startswith("\u2717  Airport maps (SafeTaxi, SurfaceWatch): the SR20"),
   "stopped row says why: " + T.status_row("surfacewatch", st["surfacewatch"]))


class K:
    def __init__(self, p=None, dead=False, quick=0, why=None):
        self.p, self.dead, self.quick, self.why, self.t0 = p, dead, quick, why, __import__("time").time()


kids = {"glide": K(p=1), "surfacewatch": K(dead=True, why="x"), "taws": K(p=1, quick=1), "efb": K(dead=True), "yoke": K()}
s = C.helper_states(kids, False)
ok(s == {"glide": "on", "surfacewatch": "stopped: x", "taws": "restarting", "efb": "stopped: keeps failing (see the log)",
         "yoke": "no yoke connected"}, "helper states: " + str(s))
ok(C.helper_states({"yoke": K()}, True)["yoke"] == "on", "a yoke on the bus but not yet started counts as on")
ok(C.helper_states({"yoke": K()}, True, False)["yoke"] == "off (turned off in setup)", "yoke feel turned off in setup")
ok(T.status_row("yoke", "off (turned off in setup)").startswith("–"), "off in setup reads as a dash, not a fault")
import json as _j  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    sp = os.path.join(td, "settings.json")
    ok(C.load_settings(sp) == {"yoke_feel": True}, "no settings file: yoke feel on")
    open(sp, "w").write(_j.dumps({"yoke_feel": False}))
    ok(C.load_settings(sp) == {"yoke_feel": False}, "settings file turns yoke feel off")
    open(sp, "w").write("{bad")
    ok(C.load_settings(sp) == {"yoke_feel": True}, "malformed settings file: defaults")
    open(sp, "w").write(_j.dumps({"yoke_feel": "no"}))
    ok(C.load_settings(sp) == {"yoke_feel": True}, "non-boolean value ignored")
    open(sp, "w").write(_j.dumps({"yoke_feel": False, "community_dir": r"D:\Sim\Community2024"}))
    ok(C.load_settings(sp) == {"yoke_feel": False, "community_dir": r"D:\Sim\Community2024"}, "folder chosen in setup")
    open(sp, "w").write(_j.dumps({"community_dir": 5}))
    ok(C.load_settings(sp) == {"yoke_feel": True}, "non-string folder ignored")

try:
    import queue
    import pystray  # noqa: F401
    q = queue.Queue()
    tr = T.Tray(q, lambda: {"taws": "on", "glide": "starting", "surfacewatch": "on", "efb": "on", "yoke": "on"}, lambda: True, False, sys.executable, "x.py", ".", lambda: None)
    menu = tr._menu()
    texts = [i.text if isinstance(i.text, str) else i.text(i) for i in menu.items if i is not pystray.Menu.SEPARATOR]
    ok("iPad setup…" in texts and "\u2713  Terrain alerts (TAWS)" in texts and "\u2713  Sim running" in texts
       and "\u2026  Glide ring (starting)" in texts and "Quit SR20 Companion" in texts and "Starting up\u2026" in texts,
       "menu built: " + str(texts))
    [i for i in menu.items if getattr(i, "text", "") == "Quit SR20 Companion"][0](None)
    ok(q.get_nowait() == "quit", "Quit only queues a command for the supervisor")
    ok(tr._end_session(0, 0) == 0 and q.empty(), "a cancelled log-off does nothing")
    tr.stopped.set()
    ok(tr._end_session(1, 0) == 0 and q.get_nowait() == "quit", "log-off asks the supervisor to stop the helpers")
except ImportError:
    pass   # pystray not in this interpreter (python -I); the frozen build carries it
try:
    img = T.make_image()
    ok(img.size == (64, 64) and img.getpixel((32, 32))[3] == 255, "icon drawn")
    ok(T.make_image(dot="red").getpixel((50, 50))[:3] == (230, 40, 40), "red light drawn in the corner")
except ImportError:
    pass   # Pillow not in this interpreter (python -I hides user packages); the frozen build carries it

print(f"{n_ok} ok, {n_bad} failed")
sys.exit(1 if n_bad else 0)
