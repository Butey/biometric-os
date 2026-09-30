#!/usr/bin/env python3
"""
Собирает установочный архив проекта health-agent в архив tar.gz.
Запуск: python scripts/build_release.py
"""

import sys
import sqlite3
import tarfile
import hashlib
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from health_core.db import DB_PATH  # noqa: E402


def get_project_root():
    """Вычислить корень проекта от расположения скрипта."""
    scripts_dir = Path(__file__).parent
    return scripts_dir.parent


def get_archive_path():
    """Путь к выходному архиву."""
    root = get_project_root()
    dist_dir = root / "dist"
    dist_dir.mkdir(exist_ok=True)
    return dist_dir / "health-agent-full.tar.gz"


# Внутри Metrics/ по умолчанию едут только данные, но не картинки: выгрузки
# весов, TCX-треки и расшифровки сна весят 11 МБ и невосстановимы, а 92
# скриншота — 47 МБ и по большей части дубли того, что уже импортировано.
METRICS_DATA_EXT = {".md", ".csv", ".json", ".txt", ".tcx", ".xls", ".xlsx"}


def should_exclude(rel_path_str, with_metrics=False):
    """
    Проверить, должен ли быть исключен файл/каталог по относительному пути.
    rel_path_str: строка пути относительно корня проекта
    with_metrics: True — класть Metrics/ целиком, включая скриншоты
    """
    # Нормализировать разделители
    path_parts = rel_path_str.replace("\\", "/").split("/")

    # Metrics — особый случай: не «всё или ничего», а данные всегда,
    # картинки по флагу. Проверяется до общего списка исключений.
    if path_parts[0] == "Metrics":
        if with_metrics:
            return False
        return Path(rel_path_str).suffix.lower() not in METRICS_DATA_EXT

    # Исключаемые имена в любой части пути
    excluded_dirs = {".venv", "__pycache__", ".git", ".pytest_cache", "dist", ".claude"}

    # Проверить, содержится ли исключаемая директория в пути
    for part in path_parts:
        if part in excluded_dirs:
            return True

    # Исключить .bak файлы
    if ".bak" in rel_path_str:
        return True

    # Исключить .pyc и .log файлы
    if rel_path_str.endswith((".pyc", ".log")):
        return True

    # Исключить БД файлы
    if rel_path_str.endswith((".db", ".db-wal", ".db-shm")) or ".sqlite" in rel_path_str:
        return True

    # Исключить .env* файлы (любые .env-подобные)
    filename = Path(rel_path_str).name
    if filename.startswith(".env") and not rel_path_str.endswith("env.template"):
        return True

    return False


def add_file_filter(tarinfo):
    """
    Фильтр для tar.add(): устанавливает бит исполнения для .sh файлов.
    """
    if tarinfo.name.endswith(".sh"):
        tarinfo.mode |= 0o755
    return tarinfo


def copy_db_to_archive(tar, db_path, archive_name):
    """
    Скопировать БД в архив через VACUUM INTO.
    db_path: Path к исходной БД
    archive_name: имя внутри архива
    Возвращает True если успешно добавлена.
    """
    if not db_path.exists():
        print(f"  Предупреждение: база {db_path} не найдена, пропускаем БД")
        return False

    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
        # NamedTemporaryFile уже создал файл, а VACUUM INTO пишет только в
        # НЕсуществующий путь. Сейчас проходит лишь потому, что файл нулевой
        # длины и SQLite это терпит — на такое полагаться нельзя, удаляем явно.
        tmp_path.unlink(missing_ok=True)

        # Использовать VACUUM INTO для безопасного снимка WAL-базы
        conn = sqlite3.connect(str(db_path))
        conn.execute("VACUUM INTO ?", (str(tmp_path),))
        conn.close()

        # Добавить временный файл в архив с фильтром
        tar.add(str(tmp_path), arcname=archive_name, filter=add_file_filter)

        # Удалить временный файл
        tmp_path.unlink()

        return True

    except Exception as e:
        print(f"  Ошибка при копировании БД: {e}")
        return False


def copy_file_to_archive(tar, src_path, archive_name):
    """
    Скопировать готовый файл в архив. Возвращает True если успешно.
    """
    if not src_path.exists():
        print(f"  Предупреждение: файл {src_path} не найден")
        return False

    try:
        tar.add(str(src_path), arcname=archive_name, filter=add_file_filter)
        return True
    except Exception as e:
        print(f"  Ошибка при добавлении файла {src_path}: {e}")
        return False


