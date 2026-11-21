"""Build the frozen SR20 Companion (dist/SR20Companion/SR20Companion.exe, a folder build: the Companion starts one
child process per helper, and a one-file build would unpack itself again for every child).

    python -I tools\\companion\\build.py          venv in tools/companion/.build-venv (pinned packages), then PyInstaller

The folder carries its own Python, numpy, pyserial, the SimConnect package and the MSFS 2024 SDK's SimConnect.dll
(the pip package's DLL lacks the facility-data and *_EX1 calls the helpers use). Users install nothing else.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
VENV = os.environ.get("SR20_BUILD_VENV") or os.path.join(HERE, ".build-venv")   # release builds reuse the repo venv
PY = os.path.join(VENV, "Scripts", "python.exe")
DIST = os.path.join(HERE, "dist")
SDK_DLL = os.environ.get("SR20_SIMCONNECT_DLL") or r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll"
PACKAGES = ["numpy==2.5.4", "pyserial==3.5", "SimConnect==0.4.26", "segno==1.6.1", "pystray==0.19.5", "pillow==11.3.0", "pyinstaller==6.21.0"]
# helper modules imported by name inside the children (PyInstaller follows the static imports from these)
HIDDEN = ["glide_ring", "glide_calc", "glide_dem", "airport_server", "add_airport", "osm_names", "yoke_helper", "flta_server", "flta_core", "obstacle_index", "efb_server", "efb_lan", "efb_setup", "segno", "tray", "pystray._win32", "PIL.ImageDraw", "fpl",
          "probe_electrical", "sim_batch", "make_layout", "serial.tools.list_ports"]


def main():
    if not os.path.exists(SDK_DLL):
        sys.exit(f"SimConnect.dll not found: {SDK_DLL} (install the MSFS 2024 SDK or set SR20_SIMCONNECT_DLL)")
    if not os.path.exists(PY):
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    subprocess.run([PY, "-m", "pip", "install", "-q", "--disable-pip-version-check"] + PACKAGES, check=True)
    work = os.path.join(HERE, "build")
    paths = [os.path.join(TOOLS, d) for d in ("glide", "surfacewatch", "yoke", "taws", "efb", "obstacles")] + [TOOLS]
    cmd = [PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--name", "SR20Companion",
           "--distpath", DIST, "--workpath", work, "--specpath", work,
           "--collect-all", "SimConnect", "--add-binary", f"{SDK_DLL};.", "--add-data", f"{os.path.join(TOOLS, 'yoke', 'yoke_feel.json')};.",
           "--add-data", f"{os.path.join(TOOLS, 'efb', 'web')};web",
           "--add-data", f"{os.path.join(TOOLS, 'obstacles', 'cache', 'obstacles.npz')};cache", "--add-data", f"{os.path.join(TOOLS, 'obstacles', 'cache', 'obstacles_meta.json')};cache"]
    for p in paths:
        cmd += ["--paths", p]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    cmd.append(os.path.join(HERE, "sr20_companion.py"))
    subprocess.run(cmd, check=True)
    shutil.rmtree(work, ignore_errors=True)
    out = os.path.join(DIST, "SR20Companion")
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(out) for f in fs)
    print(f"built {out} ({size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
