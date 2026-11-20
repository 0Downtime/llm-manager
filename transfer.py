"""Send to Mac: copy a model folder from the share to the Mac over SSH, track it, verify it.

Two ways to move the bytes, picked in Settings:

* push: this server runs `rsync` over SSH to the Mac. Nothing to set up on the Mac besides
  authorizing this server's SSH key. Survives network drops (rsync retries and keeps partial
  files); a service restart re-queues the transfer and rsync resumes.
* pull: the Mac runs `rsync` itself, detached with nohup, reading the share from a path where it
  is mounted on the Mac (for example a read-only NFS mount). The copy keeps running if this server
  restarts or loses the connection; the server re-attaches and keeps tracking it.

Either way progress is measured by listing the destination on the Mac, and when rsync finishes
every file's size on the Mac is compared with the share.
"""
import json, os, re, shlex, shutil, signal, subprocess, threading, time, uuid
from pathlib import Path

SSH_BIN = "ssh"            # overridable for tests
SSH_KEYGEN = "ssh-keygen"
RSYNC_BIN = "rsync"
POLL = 3.0                 # seconds between progress checks
PUSH_RETRIES, PUSH_RETRY_WAIT = 3, 15
PULL_RETRIES, PULL_RETRY_WAIT = 5, 30
LOST_AFTER = 300           # pull mode: give up tracking after this long without reaching the Mac
EXCLUDES = (".cache", ".hf-repo")
ACTIVE = ("queued", "starting", "running", "verifying")
TARGETS = ("omlx", "lmstudio", "custom")

HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*|[0-9A-Fa-f:]{2,39})$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
# Mac paths: absolute or under ~, no spaces or shell/rsync specials, no .. components
PATH_RE = re.compile(r"^(?:~|~?/[A-Za-z0-9._@+/-]*)$")
NAME_RE = re.compile(r"^[A-Za-z0-9][\w.+-]*$")
CMD_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

DEFAULTS = {
    "host": "", "user": "", "port": 22, "mode": "push",
    "mac_source": "",                  # pull mode: where the models folder is mounted on the Mac
    "rsync": "rsync",                  # rsync on the Mac (e.g. /opt/homebrew/bin/rsync)
    "omlx_dir": "~/.omlx/models", "lmstudio_dir": "~/.lmstudio/models",
}
ENV = {"host": "MAC_SSH_HOST", "user": "MAC_SSH_USER", "port": "MAC_SSH_PORT", "mode": "MAC_TRANSFER_MODE",
       "mac_source": "MAC_NAS_PATH", "rsync": "MAC_RSYNC", "omlx_dir": "MAC_OMLX_DIR", "lmstudio_dir": "MAC_LMSTUDIO_DIR"}


class TransferError(Exception):
    pass


def check_path(p, what):
    p = (p or "").strip()
    if p != "/":
        p = p.rstrip("/")
    if not p or not PATH_RE.match(p) or any(x == ".." for x in p.split("/")):
        raise ValueError(f"{what} must be an absolute path or start with ~/ (letters, digits and . _ - + @ / only)")
    return p


def validate(cfg):
    """Clean a settings dict; raises ValueError with a readable message."""
    out = {}
    host = str(cfg.get("host") or "").strip()
    if host and not HOST_RE.match(host):
        raise ValueError("Mac host must be a host name or IP address")
    user = str(cfg.get("user") or "").strip()
    if user and not USER_RE.match(user):
        raise ValueError("Mac user name has characters SSH won't accept")
    try:
        port = int(cfg.get("port") or 22)
    except (TypeError, ValueError):
        raise ValueError("SSH port must be a number")
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    mode = cfg.get("mode") or "push"
    if mode not in ("push", "pull"):
        raise ValueError("mode must be push or pull")
    src = str(cfg.get("mac_source") or "").strip()
    src = check_path(src, "Mac path of the models share") if src else ""
    if mode == "pull" and not src:
        raise ValueError("pull mode needs the path where the models share is mounted on the Mac")
    rs = str(cfg.get("rsync") or "rsync").strip()
    if not CMD_RE.match(rs):
        raise ValueError("rsync on the Mac must be a command name or path")
    out.update(host=host, user=user, port=port, mode=mode, mac_source=src, rsync=rs,
               omlx_dir=check_path(str(cfg.get("omlx_dir") or DEFAULTS["omlx_dir"]), "oMLX folder"),
               lmstudio_dir=check_path(str(cfg.get("lmstudio_dir") or DEFAULTS["lmstudio_dir"]), "LM Studio folder"))
    return out


