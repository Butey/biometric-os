"""Stdlib-only admin HTTP server: http.server.ThreadingHTTPServer + one
BaseHTTPRequestHandler. No Flask/FastAPI/uvicorn/jinja2 — the VPS has 1 GB RAM
and Hermes already eats 250-400 MB; html is admin/pages.py f-strings.

Binds 127.0.0.1 by default. Normal access is an ssh tunnel:

    ssh -L 8765:localhost:8765 user@vps

A non-loopback --host is refused unless --i-know-this-is-exposed is also
passed (see _refuse_exposed_bind). Even then, this is plain HTTP: put a TLS
reverse proxy in front (admin/README-proxy.md) and set ADMIN_BEHIND_TLS=1
before ever binding somewhere reachable off-box.
"""
import argparse
import contextlib
import getpass
import io
import ipaddress
import json
import os
import sys
import tempfile
import traceback
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root: for `migrate` and `plugin`

from admin import auth, pages, upload
from health_core import card_drafts
from health_core.config import CONFIG_PATH, load as load_config, user_today
from health_core.db import DB_PATH, connect, migrate as db_migrate
_connect = connect
from health_core.energy import daily_target
from health_core.export import backup_db, export_all
from health_core.guards import check_all, get_guards_status
from health_core.report import status_bar, trends, day_summary, meals_of_day, baseline_weight
from plugin.tools import _admin_set, handle_set_milestone

SESSIONS = auth.SessionStore()
RATE_LIMITER = auth.RateLimiter()

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "Knowledge"  # тот же каталог, что читает knowledge.index()

_VALID_MILESTONE_METRICS = ("weight_kg", "ffm_kg", "fat_pct", "waist")  # must match plugin/tools.py

# Таблицы с прямым user_id, ссылающимся на users(id) — взято построчным grep'ом
# "user_id INTEGER" по health_core/db.py DDL. ОБЯЗАТЕЛЬНО дополнять этот список
# при добавлении новой per-user таблицы в схему — забытая здесь таблица оставит
# осиротевшие строки медицинских данных после удаления персоны. food_items сюда
# не входит: он ссылается на food_log(id), а не на user_id напрямую, и удаляется
# отдельным запросом в _delete_persona ниже (см. также FK-каскад в комментарии там).
PERSONA_TABLES = (
    "user_targets", "milestones", "body_metrics", "anthropometry", "food_log",
    "water_log", "glucose_log", "activity", "med_schedule", "pantry",
    "daily_targets", "alerts", "med_log", "llm_calls", "import_log",
    "refeed_days", "meal_plan", "workout_plan", "persona_styles", "plan_log",
    "sick_days", "lab_results", "dispatch_log", "side_effects", "my_products",
    "daily_watch",
)


def _delete_persona(conn, target_user_id: int) -> list[tuple[str, int]]:
    """Удаляет одну персону и ВСЕ её данные в одной транзакции. Вызывающий обязан
    сам вызвать conn.commit()/conn.rollback() — эта функция только выполняет DELETE'ы.
    food_items удаляется явным JOIN'ом по food_log_id, а не через ON DELETE CASCADE:
    connect() в health_core/db.py действительно включает PRAGMA foreign_keys=ON, так
    что каскад сработал бы и сам, но по заданию мы на него не полагаемся — удаляем
    явно, чтобы поведение не зависело от PRAGMA конкретного соединения."""
    cur = conn.cursor()
    counts: list[tuple[str, int]] = []
    cur.execute(
        "DELETE FROM food_items WHERE food_log_id IN (SELECT id FROM food_log WHERE user_id=?)",
        (target_user_id,),
    )
    counts.append(("food_items", cur.rowcount))
    for table in PERSONA_TABLES:
        cur.execute(f"DELETE FROM {table} WHERE user_id=?", (target_user_id,))
        counts.append((table, cur.rowcount))

    # user_mode стоит особняком: он ключуется по telegram_user_id, а не по
    # user_id, поэтому в PERSONA_TABLES не попадает, и создаётся лениво (см.
    # _mode_table_ready в plugin/tools.py) — то есть может ещё не существовать.
    # Оставить строку здесь опаснее, чем осиротевшие данные: тот же телеграм-id,
    # зарегистрировавшись заново, унаследовал бы прежний режим admin.
    trow = cur.execute("SELECT telegram_user_id FROM users WHERE id=?", (target_user_id,)).fetchone()
    if trow is not None:
        exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_mode'"
        ).fetchone()
        if exists is not None:
            cur.execute("DELETE FROM user_mode WHERE telegram_user_id=?", (str(trow[0]),))
            counts.append(("user_mode", cur.rowcount))
        # Допуск тоже по telegram id: удалённая персона, написав снова, должна
        # пройти одобрение заново, а не молча вернуться с прежним approved.
        cur.execute("DELETE FROM access_list WHERE telegram_user_id=?", (str(trow[0]),))
        counts.append(("access_list", cur.rowcount))

    cur.execute("DELETE FROM users WHERE id=?", (target_user_id,))
    counts.append(("users", cur.rowcount))
    return counts


def _get_user_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _user_exists(conn, user_id: int) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is not None


def _resolve_user_id(conn, sess: dict, path: str) -> int | None:
    """Selected persona for this request. Priority: ?user=<id> query param (when
    it names a real row — remembered on the session for next time), else the id
    already remembered on the admin's session (when it still exists), else the
    first user in the DB (also remembered, so the fallback is sticky too).

    This is the ONLY place that should decide "which user" outside of pages that
    intentionally list every persona (personas_page, the nav selector itself) —
    every handler gets user_id from _require_auth()'s return value, never by
    re-querying `users` directly."""
    query = urllib.parse.urlparse(path).query
    raw = urllib.parse.parse_qs(query).get("user", [None])[0]
    if raw is not None:
        try:
            requested = int(raw)
        except ValueError:
            requested = None
        if requested is not None and _user_exists(conn, requested):
            sess["selected_user_id"] = requested
            return requested

    remembered = sess.get("selected_user_id")
    if remembered is not None and _user_exists(conn, remembered):
        return remembered

    fallback = _get_user_id(conn)
    sess["selected_user_id"] = fallback
    return fallback


def _list_users_for_selector(conn) -> list[dict]:
    """All personas for the nav <select>: id, telegram_user_id, and username
    from access_list when that table exists. access_list is created by the v17
    migration that db_migrate() already ran by the time this is called, but we
    tolerate its absence anyway (defensive — another agent owns that schema)."""
    rows = conn.execute("SELECT id, telegram_user_id FROM users ORDER BY id").fetchall()
    has_access_list = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='access_list'"
    ).fetchone() is not None
    out = []
    for r in rows:
        username = None
        if has_access_list:
            urow = conn.execute(
                "SELECT username FROM access_list WHERE telegram_user_id=?",
                (str(r["telegram_user_id"]),),
            ).fetchone()
            username = urow["username"] if urow else None
        out.append({"id": r["id"], "telegram_user_id": r["telegram_user_id"], "username": username})
    return out


def _run_action(conn, user_id: int, action: str) -> str:
    """Runs one long admin action synchronously (no job queue, no websockets —
    matches the brief). Never lets an exception reach the browser as a stack
    trace: logs it to stderr, returns a generic message."""
    try:
        if action == "export":
            out_dir = os.environ.get("HEALTH_EXPORT_DIR", str(DB_PATH.parent / "export"))
            result = export_all(conn, user_id, out_dir)
            return f"Экспорт: {result['rows']} строк, {len(result['files'])} файлов -> {out_dir}"
        if action == "backup":
            backup_dir = os.environ.get("HEALTH_BACKUP_DIR", str(DB_PATH.parent / "backups"))
            path = backup_db(str(DB_PATH), backup_dir)
            return f"Бэкап создан: {path}"
        if action == "recalc":
            today = user_today(conn, user_id)
            result = daily_target(conn, user_id, today)
            return f"Цель на {today}: {result['kcal']:.0f} ккал (source={result['source']})"
        if action == "import":
            return _run_historical_import(conn, user_id)
        return "Неизвестное действие."
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return "Действие не выполнено — подробности в логах сервера."


