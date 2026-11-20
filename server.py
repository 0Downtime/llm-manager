#!/usr/bin/env python3
"""LLM model manager: queue Hugging Face repo downloads into a shared folder
and watch live progress from a small web UI. Stdlib HTTP server + huggingface_hub.

Config: environment variables, see env.example / README.md.
CLI:    server.py [serve] | passwd | set-token | clear-token | status
"""
import getpass, json, os, re, shutil, sys, threading, time, uuid
import urllib.error, urllib.parse, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# huggingface_hub keeps its config/token cache under HF_HOME (default ~/.cache/huggingface).
# The service runs with ProtectHome=yes and the CLI keeps root's HOME, so point it at our
# state dir; must happen before huggingface_hub is imported (it reads HF_HOME at import).
os.environ.setdefault("HF_HOME", str(Path(os.environ.get("STATE_DIR", "/var/lib/llm-manager")) / "hf-home"))

import auth
import downloader
import hfinfo
import quants
import transfer

from huggingface_hub import HfApi
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError

LLM_DIR = Path(os.environ.get("LLM_DIR", "/mnt/files/llms"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/llm-manager"))
PORT = int(os.environ.get("PORT", "8080"))
MIN_FREE = float(os.environ.get("MIN_FREE_GB", "10")) * 1e9
WEB_DIR = Path(__file__).with_name("web")
REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9][\w.+-]{0,79}$")
MAC_SMB_URL = os.environ.get("MAC_SMB_URL", "")        # e.g. smb://<smb-host>/files
MAC_MOUNT = os.environ.get("MAC_MOUNT", "/Volumes/files")
LLM_SUBDIR = os.environ.get("LLM_SUBDIR", LLM_DIR.name)  # path of LLM_DIR inside the share

STATE_DIR.mkdir(parents=True, exist_ok=True)
(STATE_DIR / "logs").mkdir(exist_ok=True)
JOBS_FILE = STATE_DIR / "jobs.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
TOKEN_FILE = STATE_DIR / "hf_token"
AUTH = auth.Auth(STATE_DIR, enabled=os.environ.get("LLM_AUTH", "on").lower() != "off",
                 nets=os.environ.get("ALLOWED_NETS"))
lock = threading.RLock()
wake = threading.Event()
api = HfApi()

jobs = []          # persisted dicts
live = {}          # job id -> {"files": {...}, "done": int, "speed": float, ...} (not persisted)
stops = {}         # job id -> threading.Event that cancels the running download
speed_hint = [30e6]  # last observed download speed (B/s), for ETA estimates in the browser


def save():
    with lock:
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, indent=1))
        tmp.replace(JOBS_FILE)


def load():
    global jobs
    if JOBS_FILE.exists():
        jobs = json.loads(JOBS_FILE.read_text())
        for j in jobs:  # anything interrupted by a restart resumes
            if j["status"] in ("running", "preparing"):
                j["status"] = "queued"


def disk():
    # LLM_DIR may not exist yet (fresh install / quick start): measure the nearest existing parent
    p = LLM_DIR.resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    u = shutil.disk_usage(p)
    return {"total": u.total, "used": u.used, "free": u.free, "min_free": MIN_FREE}


def parse_repo(text):
    t = text.strip()
    t = re.sub(r"^https?://(www\.)?huggingface\.co/", "", t)
    t = t.split("?")[0].split("#")[0].strip("/")
    parts = t.split("/")
    rev = None
    if len(parts) >= 4 and parts[2] == "tree":
        rev = "/".join(parts[3:])
    repo = "/".join(parts[:2])
    if not REPO_RE.match(repo):
        raise ValueError(f"not a Hugging Face model repo id: {text!r}")
    return repo, rev


def read_meta(d):
    """.hf-repo lines: repo, commit sha of the complete download, requested revision (optional),
    quant variant (optional; empty = the whole repo)."""
    m = Path(d) / ".hf-repo"
    lines = [x.strip() for x in m.read_text().splitlines()] if m.exists() else []
    lines += [""] * (4 - len(lines))
    return {"repo": lines[0] or None, "sha": lines[1] or None, "rev": lines[2] or None, "variant": lines[3] or None}


def read_marker(d, with_rev=False):
    m = read_meta(d)
    return (m["repo"], m["sha"], m["rev"]) if with_rev else (m["repo"], m["sha"])


def write_marker(d, repo, sha, rev=None, variant=None):
    (Path(d) / ".hf-repo").write_text(f"{repo}\n{sha}\n{rev or ''}\n{variant or ''}\n")


