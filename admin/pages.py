"""HTML for the admin panel — plain f-strings, no template engine (stdlib-only
constraint, see admin/server.py). Every value that came from the database or a
form passes through html.escape() before landing in a page: milestone names,
alert messages, device strings, the lot. That is the entire XSS defense for a
panel serving personal medical data, so it is applied without exception.
"""
import html
from datetime import date, datetime, timezone as _tz_module
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = _tz_module.utc  # timezone-aware UTC sentinel для replace(tzinfo=...)


def _fmt_dt(value, tz_name: str | None = None) -> str:
    """Форматирует строку даты-времени из БД (хранится как UTC) в часовой пояс
    выбранного пользователя. tz_name — строка вида 'Europe/Moscow'; если None
    или невалидная — показывает время в UTC."""
    if value is None:
        return "—"
    s = str(value).strip()
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, Exception):
        tz = ZoneInfo("UTC")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt) + 2].rstrip("Z"), fmt)
            dt = dt.replace(tzinfo=_UTC).astimezone(tz)
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            continue
    return html.escape(s)


_VALID_METRICS = ("weight_kg", "ffm_kg", "fat_pct", "waist")  # must match plugin/tools.py handle_set_milestone


def _e(value) -> str:
    """Escape-or-placeholder: None becomes an em dash, everything else str()+escape."""
    if value is None:
        return "—"
    return html.escape(str(value))


def _n(value, fmt: str = "{:.0f}") -> str:
    """Number-or-dash: None becomes an em dash, else formatted. No escaping needed —
    fmt.format() on a number can never produce markup."""
    return "—" if value is None else fmt.format(value)


_STYLE = """
:root{
  color-scheme:light dark;
  --fg:#0f1521;--muted:#5d6b82;--bg:#f0f3f8;--bg2:#ffffff;
  --card:#ffffff;--border:#dde3ed;--soft:#f1f4fa;
  --accent:#2f5fe0;--accent2:#6a48e8;--ok:#0a7048;--danger:#b8331f;--warn:#b45309;--water:#0d8ba8;
  --on-accent:#fff;
  --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px -10px rgba(16,24,40,.12);
  --r:14px;
}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --fg:#e7edf7;--muted:#94a2b9;--bg:#0b0f16;--bg2:#111722;
  --card:#141b26;--border:#242e3d;--soft:#1a2230;
  --accent:#5b8cff;--accent2:#9b7cff;--ok:#2ec08c;--danger:#f0604d;--warn:#fbbf24;--water:#2bc0e0;
  --on-accent:#0b1020;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 12px 32px -16px rgba(0,0,0,.9);
}}
[data-theme="light"]{
  color-scheme:light;
  --fg:#0f1521;--muted:#5d6b82;--bg:#f0f3f8;--bg2:#ffffff;
  --card:#ffffff;--border:#dde3ed;--soft:#f1f4fa;
  --accent:#2f5fe0;--accent2:#6a48e8;--ok:#0a7048;--danger:#b8331f;--warn:#b45309;--water:#0d8ba8;
  --on-accent:#fff;
  --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px -10px rgba(16,24,40,.12);
}
[data-theme="dark"]{
  color-scheme:dark;
  --fg:#e7edf7;--muted:#94a2b9;--bg:#0b0f16;--bg2:#111722;
  --card:#141b26;--border:#242e3d;--soft:#1a2230;
  --accent:#5b8cff;--accent2:#9b7cff;--ok:#2ec08c;--danger:#f0604d;--warn:#fbbf24;--water:#2bc0e0;
  --on-accent:#0b1020;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 12px 32px -16px rgba(0,0,0,.9);
}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:15px;line-height:1.45;margin:0;
  color:var(--fg);background:var(--bg);-webkit-font-smoothing:antialiased;
  background:
    radial-gradient(1100px 560px at 10% -12%,color-mix(in srgb,var(--accent) 10%,transparent),transparent 62%),
    radial-gradient(900px 480px at 102% -4%,color-mix(in srgb,var(--accent2) 9%,transparent),transparent 58%),
    var(--bg);background-attachment:fixed;transition:background-color .2s,color .2s}

/* theme toggle button */
.theme-toggle{background:var(--soft)!important;color:var(--fg)!important;border:1px solid var(--border)!important;
  box-shadow:none!important;padding:5px 12px!important;font-size:13px!important;border-radius:8px!important;
  cursor:pointer;display:inline-flex;align-items:center;gap:5px;font-weight:500;transition:background .15s,border-color .15s}
.theme-toggle:hover{background:var(--border)!important}

/* reorder table */
.reorder-table{width:100%;border-collapse:collapse;margin-top:10px}
.model-row{cursor:grab;transition:background .15s,box-shadow .15s}
.model-row:active{cursor:grabbing}
.model-row.dragging{opacity:.35;background:var(--soft)}
.model-row.drag-over-top td{border-top:3px solid var(--accent)!important}
.model-row.drag-over-bottom td{border-bottom:3px solid var(--accent)!important}
.drag-handle{cursor:grab;font-size:18px;color:var(--muted);text-align:center;user-select:none;width:36px}
.drag-handle:hover{color:var(--accent)}
.priority-badge-primary{background:color-mix(in srgb,var(--ok) 18%,transparent)!important;color:var(--ok)!important;
  border-color:color-mix(in srgb,var(--ok) 50%,transparent)!important;font-weight:700!important}
.btn-arrow{padding:4px 8px;font-size:12px;border-radius:6px;margin:0 2px;background:var(--soft);color:var(--fg);
  border:1px solid var(--border);cursor:pointer;transition:all .15s}
.btn-arrow:hover:not(:disabled){background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.btn-arrow:disabled{opacity:.25;cursor:default;pointer-events:none}
@keyframes pulseSave{0%{transform:scale(1)}50%{transform:scale(1.03)}100%{transform:scale(1)}}
.highlight-pulse{animation:pulseSave 1.5s infinite ease-in-out}

/* navbar */
.navbar{position:sticky;top:0;z-index:100;background:var(--card);border-bottom:1px solid var(--border);
  box-shadow:var(--shadow);backdrop-filter:blur(8px)}
.nav-top{display:flex;align-items:center;justify-content:space-between;padding:10px 18px 8px;gap:12px;
  border-bottom:1px solid color-mix(in srgb,var(--border) 60%,transparent)}
.nav-brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;color:var(--fg);user-select:none}
.nav-brand .brand-tag{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  padding:2px 7px;border-radius:999px;background:color-mix(in srgb,var(--accent) 15%,transparent);
  color:var(--accent);border:1px solid color-mix(in srgb,var(--accent) 30%,transparent)}
.nav-controls{display:flex;align-items:center;gap:8px}
.btn-logout{background:transparent!important;color:var(--muted)!important;border:1px solid var(--border)!important;
  box-shadow:none!important;padding:5px 12px!important;font-size:13px!important;border-radius:8px!important;
  cursor:pointer;transition:all .15s}
.btn-logout:hover{color:var(--danger)!important;border-color:var(--danger)!important;
  background:color-mix(in srgb,var(--danger) 10%,transparent)!important}
.nav-links{display:flex;align-items:center;gap:3px;padding:6px 14px 8px;overflow-x:auto;
  -webkit-overflow-scrolling:touch;white-space:nowrap;scrollbar-width:none}
.nav-links::-webkit-scrollbar{display:none}
.nav-links a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:8px;font-weight:500;
  font-size:13px;transition:background .15s,color .15s;flex-shrink:0}
.nav-links a:hover{background:var(--soft);color:var(--fg)}
.nav-links a.active{background:linear-gradient(135deg,var(--accent),var(--accent2));color:var(--on-accent);
  font-weight:600;box-shadow:0 3px 12px -3px color-mix(in srgb,var(--accent) 65%,transparent)}

main{max-width:1120px;margin:0 auto;padding:22px 16px 48px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;
  margin-bottom:16px;box-shadow:var(--shadow)}
h1,h2{margin:0 0 12px;letter-spacing:-.01em}
h3{margin:0 0 14px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}

/* tables */
.table-responsive{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0}
.table-responsive>table{width:100%;min-width:540px}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);
  font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--soft)}

/* controls */
input,select,textarea,button{font:inherit;padding:8px 11px;border:1px solid var(--border);border-radius:10px;
  background:var(--bg2);color:var(--fg);transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
button{background:linear-gradient(135deg,var(--accent),var(--accent2));color:var(--on-accent);border:none;cursor:pointer;
  font-weight:600;box-shadow:0 4px 14px -6px color-mix(in srgb,var(--accent) 90%,transparent)}
button:hover{filter:brightness(1.07)}
button.danger{background:linear-gradient(135deg,#c03a26,#8f2716);color:#fff}
.error{color:var(--danger);font-weight:600}
.msg{color:var(--ok);font-weight:600}
pre{white-space:pre-wrap;font-family:inherit;margin:0}
textarea{width:100%;min-height:340px;font-family:ui-monospace,Consolas,monospace;font-size:13px;line-height:1.5}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;
  background:var(--soft);padding:2px 6px;border-radius:6px;border:1px solid var(--border);color:var(--accent)}
.badge-source{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;
  background:var(--soft);border:1px solid var(--border);color:var(--muted)}
.btn-del{padding:2px 8px;font-size:12px;color:var(--danger);border:1px solid var(--border);border-radius:6px;
  background:transparent;cursor:pointer;transition:all .15s}
.btn-del:hover{background:color-mix(in srgb,var(--danger) 14%,transparent);border-color:var(--danger)}
svg{width:100%;height:auto;color:var(--fg)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.row>div{display:flex;flex-direction:column;gap:3px}
label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}
.comment{font-size:12px;color:var(--muted)}

/* hero */
.hero{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.hero .big{font-size:46px;font-weight:700;line-height:1;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
@supports(-webkit-background-clip:text){.hero .big{
  background:linear-gradient(135deg,var(--fg) 20%,var(--accent));-webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent}}
.hero .big small{font-size:16px;opacity:.55;font-weight:500;margin-left:5px;letter-spacing:0;
  -webkit-text-fill-color:var(--fg)}
.hero .k{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  padding:4px 9px;border-radius:999px;margin-left:10px;vertical-align:middle;
  background:linear-gradient(135deg,#12905f,#0d6e48);color:#fff;-webkit-text-fill-color:#fff;
  box-shadow:0 4px 12px -4px rgba(18,144,95,.8)}

/* stat tiles */
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(146px,1fr));gap:12px}
.stat{position:relative;overflow:hidden;background:linear-gradient(160deg,var(--soft),var(--card));
  border:1px solid var(--border);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;
  transition:transform .15s,box-shadow .15s}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
.stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(var(--accent),var(--accent2));opacity:.85}
.stat .k{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
.stat .v{font-size:24px;font-weight:650;line-height:1.1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .v small{font-size:12px;opacity:.55;font-weight:500;margin-left:3px}
.stat .sub{font-size:12px;font-weight:600}
.stat .v.down{color:var(--ok)}.stat .v.up{color:var(--danger)}
.stat .v.txt{font-size:15px;font-weight:600;letter-spacing:0;padding:4px 0}
.stats+.chips{margin-top:12px}
.hero+.stats{margin-top:0}
.sub.down{color:var(--ok)}.sub.up{color:var(--danger)}

/* macro bars */
.bars{display:flex;flex-direction:column;gap:15px}
.macro .top{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;gap:10px}
.macro .name{font-weight:600}
.macro .meta{color:var(--muted);font-variant-numeric:tabular-nums;text-align:right}
.track{height:10px;background:var(--soft);border:1px solid var(--border);border-radius:999px;overflow:hidden}
.fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.fill.warn{background:linear-gradient(90deg,var(--danger),#a8321f)}
.fill.water{background:linear-gradient(90deg,var(--water),var(--accent))}

/* chips */
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:12px;font-weight:600;padding:5px 12px;border-radius:999px;border:1px solid var(--border);
  background:var(--soft);color:var(--fg);display:inline-flex;align-items:center;gap:4px;vertical-align:middle}
.chip.ok{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok);
  border-color:color-mix(in srgb,var(--ok) 45%,transparent)}
.chip.bad{background:color-mix(in srgb,var(--danger) 14%,transparent);color:var(--danger);
  border-color:color-mix(in srgb,var(--danger) 45%,transparent)}
.chip.warn{background:color-mix(in srgb,var(--warn) 14%,transparent);color:var(--warn);
  border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.chip.info{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);
  border-color:color-mix(in srgb,var(--accent) 45%,transparent)}

/* meals */
.meal{padding:10px 13px;margin-bottom:8px;border-radius:11px;background:var(--soft);
  border-left:3px solid var(--accent)}
.meal .mh{font-weight:600;font-size:14px;font-variant-numeric:tabular-nums}
.meal .mi{font-size:13px;color:var(--muted);margin-top:2px}
.meal summary{cursor:pointer;list-style:none}
.meal summary::-webkit-details-marker{display:none}
.meal summary::before{content:"▸";display:inline-block;width:1em;color:var(--accent);transition:transform .15s}
.meal[open] summary::before{transform:rotate(90deg)}
.meal-items{width:100%;min-width:340px;border-collapse:collapse;margin-top:8px;font-size:13px;font-variant-numeric:tabular-nums}
.meal-items th{text-align:left;font-weight:600;color:var(--muted);padding:3px 6px;border-bottom:1px solid var(--border)}
.meal-items td{padding:3px 6px;border-bottom:1px solid var(--border);color:var(--fg)}
.meal-items th:not(:first-child),.meal-items td:not(:first-child){text-align:right}
.meal-items tr:last-child td{border-bottom:none}

/* chart — styled here, not via var() inside SVG presentation attributes */
.chart .gl{stroke:currentColor;stroke-opacity:.12}
.chart .lbl{fill:var(--muted)}
.chart .val{fill:var(--accent)}
.chart .ln{stroke:var(--accent)}
.chart .dot{fill:var(--accent)}
.chart .g0{stop-color:var(--accent);stop-opacity:.32}
.chart .g1{stop-color:var(--accent);stop-opacity:0}
/* Полоса прогноза: заливка между нижней и верхней границей. */
.chart .band{fill:var(--accent);fill-opacity:.16}
.chart .bl{stroke:var(--accent);stroke-opacity:.45;stroke-dasharray:4 4}

/* guardrails */
.guard-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;
  box-shadow:var(--shadow);border-left:4px solid var(--border);display:flex;flex-direction:column;gap:10px;
  transition:transform .15s,box-shadow .15s}
.guard-card:hover{transform:translateY(-2px);box-shadow:0 4px 16px -4px rgba(0,0,0,.15)}
.guard-card.status-ok{border-left-color:var(--ok)}
.guard-card.status-warning{border-left-color:var(--warn);background:color-mix(in srgb,var(--warn) 3%,var(--card))}
.guard-card.status-critical{border-left-color:var(--danger);background:color-mix(in srgb,var(--danger) 4%,var(--card))}
.guard-card.status-info{border-left-color:var(--accent)}
.guard-card.status-nodata{border-left-color:var(--muted);opacity:.85}
.guard-header{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px}
.guard-title{font-size:15px;font-weight:700;color:var(--fg);margin:0}
.guard-category{font-size:11px;font-weight:600;text-transform:uppercase;color:var(--muted);letter-spacing:.05em}
.guard-metric-box{display:grid;grid-template-columns:1fr 1fr;gap:10px;background:var(--soft);padding:10px 14px;
  border-radius:10px;border:1px solid var(--border)}
.guard-val-label{font-size:11px;text-transform:uppercase;color:var(--muted);font-weight:600}
.guard-val-text{font-size:14px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.guard-details{margin-top:4px;font-size:13px;background:var(--soft);padding:10px 14px;border-radius:8px;border:1px solid var(--border)}
.guard-details summary{cursor:pointer;font-weight:600;color:var(--accent);user-select:none}
.guard-details p{margin:6px 0 0;line-height:1.45}
.guard-alert-box{border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:14px}
.guard-alert-box.critical{background:color-mix(in srgb,var(--danger) 8%,var(--card));border-color:var(--danger)}
.guard-alert-box.warning{background:color-mix(in srgb,var(--warn) 8%,var(--card));border-color:var(--warn)}
.guard-form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin-top:10px}
.guard-input-row{display:flex;flex-direction:column;gap:5px;background:var(--soft);padding:12px 14px;border-radius:10px;border:1px solid var(--border)}
.guard-input-row label{font-size:12px;font-weight:600;color:var(--fg);text-transform:none;letter-spacing:0}
.guard-input-row input{width:100%}
.guard-input-row .comment{font-size:11px;color:var(--muted);margin-top:2px;line-height:1.4}

@media(max-width:768px){
  main{padding:14px 10px 36px}
  .card{padding:14px;border-radius:12px;margin-bottom:12px}
  .hero .big{font-size:36px}
  .stats{grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
  .stat{padding:10px 12px}
  .stat .v{font-size:20px}
  .grid{grid-template-columns:1fr;gap:12px}
  .row{flex-direction:column;align-items:stretch}
  .row>div{width:100%}
  .nav-top{padding:8px 12px 6px}
  .nav-links{padding:4px 10px 6px}
}
"""

