"""JSON schemas for health plugin tools. Draft-07, descriptions in Russian."""

council_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["status", "request"],
            "description": "status (умолчание) — последний результат/статус; "
                           "request — созвать консилиум (нужен reason), ставит задачу в фон и сразу отвечает"
        },
        "reason": {
            "type": "string",
            "enum": ["manual", "dose"],
            "description": "Для action=request: manual — по просьбе человека («разбери мои данные»), "
                           "dose — перед любой рекомендацией дозы"
        },
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

log_food_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete"],
            "description": "add (умолчание) — записать приём; delete — удалить, нужен food_log_id"
        },
        "food_log_id": {
            "type": "integer",
            "description": "Для action=delete: номер приёма (#N из журнала)"
        },
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Для action=delete: убрать только эти продукты (подстрока названия). Не задан — удаляется приём целиком"
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название блюда"},
                    "grams": {"type": "number", "description": "Вес в граммах; обязателен вместе с per_100g"},
                    "pieces": {"type": "number", "description": "Вместо grams для нарезки: сколько кусочков съедено; граммы посчитает код по сохранённому весу кусочка (food_lookup piece). Не умножай сам. Человек назвал и граммы, и штуки («3 яйца, 150 г») - передай ОБА: ккал считаются по grams, из запаса списывается ровно pieces"},
                    "kcal": {"type": "number", "description": "Ккал; обязательно без per_100g. Не знаешь точно — оцени сам, человека не переспрашивай"},
                    "protein_g": {"type": "number", "description": "Белки, г; обязательно без per_100g — при незнании оценка"},
                    "fat_g": {"type": "number", "description": "Жиры, г; обязательно без per_100g — при незнании оценка, влияет на LIPID_GUARD"},
                    "carbs_g": {"type": "number", "description": "Углеводы, г; обязательно без per_100g — при незнании оценка"},
                    "fiber_g": {"type": "number", "description": "Клетчатка, г, отдельно от carbs_g; оценивай всегда: мясо, рыба, яйца, масло, сахар — 0; овощи, фрукты, крупы, хлеб, бобовые, орехи — число"},
                    "per_100g": {
                        "type": "object",
                        "properties": {
                            "kcal": {"type": "number"},
                            "protein_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                            "carbs_g": {"type": "number"},
                            "fiber_g": {"type": "number"}
                        },
                        "required": ["kcal", "protein_g", "fat_g", "carbs_g"],
                        "description": "Состав на 100 г из food_lookup/этикетки; с grams код сам считает КБЖУ позиции"
                    },
                    "source": {
                        "type": "string",
                        "enum": ["off", "my_product", "label", "estimate"],
                        "description": "Откуда состав: off/my_product/label — через food_lookup, estimate — оценка модели"
                    },
                    "plate_category": {"type": "string", "description": "Категория тарелки"}
                },
                "required": ["name"]
            },
            "description": "Один item — одно блюдо; items одного вызова образуют один приём пищи"
        },
        "meal_slot": {
            "type": "string",
            "enum": ["breakfast", "lunch", "dinner", "snack"],
            "description": "Только по прямому слову человека (завтрак/обед/ужин/перекус); не названо — не передавайте"
        },
        "eaten_at": {
            "type": "string",
            "description": "ISO timestamp, по умолчанию сейчас"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": ["items"]
}