def _run_historical_import(conn, user_id: int) -> str:
    scale_path = os.environ.get("HEALTH_SCALE_IMPORT_PATH")
    tcx_dir = os.environ.get("HEALTH_TCX_IMPORT_DIR")
    anthro_path = os.environ.get("HEALTH_ANTHRO_IMPORT_PATH")
    if not (scale_path or tcx_dir or anthro_path):
        return (
            "Исторический импорт не настроен: задайте HEALTH_SCALE_IMPORT_PATH / "
            "HEALTH_TCX_IMPORT_DIR / HEALTH_ANTHRO_IMPORT_PATH в ~/.hermes/.env"
        )
    import migrate as _migrate_mod  # существующий скрипт репозитория — переиспользуем как есть, не дублируем

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if scale_path:
            _migrate_mod.run_scale(conn, user_id, scale_path)
        if tcx_dir:
            _migrate_mod.run_tcx(conn, user_id, tcx_dir)
        if anthro_path:
            _migrate_mod.run_anthro(conn, user_id, anthro_path)
    conn.commit()
    return buf.getvalue() or "Импорт выполнен (стадии не дали вывода)."


_SCALE_UPLOAD_SUFFIXES = (".xlsx", ".xls", ".csv", ".tcx", ".zip")


def _import_scale_upload(conn, user_id: int, filename: str, data: bytes) -> tuple[bool, str]:
    """Загружает выгрузку весов Feelfit, TCX-тренировку или ZIP-архив (Mi Fitness / выгрузки)
    из браузера и импортирует их через функции модуля migrate."""
    suffix = Path(filename.replace("\\", "/")).suffix.lower()
    if suffix not in _SCALE_UPLOAD_SUFFIXES:
        return False, (
            f"Расширение «{suffix or '(нет)'}» не поддерживается — только "
            f"{', '.join(_SCALE_UPLOAD_SUFFIXES)}."
        )
    # Потолок для zip и выгрузок — 50 МБ
    max_bytes = 50 * 1024 * 1024
    if len(data) > max_bytes:
        return False, f"Файл больше {max_bytes // (1024 * 1024)} МБ — отклонён."

    try:
        import migrate as _migrate_mod  # существующий скрипт репозитория — переиспользуем как есть, не дублируем
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / ("upload" + suffix)
            tmp_path.write_bytes(data)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                if suffix == ".zip":
                    import zipfile
                    extract_dir = Path(tmp_dir) / "unzipped"
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    extract_dir_resolved = extract_dir.resolve()
                    with zipfile.ZipFile(tmp_path, "r") as zf:
                        for member in zf.infolist():
                            member_path = (extract_dir / member.filename).resolve()
                            if not member_path.is_relative_to(extract_dir_resolved):
                                continue  # zip slip: entry would escape extract_dir — skip it
                            zf.extract(member, extract_dir)
                    _migrate_mod.run_scale(conn, user_id, str(extract_dir))
                    _migrate_mod.run_tcx(conn, user_id, str(extract_dir))
                elif suffix == ".tcx":
                    _migrate_mod.run_tcx(conn, user_id, str(tmp_path))
                else:
                    _migrate_mod.run_scale(conn, user_id, tmp_dir)
            conn.commit()
        return True, buf.getvalue() or "Импорт выполнен (новых замеров / тренировок не найдено)."
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return False, "Импорт не выполнен — подробности в логах сервера."


def _collect_keys_dict() -> dict[str, list[str]]:
    """Собирает списки настроенных ключей для стандартных переменных окружения."""
    from bot.llm import get_provider_keys
    env_vars = ["GOOGLE_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"]
    out: dict[str, list[str]] = {}
    for var in env_vars:
        keys = get_provider_keys({"api_key_env": var})
        out[var] = keys
    return out


def _test_single_key_sync(base_url: str, model: str, key: str, provider_cfg: dict | None = None) -> dict:
    """Проверяет доступность и квоту конкретного ключа минимальным запросом."""
    import time
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    if provider_cfg:
        if "top_p" in provider_cfg:
            payload["top_p"] = provider_cfg["top_p"]
        if "reasoning_effort" in provider_cfg:
            payload["reasoning_effort"] = provider_cfg["reasoning_effort"]
        if "chat_template_kwargs" in provider_cfg and isinstance(provider_cfg["chat_template_kwargs"], dict):
            payload["chat_template_kwargs"] = provider_cfg["chat_template_kwargs"]
        if "extra_body" in provider_cfg and isinstance(provider_cfg["extra_body"], dict):
            payload.update(provider_cfg["extra_body"])
        if "extra_payload" in provider_cfg and isinstance(provider_cfg["extra_payload"], dict):
            payload.update(provider_cfg["extra_payload"])
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    t0 = time.time()
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else "***"
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            elapsed = (time.time() - t0) * 1000
            return {
                "model": model,
                "masked_key": masked,
                "status": resp.status,
                "latency_ms": elapsed,
                "message": "OK (доступна)",
            }
    except urllib.error.HTTPError as exc:
        elapsed = (time.time() - t0) * 1000
        msg = f"HTTP {exc.code}"
        try:
            body = exc.read().decode("utf-8", errors="replace")
            err_json = json.loads(body)
            if "error" in err_json:
                e = err_json["error"]
                msg = e.get("message", str(e)) if isinstance(e, dict) else str(e)
        except Exception:
            pass
        return {
            "model": model,
            "masked_key": masked,
            "status": exc.code,
            "latency_ms": elapsed,
            "message": msg[:250],
        }
    except Exception as exc:
        elapsed = (time.time() - t0) * 1000
        return {
            "model": model,
            "masked_key": masked,
            "status": 500,
            "latency_ms": elapsed,
            "message": str(exc)[:200],
        }


def _run_all_key_tests(providers: list[dict], keys_dict: dict[str, list[str]]) -> list[dict]:
    import concurrent.futures
    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = []
        for p in providers:
            model = p.get("model", "")
            base_url = p.get("base_url", "")
            env_var = p.get("api_key_env", "")
            keys = keys_dict.get(env_var, [])
            for k in keys:
                futures.append(pool.submit(_test_single_key_sync, base_url, model, k, p))
        for f in futures:
            try:
                tasks.append(f.result(timeout=15))
            except Exception as e:
                tasks.append({"model": "?", "masked_key": "?", "status": 500, "latency_ms": 0, "message": str(e)})
    return tasks


