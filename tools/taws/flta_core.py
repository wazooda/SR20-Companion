"""FLTA (forward-looking terrain avoidance) for the SR20's TAWS-B: the pure computation (docs/plans/taws_b.md
draft 3 §3.4), no SimConnect, no files. flta_server.py feeds it the aircraft state and the PFD's runway table
and writes its result to L-vars; test_flta.py drives it on synthetic terrain; replay_flight.py on a recorded track.

Pilot's Guide 190-02183-03 §6.6 (PDF pp.364-378): the required terrain clearance (Figure 6-65) is a function of the
distance to the nearest runway, lower when descending; the projected flight path grows with ground speed, narrows
near the runway, follows a turn; FLTA is auto-disabled below 200 ft within 0.5 NM of the runway or between its ends.
The look-ahead times (60 s caution / 30 s warning, 1.5 / 0.75 NM minimum), the corridor widths and the sampling are
ours (review 2, Q1); the geometry near the runway is the fix for review 2 C3: clearance distance to the nearest runway
at any bearing, clearance evaluated per projected point, the projection clipped 0.5 NM before an aligned runway, the
runway funnel sampled as the runway elevation, the corridor narrowed to 0.1 NM inside 2 NM, a climb credited at
the current rate up to 1,000 ft (review 2 asked for 15 s: that still alerted on every normal departure toward a
2-degree slope; the pilot's own climb is as much their path as a descent is). Terrain = DEM 3x3 max-pooled + a 20 m allowance (the 99th percentile of sim-minus-DEM in glide_terrain.csv).

Units: positions in degrees, altitudes in feet, speeds in knots and fpm, distances in NM unless named _m.
"""
import math

import numpy as np

NM_M = 1852.0
FT_M = 0.3048
T_CAUTION_S, T_WARNING_S = 60.0, 30.0
MIN_CAUTION_NM, MIN_WARNING_NM = 1.5, 0.75
STEP_M = 90.0                       # along-track station spacing (one GLO-90 cell)
LINE_SPACING_M = 180.0              # lateral sample lines at most two cells apart (review 2 I7)
HALF_WIDTH_NEAR_NM, HALF_WIDTH_FAR_NM, HALF_WIDTH_RUNWAY_NM = 0.2, 0.5, 0.1
NARROW_WITHIN_NM = 2.0              # of an aligned runway
ALLOWANCE_M = 20.0                  # added to every DEM sample outside a runway funnel
FUNNEL_LEN_NM, FUNNEL_HALF_NM = 2.0, 0.15
CLIP_BEFORE_THRESHOLD_NM = 0.5
ALIGN_DEG, ALIGN_XTK_NM, ALIGN_MAX_NM = 15.0, 0.3, 15.0
AUTO_DISABLE_FT, AUTO_DISABLE_NM = 200.0, 0.5
DESCENT_VS_FPM, DESCENT_HOLD_S, DESCENT_EXIT_VS_FPM, DESCENT_EXIT_HOLD_S = -300.0, 3.0, -100.0, 3.0
VS_FLOOR_FT = 2000.0                # descending projection floored at the current altitude minus this
# Near a runway a sink is not held for a full minute: lined up with a runway within 6 NM, or within 2 NM of any runway,
# the current descent is projected for 20 s and then level. 2026-11-02 at KSNA (the author: "a little too fidgety"): six
# cautions / warnings over flat ground on the RNAV 20R final (1,150-2,560 fpm while recovering from high) and one at
# 0.8 NM past the runway after a go-around (460 fpm sink). Level flight toward terrain keeps the full look-ahead.
NEAR_RWY_DESCENT_S = 20.0
# Sidestep on short final: within SIDESTEP_NM of a runway threshold ahead, course within SIDESTEP_DEG and cross-track
# within ALIGN_XTK_NM of its extended centreline, bank at most SIDESTEP_MAX_BANK_DEG. The caution look-ahead then runs
# straight along the track; the current turn is still projected over the whole warning distance (d_w: T_WARNING_S,
# at least MIN_WARNING_NM) and the worse of the two results stands, with terrain data required on both (Codex round 2).
# The terrain clipping / funnel also take this runway when no tighter alignment exists (Fable round 2 N2: a 16-30 deg
# sidestep 0.55-0.8 NM out got "Terrain ahead" over flat ground from the 20 m DEM allowance). 2026-11-15 at KSNA: 5-10 deg banks sidestepping from taxiway C to 20L and on to 20R
# (220-480 ft, 0.75-1.7 NM out) swung the 60 s corridor off the runway into the office towers beside the airport,
# "Obstacle ahead" twice on short final (both conflicts 45-55 s ahead); wings level, neither alerts. Reviews (Codex,
# Fable 2026-11-15): a steep turn (> 15 deg bank) keeps the full turn projection, in either direction; 30 deg so a
# sharper sidestep (16+ deg off, seen in the same flight) is covered; a base-to-final turn (60+ deg off) is not a sidestep.
SIDESTEP_NM, SIDESTEP_DEG, SIDESTEP_MAX_BANK_DEG = 3.0, 30.0, 15.0
SIDESTEP_LEVEL_FT = 50.0    # sidestep clipping: raw ground (no DEM allowance) up to this above the runway is airfield
SIDESTEP_FIELD_NM = 1.0     # ... and only within this of the runway (centreline segment)
NEAR_RWY_ALIGNED_NM, NEAR_RWY_ANY_NM = 6.0, 2.0
CLIMB_CREDIT_MAX_FT = 1000.0         # a climb is credited at the current rate, never beyond this above the present altitude
LIFTOFF_OFF_S = 10.0                # FLTA off on the ground and for this long after liftoff
ASSERT_TICKS, CLEAR_S, KIND_HOLD_S = 2, 3.0, 3.0
# Chatter (2026-11-17 KSNA photo flight, docs/plans/2026-11-18-flta-chatter.md): hand-flown at 450-1300 ft AGL the
# instantaneous VELOCITY WORLD Y swung -1300 / +900 fpm within 4 s and the alert flipped 0 / 1 / 2 every 3-10 s
# (38 changes, 11 separate alerts in 5 min). Garmin does not describe its filtering or hysteresis (PG §6.6 p.373,
# AXIS 6-41..6-44); these are ours. The projection uses the shallower of the instantaneous and a VS_TAU_S smoothed
# vertical speed (a momentary sink is not projected for a minute; a climb pulse is not credited); a new caution needs
# CAUTION_ASSERT_S of continuous raw alert (a warning keeps the 1 s assertion); an alert clears after ALERT_CLEAR_S of
# continuous clear results and a warning steps down to a caution after WARN_DOWN_S.
VS_TAU_S = 4.0
INST_WARN_S = 2.0       # a warning from the instantaneous VS projection stands once it has held this long (Codex r1 P1)
GAP_S = 5.0             # a sample gap longer than this, or time running backwards, restarts every filter timer (Codex r1 P3)
KIND_BACK_S = 8.0       # an imminent-impact wording (ITI / IOI) steps back to a reduced-clearance one (RTC / ROC) only after this
CAUTION_ASSERT_S, ALERT_CLEAR_S, WARN_DOWN_S = 2.0, 6.0, 5.0
TURN_BANK_DEG = 5.0
MAX_OBS_HBUF_NM = 1.0               # the largest DOF horizontal uncertainty (code 8): the obstacle query margin (plan O4 C5)
MAX_CELLS = 150                     # potential impact cells per result; more are merged along the track
G_MPS2 = 9.80665