food_lookup_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["match", "search", "remember", "piece", "list", "forget"],
            "description": "match (умолчание) — найти свой сохранённый продукт; search — до 5 кандидатов из Open Food Facts; remember — сохранить/обновить свой продукт; piece — записать вес одного кусочка продукта (name, piece_g); list — список своих продуктов; forget — убрать"
        },
        "name": {"type": "string", "description": "Название продукта. Нужно для match/search/remember/forget"},
        "off_code": {"type": "string", "description": "Для remember: код из результата search (candidate.off_code) — код сам подтянет состав по нему"},
        "kcal_100g": {"type": "number", "description": "Для remember: калории на 100 г — либо это, либо off_code"},
        "protein_100g": {"type": "number", "description": "Для remember: белки на 100 г"},
        "fat_100g": {"type": "number", "description": "Для remember: жиры на 100 г"},
        "carbs_100g": {"type": "number", "description": "Для remember: углеводы на 100 г"},
        "fiber_100g": {"type": "number", "description": "Для remember: клетчатка на 100 г"},
        "piece_g": {"type": "number", "description": "Для piece: вес одного кусочка/ломтика в граммах"},
        "source": {
            "type": "string",
            "enum": ["off", "label", "estimate"],
            "description": "Для remember: откуда цифры — off (Open Food Facts), label (с этикетки от человека) или estimate (оценка модели)"
        },
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

log_water_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать воду; delete — удалить (water_id или clear_day=true за день); list — список за дату"
        },
        "water_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи из list"
        },
        "clear_day": {
            "type": "boolean",
            "description": "Для action=delete: если true — удалить ВСЮ воду за указанную дату (по умолчанию сегодня)"
        },
        "date": {
            "type": "string",
            "description": "Дата в формате YYYY-MM-DD для action=list или action=delete (по умолчанию сегодня)"
        },
        "ml": {
            "type": "number",
            "description": "Объем в миллилитрах, больше нуля"
        },
        "at": {
            "type": "string",
            "description": "ISO timestamp, по умолчанию сейчас"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

log_sleep_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать сон; delete — удалить (sleep_id, без него — последняя ночь); list — список"
        },
        "sleep_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи"
        },
        "night_date": {
            "type": "string",
            "description": "Дата ночи = дата УТРА пробуждения, YYYY-MM-DD. По умолчанию сегодня"
        },
        "duration_min": {
            "type": "integer",
            "description": "Общая длительность сна в минутах. Главное поле"
        },
        "bedtime": {"type": "string", "description": "Время отхода ко сну HH:MM, если известно"},
        "wake_time": {"type": "string", "description": "Время подъёма HH:MM, если известно"},
        "quality": {
            "type": "integer",
            "description": "Субъективная оценка 1-5, где 5 — выспался. Человек оценку не назвал — поле НЕ передавай (не 0 и не null); не выдумывай оценку"
        },
        "deep_min": {"type": "integer", "description": "Глубокий сон, мин, только с прибора"},
        "rem_min": {"type": "integer", "description": "REM, мин, только с прибора"},
        "awake_min": {"type": "integer", "description": "Пробуждения, мин, только с прибора"},
        "spo2_avg": {"type": "integer", "description": "Средний SpO2 за ночь, % (70-100), только с прибора"},
        "source": {"type": "string", "description": "Откуда данные: scale_app, часы, вручную"},
        "notes": {"type": "string", "description": "Свободный комментарий"},
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"
        },
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": []
}

