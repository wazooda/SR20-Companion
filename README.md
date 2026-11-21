# SR20 Companion

The background helper for the **SR20 Realism Mod** (Cirrus SR20 G6 for Microsoft Flight Simulator 2024, on
flightsim.to). It runs beside the sim and provides:

- TAWS-B forward-looking terrain and obstacle alerts (`tools/taws`)
- the wind-corrected glide range ring over real terrain (`tools/glide`)
- SafeTaxi / SurfaceWatch airport maps for any airport (`tools/surfacewatch`)
- the iPad / ForeFlight flight plan link and moving map (`tools/efb`)
- RealSimGear Cirrus yoke force feel (`tools/yoke`)

`tools/companion/sr20_companion.py` starts one child process per helper and shows a tray icon.

User manual of the mod, including the Companion's tray menu and troubleshooting:
https://wazooda.github.io/SR20-Realism-Mod/

## Build

Windows, Python 3.13, and the MSFS 2024 SDK (for its `SimConnect.dll`):

    python -I tools\companion\build.py

This makes a venv with the pinned packages and runs PyInstaller; the program is in
`tools/companion/dist/SR20Companion`. Set `SR20_SIMCONNECT_DLL` if the SDK is not at `C:\MSFS 2024 SDK`.

Tests: `python -I tools\companion\test_tray.py`, `python -I tools\efb\test_fpl.py`.

## Library sources

The exact package versions are pinned in `tools/companion/build.py` (`PACKAGES`). The source archives of those
versions (every package in the build environment, listed with hashes in SOURCES.txt) are attached to each GitHub release
of this repository. The Python runtime is CPython 3.13 (source: https://www.python.org/downloads/source/).
`SimConnect.dll` comes from the Microsoft Flight Simulator 2024 SDK; it is Microsoft's and not part of this source.

## Network use

Local services on 127.0.0.1 ports 47820-47822; for the iPad link only, TCP 47823 on the home network and UDP 63093
(ForeFlight discovery). It downloads Copernicus DEM terrain tiles (AWS open data) and OpenStreetMap taxiway names.

## Licence

GNU Affero General Public License v3.0 (see `LICENSE`), because it uses the AGPL-licensed
[Python-SimConnect](https://github.com/odwdinc/Python-SimConnect). Data: Copernicus DEM (c) DLR e.V. / Airbus DS,
provided under COPERNICUS by the EU and ESA; FAA Digital Obstacle File (public domain); taxiway names
(c) OpenStreetMap contributors (ODbL). Not affiliated with Cirrus, Garmin, Microsoft or Asobo.

This snapshot: SR20 Realism Mod 1.0.0 (4588c7f).
