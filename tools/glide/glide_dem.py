"""Terrain for the glide range ring (docs/plans/glide_range_ring.md §3.1): Copernicus GLO-90 DEM tiles from AWS Open
Data, read with a minimal GeoTIFF reader (deflate + floating-point predictor; no GDAL/tifffile needed).

Heights are EGM2008 geoid (~MSL), metres. Lookups use a 3x3 max-pooled grid (conservative: a narrow ridge between
samples is not missed). Tiles not in the GLO-90 tile list are open water: elevation 0. Tiles listed but not (yet)
downloaded or unreadable return NaN: the caller treats that as unknown terrain and fails closed.

Copernicus DEM GLO-90: produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus Defence and
Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA; all rights reserved. Free to use with
this attribution.
"""
import math
import os
import queue
import struct
import threading
import time
import urllib.request
import zlib

import numpy as np

BASE = "https://copernicus-dem-90m.s3.amazonaws.com"
CACHE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SR20Glide", "tiles")
CACHE_CAP = 2 * 1024 ** 3          # bytes of .npz cache before LRU eviction


def tile_name(lat0, lon0):
    ns, ew = ("N" if lat0 >= 0 else "S"), ("E" if lon0 >= 0 else "W")
    return f"Copernicus_DSM_COG_30_{ns}{abs(lat0):02d}_00_{ew}{abs(lon0):03d}_00_DEM"