equipment_schema = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list", "add", "remove"],
                   "description": "list — что есть; add — добавить; remove — убрать"},
        "name": {"type": "string", "description": "Название: «гантели разборные», «VR-шлем», «турник»"},
        "kind": {"type": "string", "enum": ["strength", "cardio", "accessory"],
                 "description": "strength — силовое, cardio — кардио, accessory — вспомогательное"},
        "detail": {"type": "string", "description": "Детали: вес, модель, диапазон. «2x24 кг», «Quest 3»"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

log_workout_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (по умолчанию) — записать тренировку/активность; delete — удалить по workout_id; list — история"
        },
        "workout_id": {
            "type": "integer",
            "description": "Для action=delete: ID тренировки"
        },
        "sport": {
            "type": "string",
            "description": "Вид активности / спорта: «Силовая», «Гантели», «Ходьба», «Бег», «Велосипед», «VR», «Растяжка» и т.п."
        },
        "duration_min": {
            "type": "number",
            "description": "Длительность тренировки в минутах"
        },
        "kcal": {
            "type": "number",
            "description": "Потраченные калории (по часам/трекеру или расчётные)"
        },
        "avg_hr": {
            "type": "integer",
            "description": "Средний пульс (уд/мин), если измерялся"
        },
        "started_at": {
            "type": "string",
            "description": "Дата и время начала тренировки (ISO: YYYY-MM-DD HH:MM:SS), по умолчанию сейчас"
        },
        "notes": {
            "type": "string",
            "description": "Заметки о тренировке: упражнения, веса, самочувствие"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

plan_day_schema = {
    "type": "object",
    "properties": {
        # get первым намеренно: контракт инструмента проверяется вызовом только с
        # обязательными полями, а save без body — законная ошибка. Читающее
        # действие по умолчанию безопаснее пишущего.
        "action": {"type": "string", "enum": ["get", "list", "save", "delete"],
                   "description": "get — прочитать план на дату; list — история; save — записать; delete — удалить план на дату (или all=true для всех)"},
        "kind": {"type": "string", "enum": ["workout", "meal"], "description": "Тип плана (workout — тренировка, meal — питание)"},
        "date": {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"},
        "body": {"type": "string", "description": "Сам план текстом (markdown). Обязателен для save"},
        "rationale": {"type": "string",
                      "description": "На чём построен: данные дня, доступное оборудование, ограничения"},
        "all": {"type": "boolean", "description": "Для action=delete: удалить все планы (опционально с фильтром по kind)"},
        "limit": {"type": "integer", "description": "Сколько последних планов вернуть для list"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

refeed_schema = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["status", "schedule", "clear"],
                   "description": "status — фаза сегодня и уведомления; schedule — назначить рефид (по умолчанию разовый на 4 дня с причиной); clear — снять будущие"},
        "start": {"type": "string", "description": "Дата старта YYYY-MM-DD, по умолчанию сегодня или завтра"},
        "days": {"type": "integer", "description": "Длительность рефида в днях, 1..14 (по умолчанию 4)"},
        "reason": {"type": "string", "enum": ["plateau", "recovery_low", "council", "manual"],
                   "description": "Причина: plateau (плато), recovery_low (восстановление), council (консилиум), manual (вручную)"},
        "horizon_weeks": {"type": "integer", "description": "УСТАРЕЛО: цикл MATADOR на N недель (не рекомендуется)"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

meal_options_schema = {
    "type": "object",
    "properties": {
        "slot": {
            "type": "string",
            "enum": ["breakfast", "lunch", "dinner", "snack"],
            "description": "Для какого приёма собрать варианты; без него - по текущему времени"
        },
        "count": {"type": "integer", "description": "Сколько вариантов (1-5, по умолчанию 3)"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": []
}

log_bp_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) - записать замер давления; delete - удалить (bp_id, без него - последний); list - список"
        },
        "bp_id": {"type": "integer", "description": "Для action=delete: номер записи"},
        "systolic": {"type": "integer", "description": "Верхнее (систолическое) давление, мм рт. ст. (60-260)"},
        "diastolic": {"type": "integer", "description": "Нижнее (диастолическое) давление, мм рт. ст. (30-160)"},
        "pulse": {"type": "integer", "description": "Пульс с тонометра, уд/мин, если виден (30-220)"},
        "context": {"type": "string", "description": "Условия замера: утром в покое, после нагрузки, после кофе и т.д."},
        "at": {"type": "string", "description": "ISO timestamp замера, по умолчанию сейчас"},
        "limit": {"type": "integer", "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": []
}

log_glucose_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать замер; delete — удалить (glucose_id, без него — последний); list — список"
        },
        "glucose_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи"
        },
        "mmol_l": {
            "type": "number",
            "description": "Уровень глюкозы в ммоль/л"
        },
        "context": {
            "type": "string",
            "description": "Контекст замера (до еды, после еды и т.д.)"
        },
        "at": {
            "type": "string",
            "description": "ISO timestamp, по умолчанию сейчас"
        },
        "confirmed": {
            "type": "boolean",
            "description": "Подтверждено ли значение"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

log_side_effect_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать побочный эффект; delete — удалить (side_effect_id, без него — последняя); list — список"
        },
        "side_effect_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи"
        },
        "symptom": {
            "type": "string",
            "description": "Симптом (тошнота, рвота, запор, диарея, изжога, слабость и т.п.)"
        },
        "severity": {
            "type": "string",
            "enum": ["mild", "moderate", "severe"],
            "description": "Тяжесть: mild — лёгкая, moderate — средняя, severe — тяжёлая"
        },
        "notes": {
            "type": "string",
            "description": "Заметка"
        },
        "at": {
            "type": "string",
            "description": "ISO timestamp, по умолчанию сейчас"
        },
        "since_days": {
            "type": "integer",
            "description": "Для action=list: только записи не старше стольких дней (без параметра — просто последние limit записей)"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 20)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

log_weight_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать вес; delete — удалить (weight_id, без него — последний); list — список"
        },
        "weight_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи"
        },
        "weight_kg": {
            "type": "number",
            "description": "Вес в килограммах"
        },
        "fat_pct": {"type": "number", "description": "Процент жира"},
        "bmi": {"type": "number", "description": "Индекс массы тела"},
        "skeletal_muscle_pct": {"type": "number", "description": "Процент скелетной мышцы"},
        "muscle_mass_kg": {"type": "number", "description": "Мышечная масса"},
        "protein_pct": {"type": "number", "description": "Процент белка"},
        "device_bmr_kcal": {"type": "number", "description": "BMR по устройству"},
        "ffm_kg": {"type": "number", "description": "Безжировая масса"},
        "subcutaneous_fat_pct": {"type": "number", "description": "Подкожный жир"},
        "visceral_fat": {"type": "number", "description": "Висцеральный жир"},
        "water_pct": {"type": "number", "description": "Процент воды"},
        "bone_mass_kg": {"type": "number", "description": "Костная масса"},
        "metabolic_age": {"type": "number", "description": "Метаболический возраст"},
        "device_mac": {"type": "string", "description": "MAC адрес устройства"},
        "measured_at": {
            "type": "string",
            "description": "ISO timestamp, по умолчанию сейчас"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

log_anthropometry_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать замер; delete — удалить (anthropometry_id, без него — последний); list — список"
        },
        "anthropometry_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи"
        },
        "site": {
            "type": "string",
            "enum": ["талия", "грудь", "таз", "бедро", "шея", "бицепс"],
            "description": "Место измерения; обязательно для action=add. Талия/таз обязательны для расчёта Т/Б"
        },
        "value_cm": {
            "type": "number",
            "description": "Значение в сантиметрах"
        },
        "measured_on": {
            "type": "string",
            "description": "ISO date, по умолчанию сегодня"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

log_med_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать приём; delete — удалить (med_id, без него — последний); list — список"
        },
        "med_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи"
        },
        "drug": {
            "type": "string",
            "description": "Название препарата; обязательно для action=add"
        },
        "dose": {
            "type": "string",
            "description": "Дозировка; обязательно для action=add"
        },
        "route": {
            "type": "string",
            "enum": ["injection", "oral", "topical"],
            "description": "Путь введения: инъекция, перорально или топически; обязательно для action=add"
        },
        "unit": {
            "type": "string",
            "enum": ["mg", "ml", "IU", "mcg"],
            "description": "Единица дозы, необязательно"
        },
        "site": {
            "type": "string",
            "description": "Место инъекции для ротации, необязательно"
        },
        "notes": {
            "type": "string",
            "description": "Свободный комментарий, необязательно"
        },
        "at": {
            "type": "string",
            "description": "ISO timestamp, по умолчанию сейчас"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 10)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

