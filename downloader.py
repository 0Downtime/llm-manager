"""Resumable, verified Hugging Face repo downloader (stdlib HTTP).

Why not `hf download`: since huggingface_hub 1.x/2.x each run writes to a fresh
random temp file and deletes it on failure, so an interrupted multi-GB shard
restarts from zero. Here each file streams into a stable
`<model>/.cache/llm-manager/<path>.part`, resumes with an HTTP Range request,
is hashed while it downloads (SHA-256 for LFS files, git blob SHA-1 otherwise),
and only gets renamed into place once the hash matches the Hub's.
"""
import hashlib, os, shutil, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HUB = "https://huggingface.co"
PART_DIR = Path(".cache") / "llm-manager"
CHUNK = 1 << 20


class Cancelled(Exception):
    pass


class VerifyError(Exception):
    pass


class Paused(Exception):
    pass


class RateLimiter:
    """Global download speed cap shared by all worker threads (token bucket with debt).

    rate 0 = unlimited. While paused, wait() raises Paused so the caller can drop its
    HTTP connection (an idle stream would time out during long quiet hours) and resume
    later with a Range request via wait_unpaused().
    """
    def __init__(self):
        self.cv = threading.Condition()
        self.rate, self.paused = 0.0, False
        self.allow, self.t = 0.0, time.monotonic()

    def set(self, rate, paused=False):
        with self.cv:
            rate, paused = float(rate or 0), bool(paused)
            if rate != self.rate or paused != self.paused:
                self.rate, self.paused = rate, paused
                self.allow, self.t = 0.0, time.monotonic()
                self.cv.notify_all()

    def chunk(self):
        return CHUNK // 4 if self.rate else CHUNK

    def wait(self, n, stop):
        with self.cv:
            while True:
                if stop.is_set():
                    raise Cancelled()
                if self.paused:
                    raise Paused()
                if not self.rate:
                    return
                now = time.monotonic()
                self.allow = min(self.rate, self.allow + (now - self.t) * self.rate)  # burst <= 1 s
                self.t = now
                if self.allow >= 0:
                    self.allow -= n  # may go into debt; the next caller waits it off
                    return
                self.cv.wait(min(1.0, -self.allow / self.rate))

    def wait_unpaused(self, stop):
        with self.cv:
            while self.paused:
                if stop.is_set():
                    raise Cancelled()
                self.cv.wait(1.0)
        if stop.is_set():
            raise Cancelled()


LIMITER = RateLimiter()


def part_path(d, path):
    return Path(d) / PART_DIR / (path + ".part")


def _token():
    return os.environ.get("HF_TOKEN") or None


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow Hub -> CDN redirects without forwarding the HF token to another host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_StripAuthOnRedirect)


def _open(url, headers=None, timeout=60):
    h = {"User-Agent": "llm-manager/1.0", **(headers or {})}
    if _token():
        h["Authorization"] = f"Bearer {_token()}"
    return _opener.open(urllib.request.Request(url, headers=h), timeout=timeout)


def fetch_manifest(api, repo, rev=None):
    """-> (commit_sha, {path: {"size", "sha256" | None, "blob"}})"""
    info = api.model_info(repo, revision=rev, token=_token())
    files = {}
    for e in api.list_repo_tree(repo, revision=info.sha, recursive=True, token=_token()):
        if e.__class__.__name__ != "RepoFile":
            continue
        lfs = getattr(e, "lfs", None)
        sha256 = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)) if lfs else None
        files[e.path] = {"size": e.size, "sha256": sha256, "blob": getattr(e, "blob_id", None)}
    return info.sha, files


def _hasher(info):
    if info.get("sha256"):
        return hashlib.sha256(), info["sha256"]
    if info.get("blob"):
        h = hashlib.sha1(); h.update(f"blob {info['size']}\0".encode())
        return h, info["blob"]
    return None, None