def _update_providers_in_config(new_providers: list[dict]) -> None:
    import re
    import tempfile
    import yaml
    from health_core.config import CONFIG_PATH

    raw = CONFIG_PATH.read_text(encoding="utf-8")
    lines = raw.splitlines()

    start_idx = None
    indent = 2
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)providers:\s*$", line)
        if match:
            start_idx = i
            indent = len(match.group(1))
            break

    if start_idx is None:
        raise ValueError("Секция providers не найдена в config.yaml")

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent <= indent:
            end_idx = j
            break

    dumped = yaml.safe_dump(new_providers, default_flow_style=False, sort_keys=False).strip()
    item_indent = " " * (indent + 2)
    provider_lines = [item_indent + pl for pl in dumped.splitlines()]

    new_lines = lines[:start_idx + 1] + provider_lines + lines[end_idx:]
    new_raw = "\n".join(new_lines) + "\n"

    check = yaml.safe_load(new_raw)
    if not check.get("bot", {}).get("providers"):
        raise ValueError("Ошибка валидации config.yaml после обновления providers")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CONFIG_PATH.parent, delete=False) as tf:
        tf.write(new_raw)
        tmp_name = tf.name
    os.replace(tmp_name, CONFIG_PATH)
    from health_core.config import load as _load_cfg
    _load_cfg.cache_clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "HealthAdmin/1.0"

    # ---------------------------------------------------------------- low-level helpers

    def _client_ip(self) -> str:
        return auth.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _session(self):
        token = self._cookie("session")
        return token, (SESSIONS.get(token) if token else None)

    def _make_cookie(self, token: str) -> str:
        # Secure is conditional, NOT default-on: with no reverse proxy this panel is
        # plain HTTP over an ssh tunnel (ssh -L 8765:localhost:8765 user@vps), and a
        # hard "; Secure" would make the browser silently refuse to ever send the
        # cookie back over that plain-HTTP tunnel — breaking login. Set
        # ADMIN_BEHIND_TLS=1 only once a TLS-terminating proxy is actually in front
        # (see admin/README-proxy.md). Do not "fix" this to always-Secure.
        secure = "; Secure" if os.environ.get("ADMIN_BEHIND_TLS") else ""
        return f"session={token}; HttpOnly; SameSite=Strict; Path=/{secure}"

    def _send(self, status: int, content_type: str, body: bytes,
              set_cookie: str | None = None, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Security headers on every response.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, status: int, body: str, set_cookie: str | None = None) -> None:
        self._send(status, "text/html; charset=utf-8", body.encode("utf-8"), set_cookie)

    def _redirect(self, location: str, set_cookie: str | None = None) -> None:
        parts = urllib.parse.urlsplit(location)
        path = urllib.parse.quote(parts.path, safe="/:@&=+$,-_.!~*'()")
        query = urllib.parse.quote(parts.query, safe="/:@&=+$,-_.!~*'()")
        fragment = urllib.parse.quote(parts.fragment, safe="/:@&=+$,-_.!~*'()")
        safe_location = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, fragment))
        self._send(303, "text/plain; charset=utf-8", b"", set_cookie, {"Location": safe_location})

    def _error_page(self, status: int, message: str) -> None:
        self._html(status, pages.error_page(message))

    def _read_form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:  # generous cap; thresholds form is the biggest
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _parse_form(self) -> dict:
        return self._read_form()

    def _check_csrf(self, token: str | None, sess: dict | None) -> bool:
        if not sess or not token:
            return False
        return auth.csrf_ok(sess, token)

    def _read_multipart(self, max_bytes: int = 50 * 1024 * 1024) -> tuple[str, bytes] | None:
        """Читает тело multipart/form-data целиком, с потолком по Content-Length
        ПРОВЕРЕННЫМ ДО чтения. +64 KB запаса поверх потолка на сам файл —
        под служебные части multipart."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return None
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > max_bytes + 65536:
            return None
        return content_type, self.rfile.read(length)

    def _require_auth(self):
        """Returns (conn, user_id, token, session) or sends a redirect and returns None.

        user_id comes from _resolve_user_id: ?user=<id> when present and valid,
        else the session's remembered pick, else the first user — see there.
        Also stashes the persona list + selection on the handler instance for
        self._layout() to render the nav selector, without every call site
        needing to thread that through."""
        token, sess = self._session()
        if sess is None or not sess.get("authed"):
            self._redirect("/login")
            return None
        conn = connect()
        db_migrate(conn)
        user_id = _resolve_user_id(conn, sess, self.path)
        self._nav_user_id = user_id
        self._nav_users = _list_users_for_selector(conn)
        return conn, user_id, token, sess

    def _layout(self, title: str, body: str, csrf_token: str, active: str | None = None) -> str:
        """Thin wrapper over pages.layout() that adds the nav's user selector,
        using the persona list _require_auth() stashed on this request. Use this
        instead of calling pages.layout() directly in any handler reached via
        _require_auth (i.e. everywhere except /login)."""
        return pages.layout(
            title, body, csrf_token, active=active,
            users=getattr(self, "_nav_users", None),
            selected_user_id=getattr(self, "_nav_user_id", None),
            current_path=self.path,
        )

    def log_message(self, fmt, *args):  # keep default stderr logging (systemd journal captures it)
        super().log_message(fmt, *args)

    # ---------------------------------------------------------------- routing

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            routes = {
                "/login": self._get_login,
                "/": self._get_dashboard,
                "/guards": self._get_guards,
                "/milestones": self._get_milestones,
                "/thresholds": self._get_thresholds,
                "/keys": self._get_keys,
                "/personas": self._get_personas,
                "/actions": self._get_actions,
                "/knowledge": self._get_knowledge,
                "/drafts": self._get_drafts,
            }
            if parsed.path == "/alerts":
                return self._get_alerts(query)
            if parsed.path == "/plans":
                return self._get_plans(query)
            if parsed.path == "/workouts":
                return self._get_workouts(query)
            if parsed.path == "/forecast":
                return self._get_forecast(query)
            handler = routes.get(parsed.path)
            if handler is None:
                return self._error_page(404, "Страница не найдена.")
            handler()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._error_page(500, "Внутренняя ошибка. Подробности в логах сервера.")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            routes = {
                "/login": self._post_login,
                "/logout": self._post_logout,
                "/guards/save": self._post_guards_save,
                "/milestones": self._post_milestones,
                "/thresholds": self._post_thresholds,
                "/keys": self._post_keys,
                "/keys/test": self._post_keys_test,
                "/personas": self._post_personas,
                "/actions": self._post_actions,
                "/actions/import-scale": self._post_actions_import_scale,
                "/knowledge/upload": self._post_knowledge_upload,
                "/knowledge/delete": self._post_knowledge_delete,
                "/plans/save": self._post_plans_save,
                "/plans/delete": self._post_plans_delete,
                "/plans/template/save": self._post_plans_template_save,
                "/plans/template/delete": self._post_plans_template_delete,
                "/workouts/save": self._post_workouts_save,
                "/workouts/delete": self._post_workouts_delete,
                "/drafts": self._post_drafts,
            }
            handler = routes.get(parsed.path)
            if handler is None:
                return self._error_page(404, "Страница не найдена.")
            handler()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._error_page(500, "Внутренняя ошибка. Подробности в логах сервера.")

    # ---------------------------------------------------------------- auth

    def _get_login(self):
        token, sess = self._session()
        if sess is not None and sess.get("authed"):
            return self._redirect("/")
        set_cookie = None
        if sess is None:
            token = SESSIONS.create(authed=False)
            sess = SESSIONS.get(token)
            set_cookie = self._make_cookie(token)
        self._html(200, pages.login_page(csrf_token=sess["csrf"]), set_cookie)

    def _post_login(self):
        form = self._read_form()
        token, sess = self._session()
        if sess is None:
            # No session cookie at all — can't validate CSRF against anything real.
            return self._error_page(403, "Сессия не найдена. Обновите страницу входа.")
        if not auth.csrf_ok(sess, form.get("csrf_token")):
            return self._error_page(403, "Неверный CSRF-токен.")

        ip = self._client_ip()
        if RATE_LIMITER.is_locked(ip):
            return self._html(200, pages.login_page(csrf_token=sess["csrf"], error=auth.LOGIN_ERROR_MSG))

        password_hash = os.environ.get("ADMIN_PASSWORD_HASH")
        password_salt = os.environ.get("ADMIN_PASSWORD_SALT")
        password = form.get("password", "")
        if not password_hash or not password_salt or not auth.verify_password(password, password_salt, password_hash):
            RATE_LIMITER.record_failure(ip)
            return self._html(200, pages.login_page(csrf_token=sess["csrf"], error=auth.LOGIN_ERROR_MSG))

        RATE_LIMITER.record_success(ip)
        new_token = SESSIONS.authenticate(token)  # rotates token: session-fixation defense
        self._html(200, pages.login_ok_page(), self._make_cookie(new_token))

    def _post_logout(self):
        token, sess = self._session()
        form = self._read_form()
        if sess is None or not auth.csrf_ok(sess, form.get("csrf_token")):
            return self._error_page(403, "Неверный CSRF-токен.")
        SESSIONS.destroy(token)
        self._redirect("/login")

    # ---------------------------------------------------------------- dashboard

    def _get_dashboard(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            if user_id is None:
                body = pages.no_user_page()
                return self._html(200, self._layout("Дашборд", body, sess["csrf"], active="dashboard"))
            today = user_today(conn, user_id)
            alerts = check_all(conn, user_id)
            tr = trends(conn, user_id, 90)
            latest = conn.execute(
                "SELECT * FROM body_metrics WHERE user_id=? ORDER BY measured_at DESC LIMIT 1", (user_id,)
            ).fetchone()
            try:
                target = daily_target(conn, user_id, today)  # апсертит цели дня до day_summary ниже
            except ValueError:
                target = None  # no body_metrics / incomplete profile yet — not an error state
            day = day_summary(conn, user_id, today)
            meals = meals_of_day(conn, user_id, today)
            # Стартовый вес — общая с ботом report.baseline_weight(), а не своя
            # копия «профиль, иначе самое раннее взвешивание». Копия здесь уже
            # врала: она не знает про base_weight_date, поэтому профиль «120.5
            # от июня» побеждал импорт истории с февраля (158.8), и Δ старт
            # показывала 0.2 кг вместо 38. Логика одна на обе витрины.
            base_weight = baseline_weight(conn, user_id)
            # Дневная дельта: последние два разных дня взвешиваний.
            drows = conn.execute(
                "SELECT weight_kg w FROM body_metrics WHERE user_id=? AND weight_kg IS NOT NULL "
                "GROUP BY date(measured_at) HAVING measured_at=MAX(measured_at) "
                "ORDER BY date(measured_at) DESC LIMIT 2", (user_id,)
            ).fetchall()
            day_delta = (drows[0]["w"] - drows[1]["w"]) if len(drows) == 2 else None
            history = list(reversed(conn.execute(
                "SELECT measured_at, weight_kg, ffm_kg FROM body_metrics WHERE user_id=? "
                "ORDER BY measured_at DESC LIMIT 90",
                (user_id,),
            ).fetchall()))
            guards_status = get_guards_status(conn, user_id)
            body = pages.dashboard_page(alerts, tr, latest, target, history, day, meals,
                                        base_weight, day_delta, sess["csrf"], guards_status=guards_status)
            self._html(200, self._layout("Дашборд", body, sess["csrf"], active="dashboard"))
        finally:
            conn.close()

    # ---------------------------------------------------------------- guards

    def _guards_body(self, conn, user_id, sess, message=None, error=None) -> str:
        guards_status = get_guards_status(conn, user_id)
        parsed = load_config()
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        comments = pages.parse_config_comments(raw)
        return pages.guards_page(guards_status, parsed, comments, sess["csrf"], message=message, error=error)

    def _get_guards(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            if user_id is None:
                body = pages.no_user_page()
                return self._html(200, self._layout("Гардрейлы", body, sess["csrf"], active="guards"))
            body = self._guards_body(conn, user_id, sess)
            self._html(200, self._layout("Гардрейлы", body, sess["csrf"], active="guards"))
        finally:
            conn.close()

    def _post_guards_save(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            if not auth.csrf_ok(sess, form.get("csrf_token")):
                return self._error_page(403, "Неверный CSRF-токен.")

            current = load_config()
            notes = []
            had_error = False

            all_guard_keys = []
            for grp in pages.GUARD_FORM_GROUPS:
                for key, label, _ in grp["fields"]:
                    all_guard_keys.append((key, label))

            for key, label in all_guard_keys:
                field = f"guard__{key}"
                if field not in form:
                    continue
                submitted = form[field].strip()
                curr_val = current.get("guards", {}).get(key, current.get("policy", {}).get(key))
                if submitted == str(curr_val):
                    continue
                result = json.loads(_admin_set(key, submitted))
                if "error" in result:
                    had_error = True
                    notes.append(f"{label} ({key}): ОШИБКА — {result['error']}")
                else:
                    notes.append(f"{label}: сохранено ({submitted})")

            message = "; ".join(notes) if notes else "Изменений нет."
            error_msg = None
            if had_error:
                error_msg = message
                message = None
            body = self._guards_body(conn, user_id, sess, message=message, error=error_msg)
            self._html(200, self._layout("Гардрейлы", body, sess["csrf"], active="guards"))
        finally:
            conn.close()

    # ---------------------------------------------------------------- milestones

    def _milestone_rows(self, conn, user_id):
        return conn.execute(
            "SELECT id, name, metric, threshold, deadline, achieved_at FROM milestones "
            "WHERE user_id=? ORDER BY id", (user_id,),
        ).fetchall()

    def _get_milestones(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            rows = self._milestone_rows(conn, user_id) if user_id is not None else []
            body = pages.milestones_page(rows, _VALID_MILESTONE_METRICS, sess["csrf"])
            self._html(200, self._layout("Вехи", body, sess["csrf"], active="milestones"))
        finally:
            conn.close()

    def _post_milestones(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            if not auth.csrf_ok(sess, form.get("csrf_token")):
                return self._error_page(403, "Неверный CSRF-токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            action = form.get("action")
            error = None
            if action == "delete":
                try:
                    mid = int(form.get("id", ""))
                except ValueError:
                    return self._error_page(400, "Некорректный id.")
                conn.execute("DELETE FROM milestones WHERE user_id=? AND id=?", (user_id, mid))
                conn.commit()
            elif action == "save":
                # Same UPSERT the plugin uses — call the plugin's own handler rather
                # than duplicating the ON CONFLICT(user_id, name) SQL here.
                name = (form.get("name") or "").strip()
                threshold_raw = form.get("threshold") or None
                threshold = None
                if threshold_raw is not None:
                    try:
                        threshold = float(threshold_raw)
                    except ValueError:
                        error = "Порог должен быть числом."
                if not name and error is None:
                    error = "Имя вехи обязательно."
                if error is None:
                    result = json.loads(handle_set_milestone({
                        "user_id": user_id,
                        "name": name,
                        "metric": form.get("metric"),
                        "threshold": threshold,
                        "deadline": form.get("deadline") or None,
                    }))
                    if "error" in result:
                        error = result["error"]
            else:
                error = "Неизвестное действие."

            if error:
                rows = self._milestone_rows(conn, user_id)
                body = pages.milestones_page(rows, _VALID_MILESTONE_METRICS, sess["csrf"], error=error)
                return self._html(200, self._layout("Вехи", body, sess["csrf"], active="milestones"))
            self._redirect("/milestones")
        finally:
            conn.close()

    # ---------------------------------------------------------------- thresholds

    def _thresholds_body(self, sess, message=None) -> str:
        parsed = load_config()
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        comments = pages.parse_config_comments(raw)
        return pages.thresholds_page(parsed, comments, sess["csrf"], message=message)

    def _get_thresholds(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        conn.close()  # not needed for this page; config.yaml lives outside sqlite
        body = self._thresholds_body(sess)
        self._html(200, self._layout("Пороги", body, sess["csrf"], active="thresholds"))

    def _post_thresholds(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        conn.close()
        form = self._read_form()
        if not auth.csrf_ok(sess, form.get("csrf_token")):
            return self._error_page(403, "Неверный CSRF-токен.")

        current = load_config()
        notes = []
        had_error = False
        for section, key, value in pages.flatten_config(current):
            field = f"kv__{key}"
            if field not in form:
                continue
            submitted = form[field]
            if submitted == str(value):
                continue  # unchanged — skip, don't churn the file for a no-op
            # Surgical single-key edit (validates YAML, atomic replace, keeps .bak,
            # clears the config cache) — the exact function the Telegram admin
            # command uses, so config.yaml's comments are never at risk here.
            result = json.loads(_admin_set(key, submitted))
            if "error" in result:
                had_error = True
                notes.append(f"{key}: ОШИБКА — {result['error']}")
            else:
                notes.append(result.get("text", f"{key}: сохранено"))

        message = "; ".join(notes) if notes else "Изменений нет."
        if had_error:
            message = "Ошибка. " + message
        body = self._thresholds_body(sess, message=message)
        self._html(200, self._layout("Пороги", body, sess["csrf"], active="thresholds"))

    # ---------------------------------------------------------------- keys & models

    def _keys_page_html(self, sess, message: str | None = None, error: str | None = None,
                         test_results: list[dict] | None = None) -> str:
        import yaml
        from health_core.config import load as load_config
        cfg = load_config()
        providers = (cfg.get("bot") or {}).get("providers", [])
        keys_dict = _collect_keys_dict()
        providers_yaml = yaml.safe_dump(providers, default_flow_style=False, sort_keys=False)
        body = pages.keys_page(
            providers, keys_dict, providers_yaml, sess["csrf"],
            message=message, error=error, test_results=test_results,
        )
        return self._layout("Ключи и Модели", body, sess["csrf"], active="keys")

    def _get_keys(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        conn.close()
        self._html(200, self._keys_page_html(sess))

    def _post_keys(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        conn.close()
        form = self._read_form()
        if not auth.csrf_ok(sess, form.get("csrf_token")):
            return self._error_page(403, "Неверный CSRF-токен.")

        action = form.get("action")
        message, error = None, None
        if action == "save_keys":
            import re

            def parse_keys_input(raw: str) -> list[str]:
                out = []
                for p in re.split(r"[\n,;]+", raw):
                    k = p.strip()
                    if k and not k.startswith("#") and k not in out:
                        out.append(k)
                return out

            g_keys = parse_keys_input(form.get("google_api_keys", ""))
            groq_keys = parse_keys_input(form.get("groq_api_keys", ""))
            or_keys = parse_keys_input(form.get("openrouter_api_keys", ""))
            oa_keys = parse_keys_input(form.get("openai_api_keys", ""))

            updates = {
                "GOOGLE_API_KEY": g_keys[0] if g_keys else "",
                "GOOGLE_API_KEYS": ",".join(g_keys) if g_keys else "",
                "GROQ_API_KEY": groq_keys[0] if groq_keys else "",
                "GROQ_API_KEYS": ",".join(groq_keys) if groq_keys else "",
                "OPENROUTER_API_KEY": or_keys[0] if or_keys else "",
                "OPENROUTER_API_KEYS": ",".join(or_keys) if or_keys else "",
                "OPENAI_API_KEY": oa_keys[0] if oa_keys else "",
                "OPENAI_API_KEYS": ",".join(oa_keys) if oa_keys else "",
            }
            auth.update_env_vars(updates)
            auth.load_env_file(force=True)
            message = f"API-ключи успешно сохранены (Google: {len(g_keys)} шт., Groq: {len(groq_keys)} шт.). Ротация активна."

        elif action == "save_providers":
            import yaml
            raw_yaml = form.get("providers_yaml", "")
            try:
                parsed = yaml.safe_load(raw_yaml)
                if not isinstance(parsed, list) or not parsed:
                    raise ValueError("Список providers должен быть непустым списком.")
                for item in parsed:
                    if not isinstance(item, dict) or not item.get("base_url") or not item.get("model") or not item.get("api_key_env"):
                        raise ValueError("Каждый провайдер обязан содержать base_url, model, api_key_env.")
                _update_providers_in_config(parsed)
                message = "Цепочка моделей успешно обновлена."
            except Exception as exc:
                error = f"Ошибка в YAML моделей: {exc}"
        elif action == "reorder_providers":
            raw_order = form.get("model_order", "")
            try:
                model_names = json.loads(raw_order)
                if not isinstance(model_names, list) or not model_names:
                    raise ValueError("Список моделей пуст.")
                cfg = load_config()
                current_providers = list((cfg.get("bot") or {}).get("providers", []))

                by_model = {p["model"]: p for p in current_providers}
                new_providers = []
                for m_name in model_names:
                    if m_name in by_model:
                        new_providers.append(by_model.pop(m_name))
                for p in by_model.values():
                    new_providers.append(p)

                _update_providers_in_config(new_providers)
                message = f"Приоритет моделей успешно обновлён! Основная модель: {new_providers[0].get('model')}."
            except Exception as exc:
                error = f"Ошибка изменения порядка моделей: {exc}"
        elif action == "set_primary":
            target_model = form.get("model", "").strip()
            try:
                if not target_model:
                    raise ValueError("Модель не указана.")
                cfg = load_config()
                current_providers = list((cfg.get("bot") or {}).get("providers", []))
                match = [p for p in current_providers if p.get("model") == target_model]
                if not match:
                    raise ValueError(f"Модель {target_model} не найдена в цепочке.")
                other = [p for p in current_providers if p.get("model") != target_model]
                new_providers = [match[0]] + other
                _update_providers_in_config(new_providers)
                message = f"Основная модель успешно изменена на {target_model}!"
            except Exception as exc:
                error = f"Ошибка смены основной модели: {exc}"
        else:
            error = "Неизвестное действие."

        self._html(200, self._keys_page_html(sess, message=message, error=error))

    def _post_keys_test(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        conn.close()
        form = self._read_form()
        if not auth.csrf_ok(sess, form.get("csrf_token")):
            return self._error_page(403, "Неверный CSRF-токен.")

        cfg = load_config()
        providers = (cfg.get("bot") or {}).get("providers", [])
        keys_dict = _collect_keys_dict()
        results = _run_all_key_tests(providers, keys_dict)
        self._html(200, self._keys_page_html(sess, test_results=results))

    # ---------------------------------------------------------------- alerts

    def _get_alerts(self, query):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
            except (ValueError, IndexError):
                page = 1
            rows, total = [], 0
            if user_id is not None:
                total = conn.execute("SELECT COUNT(*) c FROM alerts WHERE user_id=?", (user_id,)).fetchone()["c"]
                rows = conn.execute(
                    "SELECT id, created_at, rule, message FROM alerts WHERE user_id=? "
                    "ORDER BY created_at DESC LIMIT 50 OFFSET ?",
                    (user_id, (page - 1) * 50),
                ).fetchall()
            body = pages.alerts_page(rows, page, total, sess["csrf"])
            self._html(200, self._layout("Алерты", body, sess["csrf"], active="alerts"))
        finally:
            conn.close()

    # ---------------------------------------------------------------- plans

    def _get_plans(self, query):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            # Extract selected_date from query string
            try:
                selected_date = query.get("date", [None])[0]
                # Validate date format: YYYY-MM-DD
                if selected_date:
                    datetime.strptime(selected_date, "%Y-%m-%d")
            except (ValueError, IndexError):
                selected_date = None

            today_plans = []
            all_dates = []
            selected_plan = None
            weekly_template = {"meals": [], "workouts": []}

            if user_id is not None:
                from health_core.plans import get_plan

                # Get user today date
                today_str = user_today(conn, user_id)

                # Fetch today's plans
                for kind in ["workout", "meal"]:
                    row = conn.execute(
                        "SELECT date, kind, body FROM plan_log WHERE user_id=? AND date=? AND kind=?",
                        (user_id, today_str, kind),
                    ).fetchone()
                    if row:
                        today_plans.append(dict(row))

                # Fetch all dates for history list
                all_rows = conn.execute(
                    "SELECT date, kind, body FROM plan_log WHERE user_id=? "
                    "ORDER BY date DESC",
                    (user_id,),
                ).fetchall()
                for row in all_rows:
                    r = dict(row)
                    # Extract first 100 chars as preview (or full text if shorter)
                    r["body_preview"] = r["body"][:100]
                    all_dates.append(r)

                # Fetch plan for selected date if provided
                if selected_date:
                    for kind in ["workout", "meal"]:
                        row = conn.execute(
                            "SELECT date, kind, body, rationale FROM plan_log "
                            "WHERE user_id=? AND date=? AND kind=?",
                            (user_id, selected_date, kind),
                        ).fetchone()
                        if row:
                            selected_plan = dict(row)
                            break  # Show the first plan we find for that date

                # Fetch weekly template
                weekly_template = get_plan(conn, user_id, day_of_week=None)

            body = pages.plans_page(today_plans, all_dates, selected_date, selected_plan,
                                   weekly_template, sess["csrf"])
            self._html(200, self._layout("Планы", body, sess["csrf"], active="plans"))
        finally:
            conn.close()

    def _post_plans_save(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            csrf_tok = form.get("csrf_token") or form.get("csrf")
            if not auth.csrf_ok(sess, csrf_tok):
                return self._error_page(403, "Неверный CSRF токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            date_str = (form.get("date") or "").strip()
            kind = (form.get("kind") or "").strip()
            body = (form.get("body") or "").strip()
            rationale = (form.get("rationale") or "").strip() or None

            if not date_str or not kind or not body:
                return self._error_page(400, "Заполните все обязательные поля (дата, тип, текст плана).")

            from datetime import datetime
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO plan_log(user_id, date, kind, body, rationale, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, date, kind) DO UPDATE SET "
                "body=excluded.body, rationale=COALESCE(excluded.rationale,rationale), "
                "created_at=excluded.created_at",
                (user_id, date_str, kind, body, rationale, now_iso),
            )
            conn.commit()
            self._redirect(f"/plans?date={date_str}")
        finally:
            conn.close()

    def _post_plans_template_save(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            csrf_tok = form.get("csrf_token") or form.get("csrf")
            if not auth.csrf_ok(sess, csrf_tok):
                return self._error_page(403, "Неверный CSRF токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            save_type = (form.get("type") or "").strip()
            dow = form.get("day_of_week")
            dow_int = int(dow) if dow is not None and str(dow).isdigit() else None

            if dow_int is None or dow_int not in range(7):
                return self._error_page(400, "Некорректный день недели.")

            from health_core.plans import set_meal_plan, set_workout_plan
            if save_type == "meal":
                slot = (form.get("meal_slot") or "breakfast").strip()
                name = (form.get("name") or "").strip()
                kcal = float(form.get("kcal") or 0)
                prot = float(form.get("protein_g") or 0)
                fat = float(form.get("fat_g") or 0)
                carb = float(form.get("carbs_g") or 0)
                set_meal_plan(conn, user_id, dow_int, slot, name=name, kcal=kcal, protein_g=prot, fat_g=fat, carbs_g=carb)
            elif save_type == "workout":
                name = (form.get("name") or "").strip()
                kind = (form.get("kind") or "strength").strip()
                dur = float(form.get("duration_min") or 0)
                if name:
                    set_workout_plan(conn, user_id, dow_int, name, kind=kind, duration_min=dur)
            self._redirect("/plans")
        finally:
            conn.close()

    def _post_plans_delete(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            csrf_tok = form.get("csrf_token") or form.get("csrf")
            if not auth.csrf_ok(sess, csrf_tok):
                return self._error_page(403, "Неверный CSRF токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            date_str = (form.get("date") or "").strip()
            kind = (form.get("kind") or "").strip() or None

            from health_core.plans import delete_plan_day
            delete_plan_day(conn, user_id, date_str or None, kind)
            self._redirect("/plans")
        finally:
            conn.close()

    def _post_plans_template_delete(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            csrf_tok = form.get("csrf_token") or form.get("csrf")
            if not auth.csrf_ok(sess, csrf_tok):
                return self._error_page(403, "Неверный CSRF токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            del_type = (form.get("type") or "").strip()
            dow = form.get("day_of_week")
            dow_int = int(dow) if dow is not None and str(dow).isdigit() else None

            from health_core.plans import remove_meal_plan, remove_workout_plan
            if del_type == "meal":
                slot = form.get("meal_slot") or None
                remove_meal_plan(conn, user_id, dow_int, slot)
            elif del_type == "workout":
                name = form.get("name") or None
                remove_workout_plan(conn, user_id, dow_int, name)
            elif del_type == "all":
                remove_meal_plan(conn, user_id, dow_int)
                remove_workout_plan(conn, user_id, dow_int)
            self._redirect("/plans")
        finally:
            conn.close()

    # ---------------------------------------------------------------- workouts

    def _get_workouts(self, query):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            error = query.get("error", [None])[0]
            message = query.get("msg", [None])[0]
            rows = []
            stats = {"total_count": 0, "total_duration_min": 0.0, "total_kcal": 0.0, "avg_hr": None}
            sports_efficiency = {}

            if user_id is not None:
                from health_core.report import training_efficiency
                # Все сессии
                all_rows = conn.execute(
                    "SELECT id, started_at, sport, duration_min, kcal, avg_hr, notes, source, file_hash "
                    "FROM activity WHERE user_id=? ORDER BY started_at DESC",
                    (user_id,),
                ).fetchall()
                rows = [dict(r) for r in all_rows]

                # Агрегаты
                agg = conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(duration_min),0) total_dur, "
                    "COALESCE(SUM(kcal),0) total_kcal, AVG(avg_hr) avg_hr "
                    "FROM activity WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if agg:
                    stats["total_count"] = agg["n"]
                    stats["total_duration_min"] = agg["total_dur"]
                    stats["total_kcal"] = agg["total_kcal"]
                    stats["avg_hr"] = agg["avg_hr"]

                # Эффективность
                eff = training_efficiency(conn, user_id, window_days=180)
                sports_efficiency = eff.get("sports", {})

            body = pages.workouts_page(rows, stats, sports_efficiency, sess["csrf"], error, message)
            self._html(200, self._layout("Тренировки", body, sess["csrf"], active="workouts"))
        finally:
            conn.close()

    def _post_workouts_save(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            csrf_tok = form.get("csrf_token") or form.get("csrf")
            if not auth.csrf_ok(sess, csrf_tok):
                return self._error_page(403, "Неверный CSRF токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            sport = (form.get("sport") or "Тренировка").strip()
            started_at = (form.get("started_at") or "").strip()
            if not started_at:
                from datetime import datetime
                started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elif len(started_at) == 16:
                started_at += ":00"

            try:
                duration_min = float(form.get("duration_min") or 0)
            except (TypeError, ValueError):
                duration_min = None

            try:
                kcal = float(form.get("kcal") or 0) if form.get("kcal") else None
            except (TypeError, ValueError):
                kcal = None

            try:
                avg_hr = int(float(form.get("avg_hr") or 0)) if form.get("avg_hr") else None
            except (TypeError, ValueError):
                avg_hr = None

            notes = (form.get("notes") or "").strip() or None

            import secrets
            file_hash = f"manual_{user_id}_{secrets.token_hex(12)}"

            conn.execute(
                "INSERT INTO activity(user_id, started_at, duration_min, kcal, avg_hr, sport, file_hash, notes, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
                (user_id, started_at, duration_min, kcal, avg_hr, sport, file_hash, notes),
            )
            conn.commit()
            self._redirect("/workouts?msg=Тренировка+успешно+записана")
        finally:
            conn.close()

    def _post_workouts_delete(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            csrf_tok = form.get("csrf_token") or form.get("csrf")
            if not auth.csrf_ok(sess, csrf_tok):
                return self._error_page(403, "Неверный CSRF токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")

            workout_id = form.get("workout_id")
            if not workout_id or not str(workout_id).isdigit():
                return self._error_page(400, "Некорректный workout_id.")

            conn.execute("DELETE FROM activity WHERE id=? AND user_id=?", (int(workout_id), user_id))
            conn.commit()
            self._redirect("/workouts?msg=Тренировка+удалена")
        finally:
            conn.close()

    def _get_forecast(self, query):
        """Прогноз массы. Все три параметра необязательны и приходят из строки
        запроса; кривые числа гасятся в None, а не роняют страницу."""
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            def qnum(name, cast, default=None):
                raw = query.get(name, [None])[0]
                if raw in (None, ""):
                    return default
                try:
                    return cast(raw)
                except (TypeError, ValueError):
                    return default

            horizon = qnum("horizon_days", int, 84)
            intake = qnum("intake_kcal", float)
            target = qnum("target_kg", float)

            result = {"error": "В базе нет пользователя."}
            actual = []
            reach_result = None
            if user_id is not None:
                from health_core import forecast as fc

                result = fc.project(conn, user_id, horizon, intake)
                if "error" not in result:
                    traj = result.pop("trajectory", [])
                    result["weekly"] = traj[::7]
                    if traj and traj[-1] not in result["weekly"]:
                        result["weekly"].append(traj[-1])
                    # Факт слева от прогноза: видно, откуда кривая выходит.
                    rows = conn.execute(
                        "SELECT date(measured_at) d, weight_kg w FROM body_metrics "
                        "WHERE user_id=? AND weight_kg IS NOT NULL "
                        "AND measured_at >= date('now','-28 days') "
                        "GROUP BY 1 ORDER BY 1", (user_id,)
                    ).fetchall()
                    actual = [(r["d"], r["w"]) for r in rows]
                if target is not None:
                    reach_result = fc.reach(conn, user_id, target, intake)

            body = pages.forecast_page(result, actual, horizon, intake,
                                       reach_result, target, sess["csrf"])
            self._html(200, self._layout("Прогноз", body, sess["csrf"], active="forecast"))
        finally:
            conn.close()

    # ---------------------------------------------------------------- actions

    # ---------------------------------------------------------------- personas

    def _persona_rows(self, conn) -> list[dict]:
        """Все персоны с счётчиками их данных — ровно то, что рисует personas_page.
        dict(row), а не sqlite3.Row: шаблон обращается к u.get(...), которого у Row нет."""
        out = []
        for row in conn.execute(
            "SELECT id, telegram_user_id, height_cm, sex, birth_date, base_weight_kg, created_at "
            "FROM users ORDER BY id"
        ).fetchall():
            uid = row["id"]
            food = conn.execute(
                "SELECT COUNT(*) n, MAX(eaten_at) last FROM food_log WHERE user_id=?", (uid,)
            ).fetchone()
            bm = conn.execute(
                "SELECT COUNT(*) n, MAX(measured_at) last FROM body_metrics WHERE user_id=?", (uid,)
            ).fetchone()
            out.append({
                "user": dict(row),
                "food_count": food["n"], "food_last": food["last"],
                "bm_count": bm["n"], "bm_last": bm["last"],
            })
        return out

    def _get_personas(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        try:
            body = pages.personas_page(self._persona_rows(conn), sess["csrf"])
            self._html(200, self._layout("Персоны", body, sess["csrf"], active="personas"))
        finally:
            conn.close()

    # ---------------------------------------------------------------- drug card drafts

    def _draft_rows(self, conn) -> list[dict]:
        """Pending-черновики (health_core/card_drafts.py) с полями/источниками из
        JSON и telegram_user_id запросившего — ровно то, что рисует drafts_page."""
        out = []
        for row in card_drafts.pending_drafts(conn):
            who = conn.execute(
                "SELECT telegram_user_id FROM users WHERE id=?", (row["requested_by_user_id"],)
            ).fetchone()
            out.append({
                "id": row["id"],
                "substance": row["substance"],
                "fields": json.loads(row["fields_json"] or "{}"),
                "sources": json.loads(row["sources_json"] or "[]"),
                "requested_by": who["telegram_user_id"] if who else row["requested_by_user_id"],
                "created_at": row["created_at"],
            })
        return out

    def _get_drafts(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        try:
            body = pages.drafts_page(self._draft_rows(conn), sess["csrf"])
            self._html(200, self._layout("Черновики карт", body, sess["csrf"], active="drafts"))
        finally:
            conn.close()

    def _post_drafts(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        try:
            form = self._read_form()
            if not auth.csrf_ok(sess, form.get("csrf_token")):
                return self._error_page(403, "Неверный CSRF-токен.")
            try:
                draft_id = int(form.get("id", ""))
            except ValueError:
                return self._error_page(400, "Некорректный id черновика.")

            action = form.get("action")
            error = None
            if action == "reject":
                card_drafts.reject(conn, draft_id)
            elif action == "approve":
                fields = {
                    "status": form.get("status") or "",
                    "ladder": form.get("ladder") or "",
                    "min_weeks": form.get("min_weeks") or "",
                    "interval_days": form.get("interval_days") or "",
                    "half_life_days": form.get("half_life_days") or "",
                    "tmax_h": form.get("tmax_h") or "",
                    "synonyms": form.get("synonyms") or "",
                    "source": form.get("source") or "",
                }
                try:
                    card_drafts.approve(conn, draft_id, fields)
                except ValueError as e:
                    error = str(e)
            else:
                error = "Неизвестное действие."

            if error:
                body = pages.drafts_page(self._draft_rows(conn), sess["csrf"], error=error)
                return self._html(400, self._layout("Черновики карт", body, sess["csrf"], active="drafts"))
            self._redirect("/drafts")
        finally:
            conn.close()

    def _post_personas(self):
        """Удаление персоны. Необратимо стирает личные медицинские данные, поэтому
        подтверждение — ввод её telegram_user_id руками, а не кнопка «уверены?»:
        промахнуться карточкой при нескольких персонах слишком легко."""
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        try:
            form = self._read_form()
            if not auth.csrf_ok(sess, form.get("csrf_token")):
                return self._error_page(403, "Неверный CSRF-токен.")
            try:
                target_id = int(form.get("id", ""))
            except ValueError:
                return self._error_page(400, "Некорректный id персоны.")

            row = conn.execute(
                "SELECT telegram_user_id FROM users WHERE id=?", (target_id,)
            ).fetchone()
            if row is None:
                return self._error_page(404, "Персона не найдена.")

            typed = (form.get("confirm_telegram_id") or "").strip()
            if typed != str(row["telegram_user_id"]):
                body = pages.personas_page(
                    self._persona_rows(conn), sess["csrf"],
                    error=(f"Подтверждение не совпало: введите ровно {row['telegram_user_id']}. "
                           f"Ничего не удалено."),
                )
                return self._html(400, self._layout("Персоны", body, sess["csrf"], active="personas"))

            try:
                summary = _delete_persona(conn, target_id)
                conn.commit()
            except Exception:
                # Половина удалённой персоны хуже неудачного удаления: откатываем всё.
                conn.rollback()
                traceback.print_exc(file=sys.stderr)
                body = pages.personas_page(
                    self._persona_rows(conn), sess["csrf"],
                    error="Удаление не выполнено, изменения откачены. Подробности в логах сервера.",
                )
                return self._html(500, self._layout("Персоны", body, sess["csrf"], active="personas"))

            body = pages.personas_page(
                self._persona_rows(conn), sess["csrf"],
                summary=[(t, n) for t, n in summary if n],
            )
            self._html(200, self._layout("Персоны", body, sess["csrf"], active="personas"))
        finally:
            conn.close()

    def _get_actions(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        conn.close()
        body = pages.actions_page(sess["csrf"])
        self._html(200, self._layout("Действия", body, sess["csrf"], active="actions"))

    def _post_actions(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            form = self._read_form()
            if not auth.csrf_ok(sess, form.get("csrf_token")):
                return self._error_page(403, "Неверный CSRF-токен.")
            if user_id is None:
                return self._error_page(400, "Нет пользователя в базе.")
            result = _run_action(conn, user_id, form.get("action", ""))
            body = pages.actions_page(sess["csrf"], result=result)
            self._html(200, self._layout("Действия", body, sess["csrf"], active="actions"))
        finally:
            conn.close()

    def _post_actions_import_scale(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, user_id, token, sess = ctx
        try:
            read = self._read_multipart()
            if read is None:
                body = pages.actions_page(sess["csrf"], result="Запрос повреждён или файл превышает потолок размера.")
                return self._html(400, self._layout("Действия", body, sess["csrf"], active="actions"))
            content_type, raw_body = read
            fields = upload.parse_multipart(content_type, raw_body)

            csrf_val = fields.get("csrf_token")
            if not isinstance(csrf_val, str) or not auth.csrf_ok(sess, csrf_val):
                return self._error_page(403, "Неверный CSRF-токен.")

            if user_id is None:
                body = pages.actions_page(sess["csrf"], result="Нет пользователя в базе.")
                return self._html(400, self._layout("Действия", body, sess["csrf"], active="actions"))

            file_field = fields.get("file")
            if not isinstance(file_field, tuple) or not file_field[0]:
                body = pages.actions_page(sess["csrf"], result="Файл не выбран.")
                return self._html(400, self._layout("Действия", body, sess["csrf"], active="actions"))
            filename, data = file_field

            ok, message = _import_scale_upload(conn, user_id, filename, data)
            status = 200 if ok else 400
            body = pages.actions_page(sess["csrf"], result=message)
            self._html(status, self._layout("Действия", body, sess["csrf"], active="actions"))
        finally:
            conn.close()

    # ---------------------------------------------------------------- knowledge

    def _knowledge_rows(self) -> list[dict]:
        """Список файлов Knowledge/ — читается с диска на каждый запрос (как и
        knowledge.index() в plugin), никакого кеша: панель и бот должны видеть
        один и тот же каталог сразу после загрузки/удаления."""
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        rows = []
        for p in sorted(KNOWLEDGE_DIR.iterdir()):
            if not p.is_file():
                continue
            st = p.stat()
            rows.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        return rows

    def _get_knowledge(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        conn.close()  # не нужен для этой страницы — Knowledge/ живёт на диске, не в sqlite
        body = pages.knowledge_page(self._knowledge_rows(), sess["csrf"])
        self._html(200, self._layout("Знания", body, sess["csrf"], active="knowledge"))

    def _post_knowledge_upload(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        conn.close()

        read = self._read_multipart(max_bytes=upload.MAX_UPLOAD_BYTES)
        if read is None:
            body = pages.knowledge_page(
                self._knowledge_rows(), sess["csrf"],
                error="Запрос повреждён или файл превышает потолок размера.",
            )
            return self._html(400, self._layout("Знания", body, sess["csrf"], active="knowledge"))
        content_type, raw_body = read
        fields = upload.parse_multipart(content_type, raw_body)

        csrf_val = fields.get("csrf_token")
        if not isinstance(csrf_val, str) or not auth.csrf_ok(sess, csrf_val):
            return self._error_page(403, "Неверный CSRF-токен.")

        file_field = fields.get("file")
        if not isinstance(file_field, tuple) or not file_field[0]:
            body = pages.knowledge_page(self._knowledge_rows(), sess["csrf"], error="Файл не выбран.")
            return self._html(400, self._layout("Знания", body, sess["csrf"], active="knowledge"))
        filename, data = file_field
        overwrite = fields.get("overwrite") == "on"

        ok, message = upload.save_knowledge_file(KNOWLEDGE_DIR, filename, data, overwrite)
        status = 200 if ok else 400
        body = pages.knowledge_page(
            self._knowledge_rows(), sess["csrf"],
            error=None if ok else message, message=message if ok else None,
        )
        self._html(status, self._layout("Знания", body, sess["csrf"], active="knowledge"))

    def _post_knowledge_delete(self):
        ctx = self._require_auth()
        if ctx is None:
            return
        conn, _user_id, _token, sess = ctx
        conn.close()

        form = self._read_form()
        if not auth.csrf_ok(sess, form.get("csrf_token")):
            return self._error_page(403, "Неверный CSRF-токен.")

        # safe_knowledge_path — та же resolve()-проверка, что и на загрузке: имя
        # приходит из формы (пусть и отрендеренной нами же), запрос всё равно
        # может быть подделан, доверять сырому значению нельзя.
        target = upload.safe_knowledge_path(KNOWLEDGE_DIR, form.get("name", ""))
        if target is None or not target.is_file():
            body = pages.knowledge_page(self._knowledge_rows(), sess["csrf"], error="Файл не найден.")
            return self._html(404, self._layout("Знания", body, sess["csrf"], active="knowledge"))

        target.unlink()
        self._redirect("/knowledge")


def build_server(address: tuple[str, int]) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(address, Handler)


# ---------------------------------------------------------------- CLI

def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse_exposed_bind(host: str) -> None:
    print(
        "=" * 72 + "\n"
        "ОПАСНО: --host указывает не на loopback-адрес.\n"
        "Эта панель отдаёт персональные медицинские данные и по умолчанию не\n"
        "использует TLS. Штатный доступ — ssh-туннель с рабочей машины:\n\n"
        "    ssh -L 8765:localhost:8765 user@vps\n\n"
        f"Запрошенный host={host!r} слушал бы напрямую снаружи. Это допустимо\n"
        "ТОЛЬКО за TLS-реверс-прокси (см. admin/README-proxy.md) и требует\n"
        "переменной окружения ADMIN_BEHIND_TLS=1, чтобы сессионная кука\n"
        "получила флаг Secure.\n\n"
        "Если вы осознанно принимаете риск отдать эту панель без прокси —\n"
        "повторите запуск с флагом --i-know-this-is-exposed.\n"
        + "=" * 72,
        file=sys.stderr,
    )


def _cmd_set_password() -> int:
    print("Установка пароля администратора. Эта команда НИКОГДА не пишет пароль на диск —")
    print("две строки ниже нужно вручную добавить в ~/.hermes/.env.\n")
    pw1 = getpass.getpass(f"Пароль (минимум {auth.MIN_PASSWORD_LEN} символов): ")
    if len(pw1) < auth.MIN_PASSWORD_LEN:
        print(f"Ошибка: пароль короче {auth.MIN_PASSWORD_LEN} символов.", file=sys.stderr)
        return 1
    pw2 = getpass.getpass("Повторите пароль: ")
    if pw1 != pw2:
        print("Ошибка: пароли не совпадают.", file=sys.stderr)
        return 1
    salt = auth.gen_salt()
    digest = auth.hash_password(pw1, salt)
    print("\nДобавьте в ~/.hermes/.env:\n")
    print(f"ADMIN_PASSWORD_HASH={digest.hex()}")
    print(f"ADMIN_PASSWORD_SALT={salt.hex()}")
    return 0


def _cmd_self_check() -> int:
    """Проверка удаления персоны на временной базе. Главное здесь — не то, что
    целевая персона исчезла, а что данные ВТОРОЙ персоны целы: удаление,
    задевающее соседа, — единственный по-настоящему страшный отказ этой страницы."""
    import sqlite3
    import tempfile
    from pathlib import Path as _Path

    tmp = tempfile.mkdtemp()
    os.environ["HEALTH_DB"] = str(_Path(tmp) / "check.db")
    import importlib
    import health_core.db as _db
    importlib.reload(_db)
    _db.DB_PATH = _Path(os.environ["HEALTH_DB"])

    conn = _db.connect()
    _db.migrate(conn)
    ids = {}
    for tg in (1001, 1002):
        conn.execute("INSERT INTO users(telegram_user_id, created_at) VALUES (?, '2026-08-22 00:00:00')", (tg,))
        uid = conn.execute("SELECT id FROM users WHERE telegram_user_id=?", (tg,)).fetchone()["id"]
        ids[tg] = uid
        conn.execute("INSERT INTO food_log(user_id, eaten_at, meal_slot) VALUES (?, '2026-08-22 09:00:00', 'breakfast')", (uid,))
        flid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        conn.execute("INSERT INTO food_items(food_log_id, name, grams, kcal, protein_g, fat_g, carbs_g) "
                     "VALUES (?, 'еда', 100, 200, 10, 5, 20)", (flid,))
        conn.execute("INSERT INTO body_metrics(user_id, burst_key, measured_at, weight_kg) VALUES (?, ?, '2026-08-22 07:00:00', 90.0)", (uid, f"b{tg}"))
        conn.execute("INSERT INTO water_log(user_id, at, volume_ml) VALUES (?, '2026-08-22 10:00:00', 500)", (uid,))
        conn.execute("INSERT INTO persona_styles(user_id, name, instruction, is_active, created_at) "
                     "VALUES (?, 'debian', 'x', 1, '2026-08-22 00:00:00')", (uid,))
        # daily_watch: FK user_id -> users(id) с PRAGMA foreign_keys=ON — если
        # таблицы нет в PERSONA_TABLES, DELETE FROM users падает IntegrityError.
        conn.execute("INSERT INTO daily_watch(user_id, date, steps, source, created_at) "
                     "VALUES (?, '2026-08-22', 5000, 'manual', '2026-08-22 00:00:00')", (uid,))
    conn.commit()

    victim, survivor = ids[1001], ids[1002]
    _delete_persona(conn, victim)
    conn.commit()

    for table in PERSONA_TABLES:
        n = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE user_id=?", (victim,)).fetchone()["c"]
        assert n == 0, f"{table}: у удалённой персоны осталось {n} строк"
    assert conn.execute("SELECT COUNT(*) c FROM users WHERE id=?", (victim,)).fetchone()["c"] == 0,         "строка users удалённой персоны осталась"
    assert conn.execute(
        "SELECT COUNT(*) c FROM food_items WHERE food_log_id IN (SELECT id FROM food_log WHERE user_id=?)",
        (victim,)).fetchone()["c"] == 0, "food_items удалённой персоны остались"

    # Ради этого всё и затевалось.
    assert conn.execute("SELECT COUNT(*) c FROM users WHERE id=?", (survivor,)).fetchone()["c"] == 1,         "удаление снесло чужую персону"
    for table, expected in (("food_log", 1), ("body_metrics", 1), ("water_log", 1), ("persona_styles", 1)):
        n = conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE user_id=?", (survivor,)).fetchone()["c"]
        assert n == expected, f"{table}: у соседней персоны {n} строк вместо {expected}"
    assert conn.execute(
        "SELECT COUNT(*) c FROM food_items WHERE food_log_id IN (SELECT id FROM food_log WHERE user_id=?)",
        (survivor,)).fetchone()["c"] == 1, "food_items соседней персоны пострадали"
    print("OK: персона удалена целиком, данные соседней персоны нетронуты")

    # --- проверка загрузки выгрузки весов через /actions/import-scale ---
    import csv as _csv
    from health_core.ingest.scale import COLUMNS as _SCALE_COLUMNS

    scale_csv_buf = io.StringIO()
    _w = _csv.DictWriter(scale_csv_buf, fieldnames=list(_SCALE_COLUMNS.keys()))
    _w.writeheader()
    _w.writerow({
        "Время измерения": "20/08/2026 08:00:00", "Вес(kg)": "80.0",
        "Содержание жира(%)": "20.0", "Индекс массы тела": "25.0",
        "Скелетные мыщцы(%)": "40.0", "Мышечная масса(kg)": "76.0",
        "Белки(%)": "14.0", "Скорость обмена веществ(kcal)": "2000",
        "Масса тела без учета жира(kg)": "80.0", "Подкожно-жировая клетчатка(%)": "30.0",
        "Висцеральный жир": "10", "Содержание воды в организме(%)": "45.0",
        "Костная масса(kg)": "4.0", "Метаболический возраст": "40",
        "MAC-адрес устройства": "AA:BB",
    })
    scale_csv_bytes = scale_csv_buf.getvalue().encode("utf-8-sig")

    ok, _msg = _import_scale_upload(conn, survivor, "экспорт весов.csv", scale_csv_bytes)
    assert ok, f"загрузка валидного csv весов не удалась: {_msg}"
    after_first = conn.execute("SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (survivor,)).fetchone()["c"]
    assert after_first == 2, f"ожидалась 1 новая строка body_metrics (было 1, стало {after_first})"

    ok2, _msg2 = _import_scale_upload(conn, survivor, "экспорт весов.csv", scale_csv_bytes)
    assert ok2, f"повторная загрузка того же файла должна пройти без ошибок: {_msg2}"
    after_second = conn.execute("SELECT COUNT(*) c FROM body_metrics WHERE user_id=?", (survivor,)).fetchone()["c"]
    assert after_second == after_first, "повторная загрузка того же файла не должна добавлять строки (дедуп по sha256)"

    ok3, msg3 = _import_scale_upload(conn, survivor, "virus.exe", b"x")
    assert not ok3 and msg3, "загрузка .exe должна быть отклонена с сообщением"
    print("OK: загрузка выгрузки весов импортирует данные, дедуп по хешу держит, посторонние расширения отклонены")

    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Health admin panel (stdlib-only HTTP server).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--set-password", action="store_true")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    ap.add_argument("--i-know-this-is-exposed", action="store_true", dest="exposed_ok")
    args = ap.parse_args(argv)

    auth.load_env_file()

    if args.self_check:
        return _cmd_self_check()

    if args.set_password:
        return _cmd_set_password()

    if not _is_loopback(args.host) and not args.exposed_ok:
        _refuse_exposed_bind(args.host)
        return 1

    if not os.environ.get("ADMIN_PASSWORD_HASH") or not os.environ.get("ADMIN_PASSWORD_SALT"):
        print("ADMIN_PASSWORD_HASH / ADMIN_PASSWORD_SALT не заданы. Сначала запустите:", file=sys.stderr)
        print("  python -m admin.server --set-password", file=sys.stderr)
        return 1

    httpd = build_server((args.host, args.port))
    print(f"health-admin слушает http://{args.host}:{args.port}/ (Ctrl+C для остановки)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
