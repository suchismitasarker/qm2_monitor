#!/bin/bash
# Start / stop / status of the monitor as a background Apptainer instance:
#   bash run.sh start [port]   bash run.sh stop   bash run.sh status
# Flux check: QM2_ENERGY=45 QM2_GAIN=2e-8 bash run.sh start
#         or: QM2_SPEC_DIR=/path/to/spec bash run.sh start (read-only bind)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SIF="${QM2_SIF:-$HERE/qm2monitor.sif}"
NAME=qm2mon
case "${1:-start}" in
  start)
    export QM2_PORT="${2:-${QM2_PORT:-8051}}"
    export QM2_OUT="${QM2_OUT:-$HOME/qm2monitor_results}"
    if apptainer instance list | grep -q "^$NAME "; then
      echo "Already running"; exit 0; fi
    BIND=()
    [ -n "${QM2_SPEC_DIR:-}" ] && BIND=(--bind "$QM2_SPEC_DIR:$QM2_SPEC_DIR:ro")
    apptainer instance start ${BIND[@]+"${BIND[@]}"} "$SIF" "$NAME"
    echo "Started on port $QM2_PORT; results in $QM2_OUT"
    echo "From your Mac: ssh -N -L $QM2_PORT:localhost:$QM2_PORT $(hostname)"
    ;;
  stop)   apptainer instance stop "$NAME" ;;
  status) apptainer instance list; curl -s "localhost:${QM2_PORT:-8051}/api/status" | head -c 300; echo ;;
  *) echo "usage: bash run.sh start [port] | stop | status"; exit 1 ;;
esac
