"""The "SR20 iPad setup" page (http://127.0.0.1:47822/setup, served by the EFB helper on loopback) and the firewall
repair (docs/plans/fpl_without_tailscale.md §2.4, §2.6).

The page shows the pairing QR (the URL http://<ip>:47823/#p=<code>, read by the "SR20 setup" Shortcut and by the
iPad's Camera for the upload page), the status of the LAN listener / ForeFlight / the last iPad that reached this PC,
and the network category and firewall rules for the helper's executable. Its buttons are same-origin POSTs carrying
the page's nonce (efb_server.Handler.do_POST checks Origin and nonce).

Firewall:
    read   PowerShell Get-NetConnectionProfile / Get-NetFirewallApplicationFilter as JSON (enum numbers and enum names,
           which are not localised); run when the page asks, asynchronously, with no window.
    repair the helper elevates a fixed mode of the program (no shell string):
               SR20Companion.exe --fix-firewall                      frozen
               python efb_setup.py --fix-firewall --program <exe>    from the repo
           which runs netsh.exe with argument lists: delete the program's own rules (frozen only: from the repo the
           program is python.exe, whose other rules are not ours), delete ours by name, then add TCP 47823 and UDP 63093
           inbound, private profile, remote LocalSubnet.
"""
import html
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
NO_WINDOW = 0x08000000
RULES = [("SR20 Companion - iPad flight plans (TCP 47823)", "TCP", "47823"),
         ("SR20 Companion - ForeFlight discovery (UDP 63093)", "UDP", "63093")]
# iCloud links of the two Shortcuts, filled in once the author has made and shared them
SHORTCUT_SEND_URL = "https://www.icloud.com/shortcuts/7e81c0c85b11405eb921080b0456500c"   # shared by the author 2026-11-16 (second share: full actions, Overwrite on); efb.json "shortcut_url" overrides
SHORTCUT_NAME = "Send to SR20"


def run_shortcut_url(send_address):
    """QR 2: opens the installed Shortcut with the send address as its text input; the Shortcut saves it."""
    from urllib.parse import quote
    return f"shortcuts://run-shortcut?name={quote(SHORTCUT_NAME)}&input=text&text={quote(send_address, safe='')}"
CATEGORY = {0: "Public", 1: "Private", 2: "Domain"}

PS_STATUS = r"""
$errs = @()
$a = $null; $p = @(); $f = @(); $r = @()
try { $a = (Get-NetIPAddress -IPAddress $env:SR20_IP -ErrorAction Stop).InterfaceAlias } catch { }
try { $p = @(Get-NetConnectionProfile -ErrorAction Stop | ForEach-Object { @{alias=$_.InterfaceAlias; name=$_.Name; category=[int]$_.NetworkCategory} }) }
catch { $errs += 'profiles: ' + $_.Exception.Message }
try { $f = @(Get-NetFirewallApplicationFilter -Program $env:SR20_EXE -ErrorAction Stop) }
catch { if ($_.CategoryInfo.Category -ne 'ObjectNotFound') { $errs += 'rules: ' + $_.Exception.Message } }
try {
  $r = @($f | Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.Direction -eq 'Inbound' } | ForEach-Object {
    $pf = $_ | Get-NetFirewallPortFilter -ErrorAction Stop
    @{name=$_.DisplayName; enabled=[string]$_.Enabled; action=[string]$_.Action; profile=[int]$_.Profile;
      protocol=[string]$pf.Protocol; port=(@($pf.LocalPort) -join ',')} })
} catch { $errs += 'rules: ' + $_.Exception.Message }
@{alias=$a; profiles=$p; rules=$r; errors=$errs} | ConvertTo-Json -Depth 4 -Compress
"""


def program():
    """The executable that listens (and so the one the firewall rules name). In a venv sys.executable is the launcher,
    and the listening process is the base interpreter."""
    return sys.executable if FROZEN else getattr(sys, "_base_executable", sys.executable)


def _powershell():
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


# ---------------------------------------------------------------- firewall

def firewall_status(ip, exe=None, timeout=20):
    exe = exe or program()
    env = dict(os.environ, SR20_EXE=exe, SR20_IP=ip or "")
    try:
        r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", PS_STATUS], capture_output=True,
                           text=True, timeout=timeout, env=env, creationflags=NO_WINDOW)
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"ok": False, "why": f"Couldn't read the firewall ({e.__class__.__name__})."}
    if not isinstance(d, dict) or r.returncode != 0:
        return {"ok": False, "why": "Couldn't read the firewall."}
    return judge(d, exe)


def _list(x):
    return x if isinstance(x, list) else ([x] if isinstance(x, dict) else [])


