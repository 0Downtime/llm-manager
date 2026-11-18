#!/usr/bin/env bash
# Deploy llm-manager into a Proxmox LXC from this folder.
#
#   PXE_HOST=<ssh host of the Proxmox node> CTID=<container id> ./deploy.sh            # update code + restart
#   PXE_HOST=... CTID=... ./deploy.sh --bootstrap                                        # first install (venv + deps)
#
# Needs SSH access as root to the Proxmox node (uses `pct push` / `pct exec`).
# Never copies secrets: /etc/llm-manager/env is created from env.example only if missing.
set -euo pipefail

PXE_HOST=${PXE_HOST:?set PXE_HOST to the Proxmox node (ssh host)}
CTID=${CTID:?set CTID to the container id}
BOOTSTRAP=0
[[ ${1:-} == --bootstrap ]] && BOOTSTRAP=1

cd "$(dirname "$0")"
APP_FILES=(server.py downloader.py hfinfo.py auth.py web/index.html web/login.html)
for f in "${APP_FILES[@]}" llm-manager llm-manager.service env.example; do
  [[ -f $f ]] || { echo "missing $f" >&2; exit 1; }
done
python3 -m py_compile server.py downloader.py hfinfo.py auth.py

echo "==> deploying to CT $CTID on $PXE_HOST"
# 1) upload the files to a temp dir on the Proxmox node, 2) run the install script there
RT=$(ssh "$PXE_HOST" mktemp -d)
COPYFILE_DISABLE=1 tar czf - "${APP_FILES[@]}" llm-manager llm-manager.service env.example |
  ssh "$PXE_HOST" "tar xzf - -C '$RT' 2>/dev/null"
ssh "$PXE_HOST" "CTID=$CTID BOOTSTRAP=$BOOTSTRAP T='$RT' APP_FILES='${APP_FILES[*]}' bash -s" <<'REMOTE'
set -euo pipefail
trap 'rm -rf "$T"' EXIT
cd "$T"
run() { pct exec "$CTID" -- "$@"; }

if [[ $BOOTSTRAP == 1 ]]; then
  echo "==> bootstrap: python venv + huggingface_hub"
  run bash -c 'export DEBIAN_FRONTEND=noninteractive LC_ALL=C; apt-get update -qq && apt-get install -y -qq python3 python3-venv ca-certificates curl >/dev/null'
  run bash -c 'mkdir -p /opt/llm-manager && [ -x /opt/llm-manager/venv/bin/python ] || python3 -m venv /opt/llm-manager/venv'
  run bash -c 'LC_ALL=C /opt/llm-manager/venv/bin/pip install -q --upgrade pip huggingface_hub'
fi

run mkdir -p /opt/llm-manager/web /etc/llm-manager
for f in $APP_FILES; do pct push "$CTID" "$f" "/opt/llm-manager/$f"; done
pct push "$CTID" llm-manager /usr/local/bin/llm-manager --perms 755
run ln -sf /usr/local/bin/llm-manager /usr/sbin/llm-manager  # `pct exec` uses a PATH without /usr/local/bin
pct push "$CTID" llm-manager.service /etc/systemd/system/llm-manager.service
run chmod 700 /etc/llm-manager
if ! run test -e /etc/llm-manager/env; then
  pct push "$CTID" env.example /etc/llm-manager/env --perms 600
  echo "!!  created /etc/llm-manager/env from env.example: edit it (MAC_SMB_URL, CTID, ...) and rerun"
fi
run systemctl daemon-reload
run systemctl enable --now llm-manager >/dev/null 2>&1 || true
run systemctl restart llm-manager
sleep 2
run systemctl is-active llm-manager
run bash -c 'journalctl -u llm-manager -n 3 --no-pager -o cat'
REMOTE
echo "==> done. If no password is set yet:  ssh -t $PXE_HOST pct exec $CTID -- llm-manager passwd"
