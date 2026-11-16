#!/bin/bash
# Started inside the image by %runscript / %startscript. Settings from the
# environment (all optional):
#   QM2_PORT 8051 | QM2_HOST 127.0.0.1 | QM2_OUT ~/qm2monitor_results
#   flux check (light version, lookup table ml/flux_table.json):
#   QM2_ENERGY keV and QM2_GAIN ic1 A/V (e.g. 45 and 2e-8); or only
#   QM2_ENERGY (gain from the notes table); or QM2_SPEC_DIR = folder with
#   <cycle>/<SPEC file> (energy from the newest scan); QM2_NO_FLUX=1 = off
OUT="${QM2_OUT:-$HOME/qm2monitor_results}"
mkdir -p "$OUT"
ARGS=(--port "${QM2_PORT:-8051}" --host "${QM2_HOST:-127.0.0.1}" --out "$OUT")
[ -n "${QM2_ENERGY:-}" ] && ARGS+=(--energy "$QM2_ENERGY")
[ -n "${QM2_GAIN:-}" ] && ARGS+=(--gain "$QM2_GAIN")
[ -n "${QM2_SPEC_DIR:-}" ] && ARGS+=(--spec-dir "$QM2_SPEC_DIR")
[ -n "${QM2_NO_FLUX:-}" ] && ARGS+=(--no-flux)
# image built without ml/flux_table.json: no flux check (the full twin is
# not in the image)
[ -f /opt/qm2monitor/ml/flux_table.json ] || ARGS+=(--no-flux)
exec python /opt/qm2monitor/ml/monitor_api.py "${ARGS[@]}" "$@"