def rc_ft(d_nm, descending):
    """Required terrain clearance, Figure 6-65 (vector data read off the PDF by review 2): two lines, 25 ft/NM."""
    if d_nm is None or d_nm == float("inf") or d_nm > 23.0:
        return 500.0 if descending else 700.0
    if d_nm <= 0.5:
        return 0.0
    if descending:
        if d_nm <= 2.0:
            return 120.0 * (d_nm - 0.5) / 1.5
        return min(500.0, 120.0 + 24.9 * (d_nm - 2.0))
    if d_nm <= 2.0:
        return 175.0 * (d_nm - 0.5) / 1.5
    return min(700.0, 175.0 + 25.0 * (d_nm - 2.0))


def diff_angle(a, b):
    """Smallest difference between two headings, degrees, 0..180."""
    d = (a - b + 180.0) % 360.0 - 180.0
    return abs(d)


def local_xy_nm(lat0, lon0, lat, lon):
    """Flat-earth east / north offsets in NM of (lat, lon) from (lat0, lon0)."""
    k = math.cos(math.radians(lat0))
    return (np.asarray(lon) - lon0) * 60.0 * k, (np.asarray(lat) - lat0) * 60.0


def offset(lat0, lon0, east_nm, north_nm):
    k = max(math.cos(math.radians(lat0)), 0.05)
    return lat0 + north_nm / 60.0, lon0 + east_nm / (60.0 * k)


class DescentState:
    """'Descending' for the clearance line: VS <= -300 fpm for 3 s; exit when > -100 fpm for 3 s (review 2 Q5)."""

    def __init__(self):
        self.descending = False
        self.since = None

    def update(self, vs_fpm, t):
        if self.descending:
            if vs_fpm > DESCENT_EXIT_VS_FPM:
                self.since = t if self.since is None else self.since
                if t - self.since >= DESCENT_EXIT_HOLD_S:
                    self.descending, self.since = False, None
            else:
                self.since = None
        else:
            if vs_fpm <= DESCENT_VS_FPM:
                self.since = t if self.since is None else self.since
                if t - self.since >= DESCENT_HOLD_S:
                    self.descending, self.since = True, None
            else:
                self.since = None
        return self.descending


def runway_relation(state, rw):
    """Geometry of one runway relative to the aircraft: distance to the threshold (NM), along-track distance of the
    threshold ahead of the aircraft (NM, negative behind), cross-track offset from the extended centreline (NM), the
    course difference (deg) and whether the aircraft is between the runway ends."""
    e, n = local_xy_nm(state["lat"], state["lon"], rw["lat"], rw["lon"])
    e, n = float(e), float(n)
    dist = math.hypot(e, n)
    tr = math.radians(state["track"])
    along = e * math.sin(tr) + n * math.cos(tr)             # threshold ahead along the track
    crs = math.radians(rw["crs"])
    # position of the aircraft in the runway frame: along the runway course from the threshold, and across it
    ax = -(e * math.sin(crs) + n * math.cos(crs))           # NM along the runway course, from the threshold forward
    xt = abs(-e * math.cos(crs) + n * math.sin(crs))        # NM off the extended centreline
    length_nm = rw.get("len_m", 0.0) / NM_M
    between = (0.0 <= ax <= length_nm) and xt <= ALIGN_XTK_NM
    return {"dist": dist, "along": along, "xtk": xt, "dcrs": diff_angle(rw["crs"], state["track"]), "between": between, "ax": ax}


def aligned_runway(state, runways):
    """The runway the aircraft is lined up with ahead (course within 15 deg, within 0.3 NM of the extended centreline,
    threshold ahead within 15 NM), or None. Used only for clipping, the funnel and the auto-disable."""
    best, best_d = None, None
    for rw in runways:
        r = runway_relation(state, rw)
        if r["dcrs"] <= ALIGN_DEG and r["xtk"] <= ALIGN_XTK_NM and 0.0 < r["along"] <= ALIGN_MAX_NM and r["ax"] < 0.0:
            if best is None or r["dist"] < best_d:
                best, best_d = (rw, r), r["dist"]
    return best


