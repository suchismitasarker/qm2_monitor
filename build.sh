#!/bin/bash
# Build the QM2 monitor image in this folder (Linux + Apptainer >= 1.1):
#   bash build.sh            -> qm2monitor.sif (runs the image's tests)
set -euo pipefail
cd "$(dirname "$0")"                   # paths in qm2monitor.def are relative to here
if [ "$(id -u)" = "0" ]; then
  apptainer build --force qm2monitor.sif qm2monitor.def
else
  apptainer build --fakeroot --force qm2monitor.sif qm2monitor.def
fi
apptainer test qm2monitor.sif
echo "Built $(pwd)/qm2monitor.sif"
