"""QM2 (CHESS ID4B) beamline condition monitor from the CHESS Signals
archive (read only; nothing is ever sent to the beamline).

    python ml/beamline_monitor.py                  # last 24 h, once
    python ml/beamline_monitor.py --hours 72
    python ml/beamline_monitor.py --end 2026-11-13T18:00 --hours 48
    python ml/beamline_monitor.py --every 15       # repeat every 15 min
    python ml/beamline_monitor.py --energy 20 --gain 500e-9   # override

Checks (limits from the normal 2026-3 behaviour; see LIMITS):
  beam        ring current > 50 mA
  vacuum      DCM chamber, 4B mirror chamber; spikes
  roughing    DCM roughing line (normal 0.0065 Torr; 2026-3 failure 0.01-0.33)
  ic1         ic1 per mA: scatter within the hour and change over the window
  ic2/ic1     beam on the slits: drift over the window; ×2-2.5 steps = gain
  mostab      share of shutter-open hours (ic1 > 0.2 V) with swings > 2 V
              (with the shutter closed mostab hunts without a signal);
              parked at 4.52 V = feedback off
  stale       a gauge with no data for > 2 h is reported
  experiment  "Experiment running" when ic2 > 0.1 V and diode > 0.05 V over
              the last 15 min (INFO): "Hutch Closed - Experiment Running";
              ic1 > 0.2 V only: "Hutch Closed - Beam in Hutch, Experiment
              NOT Running"; ring on, no ic1: "Hutch Open - No Beam"; ring
              off: "No Beam in CESR"; ic1 switching between beam
              (> 2,000 counts) and no beam (< 200) >= 4 times in 15 min
              with the ring on: "Check Beam Motion". Counters now: ic1 / ic2 / diode in counts per 0.1 s
              (as SPEC with a 0.1 s count time = V x 1e4)
  cryostat    Lakeshore sample / stage 1 / stage 2 (K): value, change in the
              last hour, cold or warm; information only (INFO, not graded)
  DCM temp    crystal-1 mount thermocouples, 5-95 % range with beam on
  (vacuum status uses hourly medians; brief spikes are reported, not graded)
  flux        ic1 photons/s vs the current model. Energy: from the latest
              SPEC scan (#G4); ic1 gain: #UAMP if logged, else the notes
              table for that energy (assumed). --energy/--gain override,
              --no-flux skips it.
Writes ml/results/monitor/: status_latest.md, monitor_latest.png and
appends one line per run to monitor_log.csv (tracking over time).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
URL = ("http://signals.chess.cornell.edu/plot/ARCHIVERDATA_{pvs}_{n}_FROM_"
       "{t0}_TO_{t1}")
PV = {
    "ring": "CESR_BR_POSI_A",
    "ic1": "ID4B_CNT00_VLT", "ic2": "ID4B_CNT02_VLT",
    "diode": "ID4B_CNT03_VLT", "mostab": "ID4B_CNT06_VLT",   # diode =
    # CNT03: matches the notes' Beam Condition panel (diode ~0.7 V while
    # ic1 2.0 V, ic2 1.8 V); CNT04 was a wrong match
    "dcm_cc": "S4C_DCM_CC_PR", "dcm_rv": "S4C_DCM_RV_PR",
    "mirror_cc": "S4C_FM_4B_CC_PR", "shutter": "S4F_SH_P04_PR",
    "gv1": "S4F_GV1_P01_PR", "gv2_us": "S4F_GV2_US_P02_PR",
    "gv2_ds": "S4F_GV2_DS_P03_PR",
    "x1_n": "S4C_DCM_X1_MNT_N_T", "x1_s": "S4C_DCM_X1_MNT_S_T",
}
# (warn, alert) limits
LIMITS = {
    "dcm_cc": (2.5e-7, 1.0e-6),        # Torr hourly median; normal 0.5-1.8e-7
    "mirror_cc": (1.0e-7, 1.0e-6),     # Torr; normal with beam ~3e-8
    "dcm_rv": (0.010, 0.030),          # Torr; normal 0.0065
    "ic1_cv": (2.0, 5.0),              # % scatter within an hour
    "ic1_change": (3.0, 10.0),         # % change over the window
    "ratio_drift": (5.0, 15.0),        # % ic2/ic1 change (not a gain step)
    "mostab_busy": (0.40, 0.60),       # share of beam-on hours, swing > 2 V
                                       # (good period 30 %, pump failure 72 %)
    "x1_temp_change": (1.5, 4.0),      # degC, 5-95 % range with beam on
    "flux_ratio": (0.20, 0.35),        # |measured/model - 1|
}
MOSTAB_PARK = 4.52
# experiment running = ic2 and the diode both show signal (user, 15 Nov
# 2026). Dark levels with the shutter closed (28 Oct-15 Nov 2026, 5-min
# medians): ic2 0.009 V (max 0.06), diode (CNT03) 0.0025 V (95 % 0.014);
# with an experiment the diode is ~0.35 V, up to 0.78 V (3,500-7,800
# counts per 0.1 s).
EXP_IC2_V, EXP_DIODE_V, EXP_MIN = 0.10, 0.05, 15
# beam motion (user, 15 Nov 2026): ic1 jumping between ~60 and ~19,000
# counts and back within minutes. Count switches between "beam" (ic1 >
# 0.2 V = 2,000 counts) and "no beam" (ic1 < 0.02 V = 200 counts) in the
# last EXP_MIN minutes; >= MOTION_SWITCHES (2 full up-down cycles) with
# the ring on = "Check Beam Motion".
IC1_HI_V, IC1_LO_V, MOTION_SWITCHES = 0.2, 0.02, 4


def ic1_switches(ic1, t_end, minutes=EXP_MIN):
    """Number of beam <-> no-beam switches of ic1 in the last minutes."""
    w = ic1[ic1.index >= t_end - dt.timedelta(minutes=minutes)]
    lvl = [1 if v > IC1_HI_V else 0 for v in w.values
           if v > IC1_HI_V or v < IC1_LO_V]
    return sum(a != b for a, b in zip(lvl, lvl[1:]))
# counts as SPEC shows them: V x 1e5 counts/s per V x 0.1 s count time
COUNT_TIME = 0.1
V2F = 1.0e5


def counters_now(ic1, ic2, diode, ring, t_end):
    """Counter values of the last EXP_MIN minutes as SPEC shows them
    (counts per 0.1 s count time), and the ring current."""
    def recent(x):
        w = x[x.index >= t_end - dt.timedelta(minutes=EXP_MIN)]
        return float(w.median()) if len(w) else float("nan")
    parts = []
    for name, x in (("ic1", ic1), ("ic2", ic2), ("diode", diode)):
        v = recent(x)
        parts.append(f"{name} {v * V2F * COUNT_TIME:,.0f}" if np.isfinite(v)
                     else f"{name} —")
    r = recent(ring)
    txt = " · ".join(parts) + f" counts per {COUNT_TIME:g} s"
    if np.isfinite(r):
        txt += f" · ring {r:.0f} mA"
    return txt


def experiment_state(ic1, ic2, diode, ring, t_end):
    """(state, text, share of the window with the experiment running)."""
    def recent(x):
        w = x[x.index >= t_end - dt.timedelta(minutes=EXP_MIN)]
        return float(w.median()) if len(w) else float("nan")
    i1, i2, d, r = recent(ic1), recent(ic2), recent(diode), recent(ring)
    grid = pd.concat([x.resample("5min").median().rename(k) for k, x in
                      (("ic2", ic2), ("diode", diode))], axis=1).dropna()
    share = float(((grid.ic2 > EXP_IC2_V) & (grid.diode > EXP_DIODE_V))
                  .mean()) if len(grid) else float("nan")
    vals = f"ic2 {i2:.3g} V, diode {d:.3g} V"
    nsw = ic1_switches(ic1, t_end)
    if nsw >= MOTION_SWITCHES and np.isfinite(r) and r > 50:
        state = "Check Beam Motion"
        vals = (f"ic1 jumped between beam and no beam {nsw} times; "
                + vals)
    elif np.isfinite(i2) and np.isfinite(d) and i2 > EXP_IC2_V \
            and d > EXP_DIODE_V:
        state = "Hutch Closed - Experiment Running"
    # user, 15 Nov 2026: ic1 ~16,600 counts with ic2/diode ~80/20 = hutch
    # closed, beam in the hutch, no experiment; ic1 ~60 = hutch open
    elif np.isfinite(i1) and i1 > 0.2:
        state = "Hutch Closed - Beam in Hutch, Experiment NOT Running"
    elif np.isfinite(r) and r > 50:
        state = "Hutch Open - No Beam"
    elif np.isfinite(r):
        state = "No Beam in CESR"
    else:
        state = "Unknown (no recent data)"
    txt = f"{state} ({vals}; last {EXP_MIN} min)"
    if np.isfinite(share):
        txt += f"; running {100 * share:.0f} % of the window"
    return state, txt, share
# Lakeshore cryostat (user, 15 Nov 2026); logged on change only, so the
# monitor reads the last CRYO_DAYS to know the current value. Information
# only (the sample temperature changes on purpose): not graded.
CRYO = {"sample": "ID4B_CRYOGL_SAM_T", "stage 1": "ID4B_CRYOGL_STG1_T",
        "stage 2": "ID4B_CRYOGL_STG2_T"}
CRYO_DAYS = 14
COLD_K = 250.0


def fetch_cryo(t1: dt.datetime, days: float = CRYO_DAYS) -> pd.DataFrame:
    pvs = list(CRYO.values())
    t0 = t1 - dt.timedelta(days=days)
    url = URL.format(pvs=";".join(pvs), n=";".join(["4000"] * len(pvs)),
                     t0=int(t0.timestamp() * 1000),
                     t1=int(t1.timestamp() * 1000))
    with urllib.request.urlopen(url, timeout=180) as r:
        data = json.load(r)
    rows = [(pv["name"], x, y) for pv in data
            for x, y in zip(pv["x"], pv["y"]) if y is not None]
    df = pd.DataFrame(rows, columns=["pv", "epoch_s", "value"])
    df["t"] = (pd.to_datetime(df.epoch_s, unit="s", utc=True)
               .dt.tz_convert("America/New_York").dt.tz_localize(None))
    return df


def cryo_rows(dc: pd.DataFrame, t1: dt.datetime, t0: dt.datetime):
    """Information rows and window series for the Lakeshore cryostat."""
    rows, ser = [], {}
    for name, pv in CRYO.items():
        x = dc[dc.pv == pv].set_index("t").value.sort_index()
        x = x[x.index <= t1]
        if not len(x):
            rows.append(("cryostat", f"Lakeshore {name}", "no data in "
                         f"{CRYO_DAYS} days", "INFO"))
            continue
        now = float(x.iloc[-1])
        h1 = x[x.index <= t1 - dt.timedelta(hours=1)]
        ch = now - float(h1.iloc[-1]) if len(h1) else float("nan")
        age = (t1 - x.index[-1]).total_seconds() / 3600
        txt = f"{now:.1f} K"
        if np.isfinite(ch):
            txt += f", {ch:+.1f} K in the last hour"
        txt += (f", last change {age:.0f} h ago" if age >= 1 else
                ", changing now")
        if name == "sample":
            txt += " (cold)" if now < COLD_K else " (warm)"
        rows.append(("cryostat", f"Lakeshore {name}", txt, "INFO"))
        w = x[x.index >= t0]
        before = x[x.index < t0]
        if len(before):                     # carry the value into the window
            w = pd.concat([pd.Series([before.iloc[-1]], index=[t0]), w])
        w = pd.concat([w, pd.Series([now], index=[t1])])
        ser[name] = w
    return rows, ser


def fetch(t0: dt.datetime, t1: dt.datetime, n: int = 3000) -> pd.DataFrame:
    pvs = list(PV.values())
    url = URL.format(pvs=";".join(pvs), n=";".join([str(n)] * len(pvs)),
                     t0=int(t0.timestamp() * 1000),
                     t1=int(t1.timestamp() * 1000))
    with urllib.request.urlopen(url, timeout=180) as r:
        data = json.load(r)
    rows = []
    for pv in data:
        for x, y in zip(pv["x"], pv["y"]):
            if y is not None:
                rows.append((pv["name"], x, y))
    df = pd.DataFrame(rows, columns=["pv", "epoch_s", "value"])
    df["t"] = (pd.to_datetime(df.epoch_s, unit="s", utc=True)
               .dt.tz_convert("America/New_York").dt.tz_localize(None))
    return df


SPEC_DIR = HERE.parents[1] / "align_data"      # SPEC files (read only)
# Light flux check (Apptainer image): a lookup table made by
# ml/make_flux_table.py. If flux_table.json sits next to this script (or
# --flux-table is given) it is used instead of the full twin.
FLUX_TABLE = HERE / "flux_table.json"


def _flux_table():
    import json as _json
    if FLUX_TABLE and Path(FLUX_TABLE).exists():
        return _json.loads(Path(FLUX_TABLE).read_text())
    return None


def table_flux(energy, gain):
    """(model ph/s at 143 mA, ic1 ph/s per V) from the lookup table, linear
    in energy; None if no table or the energy is outside it."""
    t = _flux_table()
    if t is None:
        return None
    r = np.array(t["rows"], dtype=float)
    if not r[0, 0] <= energy <= r[-1, 0]:
        return None
    return (float(np.interp(energy, r[:, 0], r[:, 1])),
            float(np.interp(energy, r[:, 0], r[:, 2])) * gain)
HC = 12.39842                                  # keV Angstrom


def auto_energy_gain(spec_dir: Path = SPEC_DIR) -> dict | None:
    """Energy of the most recent SPEC scan (#D date, #G4 wavelength) under
    spec_dir/<cycle>/, and the ic1 gain: the scan's #UAMP line if
    amp_gains.mac wrote one, else the gains noted in the beamline notes
    (configs/id4b_amplifier_gains.yaml, nearest energy within 1.5 keV),
    marked as assumed. None if no SPEC file is found."""
    best = None
    for f in sorted(Path(spec_dir).glob("20*/*")):
        if not f.is_file():
            continue
        when = uamp = None
        for line in open(f, errors="replace"):
            if line.startswith("#D "):
                try:
                    when = dt.datetime.strptime(line[3:].strip(),
                                                "%a %b %d %H:%M:%S %Y")
                except ValueError:
                    when = None
            elif line.startswith("#UAMP") and "ic1" in line:
                try:
                    uamp = float(line.split()[2])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("#G4") and when is not None:
                w = line.split()
                lam = float(w[4]) if len(w) > 4 else 0.0
                if lam > 0 and (best is None or when > best["when"]):
                    best = dict(when=when, energy=round(HC / lam, 2),
                                file=f"{f.parent.name}/{f.name}", uamp=uamp)
    if best is None:
        return None
    if best["uamp"]:
        best.update(gain=best["uamp"], gain_src="logged (#UAMP)")
    else:
        best["gain"], best["gain_src"] = table_gain(best["energy"])
    return best


def table_gain(energy):
    """ic1 sensitivity noted in the beamline notes for this energy (within
    1.5 keV), as (A/V, description); (None, reason) if none is known."""
    t = _flux_table()
    if t is not None:                    # light version: gains in the table
        g = {float(k): {"ic1": {"sens": v}}
             for k, v in t["ic1_gain_A_per_V"].items()}
    else:
        import yaml
        g = yaml.safe_load(open(HERE.parent / "configs" /
                                "id4b_amplifier_gains.yaml"))
    k = min(g, key=lambda e: abs(e - energy))
    if abs(k - energy) <= 1.5:
        return float(g[k]["ic1"]["sens"]), f"assumed (notes, {k:g} keV setting)"
    return None, f"no ic1 gain known near {energy:g} keV"


def level(v, key):
    w, a = LIMITS[key]
    if v is None or not np.isfinite(v):
        return "n/a"
    return "ALERT" if v >= a else "WARN" if v >= w else "OK"


def analyse(df: pd.DataFrame, energy=None, gain=None, t_end=None,
            source=""):
    def s(name):
        x = df[df.pv == PV[name]].set_index("t").value.sort_index()
        return x[x > 0] if name in ("dcm_cc", "dcm_rv", "mirror_cc") else x

    out, series = [], {}
    t_end = t_end or df.t.max()
    state, txt, _ = experiment_state(s("ic1"), s("ic2"), s("diode"),
                                     s("ring"), t_end)
    out.append(("experiment", "experiment status", txt,
                "WARN" if state == "Check Beam Motion" else "INFO"))
    out.append(("experiment", "counters now", counters_now(
        s("ic1"), s("ic2"), s("diode"), s("ring"), t_end), "INFO"))
    ring = s("ring")
    beam_on = float((ring > 50).mean()) if len(ring) else 0.0
    out.append(("beam", "ring current", f"{ring.iloc[-1]:.0f} mA now, beam "
                f"{100 * beam_on:.0f} % of the window" if len(ring) else "no "
                "data", "OK" if len(ring) and ring.iloc[-1] > 50 else "WARN"))
    # vacuum: status from the hourly median (sustained), spikes reported
    for key, lab in (("dcm_cc", "DCM chamber"), ("mirror_cc",
                                                  "4B mirror chamber"),
                     ("dcm_rv", "DCM roughing line")):
        x = s(key)
        if len(x):
            hm = x.resample("1h").median().dropna()
            sus, now, mx = float(hm.max()), float(x.iloc[-1]), float(x.max())
            spikes = int((x > LIMITS[key][1]).sum())
            txt = (f"now {now:.2g}, highest hourly median {sus:.2g} Torr")
            if spikes:
                txt += f"; {spikes} reading(s) above {LIMITS[key][1]:.0e} " \
                       f"(max {mx:.2g})"
            if key == "dcm_rv":
                txt += " (normal 0.0065)"
            st = level(sus, key)
            age = (t_end - x.index[-1]).total_seconds() / 3600
            if age > 2:
                txt += f"; NO DATA for the last {age:.0f} h"
                st = "WARN" if st == "OK" else st
            out.append(("vacuum", lab, txt, st))
    # counters normalised to ring current, beam on, shutter open
    ic1, ic2 = s("ic1"), s("ic2")
    m = pd.merge_asof(ic1.to_frame("ic1"), ring.to_frame("I"),
                      left_index=True, right_index=True, direction="nearest",
                      tolerance=pd.Timedelta("10min")).dropna()
    m = m[(m.I > 50) & (m.ic1 > 0.2)]
    if len(m) > 20:
        f = m.ic1 / m.I
        series["ic1_per_mA"] = f
        cv = float((f.resample("1h").std() / f.resample("1h").median() *
                    100).median())
        first = float(f.iloc[: max(len(f) // 10, 5)].median())
        last = float(f.iloc[-max(len(f) // 10, 5):].median())
        ch = 100 * (last - first) / first
        out.append(("beam", "ic1 per mA, scatter", f"{cv:.1f} % within an "
                    "hour", level(cv, "ic1_cv")))
        out.append(("beam", "ic1 per mA, change", f"{ch:+.1f} % over the "
                    "window", level(abs(ch), "ic1_change")))
        lut = table_flux(energy, gain) if energy and gain else None
        if energy and gain and lut is None and _flux_table() is not None:
            out.append(("flux", "vs current model", f"not checked: "
                        f"{energy:g} keV outside the flux table", "n/a"))
        elif energy and gain:
            if lut is not None:          # light version (lookup table)
                model, per_v = lut
                source += "; model from the lookup table"
            else:                        # full twin
                sys.path.insert(0, str(HERE))
                import combined_twin as ct
                import ion_chamber as icm
                per_v = icm.photons_per_s(1e5, gain, energy, "N2", 6.0)[0]
                model = ct.flux_current(energy)
            meas = last * 143.0 * per_v
            q = meas / model
            out.append(("flux", f"{energy:g} keV vs current model",
                        f"measured {meas:.2e} ph/s, model {model:.2e} "
                        f"(measured/model {q:.2f}); {source}",
                        level(abs(q - 1), "flux_ratio")))
        elif source:
            out.append(("flux", "vs current model", f"not checked: {source}",
                        "n/a"))
    r = pd.merge_asof(ic2.to_frame("ic2"), ic1.to_frame("ic1"),
                      left_index=True, right_index=True, direction="nearest",
                      tolerance=pd.Timedelta("5min")).dropna()
    r = r[(r.ic1 > 0.2) & (r.ic2 > 0.05)]
    if len(r) > 20:
        q = r.ic2 / r.ic1
        series["ic2_ic1"] = q
        first = float(q.iloc[: max(len(q) // 10, 5)].median())
        last = float(q.iloc[-max(len(q) // 10, 5):].median())
        step = last / first
        if 1.8 < step < 2.8 or 1 / 2.8 < step < 1 / 1.8:
            out.append(("beam", "ic2/ic1 (beam on the slits)",
                        f"step ×{step:.2f}: probably an ic2 gain change",
                        "WARN"))
        else:
            d = 100 * (step - 1)
            out.append(("beam", "ic2/ic1 (beam on the slits)",
                        f"{d:+.1f} % over the window",
                        level(abs(d), "ratio_drift")))
    # hours with beam in the hutch (shutter open): ic1 above its dark level
    beam = (ic1 > 0.2).resample("1h").mean() > 0.9
    ms = s("mostab")
    if len(ms):
        series["mostab"] = ms
        sw = (ms.resample("1h").max() - ms.resample("1h").min())
        sw = sw[beam.reindex(sw.index, fill_value=False)]
        busy = float((sw > 2.0).mean()) if len(sw) else float("nan")
        parked = bool(abs(ms.iloc[-1] - MOSTAB_PARK) < 0.005)
        txt = (f"{100 * busy:.0f} % of shutter-open hours ({len(sw)}) with "
               f"swings > 2 V, now {ms.iloc[-1]:.2f} V")
        txt += " (parked: feedback off or no beam)" if parked else ""
        out.append(("beam", "mostab activity", txt,
                    level(busy, "mostab_busy")))
    on = ring.resample("10min").median() > 50
    for key, lab in (("x1_n", "crystal-1 mount N"),
                     ("x1_s", "crystal-1 mount S")):
        x = s(key)
        if len(x) > 5:
            xb = x.resample("10min").median()
            xb = xb[on.reindex(xb.index, fill_value=False)].dropna()
            ch = float(np.percentile(xb, 95) - np.percentile(xb, 5)) \
                if len(xb) > 5 else float("nan")
            out.append(("DCM", lab, f"now {x.iloc[-1]:.1f} °C, 5-95 % range "
                        f"with beam {ch:.1f} °C",
                        level(ch, "x1_temp_change")))
    for key in ("dcm_cc", "dcm_rv"):
        series[key] = s(key)
    return out, series


def report(out, series, t0, t1, odir: Path):
    odir.mkdir(parents=True, exist_ok=True)
    worst = ("ALERT" if any(o[3] == "ALERT" for o in out) else
             "WARN" if any(o[3] == "WARN" for o in out) else "OK")
    lines = [f"# QM2 beamline status: {worst}",
             f"Window {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} "
             "(CHESS Signals archive, read only)", "",
             "| Group | Check | Value | Status |", "|---|---|---|---|"]
    lines += [f"| {g} | {c} | {v} | **{st}** |" for g, c, v, st in out]
    (odir / "status_latest.md").write_text("\n".join(lines) + "\n")
    log = odir / "monitor_log.csv"
    new = not log.exists()
    with open(log, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_time", "window_end", "overall"] +
                       [f"{g}:{c}" for g, c, _, _ in out])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"),
                    t1.isoformat(timespec="minutes"), worst] +
                   [st for *_, st in out])
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    panels = [k for k in ("dcm_cc", "dcm_rv", "ic1_per_mA", "ic2_ic1",
                          "mostab", "cryo") if k in series and len(series[k])]
    labels = {"dcm_cc": "DCM chamber (Torr)",
              "dcm_rv": "DCM roughing line (Torr)",
              "ic1_per_mA": "ic1 V per mA", "ic2_ic1": "ic2 / ic1",
              "mostab": "mostab (V)", "cryo": "Lakeshore (K)"}
    fig, axs = plt.subplots(len(panels), 1, figsize=(11, 2 * len(panels)),
                            sharex=True, squeeze=False)
    for ax, k in zip(axs[:, 0], panels):
        x = series[k]
        if k == "cryo":
            for name, w in x.items():
                ax.step(w.index, w.values, where="post", lw=1, label=name)
            ax.legend(frameon=False, fontsize=7, loc="upper left")
        elif k in ("ic1_per_mA", "ic2_ic1"):
            ax.plot(x.index, x.values, ".", ms=2)
        else:
            ax.plot(x.index, x.values, lw=0.8)
        if k in ("dcm_cc", "dcm_rv"):
            ax.set_yscale("log")
            for lim, c in zip(LIMITS[k], ("#eda100", "#d03b3b")):
                ax.axhline(lim, color=c, lw=0.8, ls="--")
        ax.set_ylabel(labels[k], fontsize=8)
        ax.grid(color="#e1e0d9", lw=0.6)
    axs[0, 0].set_title(f"QM2 beamline status: {worst}  "
                        f"({t0:%d %b %H:%M} → {t1:%d %b %H:%M})", loc="left",
                        fontsize=10)
    fig.tight_layout()
    fig.savefig(odir / "monitor_latest.png", dpi=130)
    plt.close(fig)
    return worst, lines


def resolve_energy_gain(energy, gain, spec_dir=SPEC_DIR, no_flux=False):
    """Energy and ic1 gain for the flux check: given on the command line,
    else automatic from the latest SPEC scan and the notes."""
    if no_flux:
        return None, None, ""
    if energy and gain:
        return energy, gain, "energy and gain given"
    if energy:                                  # energy given, gain looked up
        g, gsrc = (gain, "given") if gain else table_gain(energy)
        src = f"energy {energy:g} keV given; gain {gsrc}"
        return (energy, g, src) if g else (None, None, src)
    auto = auto_energy_gain(spec_dir)
    if auto is None:
        return None, None, "no SPEC file found for the energy"
    e = auto["energy"]
    g, gsrc = (gain, "given") if gain else (auto["gain"], auto["gain_src"])
    src = (f"energy {e:g} keV from {auto['file']} "
           f"({auto['when']:%d %b %H:%M}); gain {gsrc}")
    return (e, g, src) if g else (None, None, src)


def add_cryo(out, series, t0, t1):
    """Append the Lakeshore rows and plot series (information only)."""
    try:
        rows, ser = cryo_rows(fetch_cryo(t1), t1, t0)
    except Exception as e:                      # keep the other checks
        rows, ser = [("cryostat", "Lakeshore", f"not read: {e}", "INFO")], {}
    out.extend(rows)
    if ser:
        series["cryo"] = ser


def run_once(a):
    t1 = dt.datetime.fromisoformat(a.end) if a.end else dt.datetime.now()
    t0 = t1 - dt.timedelta(hours=a.hours)
    df = fetch(t0, t1)
    energy, gain, source = resolve_energy_gain(a.energy, a.gain,
                                               a.spec_dir, a.no_flux)
    out, series = analyse(df, energy, gain, t1, source)
    add_cryo(out, series, t0, t1)
    worst, lines = report(out, series, t0, t1, a.out)
    print("\n".join(lines))
    return worst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--end", default=None,
                    help="end of the window (default: now)")
    ap.add_argument("--every", type=float, default=0,
                    help="repeat every N minutes (0 = once)")
    ap.add_argument("--energy", type=float, default=None)
    ap.add_argument("--gain", type=float, default=None,
                    help="ic1 SR570 sensitivity, A/V (e.g. 500e-9)")
    ap.add_argument("--spec-dir", type=Path, default=SPEC_DIR,
                    help="SPEC files for the automatic energy")
    ap.add_argument("--no-flux", action="store_true",
                    help="skip the flux check")
    ap.add_argument("--out", type=Path,
                    default=HERE / "results" / "monitor")
    ap.add_argument("--flux-table", type=Path, default=None,
                    help="flux lookup table (ml/make_flux_table.py) instead "
                         "of the full twin")
    a = ap.parse_args()
    global FLUX_TABLE
    if a.flux_table:
        FLUX_TABLE = a.flux_table
    while True:
        try:
            run_once(a)
        except Exception as e:                   # keep monitoring
            print(f"[{dt.datetime.now():%H:%M}] fetch/analysis failed: {e}")
        if not a.every:
            break
        time.sleep(a.every * 60)


if __name__ == "__main__":
    main()
