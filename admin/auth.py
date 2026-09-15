"""Auth primitives for the admin panel: password hashing, sessions, CSRF, rate
limiting. Stdlib only (hashlib, hmac, secrets, threading, time).

Session-timeout / TLS-behind-proxy knobs read from environment variables, not
config.yaml — config.yaml is owned by another agent right now and has no admin
section; env vars live in the same ~/.hermes/.env channel as the password hash.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSWORD_LEN = 12

ENV_PATH = Path.home() / ".hermes" / ".env"

SESSION_TIMEOUT_SEC = int(os.environ.get("ADMIN_SESSION_TIMEOUT_MIN", "60")) * 60

# Same message for "wrong password" and "IP/globally locked" — a different
# message would let an attacker distinguish the two and time a retry.
LOGIN_ERROR_MSG = "Неверный пароль или вход временно заблокирован."


def load_env_file(path: Path = ENV_PATH, force: bool = False) -> None:
    """Populate os.environ from a KEY=VALUE file. If force=True, updates os.environ
    even if the variable was already present (used after saving keys in admin panel)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Пустую переменную тоже заполняем: ключ, добавленный в админке, иначе
        # не дойдёт до уже запущенного бота без перезапуска.
        if key and (force or not os.environ.get(key)):
            os.environ[key] = _strip_inline_comment(value)


def read_env_vars(path: Path = ENV_PATH) -> dict[str, str]:
    """Read all key-value pairs from .env without mutating os.environ."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = _strip_inline_comment(value)
    return out


def update_env_vars(updates: dict[str, str | None], path: Path = ENV_PATH) -> None:
    """Update or append variables in .env atomically with 0600 permissions,
    preserving comments and existing variables. Also updates os.environ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    remaining_updates = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key, _, old_val = stripped.partition("=")
        key = key.strip()
        if key in remaining_updates:
            new_val = remaining_updates.pop(key)
            if new_val is not None:
                new_lines.append(f"{key}={new_val}")
                os.environ[key] = new_val
            else:
                os.environ.pop(key, None)
        else:
            new_lines.append(line)

    for key, new_val in remaining_updates.items():
        if new_val is not None:
            new_lines.append(f"{key}={new_val}")
            os.environ[key] = new_val
        else:
            os.environ.pop(key, None)

    # Write atomically via temp file with 0600 permissions
    import tempfile
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tf:
        tf.write("\n".join(new_lines) + "\n")
        tmp_name = tf.name
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)


def _strip_inline_comment(value: str) -> str:
    """Отрезает хвостовой комментарий: `374939064   # Comma-separated IDs`.

    Без этого весь хвост уезжает в значение. Живой случай: TELEGRAM_ALLOWED_USERS
    превратился в строку с комментарием, ни один telegram id с ней не совпал, и
    бот молча игнорировал единственного пользователя — снаружи это выглядит как
    «бот сломался», а в логе ровно ничего.

    Режем только `#` после пробела — таково правило .env-файлов и единственное
    безопасное: `#` внутри значения (пароль, токен) остаётся на месте."""
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i].strip()
    return value.strip()


def gen_salt() -> bytes:
    return secrets.token_bytes(16)


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=64
    )


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    """Constant-time compare against the stored hash — hmac.compare_digest, never ==."""
    computed_hex = hash_password(password, bytes.fromhex(salt_hex)).hex()
    return hmac.compare_digest(computed_hex, expected_hash_hex)


def client_ip(remote_addr: str, xff_header: str | None) -> str:
    """Real client IP for rate limiting. X-Forwarded-For is trusted ONLY when
    ADMIN_BEHIND_TLS is set (a reverse proxy is known to sit in front — see
    admin/README-proxy.md) — and even then, only the LAST hop: the entry the
    immediate trusted proxy appended itself, never a client-supplied value
    further left in a spoofed chain."""
    if os.environ.get("ADMIN_BEHIND_TLS") and xff_header:
        last = xff_header.split(",")[-1].strip()
        if last:
            return last
    return remote_addr


class RateLimiter:
    """5 failures per IP / 15 min locks that IP; a global cap of 20 failed
    logins / 15 min locks the login form for everyone (defends against
    distributed brute force spread across many IPs).

    ponytail: sliding window via prune-on-read, process-local dict — no
    cleanup thread, resets on restart. Fine at single-admin-panel scale;
    upgrade to a persistent store only if this ever serves more than one
    operator.
    """

    def __init__(self, max_per_ip: int = 5, global_max: int = 20, window_sec: int = 900):
        self.max_per_ip = max_per_ip
        self.global_max = global_max
        self.window_sec = window_sec
        self._per_ip: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._lock = threading.Lock()

    def _prune(self, times: list[float], now: float) -> None:
        cutoff = now - self.window_sec
        while times and times[0] < cutoff:
            times.pop(0)

    def is_locked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            self._prune(self._global, now)
            ip_times = self._per_ip.get(ip, [])
            self._prune(ip_times, now)
            return len(ip_times) >= self.max_per_ip or len(self._global) >= self.global_max

    def record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._per_ip.setdefault(ip, []).append(now)
            self._global.append(now)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._per_ip.pop(ip, None)


