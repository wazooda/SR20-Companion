"""Obstacle lookup for the FLTA server (docs/plans/obstacles_o4.md §3.1, §6 C5): the FAA DOF snapshot in
tools/obstacles/cache (obstacles.npz + obstacles_meta.json, built by build_obstacles.py from the same snapshot as the
package tiles), binned in 0.05 degree cells for box queries.

Health (plan §3.1, parent plan R6): db 1 ok, 2 ok but the package tiles carry another snapshot (hash mismatch: alerts
still run from this cache, it is the alerting source), 0 unavailable (missing / unreadable / schema): no obstacle
alerts, terrain unaffected. A place outside every tile simply has no obstacles.
"""
import json
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
# the installed aircraft when the frozen Companion runs (SR20_PACKAGE), else the repo's package folder
PACKAGE_INDEX = os.path.join(os.environ.get("SR20_PACKAGE") or os.path.join(os.path.dirname(os.path.dirname(HERE)), "package",
                                                                           "paulw-aircraft-sr20"),
                             "html_ui", "SR20Obstacles", "index.json")
SCHEMA = 1
BIN_DEG = 0.05
FT_PER_NM = 6076.12
MAX_HBUF_NM = 6076.0 / FT_PER_NM     # the largest horizontal uncertainty in the data (accuracy code 8, 1 NM): the query margin (C5)
F_LIT, F_TURBINE = 1, 2


def symbol(flags, agl):
    """Map symbol, as Sr20ObstacleRules.symbol in sr20_obstacles.js: 0 / 1 tower < 1000 ft AGL unlit / lit, 2 / 3 tall, 4 / 5 turbine."""
    lit = 1 if flags & F_LIT else 0
    if flags & F_TURBINE:
        return 4 + lit
    return (2 if agl >= 1000 else 0) + lit


class ObstacleIndex:
    def __init__(self, cache=CACHE, package_index=PACKAGE_INDEX, log=print):
        self.db, self.why, self.currency, self.hash, self.n = 0, "not loaded", None, None, 0
        self.bins = {}
        try:
            with open(os.path.join(cache, "obstacles_meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("schema") != SCHEMA:
                raise ValueError(f"schema {meta.get('schema')}")
            z = np.load(os.path.join(cache, "obstacles.npz"))
            self.lat = z["lat"].astype(np.float64) / 1e5
            self.lon = z["lon"].astype(np.float64) / 1e5
            self.amsl = z["amsl"].astype(np.float64)
            self.agl = z["agl"].astype(np.float64)
            self.top = self.amsl + z["vacc"].astype(np.float64)          # vertical accuracy added once (Q5)
            self.hbuf_nm = z["hacc"].astype(np.float64) / FT_PER_NM
            self.flags = z["flags"].astype(np.int64)
            self.id = z["id"]
            self.n = len(self.lat)
            if not self.n or len(self.id) != self.n:
                raise ValueError("empty or ragged")
            self.currency, self.hash = meta.get("currency"), meta.get("hash")
            keys = np.floor(self.lat / BIN_DEG).astype(np.int64) * 100000 + np.floor(self.lon / BIN_DEG).astype(np.int64)
            order = np.argsort(keys, kind="stable")
            ks = keys[order]
            starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
            ends = np.r_[starts[1:], len(ks)]
            for s, e in zip(starts, ends):
                self.bins[int(ks[s])] = order[s:e]
            self.db, self.why = 1, "ok"
            try:
                with open(package_index, encoding="utf-8") as f:
                    pk = json.load(f)
                if pk.get("hash") != self.hash:
                    self.db, self.why = 2, f"package tiles {pk.get('hash')} vs server {self.hash}"
            except (OSError, ValueError) as e:
                self.db, self.why = 2, f"package index unreadable ({e})"
            log(f"obstacles: {self.n} records, currency {self.currency}, hash {self.hash}, db {self.db} ({self.why})")
        except Exception as e:  # noqa: BLE001
            self.db, self.why = 0, f"{e.__class__.__name__}: {e}"
            log(f"obstacles unavailable: {self.why}")

    @property
    def ok(self):
        return self.db > 0

    def query_box(self, lat0, lat1, lon0, lon1):
        """Records inside a lat / lon box: dict of arrays lat, lon, top (ft, + vertical accuracy), amsl, agl, hbuf_nm, id, sym."""
        if not self.ok:
            return None
        b0, b1 = math.floor(lat0 / BIN_DEG), math.floor(lat1 / BIN_DEG)
        c0, c1 = math.floor(lon0 / BIN_DEG), math.floor(lon1 / BIN_DEG)
        parts = [self.bins[k] for b in range(b0, b1 + 1) for c in range(c0, c1 + 1) if (k := b * 100000 + c) in self.bins]
        if not parts:
            return empty()
        idx = np.concatenate(parts)
        idx = idx[(self.lat[idx] >= lat0) & (self.lat[idx] <= lat1) & (self.lon[idx] >= lon0) & (self.lon[idx] <= lon1)]
        return {"lat": self.lat[idx], "lon": self.lon[idx], "top": self.top[idx], "amsl": self.amsl[idx], "agl": self.agl[idx],
                "hbuf_nm": self.hbuf_nm[idx], "id": self.id[idx], "sym": np.array([symbol(int(f), a) for f, a in zip(self.flags[idx], self.agl[idx])], np.int64)}


def empty():
    z = np.zeros(0)
    return {"lat": z, "lon": z, "top": z, "amsl": z, "agl": z, "hbuf_nm": z, "id": np.zeros(0, "<U9"), "sym": np.zeros(0, np.int64)}


def from_list(obs):
    """Test helper: a query function over a list of dicts {lat, lon, top (ft incl. vacc), hbuf_ft?, id?, agl?, sym?}."""
    lat = np.array([o["lat"] for o in obs], float)
    lon = np.array([o["lon"] for o in obs], float)
    top = np.array([o["top"] for o in obs], float)
    hb = np.array([o.get("hbuf_ft", 50.0) for o in obs], float) / FT_PER_NM
    ids = np.array([o.get("id", f"T-{i:06d}") for i, o in enumerate(obs)], "<U9")
    agl = np.array([o.get("agl", 300.0) for o in obs], float)
    sym = np.array([o.get("sym", 0) for o in obs], np.int64)

    def q(lat0, lat1, lon0, lon1):
        m = (lat >= lat0) & (lat <= lat1) & (lon >= lon0) & (lon <= lon1)
        return {"lat": lat[m], "lon": lon[m], "top": top[m], "amsl": top[m], "agl": agl[m], "hbuf_nm": hb[m], "id": ids[m], "sym": sym[m]}
    return q