def rq(path):
    """Shell word for a Mac path, keeping a leading ~ expandable."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def absolute(path, home):
    return home + path[1:] if path == "~" or path.startswith("~/") else path


LIST_SCRIPT = (  # "size<TAB>relative path" for every file under $D; GNU find locally, BSD stat on macOS
    'cd "$D" 2>/dev/null || exit 0\n'
    "if find . -maxdepth 0 -printf '' >/dev/null 2>&1; then find . -type f -printf '%s\\t%P\\n' 2>/dev/null\n"
    "else find . -type f -exec stat -f '%z%t%N' {} + 2>/dev/null; fi\n")


def parse_listing(text):
    out = {}
    for line in text.splitlines():
        size, _, path = line.partition("\t")
        if not path or not size.isdigit():
            continue
        out[path[2:] if path.startswith("./") else path] = int(size)
    return out


def excluded(rel):
    return any(p in EXCLUDES for p in rel.split("/"))


def local_files(d):
    """{relative path: size} of what rsync copies from a model folder."""
    d = Path(d)
    out = {}
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in EXCLUDES]
        for f in files:
            if f in EXCLUDES:
                continue
            p = Path(root) / f
            try:
                out[p.relative_to(d).as_posix()] = p.stat().st_size
            except FileNotFoundError:
                pass
    return out


def compare(expected, remote):
    """Per-file size check -> verification summary."""
    problems = []
    for path, size in sorted(expected.items()):
        got = remote.get(path)
        if got != size:
            problems.append({"path": path, "expected": size, "actual": got})
    extra = [p for p in remote if p not in expected and not excluded(p)]
    return {"ok": not problems, "files": len(expected), "bytes": sum(expected.values()),
            "bad": len(problems), "missing": sum(1 for p in problems if p["actual"] is None),
            "problems": problems[:20], "extra": len(extra), "checked": time.time()}


def pull_script(jobdir, src, dest, rsync):
    """Runs detached on the Mac (POSIX sh). Writes pid, log and a final status line."""
    J, S, D = shlex.quote(jobdir), shlex.quote(src), shlex.quote(dest)
    ex = " ".join(f"--exclude {shlex.quote(e)}" for e in EXCLUDES)
    return f"""#!/bin/sh
# written by llm-manager: copies one model from the share to this Mac
J={J}
echo $$ > "$J/pid"
rm -f "$J/status"
child=
trap 'if [ -n "$child" ]; then kill "$child" 2>/dev/null; fi; echo cancelled > "$J/status"; exit 143' TERM INT
if [ ! -d {S} ]; then
  echo "source folder not found on the Mac: "{S}" (is the models share mounted there?)"
  echo "exit nosource" > "$J/status"; exit 2
fi
mkdir -p {D} || {{ echo "exit mkdir" > "$J/status"; exit 1; }}
n=0
while :; do
  n=$((n+1))
  echo "$(date '+%Y-%m-%d %H:%M:%S') rsync attempt $n"
  {shlex.quote(rsync)} -a --partial {ex} {S}/ {D}/ &
  child=$!
  wait "$child"; rc=$?
  child=
  [ "$rc" -eq 0 ] && break
  [ "$n" -ge {PULL_RETRIES} ] && break
  echo "$(date '+%Y-%m-%d %H:%M:%S') rsync exited $rc, retrying in {PULL_RETRY_WAIT}s"
  sleep {PULL_RETRY_WAIT} & child=$!; wait "$child"; child=