def hash_file(p, info, stop=None):
    h, want = _hasher(info)
    if not h:
        return True
    with open(p, "rb") as f:
        while True:
            if stop is not None and stop.is_set():
                raise Cancelled()
            b = f.read(8 * CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest() == want


def adopt_hf_partials(d, files, log):
    """Turn leftovers from `hf download` (…/<hash>.<sha256>.<rand>.incomplete) into .part files.

    They are sequentially written prefixes of the file, so the largest one per file can
    be resumed; hash verification at the end catches anything bad. Then the hf cache
    folder (locks, metadata, other orphaned temp files) is removed.
    """
    hfdir = Path(d) / ".cache" / "huggingface"
    if not hfdir.exists():
        return 0
    incompletes = list(hfdir.rglob("*.incomplete"))
    adopted = 0
    for path, info in files.items():
        if (Path(d) / path).exists() or not info.get("sha256"):
            continue
        cands = [f for f in incompletes if f".{info['sha256']}." in f.name]
        cands = [(f.stat().st_size, f) for f in cands if f.stat().st_size <= info["size"]]
        if not cands:
            continue
        size, best = max(cands)
        pp = part_path(d, path)
        have = pp.stat().st_size if pp.exists() else 0
        if size > have:
            pp.parent.mkdir(parents=True, exist_ok=True)
            os.replace(best, pp)
            adopted += 1
            log(f"adopted hf partial for {path}: {size/1e9:.2f} GB")
    shutil.rmtree(hfdir, ignore_errors=True)
    log(f"removed {hfdir} ({len(incompletes)} temp files)")
    return adopted


def _key(info):
    return info["size"], info.get("sha256") or info.get("blob")


def _inside(d, path):
    p = (Path(d) / path).resolve()
    return Path(d).resolve() in p.parents


def download_file(repo, sha, d, path, info, stop, log, force=False):
    final = Path(d) / path
    pp = part_path(d, path)
    size = info["size"]
    if not force and final.exists() and final.stat().st_size == size:
        return "present"
    pp.parent.mkdir(parents=True, exist_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    have = pp.stat().st_size if pp.exists() else 0
    if have > size:
        pp.unlink(); have = 0
    h, want = _hasher(info)
    if have and h:  # re-hash the prefix we already have
        with open(pp, "rb") as f:
            left = have
            while left:
                if stop.is_set():
                    raise Cancelled()
                b = f.read(min(8 * CHUNK, left))
                if not b:
                    break
                h.update(b); left -= len(b)
    url = f"{HUB}/{repo}/resolve/{sha}/{urllib.parse.quote(path)}"
    fails = 0
    if have:
        log(f"resuming {path} at {have/1e9:.2f} GB")
    while have < size or (size == 0 and not pp.exists()):
        if stop.is_set():
            raise Cancelled()
        LIMITER.wait_unpaused(stop)
        try:
            with _open(url, {"Range": f"bytes={have}-"} if have else {}) as r:
                mode = "ab"
                if have and r.status == 200:  # server ignored the range: start over
                    have, mode = 0, "wb"
                    h, want = _hasher(info)
                with open(pp, mode) as f:
                    while True:
                        if stop.is_set():
                            raise Cancelled()
                        b = r.read(LIMITER.chunk())
                        if not b:
                            break
                        f.write(b)
                        if h:
                            h.update(b)
                        have += len(b)
                        LIMITER.wait(len(b), stop)
            if size == 0:
                break
            fails = 0
        except Paused:
            log(f"{path}: paused at {have/1e9:.2f} GB")
            continue  # connection closed by leaving the `with`; wait_unpaused() at the top
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404):
                raise
            fails += 1
            if fails > 10:
                raise
            wait = min(60, 2 ** fails)
            log(f"{path}: {type(e).__name__}: {e}; retry {fails}/10 in {wait}s")
            for _ in range(wait):
                if stop.is_set():
                    raise Cancelled()
                time.sleep(1)
    if have != size:
        raise VerifyError(f"{path}: got {have} bytes, expected {size}")
    if h and h.hexdigest() != want:
        pp.unlink(missing_ok=True)
        raise VerifyError(f"{path}: hash mismatch (deleted partial; will re-download on retry)")
    os.chmod(pp, 0o664)
    os.replace(pp, final)
    return "downloaded"


def download_all(repo, sha, d, files, stop, log, workers=4, on_verify=None, prev=None):
    """Download/verify every file of `files` (manifest at commit `sha`) into `d`.

    prev=(old_sha, old_files) turns this into an update: files whose hash is unchanged
    and that were verified at old_sha are trusted without re-hashing, changed files are
    re-downloaded (the old copy stays usable until the new one is swapped in), and files
    that no longer exist upstream are removed at the end.
    """
    d = Path(d)
    for p in files:
        if not _inside(d, p):
            raise VerifyError(f"refusing path outside the model folder: {p}")
    ok_marker = d / PART_DIR / f"verified-{sha}.txt"
    verified = set(ok_marker.read_text().splitlines()) if ok_marker.exists() else set()
    stale = set()
    if prev:
        old_sha, old_files = prev
        old_marker = d / PART_DIR / f"verified-{old_sha}.txt"
        old_ok = set(old_marker.read_text().splitlines()) if old_marker.exists() else set()
        for p, i in files.items():
            o = old_files.get(p)
            if o and _key(o) == _key(i) and p in old_ok and (d / p).exists():
                verified.add(p)
            elif o and _key(o) != _key(i):
                stale.add(p)
        log(f"update from {old_sha[:12]}: {len(stale)} changed, "
            f"{len([p for p in files if p not in old_files])} new, "
            f"{len([p for p in old_files if p not in files])} removed upstream")
    # verify files already in place (e.g. finished by an earlier `hf download`) once
    present = [p for p, i in files.items() if p not in verified and p not in stale
               and (d / p).exists() and (d / p).stat().st_size == i["size"]]
    for n, p in enumerate(present, 1):
        if on_verify:
            on_verify(f"verifying existing file {n}/{len(present)}: {p}")
        if hash_file(d / p, files[p], stop):
            verified.add(p)
        else:
            log(f"{p}: existing file failed hash check, re-downloading")
            (d / p).unlink()
    if on_verify:
        on_verify(None)
    ok_marker.parent.mkdir(parents=True, exist_ok=True)
    ok_marker.write_text("\n".join(sorted(verified)))

    todo = [p for p in files if p in stale or not ((d / p).exists() and (d / p).stat().st_size == files[p]["size"])]
    # small files first so config/tokenizer land quickly, then shards in order
    todo.sort(key=lambda p: (files[p]["size"] > 50e6, p))
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_file, repo, sha, d, p, files[p], stop, log, p in stale): p for p in todo}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                fut.result()
                verified.add(p)
                ok_marker.write_text("\n".join(sorted(verified)))  # survive restarts without re-hashing
                log(f"done {p}")
            except Cancelled:
                pass
            except Exception as e:
                errors.append(f"{p}: {e}")
                log(f"FAILED {p}: {e}")
                stop.set()
    ok_marker.write_text("\n".join(sorted(verified)))
    if errors:
        raise RuntimeError("; ".join(errors[:3]))
    if stop.is_set():
        raise Cancelled()
    if prev:
        old_sha, old_files = prev
        for p in old_files:
            if p not in files and _inside(d, p) and (d / p).is_file():
                (d / p).unlink()
                log(f"removed {p} (no longer in the repo)")
        (d / PART_DIR / f"verified-{old_sha}.txt").unlink(missing_ok=True)
    return len(todo)