import_scale_export_schema = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Путь к файлу (xlsx, xls, csv или tcx); формат определяется по содержимому, не по расширению. Путь на Google Диске задаётся как gdrive:Папка/файл.xlsx — файл скачивается сам"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": ["file_path"]
}

get_day_summary_schema = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "ISO date"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        },
        "format": {
            "type": "string",
            "enum": ["bar", "dashboard", "journal", "console", "weight_history"],
            "description": "bar — исходный JSON-payload; dashboard (по умолчанию) — сводка КБЖУ дня; journal — журнал питания по приёмам; console — единый метаболический пульт (тело, вес, КБЖУ/вода, еда, гарды, фарма); weight_history — график веса за всё время с первого замера"
        }
    }
}

pharma_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["status", "schedule", "restock", "remove"],
            "description": "status — расписание и остаток; schedule — задать/обновить препарат и дозу; restock — пополнить остаток; remove — убрать из расписания"
        },
        "substance": {"type": "string", "description": "Название препарата; обязательно для schedule/restock/remove"},
        "dose": {
            "type": "number",
            "description": "Разовая доза. schedule держит рамки лестницы титрации из карты препарата; вне рамок — только с by_doctor=true (иначе явная ошибка со ступенями)"
        },
        "by_doctor": {
            "type": "boolean",
            "description": "Доза назначена врачом (со слов человека) — снимает рамки лестницы для этой дозы"
        },
        "unit": {"type": "string", "enum": ["mg", "ml", "IU", "mcg"], "description": "Единица дозы"},
        "route": {"type": "string", "enum": ["injection", "oral", "topical"], "description": "Путь введения"},
        "every_days": {"type": "integer", "description": "Каденция: раз в N дней (7 = еженедельно)"},
        "next_at": {"type": "string", "description": "Следующая доза, ISO дата-время"},
        "stock_doses": {"type": "number", "description": "Остаток в дозах (задать при schedule; 0 = не задан, пополнение только через restock)"},
        "add_doses": {"type": "number", "description": "Сколько доз добавить (для restock)"},
        "notes": {"type": "string", "description": "Заметка"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

plans_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["show", "set_meal", "set_workout", "vs_actual", "remove_meal", "remove_workout", "clear"],
            "description": "show — недельный план; set_meal/set_workout — задать позицию в шаблон дня; vs_actual — план vs факт за дату; remove_meal/remove_workout — удалить позицию; clear — очистить шаблон"
        },
        "day_of_week": {"type": "integer", "description": "День недели 0=Пн..6=Вс (для set_*/опц. show)"},
        "meal_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"], "description": "Приём пищи (для set_meal)"},
        "name": {"type": "string", "description": "Название блюда (set_meal) или тренировки (set_workout)"},
        "kcal": {"type": "number", "description": "Плановые ккал блюда"},
        "protein_g": {"type": "number", "description": "Плановый белок, г"},
        "fat_g": {"type": "number", "description": "Плановый жир, г"},
        "carbs_g": {"type": "number", "description": "Плановые углеводы, г"},
        "kind": {"type": "string", "description": "Тип тренировки (set_workout)"},
        "duration_min": {"type": "number", "description": "Длительность тренировки, мин"},
        "date": {"type": "string", "description": "ISO-дата для vs_actual (умолчание — сегодня)"},
        "notes": {"type": "string", "description": "Заметка"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

pantry_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "add", "remove"],
            "description": "list — показать запасы по категориям; add — добавить/пополнить продукт; remove — списать"
        },
        "name": {"type": "string", "description": "Название продукта (для add/remove)"},
        "qty": {"type": "number", "description": "Количество или вес. Для remove без qty — списать позицию целиком."},
        "unit": {"type": "string", "description": "Единица: г, шт, мл и т.п. Нарезку (хлеб, сыр, колбасу) считаем в кусочках: unit=кусочек + piece_weight_g"},
        "piece_weight_g": {"type": "number", "description": "Для add в штуках/банках: вес (мл для жидкостей) ОДНОЙ штуки или банки - код сам переведёт запас в граммы. Спроси у человека, не выдумывай."},
        "no_weight": {"type": "boolean", "description": "true только если человек на вопрос о весе ответил, что не знает или не хочет называть: тогда запас останется в штуках"},
        "category": {"type": "string", "enum": ["Белковые", "Молочка/Сыры", "Овощи/Фрукты", "Сложные углеводы", "Прочее"], "description": "Категория продукта"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

style_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "add", "use", "del"],
            "description": "list — показать стили персоны, активный отмечен («мои стили»/«список стилей»); add — создать/обновить стиль, не активирует его; use — сделать стиль активным; del — удалить стиль (требует confirm)."
        },
        "name": {"type": "string", "description": "Название стиля (для add/use/del)"},
        "instruction": {"type": "string", "description": "Текст инструкции о тоне/манере ответов (для add)"},
        "confirm": {"type": "string", "description": "Для del: буквально 'УДАЛИТЬ', иначе удаление не выполняется"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

get_trends_schema = {
    "type": "object",
    "properties": {
        "window_days": {
            "type": "integer",
            "description": "Окно в днях, по умолчанию 30"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    }
}

get_status_bar_schema = {
    "type": "object",
    "properties": {
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        },
        "format": {
            "type": "string",
            "enum": ["bar", "dashboard"],
            "description": "bar (по умолчанию) — короткая строка §07. dashboard — визуальная сводка в code fence."
        }
    }
}

query_metrics_schema = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "enum": ["weight", "fat", "muscle_mass", "visceral_fat", "ffm", "bmi"],
            "description": "Название метрики"
        },
        "window_days": {
            "type": "integer",
            "description": "Окно в днях, по умолчанию 90"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": ["metric"]
}