NAV = [
    ("dashboard", "/", "Дашборд"),
    ("guards", "/guards", "Гардрейлы"),
    ("milestones", "/milestones", "Вехи"),
    ("thresholds", "/thresholds", "Пороги"),
    ("keys", "/keys", "Ключи и Модели"),
    ("alerts", "/alerts", "Алерты"),
    ("personas", "/personas", "Персоны"),
    ("drafts", "/drafts", "Черновики карт"),
    ("actions", "/actions", "Действия"),
    ("knowledge", "/knowledge", "Знания"),
    ("plans", "/plans", "Планы"),
    ("workouts", "/workouts", "Тренировки"),
    ("forecast", "/forecast", "Прогноз"),
]


def _document(title: str, body_html: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"ru\"><head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)} — Health Admin</title>\n"
        "<script>\n"
        "(function(){\n"
        "  try{\n"
        "    var t = localStorage.getItem('health_admin_theme');\n"
        "    if(t) document.documentElement.setAttribute('data-theme', t);\n"
        "  }catch(e){}\n"
        "})();\n"
        "function toggleTheme(){\n"
        "  try{\n"
        "    var cur = document.documentElement.getAttribute('data-theme');\n"
        "    var isDark = cur === 'dark' || (!cur && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);\n"
        "    var next = isDark ? 'light' : 'dark';\n"
        "    document.documentElement.setAttribute('data-theme', next);\n"
        "    localStorage.setItem('health_admin_theme', next);\n"
        "    updateThemeUI();\n"
        "  }catch(e){}\n"
        "}\n"
        "function updateThemeUI(){\n"
        "  try{\n"
        "    var cur = document.documentElement.getAttribute('data-theme');\n"
        "    var isDark = cur === 'dark' || (!cur && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);\n"
        "    var icon = document.getElementById('theme-icon');\n"
        "    var txt = document.getElementById('theme-text');\n"
        "    if(icon) icon.textContent = isDark ? '☀️' : '🌙';\n"
        "    if(txt) txt.textContent = isDark ? 'Светлая' : 'Тёмная';\n"
        "  }catch(e){}\n"
        "}\n"
        "document.addEventListener('DOMContentLoaded', updateThemeUI);\n"
        "</script>\n"
        "<script>\n"
        "document.addEventListener('submit', function(e){\n"
        "  var msg = e.target && e.target.dataset && e.target.dataset.confirm;\n"
        "  if(msg && !window.confirm(msg)) e.preventDefault();\n"
        "});\n"
        "</script>\n"
        f"<style>{_STYLE}</style>\n"
        f"</head><body>\n{body_html}\n</body></html>"
    )


def _csrf_field(csrf_token: str, user_id: int | None = None) -> str:
    return f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">' + _uid_field(user_id)


def _uid_field(user_id: int | None) -> str:
    """Persona the form was rendered for; the server rejects the POST if the
    session's persona changed meanwhile (another tab)."""
    return f'<input type="hidden" name="form_user_id" value="{int(user_id)}">' if user_id is not None else ""


def _user_selector_html(users: list[dict] | None, selected_user_id, current_path: str) -> str:
    """Compact <select> of personas for the nav. Empty when there are 0 or 1
    users (nothing to switch between) — matches the no-user page still working
    with no selector at all. Changing it navigates to the CURRENT path (query
    string stripped) with ?user=<id> — a tiny inline onchange, same style as
    the existing theme-toggle inline script."""
    if not users or len(users) < 2:
        return ""
    base_path = current_path.split("?", 1)[0] or "/"
    opts = []
    for u in users:
        label = f"#{u['id']} · tg {u['telegram_user_id']}"
        if u.get("username"):
            label += f" · {u['username']}"
        sel = " selected" if u["id"] == selected_user_id else ""
        opts.append(f'<option value="{_e(u["id"])}"{sel}>{_e(label)}</option>')
    return (
        f'<select id="user-selector" title="Выбранный пользователь" '
        # путь запроса в JS-строку не подставляем: html.escape в атрибуте
        # раскодируется браузером до исполнения JS — та же дыра, что с confirm()
        f'onchange="location.search=\'?user=\'+encodeURIComponent(this.value)" '
        f'style="max-width:220px">{"".join(opts)}</select>'
    )


def layout(title: str, body: str, csrf_token: str, active: str | None = None,
           users: list[dict] | None = None, selected_user_id=None,
           current_path: str = "/") -> str:
    links = []
    for key, href, label in NAV:
        cls = ' class="active"' if key == active else ""
        links.append(f'<a href="{href}"{cls}>{html.escape(label)}</a>')
    selector_html = _user_selector_html(users, selected_user_id, current_path)
    header_html = (
        f'<header class="navbar">'
        f'<div class="nav-top">'
        f'<div class="nav-brand">'
        f'<span>🩺</span> <span>Health Agent</span> <span class="brand-tag">Admin</span>'
        f'</div>'
        f'<div class="nav-controls">'
        f'{selector_html}'
        f'<button type="button" id="theme-toggle-btn" onclick="toggleTheme()" class="theme-toggle" '
        f'title="Переключить светлую / тёмную тему">'
        f'<span id="theme-icon">🌓</span> <span id="theme-text">Тема</span>'
        f'</button>'
        f'<form method="post" action="/logout" style="margin:0">{_csrf_field(csrf_token)}'
        f'<button type="submit" class="btn-logout" title="Выйти из системы">Выход ↪</button></form>'
        f'</div>'
        f'</div>'
        f'<nav class="nav-links">{"".join(links)}</nav>'
        f'</header>'
    )
    return _document(title, f"{header_html}<main>{body}</main>")


def error_page(message: str) -> str:
    body = f'<main style="max-width:560px;margin:60px auto;padding:16px"><div class="card"><p class="error">{html.escape(message)}</p></div></main>'
    return _document("Ошибка", body)


def no_user_page() -> str:
    return '<div class="card"><p>В базе нет ни одного пользователя. Зарегистрируйте пользователя через Telegram-бота (/start), затем обновите страницу.</p></div>'


def login_page(csrf_token: str, error: str | None = None) -> str:
    err_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    body = f"""
<div style="position:fixed;top:16px;right:16px;z-index:10">
  <button type="button" id="theme-toggle-btn" onclick="toggleTheme()" class="theme-toggle" title="Переключить тему">
    <span id="theme-icon">🌓</span> <span id="theme-text">Тема</span>
  </button>
</div>
<main style="max-width:380px;margin:80px auto;padding:16px">
  <div class="card" style="padding:28px 24px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
      <span style="font-size:28px">🩺</span>
      <h1 style="margin:0;font-size:22px">Health Admin</h1>
    </div>
    {err_html}
    <form method="post" action="/login">
      {_csrf_field(csrf_token)}
      <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px">
        <label for="password">Пароль администратора</label>
        <input type="password" id="password" name="password" placeholder="••••••••" style="width:100%" autofocus required>
      </div>
      <button type="submit" style="width:100%;padding:10px;font-size:15px">Войти в панель</button>
    </form>
  </div>
</main>"""
    return _document("Вход", body)


def login_ok_page() -> str:
    body = '<main style="max-width:360px;margin:80px auto;padding:16px"><div class="card"><p class="msg">Вход выполнен.</p><p><a href="/">Перейти в панель</a></p></div></main>'
    return _document("Вход выполнен", body)


# ---------------------------------------------------------------- SVG charts

def svg_line_chart(points: list[tuple[str, float]], label: str, width: int = 560, height: int = 200) -> str:
    """Server-rendered inline SVG: area gradient + polyline + grid + end dot —
    no chart library, no CDN, no JavaScript. points: (iso_timestamp, value),
    ascending by time. Renders an empty-state message if fewer than 2 points.
    Colours come from CSS classes in _STYLE, so dark mode needs no second render."""
    label_e = html.escape(label)
    if len(points) < 2:
        return (
            f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{label_e}">'
            f'<text class="lbl" x="{width / 2}" y="{height / 2}" font-size="13" text-anchor="middle">'
            f'Недостаточно данных: {label_e}</text></svg>'
        )
    # id must be unique per chart on the page; label text alone collides for
    # labels differing only in punctuation ("Вес, кг" vs "Вес (кг)")
    gid = f"g{abs(hash(label)) % 10 ** 9}"
    left, right, top, bottom = 44, 14, 22, 26
    values = [v for _, v in points]
    vmin, vmax = min(values), max(values)
    vspan = (vmax - vmin) or 1.0
    n = len(points)

    def x_of(i: int) -> float:
        return left + (width - left - right) * i / (n - 1)

    def y_of(v: float) -> float:
        return height - bottom - (height - top - bottom) * (v - vmin) / vspan

    coords = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, (_, v) in enumerate(points))
    area = f"M{x_of(0):.1f},{height - bottom} L" + coords.replace(" ", " L") + f" L{x_of(n - 1):.1f},{height - bottom} Z"
    grid = []
    for frac in (0.0, 0.5, 1.0):
        v = vmin + vspan * frac
        y = y_of(v)
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                    f'class="gl" stroke-dasharray="3 4"/>')
        grid.append(f'<text class="lbl" x="{left - 8}" y="{y + 4:.1f}" font-size="10" text-anchor="end">'
                    f'{v:.1f}</text>')
    lx, ly = x_of(n - 1), y_of(values[-1])
    return "\n".join([
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{label_e}">',
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">',
        f'<stop class="g0" offset="0%"/><stop class="g1" offset="100%"/>',
        "</linearGradient></defs>",
        *grid,
        f'<path d="{area}" fill="url(#{gid})"/>',
        f'<polyline points="{coords}" class="ln" fill="none" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>',
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" class="dot" r="7" fill-opacity="0.22"/>',
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" class="dot" r="3.5"/>',
        f'<text class="lbl" x="{left}" y="{top - 6}" font-size="11" font-weight="600">{label_e}</text>',
        f'<text class="lbl" x="{left}" y="{height - 6}" font-size="10">{html.escape(points[0][0][:10])}</text>',
        f'<text class="lbl" x="{width - right}" y="{height - 6}" font-size="10" text-anchor="end">'
        f'{html.escape(points[-1][0][:10])}</text>',
        f'<text class="val" x="{width - right}" y="{top - 6}" font-size="11" font-weight="700" text-anchor="end">'
        f'{values[-1]:.1f}</text>',
        "</svg>",
    ])