def judge(d, exe):
    """PowerShell's answer -> what the page says. Profile flags: 0 any, 1 domain, 2 private, 4 public. A rule counts
    only if it covers what the iPad needs: TCP 47823 (protocol TCP or Any, local port Any or 47823 or a range with it)."""
    errs = d.get("errors")
    errs = [errs] if isinstance(errs, str) else [e for e in _list(errs) if isinstance(e, str)]
    if errs:
        return {"ok": False, "why": "Couldn't read the firewall (" + "; ".join(errs)[:200] + ")."}
    alias = d.get("alias")
    profiles = _list(d.get("profiles"))
    mine = next((p for p in profiles if p.get("alias") == alias), None)
    cat = CATEGORY.get(mine.get("category")) if mine else None
    rules = [r for r in _list(d.get("rules")) if r.get("enabled") == "True"]

    def covers(r, flag):
        return (r.get("profile", 0) == 0 or (r.get("profile", 0) & flag)) and covers_port(r, "TCP", 47823)

    flag = {"Public": 4, "Domain": 1}.get(cat, 2)
    block = [r.get("name") for r in rules if r.get("action") == "Block" and covers(r, flag)]
    allow = [r.get("name") for r in rules if r.get("action") == "Allow" and covers(r, flag)]
    out = {"ok": True, "network": mine.get("name") if mine else None, "interface": alias, "category": cat,
           "block": block, "allow": allow, "program": exe, "frozen": FROZEN}
    if cat == "Public":
        out["verdict"] = "public"
    elif cat == "Domain" and not allow:
        out["verdict"] = "domain"       # the repair adds private-profile rules only
    elif block:
        out["verdict"] = "blocked"
    elif not allow:
        out["verdict"] = "no-rule"
    else:
        out["verdict"] = "allowed"
    return out


def covers_port(r, proto, port):
    if str(r.get("protocol", "Any")).upper() not in (proto, "ANY"):
        return False
    for part in str(r.get("port", "Any")).split(","):
        part = part.strip()
        if part.lower() == "any" or part == str(port):
            return True
        lo, _, hi = part.partition("-")
        if hi and lo.isdigit() and hi.isdigit() and int(lo) <= port <= int(hi):
            return True
    return False


def fix_firewall(prog, frozen, run=subprocess.run):
    """Runs elevated. -> exit code: 0 both rules added, 1 an add failed."""
    netsh = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")
    base = [netsh, "advfirewall", "firewall"]
    if frozen:   # also clears a Block rule a Cancel on Windows' prompt left behind
        run(base + ["delete", "rule", "name=all", f"program={prog}"], capture_output=True, creationflags=NO_WINDOW)
    for name, _, _ in RULES:
        run(base + ["delete", "rule", f"name={name}"], capture_output=True, creationflags=NO_WINDOW)
    rc = 0
    for name, proto, port in RULES:
        r = run(base + ["add", "rule", f"name={name}", "dir=in", "action=allow", f"program={prog}", f"protocol={proto}",
                        f"localport={port}", "profile=private", "remoteip=localsubnet", "enable=yes"],
                capture_output=True, creationflags=NO_WINDOW)
        if getattr(r, "returncode", 1) != 0:
            rc = 1
    return rc


def elevate_fix(timeout_s=180):
    """-> "done" | "failed" | "cancelled" | "error: ...". Shows Windows' UAC prompt on this PC."""
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    exe = program()
    if FROZEN:
        params = "--fix-firewall"
    else:
        params = f'"{os.path.join(HERE, "efb_setup.py")}" --fix-firewall --program "{exe}"'
    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    # NOCLOSEPROCESS | NOASYNC (this thread has no message loop); the foreground window as owner, so the UAC prompt
    # comes up in front of the setup page instead of minimised in the taskbar
    sei = SEI(cbSize=ctypes.sizeof(SEI), fMask=0x40 | 0x100, hwnd=user32.GetForegroundWindow(), lpVerb="runas",
              lpFile=exe, lpParameters=params, nShow=0)
    sh = ctypes.WinDLL("shell32", use_last_error=True)
    sh.ShellExecuteExW.argtypes = [ctypes.POINTER(SEI)]
    sh.ShellExecuteExW.restype = wintypes.BOOL
    if not sh.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        return "cancelled" if err == 1223 else f"error: Windows error {err}"
    if not sei.hProcess:
        return "error: Windows started no process"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        if k32.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000)) != 0:
            return "error: timed out"
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return "done" if code.value == 0 else "failed"
    finally:
        k32.CloseHandle(sei.hProcess)


# ---------------------------------------------------------------- the page

def qr_svg(text):
    try:
        import segno
    except ImportError:
        return None
    import io
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False,
                                     svgns=True, nl=False)
    return buf.getvalue().decode("utf-8")