query_food_schema = {
    "type": "object",
    "properties": {
        "start_date": {
            "type": "string",
            "description": "ISO date начало периода"
        },
        "end_date": {
            "type": "string",
            "description": "ISO date конец периода"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": ["start_date", "end_date"]
}

explain_target_schema = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "ISO date для объяснения цели, по умолчанию сегодня"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    }
}

get_progress_schema = {
    "type": "object",
    "properties": {
        "milestone_name": {
            "type": "string",
            "description": "Название вехи"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": ["milestone_name"]
}

get_weekly_summary_schema = {
    "type": "object",
    "properties": {
        "end_date": {
            "type": "string",
            "description": "ISO date — последний день недели, по умолчанию сегодня"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    }
}

get_evening_report_schema = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "Дата YYYY-MM-DD, по умолчанию сегодня"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    }
}

help_schema = {
    "type": "object",
    "properties": {
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    }
}

forecast_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["project", "reach", "calibrate", "plateau"],
            "description": "project — траектория на горизонт; reach — когда будет заданный вес; calibrate — интерактивный подбор калорийности и согласование срока вехи; plateau — комплексный прогноз и статус плато массы тела"
        },
        "horizon_days": {
            "type": "integer",
            "description": "Горизонт прогноза в днях, максимум 182. По умолчанию 84"
        },
        "target_kg": {
            "type": "number",
            "description": "Для reach/calibrate: целевая масса в кг. В calibrate по умолчанию берётся из активной вехи"
        },
        "deadline": {
            "type": "string",
            "description": "Для calibrate: желаемый срок YYYY-MM-DD"
        },
        "intake_kcal": {
            "type": "number",
            "description": "Сценарий «что если есть ровно столько» (для project/reach) или желаемая калорийность (для calibrate)"
        },
        "target_kcal": {
            "type": "number",
            "description": "Для calibrate: желаемая целевая калорийность принудительно (синоним intake_kcal)"
        },
        "apply": {
            "type": "boolean",
            "description": "Для calibrate: согласовать и зафиксировать пересчитанный срок вехи в БД (по умолчанию false)"
        }
    },
    "required": ["action"]
}


