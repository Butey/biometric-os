---
name: Admin Commands
description: Админ-команды для управления системой через Telegram
---

# Admin Commands Grammar

Подкоманды `admin_cmd`, парсятся в Python, не моделью.

## mode
Режим живёт в БД по caller id (не в config.yaml), по умолчанию `user`.
```
admin_cmd(command: "mode")        # текущий режим
admin_cmd(command: "mode user")   # выход из admin — доступно всем с разрешённым id
admin_cmd(command: "mode admin")  # вход в admin — только allowlist
```
Всё ниже требует ОДНОВРЕМЕННО: caller в allowlist И `mode admin`.
В `user` — отказ, сначала `mode admin`.

## status / guards / targets
Сводка БД; прогон `check_all`; пороги из config.yaml.
```
admin_cmd(command: "status")
admin_cmd(command: "guards")
admin_cmd(command: "targets")
```

## set
Правка `config.yaml`, только существующие ключи, сохраняет `.bak`.
```
admin_cmd(command: "set lbm_ratio_threshold 0.18")
```

## milestone add / del / milestones
```
admin_cmd(command: "milestone add goal_weight weight_kg 75.0 2026-12-31")
admin_cmd(command: "milestone del old_goal")
admin_cmd(command: "milestones")
```

## recalc / export / backup / alerts
```
admin_cmd(command: "recalc [date]")
admin_cmd(command: "export")
admin_cmd(command: "backup")
admin_cmd(command: "alerts [n]")   # n=10 по умолчанию
```

## Authorization
- `mode` / `mode user` — любой caller с разрешённым id.
- `mode admin` и всё остальное — только `admin.telegram_admin_ids`. Пусто = всё отклонено.
- Нет caller id вовсе — отказ на всём, включая `mode`.
```yaml
admin:
  telegram_admin_ids: [123456789, 987654321]
```
Никогда не разрешайте всех — пусто значит "выключено полностью".