def near_runway(state, runways, al=None):
    """Lined up with a runway whose threshold is within NEAR_RWY_ALIGNED_NM, or within NEAR_RWY_ANY_NM of any runway
    threshold: the descent projection is shortened (NEAR_RWY_DESCENT_S)."""
    if al is not None and al[1]["dist"] <= NEAR_RWY_ALIGNED_NM:
        return True
    return any(runway_relation(state, rw)["dist"] <= NEAR_RWY_ANY_NM for rw in runways)


def auto_disabled(state, runways):
    """PG p.353: below 200 ft above the runway within 0.5 NM of it, or between the runway ends."""
    for rw in runways:
        r = runway_relation(state, rw)
        h = state["alt"] - rw["elev"]
        if h < AUTO_DISABLE_FT and (r["dist"] <= AUTO_DISABLE_NM or r["between"]):
            return True
    return False


def sidestep_runway(state, runways, any_bank=False):
    """(runway, relation) of the runway the aircraft is sidestepping onto (see SIDESTEP_*), or None. any_bank: ignore
    the bank limit (the terrain clipping / funnel)."""
    if not any_bank and abs(float(state.get("bank", 0.0))) > SIDESTEP_MAX_BANK_DEG:
        return None
    best = None
    for rw in runways:
        r = runway_relation(state, rw)
        if r["dcrs"] <= SIDESTEP_DEG and r["xtk"] <= ALIGN_XTK_NM and 0.0 < r["along"] <= SIDESTEP_NM and r["ax"] < 0.0:
            if best is None or r["dist"] < best[1]["dist"]:
                best = (rw, r)
    return best


def _rank(out):
    return (-out["level"], out["t"] if out["level"] else float("inf"))