register_user_schema = {
    "type": "object",
    "properties": {
        # Ключ личности звонящего. Хендлер читает его как основной идентификатор,
        # в схеме его не было — и строгая проверка параметров это поймала.
        "telegram_user_id": {
            "type": "integer",
            "description": "Telegram user id — по нему находится или заводится профиль"
        },
        "height_cm": {
            "type": "number",
            "description": "Рост в сантиметрах"
        },
        "birth_date": {
            "type": "string",
            "description": "Дата рождения YYYY-MM-DD"
        },
        "sex": {
            "type": "string",
            "enum": ["m", "f"],
            "description": "Пол: m или f"
        },
        "timezone": {
            "type": "string",
            "description": "Часовой пояс (например Europe/Moscow)"
        },
        "base_weight_kg": {
            "type": "number",
            "description": "Базовый вес в килограммах"
        },
        "base_weight_date": {
            "type": "string",
            "description": "Дата базового веса YYYY-MM-DD"
        },
        "health_notes": {
            "type": "string",
            "description": "Ограничения по здоровью со слов человека (см. [ОГРАНИЧЕНИЯ ПО ЗДОРОВЬЮ])"
        },
        "meal_windows": {
            "type": "object",
            "properties": {
                "breakfast": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}},
                "lunch": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}},
                "dinner": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}}
            },
            "description": "Личные окна приёмов пищи (HH:MM), только по прямой просьбе («завтрак с 6 до 9») — можно частично, остальное останется умолчанием"
        },
        "hr_max_bpm": {
            "type": "integer",
            "description": "Личный максимальный пульс, уд/мин (100–230). Требует hr_max_source в этом же вызове"
        },
        "hr_max_source": {
            "type": "string",
            "enum": ["test", "watch"],
            "description": "Источник hr_max_bpm: 'test' — нагрузочный тест, 'watch' — часы/пульсометр"
        }
    },
    "required": []  # height_cm/birth_date/sex обязательны только при создании профиля (хендлер проверяет сам)
}