def tracked_branch(rev):
    """A download pinned to a commit (or none) follows main; a branch/tag follows itself."""
    return "main" if not rev or re.fullmatch(r"[0-9a-f]{40}", rev) else rev


def dir_for(repo, variant=None):
    """One folder per repo, or per repo + quant when only one quant of a GGUF repo is downloaded."""
    suffix = f"-{variant}" if variant else ""
    d = LLM_DIR / (repo.split("/")[1] + suffix)
    meta = read_meta(d)
    if d.exists() and meta["repo"] and (meta["repo"] != repo or meta["variant"] != variant):
        d = LLM_DIR / (repo.replace("/", "__") + suffix)
    return d


def file_progress(local_dir, path, size):
    """Bytes on disk for one repo file: final file, else its resumable .part."""
    try:
        if (local_dir / path).stat().st_size == size:
            return size, True
    except FileNotFoundError:
        pass
    try:
        return min(downloader.part_path(local_dir, path).stat().st_size, size), False
    except FileNotFoundError:
        return 0, False


def _size(v):
    return v["size"] if isinstance(v, dict) else v


def measure(job):
    d = Path(job["dir"])
    files = job.get("files") or {}
    per, done_bytes, done_files = {}, 0, 0
    for path, info in files.items():
        size = _size(info)
        b, ok = file_progress(d, path, size)
        per[path] = {"size": size, "bytes": b, "done": ok}
        done_bytes += b
        done_files += ok
    return per, done_bytes, done_files


def monitor():
    """Every 2s: apply the speed cap / quiet hours, measure on-disk progress and compute speed."""
    while True:
        time.sleep(2)
        try:
            t = effective_throttle()
            downloader.LIMITER.set(t["rate"], t["paused"])
            sync_token_file()
        except Exception as e:  # never let a bad settings file stop progress reporting
            print(f"monitor: {e}", flush=True)
        with lock:
            running = [j for j in jobs if j["status"] == "running"]
        for j in running:
            per, b, nf = measure(j)
            now = time.time()
            with lock:
                L = live.setdefault(j["id"], {"speed": 0.0})
                last_b, last_t = L.get("last_b"), L.get("last_t")
                if last_b is not None and now > last_t:
                    inst = max(0.0, (b - last_b) / (now - last_t))
                    L["speed"] = inst if L["speed"] == 0 else 0.7 * L["speed"] + 0.3 * inst
                if last_b is None or b != last_b or now - last_t > 10:
                    L["last_b"], L["last_t"] = b, now
                L.update(files=per, bytes=b, files_done=nf)
                if L["speed"] > 1e6:
                    speed_hint[0] = L["speed"]