def project(state, runways, dem_lookup_m, descending, turn=True, obstacles=None, horizon_s=None):
    """One FLTA evaluation. `dem_lookup_m(lats, lons)` returns pooled DEM metres (NaN = unknown); `obstacles(lat0, lat1,
    lon0, lon1)` returns the obstacles in a box (tools/obstacles/obstacle_index.py), None = no obstacle data.
    Returns a dict: level 0 / 1 caution / 2 warning, kind 0 RTC / 1 ITI / 2 ROC / 3 IOI (the winner of the terrain and
    obstacle candidates, arbitrate()), t (s to the first conflict), s_nm, dem_ok, min_clear (ft, terrain + RC vs
    projected, negative = conflict), stations, terrain / obstacle (the two candidates or None), obstacles (the
    conflicting obstacles of the obstacle candidate), debug fields."""
    if turn and horizon_s is None and abs(float(state.get("bank", 0.0))) > TURN_BANK_DEG and sidestep_runway(state, runways) is not None:
        straight = project(state, runways, dem_lookup_m, descending, turn=False, obstacles=obstacles)
        turning = project(state, runways, dem_lookup_m, descending, turn=True, obstacles=obstacles, horizon_s=T_WARNING_S)
        # the turning corridor stands only for a warning (a conflict inside d_w); a caution from its last station is
        # the long-range turn this rule drops
        win = turning if turning["level"] == 2 and _rank(turning) < _rank(straight) else straight
        win["sidestep"] = True
        win["dem_ok"] = bool(straight["dem_ok"] and turning["dem_ok"])   # both corridors must have terrain data
        return win
    gs = max(float(state["gs"]), 1.0)
    gs_mps = gs * NM_M / 3600.0
    d_c = max(gs * T_CAUTION_S / 3600.0, MIN_CAUTION_NM)
    d_w = max(gs * T_WARNING_S / 3600.0, MIN_WARNING_NM)
    if horizon_s is None:
        n_st = int(d_c * NM_M / STEP_M)
    else:   # the whole warning distance, endpoint included (it is at least MIN_WARNING_NM, longer than horizon_s at low speed)
        n_st = min(int(d_c * NM_M / STEP_M), int(math.ceil(max(d_w, gs * horizon_s / 3600.0) * NM_M / STEP_M)) + 1)
    s_m = np.arange(1, n_st + 1) * STEP_M                      # station distances along the path
    t_s = s_m / gs_mps
    # projected altitude
    vs = float(state["vs"])
    alt = float(state["alt"])
    al_true = aligned_runway(state, runways)
    al = al_true or sidestep_runway(state, runways, any_bank=True)   # clipping / funnel / narrowing
    if descending and vs < 0:
        t_desc = t_s
        if near_runway(state, runways, al):
            t_desc = np.minimum(t_s, NEAR_RWY_DESCENT_S)
        proj = np.maximum(alt + vs / 60.0 * t_desc, alt - VS_FLOOR_FT)
    elif vs > 0:
        proj = np.minimum(alt + vs / 60.0 * t_s, alt + CLIMB_CREDIT_MAX_FT)   # the current climb is the pilot's own path, as a descent is
    else:
        proj = np.full_like(t_s, alt)
    # centreline: straight, or along the current turn (g tan(bank) / V)
    tr = math.radians(float(state["track"]))
    bank = float(state.get("bank", 0.0))
    if turn and abs(bank) > TURN_BANK_DEG:
        omega = G_MPS2 * math.tan(math.radians(abs(bank))) / gs_mps * (1 if bank > 0 else -1)   # rad/s, right turn positive
        ang = tr + omega * t_s
        r_m = gs_mps / abs(omega)
        east_m = r_m * (np.cos(tr) - np.cos(ang)) * (1 if omega > 0 else -1)
        north_m = r_m * (np.sin(ang) - np.sin(tr)) * (1 if omega > 0 else -1)
        head = ang
    else:
        east_m, north_m = s_m * math.sin(tr), s_m * math.cos(tr)
        head = np.full_like(t_s, tr)
    east_nm, north_nm = east_m / NM_M, north_m / NM_M
    # aligned runway: clipping, funnel, narrow corridor
    keep = np.ones(n_st, bool)
    funnel = np.zeros(n_st, bool)
    side_clip = None
    side_funnel = False
    half_w = HALF_WIDTH_NEAR_NM + (HALF_WIDTH_FAR_NM - HALF_WIDTH_NEAR_NM) * (t_s / T_CAUTION_S)
    if al is not None:
        rw, rel = al
        # funnel: 2 NM before the threshold, +-0.15 NM, along the runway course
        thr_e, thr_n = local_xy_nm(state["lat"], state["lon"], rw["lat"], rw["lon"])
        crs = math.radians(rw["crs"])
        # station position along the runway course from the threshold: negative before it (the same sense as
        # runway_relation's ax). It had a leading minus until 2026-11-08, so the funnel never matched a retained station
        # and the 2 NM before the threshold was never flattened (Codex review O4 C10)
        ax = (east_nm - float(thr_e)) * math.sin(crs) + (north_nm - float(thr_n)) * math.cos(crs)
        xt = np.abs(-(east_nm - float(thr_e)) * math.cos(crs) + (north_nm - float(thr_n)) * math.sin(crs))
        funnel = (ax < 0.0) & (ax > -FUNNEL_LEN_NM) & (xt <= FUNNEL_HALF_NM)
        dist_thr = np.hypot(east_nm - float(thr_e), north_nm - float(thr_n))
        if al_true is not None:
            clip_nm = rel["along"] - CLIP_BEFORE_THRESHOLD_NM
            keep &= (s_m / NM_M) <= max(clip_nm, 0.0)
            half_w = np.where(dist_thr <= NARROW_WITHIN_NM, HALF_WIDTH_RUNWAY_NM, half_w)
        else:
            # sidestep only (15-30 deg off, or banked): the path need not end on the runway, so nothing is clipped by
            # distance; only stations on the airfield itself (within SIDESTEP_FIELD_NM of the runway, past the clip
            # line 0.5 NM before the threshold) whose raw ground is runway-level (SIDESTEP_LEVEL_FT, no DEM allowance)
            # are dropped, below once the terrain is known: a sharp cut on short final crosses the flat airfield beside
            # the runway (Fable r2 N2) and stays quiet; a hill or plateau beside the runway still alerts (Codex r3 P1 /
            # r4 1-2, Fable r3 1 / r4 N4). The funnel flattens only runway-level ground the same way (it erased a hill
            # 0.4 NM ahead, Codex r4 1); narrowing only for stations inside the runway's 0.3 NM band.
            side_funnel = True
            band = xt <= ALIGN_XTK_NM
            L = float(rw.get("len_m", 0.0)) / NM_M
            d_seg = np.where(ax < 0.0, np.hypot(ax, xt), np.where(ax > L, np.hypot(ax - L, xt), xt))
            side_clip = (ax >= -CLIP_BEFORE_THRESHOLD_NM) & (d_seg <= SIDESTEP_FIELD_NM)
            half_w = np.where(band & (dist_thr <= NARROW_WITHIN_NM), HALF_WIDTH_RUNWAY_NM, half_w)
    # required clearance per station from the nearest runway at any bearing (before the clipping return: obstacles
    # are checked on the whole path, plan O4 §6 C1)
    d_p = np.full(n_st, np.inf)
    d_0 = float("inf")
    for rw in runways:
        e, n = local_xy_nm(state["lat"], state["lon"], rw["lat"], rw["lon"])
        d_p = np.minimum(d_p, np.hypot(east_nm - float(e), north_nm - float(n)))
        d_0 = min(d_0, math.hypot(float(e), float(n)))
    rc = np.array([rc_ft(float(d), descending) for d in d_p])
    obs = None
    if obstacles is not None:
        obs = obstacle_candidate(state, obstacles, s_m, east_nm, north_nm, alt, proj, rc_ft(d_0, descending), rc, half_w, gs_mps, d_w)
    if not keep.any():
        return finish({"level": 0, "kind": 0, "t": 0.0, "s_nm": 0.0, "dem_ok": True, "min_clear": float("inf"), "stations": 0, "clipped": True, "d_nm": None, "rc": 0.0}, obs)
    # lateral sample lines
    max_w = float(half_w.max())
    n_lines = max(3, 2 * int(math.ceil(max_w * NM_M / LINE_SPACING_M)) + 1)
    frac = np.linspace(-1.0, 1.0, n_lines)
    perp_e, perp_n = np.cos(head), -np.sin(head)                 # right-hand perpendicular of the local heading
    lat_s = np.empty((n_st, n_lines))
    lon_s = np.empty((n_st, n_lines))
    for i in range(n_lines):
        e = east_nm + perp_e * half_w * frac[i]
        n = north_nm + perp_n * half_w * frac[i]
        la, lo = offset(state["lat"], state["lon"], e, n)
        lat_s[:, i], lon_s[:, i] = la, lo
    dem = np.asarray(dem_lookup_m(lat_s.ravel(), lon_s.ravel()), float).reshape(n_st, n_lines)
    terrain_m = np.nanmax(dem, axis=1) if n_lines else dem[:, 0]
    unknown = np.isnan(dem).any(axis=1) & keep
    dem_ok = not bool(unknown.any())
    terrain_ft = (terrain_m + ALLOWANCE_M) / FT_M
    if al is not None:
        if side_funnel:
            funnel = funnel & (terrain_m / FT_M <= al[0]["elev"] + SIDESTEP_LEVEL_FT)
        terrain_ft = np.where(funnel, al[0]["elev"], terrain_ft)
    if side_clip is not None:
        keep = keep & ~(side_clip & (terrain_m / FT_M <= al[0]["elev"] + SIDESTEP_LEVEL_FT))
    clear = proj - (terrain_ft + rc)
    clear = np.where(keep, clear, np.inf)
    clear = np.where(np.isnan(clear), np.inf, clear)
    rtc = clear < 0.0
    iti = (proj - terrain_ft) <= 0.0
    iti = np.where(keep, iti, False) & ~np.isnan(terrain_ft)
    out = {"level": 0, "kind": 0, "t": 0.0, "s_nm": 0.0, "dem_ok": dem_ok, "min_clear": float(clear.min()), "stations": int(keep.sum()),
           "clipped": al is not None, "d_nm": float(d_p[0]) if np.isfinite(d_p[0]) else None, "rc": float(rc[0]), "lines": n_lines}
    if not dem_ok:
        return finish(out, obs)
    idx = np.nonzero(rtc)[0]
    if len(idx) == 0:
        return finish(out, obs)
    k = int(idx[0])
    s_nm = float(s_m[k]) / NM_M
    out["s_nm"], out["t"] = s_nm, float(t_s[k])
    # ITI ("Terrain Ahead"): the aircraft is below the elevation of terrain anywhere in its projected path, not only at
    # the first conflict point. 2026-11-01 (the author, level at 2,720 ft into hills topping out near 2,990 ft): the first
    # point was 230 ft below the path, so the old rule (ITI only at or just after the first point) said RTC
    # ("Terrain, Terrain; Pull Up") all the way into the hill.
    out["kind"] = 1 if bool(iti.any()) else 0
    out["level"] = 2 if s_nm <= d_w else 1
    out["conflict_terrain_ft"], out["conflict_proj_ft"], out["conflict_rc_ft"] = float(terrain_ft[k]), float(proj[k]), float(rc[k])
    out["cells"], out["cell_along_m"] = impact_cells(dem, proj, rc, keep, funnel, al, lat_s, lon_s, s_m, head, half_w, n_lines, d_w)
    return finish(out, obs)