set_milestone_schema = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Название вехи"
        },
        "metric": {
            "type": "string",
            "enum": ["weight_kg", "ffm_kg", "fat_pct", "waist"],
            "description": "Метрика вехи"
        },
        "threshold": {
            "type": "number",
            "description": "Пороговое значение"
        },
        "deadline": {
            "type": "string",
            "description": "Дата срока YYYY-MM-DD, опционально"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": ["name", "metric", "threshold"]
}

admin_cmd_schema = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Admin command; syntax in Knowledge/admin_commands.md skill"
        }
    },
    "required": ["command"]
}

log_labs_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "list", "delete", "derived"],
            "description": "add (умолчание) — записать показатели на дату сдачи; list — история анализов; delete — удалить по lab_id; derived — расчётные показатели HOMA-IR, eGFR, non-HDL, eAG"
        },
        "taken_on": {
            "type": "string",
            "description": "Дата сдачи анализа YYYY-MM-DD, по умолчанию сегодня"
        },
        "markers": {
            "type": "object",
            "additionalProperties": {"type": "number"},
            "description": "Словарь показателей: название (глюкоза, инсулин, HbA1c, креатинин, общий холестерин, ЛПВП, ЛПНП, триглицериды, АЛТ, АСТ) → числовое значение. Единицы: ммоль/л, мкЕд/мл, %, мкмоль/л, Ед/л. Обязателен для action=add"
        },
        "notes": {
            "type": "string",
            "description": "Свободный комментарий к анализу"
        },
        "lab_id": {
            "type": "integer",
            "description": "ID анализа для action=delete"
        },
        "limit": {
            "type": "integer",
            "description": "Максимум записей для action=list (по умолчанию 30)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

log_watch_day_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "list", "delete"],
            "description": "add (умолчание) — записать день с часов за один или несколько дней; delete — удалить день (date, без него — последнюю запись); list — список"
        },
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Дата YYYY-MM-DD — ровно один календарный день, не диапазон"},
                    "hr_min": {"type": "integer", "description": "Минимальный пульс за день, уд/мин (30-150)"},
                    "hr_avg": {"type": "integer", "description": "Средний пульс за день, уд/мин (35-200)"},
                    "hr_max": {"type": "integer", "description": "Максимальный пульс за день, уд/мин (50-240)"},
                    "steps": {"type": "integer", "description": "Шаги за день (1-100000); нет на скриншоте — поле не передавать"},
                    "active_kcal": {"type": "integer", "description": "Калории активности за день (1-5000); нет на скриншоте — поле не передавать"},
                    "stress_avg": {"type": "integer", "description": "Средний стресс за день по шкале часов (1-100); нет на скриншоте — поле не передавать"},
                    "hrv_ms": {"type": "integer", "description": "Вариабельность пульса (HRV) за день, мс (5-300)"},
                    "spo2_avg": {"type": "integer", "description": "Средний уровень SpO2/кислорода за день, % (70-100)"},
                    "spo2_min": {"type": "integer", "description": "Минимальный уровень SpO2 за день, % (70-100)"},
                    "spo2_max": {"type": "integer", "description": "Максимальный уровень SpO2 за день, % (70-100)"}
                },
                "required": ["date"]
            },
            "description": "Для action=add: по одному набору показателей на КОНКРЕТНЫЙ день из данных часов (скриншот или текст суточной карточки), можно сразу несколько дней за раз (например, за месяц скриншотов). Сводки за неделю/месяц и значения, снятые на глаз с общего графика, НЕ записывать — только то, что часы показали как значение одного дня. В каждом дне нужно хотя бы одно из hr_min/hr_avg/hr_max/steps/active_kcal/stress_avg/hrv_ms/spo2_avg; повторная запись того же дня заменяет ТОЛЬКО присланные показатели — остальные, записанные раньше, не трогает"
        },
        "date": {
            "type": "string",
            "description": "Для action=delete: дата YYYY-MM-DD; без параметра удаляется последняя запись"
        },
        "since_days": {
            "type": "integer",
            "description": "Для action=list: только записи не старше стольких дней"
        },
        "limit": {
            "type": "integer",
            "description": "Для action=list: сколько последних записей вернуть (по умолчанию 30)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

sick_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start", "stop", "status"],
            "description": "start — включить режим болезни; stop — выздоровел; status (умолчание) — состояние"
        },
        "days": {
            "type": "integer",
            "description": "На сколько дней, по умолчанию из настроек"
        },
        "from_date": {
            "type": "string",
            "description": "С какой даты (YYYY-MM-DD), по умолчанию сегодня"
        },
        "note": {
            "type": "string",
            "description": "Что случилось: температура, отравление…"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}

