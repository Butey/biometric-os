"""Auth primitives for the admin panel: password hashing, sessions, CSRF, rate
limiting. Stdlib only (hashlib, hmac, secrets, threading, time).

Session-timeout / TLS-behind-proxy knobs read from environment variables, not
config.yaml — config.yaml is owned by another agent right now and has no admin
section; env vars live in the same ~/.hermes/.env channel as the password hash.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSWORD_LEN = 12

ENV_PATH = Path.home() / ".hermes" / ".env"

def _session_timeout_sec() -> int:
    # Read lazily: load_env_file() runs in main(), after this module is imported.
    return int(os.environ.get("ADMIN_SESSION_TIMEOUT_MIN", "60")) * 60

# Same message for "wrong password" and "IP/globally locked" — a different
# message would let an attacker distinguish the two and time a retry.
LOGIN_ERROR_MSG = "Неверный пароль или вход временно заблокирован."
CODE_ERROR_MSG = "Неверный или просроченный код."
# Plain on purpose: no ids, no token, no hint which of the two things is missing.
CODE_SEND_ERROR_MSG = "Не удалось отправить код подтверждения, вход невозможен."
CODE_TTL_SEC = 300
CODE_MAX_ATTEMPTS = 5


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
            if not ip_times:
                self._per_ip.pop(ip, None)
            return len(ip_times) >= self.max_per_ip or len(self._global) >= self.global_max

    def record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            # ponytail: O(n) scan of per-IP keys per failure; fine at this scale
            for k in [k for k, v in self._per_ip.items() if not v or v[-1] < now - self.window_sec]:
                del self._per_ip[k]
            self._per_ip.setdefault(ip, []).append(now)
            self._global.append(now)

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._per_ip.pop(ip, None)

    def record_issue(self) -> None:
        """An issued login code = Telegram messages sent. Counted in the global
        window only (same 20 / 15 min as failures), so a correct password cannot
        be used as an unlimited message cannon. Not per-IP: a legit login must
        not eat the 5 wrong-code attempts of its own IP."""
        with self._lock:
            self._global.append(time.time())


class SessionStore:
    """token -> {"expires": epoch, "csrf": str, "authed": bool}. In-memory only
    — an admin-panel restart logging everyone out is an acceptable trade for a
    single-operator personal tool."""

    def __init__(self, timeout_sec: int | None = None):
        self._timeout_sec = timeout_sec
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def timeout_sec(self) -> int:
        return self._timeout_sec if self._timeout_sec is not None else _session_timeout_sec()

    def create(self, authed: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            # ponytail: O(n) scan of all sessions per create; index by expiry if n ever matters
            now = time.time()
            for t in [t for t, s in self._sessions.items() if s["expires"] < now]:
                del self._sessions[t]
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


# ---- second factor: one-time code delivered through the Telegram Bot API.
# Pending state lives in the session dict: sess["pending"] = {code, expires, attempts}.
# The sess dict is shared between handler threads, hence one lock for all pending ops.
_pending_lock = threading.Lock()


def _telegram_send(token: str, chat_id: str, text: str) -> None:
    """One sendMessage (same approach as scripts/notify.py). Self-check replaces this."""
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=8) as resp:
        if not json.loads(resp.read()).get("ok"):
            raise OSError("telegram refused the message")


def _admin_ids() -> list[str]:
    from health_core.config import load as load_config
    return [str(i) for i in (load_config().get("admin", {}) or {}).get("telegram_admin_ids") or []]


def begin_login_code(sess: dict, ip: str, limiter: "RateLimiter") -> bool:
    """Call after the password was verified. True = the code-entry page can be
    shown; False = fail closed (no admins / no token / Telegram refused everyone).

    A live pending code is never replaced and nothing is sent again: re-posting
    the password only re-shows the code form. A new code needs the old one to
    expire (or be used up), and every issue is counted by the rate limiter."""
    with _pending_lock:
        pending = sess.get("pending")
        if pending and pending["expires"] > time.time():
            return True
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        ids = _admin_ids()
        if not token or not ids:
            sess.pop("pending", None)
            return False
        code = f"{secrets.randbelow(10 ** 6):06d}"
        sess["pending"] = {"code": code, "expires": time.time() + CODE_TTL_SEC, "attempts": 0}
        limiter.record_issue()
    text = (
        "Запрошен вход в админ-панель Health Admin.\n"
        f"Код: {code}\n"
        f"Действует {CODE_TTL_SEC // 60} минут. IP запроса: {ip[:64]}.\n"
        "Если это были не вы - смените пароль администратора."
    )
    sent = 0
    for chat_id in ids:  # sent outside the lock: each call may take up to the timeout
        try:
            _telegram_send(token, chat_id, text)
            sent += 1
        except Exception as e:
            # Class name only: the message of a urllib error can carry the URL, i.e. the token.
            print(f"admin login code: send failed ({type(e).__name__})", file=sys.stderr)
    if not sent:
        with _pending_lock:
            sess.pop("pending", None)
    return bool(sent)


def code_pending(sess: dict | None) -> bool:
    with _pending_lock:
        pending = (sess or {}).get("pending")
        return bool(pending and pending["expires"] > time.time())


def check_login_code(sess: dict, submitted: str | None) -> bool:
    """True once, for the right live code. Wrong guess: counted; the CODE_MAX_ATTEMPTS-th
    wrong one, expiry and success all destroy the pending state (single use)."""
    with _pending_lock:
        pending = sess.get("pending")
        if not pending:
            return False
        if pending["expires"] <= time.time():
            del sess["pending"]
            return False
        pending["attempts"] += 1
        ok = hmac.compare_digest(pending["code"].encode(), (submitted or "").strip().encode())
        if ok or pending["attempts"] >= CODE_MAX_ATTEMPTS:
            del sess["pending"]
        return ok


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

    # --- 8. second factor. `python -m admin.auth` runs this file as __main__, while
    # admin.server imports a separate admin.auth: patch the imported copy (A). ---
    import admin.auth as A

    sent = []  # (chat_id, text)
    send_mode = {"fail": False}

    def fake_send(token, chat_id, text):
        if send_mode["fail"]:
            raise OSError("boom")
        sent.append((chat_id, text))

    A._telegram_send = fake_send
    A._admin_ids = lambda: ["900111222", "900333444"]
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"

    def fresh():
        sent.clear()
        send_mode["fail"] = False
        return {"csrf": "c", "authed": False}, A.RateLimiter()

    sess, lim = fresh()
    assert A.begin_login_code(sess, "198.51.100.1", lim)
    assert [c for c, _ in sent] == ["900111222", "900333444"], "one message per admin id"
    code = sess["pending"]["code"]
    assert len(code) == 6 and code.isdigit() and code in sent[0][1] and "198.51.100.1" in sent[0][1]
    assert "смените пароль" in sent[0][1]
    assert sess["authed"] is False, "the password step must not authenticate"
    # a live code is not replaced and nothing is sent again
    assert A.begin_login_code(sess, "198.51.100.1", lim) and len(sent) == 2
    assert sess["pending"]["code"] == code
    assert len(lim._global) == 1, "one issue counted"
    print("OK: code issued once per admin id, session still unauthenticated, no re-issue while pending")

    assert not A.check_login_code(sess, "000000" if code != "000000" else "000001")
    assert not A.check_login_code(sess, "\u0663\u0662\u0661\u0660\u0661\u0662"), "non-ASCII must not raise"
    assert sess["pending"]["attempts"] == 2
    assert A.check_login_code(sess, code), "right code accepted"
    assert "pending" not in sess
    assert not A.check_login_code(sess, code), "a used code cannot be used twice"
    print("OK: right code accepted once, reuse rejected")

    sess, lim = fresh()
    A.begin_login_code(sess, "x", lim)
    code = sess["pending"]["code"]
    wrong = "000000" if code != "000000" else "000001"
    for i in range(5):
        assert "pending" in sess
        assert not A.check_login_code(sess, wrong)
    assert "pending" not in sess, "5 wrong codes destroy the pending state"
    assert not A.check_login_code(sess, code), "even the right code is dead after that"
    sess, lim = fresh()
    A.begin_login_code(sess, "x", lim)
    sess["pending"]["expires"] = time.time() - 1
    assert not A.check_login_code(sess, sess["pending"]["code"]) and "pending" not in sess
    print("OK: 5 wrong codes and expiry destroy the pending state")

    sess, lim = fresh()
    sess["pending"] = {"code": "000042", "expires": time.time() + 99, "attempts": 0}
    assert A.check_login_code(sess, " 000042 "), "leading zeros kept, whitespace trimmed"
    print("OK: leading-zero code verified")

    sess, lim = fresh()
    A._admin_ids = lambda: []
    assert not A.begin_login_code(sess, "x", lim) and not sent and "pending" not in sess
    A._admin_ids = lambda: ["900111222", "900333444"]
    os.environ.pop("TELEGRAM_BOT_TOKEN")
    assert not A.begin_login_code(sess, "x", lim) and not sent and "pending" not in sess
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    send_mode["fail"] = True
    assert not A.begin_login_code(sess, "x", lim) and "pending" not in sess
    send_mode["fail"] = False
    print("OK: no admin ids / no token / every send failing -> fail closed, no pending state")

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

            import admin.server as srv

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None

            def client():
                jar = CookieJar()
                op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)
                return jar, op

            def post(op, path, **fields):
                try:
                    r = op.open(urllib.request.Request(
                        base + path, data=urllib.parse.urlencode(fields).encode(), method="POST"), timeout=5)
                    return r.status, r.read().decode("utf-8"), r.headers
                except urllib.error.HTTPError as e:
                    return e.code, e.read().decode("utf-8", errors="replace"), e.headers

            def get(op, path):
                try:
                    r = op.open(base + path, timeout=5)
                    return r.status, r.read().decode("utf-8"), r.headers
                except urllib.error.HTTPError as e:
                    return e.code, "", e.headers

            def start():
                jar, op = client()
                _, body, _ = get(op, "/login")
                return jar, op, re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

            def reset():
                srv.RATE_LIMITER = A.RateLimiter()
                sent.clear()
                send_mode["fail"] = False

            def last_code():
                return re.search(r"Код: (\d{6})", sent[-1][1]).group(1)

            reset()
            # wrong password -> nothing sent
            jar, op, csrf = start()
            st, body, _ = post(op, "/login", password="nope", csrf_token=csrf)
            assert st == 200 and "Неверный пароль" in body and not sent
            print("OK: wrong password sends nothing")

            # right password -> one message per admin id, code form, NOT authenticated
            st, body, _ = post(op, "/login", password=password, csrf_token=csrf)
            assert st == 200 and 'action="/login/code"' in body and len(sent) == 2
            for path in ("/", "/guards", "/milestones", "/thresholds", "/keys", "/personas", "/actions",
                         "/knowledge", "/drafts", "/alerts", "/plans", "/workouts", "/forecast"):
                st, _, h = get(op, path)
                assert st == 303 and h["Location"] == "/login", f"pending session reached GET {path}: {st}"
            for path in ("/guards/save", "/milestones", "/thresholds", "/keys", "/keys/test", "/personas",
                         "/actions", "/actions/import-scale", "/knowledge/upload", "/knowledge/delete",
                         "/plans/save", "/plans/delete", "/plans/template/save", "/plans/template/delete",
                         "/workouts/save", "/workouts/delete", "/drafts"):
                st, _, h = post(op, path, csrf_token=csrf)
                assert st == 303 and h["Location"] == "/login", f"pending session reached POST {path}: {st}"
            print("OK: right password sends one message per admin id; pending session is refused by every protected route")

            # code form: CSRF required, wrong code counted, right code authenticates + rotates the cookie
            st, _, _ = post(op, "/login/code", code=last_code())
            assert st == 403, "code form without CSRF token"
            n_fail = len(srv.RATE_LIMITER._per_ip.get("127.0.0.1", []))
            st, body, _ = post(op, "/login/code", code="12345x", csrf_token=csrf)
            assert st == 200 and "Неверный или просроченный код" in body
            assert len(srv.RATE_LIMITER._per_ip["127.0.0.1"]) == n_fail + 1, "wrong code counted like a wrong password"
            before = [c.value for c in jar if c.name == "session"]
            st, body, _ = post(op, "/login/code", code=last_code(), csrf_token=csrf)
            after = [c.value for c in jar if c.name == "session"]
            assert st == 200 and "Вход выполнен" in body and after != before, "token must rotate"
            st, body, _ = get(op, "/")
            assert st == 200 and "сегодня" in body.lower()
            op_authed = op
            print("OK: right code authenticates, session token rotates, dashboard opens, CSRF enforced on the code form")

            # reused code on a fresh session cannot be replayed on another one
            reset()
            jar1, op1, csrf1 = start()
            post(op1, "/login", password=password, csrf_token=csrf1)
            code1 = last_code()
            jar2, op2, csrf2 = start()
            st, body, _ = post(op2, "/login/code", code=code1, csrf_token=csrf2)
            assert "Неверный или просроченный код" in body and get(op2, "/")[0] == 303
            post(op1, "/login/code", code=code1, csrf_token=csrf1)
            st, body, _ = post(op1, "/login/code", code=code1, csrf_token=csrf1)
            assert "Неверный или просроченный код" in body, "used code rejected the second time"
            print("OK: a code works only in its own session and only once")

            # five wrong codes through HTTP: pending destroyed, right code no longer works
            reset()
            jar, op, csrf = start()
            post(op, "/login", password=password, csrf_token=csrf)
            good = last_code()
            wrong = "000000" if good != "000000" else "000001"
            for _ in range(5):
                post(op, "/login/code", code=wrong, csrf_token=csrf)
            st, body, _ = post(op, "/login/code", code=good, csrf_token=csrf)
            assert 'name="password"' in body or "заблокирован" in body
            assert get(op, "/")[0] == 303
            print("OK: five wrong codes over HTTP kill the code (lockout applies as for passwords)")

            # expired code
            reset()
            srv.RATE_LIMITER = A.RateLimiter(max_per_ip=50)
            jar, op, csrf = start()
            post(op, "/login", password=password, csrf_token=csrf)
            good = last_code()
            for s_ in srv.SESSIONS._sessions.values():
                if "pending" in s_:
                    s_["pending"]["expires"] = time.time() - 1
            st, body, _ = post(op, "/login/code", code=good, csrf_token=csrf)
            assert 'name="password"' in body and get(op, "/")[0] == 303
            print("OK: expired code rejected, back to the password step")

            # repeated correct passwords on one session do not resend; new sessions are bounded globally
            reset()
            srv.RATE_LIMITER = A.RateLimiter(max_per_ip=50, global_max=3)
            jar, op, csrf = start()
            for _ in range(3):
                post(op, "/login", password=password, csrf_token=csrf)
            assert len(sent) == 2, "same session: one issue only"
            for _ in range(6):
                j_, o_, c_ = start()
                post(o_, "/login", password=password, csrf_token=c_)
            assert len(sent) == 2 * 3, f"global cap of 3 issues, got {len(sent) // 2}"
            print("OK: no re-issue while a code is pending; fresh sessions are capped by the global limiter")

            # fail closed over HTTP
            reset()
            A._admin_ids = lambda: []
            jar, op, csrf = start()
            st, body, _ = post(op, "/login", password=password, csrf_token=csrf)
            assert "Не удалось отправить код" in body and 'action="/login/code"' not in body and not sent
            A._admin_ids = lambda: ["900111222", "900333444"]
            send_mode["fail"] = True
            st, body, _ = post(op, "/login", password=password, csrf_token=csrf)
            assert "Не удалось отправить код" in body and "900111222" not in body and "test-token" not in body
            assert get(op, "/")[0] == 303
            print("OK: no admin ids / Telegram failing for everyone -> plain error page, no way in")
            reset()

            # POST without CSRF -> 403
            try:
                op_authed.open(
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