def _ago(t):
    if not t:
        return None
    s = time.time() - t
    return "just now" if s < 60 else f"{int(s // 60)} min ago" if s < 3600 else time.strftime("%H:%M", time.localtime(t))


def context(ctx, pending):
    import efb_lan
    last = ctx.ipads.last if ctx.ipads else None
    addrs = efb_lan.candidates(last)
    code, left = ctx.pairing.current_secret()
    try:
        key = ctx.pairing.send_key()
    except OSError:
        key = None
    return {"addresses": addrs, "code": code, "key": key, "left_min": max(1, int(left // 60)),
            "lan": ctx.lan.state if ctx.lan else "not started", "heard": ctx.ipads.heard() if ctx.ipads else [],
            "last_plan": getattr(ctx.lan, "last_plan", None) if ctx.lan else None, "mfd": pending.mfd_alive(),
            "settings_error": ctx.settings_error, "shortcut_url": getattr(ctx, "shortcut_url", None)}


def _clock(t):
    return time.strftime("%I:%M %p", time.localtime(t)).lstrip("0")


def page(c, nonce):
    e = html.escape
    addrs = c["addresses"]
    main = addrs[0] if addrs else None

    # ---- the two setup codes
    shortcut = c.get("shortcut_url") or SHORTCUT_SEND_URL
    svg1 = qr_svg(shortcut) if shortcut else None
    if shortcut:
        code1 = (f'<div class="qr">{svg1}</div>' if svg1 else f'<p><a href="{e(shortcut)}">{e(shortcut)}</a></p>')
    else:
        code1 = '<p class="bad">Not available yet in this build.</p>'
    code2 = ""
    if main and c["key"]:
        # an ordinary web address: the iPad Camera won't open a shortcuts:// link from a QR code (the author, 2026-11-16);
        # the page it opens has the Connect button that runs the Shortcut with this PC's send address
        connect = f"http://{main}:47823/connect#k={c['key']}"
        svg2 = qr_svg(connect)
        code2 = f'<div class="qr">{svg2}</div>' if svg2 else f'<p><code>{e(connect)}</code></p>'
    elif not main:
        code2 = '<p class="bad">This PC isn\'t on a network. Connect it to your home Wi-Fi or router, then reload this page.</p>'
    else:
        code2 = '<p class="bad">Something went wrong. Restart the SR20 Companion, then reload this page.</p>'

    # ---- status, in plain words
    def row(ok, text):   # ok: True (tick), False (problem), None (just information)
        return f'<li class="{"ok" if ok else "info" if ok is None else "bad"}">{text}</li>'

    rows = []
    if c["lan"] == "ready":
        rows.append(row(True, "Ready to receive flight plans from your iPad."))
    else:
        rows.append(row(False, f"Can't receive flight plans: {e(c['lan'])}. Restart the SR20 Companion."))
    lp = c["last_plan"]
    if lp:
        rows.append(row(True, f"Last flight plan received at {e(_clock(lp[1]))}: {e(lp[2])}."))
    else:
        rows.append(row(None, "No flight plan received yet."))
    rows.append(row(True, "The sim is running and the MFD is ready.") if c["mfd"] else
                row(None, "The sim isn't running. Plans you send will wait and pop up once it is."))
    if c["heard"]:
        rows.append(row(True, f"ForeFlight's moving map is getting the sim's position ({e(', '.join(c['heard']))})."))
    if c["settings_error"]:
        rows.append(row(False, e(c["settings_error"])))

    # ---- other addresses and the Safari way, for the few who need them
    safari = ""
    if main:
        url = f"http://{main}:47823/#p={c['code']}"
        svg3 = qr_svg(url)
        safari = (f'<div class="qr small">{svg3}</div>' if svg3 else "") + \
            f'<p class="muted">This code works for {c["left_min"]} more minutes. <button data-act="new-code">Make a new one</button></p>'
    others = ""
    if len(addrs) > 1:
        others = '<p class="muted">This PC also has these network addresses (VPN, Tailscale, virtual networks): ' + \
            ", ".join(e(a) for a in addrs[1:]) + '. The codes above use ' + e(main or "") + '.</p>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="sr20-nonce" content="{e(nonce)}">
<title>SR20 iPad setup</title><link rel="stylesheet" href="/setup.css"></head><body><main>
<h1>Send flight plans from your iPad</h1>
<p class="lede">Build your route in ForeFlight, send it, and it pops up on the SR20's MFD, the way FlightStream works in the
airplane. Your iPad and this PC just need to be on the same home Wi-Fi.</p>

<section><h2>Set up your iPad</h2>
<p>Do this once on each iPad. It takes about a minute. Use the iPad's own <b>Camera</b> app.</p>
<div class="pair">
<div class="step"><h3><span class="n">1</span> Add the Shortcut</h3>{code1}
<p>Point the Camera at this code and tap the link that appears. Then tap <b>Add Shortcut</b>.</p></div>
<div class="step"><h3><span class="n">2</span> Connect it to this PC</h3>{code2}
<p>Point the Camera at this code and tap the link. On the page that opens, tap <b>Connect</b>, then <b>Allow</b>.
The iPad shows <b>SR20 is set up</b>.</p></div>
</div></section>

<section><h2>Send a flight plan</h2><ol class="steps">
<li>In ForeFlight, open your route and tap the share button, then <b>Share FPL File</b>.</li>
<li>Tap <b>Send to SR20</b>. The iPad says <b>Flight plan received</b> and lists the waypoints.</li>
<li>On the MFD, the <b>1 PENDING FLIGHT PLAN</b> pop-up appears. Press <b>ENT</b> to preview it, then <b>ENT</b> and <b>OK</b> to activate it.</li>
</ol>
<p class="muted">Approaches, departures and arrivals don't come across; load them on the PROC page, as in the airplane.</p></section>

<section><h2>Status</h2><ul class="status">{''.join(rows)}</ul></section>

<section><h2>Windows firewall</h2><div id="fw" class="muted">Checking…</div>
<p><button data-act="fix-firewall" id="fix" hidden>Let the SR20 Companion receive on my home network</button></p></section>

<section><h2>If it doesn't work</h2><dl class="help">
<dt>"The request timed out"</dt>
<dd>The iPad can't reach this PC. Check that the SR20 Companion is running and that both are on the same Wi-Fi (guest
networks keep devices apart). If you changed routers, this PC may have a new address: scan code 2 again.</dd>
<dt>"This iPad isn't connected to this PC"</dt>
<dd>Scan code 2 again.</dd>
<dt>Send to SR20 isn't in the share list</dt>
<dd>Scroll the list of apps to the end, tap <b>More</b>, and turn on Send to SR20. Or scan code 1 again.</dd>
<dt>Nothing pops up on the MFD</dt>
<dd>The plan waits until the sim is running. If a window or menu is open on the MFD, the pop-up appears when you close it.</dd>
</dl></section>

<section class="quiet"><h2>More</h2>
<p><b>Lost or sold an iPad?</b> <button data-act="forget">Disconnect all iPads</button>
<span class="muted">Every iPad then needs code 2 scanned again before it can send.</span></p>
<details><summary>Send without the Shortcut (Safari)</summary>
<p class="muted">Point the Camera at this code and open the page. In ForeFlight, use Share FPL File &gt; Save to Files,
then pick that file on the page.</p>{safari}</details>
{others}
<p class="muted">Works with ForeFlight; not affiliated with ForeFlight or Garmin.</p></section>
</main><script src="/setup.js"></script></body></html>"""


def _file(name, ctype):
    try:
        with open(os.path.join(HERE, "web", name), "rb") as fh:
            return 200, fh.read(), ctype, CSP
    except OSError:
        return 404, b"not found", "text/plain", ()


CSP = [("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                   "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
       ("Referrer-Policy", "no-referrer")]


def get(path, q, ctx, pending):
    """-> (code, bytes, content type, extra headers)."""
    if path == "/setup":
        if ctx.pairing is None:
            return 503, b"starting", "text/plain", ()
        return 200, page(context(ctx, pending), ctx.nonce).encode("utf-8"), "text/html; charset=utf-8", CSP
    if path == "/setup.js":
        return _file("setup.js", "text/javascript; charset=utf-8")
    if path == "/setup.css":
        return _file("setup.css", "text/css; charset=utf-8")
    if path == "/setup/firewall":
        import efb_lan
        addrs = efb_lan.candidates(ctx.ipads.last if ctx.ipads else None)
        d = firewall_status(addrs[0] if addrs else "")
        return 200, json.dumps(d).encode("utf-8"), "application/json", ()
    return 404, b"not found", "text/plain", ()


def post(path, ctx):
    if path == "/setup/new-code":
        ctx.pairing.current_secret(new=True)
        return {"ok": True}
    if path == "/setup/new-key":
        try:
            ctx.pairing.send_key(new=True)
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "why": f"couldn't save: {e}"}
    if path == "/setup/forget":
        try:
            return {"ok": True, "forgot": ctx.pairing.forget_all()}
        except OSError as e:
            return {"ok": False, "why": f"couldn't save: {e}"}
    if path == "/setup/fix-firewall":
        return {"ok": True, "result": elevate_fix()}
    return {"ok": False, "why": "unknown action"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-firewall", action="store_true")
    ap.add_argument("--program")
    a = ap.parse_args()
    if a.fix_firewall:
        sys.exit(fix_firewall(a.program or program(), frozen=False))