def read_geotiff(data):
    """-> (elev float32 [rows, cols] north-up, west lon, north lat, dlon, dlat) for a GLO-90 COG (first IFD)."""
    bo = "<" if data[:2] == b"II" else ">"
    if struct.unpack(bo + "H", data[2:4])[0] != 42:
        raise ValueError("not a classic TIFF")
    off = struct.unpack(bo + "I", data[4:8])[0]
    size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 11: 4, 12: 8, 16: 8}
    fmt = {3: "H", 4: "I", 11: "f", 12: "d", 16: "Q"}
    tags = {}
    n = struct.unpack(bo + "H", data[off:off + 2])[0]
    for i in range(n):
        e = data[off + 2 + 12 * i:off + 14 + 12 * i]
        tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
        nbytes = size.get(typ, 1) * cnt
        raw = e[8:8 + nbytes] if nbytes <= 4 else data[struct.unpack(bo + "I", e[8:12])[0]:][:nbytes]
        tags[tag] = struct.unpack(bo + fmt[typ] * cnt, raw) if typ in fmt else raw
    w, h = tags[256][0], tags[257][0]
    tw, th = tags[322][0], tags[323][0]
    if tags[258][0] != 32 or tags.get(339, (3,))[0] != 3:
        raise ValueError("expected float32 samples")
    comp, pred = tags[259][0], tags.get(317, (1,))[0]
    offs, cnts = tags[324], tags[325]
    across = (w + tw - 1) // tw
    img = np.zeros((((h + th - 1) // th) * th, across * tw), np.float32)
    for k, (o, c) in enumerate(zip(offs, cnts)):
        buf = data[o:o + c]
        if comp == 8:
            buf = zlib.decompress(buf)
        elif comp != 1:
            raise ValueError(f"compression {comp} not supported")
        b = np.frombuffer(buf, np.uint8)[:tw * th * 4].reshape(th, 4 * tw)
        if pred == 3:   # floating-point predictor: byte-wise horizontal differencing, then MSB..LSB byte planes
            b = np.cumsum(b, axis=1, dtype=np.uint8)
            tile = b.reshape(th, 4, tw).transpose(0, 2, 1).copy().view(">f4").reshape(th, tw)
        else:
            tile = b.copy().view(bo + "f4").reshape(th, tw)
        r, cc = divmod(k, across)
        img[r * th:(r + 1) * th, cc * tw:(cc + 1) * tw] = tile
    sx, sy = tags[33550][0], tags[33550][1]
    west, north = tags[33922][3], tags[33922][4]
    return img[:h, :w], west, north, sx, sy


def maxpool3(a):
    p = np.pad(a, 1, mode="edge")
    out = a.copy()
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            np.maximum(out, p[dy:dy + a.shape[0], dx:dx + a.shape[1]], out=out)
    return out


class Dem:
    def __init__(self, log=print, offline=False):
        self.log, self.offline = log, offline
        self.tiles = {}            # (lat0, lon0) -> dict(grid, raw, west, north, dx, dy) | "water" | "missing"
        self.listed = None
        self.q, self.pending, self.failed = queue.PriorityQueue(), set(), {}
        self.lock = threading.Lock()
        os.makedirs(CACHE, exist_ok=True)
        if not offline:
            threading.Thread(target=self._worker, daemon=True).start()

    # ---- tile list and download ---------------------------------------------------------------------------------
    def _tile_list(self):
        if self.listed is not None:
            return self.listed
        p = os.path.join(CACHE, "tileList.txt")
        if not os.path.exists(p) and not self.offline:
            self._fetch(BASE + "/tileList.txt", p)
        try:
            self.listed = set(open(p, encoding="ascii").read().split())
        except OSError:
            return None
        return self.listed

    def _fetch(self, url, path):
        part = path + ".part"
        with urllib.request.urlopen(url, timeout=10) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(part, path)

    def _npz(self, key):
        return os.path.join(CACHE, tile_name(*key) + ".npz")

    def _load_npz(self, key):
        p = self._npz(key)
        if not os.path.exists(p):
            return None
        try:
            z = np.load(p)
            os.utime(p)   # LRU stamp
            return dict(grid=z["grid"], raw=z["raw"], west=float(z["west"]), north=float(z["north"]),
                        dx=float(z["dx"]), dy=float(z["dy"]))
        except Exception as e:
            self.log(f"terrain cache {os.path.basename(p)} unreadable ({e}), refetching")
            try:
                os.remove(p)
            except OSError:
                pass
            return None

    def _download(self, key):
        name = tile_name(*key)
        tif = os.path.join(CACHE, name + ".tif")
        self._fetch(f"{BASE}/{name}/{name}.tif", tif)
        try:
            elev, west, north, dx, dy = read_geotiff(open(tif, "rb").read())
        finally:
            try:
                os.remove(tif)
            except OSError:
                pass
        if elev.shape[0] < 100 or not np.isfinite(elev).any():
            raise ValueError("tile decoded empty")
        elev = np.where(np.isfinite(elev), elev, 0.0)
        raw = np.ceil(elev).astype(np.int16)
        grid = maxpool3(raw)
        np.savez(self._npz(key) + ".part.npz", grid=grid, raw=raw, west=west, north=north, dx=dx, dy=dy)
        os.replace(self._npz(key) + ".part.npz", self._npz(key))
        self._evict()
        return dict(grid=grid, raw=raw, west=west, north=north, dx=dx, dy=dy)

    def _evict(self):
        files = [os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.endswith(".npz")]
        total = sum(os.path.getsize(f) for f in files)
        for f in sorted(files, key=os.path.getmtime):
            if total <= CACHE_CAP:
                break
            total -= os.path.getsize(f)
            os.remove(f)

    def _worker(self):
        while True:
            _, key = self.q.get()
            try:
                t = self._download(key)
                with self.lock:
                    self.tiles[key] = t
                self.log(f"terrain tile {tile_name(*key)} ready")
            except Exception as e:
                n = self.failed.get(key, 0) + 1
                self.failed[key] = n
                self.log(f"terrain tile {tile_name(*key)} failed ({e.__class__.__name__}: {e}), try {n}/3")
                if n < 3:
                    time.sleep(2 ** n)
                    self.q.put((99, key))
                    continue
            finally:
                self.pending.discard(key)

    # ---- public ---------------------------------------------------------------------------------------------------
    def tile(self, key):
        """Resolve a tile without blocking: dict, 'water', or None (not available yet)."""
        t = self.tiles.get(key)
        if t is not None:
            return t
        listed = self._tile_list()
        if listed is None:
            return None
        if tile_name(*key) not in listed:
            self.tiles[key] = "water"
            return "water"
        t = self._load_npz(key)
        if t is not None:
            with self.lock:
                self.tiles[key] = t
            return t
        return None

    def prefetch(self, lat, lon, radius_nm):
        """Queue every tile within radius_nm of (lat, lon), nearest first."""
        dlat = radius_nm / 60.0
        dlon = dlat / max(math.cos(math.radians(lat)), 0.1)
        for la in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
            for lo in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
                lo_w = ((lo + 180) % 360) - 180
                key = (la, lo_w)
                if abs(la) >= 80 or self.tile(key) is not None or key in self.pending or self.failed.get(key, 0) >= 3:
                    continue
                if self.offline:
                    continue
                self.pending.add(key)
                self.q.put((abs(la + 0.5 - lat) + abs(lo + 0.5 - lon), key))

    def lookup(self, lat, lon, pooled=True):
        """Elevation (m) at arrays of lat/lon: NaN where terrain is unknown (tile not available)."""
        lat = np.asarray(lat, float)
        lon = ((np.asarray(lon, float) + 180.0) % 360.0) - 180.0
        out = np.full(lat.shape, np.nan)
        la0, lo0 = np.floor(lat).astype(int), np.floor(lon).astype(int)
        for key in set(zip(la0.ravel().tolist(), lo0.ravel().tolist())):
            m = (la0 == key[0]) & (lo0 == key[1])
            t = self.tile(key)
            if t == "water":
                out[m] = 0.0
            elif isinstance(t, dict):
                g = t["grid"] if pooled else t["raw"]
                r = np.clip(((t["north"] - lat[m]) / t["dy"]).astype(int), 0, g.shape[0] - 1)
                c = np.clip(((lon[m] - t["west"]) / t["dx"]).astype(int), 0, g.shape[1] - 1)
                out[m] = g[r, c]
        return out
