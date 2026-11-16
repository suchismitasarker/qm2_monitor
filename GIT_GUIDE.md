# Git and GitHub for qm2_monitor: a short guide

Git keeps the history of this folder: every saved version of the files, who
saved it and why. GitHub keeps a copy of that history online. GitHub
Actions runs the tests and builds the image every time new work arrives.

```
 your Mac                        GitHub (website)                    lnx201
 ────────                        ────────────────                    ──────
 qm2_monitor/  ── git push ──▶   suchismitasarker/qm2_monitor  ◀── git clone / git pull
 (edit here)                       ├─ every saved version (commits)
                                   └─ Actions: tests → .sif → Trivy → ghcr.io
```

## 1. Words

| Word | Meaning |
|---|---|
| repository (repo) | a folder whose history git tracks (history is in the hidden `.git/` folder) |
| commit | a saved snapshot of the files, with a message saying why |
| branch | a line of commits; ours is `main` |
| remote / origin | the online copy: `https://github.com/suchismitasarker/qm2_monitor.git` |
| push | send your new commits from the Mac to GitHub |
| clone / pull | get the repository from GitHub (clone = first time, pull = updates) |
| tag | a name for one commit, used for releases (`v1.0.0`) |

Three separate steps: **edit** the files → **commit** (snapshot, on your
Mac only) → **push** (send to GitHub). Until you push, GitHub sees nothing.

## 2. Everyday work (on the Mac, inside qm2_monitor)

```bash
git status                      # what changed? always safe, run it often
git diff                        # the exact changed lines
git add .                       # include NEW files in the next commit
git commit -am "what and why"   # snapshot of all changed tracked files
git push                        # to GitHub -> tests + image build start
git log --oneline               # list of past commits
```

After `git push`, watch https://github.com/suchismitasarker/qm2_monitor/actions
(green ✓ = passed, red ✗ = failed; click a run for the error and downloads),
or in the terminal:

```bash
gh run list -R suchismitasarker/qm2_monitor          # recent runs
gh run view <run-id> -R suchismitasarker/qm2_monitor --log-failed   # red lines
```

## 3. On lnx201

```bash
cd /nfs/chess/id4baux/suchi/2026/nxrefine_data_analysis
git clone https://github.com/suchismitasarker/qm2_monitor.git   # first time only
cd qm2_monitor
git pull                                   # later: get the newest version
bash run.sh stop; bash build.sh && bash run.sh start
```

The repository is private: lnx201 needs a GitHub sign-in for clone/pull
(`gh auth login`, or a token with `repo` scope typed at the password prompt).

## 4. Releases

```bash
git tag v1.0.1
git push origin v1.0.1
```

GitHub then makes a Release page with the `.sif` and a source archive
attached (`release.yml`).

## 5. What runs on GitHub (.github/workflows/)

| File | When | What |
|---|---|---|
| `ci.yml` | every push | lint + 14 offline tests, Python 3.11 and 3.12 |
| `containers.yml` | every push, and every Monday 06:00 UTC | build `qm2monitor.sif` + its tests, smoke test (page loads), Trivy vulnerability scan (report only), save `.sif` + report as downloads, push the image to `oras://ghcr.io/suchismitasarker/qm2monitor` |
| `release.yml` | a pushed tag `v*.*.*` | GitHub Release with the `.sif` |

The Monday run rebuilds with the newest Debian base image, so security
fixes arrive without you changing anything. The Trivy summary is on each
run's page; the full report is the `trivy-report` download.

## 6. Signing in (done once on the Mac)

```bash
brew install gh
gh auth login          # GitHub.com -> HTTPS -> Yes -> Login with a web browser
                       # copy the 8-character code shown in the terminal,
                       # paste it on github.com/login/device, Authorize
gh auth setup-git      # make git use this sign-in (no password prompts)
gh auth status         # check: scopes must include 'repo' and 'workflow'
```

Never paste a token into a chat, a file or a commit. If one leaks, delete
it on GitHub: Settings -> Developer settings -> Personal access tokens.

## 7. Rules of thumb

- Small commits with clear messages: you can always see what changed when.
- Never commit data, passwords or tokens; `.gitignore` keeps out `*.sif`,
  `trivy_cache/`, `qm2monitor_sandbox/` and the Trivy reports.
- Unsure? `git status` first; it never changes anything.
- A rejected push or a red run: read the message, it usually says what is
  wrong (missing permission, a failing test, a network hiccup -> re-run).

## 8. Problems we met (and the fixes)

| Message | Cause | Fix |
|---|---|---|
| `refusing to allow a Personal Access Token ... without workflow scope` | token without `workflow` permission | sign in with `gh auth login` |
| `Invalid username or token. Password authentication is not supported` | git used the keychain, not gh | `gh auth setup-git` |
| `No file matched to **/requirements.txt` | pip cache looked for requirements.txt | `cache-dependency-path: requirements.lock` |
| `Failed to create container process: Operation not permitted` | Ubuntu 24.04 runner blocks user namespaces | `sysctl kernel.apparmor_restrict_unprivileged_userns=0` in the workflow |
| `invalid literal for int() with base 16` while installing Apptainer | temporary download error | re-run the job |