def svg_band_chart(points: list[dict], label: str, actual: list[tuple[str, float]] | None = None,
                   width: int = 560, height: int = 220) -> str:
    """График прогноза: полоса между lo и hi плюс центральная линия.

    points: [{"date","lo","mid","hi"}] по возрастанию даты.
    actual: фактические замеры (дата, вес) — рисуются сплошной слева от прогноза,
    чтобы было видно, откуда кривая выходит.

    Полоса, а не одна линия, здесь принципиальна: прогноз массы — оценка с
    известной ошибкой, и одиночная кривая читается как обещание."""
    label_e = html.escape(label)
    if len(points) < 2:
        return (
            f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{label_e}">'
            f'<text class="lbl" x="{width / 2}" y="{height / 2}" font-size="13" text-anchor="middle">'
            f'Недостаточно данных: {label_e}</text></svg>'
        )
    left, right, top, bottom = 44, 14, 22, 26
    actual = actual or []
    n_a = len(actual)
    n = n_a + len(points)
    vals = [v for _, v in actual] + [p["lo"] for p in points] + [p["hi"] for p in points]
    vmin, vmax = min(vals), max(vals)
    vspan = (vmax - vmin) or 1.0

    def x_of(i: int) -> float:
        return left + (width - left - right) * i / (n - 1)

    def y_of(v: float) -> float:
        return height - bottom - (height - top - bottom) * (v - vmin) / vspan

    def seq(key):
        return [(x_of(n_a + i), y_of(p[key])) for i, p in enumerate(points)]

    lo, hi, mid = seq("lo"), seq("hi"), seq("mid")
    # Полигон полосы: вперёд по нижней границе, назад по верхней.
    band = " ".join(f"{x:.1f},{y:.1f}" for x, y in lo + hi[::-1])
    mid_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in mid)
    lo_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in lo)
    hi_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in hi)

    grid = []
    for frac in (0.0, 0.5, 1.0):
        v = vmin + vspan * frac
        y = y_of(v)
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                    f'class="gl" stroke-dasharray="3 4"/>')
        grid.append(f'<text class="lbl" x="{left - 8}" y="{y + 4:.1f}" font-size="10" text-anchor="end">'
                    f'{v:.1f}</text>')

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{label_e}">',
        *grid,
        f'<polygon points="{band}" class="band"/>',
        f'<polyline points="{lo_pts}" class="bl" fill="none" stroke-width="1.5"/>',
        f'<polyline points="{hi_pts}" class="bl" fill="none" stroke-width="1.5"/>',
        f'<polyline points="{mid_pts}" class="ln" fill="none" stroke-width="2.5" '
        f'stroke-dasharray="6 3" stroke-linejoin="round"/>',
    ]
    if n_a >= 2:
        a_pts = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, (_, v) in enumerate(actual))
        parts.insert(-4, f'<polyline points="{a_pts}" class="ln" fill="none" stroke-width="2.5" '
                         f'stroke-linejoin="round" stroke-linecap="round"/>')
    ex, ey = mid[-1]
    parts += [
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" class="dot" r="7" fill-opacity="0.22"/>',
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" class="dot" r="3.5"/>',
        f'<text class="lbl" x="{left}" y="{top - 6}" font-size="11" font-weight="600">{label_e}</text>',
        f'<text class="lbl" x="{left}" y="{height - 6}" font-size="10">'
        f'{html.escape((actual[0][0] if actual else points[0]["date"])[:10])}</text>',
        f'<text class="lbl" x="{width - right}" y="{height - 6}" font-size="10" text-anchor="end">'
        f'{html.escape(points[-1]["date"][:10])}</text>',
        f'<text class="val" x="{width - right}" y="{top - 6}" font-size="11" font-weight="700" '
        f'text-anchor="end">{points[-1]["lo"]:.1f}–{points[-1]["hi"]:.1f}</text>',
        "</svg>",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- dashboard

def _stat(label: str, value: str, sub: str = "", sub_cls: str = "", v_cls: str = "") -> str:
    sub_html = f'<span class="sub {sub_cls}">{html.escape(sub)}</span>' if sub else ""
    return (f'<div class="stat"><span class="k">{html.escape(label)}</span>'
            f'<span class="v {v_cls}">{value}</span>{sub_html}</div>')


def _trend_stat(label: str, value, unit: str, good: str = "") -> str:
    """Плитка тренда: значение со знаком, окрашенное по тому, в какую сторону
    изменение считается хорошим. good="down" — снижение в плюс (вес, талия),
    good="up" — рост в плюс (тощая масса), "" — нейтрально (WHR, TDEE)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _stat(label, _e(value))
    us = f"<small>{html.escape(unit)}</small>" if unit else ""
    if not good:
        return _stat(label, f"{v:g}{us}")
    better = (v < 0) if good == "down" else (v > 0)
    cls = "" if v == 0 else ("down" if better else "up")
    return _stat(label, f"{v:+.1f}{us}", "", cls, cls)


def _macro_bar(name: str, val: float, target, unit: str, dec: int, water: bool = False, floor: bool = False) -> str:
    """Одна строка бюджета: имя, факт/цель/%/остаток и заливка. Без цели — только факт.
    floor=True for metrics where more is better (e.g. fiber): never warns even if pct > 105."""
    if val is None:
        # Ноль и «не знаем» — разные вещи. Сумма по пустой колонке даёт 0.0, и
        # «Клетчатка 0/25» читалось бы как «не ел клетчатку» вместо «не записано».
        return (f'<div class="macro"><div class="top"><span class="name">{html.escape(name)}</span>'
                f'<span class="meta">нет данных</span></div></div>')
    if not target:
        return (f'<div class="macro"><div class="top"><span class="name">{html.escape(name)}</span>'
                f'<span class="meta">{val:.{dec}f} {html.escape(unit)}</span></div></div>')
    pct = val / target * 100 if target else 0
    rem = max(0.0, target - val)
    cls = "water" if water else ("" if floor else ("warn" if pct > 105 else ""))
    return (
        f'<div class="macro"><div class="top">'
        f'<span class="name">{html.escape(name)}</span>'
        f'<span class="meta">{val:.{dec}f} / {target:.{dec}f} {html.escape(unit)} · {pct:.0f}% · ост {rem:.{dec}f}</span>'
        f'</div><div class="track"><div class="fill {cls}" style="width:{min(100.0, max(0.0, pct)):.0f}%"></div></div></div>'
    )


def dashboard_page(alerts: list[dict], tr: dict, latest, target: dict | None,
                    history: list, day: dict | None, meals: list, base_weight,
                    day_delta, csrf_token: str, guards_status: list[dict] | None = None) -> str:
    # ── Hero + биометрия ───────────────────────────────────────────────
    if latest is None:
        hero = '<div class="hero"><div class="big">Нет замеров</div></div>'
        stats = ""
    else:
        m = dict(latest)
        w = m.get("weight_kg")
        w_hist = [r["weight_kg"] for r in history if r["weight_kg"] is not None]
        new_low = w is not None and w_hist and w <= min(w_hist)
        tag = '<span class="tag">NEW LOW</span>' if new_low else ""
        hero = (
            f'<div class="hero"><div class="big">{_e(w)}<small>кг</small>{tag}</div>'
            f'<div class="k">{_e(str(m.get("measured_at"))[:16])}</div></div>'
        )
        cells = []
        if day_delta is not None:
            cells.append(_stat("⏱️ За день", f'{day_delta:+.1f}<small>кг</small>',
                               "снижение" if day_delta < 0 else "рост",
                               "down" if day_delta <= 0 else "up"))
        if base_weight is not None and w is not None:
            td = w - base_weight
            pct = td / base_weight * 100 if base_weight else 0
            cells.append(_stat("🏁 От старта", f'{td:+.1f}<small>кг</small>', f'{pct:+.1f}%',
                               "down" if td <= 0 else "up"))
        for key, label, unit in [
            ("fat_pct", "🍖 Жир", "%"), ("muscle_mass_kg", "💪 Мышцы", "кг"), ("ffm_kg", "📊 Сухая", "кг"),
            ("visceral_fat", "🫀 Висц. жир", ""), ("water_pct", "💧 Вода", "%"),
            ("bmi", "⚖️ BMI", ""), ("metabolic_age", "🧠 Метаб. возр.", "лет"),
        ]:
            v = m.get(key)
            if v is not None:
                us = f'<small>{unit}</small>' if unit else ""
                cells.append(_stat(label, f'{_e(v)}{us}'))
        if target is not None:
            cells.append(_stat("⚡ BMR", f'{target["bmr_floor"]:.0f}<small>ккал</small>'))
        stats = f'<div class="stats">{"".join(cells)}</div>'

    # ── Бюджет КБЖУ/вода ───────────────────────────────────────────────
    if day is None:
        budget = "<p>Нет данных за сегодня.</p>"
    else:
        rows = [
            _macro_bar("Калории", day["kcal_eaten"], day.get("kcal_target"), "ккал", 0),
            _macro_bar("Белок", day["protein_g"], day.get("protein_g_target"), "г", 0),
            _macro_bar("Жиры", day["fat_g"], day.get("fat_g_target"), "г", 0),
            _macro_bar("Углеводы", day["carb_g"], day.get("carbs_g_target"), "г", 0),
            _macro_bar("Клетчатка", day.get("fiber_g") if day.get("fiber_known") else None,
                       day.get("fiber_g_target"), "г", 1, floor=True),
            _macro_bar("Вода", (day["water_ml"] or 0) / 1000,
                       (day["water_target_ml"] / 1000) if day.get("water_target_ml") else None, "л", 1, water=True),
        ]
        budget = f'<div class="bars">{"".join(rows)}</div>'

    # ── Гарды ──────────────────────────────────────────────────────────
    if guards_status:
        active_guards = [g for g in guards_status if g.get("status") in ("critical", "warning")]
        ok_count = sum(1 for g in guards_status if g.get("status") == "ok")
        warn_count = sum(1 for g in guards_status if g.get("status") == "warning")
        crit_count = sum(1 for g in guards_status if g.get("status") == "critical")
        chips = [f'<span class="chip ok">✓ {ok_count} в норме</span>']
        if crit_count:
            chips.append(f'<span class="chip bad">🔴 {crit_count} критических</span>')
        if warn_count:
            chips.append(f'<span class="chip warn">🟡 {warn_count} предупреждений</span>')

        if active_guards:
            alert_cards = []
            for g in active_guards:
                st = g.get("status", "warning")
                badge_cls = "bad" if st == "critical" else "warn"
                badge_lbl = "🔴 Критический риск" if st == "critical" else "🟡 Внимание / Предупреждение"
                alert_cards.append(
                    f'<div class="guard-alert-box {st}" style="margin-top:10px;">'
                    f'<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">'
                    f'<div><span class="chip {badge_cls}" style="margin-bottom:4px;">{badge_lbl}</span>'
                    f'<h4 style="margin:4px 0 2px;font-size:15px;color:var(--fg);">{_e(g.get("name"))} <code style="font-size:11px;">{_e(g.get("code"))}</code></h4>'
                    f'<span class="guard-category">{_e(g.get("category"))}</span></div>'
                    f'<div style="font-size:13px;text-align:right;">'
                    f'<span style="color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:600;display:block;">Зафиксировано:</span>'
                    f'<b style="font-variant-numeric:tabular-nums;">{_e(g.get("current_val"))}</b> <span style="color:var(--muted);">(порог: {_e(g.get("threshold_val"))})</span>'
                    f'</div></div>'
                    f'<p style="margin:8px 0;font-size:13px;line-height:1.45;color:var(--fg);">{_e(g.get("message"))}</p>'
                    f'<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:12px;display:flex;flex-direction:column;gap:5px;margin-top:6px;">'
                    f'<div><strong style="color:var(--fg);">🧬 Обоснование:</strong> <span style="color:var(--muted);">{_e(g.get("rationale"))}</span></div>'
                    f'<div><strong style="color:var(--fg);">🎯 Рекомендация:</strong> <span style="color:var(--muted);">{_e(g.get("action"))}</span></div>'
                    f'</div></div>'
                )
            guards_html = f'<div class="chips">{"".join(chips)}</div>' + "".join(alert_cards)
        else:
            guards_html = (
                f'<div class="chips">{"".join(chips)}</div>'
                '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:8px;">'
                '<p style="margin:0;font-size:13px;color:var(--muted);">'
                'Все 16 детерминированных гардрейлов в норме: сохранение мышечной массы (LBM), базовый обмен (BMR floor), вариативность сахара и липиды для препаратов под контролем.'
                '</p>'
                '<a href="/guards" class="chip info" style="text-decoration:none;white-space:nowrap;">Подробнее &rarr;</a>'
                '</div>'
            )
    elif alerts:
        seen, chips = set(), []
        for a in alerts:
            code = a.get("code")
            if code in seen:
                continue
            seen.add(code)
            chips.append(f'<span class="chip bad" title="{_e(a.get("message"))}">⚠ {_e(code)}</span>')
        guards_html = f'<div class="chips">{"".join(chips)}</div>'
    else:
        guards_html = '<div class="chips"><span class="chip ok">✓ Все гарды в норме</span></div>'

    # ── Лог еды ────────────────────────────────────────────────────────
    if meals:
        blocks = []
        for mm in meals:
            emoji, label = _MEAL_LABELS_WEB.get(mm["meal_slot"], ("🍽", "Приём"))
            clock = str(mm["eaten_at"] or "")[11:16]
            items = mm["items"]
            if items:
                item_rows = "".join(
                    f'<tr><td>{_e(i.get("name"))}</td><td>{_n(i.get("grams"))}</td>'
                    f'<td>{_n(i.get("kcal"))}</td><td>{_n(i.get("protein_g"))}</td>'
                    f'<td>{_n(i.get("fat_g"))}</td><td>{_n(i.get("carbs_g"))}</td>'
                    f'<td>{_n(i.get("fiber_g"))}</td></tr>'
                    for i in items
                )
                items_html = (
                    '<div class="table-responsive"><table class="meal-items"><tr><th>Продукт</th><th>Г</th><th>Ккал</th>'
                    f'<th>Б</th><th>Ж</th><th>У</th><th>Кл</th></tr>{item_rows}</table></div>'
                )
            else:
                items_html = '<p class="mi">Нет данных по составу.</p>'
            blocks.append(
                f'<details class="meal"><summary class="mh">{emoji} {html.escape(clock)} {html.escape(label)} · '
                f'{mm["kcal"]:.0f} ккал · Б {mm["protein_g"]:.0f} · Ж {mm["fat_g"]:.0f} · У {mm["carbs_g"]:.0f}</summary>'
                f'{items_html}</details>'
            )
        meals_html = "".join(blocks)
    else:
        meals_html = "<p>За сегодня ничего не записано.</p>"

    # ── Тренды + цель ──────────────────────────────────────────────────
    # Знак сам по себе ничего не значит: минус по весу и талии — успех, минус
    # по тощей массе — потеря мышц, то есть провал. Отсюда разный good.
    trends_html = '<div class="stats">' + "".join([
        _trend_stat("🧍 Вес", tr.get("weight_delta_kg"), "кг", "down"),
        _trend_stat("🦴 Масса", tr.get("lbm_delta_kg"), "кг", "up"),
        _trend_stat("📏 Талия", tr.get("waist_delta_cm"), "см", "down"),
        _trend_stat("⌛ WHR", tr.get("whr"), ""),
        _trend_stat("💨 TDEE", tr.get("actual_tdee_kcal"), "ккал"),
    ]) + "</div>"
    trends_title = f"Тренды ({_e(tr.get('window_days'))} дней)"
    if target is None:
        target_html = "<p>Недостаточно данных для расчёта цели.</p>"
    else:
        chips = []
        if target.get("refeed"):
            chips.append('<span class="chip ok">🍚 Рефид сегодня</span>')
        chips.append('<span class="chip bad">⚠ Срок недостижим</span>' if target.get("deadline_unreachable")
                     else '<span class="chip ok">✓ Срок реалистичен</span>')
        target_html = (
            f'<div class="hero"><div class="big">{target["kcal"]:.0f}<small>ккал</small></div>'
            f'<div class="k">источник: {_e(target.get("source"))}</div></div>'
            f'<div class="stats">'
            + _stat("🛡️ BMR floor", f'{target["bmr_floor"]:.0f}<small>ккал</small>')
            + _stat("🔋 Тренировки", f'{target["tcx_net"]:+.0f}<small>ккал</small>')
            + _stat("🎯 Веха", _e(target.get("active_milestone")), "", "", "txt")
            + f'</div><div class="chips">{"".join(chips)}</div>'
        )

    weight_points = [(r["measured_at"], r["weight_kg"]) for r in history if r["weight_kg"] is not None]
    ffm_points = [(r["measured_at"], r["ffm_kg"]) for r in history if r["ffm_kg"] is not None]

    return f"""
<div class="card">{hero}{stats}</div>
<div class="card"><h3>Бюджет КБЖУ / вода (сегодня)</h3>{budget}</div>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;">
    <h3 style="margin:0;">Гардрейлы безопасности</h3>
    <a href="/guards" style="font-size:12px;font-weight:600;color:var(--accent);text-decoration:none;">Управление и все правила &rarr;</a>
  </div>
  {guards_html}
</div>
<div class="grid">
  <div class="card"><h3>Вес, 90 дней</h3>{svg_line_chart(weight_points, "Вес, кг")}</div>
  <div class="card"><h3>Тощая масса (FFM), 90 дней</h3>{svg_line_chart(ffm_points, "FFM, кг")}</div>
</div>
<div class="card"><h3>Лог еды (сегодня)</h3>{meals_html}</div>
<div class="grid">
  <div class="card"><h3>{trends_title}</h3>{trends_html}</div>
  <div class="card"><h3>Цель на сегодня</h3>{target_html}</div>
</div>
"""


_MEAL_LABELS_WEB = {
    "breakfast": ("🍳", "Завтрак"),
    "lunch": ("🍲", "Обед"),
    "dinner": ("🍛", "Ужин"),
    "snack": ("🍎", "Перекус"),
}


# ---------------------------------------------------------------- guards

GUARD_FORM_GROUPS = [
    {
        "category": "💪 Сохранение мышечной массы (FFM / LBM)",
        "desc": "Контроль катаболизма тощей массы за 14 дней и за всю историю, защита от саркопении.",
        "fields": [
            ("lbm_ratio_threshold", "Порог потери мышц (14 дней)", "Доля тощей массы в потере за 14 дней (по умолч. 0.15 = 15%). Выше — риск катаболизма."),
            ("lbm_ratio_window_days", "Окно контроля мышц (дней)", "Длина скользящего окна для расчёта дельты тощей массы (по умолч. 14)."),
            ("lbm_ratio_min_points", "Мин. точек в окне мышц", "Мин. количество замеров состава тела за окно для расчёта (по умолч. 4)."),
            ("lbm_drift_threshold", "Накопительный дрейф мышц", "Доля потери тощей массы за всё время от старта (по умолч. 0.20 = 20%)."),
            ("ffm_noise_floor_kg", "Порог шума биоимпеданса (кг)", "Если потеря FFM меньше шума весов, алерт не выставляется (по умолч. 0.5 кг)."),
            ("ffmi_floor_m", "Минимальный FFMI, мужчины (кг/м²)", "Критический пол индекса мышечной массы для мужчин (саркопения, по умолч. 19.0)."),
            ("ffmi_floor_f", "Минимальный FFMI, женщины (кг/м²)", "Критический пол индекса мышечной массы для женщин (саркопения, по умолч. 16.0)."),
        ],
    },
    {
        "category": "⚡ Базовый метаболизм и дефицит (BMR / Питание)",
        "desc": "Физиологический пол калоража по модели Alpert, защита от срывов и баланс белка.",
        "fields": [
            ("fat_supply_kcal_per_kg", "Отдача энергии жиром (ккал/кг/сут)", "Модель Alpert 2005 (PMID 15615615): предел отдачи энергии жировой тканью (по умолч. 69.3)."),
            ("fat_share_lean", "Доля предела у худого", "Допустимая доля предела Alpert на нижней границе шкалы процента жира (по умолч. 0.70)."),
            ("fat_share_fat", "Доля предела у полного", "Допустимая доля предела Alpert на верхней границе шкалы, пока доля мышц не доказана (по умолч. 0.30)."),
            ("fat_share_fat_proven", "Доля предела у полного (доказано)", "Доля на верхней границе шкалы, если замеры доказали малую долю мышц в потере (по умолч. 0.40)."),
            ("fat_share_bounds_m", "Шкала % жира, мужчины", "Нижняя/верхняя граница процента жира для интерполяции доли, мужская шкала (по умолч. [12, 30]); используется и при неизвестном поле."),
            ("fat_share_bounds_f", "Шкала % жира, женщины", "Нижняя/верхняя граница процента жира для интерполяции доли, женская шкала (по умолч. [20, 38])."),
            ("fat_mass_window_days", "Окно медианы жировой массы (дней)", "Сглаживание жировой массы для пола калоража, чтобы шум биоимпеданса не двигал цель день ко дню (по умолч. 7)."),
            ("lean_share_window_days", "Окно доказательства доли мышц (дней)", "Период, за который проверяется доля тощей массы в потере веса (по умолч. 28)."),
            ("lean_share_min_points", "Мин. замеров для доказательства", "Мин. число замеров состава тела с FFM в окне, иначе доля мышц не считается доказанной (по умолч. 8)."),
            ("lean_share_min_loss_kg", "Мин. потеря веса для доказательства (кг)", "Ниже этой потери за окно доля мышц не считается доказанной (по умолч. 1.0 кг)."),
            ("undereating_ratio", "Порог недоедания от цели", "Доля от нормы калоража (по умолч. 0.80 = 80%). Ниже — деструктивное недоедание."),
            ("undereating_days", "Дней недоедания подряд", "Сколько дней подряд калораж ниже нормы до срабатывания предупреждения (по умолч. 3)."),
            ("protein_skew_threshold", "Макс. доля белка в 1 приёме", "Доля суточного плана белка в одном приёме (по умолч. 0.50 = 50%). Выше — дисбаланс MPS."),
            ("protein_skew_min_meals", "Мин. приёмов для проверки белка", "Мин. число залогированных приёмов пищи за день (по умолч. 3)."),
            ("protein_skew_min_total_g", "Мин. суточный белок (г)", "Мин. общий белок за день для проверки распределения (по умолч. 60 г)."),
        ],
    },
    {
        "category": "📉 Скорость снижения веса и плато",
        "desc": "Соблюдение клинических темпов ВОЗ для профилактики желчнокаменной болезни и застоя веса.",
        "fields": [
            ("weight_rate_pct_week_max", "Макс. темп сброса (%/нед)", "Порог по доле веса; действует min с weight_rate_kg_week_max (по умолч. 1.5)."),
            ("weight_rate_kg_week_max", "Макс. темп сброса (кг/нед)", "Порог в килограммах; действует min с weight_rate_pct_week_max (по умолч. 1.5)."),
            ("weight_rate_window_days", "Окно расчёта темпа (дней)", "Длина окна для вычисления недельного темпа снижения веса (по умолч. 14)."),
            ("weight_rate_min_points", "Мин. точек для темпа", "Мин. замеров веса в окне для надёжного расчёта (по умолч. 4)."),
            ("plateau_range_kg", "Размах веса для плато (кг)", "Если разброс веса за окно укладывается в этот диапазон — плато (по умолч. 0.5 кг)."),
            ("plateau_window_days", "Окно фиксации плато (дней)", "Период застоя веса для фиксации плато (по умолч. 10 дней)."),
            ("plateau_min_points", "Мин. замеров для плато", "Необходимое число замеров веса в окне (по умолч. 5)."),
            ("regain_window_days", "Окно возврата веса (дней)", "Длина окна для проверки роста веса после снижения (по умолч. 28 дней)."),
            ("regain_min_points", "Мин. замеров для возврата веса", "Мин. число взвешиваний в окне (после исключения дней рефида/болезни) для срабатывания (по умолч. 6)."),
            ("regain_pct", "Порог возврата веса (%)", "Рост медианы веса от начала окна к концу, выше которого гард сообщает факт возврата (по умолч. 3.0%)."),
        ],
    },
    {
        "category": "💊 Медикаменты и метаболизм",
        "desc": "Липиды для абсорбции жирорастворимых препаратов, висцеральное ожирение и вариативность гликемии.",
        "fields": [
            ("lipid_guard_fat_g", "Мин. жиров для препарата (г)", "Минимум липидов в окне ±3ч от орального препарата для желчеотделения и абсорбции (по умолч. 10 г)."),
            ("whr_male_max", "Макс. талия/бёдра (мужчины)", "Верхняя граница абдоминального ожирения по ВОЗ для мужчин (по умолч. 0.90)."),
            ("whr_female_max", "Макс. талия/бёдра (женщины)", "Верхняя граница абдоминального ожирения по ВОЗ для женщин (по умолч. 0.85)."),
            ("glucose_sd_threshold", "Порог SD глюкозы (ммоль/л)", "Стандартное отклонение сахара за 14 дней. Выше — опасные гликемические качели (по умолч. 0.8)."),
            ("glucose_trend_threshold", "Порог тренда глюкозы (ммоль/л/нед)", "Скорость роста среднего уровня глюкозы в неделю (по умолч. 0.1)."),
            ("glucose_window_days", "Окно анализа глюкозы (дней)", "Период оценки вариативности и тренда сахара (по умолч. 14)."),
            ("glucose_min_points", "Мин. замеров глюкозы", "Мин. измерений глюкометра за окно (по умолч. 5)."),
        ],
    },
    {
        "category": "📋 Дисциплина данных и замеры",
        "desc": "Регулярность пищевого дневника для сходимости TDEE и превентивные напоминания о взвешивании.",
        "fields": [
            ("stale_calib_min_days", "Мин. серия дней дневника питания", "Необходимо непрерывных дней подряд для адаптивной модели TDEE (по умолч. 7)."),
            ("stale_calib_window_days", "Окно проверки дневника (дней)", "Длина окна проверки непрерывности пищевого дневника (по умолч. 14)."),
            ("no_measure_days", "Дней без замеров (строго)", "Порог длительного отсутствия антропометрии и веса (по умолч. 7 дней)."),
            ("measure_soon_after_days", "Дней до напоминания о замере", "Мягкое превентивное напоминание для ритма мониторинга (по умолч. 5 дней)."),
        ],
    },
    {
        "category": "🍽️ Риск пищевого срыва (триада)",
        "desc": "Совпадение накопленного дефицита калорий, короткого сна и нехватки белка на завтрак — предиктор компульсивного переедания.",
        "fields": [
            ("binge_window_days", "Окно накопленного дефицита (дней)", "Длина окна для суммарного дефицита калорий, заканчивающегося вчера (по умолч. 5)."),
            ("binge_deficit_kcal", "Порог накопленного дефицита (ккал)", "Суммарный дефицит за окно, выше которого фактор срыва считается сработавшим (по умолч. 3500 ккал)."),
            ("binge_sleep_min", "Мин. сон прошлой ночью (мин)", "Порог продолжительности сна; короче — фактор недосыпа сработал (по умолч. 390 мин = 6ч30м)."),
            ("binge_breakfast_protein_g", "Мин. белок на завтрак (г)", "Порог белка в завтраке; меньше (или завтрак пропущен после полудня) — фактор сработал (по умолч. 20 г)."),
            ("binge_min_factors", "Мин. факторов для триады", "Сколько из трёх факторов должны совпасть, чтобы сработал гардрейл (по умолч. 2 из 3)."),
        ],
    },
    {
        "category": "⌚ Часы: восстановление и шаги",
        "desc": "Сигнал недовосстановления по HRV/минимальному пульсу и падение активности по шагам (CONTEXT.md «Сигнал восстановления», «Цель шагов»).",
        "fields": [
            ("recovery_hrv_drop_pct", "Падение HRV от медианы (%)", "На сколько HRV должна быть ниже своей медианы за 28 дней, чтобы день засчитался (по умолч. 15%)."),
            ("recovery_hr_min_rise_bpm", "Рост мин. пульса от медианы (уд/мин)", "На сколько минимальный пульс должен быть выше своей медианы за 28 дней (по умолч. 5)."),
            ("steps_drop_pct", "Падение медианы шагов (%)", "Порог падения медианы шагов за 28 дней против предыдущих 28 (по умолч. 15%)."),
        ],
    },
]


def guards_page(guards_status: list[dict], guards_cfg: dict, comments: dict[str, str],
                csrf_token: str, message: str | None = None, error: str | None = None) -> str:
    msg_html = ""
    if error:
        msg_html = f'<div class="card" style="border-left:4px solid var(--danger);"><p class="error">{_e(error)}</p></div>'
    elif message:
        msg_html = f'<div class="card" style="border-left:4px solid var(--ok);"><p class="msg">{_e(message)}</p></div>'

    total = len(guards_status)
    n_ok = sum(1 for g in guards_status if g.get("status") == "ok")
    n_warn = sum(1 for g in guards_status if g.get("status") == "warning")
    n_crit = sum(1 for g in guards_status if g.get("status") == "critical")
    n_nodata = sum(1 for g in guards_status if g.get("status") == "nodata")

    stat_tiles = (
        f'<div class="stats" style="margin-bottom:18px;">'
        + _stat("🛡️ Всего правил", str(total))
        + _stat("🟢 В норме", str(n_ok))
        + _stat("🟡 Предупреждения", str(n_warn), sub_cls="warn" if n_warn else "")
        + _stat("🔴 Критические", str(n_crit), sub_cls="up" if n_crit else "")
        + _stat("⚪ Нет данных", str(n_nodata))
        + '</div>'
    )

    # Active alerts block
    active_guards = [g for g in guards_status if g.get("status") in ("critical", "warning")]
    active_html = ""
    if active_guards:
        alert_boxes = []
        for g in active_guards:
            st = g.get("status", "warning")
            badge_cls = "bad" if st == "critical" else "warn"
            badge_lbl = "🔴 КРИТИЧЕСКИЙ РИСК" if st == "critical" else "🟡 ВНИМАНИЕ / ПРЕДУПРЕЖДЕНИЕ"
            alert_boxes.append(
                f'<div class="guard-alert-box {st}">'
                f'<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">'
                f'<div><span class="chip {badge_cls}">{badge_lbl}</span> '
                f'<h3 style="margin:6px 0 2px;font-size:16px;color:var(--fg);display:inline-block;vertical-align:middle;">{_e(g.get("name"))}</h3> '
                f'<code style="font-size:11px;">{_e(g.get("code"))}</code><div class="guard-category" style="margin-top:2px;">{_e(g.get("category"))}</div></div>'
                f'<div style="font-size:13px;text-align:right;">'
                f'<span style="color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:600;display:block;">Зафиксировано:</span>'
                f'<b style="font-variant-numeric:tabular-nums;font-size:15px;">{_e(g.get("current_val"))}</b> <span style="color:var(--muted);">(порог: {_e(g.get("threshold_val"))})</span>'
                f'</div></div>'
                f'<p style="margin:10px 0 8px;font-size:14px;line-height:1.45;color:var(--fg);font-weight:500;">{_e(g.get("message"))}</p>'
                f'<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:13px;display:flex;flex-direction:column;gap:8px;margin-top:8px;">'
                f'<div><strong style="color:var(--fg);">🧬 Физиологическое обоснование:</strong> <span style="color:var(--muted);">{_e(g.get("rationale"))}</span></div>'
                f'<div><strong style="color:var(--fg);">🎯 Рекомендуемое действие:</strong> <span style="color:var(--muted);">{_e(g.get("action"))}</span></div>'
                f'</div></div>'
            )
        active_html = (
            f'<div class="card" style="border-left:4px solid var(--warn);">'
            f'<h2 style="margin:0 0 6px;color:var(--fg);">⚠️ Активные отклонения и риски ({len(active_guards)})</h2>'
            f'<p class="comment" style="margin-bottom:14px;">Гардрейлы обнаружили отклонения от безопасных норм. Ознакомьтесь с клиническим обоснованием и рекомендациями по корректировке.</p>'
            + "".join(alert_boxes)
            + '</div>'
        )

    # 14 Guardrails Catalog
    categories_order = [
        "Сохранение мышц",
        "Питание и калории",
        "Скорость снижения веса",
        "Питание и медикаменты",
        "Здоровье и метаболизм",
        "Дисциплина данных",
    ]
    catalog_blocks = []
    for cat in categories_order:
        items = [g for g in guards_status if g.get("category") == cat]
        if not items:
            continue
        cards = []
        for item in items:
            st = item.get("status", "ok")
            if st == "ok":
                badge = '<span class="chip ok">🟢 В норме</span>'
            elif st == "critical":
                badge = '<span class="chip bad">🔴 Критично</span>'
            elif st == "warning":
                badge = '<span class="chip warn">🟡 Внимание</span>'
            else:
                badge = '<span class="chip info" style="opacity:0.8;">⚪ Нет данных</span>'

            keys_chips = " ".join(f'<code>{_e(k)}</code>' for k in item.get("keys", []))
            cards.append(
                f'<div class="guard-card status-{st}">'
                f'<div class="guard-header">'
                f'<div><h4 class="guard-title">{_e(item.get("name"))}</h4>'
                f'<div style="display:flex;gap:6px;align-items:center;margin-top:3px;"><code style="font-size:11px;">{_e(item.get("code"))}</code><span class="guard-category">{_e(item.get("category"))}</span></div></div>'
                f'<div>{badge}</div>'
                f'</div>'
                f'<p style="margin:0;font-size:13px;color:var(--muted);line-height:1.4;">{_e(item.get("description"))}</p>'
                f'<div class="guard-metric-box">'
                f'<div><span class="guard-val-label">Текущее</span><div class="guard-val-text">{_e(item.get("current_val"))}</div></div>'
                f'<div><span class="guard-val-label">Порог</span><div class="guard-val-text">{_e(item.get("threshold_val"))}</div></div>'
                f'</div>'
                f'<details class="guard-details">'
                f'<summary>🧬 Клиническое обоснование и логика</summary>'
                f'<p><strong style="color:var(--fg);">Обоснование:</strong> {_e(item.get("rationale"))}</p>'
                f'<p><strong style="color:var(--fg);">Рекомендация:</strong> {_e(item.get("action"))}</p>'
                f'<p style="margin-top:8px;font-size:11px;color:var(--muted);"><strong style="color:var(--fg);">Параметры config.yaml:</strong> {keys_chips}</p>'
                f'</details>'
                f'</div>'
            )
        catalog_blocks.append(
            f'<div style="margin-bottom:20px;">'
            f'<h3 style="margin:0 0 10px;font-size:13px;letter-spacing:.06em;">{_e(cat)}</h3>'
            f'<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(330px,1fr));">'
            + "".join(cards)
            + f'</div></div>'
        )

    # Management form
    form_sections = []
    for grp in GUARD_FORM_GROUPS:
        rows = []
        for key, label, tip in grp["fields"]:
            curr_val = guards_cfg.get("guards", {}).get(key, guards_cfg.get("policy", {}).get(key, ""))
            comm = comments.get(f"guards.{key}", comments.get(f"policy.{key}", tip))
            rows.append(
                f'<div class="guard-input-row">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
                f'<label for="guard__{_e(key)}">{_e(label)}</label>'
                f'<code style="font-size:11px;">{_e(key)}</code>'
                f'</div>'
                f'<input type="text" id="guard__{_e(key)}" name="guard__{_e(key)}" value="{_e(str(curr_val))}">'
                f'<span class="comment">{_e(comm)}</span>'
                f'</div>'
            )
        form_sections.append(
            f'<div style="margin-bottom:18px;">'
            f'<h4 style="margin:0 0 4px;font-size:14px;color:var(--fg);">{_e(grp["category"])}</h4>'
            f'<p class="comment" style="margin:0 0 8px;">{_e(grp["desc"])}</p>'
            f'<div class="guard-form-grid">{"".join(rows)}</div>'
            f'</div>'
        )

    form_html = (
        f'<div class="card">'
        f'<h2 style="margin:0 0 6px;">⚙️ Управление порогами гардрейлов</h2>'
        f'<p class="comment" style="margin-bottom:16px;">'
        f'Пороги сохраняются хирургически построчно в <code>config.yaml</code> через валидацию YAML '
        f'с сохранением всех комментариев и автоматическим созданием резервной копии <code>.bak</code>.'
        f'</p>'
        f'<p class="comment" style="margin-bottom:16px;"><strong>Эти настройки глобальные — они применяются '
        f'ко всем пользователям</strong>, а не только к выбранному в шапке.</p>'
        f'<form method="post" action="/guards/save">'
        f'{_csrf_field(csrf_token)}'
        + "".join(form_sections)
        + f'<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;">'
        f'<button type="submit" style="font-size:14px;padding:10px 22px;">💾 Сохранить пороги гардрейлов</button>'
        f'</div>'
        f'</form>'
        f'</div>'
    )

    return f"""
<div class="hero">
  <div>
    <h1 style="margin:0 0 4px;font-size:28px;">🛡️ Гардрейлы безопасности</h1>
    <div class="k">14 физиологических и клинических защитных механизмов</div>
  </div>
</div>
{msg_html}
{stat_tiles}
{active_html}
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
    <h2 style="margin:0;">Мониторинг всех правил</h2>
    <span class="comment">Данные обновляются в реальном времени</span>
  </div>
  {"".join(catalog_blocks)}
</div>
{form_html}
"""


# ---------------------------------------------------------------- milestones

def milestones_page(rows, valid_metrics, csrf_token: str, error: str | None = None, user_id: int | None = None) -> str:
    err_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    options = "".join(f'<option value="{html.escape(m)}">{html.escape(m)}</option>' for m in valid_metrics)

    if not rows:
        table_html = "<p>Вех пока нет.</p>"
    else:
        trs = []
        for r in rows:
            m = dict(r)
            achieved = "достигнута" if m.get("achieved_at") else "—"
            trs.append(
                "<tr>"
                f"<td>{_e(m.get('name'))}</td><td>{_e(m.get('metric'))}</td>"
                f"<td>{_e(m.get('threshold'))}</td><td>{_e(m.get('deadline'))}</td>"
                f"<td>{achieved}</td>"
                f"<td><form method='post' action='/milestones' onsubmit='return true'>"
                f"{_csrf_field(csrf_token, user_id)}"
                f"<input type='hidden' name='action' value='delete'>"
                f"<input type='hidden' name='id' value='{m['id']}'>"
                f"<button type='submit' class='danger'>Удалить</button></form></td>"
                "</tr>"
            )
        table_html = (
            '<div class="table-responsive"><table><tr><th>Имя</th><th>Метрика</th><th>Порог</th><th>Срок</th><th>Статус</th><th></th></tr>'
            + "".join(trs) + "</table></div>"
        )

    return f"""
<div class="card"><h2>Вехи</h2>{table_html}</div>
<div class="card">
  <h3>Добавить / изменить веху</h3>
  <p class="comment">Сохранение — UPSERT по (пользователь, имя): та же логика, что использует плагин. Существующее имя обновит веху, новое — создаст.</p>
  {err_html}
  <form method="post" action="/milestones">
    {_csrf_field(csrf_token, user_id)}
    <input type="hidden" name="action" value="save">
    <div class="row">
      <div><label>Имя</label><input type="text" name="name" required></div>
      <div><label>Метрика</label><select name="metric">{options}</select></div>
      <div><label>Порог</label><input type="number" step="any" name="threshold"></div>
      <div><label>Срок (YYYY-MM-DD)</label><input type="date" name="deadline"></div>
      <div><button type="submit">Сохранить</button></div>
    </div>
  </form>
</div>
"""


# ---------------------------------------------------------------- thresholds

def parse_config_comments(raw_text: str) -> dict[str, str]:
    """Maps 'section.key' -> the Russian comment line(s) directly above it in
    config.yaml. yaml.safe_load() drops comments entirely, so we walk the raw
    text ourselves. Good enough for this file's flat two-level layout
    (section: / then indented key: value with comments right above); not a
    general-purpose YAML comment parser."""
    comments: dict[str, str] = {}
    section = None
    buf: list[str] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            buf = []
            continue
        if stripped.startswith("#"):
            buf.append(stripped.lstrip("#").strip())
            continue
        if not line[0].isspace() and stripped.endswith(":"):
            section = stripped[:-1]
            buf = []
            continue
        if ":" in stripped and section is not None:
            key = stripped.split(":", 1)[0].strip()
            comments[f"{section}.{key}"] = " ".join(buf)
        buf = []
    return comments


# Sections plugin.tools._admin_set is able to edit; keep in sync with its section loop.
ADMIN_SET_SECTIONS = ("policy", "guards", "ingest", "backup", "admin")


def flatten_config(parsed: dict) -> list[tuple[str, str, object]]:
    out = []
    for section, values in parsed.items():
        if section not in ADMIN_SET_SECTIONS or not isinstance(values, dict):
            continue
        for key, value in values.items():
            out.append((section, key, value))
    return out


def thresholds_page(parsed: dict, comments: dict[str, str], csrf_token: str,
                     message: str | None = None) -> str:
    """Per-key inputs, saved via plugin.tools._admin_set — surgical single-line
    regex replacement in config.yaml, never yaml.dump. A whole-file rewrite
    (dump-and-rewrite, or a textarea holding the full raw text) has already
    destroyed all 39 Russian comments in this file twice; this page must not
    reintroduce that path. See plugin/tools.py::_admin_set for the mechanism."""
    msg_html = ""
    if message:
        cls = "error" if message.startswith("Ошибка") else "msg"
        msg_html = f'<p class="{cls}">{html.escape(message)}</p>'

    trs = []
    for section, key, value in flatten_config(parsed):
        comment = comments.get(f"{section}.{key}", "")
        trs.append(
            f"<tr><td>{html.escape(section)}</td><td>{html.escape(key)}</td>"
            f"<td><input type='text' name='kv__{html.escape(key)}' value='{html.escape(str(value))}'></td>"
            f"<td class='comment'>{html.escape(comment)}</td></tr>"
        )
    table_html = (
        '<div class="table-responsive"><table><tr><th>Секция</th><th>Ключ</th><th>Значение</th><th>Комментарий</th></tr>'
        + "".join(trs) + "</table></div>"
    )

    return f"""
<div class="card">
  <h2>config.yaml</h2>
  {msg_html}
  <p class="comment"><strong>Эти настройки глобальные — они применяются ко всем пользователям</strong>,
  а не только к выбранному в шапке.</p>
  <p class="comment">Каждое сохранённое поле правится построчно, регулярным выражением по имени ключа
  (та же функция, что использует Telegram-команда admin_cmd set) — файл никогда не пересобирается через
  yaml.dump, поэтому русские комментарии не теряются. Проверка, что результат — валидный YAML, идёт до
  подмены файла; предыдущая версия остаётся в config.bak.</p>
  <form method="post" action="/thresholds">
    {_csrf_field(csrf_token)}
    {table_html}
    <p><button type="submit">Сохранить изменения</button></p>
  </form>
</div>
"""


# ---------------------------------------------------------------- alerts

def alerts_page(rows, page: int, total: int, csrf_token: str, tz_name: str | None = None) -> str:
    if not rows:
        table_html = "<p>Алертов нет.</p>"
    else:
        trs = "".join(
            f"<tr><td>{_fmt_dt(r['created_at'], tz_name)}</td><td>{_e(r['rule'])}</td><td>{_e(r['message'])}</td></tr>"
            for r in rows
        )
        table_html = f'<div class="table-responsive"><table><tr><th>Когда</th><th>Правило</th><th>Сообщение</th></tr>{trs}</table></div>'

    last_page = max(1, -(-total // 50))
    nav = []
    if page > 1:
        nav.append(f'<a href="/alerts?page={page - 1}">&larr; назад</a>')
    nav.append(f'<span>Стр. {page} из {last_page} ({total} всего)</span>')
    if page < last_page:
        nav.append(f'<a href="/alerts?page={page + 1}">вперёд &rarr;</a>')
    pager = " &nbsp; ".join(nav)

    return f'<div class="card"><h2>Алерты</h2>{table_html}<p>{pager}</p></div>'


# ---------------------------------------------------------------- personas

def personas_page(personas: list[dict], csrf_token: str, error: str | None = None,
                   summary: list[tuple[str, int]] | None = None,
                   tz_name: str | None = None) -> str:
    """Одна карточка на персону (= строка users): профиль, счётчики записей,
    форма удаления с подтверждением через ввод telegram_user_id. summary — итог
    последнего удаления (список (таблица, число_удалённых_строк))."""
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    if summary:
        rows_html = "".join(f"<tr><td>{html.escape(t)}</td><td>{c}</td></tr>" for t, c in summary)
        summary_html = (
            '<div class="card"><h3>Персона удалена</h3>'
            f'<div class="table-responsive"><table><tr><th>Таблица</th><th>Удалено строк</th></tr>{rows_html}</table></div></div>'
        )
    else:
        summary_html = ""

    if not personas:
        cards = '<div class="card"><p>Персон пока нет.</p></div>'
    else:
        blocks = []
        for p in personas:
            u = p["user"]
            food_line = (f"{p['food_count']} записей (последняя: {_e(p['food_last'])})"
                         if p["food_count"] else "нет записей")
            bm_line = (f"{p['bm_count']} записей (последняя: {_e(p['bm_last'])})"
                       if p["bm_count"] else "нет записей")
            blocks.append(f"""
<div class="card">
  <h3>Персона #{_e(u['id'])} · telegram_user_id {_e(u['telegram_user_id'])}</h3>
  <div class="table-responsive">
  <table>
    <tr><td>Рост</td><td>{_e(u.get('height_cm'))} см</td></tr>
    <tr><td>Пол</td><td>{_e(u.get('sex'))}</td></tr>
    <tr><td>Дата рождения</td><td>{_e(u.get('birth_date'))}</td></tr>
    <tr><td>Стартовый вес</td><td>{_e(u.get('base_weight_kg'))} кг</td></tr>
    <tr><td>Дата регистрации</td><td>{_fmt_dt(u.get('created_at'), tz_name)}</td></tr>
    <tr><td>Записи еды (food_log)</td><td>{food_line}</td></tr>
    <tr><td>Замеры тела (body_metrics)</td><td>{bm_line}</td></tr>
  </table>
  </div>
  <p class="comment error">
    Удаление стирает ВСЕ данные этой персоны безвозвратно и не подлежит отмене:
    {p['food_count']} записей еды, {p['bm_count']} замеров тела и все связанные
    записи во всех остальных таблицах (вехи, пороги дня, алерты, лог воды,
    глюкозы, активности, лекарств, план питания/тренировок и т.д.).
  </p>
  <form method="post" action="/personas">
    {_csrf_field(csrf_token)}
    <input type="hidden" name="id" value="{u['id']}">
    <div class="row">
      <div><label>Введите telegram_user_id для подтверждения</label>
        <input type="text" name="confirm_telegram_id" required></div>
      <div><button type="submit" class="danger">Удалить персону</button></div>
    </div>
  </form>
</div>""")
        cards = "".join(blocks)

    return f"<h2>Персоны</h2>{error_html}{summary_html}{cards}"


# ---------------------------------------------------------------- drug card drafts

def drafts_page(drafts: list[dict], csrf_token: str, error: str | None = None,
                tz_name: str | None = None) -> str:
    """Pending-черновики карт (health_core/card_drafts.py, CONTEXT.md «Черновик
    карты», docs/adr/0002): одна карточка на черновик, поля редактируемые —
    цифры составила модель по официальным источникам, но подтверждает их
    администратор, а не код. Одобрение (approve) дописывает карту в
    Knowledge/drug_cards.md именно с этими, возможно отредактированными,
    значениями.

    drafts — [{"id", "substance", "fields": {...}, "sources": [...],
    "requested_by", "created_at"}, ...], как собирает admin/server.py::_draft_rows.
    """
    error_html = f'<p class="error">{_e(error)}</p>' if error else ""
    if not drafts:
        return f'<h2>Черновики карт</h2>{error_html}<div class="card"><p>Черновиков, ждущих решения, нет.</p></div>'

    def field(label: str, name: str, value) -> str:
        return (f'<div><label>{_e(label)}</label>'
                f'<input type="text" name="{name}" value="{_e(value)}"></div>')

    blocks = []
    for d in drafts:
        f = d["fields"] if isinstance(d.get("fields"), dict) else {}
        sources = d.get("sources") or []
        sources_html = "".join(
            f'<li><a href="{_e(u)}" target="_blank" rel="noopener">{_e(u)}</a></li>' for u in sources
        ) or "<li>—</li>"
        source_default = f.get("source") or "; ".join(sources)
        blocks.append(f"""
<div class="card">
  <h3>#{_e(d['id'])} · {_e(d['substance'])}</h3>
  <p class="comment">Запросил пользователь {_e(d['requested_by'])} · {_fmt_dt(d['created_at'], tz_name)}</p>
  <p class="comment error">Составлено моделью по официальным источникам — сверь цифры перед одобрением.</p>
  <ul>{sources_html}</ul>
  <form method="post" action="/drafts">
    {_csrf_field(csrf_token)}
    <input type="hidden" name="id" value="{_e(d['id'])}">
    <div class="row">
      {field("Статус", "status", f.get("status", ""))}
      {field("Лестница", "ladder", f.get("ladder", ""))}
    </div>
    <div class="row">
      {field("Минимум недель на ступени", "min_weeks", f.get("min_weeks", ""))}
      {field("Интервал приёма", "interval_days", f.get("interval_days", ""))}
    </div>
    <div class="row">
      {field("Период полувыведения", "half_life_days", f.get("half_life_days", ""))}
      {field("Пик концентрации", "tmax_h", f.get("tmax_h", ""))}
    </div>
    <div class="row">
      {field("Синонимы (через /)", "synonyms", f.get("synonyms", ""))}
      {field("Источник", "source", source_default)}
    </div>
    <div class="row">
      <div><button type="submit" name="action" value="approve">Одобрить</button></div>
      <div><button type="submit" name="action" value="reject" class="danger">Отклонить</button></div>
    </div>
  </form>
</div>""")
    return f"<h2>Черновики карт</h2>{error_html}{''.join(blocks)}"


# ---------------------------------------------------------------- actions

def actions_page(csrf_token: str, result: str | None = None, user_id: int | None = None) -> str:
    result_html = f'<div class="card"><h3>Результат</h3><pre>{html.escape(result)}</pre></div>' if result else ""

    def form(action: str, label: str, hint: str) -> str:
        return f"""
<div class="card">
  <h3>{html.escape(label)}</h3>
  <p class="comment">{html.escape(hint)}</p>
  <form method="post" action="/actions">
    {_csrf_field(csrf_token, user_id)}
    <input type="hidden" name="action" value="{action}">
    <button type="submit">Запустить</button>
  </form>
</div>"""


    scale_upload_html = f"""
<div class="card">
  <h3>Загрузить файлы (весы / тренировки / ZIP-архив)</h3>
  <p class="comment">Принимает выгрузку весов Feelfit (.xlsx/.xls/.csv), тренировки (.tcx) или ZIP-архив (выгрузка Mi Fitness / папка с .tcx/.csv). Повторная загрузка тех же файлов безопасна — действует дедупликация по sha256.</p>
  <form method="post" action="/actions/import-scale" enctype="multipart/form-data">
    {_csrf_field(csrf_token, user_id)}
    <div class="row">
      <div><label>Файл</label><input type="file" name="file" accept=".xlsx,.xls,.csv,.tcx,.zip" required></div>
      <div><button type="submit">Загрузить и импортировать</button></div>
    </div>
  </form>
</div>"""

    return "".join([
        form("export", "Экспорт", "Выгружает витрину (Metrics/CSV, Nutrition/*.md) в HEALTH_EXPORT_DIR."),
        form("backup", "Бэкап", "Онлайн-копия health.db через sqlite3 .backup() в HEALTH_BACKUP_DIR."),
        form("recalc", "Пересчитать цель на сегодня", "Пересчитывает daily_target() на сегодняшнюю дату."),
        form("import", "Исторический импорт",
             "Импортирует весы/TCX/антропометрию из HEALTH_SCALE_IMPORT_PATH / "
             "HEALTH_TCX_IMPORT_DIR / HEALTH_ANTHRO_IMPORT_PATH (если заданы)."),
        scale_upload_html,
        result_html,
    ])


# ---------------------------------------------------------------- plans

def plans_page(recent_plans: list[dict], all_dates: list[dict], selected_date: str | None,
               selected_plan: dict | None, weekly_template: dict, csrf_token: str,
               user_id: int | None = None) -> str:
    """Dated plans (plan_log): recent plans at top, history list, full plan detail on selection,
    weekly templates at bottom.

    recent_plans: list of {"date", "kind", "body"} sorted desc by date (up to ~3 recent).
    all_dates: list of {"date", "kind", "body_preview"} for history list, sorted desc.
    selected_date: YYYY-MM-DD if ?date= param set.
    selected_plan: {"date", "kind", "body", "rationale"} for the selected date, or None.
    weekly_template: {"meals": [...], "workouts": [...]} from get_plan().
    """

    # ── Recent plans ───────────────────────────────────────────────────────
    if recent_plans:
        # Group by date, collecting meal and workout for each date
        plans_by_date = {}
        for p in recent_plans:
            d = p["date"]
            if d not in plans_by_date:
                plans_by_date[d] = {}
            plans_by_date[d][p["kind"]] = p

        cards = []
        for date, kinds in sorted(plans_by_date.items(), reverse=True):
            # Show workout and meal side by side
            cells = []
            for kind in ["workout", "meal"]:
                if kind in kinds:
                    p = kinds[kind]
                    preview = (p["body"][:100] + "…") if len(p["body"]) > 100 else p["body"]
                    kind_label = "Тренировка" if kind == "workout" else "Питание"
                    link = f'<a href="/plans?date={_e(date)}">{kind_label}</a>'
                    del_form = f"""<form method="post" action="/plans/delete" style="display:inline;margin-left:8px;" data-confirm="Удалить план ({kind_label}) на {_e(date)}?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="date" value="{_e(date)}">
<input type="hidden" name="kind" value="{_e(kind)}">
<button type="submit" class="btn-del" style="padding:2px 8px;font-size:12px;color:var(--danger);border:1px solid var(--border);border-radius:6px;background:transparent;cursor:pointer;">Удалить</button>
</form>"""
                    cells.append(
                        f'<div class="card"><h3>{_e(date)}</h3>'
                        f'<div class="comment" style="display:flex;align-items:center;">{link}{del_form}</div>'
                        f'<pre>{_e(preview)}</pre></div>'
                    )
            cards.append(f'<div class="grid">{"".join(cells)}</div>')
        recent_html = "".join(cards)
    else:
        recent_html = '<div class="card"><p>На сегодня планов нет.</p></div>'

    # ── History list ───────────────────────────────────────────────────────
    if all_dates:
        trs = []
        for row in all_dates:
            d = row["date"]
            k = row["kind"]
            preview = (row["body_preview"][:80] + "…") if len(row["body_preview"]) > 80 else row["body_preview"]
            kind_label = "Тренировка" if k == "workout" else "Питание"
            del_btn = f"""<form method="post" action="/plans/delete" style="display:inline;" data-confirm="Удалить план ({kind_label}) на {_e(d)}?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="date" value="{_e(d)}">
<input type="hidden" name="kind" value="{_e(k)}">
<button type="submit" class="btn-del">✕</button>
</form>"""
            trs.append(
                f"<tr><td>{_e(d)}</td><td>{_e(kind_label)}</td>"
                f"<td><a href='/plans?date={_e(d)}'>{_e(preview)}</a></td>"
                f"<td style='text-align:right;'>{del_btn}</td></tr>"
            )
        list_html = f"<div class=\"table-responsive\"><table><tr><th>Дата</th><th>Вид</th><th>Предпросмотр</th><th style='width:50px;'></th></tr>{''.join(trs)}</table></div>"
    else:
        list_html = "<p>История планов пуста.</p>"

    # ── Selected date detail / Edit ─────────────────────────────────────────
    detail_html = ""
    if selected_date and selected_plan:
        kind_label = "Тренировка" if selected_plan["kind"] == "workout" else "Питание"
        back_link = '<a href="/plans">← Вернуться к списку</a>'
        del_detail = f"""<form method="post" action="/plans/delete" style="display:inline;margin-left:12px;" data-confirm="Удалить план ({kind_label}) на {_e(selected_date)}?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="date" value="{_e(selected_date)}">
<input type="hidden" name="kind" value="{_e(selected_plan['kind'])}">
<button type="submit" style="padding:4px 12px;font-size:13px;color:var(--danger);border:1px solid var(--danger);border-radius:6px;background:transparent;cursor:pointer;">Удалить этот план</button>
</form>"""
        body_val = html.escape(selected_plan.get("body") or "")
        rat_val = html.escape(selected_plan.get("rationale") or "")

        detail_html = f"""
<div class="card">
  <h2>{_e(kind_label)} на {_e(selected_date)}</h2>
  <p>{back_link} {del_detail}</p>
  <form method="post" action="/plans/save" style="margin-top:16px;">
    <input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
    <input type="hidden" name="date" value="{_e(selected_date)}">
    <input type="hidden" name="kind" value="{_e(selected_plan['kind'])}">
    
    <label style="display:block;margin-bottom:6px;font-weight:600;">Текст плана:</label>
    <textarea name="body" rows="12" style="width:100%;font-family:inherit;font-size:14px;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--fg);resize:vertical;" required>{body_val}</textarea>
    
    <label style="display:block;margin-top:12px;margin-bottom:6px;font-weight:600;">Обоснование / заметка (опционально):</label>
    <textarea name="rationale" rows="4" style="width:100%;font-family:inherit;font-size:14px;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--fg);resize:vertical;">{rat_val}</textarea>
    
    <div style="margin-top:14px;">
      <button type="submit" style="padding:8px 18px;font-size:14px;background:var(--accent);color:var(--on-accent);border:none;border-radius:8px;cursor:pointer;font-weight:500;">💾 Сохранить изменения</button>
    </div>
  </form>
</div>
"""
    elif selected_date:
        # Requested date has no plan
        back_link = '<a href="/plans">← Вернуться к списку</a>'
        detail_html = f"""<div class="card"><p>{back_link}</p><p>Плана на {_e(selected_date)} нет.</p>
  <form method="post" action="/plans/save" style="margin-top:14px;">
    <input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
    <input type="hidden" name="date" value="{_e(selected_date)}">
    <label style="display:block;margin-bottom:6px;font-weight:600;">Создать план:</label>
    <select name="kind" style="padding:6px 10px;margin-bottom:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--fg);">
      <option value="workout">Тренировка</option>
      <option value="meal">Питание</option>
    </select>
    <textarea name="body" rows="8" placeholder="Текст плана..." style="width:100%;font-family:inherit;font-size:14px;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--fg);resize:vertical;" required></textarea>
    <div style="margin-top:10px;">
      <button type="submit" style="padding:8px 18px;font-size:14px;background:var(--accent);color:var(--on-accent);border:none;border-radius:8px;cursor:pointer;">Создать план</button>
    </div>
  </form>
</div>"""

    # ── Weekly template ────────────────────────────────────────────────────
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    meals_by_day = {}
    for m in weekly_template.get("meals", []):
        dow = m.get("day_of_week")
        if dow not in meals_by_day:
            meals_by_day[dow] = []
        meals_by_day[dow].append(m)

    workouts_by_day = {}
    for w in weekly_template.get("workouts", []):
        dow = w.get("day_of_week")
        if dow not in workouts_by_day:
            workouts_by_day[dow] = []
        workouts_by_day[dow].append(w)

    weekly_rows = []
    for dow in range(7):
        day_label = weekdays[dow]
        meals = meals_by_day.get(dow, [])
        workouts = workouts_by_day.get(dow, [])

        meal_str = ""
        if meals:
            meal_lines = []
            for m in meals:
                slot = _e(m.get("meal_slot", "—"))
                name = _e(m.get("name", "—"))
                kcal = _n(m.get("kcal"), "{:.0f}")
                raw_slot = m.get("meal_slot", "")
                del_m = f"""<form method="post" action="/plans/template/delete" style="display:inline;margin-left:4px;" data-confirm="Удалить {slot}?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="type" value="meal">
<input type="hidden" name="day_of_week" value="{dow}">
<input type="hidden" name="meal_slot" value="{html.escape(raw_slot)}">
<button type="submit" class="btn-del">✕</button>
</form>"""
                meal_lines.append(f"{slot}: {name} ({kcal} ккал){del_m}")
            meal_str = "; ".join(meal_lines)

        workout_str = ""
        if workouts:
            wo_lines = []
            for w in workouts:
                name = _e(w.get("name", "—"))
                kind = _e(w.get("kind", "—"))
                dur = _n(w.get("duration_min"), "{:.0f}")
                raw_name = w.get("name", "")
                del_w = f"""<form method="post" action="/plans/template/delete" style="display:inline;margin-left:4px;" data-confirm="Удалить тренировку {name}?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="type" value="workout">
<input type="hidden" name="day_of_week" value="{dow}">
<input type="hidden" name="name" value="{html.escape(raw_name)}">
<button type="submit" class="btn-del">✕</button>
</form>"""
                wo_lines.append(f"{name} ({kind}, {dur} мин){del_w}")
            workout_str = "; ".join(wo_lines)

        weekly_rows.append(
            f"<tr><td><strong>{day_label}</strong></td><td>{meal_str or '—'}</td><td>{workout_str or '—'}</td></tr>"
        )

    clear_template_btn = ""
    if any(meals_by_day.values()) or any(workouts_by_day.values()):
        clear_template_btn = f"""<form method="post" action="/plans/template/delete" style="display:inline;margin-left:12px;" data-confirm="Очистить весь недельный шаблон?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="type" value="all">
<button type="submit" style="padding:4px 10px;font-size:12px;color:var(--danger);border:1px solid var(--border);border-radius:6px;background:transparent;cursor:pointer;">Очистить шаблон</button>
</form>"""
        weekly_html = (
            f"<div class=\"table-responsive\"><table><tr><th>День</th><th>Питание</th><th>Тренировка</th></tr>"
            f"{''.join(weekly_rows)}</table></div>"
        )
    else:
        weekly_html = "<p>Недельный шаблон пуст.</p>"

    add_template_html = f"""
<details style="margin-top:16px;background:var(--soft);border:1px solid var(--border);border-radius:10px;padding:14px 16px;">
  <summary style="font-weight:600;cursor:pointer;color:var(--accent);font-size:14px;">➕ Добавить элемент в недельный шаблон</summary>
  <div style="margin-top:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;">
    <form method="post" action="/plans/template/save" style="background:var(--card);padding:14px;border:1px solid var(--border);border-radius:10px;">
      {_csrf_field(csrf_token, user_id)}
      <input type="hidden" name="type" value="meal">
      <h4 style="margin:0 0 10px;font-size:13px;text-transform:uppercase;color:var(--muted)">🥗 Добавить приём пищи</h4>
      <div style="display:flex;flex-direction:column;gap:8px">
        <div class="row">
          <div><label>День недели</label>
            <select name="day_of_week" required>
              <option value="0">Понедельник</option>
              <option value="1">Вторник</option>
              <option value="2">Среда</option>
              <option value="3">Четверг</option>
              <option value="4">Пятница</option>
              <option value="5">Суббота</option>
              <option value="6">Воскресенье</option>
            </select>
          </div>
          <div><label>Приём пищи</label>
            <select name="meal_slot">
              <option value="breakfast">Завтрак</option>
              <option value="lunch">Обед</option>
              <option value="dinner">Ужин</option>
              <option value="snack">Перекус</option>
            </select>
          </div>
        </div>
        <div><label>Название / Меню</label>
          <input type="text" name="name" placeholder="Овсянка с ягодами, кофе" required style="width:100%">
        </div>
        <div class="row">
          <div><label>Ккал</label><input type="number" step="any" name="kcal" placeholder="0" style="width:80px"></div>
          <div><label>Белки (г)</label><input type="number" step="any" name="protein_g" placeholder="0" style="width:80px"></div>
          <div><label>Жиры (г)</label><input type="number" step="any" name="fat_g" placeholder="0" style="width:80px"></div>
          <div><label>Углеводы (г)</label><input type="number" step="any" name="carbs_g" placeholder="0" style="width:80px"></div>
        </div>
        <div style="margin-top:6px">
          <button type="submit" style="padding:6px 14px;font-size:13px">➕ Сохранить блюдо</button>
        </div>
      </div>
    </form>

    <form method="post" action="/plans/template/save" style="background:var(--card);padding:14px;border:1px solid var(--border);border-radius:10px;">
      {_csrf_field(csrf_token, user_id)}
      <input type="hidden" name="type" value="workout">
      <h4 style="margin:0 0 10px;font-size:13px;text-transform:uppercase;color:var(--muted)">🏋️ Добавить тренировку</h4>
      <div style="display:flex;flex-direction:column;gap:8px">
        <div class="row">
          <div><label>День недели</label>
            <select name="day_of_week" required>
              <option value="0">Понедельник</option>
              <option value="1">Вторник</option>
              <option value="2">Среда</option>
              <option value="3">Четверг</option>
              <option value="4">Пятница</option>
              <option value="5">Суббота</option>
              <option value="6">Воскресенье</option>
            </select>
          </div>
          <div><label>Тип нагрузки</label>
            <select name="kind">
              <option value="strength">Силовая (strength)</option>
              <option value="cardio">Кардио (cardio)</option>
              <option value="hiit">HIIT</option>
              <option value="yoga">Йога / Растяжка</option>
              <option value="other">Другое</option>
            </select>
          </div>
          <div><label>Длительность (мин)</label>
            <input type="number" step="any" name="duration_min" placeholder="45" style="width:90px">
          </div>
        </div>
        <div><label>Описание тренировки</label>
          <input type="text" name="name" placeholder="Фулбоди А: присед, жим, тяга" required style="width:100%">
        </div>
        <div style="margin-top:6px">
          <button type="submit" style="padding:6px 14px;font-size:13px">➕ Сохранить тренировку</button>
        </div>
      </div>
    </form>
  </div>
</details>"""

    return f"""
<div class="card"><h2>Сегодняшние планы</h2>{recent_html}</div>
<div class="card"><h2>История планов</h2>{list_html}</div>
{detail_html}
<div class="card"><div style="display:flex;align-items:center;gap:12px;margin-bottom:12px"><h2>Недельный шаблон</h2>{clear_template_btn}</div>{weekly_html}{add_template_html}</div>
"""

# ---------------------------------------------------------------- workouts

def workouts_page(rows: list[dict], stats: dict, sports_efficiency: dict,
                  csrf_token: str, error: str | None = None, message: str | None = None,
                  user_id: int | None = None, now_local: str = "") -> str:
    """Страница тренировок: дашборд статистики/эффективности, форма быстрой записи и история."""
    err_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    msg_html = f'<p class="msg">{html.escape(message)}</p>' if message else ""

    # ── Карточки статистики ──
    total_count = stats.get("total_count", 0)
    total_min = stats.get("total_duration_min", 0.0)
    total_hours = total_min / 60.0
    total_kcal = stats.get("total_kcal", 0.0)
    avg_hr_all = stats.get("avg_hr", None)

    stat_cards = (
        _stat("Всего тренировок", f"{total_count}") +
        _stat("Общее время", f"{total_hours:.1f}<small>ч</small>") +
        _stat("Сожжено", f"{total_kcal:.0f}<small>ккал</small>") +
        _stat("Средний пульс", f"{avg_hr_all:.0f}<small>уд/мин</small>" if avg_hr_all else "—")
    )
    stat_block = f'<div class="stats">{stat_cards}</div>'

    # ── Эффективность по видам спорта ──
    eff_cards = []
    for sport_name, sp in sports_efficiency.items():
        sess_n = sp.get("sessions", 0)
        k_min = sp.get("kcal_per_min", 0.0)
        eff_cards.append(
            f'<div class="card" style="margin-bottom:10px;">'
            f'<h3>{_e(sport_name)}</h3>'
            f'<div class="stats">'
            f'{_stat("Сессий", f"{sess_n}")}'
            f'{_stat("ккал/мин", f"{k_min:.1f}")}'
            f'</div></div>'
        )
    eff_html = f'<div class="grid">{"".join(eff_cards)}</div>' if eff_cards else '<p>Нет данных по эффективности.</p>'

    # ── Форма добавления ──
    add_form = f"""
<form method="post" action="/workouts/save" class="grid" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; align-items: end;">
  <input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
  <div>
    <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Вид спорта / активность:</label>
    <input type="text" name="sport" placeholder="Силовая, Гантели, Ходьба..." required style="width:100%">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Дата и время:</label>
    <input type="text" name="started_at" value="{html.escape(now_local)}" style="width:100%">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Длительность (мин):</label>
    <input type="number" step="0.1" name="duration_min" placeholder="45" required style="width:100%">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Калории (ккал):</label>
    <input type="number" step="1" name="kcal" placeholder="250" style="width:100%">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Средний пульс (bpm):</label>
    <input type="number" name="avg_hr" placeholder="125" style="width:100%">
  </div>
  <div>
    <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">Заметки / упражнения:</label>
    <input type="text" name="notes" placeholder="3 круга, гантели 16кг..." style="width:100%">
  </div>
  <div>
    <button type="submit" style="width:100%;padding:9px 16px;">+ Записать тренировку</button>
  </div>
</form>
"""

    # ── История тренировок (таблица) ──
    if rows:
        trs = []
        for r in rows:
            w_id = r["id"]
            dt_str = str(r["started_at"] or "")[:16]
            sport_val = r.get("sport") or "Тренировка"
            dur_val = f"{r['duration_min']:.0f} мин" if r.get("duration_min") is not None else "—"
            kcal_val = f"{r['kcal']:.0f}" if r.get("kcal") is not None else "—"
            hr_val = f"{r['avg_hr']}" if r.get("avg_hr") is not None else "—"
            notes_val = r.get("notes") or ""
            source_val = r.get("source") or ("TCX" if r.get("file_hash") and not r.get("file_hash", "").startswith("manual") else "Вручную")

            del_btn = f"""<form method="post" action="/workouts/delete" style="display:inline;" data-confirm="Удалить тренировку #{w_id} ({_e(sport_val)})?">
<input type="hidden" name="csrf" value="{csrf_token}">{_uid_field(user_id)}
<input type="hidden" name="workout_id" value="{w_id}">
<button type="submit" class="btn-del">✕</button>
</form>"""

            trs.append(
                f"<tr><td><strong>{_e(dt_str)}</strong></td>"
                f"<td>{_e(sport_val)}</td>"
                f"<td>{_e(dur_val)}</td>"
                f"<td>{_e(kcal_val)}</td>"
                f"<td>{_e(hr_val)}</td>"
                f"<td><small>{_e(notes_val)}</small></td>"
                f"<td><span class='badge-source'>{_e(source_val)}</span></td>"
                f"<td style='text-align:right;'>{del_btn}</td></tr>"
            )
        table_html = (
            f'<div class="table-responsive"><table><tr>'
            f"<th>Дата и время</th><th>Активность</th><th>Длительность</th><th>Калории</th><th>Пульс</th><th>Заметки</th><th>Источник</th><th style='width:50px;'></th>"
            f"</tr>{''.join(trs)}</table></div>"
        )
    else:
        table_html = "<p>История тренировок пуста.</p>"

    return f"""
{err_html}
{msg_html}
<div class="card">
  <h2>Дашборд тренировок</h2>
  {stat_block}
</div>

<div class="card">
  <h2>Эффективность по видам спорта</h2>
  {eff_html}
</div>

<div class="card">
  <h2>Записать тренировку</h2>
  {add_form}
</div>

<div class="card">
  <h2>История тренировок (сессий: {len(rows)})</h2>
  {table_html}
</div>
"""


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.2f} МБ"


def knowledge_page(rows: list[dict], csrf_token: str, error: str | None = None,
                    message: str | None = None) -> str:
    """Список файлов Knowledge/ (тема = имя файла без расширения — так же, как
    знание.index()/read() в plugin трактуют тему) + форма загрузки.

    Подтверждения удаления в браузере НЕТ и быть не может: CSP страницы
    (server.py, default-src 'none' без script-src) блокирует любой inline-JS,
    так что onsubmit="return confirm(...)" здесь был бы мёртвым кодом,
    создающим ложное чувство защиты. Кнопка удаляет сразу. От чужого запроса
    защищает CSRF-токен в форме, от промаха — то, что файл можно загрузить
    заново."""
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    msg_html = f'<p class="msg">{html.escape(message)}</p>' if message else ""

    if not rows:
        table_html = "<p>Файлов пока нет.</p>"
    else:
        trs = []
        for r in rows:
            name_e = _e(r["name"])
            trs.append(
                "<tr>"
                f"<td>{name_e}</td><td>{_human_size(r['size'])}</td><td>{_e(r['mtime'])}</td>"
                f"<td><form method='post' action='/knowledge/delete'>"
                f"{_csrf_field(csrf_token)}"
                f"<input type='hidden' name='name' value='{name_e}'>"
                f"<button type='submit' class='danger'>Удалить</button></form></td>"
                "</tr>"
            )
        table_html = (
            '<div class="table-responsive"><table><tr><th>Тема</th><th>Размер</th><th>Изменён</th><th></th></tr>'
            + "".join(trs) + "</table></div>"
        )

    return f"""
<div class="card"><h2>Файлы знаний</h2>{error_html}{msg_html}{table_html}</div>
<div class="card">
  <h3>Загрузить файл</h3>
  <p class="comment">.md и .txt принимаются как есть; .pdf при загрузке конвертируется в .md
  (нужен пакет pypdf). Имя файла санируется до [A-Za-z0-9_-]. Потолок — 5 МБ.</p>
  <form method="post" action="/knowledge/upload" enctype="multipart/form-data">
    {_csrf_field(csrf_token)}
    <div class="row">
      <div><label>Файл</label><input type="file" name="file" accept=".md,.txt,.pdf" required></div>
      <div><label><input type="checkbox" name="overwrite"> перезаписать существующую тему</label></div>
      <div><button type="submit">Загрузить</button></div>
    </div>
  </form>
</div>
"""


# ---------------------------------------------------------------- forecast

def forecast_page(result: dict, actual: list[tuple[str, float]], horizon_days: int,
                  intake_kcal: float | None, reach_result: dict | None,
                  target_kg: float | None, csrf_token: str) -> str:
    """Прогноз массы: полоса, вехи по неделям, сценарий «а если есть N ккал».

    result — вывод forecast.project(), либо {"error": ...}. Отказ показывается
    как отказ: страница с пустым графиком и объяснением честнее, чем кривая,
    построенная на подставленных за человека числах.
    """
    def num(name, value, step="1", extra=""):
        v = "" if value is None else html.escape(str(value))
        return (f'<div><label>{html.escape(name)}</label>'
                f'<input type="number" name="{name}" value="{v}" step="{step}" {extra}></div>')

    form = f"""
<form method="get" action="/forecast">
  <div class="row">
    {num("horizon_days", horizon_days, "7", 'min="1" max="182"')}
    {num("intake_kcal", None if intake_kcal is None else round(intake_kcal), "50", 'min="0" placeholder="из лога"')}
    {num("target_kg", target_kg, "0.5", 'min="0" placeholder="напр. 115"')}
    <div><label>&nbsp;</label><button type="submit">Пересчитать</button></div>
  </div>
  <p class="comment">Приход пуст — берётся фактический лог за 14 дней. Задан —
  считается сценарий «что если есть ровно столько». Цель задана — внизу появится
  диапазон дат её достижения.</p>
</form>"""

    if "error" in result:
        return f"""
<div class="card"><h2>Прогноз массы</h2>
  <p class="error">{_e(result["error"])}</p>
  <p class="comment">Прогноз не выдаётся, пока данных не хватает: подставлять
  недостающие числа за человека — это выдавать оценку за измерение.</p>
</div>
<div class="card"><h3>Параметры</h3>{form}</div>"""

    weekly = result.get("weekly") or []
    rows = []
    for w in weekly:
        delta = w["mid"] - result["start_weight_kg"]
        rows.append(f'<tr><td>{_e(w["date"])}</td><td>{w["mid"]:.1f}</td>'
                    f'<td class="{"down" if delta < 0 else "up"}">{delta:+.1f}</td>'
                    f'<td>{w["lo"]:.1f} – {w["hi"]:.1f}</td></tr>')
    table = ('<div class="table-responsive"><table><tr><th>Дата</th><th>Ожидаемо, кг</th><th>Δ от старта</th>'
             '<th>Полоса, кг</th></tr>' + "".join(rows) + "</table></div>")

    end = result["end"]
    stats = "".join([
        _stat("⚖️ Сейчас", f'{result["start_weight_kg"]:.1f}<small>кг</small>'),
        _stat("🎯 Через " + str(result["horizon_days"]) + " дн", f'{end["mid"]:.1f}<small>кг</small>'),
        _stat("📉 Изменение", f'{end["mid"] - result["start_weight_kg"]:+.1f}<small>кг</small>', "", "down", "down"),
        _stat("🍽️ Приход", f'{result["intake_kcal"]}<small>ккал</small>', _e(result["intake_source"])),
        _stat("🔥 Изм. TDEE", f'{_e(result["measured_tdee"])}<small>ккал</small>',
              "нет 11 дней лога" if result["measured_tdee"] is None else "по логу еды"),
    ])

    chart = svg_band_chart(weekly, "Прогноз массы, кг", actual)

    plateau = ""
    if result.get("plateau"):
        plateau = f'<div class="card"><h3>Плато</h3><p>{_e(result["plateau"])}</p></div>'

    reach_html = ""
    if reach_result:
        if "error" in reach_result:
            reach_html = f'<p class="error">{_e(reach_result["error"])}</p>'
        elif reach_result.get("reached"):
            reach_html = (
                f'<p><b>{reach_result["target_kg"]:.1f} кг</b> ожидается '
                f'<b>{_e(reach_result["expected"])}</b>, диапазон '
                f'{_e(reach_result["earliest"])} – {_e(reach_result["latest"])}.</p>'
                f'<p class="comment">Диапазон, а не дата. Точная дата тут была бы '
                f'выдумкой: сама модель на полугоде ошибается на проценты массы.</p>')
        else:
            reach_html = f'<p>{_e(reach_result.get("note"))}</p>'
        reach_html = f'<div class="card"><h3>Достижение цели</h3>{reach_html}</div>'

    return f"""
<div class="card">
  <h2>Прогноз массы <span class="chip">оценка</span></h2>
  {stats}
  {chart}
  <p class="comment">{_e(result["caveat"])}</p>
</div>
{plateau}
{reach_html}
<div class="card"><h3>Параметры</h3>{form}</div>
<div class="card"><h3>По неделям</h3>{table}</div>
<div class="card"><h3>Как это посчитано</h3>
  <p>Не продолжение тренда. Посуточная симуляция баланса энергии: расход
  пересчитывается каждый день по текущей массе, поэтому по мере похудения
  дефицит сжимается и потеря замедляется — то, чего прямая линия не видит.
  Потеря делится на жир и тощую массу по правилу Форбса.</p>
  <p class="comment">Модель в духе Hall (Lancet, 2011), она же лежит в основе
  NIH Body Weight Planner. Статичное правило «7700 ккал = 1 кг» на годовом
  горизонте переоценивает потерю примерно вдвое.</p>
  <p class="comment">Адаптивный термогенез выключен (0%): согласованной величины
  в литературе нет — Fothergill (Obesity, 2016) против Martins (AJCN, 2022).
  Ставить туда правдоподобное число значило бы его выдумать.</p>
</div>"""


# ---------------------------------------------------------------- keys & models

_REORDER_SCRIPT = """<script>
(function(){
  var dragSrcEl = null;

  function initModelReorder() {
    var tbody = document.getElementById('models-tbody');
    if (!tbody) return;

    var rows = tbody.querySelectorAll('.model-row');
    rows.forEach(function(row) {
      row.addEventListener('dragstart', handleDragStart);
      row.addEventListener('dragenter', handleDragEnter);
      row.addEventListener('dragover', handleDragOver);
      row.addEventListener('dragleave', handleDragLeave);
      row.addEventListener('drop', handleDrop);
      row.addEventListener('dragend', handleDragEnd);
    });
    updateRowState();
  }

  function handleDragStart(e) {
    dragSrcEl = this;
    this.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', this.getAttribute('data-model') || '');
  }

  function handleDragOver(e) {
    if (e.preventDefault) e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    var target = e.target.closest('.model-row');
    if (target && target !== dragSrcEl) {
      var rect = target.getBoundingClientRect();
      var mid = rect.top + rect.height / 2;
      if (e.clientY < mid) {
        target.classList.add('drag-over-top');
        target.classList.remove('drag-over-bottom');
      } else {
        target.classList.add('drag-over-bottom');
        target.classList.remove('drag-over-top');
      }
    }
    return false;
  }

  function handleDragEnter(e) {}

  function handleDragLeave(e) {
    var target = e.target.closest('.model-row');
    if (target) {
      target.classList.remove('drag-over-top', 'drag-over-bottom');
    }
  }

  function handleDrop(e) {
    if (e.stopPropagation) e.stopPropagation();
    var target = e.target.closest('.model-row');
    if (target && target !== dragSrcEl) {
      var rect = target.getBoundingClientRect();
      var mid = rect.top + rect.height / 2;
      if (e.clientY < mid) {
        target.parentNode.insertBefore(dragSrcEl, target);
      } else {
        target.parentNode.insertBefore(dragSrcEl, target.nextSibling);
      }
      onOrderChanged();
    }
    return false;
  }

  function handleDragEnd(e) {
    this.classList.remove('dragging');
    var rows = document.querySelectorAll('.model-row');
    rows.forEach(function(r) {
      r.classList.remove('drag-over-top');
      r.classList.remove('drag-over-bottom');
    });
  }

  window.moveRowUp = function(btn) {
    var row = btn.closest('.model-row');
    var prev = row.previousElementSibling;
    if (prev && prev.classList.contains('model-row')) {
      row.parentNode.insertBefore(row, prev);
      onOrderChanged();
    }
  };

  window.moveRowDown = function(btn) {
    var row = btn.closest('.model-row');
    var next = row.nextElementSibling;
    if (next && next.classList.contains('model-row')) {
      row.parentNode.insertBefore(next, row);
      onOrderChanged();
    }
  };

  window.makePrimary = function(btn) {
    var row = btn.closest('.model-row');
    var tbody = row.parentNode;
    var first = tbody.firstElementChild;
    if (row !== first) {
      tbody.insertBefore(row, first);
    }
    updateRowState();
    var banner = document.getElementById('reorder-banner');
    if (banner) {
      banner.style.display = 'block';
      banner.innerHTML = '<div style="font-weight:600;color:var(--accent);padding:4px">⏳ Сохраняем новую основную модель...</div>';
    }
    var form = document.getElementById('reorder-form');
    if (form) form.submit();
  };
  window.moveRowToTop = window.makePrimary;

  function onOrderChanged() {
    updateRowState();
    var banner = document.getElementById('reorder-banner');
    if (banner) banner.style.display = 'block';
    var saveBtn = document.getElementById('save-order-btn-bottom');
    if (saveBtn) {
      saveBtn.style.display = 'inline-block';
      saveBtn.classList.add('highlight-pulse');
    }
  }

  function updateRowState() {
    var tbody = document.getElementById('models-tbody');
    if (!tbody) return;
    var rows = tbody.querySelectorAll('.model-row');
    var order = [];
    rows.forEach(function(row, index) {
      var m = row.getAttribute('data-model');
      order.push(m);
      var badgeCell = row.querySelector('.priority-cell');
      if (badgeCell) {
        if (index === 0) {
          badgeCell.innerHTML = '<span class="chip priority-badge priority-badge-primary">🟢 #1 ОСНОВНАЯ</span>';
        } else {
          badgeCell.innerHTML = '<span class="chip priority-badge">#' + (index + 1) + '</span>';
        }
      }
      var upBtn = row.querySelector('.btn-up');
      var downBtn = row.querySelector('.btn-down');
      var topBtn = row.querySelector('.btn-top');
      if (upBtn) upBtn.disabled = (index === 0);
      if (downBtn) downBtn.disabled = (index === rows.length - 1);
      if (topBtn) topBtn.disabled = (index === 0);
    });
    var input = document.getElementById('model_order_input');
    if (input) input.value = JSON.stringify(order);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initModelReorder);
  } else {
    initModelReorder();
  }
})();
</script>"""


def keys_page(
    providers_list: list[dict],
    keys_dict: dict[str, list[str]],
    providers_yaml: str,
    csrf_token: str,
    message: str | None = None,
    error: str | None = None,
    test_results: list[dict] | None = None,
) -> str:
    msg_html = ""
    if error:
        msg_html = f'<p class="error">{html.escape(error)}</p>'
    elif message:
        msg_html = f'<p class="msg">{html.escape(message)}</p>'

    google_keys = keys_dict.get("GOOGLE_API_KEY", [])
    groq_keys = keys_dict.get("GROQ_API_KEY", [])
    openrouter_keys = keys_dict.get("OPENROUTER_API_KEY", [])
    openai_keys = keys_dict.get("OPENAI_API_KEY", [])

    total_keys = len(google_keys) + len(groq_keys) + len(openrouter_keys) + len(openai_keys)

    # Статистика
    stats_html = "".join([
        _stat("🔑 Google API Ключей", f'{len(google_keys)}<small>шт</small>', "Ротация активна" if len(google_keys) > 1 else "1 ключ"),
        _stat("🤖 Моделей в цепочке", f'{len(providers_list)}<small>мод</small>', "Порядок фолбэка"),
        _stat("🛡️ Резервные ключи", f'{len(groq_keys) + len(openrouter_keys) + len(openai_keys)}<small>шт</small>', "Groq / OpenRouter"),
    ])

    # Таблица моделей с drag & drop и стрелочками приоритета
    provider_rows = []
    for idx, p in enumerate(providers_list, 1):
        m = p.get("model", "—")
        url = p.get("base_url", "—")
        env_var = p.get("api_key_env", "—")
        assigned_keys = keys_dict.get(env_var, [])
        keys_count = len(assigned_keys)
        
        status_badge = f'<span class="chip ok">✓ {keys_count} ключ(ей)</span>' if keys_count > 0 else '<span class="chip bad">⚠ Нет ключей</span>'
        
        priority_badge = (
            f'<span class="chip priority-badge priority-badge-primary">🟢 #1 ОСНОВНАЯ</span>'
            if idx == 1 else
            f'<span class="chip priority-badge">#{idx}</span>'
        )

        extra_badges = []
        if "max_tokens" in p:
            extra_badges.append(f'<span class="comment" style="font-size:11px;margin-left:4px">max:{p["max_tokens"]}</span>')
        if "reasoning_effort" in p:
            extra_badges.append(f'<span class="comment" style="font-size:11px;margin-left:4px">reasoning:{p["reasoning_effort"]}</span>')
        if "extra_body" in p:
            extra_badges.append('<span class="comment" style="font-size:11px;margin-left:4px">thinking</span>')
        extra_badges_html = " ".join(extra_badges)

        provider_rows.append(
            f'<tr class="model-row" draggable="true" data-model="{html.escape(m)}" data-idx="{idx-1}">'
            f'<td class="drag-handle" title="Потяните для перетаскивания (Drag & Drop)">⠿</td>'
            f'<td class="priority-cell">{priority_badge}</td>'
            f'<td><code style="font-size:13px;font-weight:600">{html.escape(m)}</code>{extra_badges_html}</td>'
            f'<td><div><code>{html.escape(env_var)}</code></div><small style="color:var(--muted);font-size:11px">{html.escape(url)}</small></td>'
            f'<td>{status_badge}</td>'
            f'<td style="text-align:center;white-space:nowrap">'
            f'<button type="button" class="btn-arrow btn-up" onclick="moveRowUp(this)" title="Поднять выше в цепочке"'
            f'{" disabled" if idx == 1 else ""}>▲</button>'
            f'<button type="button" class="btn-arrow btn-down" onclick="moveRowDown(this)" title="Опустить ниже в цепочке"'
            f'{" disabled" if idx == len(providers_list) else ""}>▼</button>'
            f'<button type="button" class="btn-arrow btn-top" onclick="makePrimary(this)" title="Сделать основной моделью (#1) прямо сейчас"'
            f'{" disabled" if idx == 1 else ""}>🔝</button>'
            f'</td></tr>'
        )

    model_options = []
    for idx, p in enumerate(providers_list, 1):
        m = p.get("model", "—")
        sel = " selected" if idx == 1 else ""
        model_options.append(f'<option value="{html.escape(m)}"{sel}>{"🟢 " if idx==1 else ""}{idx}. {html.escape(m)}</option>')

    quick_select_html = (
        '<form method="post" action="/keys" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;'
        'background:var(--soft);padding:12px 16px;border-radius:10px;margin-bottom:14px;border:1px solid var(--border)">'
        f'{_csrf_field(csrf_token)}'
        '<input type="hidden" name="action" value="set_primary">'
        '<span style="font-weight:600;font-size:13px">Быстро назначить основную модель (#1):</span>'
        f'<select name="model" style="padding:6px 10px;font-size:13px;border-radius:8px;background:var(--bg2);color:var(--fg)">{"".join(model_options)}</select>'
        '<button type="submit" style="padding:6px 14px;font-size:13px">⚡ Сделать основной</button>'
        '</form>'
    )

    providers_table = (
        quick_select_html +
        '<form id="reorder-form" method="post" action="/keys" style="margin:0">'
        f'{_csrf_field(csrf_token)}'
        '<input type="hidden" name="action" value="reorder_providers">'
        '<input type="hidden" name="model_order" id="model_order_input" value="">'
        '<div id="reorder-banner" style="display:none;background:color-mix(in srgb,var(--accent) 12%,var(--bg2));'
        'border:1px solid var(--accent);border-radius:10px;padding:12px 16px;margin-bottom:14px">'
        '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">'
        '<div><b>⚡ Порядок моделей изменён!</b>'
        '<div class="comment" style="margin-top:2px">Нажмите «Сохранить новый порядок», чтобы применить изменения для бота.</div></div>'
        '<button type="submit" class="highlight-pulse" style="background:linear-gradient(135deg,#12905f,#0a7048);color:#fff;padding:8px 16px">'
        '💾 Сохранить новый порядок</button></div></div>'
        '<div class="table-responsive">'
        '<table class="reorder-table" id="models-table">'
        '<thead><tr>'
        '<th style="width:36px;text-align:center">⠿</th>'
        '<th style="width:130px">Приоритет</th>'
        '<th>Модель</th>'
        '<th>Ключ / Base URL</th>'
        '<th>Ключи</th>'
        '<th style="width:140px;text-align:center">Действия</th>'
        '</tr></thead>'
        f'<tbody id="models-tbody">{"".join(provider_rows)}</tbody>'
        '</table>'
        '</div>'
        '<p style="margin-top:14px">'
        '<button type="submit" id="save-order-btn-bottom" style="display:none;background:linear-gradient(135deg,#12905f,#0a7048);color:#fff">'
        '💾 Сохранить новый порядок моделей</button>'
        '</p>'
        '</form>'
    )

    # Результаты тестирования
    test_html = ""
    if test_results is not None:
        t_rows = []
        for tr in test_results:
            st = tr.get("status")
            badge = '<span class="chip ok">🟢 200 OK</span>' if st == 200 else (
                '<span class="chip warn">🟡 429 Лимит квоты</span>' if st == 429 else
                '<span class="chip bad">🔴 Ошибка ' + str(st) + '</span>'
            )
            lat = f"{tr.get('latency_ms', 0):.0f} мс" if tr.get("latency_ms") else "—"
            err_detail = html.escape(str(tr.get("message") or ""))
            t_rows.append(
                f"<tr><td><code>{html.escape(tr.get('model', ''))}</code></td>"
                f"<td><code>{html.escape(tr.get('masked_key', ''))}</code></td>"
                f"<td>{badge}</td><td>{lat}</td><td><small>{err_detail}</small></td></tr>"
            )
        test_html = f"""
