"""JSON schemas for health plugin tools. Draft-07, descriptions in Russian."""

log_food_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete"],
            "description": "add (умолчание) — записать приём; delete — удалить ошибочно записанное. Для delete нужен food_log_id"
        },
        "food_log_id": {
            "type": "integer",
            "description": "Для action=delete: номер приёма. Показан в журнале как #N (get_day_summary format=journal)"
        },
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Для action=delete: убрать только эти продукты из приёма (подстрока названия, регистр не важен). Не задан — удаляется приём целиком"
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название блюда"},
                    "grams": {"type": "number", "description": "Вес в граммах, если известен"},
                    "kcal": {"type": "number", "description": "Калорийность, ккал. Обязательно: без неё запись отклоняется. Незнакомое блюдо — дайте оценку"},
                    "protein_g": {"type": "number", "description": "Белки, г. Обязательно"},
                    "fat_g": {"type": "number", "description": "Жиры, г. Обязательно, влияет на LIPID_GUARD"},
                    "carbs_g": {"type": "number", "description": "Углеводы, г. Обязательно"},
                    "fiber_g": {"type": "number", "description": "Клетчатка, г. Не входит в carbs_g и почти не даёт калорий — считается отдельно. Не знаешь — не передавай"},
                    "plate_category": {"type": "string", "description": "Категория тарелки"}
                },
                "required": ["name", "kcal", "protein_g", "fat_g", "carbs_g"]
            },
            "description": "Массив блюд, один item — один приём пищи. Не агрегируйте день целиком: у позиции есть потолок ккал, разбивайте на отдельные блюда"
        },
        "meal_slot": {
            "type": "string",
            "enum": ["breakfast", "lunch", "dinner", "snack"],
            "description": "Передавайте ТОЛЬКО если человек прямо назвал приём: «завтрак ...» → breakfast, «обед ...» → lunch, «ужин ...» → dinner, «перекус ...» → snack. Слово не названо — не передавайте вовсе, код определит приём сам по окнам приёма пищи и времени еды. По часам не угадывать"
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

log_water_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать воду; delete — удалить запись воды (по water_id или всю за день с clear_day=true); list — список записей воды за дату"
        },
        "water_id": {
            "type": "integer",
            "description": "Для action=delete: номер конкретной записи воды (показан в журнале или list как #N)"
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
            "description": "add (умолчание) — записать сон; delete — удалить ошибочную запись или последнюю ночь (без sleep_id); list — список записей сна"
        },
        "sleep_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи сна"
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
            "description": "Субъективная оценка 1-5, где 5 — выспался. Спроси человека, если не сказал"
        },
        "deep_min": {"type": "integer", "description": "Глубокий сон, мин. Только если прибор дал число"},
        "rem_min": {"type": "integer", "description": "REM, мин. Только если прибор дал число"},
        "awake_min": {"type": "integer", "description": "Пробуждения, мин. Только если прибор дал число"},
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
            "description": "add (по умолчанию) — записать тренировку/активность; delete — удалить запись по workout_id; list — посмотреть историю тренировок"
        },
        "workout_id": {
            "type": "integer",
            "description": "Для action=delete: ID тренировки в базе"
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
                   "description": "status — фаза сегодня; schedule — расставить цикл; clear — снять будущие"},
        "start": {"type": "string", "description": "Дата старта цикла YYYY-MM-DD, по умолчанию сегодня"},
        "horizon_weeks": {"type": "integer", "description": "На сколько недель вперёд, по умолчанию 12"},
        "user_id": {"type": "integer", "description": "ID пользователя"}
    },
    "required": ["action"]
}

log_glucose_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать замер; delete — удалить ошибочную запись или последний замер (без glucose_id); list — список записей глюкозы"
        },
        "glucose_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи глюкозы"
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

