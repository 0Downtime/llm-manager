"""Access control: client IP allowlist + single-password login with persistent sessions.

- Password: PBKDF2-SHA256 hash in STATE_DIR/auth.json (0600). Set only from the CLI
  (`llm-manager passwd`), never over HTTP, so nobody on the LAN can claim it first.
- Sessions: random token in an HttpOnly/SameSite=Strict cookie; only its SHA-256 is
  stored (STATE_DIR/sessions.json), so restarts don't log you out and a leaked state
  file doesn't leak usable cookies.
- Login throttling: MAX_FAILS failures per IP per FAIL_WINDOW seconds -> locked.
"""
import hashlib, hmac, ipaddress, json, os, secrets, threading, time
from pathlib import Path

ITERATIONS = 600_000
SESSION_DAYS = 30
MAX_FAILS, FAIL_WINDOW = 5, 300
COOKIE = "llmm_session"
DEFAULT_NETS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7"


class Auth:
    def __init__(self, state_dir, enabled=True, nets=None):
        self.dir = Path(state_dir)
        self.enabled = enabled
        self.nets = [ipaddress.ip_network(n.strip(), strict=False)
                     for n in (nets or DEFAULT_NETS).split(",") if n.strip()]
        self.auth_file = self.dir / "auth.json"
        self.sess_file = self.dir / "sessions.json"
        self.lock = threading.Lock()
        self.fails = {}  # ip -> [timestamps]
        self.sessions = self._load_sessions()

    # ----- allowlist -----
    def ip_allowed(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in n for n in self.nets)

    # ----- password -----
    def has_password(self):
        return self.auth_file.exists()

    def set_password(self, pw):
        if len(pw) < 10:
            raise ValueError("password must be at least 10 characters")
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS)
        data = {"algo": "pbkdf2_sha256", "iterations": ITERATIONS, "salt": salt.hex(), "hash": h.hex(),
                "set": time.time()}
        _write_private(self.auth_file, json.dumps(data))
        with self.lock:  # a new password logs everyone out
            self.sessions = {}
            self._save_sessions()

    def check_password(self, pw):
        try:
            d = json.loads(self.auth_file.read_text())
        except (FileNotFoundError, ValueError):
            return False
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(d["salt"]), int(d["iterations"]))
        return hmac.compare_digest(h.hex(), d["hash"])

    # ----- throttling -----
    def locked_for(self, ip):
        now = time.time()
        with self.lock:
            f = [t for t in self.fails.get(ip, []) if now - t < FAIL_WINDOW]
            self.fails[ip] = f
            return int(FAIL_WINDOW - (now - f[0])) + 1 if len(f) >= MAX_FAILS else 0

    def note_fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    # ----- sessions -----
    def _load_sessions(self):
        try:
            s = json.loads(self.sess_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        now = time.time()
        return {k: v for k, v in s.items() if v.get("exp", 0) > now}

    def login(self, ip, pw):
        """-> (token, None) on success, (None, error message) otherwise."""
        wait = self.locked_for(ip)
        if wait:
            return None, f"Too many failed attempts. Try again in {wait} s."
        if not self.check_password(pw or ""):
            self.note_fail(ip)
            return None, "Wrong password."
        token = secrets.token_urlsafe(32)
        with self.lock:
            self._sync()
            self.fails.pop(ip, None)
            self.sessions[_digest(token)] = {"exp": time.time() + SESSION_DAYS * 86400, "ip": ip,
                                              "created": time.time()}
            self._save_sessions()
        return token, None

    def _sync(self):
        """Pick up changes made by another process (e.g. `llm-manager passwd` logging everyone out)."""
        try:
            m = self.sess_file.stat().st_mtime
        except FileNotFoundError:
            m = None
        if m != getattr(self, "_mtime", "unset"):
            self.sessions = self._load_sessions()
            self._mtime = m

    def _save_sessions(self):
        _write_private(self.sess_file, json.dumps(self.sessions))
        try:
            self._mtime = self.sess_file.stat().st_mtime
        except FileNotFoundError:
            pass

    def valid(self, token):
        if not token:
            return False
        with self.lock:
            self._sync()
            s = self.sessions.get(_digest(token))
            if not s:
                return False
            if s["exp"] < time.time():
                self.sessions.pop(_digest(token), None); self._save_sessions()
                return False
            return True

    def logout(self, token):
        if token:
            with self.lock:
                self._sync()
                if self.sessions.pop(_digest(token), None):
                    self._save_sessions()

    def cookie(self, token, max_age=SESSION_DAYS * 86400):
        return f"{COOKIE}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"

    @staticmethod
    def token_from(cookie_header):
        for part in (cookie_header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _write_private(path, text):
    tmp = Path(str(path) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