drug_card_draft_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["fetch", "save"],
            "description": "fetch — официальные тексты по МНН (openFDA/ClinicalTrials.gov); save — черновик из этих текстов на одобрение админу"
        },
        "inn": {
            "type": "string",
            "description": "Для action=fetch: МНН препарата латиницей (например tirzepatide)"
        },
        "substance": {
            "type": "string",
            "description": "Для action=save: имя препарата для заголовка карты, как в Knowledge/drug_cards.md (например 'Ретатрутид (Retatrutide)')"
        },
        "fields": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "'зарегистрирован' или 'не зарегистрирован (данные исследований)' — только по тексту источника"},
                "ladder": {"type": "string", "description": "Ступени лестницы титрации через запятую с единицей, например '2, 4, 6, 9, 12 мг'"},
                "min_weeks": {"type": "string", "description": "Минимум недель на ступени, число"},
                "interval_days": {"type": "string", "description": "Интервал приёма в днях"},
                "half_life_days": {"type": "string", "description": "Период полувыведения, например '6 сут'"},
                "tmax_h": {"type": "string", "description": "Пик концентрации, например '48 ч'"},
                "synonyms": {"type": "string", "description": "Прочие названия того же препарата через ' / ', если встретились в источниках"}
            },
            "description": "Только из текстов action=fetch. Отсутствующее в источнике поле не передавай"
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ссылки на источники (url из fetch, plus https://clinicaltrials.gov/study/<NCTId>)"
        },
        "user_id": {
            "type": "integer",
            "description": "ID пользователя"
        }
    },
    "required": []
}
