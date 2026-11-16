#!/bin/bash
# Vulnerability scan of the monitor image with Trivy (run on lnx201, in
# this folder, after bash build.sh). Trivy runs through Apptainer: nothing
# to install. Needs internet the first time (Trivy image + database ~60 MB).
#   bash scan.sh               HIGH and CRITICAL only
#   bash scan.sh all           every severity
# Report: trivy_report.txt (whole image: Debian + installed Python packages)
set -euo pipefail
cd "$(dirname "$0")"
TRIVY="docker://aquasec/trivy:0.58.1"
# cache in this folder (bound into the container): ~/.cache can be a link
# to a disk the container cannot see ("mkdir ~/.cache: file exists")
CACHE="${TRIVY_CACHE:-$PWD/trivy_cache}"
SEV="HIGH,CRITICAL"; [ "${1:-}" = "all" ] && SEV="UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL"
[ -f qm2monitor.sif ] || { echo "qm2monitor.sif not found: run bash build.sh first"; exit 1; }
mkdir -p "$CACHE"
# use the host's trusted certificates inside the Trivy container (the
# network's HTTPS inspection CA is trusted by lnx201, not by the container)
if [ -f /etc/pki/tls/certs/ca-bundle.crt ]; then          # RHEL / Alma (lnx201)
  CERT=(--bind /etc/pki:/etc/pki --env SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt)
elif [ -f /etc/ssl/certs/ca-certificates.crt ]; then     # Debian / Ubuntu
  CERT=(--bind /etc/ssl:/etc/ssl --env SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt)
else
  CERT=()
fi
echo "== host certificates: ${CERT[3]:-none found}"

echo "== 1/2 unpack the image to qm2monitor_sandbox/ (about 1 min)"
[ -d qm2monitor_sandbox ] || apptainer build --sandbox qm2monitor_sandbox qm2monitor.sif

echo "== 2/2 scan (severity $SEV)"
apptainer run --bind "$PWD" ${CERT[@]+"${CERT[@]}"} "$TRIVY" rootfs --scanners vuln \
    --cache-dir "$CACHE" --severity "$SEV" \
    --format table -o trivy_report.txt qm2monitor_sandbox

echo; echo "Summary:"
grep -E "^Total" trivy_report.txt || echo "  no findings at $SEV"
echo; echo "Report: $PWD/trivy_report.txt"
echo "Can be removed when done: qm2monitor_sandbox/ (~400 MB); trivy_cache/"
echo "(~140 MB database, re-downloaded when older than a day)."