def calculate_sha256(file_path):
    """Вычислить SHA256 архива."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def build_release(with_metrics=False):
    """Собрать архив релиза. with_metrics — класть и скриншоты Metrics/."""
    root = get_project_root()
    archive_path = get_archive_path()

    print(f"\nСборка архива...")
    print(f"  Metrics: {'целиком, со скриншотами' if with_metrics else 'только данные, без скриншотов'}")
    print(f"  Корень проекта: {root}")
    print(f"  Выходной архив: {archive_path}\n")

    file_count = 0

    try:
        with tarfile.open(str(archive_path), "w:gz") as tar:
            # 1. Добавить весь каталог проекта под префиксом health_agent_system/
            for item in root.rglob("*"):
                if item.is_file():
                    relative_path = item.relative_to(root)
                    path_str = str(relative_path)

                    # Проверить исключения
                    if should_exclude(path_str, with_metrics):
                        continue

                    # Добавить в архив с префиксом (нормализировать разделители)
                    arcname = "health_agent_system/" + path_str.replace("\\", "/")
                    tar.add(str(item), arcname=arcname, filter=add_file_filter)
                    file_count += 1

            # 2. Добавить снимок БД если она существует
            if copy_db_to_archive(tar, DB_PATH, "data/health.db"):
                file_count += 1

            # 3. Добавить готовый env.template
            if copy_file_to_archive(tar, root.parent / "data" / "env.template", "data/env.template"):
                file_count += 1

            # 4. Добавить готовый RESTORE.md
            if copy_file_to_archive(tar, root.parent / "RESTORE.md", "RESTORE.md"):
                file_count += 1

        # Вычислить статистику
        archive_size_mb = archive_path.stat().st_size / (1024 * 1024)
        sha256 = calculate_sha256(archive_path)

        print(f"Архив собран успешно!")
        print(f"  Файлов в архиве: {file_count}")
        print(f"  Размер архива: {archive_size_mb:.2f} МБ")
        print(f"  SHA256: {sha256}\n")

        return archive_path

    except Exception as e:
        print(f"\nОшибка при сборке архива: {e}")
        raise


def verify_archive(archive_path, with_metrics=False):
    """
    Самопроверка: открыть архив и проверить содержимое.
    Падает с AssertionError если проверки не пройдены.
    """
    print("Проверка архива...")

    file_names = []
    has_health_db = False
    has_install_sh = False
    has_env_file = False
    has_env_template = False
    has_restore_md = False

    try:
        with tarfile.open(str(archive_path), "r:gz") as tar:
            file_names = tar.getnames()

            for name in file_names:
                # Проверка 1: исключаемые в пути
                # Metrics/ теперь допустим, но картинки — только с флагом.
                if "Metrics/" in name and not with_metrics:
                    assert Path(name).suffix.lower() in METRICS_DATA_EXT, (
                        f"скриншот попал в архив без --with-metrics: {name}")
                assert "__pycache__" not in name, f"Найден исключаемый путь __pycache__ в {name}"
                assert ".bak" not in name, f"Найден .bak файл: {name}"
                assert "/dist/" not in name, f"Найден исключаемый путь dist/ в {name}"

                # Проверка 4: не заканчивается на /.env
                assert not name.endswith("/.env"), f"Найден .env файл в подпапке: {name}"

                # Сбор информации
                if name == "data/health.db":
                    has_health_db = True
                if name == "health_agent_system/install.sh":
                    has_install_sh = True
                if name == "data/env.template":
                    has_env_template = True
                if name == "RESTORE.md":
                    has_restore_md = True
                if name.endswith(".env"):
                    has_env_file = True

        # Проверка 2: health.db если база была
        if DB_PATH.exists():
            assert has_health_db, "data/health.db должна быть в архиве если база существует"

        # Проверка 3: install.sh должен быть
        assert has_install_sh, "health_agent_system/install.sh не найден в архиве"

        # env.template/RESTORE.md — только если реально лежат на сервере
        # (copy_file_to_archive молча пропускает отсутствующий файл, не
        # ошибку — без этой проверки установочный архив может выйти без
        # инструкции по восстановлению, никак об этом не сообщив).
        root = get_project_root()
        if (root.parent / "data" / "env.template").exists():
            assert has_env_template, "data/env.template не найден в архиве"
        if (root.parent / "RESTORE.md").exists():
            assert has_restore_md, "RESTORE.md не найден в архиве"

        # Проверка: нет .env файлов вообще
        assert not has_env_file, "В архиве найдены .env файлы"

        print(f"Проверка пройдена! ({len(file_names)} файлов в архиве)\n")

    except AssertionError as e:
        print(f"\nОшибка проверки архива:")
        print(f"  {e}\n")
        raise


def main():
    """Главная функция."""
    try:
        with_metrics = "--with-metrics" in sys.argv
        archive_path = build_release(with_metrics)
        verify_archive(archive_path, with_metrics)
        print("Сборка и проверка завершены успешно!")
    except Exception as e:
        print(f"\nОшибка: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