log_weight_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "delete", "list"],
            "description": "add (умолчание) — записать вес; delete — удалить ошибочную запись или последний вес (без weight_id); list — список записей веса"
        },
        "weight_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи веса"
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
            "description": "add (умолчание) — записать замер; delete — удалить ошибочную запись или последний замер (без anthropometry_id); list — список записей замеров"
        },
        "anthropometry_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи замера"
        },
        "site": {
            "type": "string",
            "enum": ["талия", "грудь", "таз", "бедро", "шея", "бицепс"],
            "description": "Место измерения. Талия/таз обязательны для расчёта Т/Б"
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
            "description": "add (умолчание) — записать приём; delete — удалить ошибочную запись или последний приём (без med_id); list — список записей препаратов"
        },
        "med_id": {
            "type": "integer",
            "description": "Для action=delete: номер записи препарата"
        },
        "drug": {
            "type": "string",
            "description": "Название препарата"
        },
        "dose": {
            "type": "string",
            "description": "Дозировка"
        },
        "route": {
            "type": "string",
            "enum": ["injection", "oral", "topical"],
            "description": "Путь введения: инъекция, перорально или топически"
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
            "enum": ["bar", "dashboard", "journal", "console"],
            "description": "bar — исходный JSON-payload. dashboard (по умолчанию) — сводка КБЖУ дня. journal — детальный журнал питания по приёмам пищи (для «покажи еду»/«что я съел»/«журнал»). console — единый метаболический пульт (для «общий дашборд»/«полная сводка»/«пульт»): тело, тренд веса, бюджет КБЖУ/вода, лог еды, гарды и фарма одним блоком."
        }
    }
}

pharma_schema = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["status", "schedule", "restock", "remove"],
            "description": "status — расписание, следующая доза и остаток («фарма»/«когда колоть»). schedule — задать/обновить препарат, дозу (её рекомендуешь ты), каденцию, следующую дозу, остаток. restock — пополнить остаток доз. remove — убрать препарат из расписания."
        },
        "substance": {"type": "string", "description": "Название препарата (для schedule/restock/remove)"},
        "dose": {
            "type": "number",
            "description": "Разовая доза (число). Рекомендуешь ты, но schedule держит рамки лестницы титрации из карты препарата: доза — её ступень, не выше максимума, повышение только на соседнюю ступень не раньше минимального срока на ступени, а после перерыва в терапии (>14 дней без приёма) — не выше прежней дозы. Выход за рамки — только с by_doctor=true. Без карты/лестницы у препарата — без проверок."
        },
        "by_doctor": {
            "type": "boolean",
            "description": "Доза по назначению врача (человек сказал, что её назначил врач) — снимает рамки лестницы для этой дозы. Без этого флага код отклонит дозу вне рамок явной ошибкой со списком ступеней."
        },
        "unit": {"type": "string", "enum": ["mg", "ml", "IU", "mcg"], "description": "Единица дозы"},
        "route": {"type": "string", "enum": ["injection", "oral", "topical"], "description": "Путь введения"},
        "every_days": {"type": "integer", "description": "Каденция: раз в N дней (7 = еженедельно)"},
        "next_at": {"type": "string", "description": "Следующая доза, ISO дата-время"},
        "stock_doses": {"type": "number", "description": "Остаток в дозах (задать при schedule)"},
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
            "description": "show — недельный план питания и тренировок; set_meal — задать блюдо в шаблон дня; set_workout — задать тренировку в шаблон дня; vs_actual — сравнить план с фактом за дату; remove_meal — удалить блюдо/приём пищи из шаблона; remove_workout — удалить тренировку из шаблона; clear — очистить шаблон (день или всю неделю)."
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
            "description": "list — показать запасы по категориям («холодильник»/«что в холодильнике»); add — добавить/пополнить продукт; remove — списать."
        },
        "name": {"type": "string", "description": "Название продукта (для add/remove)"},
        "qty": {"type": "number", "description": "Количество или вес. Для remove без qty — списать позицию целиком."},
        "unit": {"type": "string", "description": "Единица: г, шт, мл и т.п."},
        "category": {"type": "string", "description": "Категория: Белковые, Овощи/Фрукты, Сложные углеводы, Молочка/Сыры, Прочее."},
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
            "enum": ["project", "reach"],
            "description": "project — траектория на горизонт; reach — когда будет заданный вес"
        },
        "horizon_days": {
            "type": "integer",
            "description": "Горизонт прогноза в днях, максимум 182. По умолчанию 84"
        },
        "target_kg": {
            "type": "number",
            "description": "Для action=reach: до какой массы считать"
        },
        "intake_kcal": {
            "type": "number",
            "description": "Сценарий «что если есть ровно столько». Не задан — берётся фактический лог за 14 дней"
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
            "description": "Личные ограничения по здоровью: травмы, противопоказания, диагнозы — только со слов самого человека"
        },
        "meal_windows": {
            "type": "object",
            "properties": {
                "breakfast": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}},
                "lunch": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}},
                "dinner": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}}
            },
            "description": "Личные окна приёмов пищи (HH:MM), переопределяют умолчание из config.yaml. Задавайте только по прямой просьбе человека («у меня завтрак с 6 до 9») — можно частично, один приём, остальные останутся умолчанием"
        }
    },
    "required": ["height_cm", "birth_date", "sex"]
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
