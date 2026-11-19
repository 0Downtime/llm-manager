# CI/CD

`.github/workflows/ci-cd.yml`:

| Event | What runs |
|---|---|
| Pull request into `main` | **test** on GitHub's runner: compile, `python -m unittest`, shell syntax, and a smoke test that starts the server and calls the UI and `/api/state` |
| Push to `main` (a merged PR) | **test**, then **deploy** to the container |
| Run workflow, `deploy` ticked | the same deploy for `main` without a new commit |

## Deploy

The deploy job runs on a self-hosted runner **inside the llm-manager container** (label `llm-manager`,
user `ghrunner`). It checks out the commit, stamps a `.release` file and runs

```
sudo -n /usr/local/sbin/llm-manager-deploy "$GITHUB_WORKSPACE"
```

That is the runner's only root access (`/etc/sudoers.d/llm-manager-ci`). `ci/llm-manager-deploy`:

1. copies the app files to a staging folder and compile-checks them (no new code runs as root);
2. updates Python packages if `requirements.txt` changed, wheels only;
3. backs up the running version to `/opt/llm-manager/.prev`, installs the new one, restarts the service;
4. waits for `GET /login` to answer 200, and **rolls back** to `.prev` if it doesn't within 20 s.

It never touches `/etc/llm-manager/env`, the state dir (password, sessions, HF token, job list), the systemd
unit or the models share. A restart pauses a running download; it resumes from its partial files.
New app files must be added to `FILES` in `ci/llm-manager-deploy` and `APP_FILES` in `deploy.sh`.

The repo is public, so the self-hosted runner only ever gets pushes to `main` and manual runs from this
repo, never pull requests (from forks or not).

## Setting up the runner

From a workstation with root SSH to the Proxmox node and `gh` signed in with admin on the repo:

```bash
PXE_HOST=<proxmox node> CTID=<container id> ci/setup-runner.sh          # install, or re-register
PXE_HOST=<proxmox node> CTID=<container id> ci/setup-runner.sh remove   # uninstall
```

It installs `sudo` and `git` in the container, creates `ghrunner`, installs the deploy helper and the
latest Actions runner (sha256-checked against the release notes) as a systemd service.

`deploy.sh` still works for a first install or a manual deploy, and installs the deploy helper too.