done
echo "exit $rc" > "$J/status"
"""


def _atomic_write(path, text):
    """Write via a uniquely named temp file, so two writers never rename each other's file away."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Manager:
    """Persisted queue of transfers (one runs at a time) plus the Mac connection settings."""

    def __init__(self, state_dir, llm_dir, busy=lambda name: False):
        self.state_dir, self.llm_dir, self.busy = Path(state_dir), Path(llm_dir), busy
        self.file = self.state_dir / "transfers.json"
        self.cfg_file = self.state_dir / "mac.json"
        self.ssh_dir = self.state_dir / "ssh"
        self.key = Path(os.environ.get("MAC_SSH_KEY") or self.ssh_dir / "id_ed25519")
        self.known_hosts = self.ssh_dir / "known_hosts"
        self.logs = self.state_dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.jobs, self.live, self.stops, self.procs = [], {}, {}, {}
        self.load()

    # ----- settings -----
    def config(self):
        c = dict(DEFAULTS)
        for k, env in ENV.items():
            if os.environ.get(env):
                c[k] = os.environ[env]
        try:
            c.update(json.loads(self.cfg_file.read_text()))
        except (FileNotFoundError, ValueError):
            pass
        try:
            return validate(c)
        except ValueError:
            return {**DEFAULTS, **{k: c[k] for k in ("host", "user")}}

    def save_config(self, data):
        c = validate({**self.config(), **(data or {})})
        _atomic_write(self.cfg_file, json.dumps(c, indent=1))
        return c

    def configured(self, c=None):
        c = c or self.config()
        return bool(c["host"] and c["user"])

    def public_key(self, create=True):
        """This server's SSH public key, generated on first use. -> (key or None, error or None)"""
        pub = Path(str(self.key) + ".pub")
        if not pub.exists() and create and not os.environ.get("MAC_SSH_KEY"):
            if not shutil.which(SSH_KEYGEN):
                return None, "ssh-keygen isn't installed on this server (apt install openssh-client)"
            self.ssh_dir.mkdir(mode=0o700, exist_ok=True)
            r = subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "llm-manager",
                                "-f", str(self.key)], capture_output=True, text=True, timeout=30)
            if r.returncode:
                return None, f"ssh-keygen failed: {r.stderr.strip()[:200]}"
        try:
            return pub.read_text().strip(), None
        except FileNotFoundError:
            return None, f"no public key next to {self.key}"

    def view(self):
        c = self.config()
        pub, err = self.public_key()
        return {**c, "configured": self.configured(c), "public_key": pub, "key_error": err,
                "key_path": str(self.key), "key_from_env": bool(os.environ.get("MAC_SSH_KEY"))}

    # ----- ssh -----
    def ssh_cmd(self, c):
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)
        return [SSH_BIN, "-F", "/dev/null", "-i", str(self.key), "-p", str(c["port"]),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "LogLevel=ERROR"]

    def remote(self, c, script, stdin=None, timeout=60):
        """Run a POSIX sh script on the Mac. -> CompletedProcess (text)."""
        if not shutil.which(SSH_BIN):
            raise TransferError("ssh isn't installed on this server (apt install openssh-client)")
        args = self.ssh_cmd(c) + ["--", f"{c['user']}@{c['host']}", "sh -c " + shlex.quote(script)]
        try:
            return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TransferError(f"the Mac didn't answer within {timeout}s")

    def remote_ok(self, c, script, **kw):
        r = self.remote(c, script, **kw)
        if r.returncode:
            msg = (r.stderr or r.stdout).strip().splitlines()
            msg = msg[-1] if msg else f"exit code {r.returncode}"
            if r.returncode == 255:
                if "Permission denied" in msg:
                    msg += " (authorize this server's SSH key on the Mac: see Settings)"
                raise TransferError(f"can't reach {c['user']}@{c['host']}: {msg}")
            raise TransferError(msg)
        return r.stdout

    def test(self):
        c = self.config()
        if not self.configured(c):
            raise ValueError("set the Mac host and user first")
        self.public_key()
        src = f'S={rq(c["mac_source"])}; if [ -d "$S" ]; then echo "source=ok"; else echo "source=missing"; fi\n' \
            if c["mac_source"] else 'echo "source=unset"\n'
        out = self.remote_ok(c, 'echo "home=$HOME"; echo "os=$(uname -sr)"; '
                                f'R={shlex.quote(c["rsync"])}; if command -v "$R" >/dev/null 2>&1; then '
                                'echo "rsync=$("$R" --version 2>&1 | head -n 1)"; else echo "rsync="; fi\n' + src,
                             timeout=20)
        info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        res = {"ok": True, "home": info.get("home"), "os": info.get("os"), "rsync": info.get("rsync") or None,
               "source": info.get("source"), "mode": c["mode"], "problems": []}
        if not res["rsync"]:
            res["problems"].append(f"{c['rsync']} isn't on the Mac's PATH for SSH sessions")
        if c["mode"] == "push" and not shutil.which(RSYNC_BIN):
            res["problems"].append("rsync isn't installed on this server (apt install rsync)")
        if c["mode"] == "pull" and res["source"] != "ok":
            res["problems"].append(f"{c['mac_source']} isn't there on the Mac: mount the models share first")
        res["ok"] = not res["problems"]
        return res

    # ----- queue -----
    def save(self):
        with self.lock:
            _atomic_write(self.file, json.dumps(self.jobs, indent=1))

    def load(self):
        try:
            self.jobs = json.loads(self.file.read_text())
        except (FileNotFoundError, ValueError):
            self.jobs = []
        for j in self.jobs:  # interrupted by a restart: push resumes, pull re-attaches to the Mac
            if j["status"] in ("starting", "running", "verifying"):
                j["status"] = "queued"

    def find(self, tid):
        j = next((j for j in self.jobs if j["id"] == tid), None)
        if not j:
            raise KeyError(tid)
        return j

    def dest_for(self, c, name, repo, variant, target, custom=None):
        """Same folder layout as the copy commands in the UI."""
        org, base = (repo or f"local/{name}").split("/", 1)
        if target in ("omlx", "lmstudio"):
            d = f"{c[target + '_dir']}/{org}/{name if variant else base}"
        elif target == "custom":
            d = f"{check_path(custom or '~/models', 'Parent folder')}/{name}"
        else:
            raise ValueError("target must be omlx, lmstudio or custom")
        return check_path(d, "Destination")

    def add(self, name, target="omlx", custom=None, meta=None):
        c = self.config()
        if not self.configured(c):
            raise ValueError("set up the Mac connection in Settings first")
        if not name or not NAME_RE.match(name) or not (self.llm_dir / name).is_dir() or (self.llm_dir / name).is_symlink():
            raise ValueError(f"{name} is not a model folder")
        if self.busy(name):
            raise ValueError(f"{name} is still downloading: send it once it's done")
        if any((self.llm_dir / name / ".cache" / "llm-manager").rglob("*.part")):
            raise ValueError(f"{name} has an unfinished download: resume it first")
        meta = meta or {}
        dest = self.dest_for(c, name, meta.get("repo"), meta.get("variant"), target, custom)
        with self.lock:
            for j in self.jobs:
                if j["name"] == name and j["dest"] == dest and j["status"] in ACTIVE:
                    raise ValueError(f"{name} is already being sent to that folder")
            j = {"id": uuid.uuid4().hex[:10], "name": name, "repo": meta.get("repo"), "target": target,
                 "dest": dest, "host": c["host"], "user": c["user"], "mode": c["mode"], "status": "queued",
                 "created": time.time(), "error": None, "verify": None, "attempts": 0}
            self.jobs.append(j)
            self.save()
        self.wake.set()
        return j

    def cancel(self, tid):
        with self.lock:
            j = self.find(tid)
            if j["status"] in ACTIVE:
                if j["status"] == "queued":
                    j["status"] = "cancelled"
                ev = self.stops.get(tid)
                if ev:
                    ev.set()
                self.save()
        self.wake.set()
        return j

    def retry(self, tid):
        with self.lock:
            j = self.find(tid)
            if j["status"] in ("failed", "cancelled", "done"):
                j.update(status="queued", error=None)
                self.save()
        self.wake.set()
        return j

    def remove(self, tid):
        with self.lock:
            j = self.find(tid)
            if j["status"] not in ACTIVE:
                self.jobs.remove(j)
                self.live.pop(tid, None)
                self.save()

    def active_for(self, name):
        with self.lock:
            return any(j["name"] == name and j["status"] in ACTIVE for j in self.jobs)

    def snapshot(self):
        with self.lock:
            out = []
            for j in reversed(self.jobs):
                x = dict(j)
                L = self.live.get(j["id"])
                if L:
                    x["bytes"] = L.get("bytes", x.get("bytes"))
                    x["speed"] = L.get("speed", 0) if j["status"] == "running" else 0
                out.append(x)
            return out

    def log_path(self, tid):
        return self.logs / f"mac-{tid}.log"

    # ----- running -----
    def worker(self):
        while True:
            with self.lock:
                nxt = next((j for j in self.jobs if j["status"] == "queued"), None)
            if nxt is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.run(nxt)

    def _set(self, j, **kw):
        with self.lock:
            j.update(kw)
            self.save()

    def run(self, j):
        tid = j["id"]
        logf = self.log_path(tid)

        def log(msg):
            with open(logf, "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip("\n") + "\n")

        stop = threading.Event()
        with self.lock:
            if j["status"] != "queued":
                return
            self.stops[tid] = stop
            j.update(status="starting", error=None, attempts=j.get("attempts", 0) + 1)
            self.save()
        try:
            c = self.config()
            if not self.configured(c):
                raise TransferError("the Mac connection isn't set up in Settings")
            # the settings may have changed since this was queued: use the ones it was queued with
            c.update(host=j["host"], user=j["user"], mode=j["mode"])
            src = self.llm_dir / j["name"]
            if not src.is_dir():
                raise TransferError(f"{src} no longer exists")
            expected = local_files(src)
            total = sum(expected.values())
            home = self.remote_ok(c, 'printf %s "$HOME"', timeout=30).strip()
            if not home.startswith("/"):
                raise TransferError("couldn't read the home folder on the Mac")
            dest = absolute(j["dest"], home)
            jobdir = f"{home}/.llm-manager/transfers/{tid}"
            self._set(j, total=total, files=len(expected), dest_abs=dest, started=j.get("started") or time.time())
            log(f"start {j['mode']}: {src} -> {c['user']}@{c['host']}:{dest} ({len(expected)} files, {total/1e9:.2f} GB)")
            if j["mode"] == "pull":
                self._pull(j, c, home, dest, jobdir, stop, log)
            else:
                self._push(j, c, src, dest, stop, log)
            if stop.is_set():
                raise _Cancelled()
            self._set(j, status="verifying")
            remote = parse_listing(self.remote_ok(c, f"D={shlex.quote(dest)}\n" + LIST_SCRIPT, timeout=120))
            v = compare(local_files(src), remote)
            with self.lock:
                self.live.setdefault(tid, {})["bytes"] = sum(min(remote.get(p, 0), s) for p, s in expected.items())
            log(f"verify: {v['files'] - v['bad']}/{v['files']} files match"
                + "".join(f"\n  {p['path']}: expected {p['expected']} bytes, "
                          f"{'missing' if p['actual'] is None else str(p['actual']) + ' bytes'} on the Mac"
                          for p in v["problems"]))
            if not v["ok"]:
                missing = " ({} missing)".format(v["missing"]) if v["missing"] else ""
                self._set(j, status="failed", verify=v,
                          error=f"{v['bad']} of {v['files']} files don't match on the Mac{missing}: Retry copies them again")
                return
            self._set(j, status="done", verify=v, finished=time.time(), bytes=total)
            log("done: every file's size matches the share")
        except _Cancelled:
            log("cancelled")
            self._set(j, status="cancelled")
        except TransferError as e:
            log(f"failed: {e}")
            self._set(j, status="cancelled" if stop.is_set() else "failed", error=None if stop.is_set() else str(e))
        except Exception as e:  # noqa: keep the worker alive
            log(f"failed: {type(e).__name__}: {e}")
            self._set(j, status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            with self.lock:
                self.stops.pop(tid, None)
                self.procs.pop(tid, None)
                if tid in self.live:
                    self.live[tid]["speed"] = 0
                    j["bytes"] = self.live[tid].get("bytes", j.get("bytes"))
                self.save()

    def _progress(self, j, c, dest):
        """List the destination on the Mac; update bytes and speed. False if the Mac didn't answer."""
        try:
            r = self.remote(c, f"D={shlex.quote(dest)}\n" + LIST_SCRIPT, timeout=60)
        except TransferError:
            return False
        if r.returncode == 255:
            return False
        b = min(sum(s for p, s in parse_listing(r.stdout).items() if not excluded(p)), j.get("total") or 0)
        now = time.time()
        with self.lock:
            L = self.live.setdefault(j["id"], {"speed": 0.0})
            lb, lt = L.get("last_b"), L.get("last_t")
            if lb is not None and now > lt:
                inst = max(0.0, (b - lb) / (now - lt))
                L["speed"] = inst if not L.get("speed") else 0.6 * L["speed"] + 0.4 * inst
            L.update(last_b=b, last_t=now, bytes=b)
            j["bytes"] = b
        return True

    def _push(self, j, c, src, dest, stop, log):
        if not shutil.which(RSYNC_BIN):
            raise TransferError("rsync isn't installed on this server (apt install rsync)")
        self.remote_ok(c, f"mkdir -p {shlex.quote(dest)}", timeout=30)
        cmd = [RSYNC_BIN, "-a", "--partial"] + [x for e in EXCLUDES for x in ("--exclude", e)] + [
            "-e", " ".join(shlex.quote(a) for a in self.ssh_cmd(c)), "--rsync-path", c["rsync"],
            f"{src}/", f"{c['user']}@{c['host']}:{dest}/"]
        self._set(j, status="running")
        for attempt in range(1, PUSH_RETRIES + 1):
            log(f"rsync attempt {attempt}")
            with open(self.log_path(j["id"]), "a") as out:
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
            with self.lock:
                self.procs[j["id"]] = p
            while p.poll() is None:
                if stop.wait(POLL):
                    self._kill(p)
                    raise _Cancelled()
                self._progress(j, c, dest)
            if p.returncode == 0:
                log("rsync finished")
                return
            log(f"rsync exited {p.returncode}")
            if attempt < PUSH_RETRIES and stop.wait(PUSH_RETRY_WAIT):
                raise _Cancelled()
        raise TransferError(f"rsync failed {PUSH_RETRIES} times (exit {p.returncode}): see the log; Retry resumes")

    @staticmethod
    def _kill(p):
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _pull_state(self, c, jobdir):
        """-> (final status line or "", still running?) or None if the Mac didn't answer."""
        r = self.remote(c, f"J={shlex.quote(jobdir)}\n"
                           'printf "status=%s\\n" "$(cat "$J/status" 2>/dev/null)"\n'
                           'if [ -f "$J/pid" ] && kill -0 "$(cat "$J/pid")" 2>/dev/null; then echo alive=1; else echo alive=0; fi\n',
                        timeout=30)
        if r.returncode == 255:
            return None
        info = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        return info.get("status", "").strip(), info.get("alive") == "1"

    def _pull(self, j, c, home, dest, jobdir, stop, log):
        src = absolute(c["mac_source"], home).rstrip("/") + "/" + j["name"]
        st = self._pull_state(c, jobdir)
        if st and st[1]:
            log("re-attached to the copy still running on the Mac")
        else:
            script = pull_script(jobdir, src, dest, c["rsync"])
            J = shlex.quote(jobdir)
            self.remote_ok(c, f"mkdir -p {J} && cat > {J}/run.sh && rm -f {J}/status && cd {J} && "
                              "(nohup /bin/sh ./run.sh >> log 2>&1 < /dev/null &) && echo started",
                           stdin=script, timeout=30)
            log(f"started on the Mac: rsync {src}/ -> {dest}/ (log: {jobdir}/log)")
        self._set(j, status="running", remote_dir=jobdir)
        lost_since = None
        while True:
            if stop.wait(POLL):
                try:
                    self.remote(c, f'J={shlex.quote(jobdir)}; [ -f "$J/pid" ] && kill "$(cat "$J/pid")"', timeout=20)
                except TransferError:
                    pass
                raise _Cancelled()
            st = self._pull_state(c, jobdir)
            if st is None:
                lost_since = lost_since or time.time()
                if time.time() - lost_since > LOST_AFTER:
                    raise TransferError(f"lost contact with the Mac for {LOST_AFTER // 60} minutes; the copy may still "
                                        "be running there. Retry re-attaches to it")
                continue
            lost_since = None
            status, alive = st
            self._progress(j, c, dest)
            if alive and not status:
                continue
            tail = self.remote(c, f"tail -n 40 {shlex.quote(jobdir)}/log", timeout=20).stdout
            log("Mac log:\n" + tail)
            if status == "exit 0":
                return
            if status == "exit nosource":
                raise TransferError(f"{src} isn't there on the Mac: mount the models share there, then Retry")
            if status == "cancelled":
                raise _Cancelled()
            if not status:
                raise TransferError("the copy on the Mac stopped unexpectedly (Mac restarted?): Retry resumes it")
            raise TransferError(f"rsync on the Mac failed ({status}): see the log; Retry resumes")


class _Cancelled(Exception):
    pass