<div class="card">
  <h3>Результаты диагностики моделей и ключей</h3>
  <div class="table-responsive">
  <table><tr><th>Модель</th><th>Ключ</th><th>Статус</th><th>Задержка</th><th>Подробности</th></tr>
  {''.join(t_rows)}
  </table>
  </div>
</div>"""

    joined_google = "\n".join(google_keys)
    joined_groq = "\n".join(groq_keys)
    joined_openrouter = "\n".join(openrouter_keys)
    joined_openai = "\n".join(openai_keys)

    return f"""
<div class="card">
  <h2>Ротация моделей и API-ключей</h2>
  {msg_html}
  <div class="stats">{stats_html}</div>
</div>

<div class="card">
  <h3>1. Список API-ключей</h3>
  <p class="comment">Ключи сохраняются в <code>~/.hermes/.env</code> с правами 0600 и подхватываются ботом на лету без перезагрузки.
  Если задано несколько ключей (каждый с новой строки), бот циклически распределяет нагрузку между ними (Round-Robin) и автоматически переключается на следующий при ошибке 429 (Resource Exhausted / исчерпание квоты).</p>
  
  <form method="post" action="/keys">
    {_csrf_field(csrf_token)}
    <input type="hidden" name="action" value="save_keys">

    <div style="margin-bottom:16px">
      <label for="google_api_keys"><b>Google Gemini API Ключи</b> (основные бесплатные модели)</label>
      <textarea id="google_api_keys" name="google_api_keys" style="min-height:100px;margin-top:6px" placeholder="AIzaSy...&#10;AIzaSy... (каждый ключ с новой строки)">{html.escape(joined_google)}</textarea>
      <p class="comment">Бесплатные ключи создаются в <a href="https://aistudio.google.com/app/apikey" target="_blank">Google AI Studio</a>. Рекомендуется добавить 2-3 ключа из разных Google-аккаунтов для полной защиты от 429.</p>
    </div>

    <div style="margin-bottom:16px">
      <label for="groq_api_keys"><b>Groq API Ключи</b> (опционально: быстрый бесплатный LLaMA 3.3 70B)</label>
      <textarea id="groq_api_keys" name="groq_api_keys" style="min-height:60px;margin-top:6px" placeholder="gsk_...">{html.escape(joined_groq)}</textarea>
      <p class="comment">Бесплатный ключ на <a href="https://console.groq.com/keys" target="_blank">console.groq.com</a>.</p>
    </div>

    <div class="grid" style="margin-bottom:16px">
      <div>
        <label for="openrouter_api_keys"><b>OpenRouter API Ключи</b> (опционально)</label>
        <textarea id="openrouter_api_keys" name="openrouter_api_keys" style="min-height:60px;margin-top:6px" placeholder="sk-or-v1-...">{html.escape(joined_openrouter)}</textarea>
      </div>
      <div>
        <label for="openai_api_keys"><b>OpenAI API Ключи</b> (опционально)</label>
        <textarea id="openai_api_keys" name="openai_api_keys" style="min-height:60px;margin-top:6px" placeholder="sk-...">{html.escape(joined_openai)}</textarea>
      </div>
    </div>

    <p><button type="submit">💾 Сохранить API-ключи</button></p>
  </form>