def finish(out, obs):
    """The terrain result in `out` becomes the terrain candidate; the winner of it and the obstacle candidate fills the
    level / kind / t / s_nm fields (plan O4 §3.2, §6 C6)."""
    ter = {"source": "terrain", "level": out["level"], "kind": out["kind"], "t": out["t"], "s_nm": out["s_nm"], "obstacles": []} if out["level"] > 0 else None
    out["terrain"], out["obstacle"] = ter, obs
    w = arbitrate(ter, obs)
    if w is not None:
        out["level"], out["kind"], out["t"], out["s_nm"] = w["level"], w["kind"], w["t"], w["s_nm"]
    out["obstacles"] = obs["obstacles"] if obs is not None else []
    return out


def arbitrate(*cands):
    """Higher level, then the earlier conflict, then terrain (a trainer policy; the SDK's own priorities put RTC above
    ITI / ROC / IOI, garminsdk.js:59625)."""
    c = [x for x in cands if x is not None and x["level"] > 0]
    if not c:
        return None
    return min(c, key=lambda x: (-x["level"], x["t"], 0 if x["source"] == "terrain" else 1))


OBS_MAX_LISTED = 20


def obstacle_candidate(state, obstacles, s_m, east_nm, north_nm, alt, proj, rc0, rc, half_w, gs_mps, d_w):
    """ROC / IOI (PG Table 6-15) from point obstacles (plan O4 §3.2, §6 C4 / C5 / C9): every obstacle is projected on the
    centreline segments (the aircraft, then the stations), with the along-track position limited to the path (nothing
    beyond the last station or before the aircraft) and the altitude, RC and half width interpolated at its foot; it is
    inside when its cross-track distance is within the half width plus its own horizontal uncertainty. No runway
    clipping, no funnel flattening, no DEM allowance (those are terrain-only, parent plan R1). ROC when the projected
    path is below top + RC, IOI when it is at or below the top; either is a conflict. None, or the candidate."""
    S = np.r_[0.0, s_m / NM_M]
    E = np.r_[0.0, east_nm]
    N = np.r_[0.0, north_nm]
    P = np.r_[alt, proj]
    R = np.r_[rc0, rc]
    H = np.r_[half_w[0], half_w]
    margin = float(H.max()) + MAX_OBS_HBUF_NM + 0.05
    lat0, lon0 = offset(state["lat"], state["lon"], float(E.min()) - margin, float(N.min()) - margin)
    lat1, lon1 = offset(state["lat"], state["lon"], float(E.max()) + margin, float(N.max()) + margin)
    q = obstacles(lat0, lat1, lon0, lon1)
    if q is None or len(q["lat"]) == 0:
        return None
    n_o = len(q["lat"])
    ox, oy = local_xy_nm(state["lat"], state["lon"], q["lat"], q["lon"])
    ox, oy = np.asarray(ox, float)[:, None], np.asarray(oy, float)[:, None]
    ax, ay, dx, dy = E[:-1][None, :], N[:-1][None, :], np.diff(E)[None, :], np.diff(N)[None, :]
    l2 = np.maximum(dx * dx + dy * dy, 1e-12)
    u = ((ox - ax) * dx + (oy - ay) * dy) / l2                          # (n_obs, n_seg)
    dist = np.where((u >= 0.0) & (u <= 1.0), np.hypot(ox - (ax + u * dx), oy - (ay + u * dy)), np.inf)
    rows = np.arange(n_o)
    i_seg = np.argmin(dist, axis=1)
    d_o = dist[rows, i_seg]
    uu = np.clip(u[rows, i_seg], 0.0, 1.0)

    def lerp(a):
        return a[i_seg] + (a[i_seg + 1] - a[i_seg]) * uu
    s_o, p_o, r_o, h_o = lerp(S), lerp(P), lerp(R), lerp(H)
    if len(E) > 2:     # interior vertices too: the outside of a turn falls between two segments' perpendiculars
        # only in the wedge outside a joint: beyond the end of the segment before and before the start of the one after
        # (a plain nearest-vertex test would extend the corridor past the last station)
        wedge = (u[:, :-1] > 1.0) & (u[:, 1:] < 0.0)
        vd = np.where(wedge, np.hypot(ox - E[None, 1:-1], oy - N[None, 1:-1]), np.inf)
        i_v = np.argmin(vd, axis=1)
        d_v = vd[rows, i_v]
        use_v = d_v < d_o
        k = i_v + 1
        s_o, p_o, r_o, h_o, d_o = (np.where(use_v, S[k], s_o), np.where(use_v, P[k], p_o), np.where(use_v, R[k], r_o),
                                   np.where(use_v, H[k], h_o), np.where(use_v, d_v, d_o))
    inside = np.isfinite(d_o) & (d_o <= h_o + q["hbuf_nm"])
    top = np.asarray(q["top"], float)
    roc = (p_o - (top + r_o)) < 0.0
    ioi = (p_o - top) <= 0.0
    hit = inside & (roc | ioi)
    if not hit.any():
        return None
    order = np.argsort(np.where(hit, s_o, np.inf), kind="stable")[: int(hit.sum())]
    s_first = float(s_o[order[0]])
    lst = [{"id": str(q["id"][j]), "lat": round(float(q["lat"][j]), 5), "lon": round(float(q["lon"][j]), 5), "top": int(round(float(q["amsl"][j]))),
            "agl": int(round(float(q["agl"][j]))), "sym": int(q["sym"][j]), "level": 2 if s_o[j] <= d_w else 1,
            "s_nm": round(float(s_o[j]), 3), "clear_ft": int(round(float(p_o[j] - top[j] - r_o[j])))} for j in order[:OBS_MAX_LISTED]]
    return {"source": "obstacle", "kind": 3 if bool((hit & ioi).any()) else 2, "level": 2 if s_first <= d_w else 1,
            "t": s_first * NM_M / gs_mps, "s_nm": s_first, "obstacles": lst}


