#!/usr/bin/env bash
# Install (or remove) the GitHub Actions runner that deploys llm-manager, inside its Proxmox container.
# Run from your workstation; needs root SSH to the Proxmox node and `gh` signed in with admin on the repo.
#
#   PXE_HOST=<node> CTID=<ctid> ci/setup-runner.sh          # install / re-register
#   PXE_HOST=<node> CTID=<ctid> ci/setup-runner.sh remove   # unregister and uninstall
#
# The runner runs as an unprivileged `ghrunner` user with label `llm-manager`. Its only root access is
# `sudo /usr/local/sbin/llm-manager-deploy <dir>` (ci/llm-manager-deploy), which installs the app code
# and rolls back if the service doesn't come up. The repo is public: the workflow only sends pushes to
# main and manual runs to this runner, never pull requests. docs/CI.md
set -euo pipefail
PXE_HOST=${PXE_HOST:?set PXE_HOST to the Proxmox node (ssh host)}
CTID=${CTID:?set CTID to the container id}
REPO=${REPO:-0Downtime/llm-manager}
NAME=${RUNNER_NAME:-llm-manager-ct$CTID}
cd "$(dirname "$0")/.."

ct() { ssh "$PXE_HOST" "pct exec $CTID -- bash -s"; }

if [[ ${1:-} == remove ]]; then
  T=$(gh api -X POST "repos/$REPO/actions/runners/remove-token" --jq .token)
  printf 'T=%q\n%s\n' "$T" '
set -e; cd /home/ghrunner/runner 2>/dev/null || exit 0
./svc.sh stop || true; ./svc.sh uninstall || true
runuser -u ghrunner -- ./config.sh remove --token "$T" || true
rm -f /etc/sudoers.d/llm-manager-ci' | ct
  echo "==> runner removed"; exit 0
fi

# latest runner release, with the sha256 GitHub publishes in its release notes
REL=$(gh api repos/actions/runner/releases/latest)
VER=$(jq -r .tag_name <<<"$REL" | sed 's/^v//')
SHA=$(jq -r .body <<<"$REL" | grep -Eo 'BEGIN SHA linux-x64 -->[0-9a-f]{64}' | head -1 | grep -Eo '[0-9a-f]{64}$' || true)
[[ $SHA =~ ^[0-9a-f]{64}$ ]] || { echo "could not read the runner's sha256 from the v$VER release notes" >&2; exit 1; }
T=$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)
echo "==> runner v$VER into CT $CTID on $PXE_HOST as $NAME"

# the deploy helper first (root-owned, not writable by the runner)
ssh "$PXE_HOST" "pct exec $CTID -- sh -c 'cat > /usr/local/sbin/.llm-manager-deploy.new && chown root:root /usr/local/sbin/.llm-manager-deploy.new && chmod 755 /usr/local/sbin/.llm-manager-deploy.new && mv /usr/local/sbin/.llm-manager-deploy.new /usr/local/sbin/llm-manager-deploy'" < ci/llm-manager-deploy

printf 'T=%q VER=%q SHA=%q REPO=%q NAME=%q\n%s\n' "$T" "$VER" "$SHA" "$REPO" "$NAME" '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
apt-get update -qq && apt-get install -y -qq sudo git curl ca-certificates >/dev/null
id ghrunner >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash ghrunner
echo "ghrunner ALL=(root) NOPASSWD: /usr/local/sbin/llm-manager-deploy" > /etc/sudoers.d/llm-manager-ci
chmod 440 /etc/sudoers.d/llm-manager-ci; visudo -cq
D=/home/ghrunner/runner
if [ -x $D/svc.sh ] && [ -f $D/.runner ]; then (cd $D && ./svc.sh stop >/dev/null 2>&1; ./svc.sh uninstall >/dev/null 2>&1; runuser -u ghrunner -- ./config.sh remove --token "$T" >/dev/null 2>&1 || true); fi
rm -rf $D; runuser -u ghrunner -- mkdir -p $D; cd $D
F=actions-runner-linux-x64-$VER.tar.gz
runuser -u ghrunner -- curl -fsSLo $F https://github.com/actions/runner/releases/download/v$VER/$F
echo "$SHA  $F" | sha256sum -c --quiet
runuser -u ghrunner -- tar xzf $F; rm -f $F
./bin/installdependencies.sh >/dev/null
runuser -u ghrunner -- ./config.sh --unattended --replace --url https://github.com/$REPO --token "$T" --name "$NAME" --labels llm-manager --work _work
./svc.sh install ghrunner >/dev/null && ./svc.sh start >/dev/null
./svc.sh status | grep -E "Active:" || true' | ct
echo "==> done: the runner shows up under Settings > Actions > Runners with label llm-manager"