def run_job(j):
    jid = j["id"]
    logf = STATE_DIR / "logs" / f"{jid}.log"

    def log(msg):
        with open(logf, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

    def note(msg):
        with lock:
            j["note"] = msg

    stop = threading.Event()
    with lock:
        stops[jid] = stop
    try:
        with lock:
            j["status"] = "preparing"; j["error"] = None
            save()
        # pin to one commit: resuming after the repo changes upstream must not mix revisions
        sha, files = downloader.fetch_manifest(api, j["repo"], j.get("sha") or j.get("revision"))
        files = quants.select(files, j.get("variant"))  # one quant of a GGUF repo, not all of them
        total = sum(i["size"] for i in files.values())
        d = Path(j["dir"])
        if d.exists():
            downloader.adopt_hf_partials(d, files, log)
        already = measure({**j, "files": files})[1] if d.exists() else 0
        need = total - already
        free = disk()["free"]
        with lock:
            j["files"], j["total"], j["sha"] = files, total, sha
            if not j.get("force") and need > free - MIN_FREE:
                j["status"] = "blocked"
                j["error"] = (f"needs {need/1e9:.1f} GB more but only {free/1e9:.1f} GB free "
                              f"(keeping {MIN_FREE/1e9:.0f} GB reserve). Free space or retry with override.")
                save(); return
            if stop.is_set():
                return
            j["status"] = "running"; j["started"] = j.get("started") or time.time()
            save()
        d.mkdir(parents=True, exist_ok=True)
        owner, old_sha = read_marker(d)
        prev = None
        if owner == j["repo"] and old_sha and old_sha != sha:
            # updating an existing download: keep the old marker until the update completes,
            # so a failed/cancelled update still reports the folder as the old version
            try:
                prev = (old_sha, quants.select(manifest_for(j["repo"], old_sha), j.get("variant"), strict=False))
            except Exception as e:
                log(f"could not load manifest of previous commit {old_sha[:12]} ({e}); re-verifying all files")
        elif owner != j["repo"]:
            write_marker(d, j["repo"], sha, j.get("revision"), j.get("variant"))
        log(f"start {j['repo']}@{sha[:12]}: {len(files)} files, {total/1e9:.2f} GB, {already/1e9:.2f} GB already on disk")
        downloader.download_all(j["repo"], sha, d, files, stop, log, workers=4, on_verify=note, prev=prev)
        write_marker(d, j["repo"], sha, j.get("revision"), j.get("variant"))
        with lock:
            j["status"] = "done"; j["finished"] = time.time(); j["note"] = None
            updates.pop(d.name, None)
            _models_cache["t"] = 0
            save()
        log("all files downloaded and verified")
    except downloader.Cancelled:
        log("cancelled")
        with lock:
            j["note"] = None; save()
    except GatedRepoError:
        with lock:
            j["status"] = "failed"; j["error"] = "repo is gated: accept its terms on huggingface.co and set HF_TOKEN"; save()
    except RepositoryNotFoundError:
        with lock:
            j["status"] = "failed"; j["error"] = "repo not found (or private without HF_TOKEN)"; save()
    except Exception as e:  # noqa
        log(f"failed: {type(e).__name__}: {e}")
        with lock:
            if j["status"] != "cancelled":
                j["status"] = "failed"; j["error"] = f"{type(e).__name__}: {e}"
            j["note"] = None; save()
    finally:
        with lock:
            stops.pop(jid, None)


def worker():
    while True:
        with lock:
            nxt = next((j for j in jobs if j["status"] == "queued"), None)
        if nxt is None:
            wake.wait(5); wake.clear(); continue
        run_job(nxt)


_models_cache = {"t": 0, "v": []}


def list_models():
    if time.time() - _models_cache["t"] < 8:
        return _models_cache["v"]
    out = []
    if LLM_DIR.exists():
        for d in sorted(LLM_DIR.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            size, n = 0, 0
            for root, dirs, fs in os.walk(d):
                dirs[:] = [x for x in dirs if x != ".cache"]
                for f in fs:
                    if f == ".hf-repo":
                        continue
                    try:
                        size += os.stat(os.path.join(root, f)).st_size; n += 1
                    except FileNotFoundError:
                        pass
            meta = read_meta(d)
            pd = d / downloader.PART_DIR
            out.append({"name": d.name, "repo": meta["repo"], "sha": meta["sha"], "rev": meta["rev"], "variant": meta["variant"],
                        "size": size, "files": n, "mtime": d.stat().st_mtime,
                        "partial": pd.exists() and any(pd.rglob("*.part"))})
    _models_cache.update(t=time.time(), v=out)
    return out


def _busy(name):
    with lock:
        return any(Path(j["dir"]).name == name and j["status"] in ACTIVE for j in jobs)


MAC = transfer.Manager(STATE_DIR, LLM_DIR, busy=_busy)   # Send to Mac: transfers over SSH


def send_to_mac(data):
    name = data.get("name") or ""
    meta = read_meta(LLM_DIR / name) if name and "/" not in name and not name.startswith(".") else {}
    return MAC.add(name, data.get("target") or "omlx", data.get("custom"), meta)


def snapshot():
    with lock:
        js = []
        for j in reversed(jobs):
            x = {k: v for k, v in j.items() if k not in ("files", "force")}
            L = live.get(j["id"], {})
            if L:
                x["bytes"] = L.get("bytes", 0); x["files_done"] = L.get("files_done", 0)
            if j["status"] == "running" and L:
                x["speed"] = L.get("speed", 0)
                x["file_list"] = sorted(({"path": k, **v} for k, v in L.get("files", {}).items()),
                                        key=lambda f: f["path"])
            x["file_count"] = len(j.get("files") or {})
            js.append(x)
        active = {Path(j["dir"]).name for j in jobs if j["status"] in ACTIVE}
        models = [{**m, "update": updates.get(m["name"]), "busy": m["name"] in active} for m in list_models()]
    return {"jobs": js, "models": models, "disk": disk(), "llm_dir": str(LLM_DIR),
            "updates_checking": update_state["running"], "updates_checked": update_state["checked"],
            "throttle": effective_throttle(), "token_set": bool(os.environ.get("HF_TOKEN")),
            "mac": {"smb_url": MAC_SMB_URL, "mount": MAC_MOUNT, "subdir": LLM_SUBDIR},
            "transfers": MAC.snapshot(), "mac_ssh": _mac_brief(),
            "auth": AUTH.enabled, "now": time.time()}


def _mac_brief():
    c = MAC.config()
    return {"configured": MAC.configured(c), "host": c["host"], "user": c["user"], "mode": c["mode"],
            "omlx_dir": c["omlx_dir"], "lmstudio_dir": c["lmstudio_dir"]}


# ---------- update check ----------
ACTIVE = ("queued", "preparing", "running")
UPDATE_EVERY = float(os.environ.get("UPDATE_CHECK_HOURS", "6")) * 3600
updates = {}                       # folder name -> result of check_update()
update_state = {"running": False, "checked": None}
_manifests = {}                    # (repo, sha) -> files; a commit's file list never changes


def manifest_for(repo, sha):
    if (repo, sha) not in _manifests:
        _manifests[(repo, sha)] = downloader.fetch_manifest(api, repo, sha)[1]
    return _manifests[(repo, sha)]


def check_update(m):
    repo, sha = m.get("repo"), m.get("sha")
    if not repo:
        return {"status": "unknown", "reason": "not downloaded by this tool"}
    if not sha:
        return {"status": "unknown", "reason": "no commit recorded"}
    branch = tracked_branch(m.get("rev"))
    info = api.model_info(repo, revision=branch, token=os.environ.get("HF_TOKEN") or None)
    res = {"status": "current", "branch": branch, "local_sha": sha, "remote_sha": info.sha,
           "remote_date": info.last_modified.isoformat() if info.last_modified else None, "checked": time.time()}
    if info.sha == sha:
        return res
    v = m.get("variant")
    old, new = quants.select(manifest_for(repo, sha), v, strict=False), quants.select(manifest_for(repo, info.sha), v, strict=False)
    key = downloader._key
    changed = [p for p in new if p in old and key(new[p]) != key(old[p])]
    added = [p for p in new if p not in old]
    removed = [p for p in old if p not in new]
    res.update(status="update" if changed or added or removed else "current",
               changed=len(changed), added=len(added), removed=len(removed),
               bytes=sum(new[p]["size"] for p in changed + added),
               files=sorted(changed + added)[:30], removed_files=sorted(removed)[:30])
    return res


def check_all_updates():
    with lock:
        if update_state["running"]:
            return False
        update_state["running"] = True
    try:
        _models_cache["t"] = 0
        for m in list_models():
            with lock:
                busy = any(Path(j["dir"]).name == m["name"] and j["status"] in ACTIVE for j in jobs)
            if busy or m.get("partial"):
                continue
            try:
                r = check_update(m)
            except GatedRepoError:
                r = {"status": "error", "reason": "gated repo: set HF_TOKEN"}
            except RepositoryNotFoundError:
                r = {"status": "error", "reason": "repo no longer exists or is private"}
            except Exception as e:  # noqa
                r = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
            with lock:
                updates[m["name"]] = r
    finally:
        with lock:
            update_state.update(running=False, checked=time.time())
    return True


def update_loop():
    time.sleep(60)
    while True:
        check_all_updates()
        time.sleep(UPDATE_EVERY)


def start_update_check():
    threading.Thread(target=check_all_updates, daemon=True).start()


# ---------- settings: speed cap + quiet hours ----------
DEFAULT_SETTINGS = {"limit_mbps": 0,
                    "quiet": {"enabled": False, "start": "08:00", "end": "23:00", "mode": "limit", "limit_mbps": 5}}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_settings_lock = threading.Lock()


def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        d = json.loads(SETTINGS_FILE.read_text())
        s["limit_mbps"] = d.get("limit_mbps", s["limit_mbps"])
        s["quiet"].update(d.get("quiet") or {})
    except (FileNotFoundError, ValueError):
        pass
    return s


def save_speed_settings(data):
    def mbps(v, name):
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if not 0 <= v <= 100000:
            raise ValueError(f"{name} must be between 0 and 100000 MB/s")
        return v
    q = data.get("quiet") or {}
    new = {"limit_mbps": mbps(data.get("limit_mbps", 0), "speed cap"),
           "quiet": {"enabled": bool(q.get("enabled")),
                     "start": str(q.get("start", "08:00")), "end": str(q.get("end", "23:00")),
                     "mode": q.get("mode", "limit"), "limit_mbps": mbps(q.get("limit_mbps", 0), "quiet-hours cap")}}
    if not (HHMM.match(new["quiet"]["start"]) and HHMM.match(new["quiet"]["end"])):
        raise ValueError("quiet hours need HH:MM times")
    if new["quiet"]["mode"] not in ("limit", "pause"):
        raise ValueError("quiet-hours mode must be 'limit' or 'pause'")
    with _settings_lock:
        auth._write_private(SETTINGS_FILE, json.dumps(new, indent=1))
    t = effective_throttle()
    downloader.LIMITER.set(t["rate"], t["paused"])  # apply now, not on the next tick
    return new


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now_min, start, end):
    s, e = _minutes(start), _minutes(end)
    if s == e:
        return False
    return s <= now_min < e if s < e else (now_min >= s or now_min < e)  # wraps past midnight


def effective_throttle(now=None):
    """-> {"rate": bytes/s (0 = unlimited), "paused": bool, "reason": str|None}"""
    s = load_settings()
    q = s["quiet"]
    now = now or datetime.now()
    if q["enabled"] and in_window(now.hour * 60 + now.minute, q["start"], q["end"]):
        if q["mode"] == "pause":
            return {"rate": 0, "paused": True, "reason": f"paused for quiet hours until {q['end']}"}
        if q["limit_mbps"] > 0:
            return {"rate": q["limit_mbps"] * 1e6, "paused": False,
                    "reason": f"quiet hours: capped at {q['limit_mbps']:g} MB/s until {q['end']}"}
        return {"rate": 0, "paused": False, "reason": None}
    if s["limit_mbps"] > 0:
        return {"rate": s["limit_mbps"] * 1e6, "paused": False, "reason": f"capped at {s['limit_mbps']:g} MB/s"}
    return {"rate": 0, "paused": False, "reason": None}


# ---------- Hugging Face token ----------
token_info = {"user": None, "checked": None, "error": None}


def hf_whoami(tok):
    req = urllib.request.Request("https://huggingface.co/api/whoami-v2",
                                 headers={"Authorization": f"Bearer {tok}", "User-Agent": "llm-manager/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ValueError("Hugging Face rejected this token")
        raise ValueError(f"could not verify the token (Hub error {e.code})")
    except urllib.error.URLError as e:
        raise ValueError(f"could not reach huggingface.co to verify the token ({e.reason})")


def load_token():
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            os.environ["HF_TOKEN"] = tok
    if os.environ.get("HF_TOKEN"):
        def bg():
            try:
                token_info.update(user=hf_whoami(os.environ["HF_TOKEN"]).get("name"), checked=time.time(), error=None)
            except ValueError as e:
                token_info.update(error=str(e), checked=time.time())
        threading.Thread(target=bg, daemon=True).start()


_token_mtime = ["unset"]


def sync_token_file():
    """Apply a token set/cleared by the CLI (another process) without a restart."""
    try:
        m = TOKEN_FILE.stat().st_mtime
    except FileNotFoundError:
        m = None
    if m == _token_mtime[0]:
        return
    first = _token_mtime[0] == "unset"
    _token_mtime[0] = m
    if first:
        return  # startup already handled by load_token()
    if m is None:
        os.environ.pop("HF_TOKEN", None)
        token_info.update(user=None, checked=None, error=None)
    else:
        os.environ["HF_TOKEN"] = TOKEN_FILE.read_text().strip()
        token_info.update(user=None, error=None)
        load_token()  # refreshes the account name in the background
    hfinfo.clear_cache()


def set_token(tok):
    tok = (tok or "").strip()
    if not re.fullmatch(r"hf_[A-Za-z0-9]{20,}", tok):
        raise ValueError("that doesn't look like a Hugging Face access token (hf_…)")
    who = hf_whoami(tok)
    auth._write_private(TOKEN_FILE, tok + "\n")
    _token_mtime[0] = TOKEN_FILE.stat().st_mtime  # our own write: don't re-apply it on the next tick
    os.environ["HF_TOKEN"] = tok
    token_info.update(user=who.get("name"), checked=time.time(), error=None)
    hfinfo.clear_cache()
    return token_status()


def clear_token():
    TOKEN_FILE.unlink(missing_ok=True)
    _token_mtime[0] = None
    os.environ.pop("HF_TOKEN", None)
    token_info.update(user=None, checked=None, error=None)
    hfinfo.clear_cache()
    return token_status()


def token_status():
    """Never includes the token itself."""
    return {"set": bool(os.environ.get("HF_TOKEN")), "source": "settings" if TOKEN_FILE.exists() else
            ("environment" if os.environ.get("HF_TOKEN") else None),
            "user": token_info["user"], "error": token_info["error"]}


def settings_view():
    return {"speed": load_settings(), "throttle": effective_throttle(), "token": token_status(),
            "auth": {"enabled": AUTH.enabled}, "mac": MAC.view()}


# ---------- delete ----------
def delete_model(name, confirm):
    """Remove one model folder from LLM_DIR. Requires the folder name typed back as confirmation."""
    if not name or confirm != name:
        raise ValueError("confirmation does not match the folder name")
    if name.startswith(".") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError("invalid folder name")
    d = LLM_DIR / name
    if d.is_symlink() or not d.is_dir() or d.resolve().parent != LLM_DIR.resolve():
        raise ValueError(f"{name} is not a model folder in {LLM_DIR}")
    with lock:
        if any(Path(j["dir"]).resolve() == d.resolve() and j["status"] in ACTIVE for j in jobs):
            raise ValueError(f"{name} has an active download: cancel it first")
    if MAC.active_for(name):
        raise ValueError(f"{name} is being sent to the Mac: cancel that first")
    freed = sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and not f.is_symlink())
    shutil.rmtree(d)
    with lock:
        updates.pop(name, None)
        _models_cache["t"] = 0
    with open(STATE_DIR / "actions.log", "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"deleted {d} ({freed} bytes)\n")
    return {"deleted": name, "freed": freed}


def local_status(repo):
    """Is this repo (or which of its quants) already in the folder / in the queue?
    Keys of local/jobs are the quant variant, "" for a whole-repo download."""
    with lock:
        js = {}
        for j in jobs:  # later jobs win
            if j["repo"] == repo:
                js[j.get("variant") or ""] = j["status"]
    local = {m.get("variant") or "": m["name"] for m in list_models() if m["repo"] == repo and not m["partial"]}
    whole = dir_for(repo)
    owner, sha = read_marker(whole)
    return {"job_status": js.get(""), "local_dir": str(whole) if owner == repo else None,
            "local_sha": sha if owner == repo else None, "local_variants": local, "job_variants": js}


def preview(q, lib=None):
    """Search results for free text, or full model stats for a repo id / URL."""
    q = (q or "").strip()
    try:
        repo, _ = parse_repo(q)
    except ValueError:
        repo = None
    extra = {"disk_free": disk()["free"], "min_free": MIN_FREE, "speed_hint": speed_hint[0]}
    if repo:
        try:
            return {**hfinfo.model(repo), **local_status(repo), **extra}
        except hfinfo.HubError as e:
            # unauthenticated, the Hub answers 401 for repos that don't exist; a half-typed
            # id like "qwen/fla" should fall back to search instead of showing an error
            if e.status not in (401, 404) or "huggingface.co" in q:
                return {"kind": "error", "error": f"{repo}: {e}"}
    if len(q) < 2:
        return {"kind": "empty"}
    return {"kind": "search", "query": q, "results": hfinfo.search(q, lib=lib or None), **extra}


def add_job(repo_text, force=False, update=False, variant=None, name=None):
    repo, rev = parse_repo(repo_text)
    variant = (variant or "").strip() or None
    if variant and not VARIANT_RE.match(variant):
        raise ValueError(f"not a valid quant name: {variant!r}")
    if update:
        if name:  # the Library names the folder: a repo can be in several (one per quant)
            if name.startswith(".") or "/" in name or "\\" in name or not (LLM_DIR / name).is_dir():
                raise ValueError(f"{name} is not a model folder")
            d = LLM_DIR / name
        else:
            d = dir_for(repo, variant)
        meta = read_meta(d)
        if meta["repo"] != repo:
            raise ValueError(f"{repo} is not in the folder yet")
        variant = meta["variant"]
        rev = None if tracked_branch(meta["rev"]) == "main" else meta["rev"]
    else:
        d = dir_for(repo, variant)
    label = repo + (f" ({variant})" if variant else "")
    with lock:
        for j in jobs:
            if j["dir"] == str(d) and j["status"] in ACTIVE:
                raise ValueError(f"{label} is already queued")
        j = {"id": uuid.uuid4().hex[:10], "repo": repo, "revision": rev, "variant": variant, "dir": str(d),
             "status": "queued", "created": time.time(), "force": bool(force), "error": None,
             "kind": "update" if update else "download"}
        jobs.append(j); save()
    wake.set()
    return j


def cancel(jid):
    with lock:
        j = next((j for j in jobs if j["id"] == jid), None)
        if not j:
            raise KeyError(jid)
        if j["status"] in ("queued", "blocked", "preparing", "running"):
            j["status"] = "cancelled"
            ev = stops.get(jid)
            if ev:
                ev.set()   # partial .part files stay on disk, so Resume continues from there
            save()
    return j


def retry(jid, force=False):
    with lock:
        j = next((j for j in jobs if j["id"] == jid), None)
        if not j:
            raise KeyError(jid)
        if j["status"] in ("failed", "cancelled", "blocked", "done"):
            j["status"] = "queued"; j["error"] = None
            if force:
                j["force"] = True
            save()
    wake.set()
    return j


def remove(jid):
    """Forget a finished job (does not touch downloaded files)."""
    with lock:
        j = next((j for j in jobs if j["id"] == jid), None)
        if j and j["status"] not in ("queued", "preparing", "running"):
            jobs.remove(j); live.pop(jid, None); save()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}
OPEN_PATHS = {"/login", "/login.html", "/api/login", "/favicon.ico"}

SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Models: set a password</title><style>body{font:15px/1.5 system-ui,sans-serif;max-width:560px;margin:12vh auto;padding:0 16px;color:#1b1f24;background:#f6f7f9}
@media (prefers-color-scheme:dark){body{color:#e6e8ec;background:#0f1115}}code{display:block;padding:10px 12px;border-radius:8px;background:rgba(127,127,127,.15);margin:10px 0;overflow-x:auto}</style></head>
<body><h1>Set a password first</h1><p>This LLM Models server has no password yet, so it won't show anything until one is set.
Run this on the Proxmox host (or inside the container without the <code style="display:inline;padding:1px 5px">pct exec</code> part):</p>
<code>pct exec __CTID__ -- llm-manager passwd</code><p>Then reload this page.</p></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "llm-manager"
    sys_version = ""

    def log_message(self, *a):
        pass

    # ----- helpers -----
    def send(self, code, body, ctype="application/json", headers=None):
        b = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, where, headers=None):
        self.send(302, b"", "text/plain", {"Location": where, **(headers or {})})

    @property
    def ip(self):
        return self.client_address[0]

    @property
    def token(self):
        return auth.Auth.token_from(self.headers.get("Cookie"))

    def gate(self):
        """Allowlist + login. Returns True if the request may proceed; otherwise has replied."""
        path = urllib.parse.urlparse(self.path).path
        is_api = path.startswith("/api/")
        if not AUTH.ip_allowed(self.ip):
            self.send(403, {"error": "forbidden"} if is_api else b"Forbidden: this address is not on the allowlist.",
                      "application/json" if is_api else "text/plain; charset=utf-8")
            return False
        if not AUTH.enabled:
            return True
        if not AUTH.has_password():
            if is_api:
                self.send(503, {"error": "no password set: run `llm-manager passwd` in the container"})
            else:
                self.send(503, SETUP_PAGE.replace("__CTID__", os.environ.get("CTID", "<ctid>")), "text/html; charset=utf-8")
            return False
        if path in OPEN_PATHS or AUTH.valid(self.token):
            return True
        if is_api:
            self.send(401, {"error": "login required"})
        else:
            self.redirect("/login")
        return False

    def read_json(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":  # with SameSite=Strict cookies this blocks cross-site form posts
            raise PermissionError("requests must be application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # ----- routes -----
    def do_GET(self):
        if not self.gate():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path in ("/login", "/login.html"):
            if AUTH.enabled and AUTH.valid(self.token):
                return self.redirect("/")
            return self.send(200, (WEB_DIR / "login.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        if u.path == "/api/state":
            return self.send(200, snapshot())
        if u.path == "/api/settings":
            return self.send(200, settings_view())
        if u.path == "/api/preview":
            qs = urllib.parse.parse_qs(u.query)
            try:
                return self.send(200, preview(qs.get("q", [""])[0], qs.get("lib", [""])[0]))
            except hfinfo.HubError as e:
                return self.send(200, {"kind": "error", "error": str(e)})
            except Exception as e:  # network trouble etc.
                return self.send(200, {"kind": "error", "error": f"{type(e).__name__}: {e}"})
        if u.path.startswith("/api/transfers/") and u.path.endswith("/log"):
            tid = u.path.split("/")[3]
            if not re.fullmatch(r"[0-9a-f]{10}", tid):
                return self.send(404, {"error": "no such transfer"})
            f = MAC.log_path(tid)
            txt = f.read_text(errors="replace")[-30000:] if f.exists() else ""
            return self.send(200, txt.encode(), "text/plain; charset=utf-8")
        if u.path.startswith("/api/jobs/") and u.path.endswith("/log"):
            jid = u.path.split("/")[3]
            if not re.fullmatch(r"[0-9a-f]{10}", jid):
                return self.send(404, {"error": "no such job"})
            f = STATE_DIR / "logs" / f"{jid}.log"
            txt = f.read_text(errors="replace")[-20000:] if f.exists() else ""
            return self.send(200, txt.encode(), "text/plain; charset=utf-8")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.gate():
            return
        try:
            data = self.read_json()
            parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
            if parts == ["api", "login"]:
                if not AUTH.enabled:
                    return self.send(200, {"ok": True})
                tok, err = AUTH.login(self.ip, data.get("password"))
                if not tok:
                    return self.send(401 if "Wrong" in err else 429, {"error": err})
                return self.send(200, {"ok": True}, headers={"Set-Cookie": AUTH.cookie(tok)})
            if parts == ["api", "logout"]:
                AUTH.logout(self.token)
                return self.send(200, {"ok": True}, headers={"Set-Cookie": AUTH.cookie("", 0)})
            if parts == ["api", "jobs"]:
                return self.send(200, add_job(data.get("repo", ""), data.get("force"), data.get("update"),
                                              data.get("variant"), data.get("name")))
            if parts == ["api", "updates", "check"]:
                start_update_check(); return self.send(200, {"started": True})
            if parts == ["api", "models", "delete"]:
                return self.send(200, delete_model(data.get("name"), data.get("confirm")))
            if parts == ["api", "settings", "speed"]:
                save_speed_settings(data); return self.send(200, settings_view())
            if parts == ["api", "settings", "token"]:
                set_token(data.get("token")); return self.send(200, settings_view())
            if parts == ["api", "settings", "token", "clear"]:
                clear_token(); return self.send(200, settings_view())
            if parts == ["api", "settings", "mac"]:
                MAC.save_config(data); return self.send(200, settings_view())
            if parts == ["api", "mac", "test"]:
                try:
                    return self.send(200, MAC.test())
                except transfer.TransferError as e:
                    return self.send(200, {"ok": False, "problems": [str(e)]})
            if parts == ["api", "transfers"]:
                return self.send(200, send_to_mac(data))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"]:
                tid, act = parts[2], parts[3]
                if act == "cancel":
                    return self.send(200, MAC.cancel(tid))
                if act == "retry":
                    return self.send(200, MAC.retry(tid))
                if act == "remove":
                    MAC.remove(tid); return self.send(200, {"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                jid, act = parts[2], parts[3]
                if act == "cancel":
                    return self.send(200, cancel(jid))
                if act == "retry":
                    return self.send(200, retry(jid, data.get("force")))
                if act == "remove":
                    remove(jid); return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except PermissionError as e:
            self.send(415, {"error": str(e)})
        except (ValueError, KeyError) as e:
            self.send(400, {"error": str(e)})


# ---------- CLI ----------
def _ask(prompt):
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    line = sys.stdin.readline()  # non-interactive (tests, automation)
    return line.rstrip("\n")


def cli(cmd):
    if cmd == "passwd":
        pw = _ask("New password (min 10 chars): ")
        if _ask("Repeat it: ") != pw:
            sys.exit("Passwords don't match; nothing changed.")
        try:
            AUTH.set_password(pw)
        except ValueError as e:
            sys.exit(str(e))
        print("Password set. Any existing sessions were logged out; the running service picks this up immediately.")
    elif cmd == "set-token":
        try:
            st = set_token(_ask("Hugging Face access token (hf_…): "))
        except ValueError as e:
            sys.exit(str(e))
        print(f"Token saved for Hugging Face account: {st['user']}. The running service picks it up within a few seconds.")
    elif cmd == "clear-token":
        clear_token()
        print("Token removed. The running service picks this up within a few seconds.")
    elif cmd == "status":
        load_token()
        print(json.dumps({"password_set": AUTH.has_password(), "auth_enabled": AUTH.enabled,
                          "sessions": len(AUTH.sessions), "allowed_nets": [str(n) for n in AUTH.nets],
                          "token": {k: v for k, v in token_status().items() if k != "user"},
                          "speed": load_settings(), "throttle_now": effective_throttle(),
                          "llm_dir": str(LLM_DIR), "state_dir": str(STATE_DIR)}, indent=1))
    else:
        sys.exit("usage: llm-manager [serve|passwd|set-token|clear-token|status]")


def serve():
    load()
    load_token()
    t = effective_throttle()
    downloader.LIMITER.set(t["rate"], t["paused"])
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=MAC.worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"llm-manager on :{PORT}, models in {LLM_DIR}, login {'on' if AUTH.enabled else 'OFF (testing only)'}"
          f"{'' if AUTH.has_password() or not AUTH.enabled else ' (no password set yet: run llm-manager passwd)'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "serve"
    serve() if arg == "serve" else cli(arg)
