"""По одному frozen dataclass на таблицу. Транспортные записи, без логики."""
import sqlite3
from dataclasses import dataclass


def row_to(cls, row: sqlite3.Row):
    return cls(**{k: row[k] for k in row.keys()})


@dataclass(frozen=True)
class User:
    id: int
    telegram_user_id: int
    height_cm: float | None
    birth_date: str | None
    sex: str | None
    timezone: str | None
    base_weight_kg: float | None
    base_weight_date: str | None
    created_at: str


@dataclass(frozen=True)
class UserTarget:
    id: int
    user_id: int
    valid_from: str
    protein_g: float | None
    water_ml: float | None
    kcal_floor: float | None


@dataclass(frozen=True)
class Milestone:
    id: int
    user_id: int
    name: str
    metric: str | None
    threshold: float | None
    deadline: str | None
    achieved_at: str | None


@dataclass(frozen=True)
class BodyMetric:
    id: int
    user_id: int
    burst_key: str
    measured_at: str
    weight_kg: float
    fat_pct: float | None
    bmi: float | None
    skeletal_muscle_pct: float | None
    muscle_mass_kg: float | None
    protein_pct: float | None
    device_bmr_kcal: float | None
    ffm_kg: float | None
    subcutaneous_fat_pct: float | None
    visceral_fat: float | None
    water_pct: float | None
    bone_mass_kg: float | None
    metabolic_age: float | None
    device_mac: str | None


@dataclass(frozen=True)
class Anthropometry:
    id: int
    user_id: int
    measured_on: str
    site: str
    value_cm: float


@dataclass(frozen=True)
class FoodLog:
    id: int
    user_id: int
    eaten_at: str
    notes: str | None


@dataclass(frozen=True)
class FoodItem:
    id: int
    food_log_id: int
    name: str | None
    grams: float | None
    kcal: float | None
    protein_g: float | None
    fat_g: float | None
    carbs_g: float | None
    plate_category: str | None


@dataclass(frozen=True)
class WaterLog:
    id: int
    user_id: int
    at: str
    volume_ml: float


@dataclass(frozen=True)
class GlucoseLog:
    id: int
    user_id: int
    at: str
    mmol_l: float
    context: str | None
    confirmed: int | None


@dataclass(frozen=True)
class Activity:
    id: int
    user_id: int
    started_at: str
    duration_min: float | None
    kcal: float | None
    avg_hr: int | None
    sport: str | None
    file_hash: str


@dataclass(frozen=True)
class DailyTarget:
    id: int
    user_id: int
    date: str
    kcal_target: float | None
    protein_g_target: float | None
    water_ml_target: float | None
    computed_from: str | None


@dataclass(frozen=True)
class Alert:
    id: int
    user_id: int
    created_at: str
    rule: str | None
    message: str | None


@dataclass(frozen=True)
class MedLog:
    id: int
    user_id: int
    at: str
    substance: str | None
    dose: str | None


@dataclass(frozen=True)
class LlmCall:
    id: int
    user_id: int | None
    created_at: str
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class ImportLog:
    id: int
    user_id: int
    imported_at: str
    file_hash: str