class SessionStore:
    """token -> {"expires": epoch, "csrf": str, "authed": bool}. In-memory only
    — an admin-panel restart logging everyone out is an acceptable trade for a
    single-operator personal tool."""

    def __init__(self, timeout_sec: int = SESSION_TIMEOUT_SEC):
        self.timeout_sec = timeout_sec
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, authed: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "expires": time.time() + self.timeout_sec,
                "csrf": secrets.token_urlsafe(32),
                "authed": authed,
                # Remembers which persona (users.id) this admin picked via the
                # nav selector (admin/server.py::_resolve_user_id), so plain
                # links keep working without a `?user=` on every request.
                # None until a selection is made or a fallback is resolved.
                "selected_user_id": None,
            }
        return token

    def get(self, token: str | None) -> dict | None:
        """Valid, non-expired session dict, or None. Touches (renews) the idle
        timeout on access — that's what makes it an *idle* timeout."""
        if not token:
            return None
        with self._lock:
            sess = self._sessions.get(token)
            if sess is None:
                return None
            if sess["expires"] < time.time():
                del self._sessions[token]
                return None
            sess["expires"] = time.time() + self.timeout_sec
            return sess

    def authenticate(self, token: str) -> str:
        """Marks a session authenticated, rotating its token (session-fixation
        defense: a token an attacker saw pre-login becomes worthless). Returns
        the new token; raises KeyError if the old token is gone/expired."""
        with self._lock:
            sess = self._sessions.pop(token, None)
            if sess is None:
                raise KeyError(token)
            sess["authed"] = True
            sess["expires"] = time.time() + self.timeout_sec
            new_token = secrets.token_urlsafe(32)
            self._sessions[new_token] = sess
            return new_token

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


def csrf_ok(session: dict | None, submitted: str | None) -> bool:
    if session is None or not submitted:
        return False
    return hmac.compare_digest(session["csrf"], submitted)


