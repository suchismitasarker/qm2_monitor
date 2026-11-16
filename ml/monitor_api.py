"""QM2 beamline condition monitor as a small FastAPI web app (read only:
it only reads the CHESS Signals archive; nothing is sent to the beamline).

    pip install fastapi uvicorn
    cd autoalignment-4b
    python ml/monitor_api.py                     # http://127.0.0.1:8050
    python ml/monitor_api.py --energy 20 --gain 500e-9   # override the
                                                 # automatic energy/gain

Endpoints
    /                    status page (auto-refreshes), plot and history
    /api/status          latest status as JSON
    /api/check?hours=48&end=2026-11-13T18:00&energy=20&gain=5e-7
                         run the checks for any window now (JSON)
    /plot.png            latest plot
    /api/history         the run log (monitor_log.csv) as JSON
The checks and limits are those of ml/beamline_monitor.py. The server binds
to 127.0.0.1 (this computer only) unless --host is given.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import beamline_monitor as bm  # noqa: E402

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402

CFG = dict(hours=24.0, refresh=10.0, fast=1.0, energy=None, gain=None,
           spec_dir=bm.SPEC_DIR, no_flux=False,
           out=HERE / "results" / "monitor")
STATE = {"checks": [], "overall": "starting", "window": None,
         "updated": None, "error": None}
LOCK = threading.Lock()
COLOR = {"OK": "#1b8a3a", "WARN": "#b07800", "ALERT": "#c62828",
         "INFO": "#2a78d6",
         "n/a": "#666666"}

app = FastAPI(title="QM2 beamline monitor",
              description="Read-only condition monitor from the CHESS "
                          "Signals archive (ml/beamline_monitor.py checks).")


def run_checks(hours, end=None, energy=None, gain=None, write=True):
    t1 = dt.datetime.fromisoformat(end) if end else dt.datetime.now()
    t0 = t1 - dt.timedelta(hours=hours)
    df = bm.fetch(t0, t1)
    e, g, src = bm.resolve_energy_gain(energy, gain, CFG["spec_dir"],
                                       CFG["no_flux"])
    out, series = bm.analyse(df, e, g, t1, src)
    if not end:                         # live: beam rows at full resolution
        bm.use_beam_rows(out, t1)
    bm.add_cryo(out, series, t0, t1)
    if write:
        worst, _ = bm.report(out, series, t0, t1, CFG["out"])
    else:
        worst = ("ALERT" if any(o[3] == "ALERT" for o in out) else
                 "WARN" if any(o[3] == "WARN" for o in out) else "OK")
    return dict(overall=worst,
                window=[t0.isoformat(timespec="minutes"),
                        t1.isoformat(timespec="minutes")],
                checks=[dict(group=g, check=c, value=v, status=s)
                        for g, c, v, s in out])


def refresh_once():
    try:
        res = run_checks(CFG["hours"], None, CFG["energy"], CFG["gain"])
        with LOCK:
            now = dt.datetime.now().isoformat(timespec="seconds")
            STATE.update(res, updated=now, beam_updated=now, error=None)
    except Exception as e:                          # keep serving
        with LOCK:
            STATE.update(error=f"{type(e).__name__}: {e}",
                         updated=dt.datetime.now().isoformat(
                             timespec="seconds"))


def _worst(checks):
    ranks = [c["status"] for c in checks]
    return ("ALERT" if "ALERT" in ranks else "WARN" if "WARN" in ranks
            else "OK")


def refresh_beam():
    """Light update every CFG['fast'] minutes: beam status and counters
    (last bm.STATUS_MIN min, full archive resolution); the other checks
    keep their last full read."""
    try:
        fresh = {c: (v, s) for _, c, v, s in bm.beam_rows(dt.datetime.now())}
    except Exception:                               # keep the last values
        return
    with LOCK:
        if not STATE["checks"]:
            return
        for c in STATE["checks"]:
            if c["check"] in fresh:
                c["value"], c["status"] = fresh[c["check"]]
        STATE["overall"] = _worst(STATE["checks"])
        STATE["beam_updated"] = dt.datetime.now().isoformat(
            timespec="seconds")


def loop():
    next_full = 0.0
    while True:
        if time.time() >= next_full:
            refresh_once()
            next_full = time.time() + CFG["refresh"] * 60
        else:
            refresh_beam()
        time.sleep(min(CFG["fast"], CFG["refresh"]) * 60)


@app.get("/api/status")
def api_status():
    with LOCK:
        return dict(STATE)


@app.get("/api/check")
def api_check(hours: float = 24.0, end: str | None = None,
              energy: float | None = None, gain: float | None = None):
    if not 0 < hours <= 24 * 60:
        raise HTTPException(400, "hours must be between 0 and 1440")
    try:
        return run_checks(hours, end, energy, gain, write=False)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/plot.png")
def plot():
    f = CFG["out"] / "monitor_latest.png"
    if not f.exists():
        raise HTTPException(404, "no plot yet")
    return FileResponse(f, media_type="image/png")


@app.get("/api/history")
def history(last: int = 200):
    f = CFG["out"] / "monitor_log.csv"
    if not f.exists():
        return []
    rows = list(csv.reader(open(f)))
    head, body = rows[0], rows[1:]
    return [dict(zip(head, r)) for r in body[-last:]]


# ---------------------------------------------------------------- page
# What each check means and what to look at when it is not OK (shown on
# the page for beamline users).
HELP = {
    "counters now": ("ic1, ic2 and the beam-stop diode as SPEC shows them "
                     "(counts per 0.1 s count time), median of the last "
                     "1 min (updated every minute), and the ring current.",
                     ""),
    "experiment status": ("Hutch Closed - Experiment Running when ic2 and "
                          "the beam-stop "
                          "diode both show signal (ic2 > 0.1 V = 1,000 "
                          "counts, diode > 0.05 V = 500 counts per 0.1 s, "
                          "median of the last 1 min, updated every minute). "
                          "Hutch Closed - Beam in "
                          "Hutch, Experiment NOT Running: beam on ic1 "
                          "(> 2,000 counts), none on ic2 and the diode. "
                          "Hutch Open - No Beam: ic1 about 0 (e.g. 60 "
                          "counts) with the ring on. Check Beam Motion: ic1 "
                          "jumps between beam and no beam (e.g. 60 <-> "
                          "19,000 counts) 4 or more times in 15 min.",
                          "Beam moving or shutter/feedback cycling: check "
                          "mostab, the DCM and the shutter."),
    "ring current": ("Stored beam in CESR.",
                     "No beam or a dump: check the CESR status."),
    "DCM chamber": ("Vacuum in the monochromator chamber; rises a few "
                    "times when the beam comes on (gas load).",
                    "Sustained rise: tell the staff (pumps, leak)."),
    "4B mirror chamber": ("Vacuum in the mirror chamber.",
                          "Sustained rise: tell the staff."),
    "DCM roughing line": ("Backing (roughing) pump line of the DCM turbo "
                          "pump; normal 0.0065 Torr.",
                          "Rising or no data: roughing pump problem "
                          "(as on 13-14 Nov 2026); tell the staff."),
    "ic1 per mA, scatter": ("How steady the beam into ic1 is within an "
                            "hour (normalised to ring current).",
                            "Unsteady: check mostab, refills, the DCM."),
    "ic1 per mA, change": ("Slow change of the beam into ic1 over the "
                           "window.",
                           "Drop: re-align (mond, und_cl); check gains."),
    "ic2/ic1 (beam on the slits)": ("ic1 is before the slits, ic2 after: "
                                    "a change means the beam moved on the "
                                    "slits.",
                                    "Drift: check the beam position, "
                                    "mostab, the table; a ×2-2.5 step is "
                                    "usually an ic2 gain change."),
    "mostab activity": ("How hard the mostab feedback works while the "
                        "shutter is open.",
                        "Busy: the beam is moving; check the DCM and the "
                        "vacuum. 4.52 V = parked (off or no beam)."),
    "crystal-1 mount N": ("Temperature of the first DCM crystal mount.",
                          "Large changes with beam: cooling water, heat "
                          "load."),
    "crystal-1 mount S": ("Temperature of the first DCM crystal mount.",
                          "Large changes with beam: cooling water, heat "
                          "load."),
    "flux": ("Photons/s in ic1 compared with the beamline model (energy "
             "from the latest SPEC scan, ic1 gain from the scan or the "
             "notes).",
             "Far from 1: check the ic1 gain, the alignment, the harmonic, "
             "filters."),
    "Lakeshore": ("Cryostat temperatures (sample, stage 1, stage 2); "
                  "information only.", ""),
}
CARDS = [("Counters now", ["counters now"]),
         ("Beam", ["ring current", "ic1 per mA, scatter",
                   "ic1 per mA, change"]),
         ("Vacuum", ["DCM chamber", "4B mirror chamber",
                     "DCM roughing line"]),
         ("Beam position & mostab", ["ic2/ic1 (beam on the slits)",
                                     "mostab activity"]),
         ("DCM crystal", ["crystal-1 mount N", "crystal-1 mount S"]),
         ("Flux vs model", ["flux"]),
         ("Cryostat", ["Lakeshore"])]
ICON = {"OK": "✓", "WARN": "!", "ALERT": "✕", "INFO": "i", "n/a": "–"}
SENTENCE = {"OK": "Everything monitored is normal.",
            "WARN": "Something needs a look (see the yellow items).",
            "ALERT": "Something is wrong (see the red items); tell the "
                     "beamline staff.",
            "starting": "Reading the CHESS Signals archive for the first "
                        "time (about 1-2 minutes)."}
RANK = {"ALERT": 3, "WARN": 2, "OK": 1, "INFO": 0, "n/a": 0}


def _key(check):
    """Help/card key of a check name."""
    if check.startswith("Lakeshore"):
        return "Lakeshore"
    if "current model" in check:
        return "flux"
    return check


def _ago(iso):
    try:
        m = (dt.datetime.now() - dt.datetime.fromisoformat(iso)
             ).total_seconds() / 60
    except (TypeError, ValueError):
        return "—"
    return "just now" if m < 1 else f"{m:.0f} min ago" if m < 90 else \
        f"{m / 60:.1f} h ago"


def _chip(status):
    s = status if status in ICON else "n/a"
    return (f"<span class='chip s-{s.replace('/', '')}'><b>{ICON[s]}</b> "
            f"{html.escape(status)}</span>")


@app.get("/", response_class=HTMLResponse)
def page():
    with LOCK:
        st = dict(STATE)
    checks = st["checks"]
    starting = not checks
    overall = st["overall"] if not starting else "starting"
    by_key = {}
    for c in checks:
        by_key.setdefault(_key(c["check"]), []).append(c)

    cards = []
    for title, keys in CARDS:
        items = [c for k in keys for c in by_key.get(k, [])]
        if not items:
            continue
        worst = max(items, key=lambda c: RANK.get(c["status"], 0))["status"]
        if title == "Counters now" and items:
            parts = items[0]["value"].replace(" counts per 0.1 s", "")
            lines = "".join(
                f"<li><span>{html.escape(x.split(' ', 1)[0])}</span><span "
                f"class='v'>{html.escape(x.split(' ', 1)[1])}</span></li>"
                for x in parts.split(" · ")) + \
                "<li><span class='muted'>counts per 0.1 s, last 1 min" \
                "</span><span></span></li>"
        else:
            lines = "".join(
                f"<li><span>{html.escape(c['check'])}</span>"
                f"<span class='v'>{html.escape(c['value'].split(';')[0])}"
                f"</span></li>" for c in items)
        cards.append(f"<section class='card'><header><h3>{title}</h3>"
                     f"{_chip(worst)}</header><ul>{lines}</ul></section>")

    rows = []
    for c in checks:
        meaning, action = HELP.get(_key(c["check"]), ("", ""))
        todo = action if c["status"] in ("WARN", "ALERT") else ""
        rows.append(
            f"<tr class='r-{c['status'].replace('/', '')}'><td>"
            f"{_chip(c['status'])}</td><td><b>{html.escape(c['check'])}</b>"
            f"<div class='muted'>{html.escape(meaning)}</div></td><td>"
            f"{html.escape(c['value'])}"
            + (f"<div class='todo'>→ {html.escape(todo)}</div>" if todo
               else "") + "</td></tr>")
    table = ("<table><thead><tr><th>Status</th><th>Check</th><th>Value"
             "</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
             if rows else "")

    hist = history(48)
    strip = "".join(
        f"<span class='tick s-{h.get('overall', 'n/a')}' title='"
        f"{html.escape(h.get('window_end', ''))}: "
        f"{html.escape(h.get('overall', ''))}'></span>" for h in hist)
    err = (f"<div class='err'>Last update failed: "
           f"{html.escape(st['error'])}. Showing the previous status.</div>"
           if st.get("error") else "")
    win = " → ".join(w.replace("T", " ") for w in st["window"]) \
        if st.get("window") else "—"
    exp = next((c["value"] for c in checks
                if c["check"] == "experiment status"), "")
    exp_state = exp.split(" (")[0] if exp else ""
    # beam-status banner colours (user, 15 Nov 2026): experiment running
    # dark green, hutch closed + beam normal green, hutch open orange,
    # no beam in CESR red
    lo = exp_state.lower()
    exp_cls = ("x-motion" if lo.startswith("check beam motion") else
               "x-run" if lo.endswith("experiment running") else
               "x-closed" if lo.startswith("hutch closed") else
               "x-open" if lo.startswith("hutch open") else
               "x-nobeam" if lo.startswith("no beam") else "x-unknown")
    cnt = next((c["value"] for c in checks
                if c["check"] == "counters now"), "")
    exp_html = (f"<div class='exp {exp_cls}'><span class='lbl'>BEAM STATUS"
                f"</span><span class='dot'></span><b>{html.escape(exp_state)}"
                f"</b><span class='counts'>{html.escape(cnt)}</span></div>"
                if exp else "")
    reload_s = 10 if starting else 60
    plot = ("" if starting else
            f"<h2>Last {CFG['hours']:g} h</h2><figure><img src='/plot.png?t="
            f"{time.time():.0f}' alt='Vacuum, roughing line, ic1, ic2/ic1, "
            f"mostab and cryostat over the window'><figcaption>Dashed lines: "
            f"warning (yellow) and alarm (red) limits. Gaps: shutter closed "
            f"or no beam.</figcaption></figure>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{reload_s}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QM2 beamline status</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#1f2328;--muted:#5b6370;--line:#e3e2dc;
--ok:#1b7f3b;--okbg:#e6f4ea;--warn:#8a5a00;--warnbg:#fff3d6;--alert:#b3261e;
--alertbg:#fde7e6;--info:#1f5fae;--infobg:#e7f0fb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--card:#1f2228;
--ink:#e8e8e6;--muted:#a3a8b1;--line:#30343b;--ok:#6fd38f;--okbg:#1d3326;
--warn:#f0c46a;--warnbg:#3a311c;--alert:#ff8a80;--alertbg:#3d2120;
--info:#8cb8ff;--infobg:#1d2a3d}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1150px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:0}} h2{{font-size:16px;margin:28px 0 8px}}
h3{{font-size:15px;margin:0}}
.banner{{border-radius:12px;padding:12px 18px;margin:12px 0 16px;
display:flex;gap:14px;align-items:center;flex-wrap:wrap;
background:var(--card);border:1px solid var(--line);border-left:10px solid}}
.banner .lbl{{font-size:12px;font-weight:700;letter-spacing:1px;
color:var(--muted);border:1px solid var(--line);border-radius:999px;
padding:2px 9px}}
.banner .big{{font-size:22px;font-weight:800;letter-spacing:.5px}}
.b-OK{{border-left-color:#1f6fd1}} .b-OK .big{{color:#1f6fd1}}
.b-WARN{{border-left-color:#e0a92b}} .b-WARN .big{{color:var(--warn)}}
.b-ALERT{{border-left-color:#7b1fa2}} .b-ALERT .big{{color:#7b1fa2}}
@media (prefers-color-scheme:dark){{.b-ALERT .big{{color:#c58af0}}
.b-OK .big{{color:#6aa8ff}}}}
.b-starting{{border-left-color:var(--info)}} .b-starting .big{{color:var(--info)}}
.meta{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:12px 14px}}
.card header{{display:flex;justify-content:space-between;align-items:center;
margin-bottom:6px}}
.card ul{{list-style:none;margin:0;padding:0;font-size:13px}}
.card li{{display:flex;justify-content:space-between;gap:8px;padding:3px 0;
border-top:1px solid var(--line)}}
.card li .v{{text-align:right;color:var(--muted)}}
.chip{{display:inline-flex;gap:5px;align-items:center;border-radius:999px;
padding:1px 9px;font-size:12px;font-weight:700;white-space:nowrap}}
.s-OK{{background:var(--okbg);color:var(--ok)}}
.s-WARN{{background:var(--warnbg);color:var(--warn)}}
.s-ALERT{{background:var(--alertbg);color:var(--alert)}}
.s-INFO,.s-na{{background:var(--infobg);color:var(--info)}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden}}
th,td{{text-align:left;vertical-align:top;padding:8px 10px;
border-top:1px solid var(--line);font-size:14px}}
th{{font-size:12px;color:var(--muted);text-transform:uppercase;
letter-spacing:.4px}}
.muted{{color:var(--muted);font-size:12.5px}}
.todo{{margin-top:4px;font-size:12.5px;font-weight:600}}
.r-WARN .todo{{color:var(--warn)}} .r-ALERT .todo{{color:var(--alert)}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:8px}}
img{{max-width:100%;height:auto;display:block;background:#fff;
border-radius:8px}}
figcaption{{color:var(--muted);font-size:12.5px;padding:6px 4px 0}}
.strip{{display:flex;gap:2px;flex-wrap:wrap}}
.tick{{width:12px;height:22px;border-radius:3px}}
.tick.s-OK{{background:var(--ok)}} .tick.s-WARN{{background:#e0a92b}}
.tick.s-ALERT{{background:var(--alert)}}
.err{{background:var(--alertbg);color:var(--alert);border-radius:8px;
padding:8px 12px;margin-bottom:12px}}
.exp{{display:flex;gap:12px 14px;align-items:center;flex-wrap:wrap;
border-radius:14px;padding:18px 22px;margin:12px 0;font-size:24px;
color:#fff;background:#5b6370;box-shadow:0 2px 6px rgba(0,0,0,.15)}}
.exp b{{font-weight:800}}
.exp .lbl{{font-size:12px;font-weight:700;letter-spacing:1px;opacity:.85;
border:1px solid rgba(255,255,255,.6);border-radius:999px;padding:2px 9px}}
.exp .dot{{width:16px;height:16px;border-radius:50%;background:#fff;
box-shadow:0 0 0 4px rgba(255,255,255,.3)}}
.exp .counts{{font-variant-numeric:tabular-nums;font-size:15px;opacity:.95;
flex-basis:100%}}
.x-run{{background:#0f5a2c}}
.x-closed{{background:#2e9e4f}}
.x-open{{background:#d35f00}}
.x-nobeam{{background:#c62828}}
.x-motion{{background:#7a0c0c}}
.x-unknown{{background:#5b6370}}
footer{{color:var(--muted);font-size:12px;margin:28px 0 8px;
border-top:1px solid var(--line);padding-top:10px}}
@media (max-width:600px){{.banner .big{{font-size:22px}}
th:nth-child(1),td:nth-child(1){{width:70px}}}}
</style></head><body><main>
<h1>QM2 (CHESS ID4B) beamline status</h1>
{exp_html}
<div class="banner b-{overall}"><span class="lbl">BEAMLINE HEALTH</span><span class="big">{html.escape(overall.upper())}
</span><span>{SENTENCE.get(overall, '')}</span></div>
<div class="meta">Beam status updated {_ago(st.get('beam_updated') or st.get('updated'))}
(every {CFG['fast']:g} min) · other checks updated {_ago(st.get('updated'))}
(every {CFG['refresh']:g} min) · window {win} · page refreshes every minute</div>
{err}
<h2>Overview</h2><div class="grid">{''.join(cards)}</div>
<h2>Details</h2>{table}
{plot}
<h2>History</h2><div class="strip">{strip or
'<span class="meta">No runs logged yet.</span>'}</div>
<div class="meta" style="margin-top:6px">One bar per update (oldest left);
green OK, yellow WARN, red ALERT. Hover for the time.</div>
<footer>Read only: values come from the CHESS Signals archive; nothing is
sent to the beamline. Limits are set from normal 2026-3 operation.
Questions about a warning: ask the QM2 beamline staff.
Data for scripts: <a href="/api/status">/api/status</a> ·
<a href="/api/history">/api/history</a> ·
/api/check?hours=48</footer>
</main></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--refresh", type=float, default=10.0,
                    help="minutes between full archive reads")
    ap.add_argument("--fast", type=float, default=1.0,
                    help="minutes between beam-status/counter updates")
    ap.add_argument("--energy", type=float, default=None)
    ap.add_argument("--gain", type=float, default=None)
    ap.add_argument("--spec-dir", type=Path, default=bm.SPEC_DIR)
    ap.add_argument("--no-flux", action="store_true")
    ap.add_argument("--out", type=Path, default=CFG["out"],
                    help="folder for status/plot/log (must be writable; "
                         "used by the Apptainer image)")
    ap.add_argument("--flux-table", type=Path, default=None,
                    help="flux lookup table (ml/make_flux_table.py) instead "
                         "of the full twin")
    a = ap.parse_args()
    if a.flux_table:
        bm.FLUX_TABLE = a.flux_table
    CFG.update(hours=a.hours, refresh=a.refresh, fast=a.fast,
               energy=a.energy,
               gain=a.gain, spec_dir=a.spec_dir, no_flux=a.no_flux,
               out=a.out)
    threading.Thread(target=loop, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
