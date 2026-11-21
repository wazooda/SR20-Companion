"""Glide range ring geometry (docs/plans/glide_range_ring.md §3.2). Pure functions, no sim: testable offline.

72 trajectories, one per ground track 0, 5, ... 355 deg true. Each starts from the current true heading, turns the
shortest way at BANK_DEG toward the air heading that makes good that track (crab into the wind), then glides straight;
it ends where the true altitude comes within 1000 ft of the terrain, at MAX_NM, or where terrain is unknown (the ray is
then "missing" and the ring leaves a gap). Endpoints, not ray lengths, are returned: a drifting turn moves them off the
nominal radial, which is correct.

SR20 numbers: best glide 100 KIAS at 3150 lb, 92 at 2600 lb, flaps up (POH §3 p.3-16); glide ratio ~9:1 (POH §3
Figure 3-1). Turn model (explicit approximation, tune against Pilot's Guide Figure 5-22): 30 deg bank, sink in the turn
increased by 1/cos^1.5(bank).
"""
import math

import numpy as np

KT = 0.514444                 # m/s per knot
FT = 0.3048
NM = 1852.0
G = 9.80665
RATIO = 9.0
BANK_DEG = 30.0
FLOOR_FT = 1000.0
MAX_NM = 60.0
STEP_M = 45.0                 # straight-segment sampling, half a GLO-90 cell
MIN_NM = 0.2                  # aircraft always inside the ring
RHO0 = 0.0023769              # slug/ft^3, ISA sea level
N_RAYS = 72


def glide_kias(weight_lb):
    w = min(max(weight_lb, 2600.0), 3150.0)
    return 92.0 + (w - 2600.0) / 550.0 * 8.0


def ring(state, terrain, n_rays=N_RAYS):
    """state: lat, lon (deg), alt_ft (true MSL), hdg_true (deg), wind_from (deg true), wind_kt, weight_lb,
    density (slug/ft^3). terrain(lat_array, lon_array) -> metres MSL, NaN = unknown.
    Returns (lat[n], lon[n], missing[n] bool, info dict)."""
    lat0, lon0 = state["lat"], state["lon"]
    tas = glide_kias(state["weight_lb"]) * math.sqrt(RHO0 / max(state["density"], 1e-5)) * KT
    sink = tas / RATIO
    phi = math.radians(BANK_DEG)
    omega = G * math.tan(phi) / tas
    sink_turn = sink / math.cos(phi) ** 1.5
    wf = math.radians(state["wind_from"])
    w = -state["wind_kt"] * KT * np.array([math.sin(wf), math.cos(wf)])          # wind-to vector (east, north)
    R = 6371000.0
    coslat = max(math.cos(math.radians(lat0)), 0.05)

    def geo(x, y):
        return lat0 + np.degrees(y / R), lon0 + np.degrees(x / (R * coslat))

    def clearance(x, y, h):
        la, lo = geo(x, y)
        return h - terrain(la, lo) - FLOOR_FT * FT

    th = np.radians(np.arange(n_rays) * 360.0 / n_rays)
    u = np.stack([np.sin(th), np.cos(th)], 1)            # desired ground track
    nrm = np.stack([np.cos(th), -np.sin(th)], 1)         # right of track
    w_al, w_cr = u @ w, nrm @ w
    c = -w_cr / tas                                       # sine of the crab angle
    feasible = np.abs(c) < 0.999
    c = np.clip(c, -0.999, 0.999)
    gs = w_al + tas * np.sqrt(1 - c * c)
    feasible &= gs > 1.0
    air = tas * (np.sqrt(1 - c * c)[:, None] * u + c[:, None] * nrm)
    psi = np.arctan2(air[:, 0], air[:, 1])

    # turn: integrate 1 s steps, all rays together
    psi0 = math.radians(state["hdg_true"])
    d = (psi - psi0 + math.pi) % (2 * math.pi) - math.pi
    t_turn = np.abs(d) / omega
    x, y = np.zeros(n_rays), np.zeros(n_rays)
    h = np.full(n_rays, state["alt_ft"] * FT)
    ended = np.zeros(n_rays, bool)
    missing = np.zeros(n_rays, bool)
    t = 0.0
    while True:
        dt = np.clip(t_turn - t, 0.0, 1.0)
        live = (dt > 0) & ~ended
        if not live.any():
            break
        hd = psi0 + np.sign(d) * np.minimum(t + dt / 2, t_turn) * omega
        x1 = x + (tas * np.sin(hd) + w[0]) * dt
        y1 = y + (tas * np.cos(hd) + w[1]) * dt
        h1 = h - sink_turn * dt
        cl = clearance(x1, y1, h1)
        unknown = live & np.isnan(cl)
        missing |= unknown
        hit = live & ~unknown & (cl <= 0)
        x = np.where(live & ~unknown, x1, x)
        y = np.where(live & ~unknown, y1, y)
        h = np.where(live & ~unknown, h1, h)
        ended |= hit | unknown
        t += 1.0

    # straight glide along each track from the turn exit
    steps = int(MAX_NM * NM / STEP_M)
    k = np.arange(1, steps + 1) * STEP_M                                          # along-track distance
    go = ~ended & feasible
    end_x, end_y = x.copy(), y.copy()
    if go.any():
        idx = np.where(go)[0]
        px = x[idx, None] + u[idx, 0, None] * k[None, :]
        py = y[idx, None] + u[idx, 1, None] * k[None, :]
        ph = h[idx, None] - sink * k[None, :] / gs[idx, None]
        dist = np.hypot(px, py)
        cl = clearance(px, py, ph)
        cl = np.where(dist > MAX_NM * NM, -1.0, cl)       # cap: treat beyond MAX_NM as the end
        bad = np.isnan(cl) | (cl <= 0)
        first = np.where(bad.any(1), bad.argmax(1), steps - 1)
        for j, r in enumerate(idx):
            f = first[j]
            if np.isnan(cl[j, f]):
                missing[r] = True
                continue
            if not bad[j, f]:                                # never hit anything within the sampled distance
                end_x[r], end_y[r] = px[j, f], py[j, f]
            elif f == 0:
                c0 = clearance(np.array([x[r]]), np.array([y[r]]), np.array([h[r]]))[0]
                frac = c0 / (c0 - cl[j, 0]) if c0 > 0 else 0.0
                end_x[r], end_y[r] = x[r] + u[r, 0] * STEP_M * frac, y[r] + u[r, 1] * STEP_M * frac
            else:
                a, b = cl[j, f - 1], cl[j, f]
                frac = a / (a - b) if a != b else 1.0
                end_x[r] = px[j, f - 1] + (px[j, f] - px[j, f - 1]) * frac
                end_y[r] = py[j, f - 1] + (py[j, f] - py[j, f - 1]) * frac

    # aircraft always inside: endpoints at least MIN_NM out along their track
    r = np.hypot(end_x, end_y)
    along = end_x * u[:, 0] + end_y * u[:, 1]
    small = (r < MIN_NM * NM) | (along < MIN_NM * NM)
    end_x = np.where(small, u[:, 0] * MIN_NM * NM, end_x)
    end_y = np.where(small, u[:, 1] * MIN_NM * NM, end_y)
    la, lo = geo(end_x, end_y)
    info = dict(tas_kt=tas / KT, sink_fpm=sink / FT * 60, max_nm=float(np.hypot(end_x, end_y).max() / NM),
                min_nm=float(np.hypot(end_x, end_y).min() / NM), missing=int(missing.sum()))
    return la, lo, missing, info