if __name__ == "__main__":
    import html
    import re
    import sys
    import tempfile
    import threading as _threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from datetime import datetime
    from http.cookiejar import CookieJar
    from pathlib import Path as _Path

    sys.stdout.reconfigure(encoding="utf-8")

    # --- 1. scrypt hash of a known password verifies; a wrong password does not ---
    salt = gen_salt()
    good_hash = hash_password("correct horse battery staple!", salt).hex()
    assert verify_password("correct horse battery staple!", salt.hex(), good_hash)
    assert not verify_password("wrong password entirely!!", salt.hex(), good_hash)
    print("OK: scrypt hash verifies the correct password, rejects a wrong one")

    # --- 2. compare_digest used; no == against the stored hash anywhere in this file ---
    src = Path(__file__).read_text(encoding="utf-8")
    assert "compare_digest" in src
    forbidden = re.compile(r"(computed\w*\s*==|==\s*expected_hash|stored_hash\s*==|==\s*stored_hash)")
    assert not forbidden.search(src), "found a == comparison against a hash variable"
    print("OK: hash comparisons go through hmac.compare_digest, no == against a hash found")

    # --- 3. session token expires after its idle timeout and is rejected afterwards ---
    store = SessionStore(timeout_sec=1)
    tok = store.create(authed=True)
    assert store.get(tok) is not None
    time.sleep(1.2)
    assert store.get(tok) is None, "expired session should be rejected"
    print("OK: session token expires after its idle timeout and is then rejected")

    # --- 4. CSRF: POST body without the token rejected, with it accepted ---
    store2 = SessionStore(timeout_sec=60)
    tok2 = store2.create(authed=True)
    sess2 = store2.get(tok2)
    real_csrf = sess2["csrf"]
    assert not csrf_ok(sess2, None)
    assert not csrf_ok(sess2, "garbage-token")
    assert csrf_ok(sess2, real_csrf)
    print("OK: CSRF check rejects missing/wrong token, accepts the real one")

    # --- 5. rate limiter locks after exactly 5 failures, refuses the 6th ---
    rl = RateLimiter(max_per_ip=5, global_max=1000, window_sec=900)
    probe_ip = "203.0.113.7"
    for i in range(5):
        assert not rl.is_locked(probe_ip), f"should not be locked before failure {i + 1}"
        rl.record_failure(probe_ip)
    assert rl.is_locked(probe_ip), "must be locked after exactly 5 failures"
    print("OK: rate limiter locks after exactly 5 failures, refuses the 6th attempt")

    # --- 6. html.escape() neutralises <script> in a milestone-name-shaped string ---
    dangerous = "<script>alert(1)</script>"
    escaped = html.escape(dangerous)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    print("OK: html.escape() neutralises <script>alert(1)</script>")

    # --- 7. /thresholds surgical edit preserves every comment line (regression:
    # yaml.dump over the live config destroyed all 39 Russian comments, twice).
    # Never touches the real config.yaml — copies it into a temp dir first and
    # points health_core.config.CONFIG_PATH at the copy for the duration. ---
    import json
    import shutil

    import health_core.config as _cfgmod
    from plugin.tools import _admin_set

    real_cfg_path = _cfgmod.CONFIG_PATH
    real_cfg_before = real_cfg_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as cfg_tmp:
        copy_path = _Path(cfg_tmp) / "config.yaml"
        shutil.copy2(real_cfg_path, copy_path)
        original_text = copy_path.read_text(encoding="utf-8")
        comments_before = sum(1 for ln in original_text.splitlines() if ln.strip().startswith("#"))

        _cfgmod.CONFIG_PATH = copy_path
        _cfgmod.load.cache_clear()
        try:
            result = json.loads(_admin_set("keep_copies", "9"))
            assert "error" not in result, f"_admin_set failed: {result}"
            new_text = copy_path.read_text(encoding="utf-8")
            comments_after = sum(1 for ln in new_text.splitlines() if ln.strip().startswith("#"))
            assert comments_after == comments_before, (
                f"comment count changed: {comments_before} -> {comments_after} "
                "(a surgical edit must never touch comment lines)"
            )
            assert new_text != original_text, "the edited value should have actually changed on disk"
            assert _cfgmod.load()["backup"]["keep_copies"] == 9
        finally:
            _cfgmod.CONFIG_PATH = real_cfg_path
            _cfgmod.load.cache_clear()
    real_cfg_after = real_cfg_path.read_text(encoding="utf-8")
    assert real_cfg_after == real_cfg_before, "the self-check must never touch the real config.yaml"
    print(f"OK: /thresholds surgical edit preserves all {comments_before} comment lines; real config.yaml untouched")

    # --- integration: real server on an ephemeral localhost port, driven by urllib ---
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HEALTH_DB"] = str(_Path(tmp) / "health.db")
        os.environ["ADMIN_SESSION_TIMEOUT_MIN"] = "60"
        password = "integration-test-pw12"
        salt3 = gen_salt()
        os.environ["ADMIN_PASSWORD_HASH"] = hash_password(password, salt3).hex()
        os.environ["ADMIN_PASSWORD_SALT"] = salt3.hex()
        os.environ.pop("ADMIN_BEHIND_TLS", None)

        import health_core.db as _db

        _db.DB_PATH = _Path(os.environ["HEALTH_DB"])
        conn = _db.connect()
        _db.migrate(conn)
        conn.execute(
            "INSERT INTO users(telegram_user_id, height_cm, birth_date, sex, created_at) "
            "VALUES (1, 185, '1990-01-01', 'm', ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
        )
        conn.commit()
        conn.close()  # closed before the server (and later the temp dir) touch it again

        from admin.server import build_server

        httpd = build_server(("127.0.0.1", 0))
        port = httpd.server_address[1]
        thread = _threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"

            # unauthenticated "/" -> redirect or 401, NEVER 200 with dashboard data
            try:
                resp = urllib.request.urlopen(base + "/", timeout=5)
                body = resp.read().decode("utf-8", errors="replace")
                assert resp.status != 200 or "сегодня" not in body.lower(), (
                    "unauthenticated / must never return 200 with dashboard data"
                )
            except urllib.error.HTTPError as e:
                assert e.code in (302, 303, 401), f"unexpected status {e.code}"
            print("OK: unauthenticated / never returns 200 with dashboard data")

            cj = CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

            # GET /login to obtain a pre-session cookie + csrf token
            resp = opener.open(base + "/login", timeout=5)
            login_body = resp.read().decode("utf-8")
            m = re.search(r'name="csrf_token" value="([^"]+)"', login_body)
            assert m, "login form missing csrf token"
            login_csrf = m.group(1)

            # login with the right password -> 200, session cookie set
            data = urllib.parse.urlencode({"password": password, "csrf_token": login_csrf}).encode()
            resp = opener.open(urllib.request.Request(base + "/login", data=data, method="POST"), timeout=5)
            assert resp.status == 200, f"login expected 200, got {resp.status}"
            assert any(c.name == "session" for c in cj), "no session cookie set after login"
            print("OK: login with the right password returns 200 and sets a session cookie")

            # "/" with the cookie -> 200 containing the status bar
            resp = opener.open(base + "/", timeout=5)
            body = resp.read().decode("utf-8")
            assert resp.status == 200
            assert "сегодня" in body.lower(), "dashboard should contain the status bar"
            print("OK: authenticated / returns 200 containing the status bar")

            # POST without CSRF -> 403
            try:
                opener.open(
                    urllib.request.Request(base + "/actions", data=b"action=recalc", method="POST"), timeout=5
                )
                assert False, "POST without CSRF should have failed"
            except urllib.error.HTTPError as e:
                assert e.code == 403, f"expected 403, got {e.code}"
            print("OK: POST without CSRF token is rejected with 403")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    print("OK: admin.auth self-check passed")