def impact_cells(dem, proj, rc, keep, funnel, al, lat_s, lon_s, s_m, head, half_w, n_lines, d_w):
    """Potential impact areas (PG Table 6-12, Figure 2-47): every sample whose own terrain + RC is above the projected
    path, as [lat, lon, terrain ft (DEM, no allowance), level 1 / 2 (inside the warning distance), heading deg true,
    across-track width m, corner terrain ft front-left, front-right, back-right, back-left]. The corner heights are the
    highest of the samples around each corner, so neighbouring cells share them and the shading on the synthetic vision
    follows the terrain surface (flat slabs at one height were slivers from a shallow eye, 2026-11-01 in the sim).
    The cells then carry their four corner positions (lat, lon in the same order, items 10 to 17): one vertex grid at
    the boundaries between samples, shared by neighbouring cells, so the shading is one seamless surface (cells drawn
    from their centres with an overlap left seams and orange caution / warning overlaps, the author 2026-11-01).
    The along-track length is the station step times the merge factor (returned second). More than MAX_CELLS
    conflicting samples are merged along the track (k stations per cell) so the area stays whole."""
    terr_s = (dem + ALLOWANCE_M) / FT_M
    if al is not None:
        terr_s = np.where(funnel[:, None], al[0]["elev"], terr_s)
    with np.errstate(invalid="ignore"):
        conflict = (proj[:, None] - (terr_s + rc[:, None]) < 0.0) & keep[:, None] & ~np.isnan(terr_s)
    n = int(conflict.sum())
    if n == 0:
        return [], STEP_M
    k = max(1, int(math.ceil(n / MAX_CELLS)))
    cells = []
    n_st = conflict.shape[0]
    v_lat, v_lon = vertex_grid(lat_s), vertex_grid(lon_s)        # (n_st + 1, n_lines + 1): boundary b sits before station b
    for b in range(0, n_st, k):
        blk = conflict[b:b + k]
        if not blk.any():
            continue
        mid = min(b + k // 2, n_st - 1)
        front = slice(min(b + k - 1, n_st - 1), min(b + k + 1, n_st))      # stations around the front edge
        back = slice(max(b - 1, 0), b + 1)                                  # and the back edge
        for j in np.nonzero(blk.any(axis=0))[0]:
            elev = float(np.nanmax(dem[b:b + k, j])) / FT_M
            across = 2.0 * float(half_w[mid]) * NM_M / max(n_lines - 1, 1)
            lvl = 2 if s_m[mid] / NM_M <= d_w else 1
            left, right = slice(max(j - 1, 0), j + 1), slice(j, min(j + 2, n_lines))   # lines: the index grows to the right

            def corner(st, ln, elev=elev):
                v = dem[st, ln]
                return round(float(np.nanmax(v)) / FT_M) if np.isfinite(v).any() else round(elev)
            fb = min(b + k, n_st)                                   # vertex rows: front and back boundaries
            vtx = []
            for r_, c_ in ((fb, j), (fb, j + 1), (b, j + 1), (b, j)):   # front-left, front-right, back-right, back-left
                vtx += [round(float(v_lat[r_, c_]), 6), round(float(v_lon[r_, c_]), 6)]
            cells.append([round(float(lat_s[mid, j]), 5), round(float(lon_s[mid, j]), 5), round(elev), lvl,
                          round(math.degrees(float(head[mid])) % 360.0, 1), round(across),
                          corner(front, left), corner(front, right), corner(back, right), corner(back, left)] + vtx)
    return cells, STEP_M * k


def vertex_grid(g):
    """The corners between the samples of a (stations, lines) grid: the mean of the four samples around each, the grid
    extended by one linear step on every side first. Shape (stations + 1, lines + 1)."""
    g = np.asarray(g, float)
    if g.shape[0] < 2:
        g = np.vstack([g, g])
    p = np.empty((g.shape[0] + 2, g.shape[1] + 2))
    p[1:-1, 1:-1] = g
    p[0, 1:-1], p[-1, 1:-1] = 2 * g[0] - g[1], 2 * g[-1] - g[-2]
    p[:, 0], p[:, -1] = 2 * p[:, 1] - p[:, 2], 2 * p[:, -2] - p[:, -3]
    return (p[:-1, :-1] + p[1:, :-1] + p[:-1, 1:] + p[1:, 1:]) / 4.0


class Flta:
    """The stateful loop: descent state, on-ground / liftoff gate, auto-disable, assertion and clear filters."""

    def __init__(self, turn=True):
        self.turn = turn
        self.descent = DescentState()
        self.level = 0
        self.kind = 0
        self.t = 0.0
        self.raw_hist = []            # (t, level, kind)
        self.clear_since = None
        self.kind_since = None
        self.pending = None                # (kind, since): a different kind waiting out KIND_HOLD_S
        self.cand = None                   # the held candidate (plan O4 C6)
        self.liftoff_t = None
        self.was_on_ground = None          # unknown until the first sample: only a real ground-to-air edge starts the liftoff timer
        self.last = None
        self.vs_f = None                   # smoothed vertical speed (fpm) and its sample time
        self.vs_t = None
        self.down_since = None             # start of the continuous absence of raw warnings under a warning
        self.inst_since = None             # start of the run of instantaneous-VS warnings
        self.last_t = None
        self.last_out = None
        self.last_key = None
        self.raw_since = None              # start of the current run of raw alert results (level > 0)

    def proj_vs(self, vs, t):
        """The vertical speed the projection uses: the shallower of the current one and the VS_TAU_S smoothed one
        (opposite signs: level)."""
        if self.vs_f is not None and self.vs_t is not None and t == self.vs_t:
            pass                           # the same sample again (Fable F1): no update
        elif self.vs_f is None or self.vs_t is None or t < self.vs_t or t - self.vs_t > GAP_S:
            self.vs_f = vs
        else:
            a = 1.0 - math.exp(-(t - self.vs_t) / VS_TAU_S)
            self.vs_f += a * (vs - self.vs_f)
        self.vs_t = t
        f = self.vs_f
        if vs < 0.0 and f < 0.0:
            return max(vs, f)
        if vs > 0.0 and f > 0.0:
            return min(vs, f)
        return 0.0

    def tick(self, state, runways, dem_lookup_m, now=None, obstacles=None):
        """state: lat, lon, alt (ft), vs (fpm), track (deg true), gs (kt), bank (deg), on_ground (bool), t (s, sample
        time), gps_valid (bool). now = the clock when the result is published (age correction). Returns the output
        dict with level / kind / t / dem (1 ok, 0 gap) / stamp / raw."""
        t = float(state["t"])
        now = t if now is None else float(now)
        key = tuple(state.get(k) for k in ("lat", "lon", "alt", "vs", "track", "gs", "bank", "on_ground", "gps_valid"))
        same_t = self.last_t is not None and t == self.last_t
        if same_t and key == self.last_key and self.last_out is not None:
            # the same packet again (the server re-reads the last SimConnect packet when the sim is slow): the held output,
            # no filter state touched (Fable F1: treating it as a rewind wiped every timer)
            return dict(self.last_out, stamp=now)
        # same t with different inputs (a frozen clock, Codex r4 P2): processed in full so ground / GPS edges are not
        # lost, but no timer advances (t is unchanged) and it replaces the last assertion entry instead of adding one
        self.last_key = key
        if self.last_t is not None and (t < self.last_t or t - self.last_t > GAP_S):
            # a rewind or a gap: no filter timer may span it (the held alert itself stays until it clears)
            self.raw_hist, self.raw_since, self.down_since, self.clear_since, self.inst_since, self.pending = [], None, None, None, None, None
            if self.kind_since is not None:
                self.kind_since = t
            # the descent qualification restarts too (Codex r2 P1: a stale `since` kept the descending line off), and a
            # liftoff timer on the old timeline is dropped (it gated every alert until the old t + 10 s)
            self.descent.since = None
            if self.liftoff_t is not None and (t < self.liftoff_t or t - self.liftoff_t > LIFTOFF_OFF_S):
                self.liftoff_t = None
        self.last_t = t
        on_ground = bool(state.get("on_ground", False))
        if self.was_on_ground is True and not on_ground:
            self.liftoff_t = t
        self.was_on_ground = on_ground
        descending = self.descent.update(float(state["vs"]), t)
        vs_p = self.proj_vs(float(state["vs"]), t)
        gate = on_ground or (self.liftoff_t is not None and t - self.liftoff_t < LIFTOFF_OFF_S) or not state.get("gps_valid", True)
        raw = None
        if gate or auto_disabled(state, runways):
            raw = {"level": 0, "kind": 0, "t": 0.0, "dem_ok": True, "gated": True, "terrain": None, "obstacle": None, "obstacles": []}
            self.inst_since = None          # a gated sample breaks the instantaneous-warning run (Codex r2 P2)
        else:
            raw = project(dict(state, vs=vs_p), runways, dem_lookup_m, descending, turn=self.turn, obstacles=obstacles)
            vs_i = float(state["vs"])
            inst = None
            if vs_i < vs_p - 50.0:
                # the instantaneous (steeper) descent: a warning from it, held INST_WARN_S, overrides the smoothed result
                # so a real descent after a climb is not delayed by the filter's memory of the climb
                inst = project(state, runways, dem_lookup_m, descending, turn=self.turn, obstacles=obstacles)
            if inst is not None and inst["level"] == 2:
                self.inst_since = t if self.inst_since is None else self.inst_since
                if t - self.inst_since >= INST_WARN_S and raw["level"] < 2:
                    inst["inst"] = True
                    raw = inst
            else:
                self.inst_since = None
        raw["descending"] = descending
        raw["vs_proj"] = vs_p
        self.last = raw
        # ---- filters: 1 s assertion (two consecutive results; a new caution CAUTION_ASSERT_S), upgrades immediate,
        # ALERT_CLEAR_S clear, WARN_DOWN_S warning-to-caution, kind hysteresis.
        # The held alert is a whole candidate (kind = wording and source, its time and its obstacles), so the published
        # wording, countdown and obstacle payload always belong to one conflict (plan O4 §6 C6). A different kind takes
        # over only after KIND_HOLD_S of continuous disagreement (the pending-kind timer).
        if same_t and self.raw_hist:
            self.raw_hist[-1] = (t, raw["level"], raw["kind"])
        else:
            self.raw_hist.append((t, raw["level"], raw["kind"]))
        self.raw_hist = self.raw_hist[-ASSERT_TICKS:]
        lv = raw["level"]
        if lv > 0:
            self.raw_since = t if self.raw_since is None else self.raw_since
        else:
            self.raw_since = None
        # warning release: the continuous absence of raw warnings (caution or clear results alike) for WARN_DOWN_S steps
        # a held warning down to a caution; a clear run of ALERT_CLEAR_S then clears it (Codex r1 P2: caution and clear
        # results used to reset each other's timer and a warning could stick)
        if self.level == 2 and lv < 2:
            self.down_since = t if self.down_since is None else self.down_since
            if t - self.down_since >= WARN_DOWN_S and (self.kind_since is None or t - self.kind_since >= CLEAR_S):
                self.level, self.down_since = 1, None
        else:
            self.down_since = None
        if lv > 0:
            self.clear_since = None
            if lv > self.level:
                asserted = len(self.raw_hist) >= ASSERT_TICKS and all(h[1] >= lv for h in self.raw_hist)
                if lv == 1 and self.level == 0:
                    asserted = asserted and t - self.raw_since >= CAUTION_ASSERT_S
                # over a held caution a warning is immediate while the raw alert run is 1 s old or more; a run that has
                # just restarted (the held caution's conflict had gone) needs two results (Fable F3: one spike was a PULL UP)
                upgrade = self.level > 0 and (t - self.raw_since >= 1.0 or asserted)
                if upgrade or (self.level == 0 and asserted):
                    # a new or more severe alert takes the raw winner's kind at once (as before)
                    self.level, self.kind, self.kind_since, self.pending = lv, raw["kind"], t, None
                    self.cand = self._cand(raw, self.kind, t)
            if self.level > 0:
                if raw["kind"] != self.kind:
                    if self.pending is None or self.pending[0] != raw["kind"]:
                        self.pending = (raw["kind"], t)
                    elif t - self.pending[1] >= (KIND_BACK_S if (self.kind in (1, 3) and raw["kind"] in (0, 2)) else KIND_HOLD_S):
                        self.kind, self.kind_since, self.pending = raw["kind"], t, None
                else:
                    self.pending = None
                c = self._cand(raw, self.kind, t)
                if c is not None:
                    self.cand = c
        else:
            self.pending = None
            if self.level > 0:
                self.clear_since = t if self.clear_since is None else self.clear_since
                if t - self.clear_since >= ALERT_CLEAR_S:
                    self.level, self.kind, self.clear_since, self.kind_since, self.cand = 0, 0, None, None, None
        c = self.cand
        # age clamped at 0: after a rewind a held candidate's stamp is in the future (Fable F4; FLTA_T is logged, not shown)
        self.t = max(0.0, c["t"] - max(0.0, now - c["at"])) if (self.level > 0 and c is not None) else 0.0
        # impact cells only behind an asserted alert; red only while the alert itself is a warning
        cells = [c_[:3] + [2 if (self.level == 2 and c_[3] == 2) else 1] + c_[4:] for c_ in raw.get("cells", [])] if self.level > 0 else []
        obs = []
        if self.level > 0 and self.kind in (2, 3) and c is not None:
            obs = [dict(o, level=2 if (self.level == 2 and o["level"] == 2) else 1) for o in c.get("obstacles", [])]
        self.last_out = {"level": self.level, "kind": self.kind, "t": round(self.t, 1), "dem": 1 if raw["dem_ok"] else 0, "stamp": now, "raw": raw,
                         "cells": cells, "cell_along_m": raw.get("cell_along_m", STEP_M), "obstacles": obs}
        return self.last_out

    @staticmethod
    def _cand(raw, kind, t):
        """The raw candidate of the held kind (terrain 0 / 1 or obstacle 2 / 3), stamped with its sample time, or None."""
        c = raw.get("obstacle") if kind in (2, 3) else raw.get("terrain")
        if c is None or c["kind"] != kind:
            return None
        return dict(c, at=t)