</div>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <h3 style="margin-bottom:6px">2. Цепочка и приоритет моделей (Fallback Chain)</h3>
    <span class="comment">Перетаскивайте мышью (drag & drop) или используйте стрелочки ▲ / ▼</span>
  </div>
  <p class="comment" style="margin-top:0">Порядок обращения: бот пробует первую модель (#1) со всеми её ключами. Если модель исчерпала квоту или вернула ошибку, запрос автоматически передаётся следующей модели в цепочке.</p>
  {providers_table}

  <details style="margin-top:16px">
    <summary style="cursor:pointer;font-weight:600;color:var(--accent)">Прямое редактирование в YAML (config.yaml)</summary>
    <form method="post" action="/keys" style="margin-top:10px">
      {_csrf_field(csrf_token)}
      <input type="hidden" name="action" value="save_providers">
      <textarea name="providers_yaml" style="min-height:160px;font-family:ui-monospace,monospace">{html.escape(providers_yaml)}</textarea>
      <p><button type="submit">Сохранить YAML моделей</button></p>
    </form>
  </details>
</div>

<div class="card">
  <h3>3. Диагностика и проверка связи</h3>
  <p class="comment">Отправляет тестовый минимальный запрос к каждой модели с каждым из настроенных ключей для проверки доступности и квот.</p>
  <form method="post" action="/keys/test">
    {_csrf_field(csrf_token)}
    <button type="submit">⚡ Проверить все ключи и модели</button>
  </form>
</div>

{test_html}

{_REORDER_SCRIPT}
"""

