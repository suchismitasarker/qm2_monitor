# qm2_monitor: QM2 beamline monitor as an Apptainer image (lnx201)

Read-only status page from the CHESS Signals archive (beam status, counters,
vacuum, mostab, DCM temperature, Lakeshore). Flux-vs-model check is off.

## Files (copy this whole folder, nothing else)
    qm2monitor.def               image recipe (python 3.11 + 5 packages)
    ml/beamline_monitor.py       checks
    ml/monitor_api.py            web page
    requirements.lock            numpy, pandas, matplotlib, fastapi, uvicorn
    tests/test_monitor_offline.py  10 tests, run during the build
    build.sh                     build qm2monitor.sif
    run.sh                       start [port] | stop | status

## 1. Copy (on your Mac)
    scp -r qm2_monitor lnx201:~/

## 2. Build once (on lnx201; needs internet during the build)
    cd ~/qm2_monitor
    apptainer --version
    bash build.sh

## 3. Run (on lnx201)
    bash run.sh start          # background, port 8051, results ~/qm2monitor_results
    bash run.sh status
    bash run.sh stop
Other port: `bash run.sh start 8055`.

## 4. Open (on your Mac)
    ssh -N -L 8051:localhost:8051 lnx201
    open http://localhost:8051      # first fill takes 1-2 minutes

## If something fails
- `apptainer: command not found` or a fakeroot error: ask CLASSE IT
  (Apptainer / permission to build images).
- Page empty: `curl -sI http://signals.chess.cornell.edu` must answer.
- Sharing without a tunnel: `QM2_HOST=0.0.0.0 bash run.sh start` and an
  open firewall port; ask CLASSE IT first (no password on the page).

## Update the monitor later
Copy the new `ml/beamline_monitor.py` / `ml/monitor_api.py` into `ml/`,
then `bash run.sh stop && bash build.sh && bash run.sh start`.

## CI/CD (GitHub Actions, .github/workflows/)
    ci.yml          every push/PR: lint (syntax) + offline tests, Python 3.11/3.12
    containers.yml  push to main / tag / PR: build qm2monitor.sif, apptainer test,
                    smoke test (page serves), artifact 14 days; push to
                    oras://ghcr.io/<owner>/qm2monitor:<branch|tag> (+ :latest on main)
    release.yml     tag v1.0.0 ...: GitHub Release with the .sif + source tarball
    (containers.yml also runs a Trivy scan of the image: job summary +
     artifact trivy-report, never fails the build; and every Monday 06:00 UTC)

First time (no repository yet):
1. github.com -> New repository -> name `qm2_monitor`, Private, no README.
2. Here (this folder is already `git init`-ed, branch main):

       git add .
       git commit -m "QM2 beamline monitor"
       git remote add origin https://github.com/<you>/qm2_monitor.git
       git push -u origin main

3. GitHub -> Actions tab: CI and Containers run. Release: 
       git tag v1.0.0 && git push origin v1.0.0
4. lnx201 (private image needs a token with read:packages):

       apptainer registry login -u <you> oras://ghcr.io
       apptainer pull qm2monitor.sif oras://ghcr.io/<you>/qm2monitor:latest
       bash run.sh start

Before the first push: ml/flux_table.json must exist (CI checks it; the
image build needs it): `PYTHONPATH=. python ml/make_flux_table.py` in
autoalignment-4b, then copy ml/results/flux_table.json to ml/ here.
GitHub runners cannot reach signals.chess.cornell.edu: the tests and the
smoke test do not need it; real data is read only on lnx201.

## Vulnerability scan (Trivy, on lnx201)
    bash scan.sh          # HIGH + CRITICAL; bash scan.sh all = every severity
Runs Trivy through Apptainer (nothing to install; internet the first
time). Writes trivy_report.txt (whole image: Debian + installed Python
packages). Fix: rebuild (newest base image) or
raise a version in requirements.lock. Remove qm2monitor_sandbox/ when done.
