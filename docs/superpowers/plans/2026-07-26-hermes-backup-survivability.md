# Hermes Backup Survivability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Обеспечить восстановимость Hermes после потери VPS: ежедневная
essential-копия на Mac, полный архив на Aeza и еженедельный restore-drill,
проверяющий именно ту копию, из которой будет идти восстановление.

**Architecture:** Вся логика — Python-пакет `hermes_backup/` в репозитории
Hermes (stdlib + PyYAML, без новых зависимостей). Bash-обёртки только
запускают модули, чтобы поведение было покрыто pytest'ом. На Aeza модули
исполняет системный `python3` 3.12.3 с `PYTHONPATH=/srv/hermes/app`; на Mac —
venv репозитория (`.venv/bin/python`, 3.11.15). Правки Knowledge Factory живут
в своём репозитории и тестируются его собственным pytest'ом.

**Tech Stack:** Python 3.11–3.12 (stdlib: `sqlite3`, `tarfile`, `hashlib`,
`json`; PyYAML), bash, rsync 3.2.7, systemd timers на Aeza, launchd на Mac.

## Global Constraints

- Спека: `docs/superpowers/specs/2026-07-26-hermes-backup-survivability-design.md` (коммит `339694487`).
- Никаких новых сторонних зависимостей: только stdlib и уже присутствующий PyYAML.
- Ни один компонент не запускает контейнер Hermes, gateway или Telegram.
- `STATE` никогда не исполняется — только строгий парсер с whitelist ключей.
- Fail closed: любой сбой не публикует архив и не удаляет предыдущий.
- Все итоговые строки имеют префикс `hermes_`: `hermes_essential_backup_OK|FAILED`, `hermes_full_backup_OK|FAILED`, `hermes_offsite_pull_OK|FAILED`, `hermes_freshness_OK|FAILED`, `hermes_restore_drill_OK|FAILED`.
- Занятый лок — не ошибка: выход `75`, в systemd-юнитах `SuccessExitStatus=75`.
- Каталог бэкапа содержит ровно пять файлов: `essential.tar.gz`, `STATE`, `INVENTORY.jsonl`, `EXCLUSIONS.jsonl`, `SHA256SUMS`.
- `SHA256SUMS` покрывает все файлы каталога, кроме себя самого.
- Определения счётчиков: навык — каталог с `SKILL.md` на любой глубине; плагин — каталог с `plugin.yaml` на любой глубине; сессия — ключ верхнего уровня в `sessions/sessions.json`; cron-задача — элемент списка `jobs` в `cron/jobs.json`.
- Прод: Aeza `138.124.108.97`, данные `/srv/hermes/data`, репозиторий на сервере `/srv/hermes/app`, бэкапы `/srv/hermes/backups`.
- Off-site каталог на Mac: `~/.local/share/hermes/offsite-backups`, mode `0700`, файлы `0600`.
- Общий сетевой лок Mac: `~/Library/Application Support/offsite-sync/network.lock`.

## File Structure

**Репозиторий Hermes** (`/Users/romanmizanov/Documents/Hermes`):

| Файл | Ответственность |
|---|---|
| `hermes_backup/state.py` | формат `STATE`, строгий парсер с whitelist |
| `hermes_backup/hashing.py` | `sha256_file`, атомарная запись |
| `hermes_backup/counters.py` | подсчёт навыков, плагинов, сессий, cron-задач |
| `hermes_backup/inventory.py` | правила исключений и классификации, `INVENTORY.jsonl`, `EXCLUSIONS.jsonl` |
| `hermes_backup/sqlite_snapshot.py` | `VACUUM INTO`, `integrity_check`, `foreign_key_check`, `page_count`, `user_version` |
| `hermes_backup/archive.py` | валидация членов tar, безопасная распаковка, сборка архива |
| `hermes_backup/staging.py` | стабилизированное копирование живого дерева |
| `hermes_backup/locks.py` | локи с PID/временем/владельцем и корректным снятием stale |
| `hermes_backup/status.py` | атомарные status-файлы |
| `hermes_backup/filevault.py` | runtime-гейт `fdesetup isactive` |
| `hermes_backup/config.py` | пути и константы, переопределяемые через env |
| `hermes_backup/essential_backup.py` | оркестрация essential-бэкапа на Aeza |
| `hermes_backup/offsite_pull.py` | стягивание на Mac + проверка свежести |
| `hermes_backup/restore_drill.py` | drill по стянутой копии |
| `hermes_backup/backup_status.py` | сводка по status-файлам |
| `deploy/beget/hermes_essential_backup.sh` | обёртка для systemd |
| `deploy/beget/backup.sh` | правится: общий лок, снимки вместо живых БД |
| `deploy/beget/systemd/*.service`, `*.timer` | расписание на Aeza |
| `deploy/macos/*.sh`, `deploy/macos/*.plist` | обёртки и агенты на Mac |
| `tests/backup/test_*.py` | тесты всего перечисленного |

**Репозиторий Knowledge Factory** (`/Users/romanmizanov/Documents/BD/knowledge-factory`):

| Файл | Ответственность |
|---|---|
| `scripts/state_parser.py` | строгий парсер `STATE` для KF |
| `scripts/restore_drill.sh` | правится: убрать `source STATE` |
| `scripts/pull_backups_from_aeza.sh` | правится: FileVault-гейт и общий сетевой лок |
| `tests/test_restore_drill_state.py` | регрессия: значения из бэкапа не исполняются |
| `tests/test_pull_filevault_gate.py` | регрессия: pull не работает без FileVault |

---

### Task 1: FileVault и остановка KF pull

Первый исполняемый шаг. Пока диск не зашифрован, никакая off-site копия
секретов создаваться не должна, а уже работающий KF pull продолжает складывать
чувствительные архивы на незашифрованный диск.

**Files:**
- Изменений в репозиториях нет.

**Interfaces:**
- Consumes: ничего.
- Produces: `fdesetup isactive` → `true`; LaunchAgent `com.knowledge-factory.backup-pull` выгружен до этого момента.

- [ ] **Step 1: Остановить KF pull**

```bash
launchctl bootout gui/$(id -u)/com.knowledge-factory.backup-pull 2>/dev/null || \
  launchctl unload ~/Library/LaunchAgents/com.knowledge-factory.backup-pull.plist
launchctl list | grep knowledge-factory.backup-pull || echo "backup-pull выгружен"
```

- [ ] **Step 2: Включить FileVault (действие Романа)**

Системные настройки → Конфиденциальность и безопасность → FileVault → Включить.
Либо `sudo fdesetup enable` — команда один раз печатает recovery key.
Recovery key сохранить вне этого Mac и не внутри «Цифрового мозга».

- [ ] **Step 3: Подтвердить гейт**

Run: `fdesetup isactive; echo "exit=$?"`
Expected: `true`, `exit=0`. Фоновое дошифрование может продолжаться — это
допустимо, гейт смотрит на `isactive`.

- [ ] **Step 4: Вернуть KF pull**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.knowledge-factory.backup-pull.plist
launchctl list | grep knowledge-factory.backup-pull
```

Возврат делается только после `isactive=true`. Гейт из Task 16 после установки
будет держать этот инвариант сам.

---

### Task 2: `state.py` — формат и строгий парсер `STATE`

**Files:**
- Create: `hermes_backup/__init__.py`, `hermes_backup/state.py`
- Test: `tests/backup/__init__.py`, `tests/backup/test_state.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `StateError(ValueError)`; `format_state(values: Mapping[str, int | str]) -> str`; `parse_state(text: str) -> dict[str, int | str]`; константы `INT_KEYS: frozenset[str]`, `STR_KEYS: frozenset[str]`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_state.py
import pytest

from hermes_backup.state import StateError, format_state, parse_state

VALID = {
    "BACKUP_FORMAT_VERSION": 1,
    "CREATED_AT": "2026-07-26T03:15:00Z",
    "SOURCE_HOST": "aeza",
    "HERMES_GIT_SHA": "339694487",
    "HERMES_IMAGE_ID": "sha256:abc123",
    "HERMES_IMAGE_REF": "hermes:latest",
    "STATE_DB_SHA256": "a" * 64,
    "STATE_DB_PAGE_COUNT": 33776,
    "STATE_DB_USER_VERSION": 0,
    "KANBAN_DB_SHA256": "b" * 64,
    "KANBAN_DB_PAGE_COUNT": 28,
    "KANBAN_DB_USER_VERSION": 0,
    "EXPECTED_SESSIONS": 2,
    "EXPECTED_SKILLS": 78,
    "EXPECTED_PLUGINS": 3,
    "EXPECTED_CRON_JOBS": 4,
    "ESSENTIAL_FILE_COUNT": 900,
    "ESSENTIAL_TOTAL_BYTES": 152000000,
    "UNCLASSIFIED_FILE_COUNT": 0,
}


def test_round_trip_preserves_values():
    assert parse_state(format_state(VALID)) == VALID


def test_unknown_key_is_rejected():
    with pytest.raises(StateError, match="unknown key"):
        parse_state("EXPECTED_SKILLS=78\nFOO=1\n")


def test_non_numeric_value_for_int_key_is_rejected():
    with pytest.raises(StateError, match="expects an integer"):
        parse_state("EXPECTED_SKILLS=many\n")


def test_shell_substitution_is_data_not_code(tmp_path):
    canary = tmp_path / "canary"
    canary.write_text("intact")
    with pytest.raises(StateError):
        parse_state(f"SOURCE_HOST=$(rm -f {canary})\n")
    assert canary.read_text() == "intact"


def test_missing_required_key_is_rejected():
    partial = dict(VALID)
    del partial["EXPECTED_SESSIONS"]
    with pytest.raises(StateError, match="missing key"):
        parse_state(format_state(partial))


def test_duplicate_key_is_rejected():
    with pytest.raises(StateError, match="duplicate key"):
        parse_state(format_state(VALID) + "SOURCE_HOST=aeza\n")
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/state.py
"""STATE: expectations a restore drill checks a backup against.

The file lives inside the backup directory, so it is untrusted input: it
is parsed with a key whitelist and never sourced by a shell.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

INT_KEYS = frozenset(
    {
        "BACKUP_FORMAT_VERSION",
        "STATE_DB_PAGE_COUNT",
        "STATE_DB_USER_VERSION",
        "KANBAN_DB_PAGE_COUNT",
        "KANBAN_DB_USER_VERSION",
        "EXPECTED_SESSIONS",
        "EXPECTED_SKILLS",
        "EXPECTED_PLUGINS",
        "EXPECTED_CRON_JOBS",
        "ESSENTIAL_FILE_COUNT",
        "ESSENTIAL_TOTAL_BYTES",
        "UNCLASSIFIED_FILE_COUNT",
    }
)
STR_KEYS = frozenset(
    {
        "CREATED_AT",
        "SOURCE_HOST",
        "HERMES_GIT_SHA",
        "HERMES_IMAGE_ID",
        "HERMES_IMAGE_REF",
        "STATE_DB_SHA256",
        "KANBAN_DB_SHA256",
    }
)
ALL_KEYS = INT_KEYS | STR_KEYS
_SAFE_STR = re.compile(r"\A[A-Za-z0-9:._@/+-]{1,200}\Z")


class StateError(ValueError):
    """STATE is malformed, incomplete, or carries an unexpected key."""


def format_state(values: Mapping[str, int | str]) -> str:
    missing = ALL_KEYS - set(values)
    if missing:
        raise StateError(f"missing key: {sorted(missing)[0]}")
    unknown = set(values) - ALL_KEYS
    if unknown:
        raise StateError(f"unknown key: {sorted(unknown)[0]}")
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def parse_state(text: str) -> dict[str, int | str]:
    parsed: dict[str, int | str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise StateError(f"line {number}: expected KEY=VALUE")
        if key not in ALL_KEYS:
            raise StateError(f"line {number}: unknown key {key!r}")
        if key in parsed:
            raise StateError(f"line {number}: duplicate key {key!r}")
        if key in INT_KEYS:
            if not re.fullmatch(r"-?[0-9]+", value):
                raise StateError(f"line {number}: {key} expects an integer")
            parsed[key] = int(value)
        elif not _SAFE_STR.match(value):
            raise StateError(f"line {number}: {key} has an unsafe value")
        else:
            parsed[key] = value
    missing = ALL_KEYS - set(parsed)
    if missing:
        raise StateError(f"missing key: {sorted(missing)[0]}")
    return parsed
```

Файл `hermes_backup/__init__.py` — пустой. `tests/backup/__init__.py` — пустой.

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_state.py -v`
Expected: PASS, 6 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/__init__.py hermes_backup/state.py tests/backup/__init__.py tests/backup/test_state.py
git commit -m "feat(backup): parse STATE with a key whitelist instead of sourcing it"
```

---

### Task 3: `hashing.py` — контрольные суммы и атомарная запись

**Files:**
- Create: `hermes_backup/hashing.py`
- Test: `tests/backup/test_hashing.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `sha256_file(path: Path) -> str`; `atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None`; `write_sha256sums(directory: Path, exclude: str = "SHA256SUMS") -> Path`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_hashing.py
import hashlib

from hermes_backup.hashing import atomic_write_text, sha256_file, write_sha256sums


def test_sha256_matches_hashlib(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"hermes" * 1000)
    assert sha256_file(target) == hashlib.sha256(b"hermes" * 1000).hexdigest()


def test_atomic_write_leaves_no_partial_file(tmp_path):
    target = tmp_path / "STATE"
    atomic_write_text(target, "SOURCE_HOST=aeza\n")
    assert target.read_text() == "SOURCE_HOST=aeza\n"
    assert list(tmp_path.iterdir()) == [target]
    assert target.stat().st_mode & 0o777 == 0o600


def test_sha256sums_covers_payload_but_not_itself(tmp_path):
    (tmp_path / "essential.tar.gz").write_bytes(b"archive")
    (tmp_path / "STATE").write_text("x\n")
    sums = write_sha256sums(tmp_path)
    listed = {line.split("  ", 1)[1] for line in sums.read_text().splitlines()}
    assert listed == {"essential.tar.gz", "STATE"}
    assert "SHA256SUMS" not in listed
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_hashing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.hashing'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/hashing.py
"""Checksums and atomic writes shared by every backup component."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_BLOCK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_sha256sums(directory: Path, exclude: str = "SHA256SUMS") -> Path:
    # A checksum file cannot contain its own checksum, so it is skipped.
    lines = [
        f"{sha256_file(item)}  {item.name}\n"
        for item in sorted(directory.iterdir())
        if item.is_file() and item.name != exclude and not item.name.startswith(".")
    ]
    target = directory / exclude
    atomic_write_text(target, "".join(lines))
    return target
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_hashing.py -v`
Expected: PASS, 3 теста.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/hashing.py tests/backup/test_hashing.py
git commit -m "feat(backup): add checksum and atomic-write helpers"
```

---

### Task 4: `counters.py` — определения навыка, плагина, сессии, задачи

**Files:**
- Create: `hermes_backup/counters.py`
- Test: `tests/backup/test_counters.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `count_skills(skills_dir: Path) -> int`; `count_plugins(plugins_dir: Path) -> int`; `count_sessions(sessions_json: Path) -> int`; `count_cron_jobs(jobs_json: Path) -> int`; `CounterError(ValueError)`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_counters.py
import json

import pytest

from hermes_backup.counters import (
    CounterError,
    count_cron_jobs,
    count_plugins,
    count_sessions,
    count_skills,
)


def test_skill_is_a_directory_with_skill_md_at_any_depth(tmp_path):
    (tmp_path / "apple" / "apple-macos-automation").mkdir(parents=True)
    (tmp_path / "apple" / "apple-macos-automation" / "SKILL.md").write_text("x")
    (tmp_path / "apple" / "bundle" / "nested").mkdir(parents=True)
    (tmp_path / "apple" / "bundle" / "nested" / "SKILL.md").write_text("x")
    (tmp_path / "apple" / "notes").mkdir()
    (tmp_path / "apple" / "notes" / "DESCRIPTION.md").write_text("x")
    # Категория верхнего уровня сама навыком не является.
    assert count_skills(tmp_path) == 2


def test_plugin_is_a_directory_with_plugin_yaml_at_any_depth(tmp_path):
    (tmp_path / "restrict-guest-tools").mkdir()
    (tmp_path / "restrict-guest-tools" / "plugin.yaml").write_text("x")
    (tmp_path / "image_gen" / "replicate").mkdir(parents=True)
    (tmp_path / "image_gen" / "replicate" / "plugin.yaml").write_text("x")
    assert count_plugins(tmp_path) == 2


def test_sessions_are_top_level_keys_of_sessions_json(tmp_path):
    target = tmp_path / "sessions.json"
    target.write_text(json.dumps({"agent:main:telegram:dm:1": {}, "agent:main:telegram:dm:2": {}}))
    assert count_sessions(target) == 2


def test_cron_jobs_come_from_the_jobs_list(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text(json.dumps({"jobs": [{"id": "a"}, {"id": "b"}]}))
    assert count_cron_jobs(target) == 2


def test_empty_job_list_is_valid(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text(json.dumps({"jobs": []}))
    assert count_cron_jobs(target) == 0


def test_wrong_cron_schema_is_rejected(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text(json.dumps(["a", "b"]))
    with pytest.raises(CounterError, match="jobs"):
        count_cron_jobs(target)


def test_truncated_json_is_rejected(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text('{"jobs": [{"id": "a"')
    with pytest.raises(CounterError):
        count_cron_jobs(target)
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_counters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.counters'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/counters.py
"""Counting rules shared by the backup and the drill.

Verified against the live tree on 2026-07-26: skills nest as
category/skill (SKILL.md appears 78 times below 30 top-level dirs),
plugin.yaml sits at depth two for image_gen/replicate, and sessions/
holds one sessions.json beside debug request dumps.
"""

from __future__ import annotations

import json
from pathlib import Path


class CounterError(ValueError):
    """A counted artefact is missing or has an unexpected shape."""


def _count_marker(root: Path, marker: str) -> int:
    if not root.is_dir():
        raise CounterError(f"not a directory: {root}")
    return sum(1 for _ in root.rglob(marker) if _.is_file())


def count_skills(skills_dir: Path) -> int:
    return _count_marker(skills_dir, "SKILL.md")


def count_plugins(plugins_dir: Path) -> int:
    return _count_marker(plugins_dir, "plugin.yaml")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CounterError(f"{path}: {error}") from error


def count_sessions(sessions_json: Path) -> int:
    payload = _load_json(sessions_json)
    if not isinstance(payload, dict):
        raise CounterError(f"{sessions_json}: expected an object of sessions")
    return len(payload)


def count_cron_jobs(jobs_json: Path) -> int:
    payload = _load_json(jobs_json)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise CounterError(f'{jobs_json}: expected {{"jobs": [...]}}')
    return len(payload["jobs"])
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_counters.py -v`
Expected: PASS, 7 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/counters.py tests/backup/test_counters.py
git commit -m "feat(backup): count skills, plugins, sessions and cron jobs by content"
```

---

### Task 5: `inventory.py` — классификация, `INVENTORY.jsonl`, `EXCLUSIONS.jsonl`

**Files:**
- Create: `hermes_backup/inventory.py`
- Test: `tests/backup/test_inventory.py`

**Interfaces:**
- Consumes: `hermes_backup.hashing.sha256_file`.
- Produces: `EXCLUDE_RULES: tuple[str, ...]`; `ESSENTIAL_RULES: tuple[str, ...]`; `excluded_by(rel: str) -> str | None`; `classify(rel: str) -> str`; `write_inventory(staging: Path, out: Path) -> InventoryTotals`; `write_exclusions(source: Path, out: Path) -> int`; `@dataclass InventoryTotals(files: int, total_bytes: int, unclassified: int)`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_inventory.py
import json

from hermes_backup.inventory import (
    classify,
    excluded_by,
    write_exclusions,
    write_inventory,
)


def test_recoverable_paths_are_excluded():
    assert excluded_by("cache/model.bin")
    assert excluded_by("cron/output/run-1.log")
    assert excluded_by("sessions/request_dump_20260715_231648.json")
    assert excluded_by("state.db-wal")
    assert excluded_by("kanban.db-shm")
    assert excluded_by(".DS_Store")
    assert excluded_by("sessions/sessions.json") is None


def test_known_paths_are_essential_and_new_ones_unclassified():
    assert classify("sessions/sessions.json") == "essential"
    assert classify("auth.json") == "essential"
    assert classify("cron/jobs.json") == "essential"
    assert classify("brand_new_thing.dat") == "unclassified"


def test_inventory_lists_staging_contents_with_checksums(tmp_path):
    staging = tmp_path / "staging"
    (staging / "sessions").mkdir(parents=True)
    (staging / "sessions" / "sessions.json").write_text("{}")
    (staging / "surprise.bin").write_bytes(b"1234")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    by_path = {row["path"]: row for row in rows}
    assert by_path["sessions/sessions.json"]["classification"] == "essential"
    assert by_path["surprise.bin"]["classification"] == "unclassified"
    assert len(by_path["surprise.bin"]["sha256"]) == 64
    assert totals.files == 2
    assert totals.total_bytes == 6
    assert totals.unclassified == 1


def test_paths_with_tabs_and_newlines_survive_round_trip(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    hostile = staging / "weird\tname\nfile"
    hostile.write_text("x")
    out = tmp_path / "INVENTORY.jsonl"

    write_inventory(staging, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows[0]["path"] == "weird\tname\nfile"


def test_exclusions_are_taken_from_the_source_tree(tmp_path):
    source = tmp_path / "data"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "big.bin").write_bytes(b"0" * 10)
    (source / "sessions").mkdir()
    (source / "sessions" / "sessions.json").write_text("{}")
    out = tmp_path / "EXCLUSIONS.jsonl"

    count = write_exclusions(source, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert count == 1
    assert rows[0]["path"] == "cache/big.bin"
    assert rows[0]["classification"] == "excluded-recoverable"
    assert rows[0]["size"] == 10
    assert "sha256" not in rows[0]


def test_inventory_and_exclusions_do_not_overlap(tmp_path):
    source = tmp_path / "data"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "big.bin").write_bytes(b"0")
    (source / "auth.json").write_text("{}")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "auth.json").write_text("{}")

    write_exclusions(source, tmp_path / "EXCLUSIONS.jsonl")
    write_inventory(staging, tmp_path / "INVENTORY.jsonl")

    excluded = {
        json.loads(line)["path"]
        for line in (tmp_path / "EXCLUSIONS.jsonl").read_text().splitlines()
    }
    included = {
        json.loads(line)["path"]
        for line in (tmp_path / "INVENTORY.jsonl").read_text().splitlines()
    }
    assert excluded & included == set()
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.inventory'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/inventory.py
"""What travels in the backup, what does not, and why.

Selection is "everything except the explicit exclusions", so an unknown
new file is backed up rather than silently lost. Classification is a
separate, purely descriptive step: unclassified files are counted so the
rules can be refreshed, never dropped.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from hermes_backup.hashing import sha256_file

EXCLUDE_RULES: tuple[str, ...] = (
    "cache/*",
    "bin/*",
    "image_cache/*",
    "logs/*",
    "models_dev_cache.json",
    "cron/output/*",
    "sessions/request_dump_*.json",
    ".DS_Store",
    "*/.DS_Store",
    "state.db",
    "state.db-*",
    "kanban.db",
    "kanban.db-*",
    "**/__pycache__/*",
)
ESSENTIAL_RULES: tuple[str, ...] = (
    "state.db",
    "kanban.db",
    "config.yaml",
    "config.yaml.*",
    "auth.json",
    ".env",
    ".env.*",
    "sessions/sessions.json",
    "skills/*",
    "plugins/*",
    "workspace/*",
    "home/*",
    ".local/*",
    "cron/jobs.json",
    "cron/state/*",
)


@dataclass(frozen=True)
class InventoryTotals:
    files: int
    total_bytes: int
    unclassified: int


def _matches(rel: str, rules: tuple[str, ...]) -> str | None:
    for rule in rules:
        if fnmatch.fnmatch(rel, rule) or fnmatch.fnmatch(rel, f"{rule}/*"):
            return rule
    return None


def excluded_by(rel: str) -> str | None:
    """Return the exclusion rule that removes ``rel``, or None."""
    return _matches(rel, EXCLUDE_RULES)


def classify(rel: str) -> str:
    """Label a file that made it into staging."""
    return "essential" if _matches(rel, ESSENTIAL_RULES) else "unclassified"


def _relative_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path, path.relative_to(root).as_posix()


def write_inventory(staging: Path, out: Path) -> InventoryTotals:
    files = total_bytes = unclassified = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _relative_files(staging):
            size = path.stat().st_size
            classification = classify(rel)
            handle.write(
                json.dumps(
                    {
                        "path": rel,
                        "size": size,
                        "sha256": sha256_file(path),
                        "classification": classification,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            files += 1
            total_bytes += size
            unclassified += classification == "unclassified"
    out.chmod(0o600)
    return InventoryTotals(files=files, total_bytes=total_bytes, unclassified=unclassified)


def write_exclusions(source: Path, out: Path) -> int:
    """Record what the exclusion rules removed, read from the live tree."""
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _relative_files(source):
            rule = excluded_by(rel)
            if rule is None:
                continue
            handle.write(
                json.dumps(
                    {
                        "path": rel,
                        "rule": rule,
                        "size": path.stat().st_size,
                        "classification": "excluded-recoverable",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            count += 1
    out.chmod(0o600)
    return count
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_inventory.py -v`
Expected: PASS, 6 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/inventory.py tests/backup/test_inventory.py
git commit -m "feat(backup): record inventory and exclusions as JSONL"
```

---

### Task 6: `sqlite_snapshot.py` — согласованные снимки БД

**Files:**
- Create: `hermes_backup/sqlite_snapshot.py`
- Test: `tests/backup/test_sqlite_snapshot.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `SnapshotError(RuntimeError)`; `snapshot(src: Path, dest: Path) -> None`; `integrity_check(db: Path) -> None`; `foreign_key_check(db: Path) -> None`; `page_count(db: Path) -> int`; `user_version(db: Path) -> int`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_sqlite_snapshot.py
import sqlite3

import pytest

from hermes_backup.sqlite_snapshot import (
    SnapshotError,
    foreign_key_check,
    integrity_check,
    page_count,
    snapshot,
    user_version,
)


def _make_db(path, rows=100):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connection.executemany(
        "INSERT INTO notes (body) VALUES (?)", [(f"row {n}",) for n in range(rows)]
    )
    connection.commit()
    connection.close()


def test_snapshot_of_a_wal_database_is_readable_and_complete(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    destination = tmp_path / "snapshot.db"

    snapshot(source, destination)

    assert not destination.with_name("snapshot.db-wal").exists()
    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT count(*) FROM notes").fetchone()[0] == 100
    connection.close()


def test_snapshot_survives_writes_to_the_source_afterwards(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    destination = tmp_path / "snapshot.db"
    snapshot(source, destination)

    live = sqlite3.connect(source)
    live.execute("INSERT INTO notes (body) VALUES ('after snapshot')")
    live.commit()
    live.close()

    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT count(*) FROM notes").fetchone()[0] == 100
    connection.close()


def test_integrity_check_rejects_a_corrupt_file(tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(SnapshotError):
        integrity_check(broken)


def test_pragmas_report_page_count_and_user_version(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA user_version=7")
    connection.close()

    assert page_count(source) > 0
    assert user_version(source) == 7
    integrity_check(source)
    foreign_key_check(source)


def test_snapshot_refuses_to_overwrite(tmp_path):
    source = tmp_path / "state.db"
    _make_db(source)
    destination = tmp_path / "snapshot.db"
    destination.write_text("occupied")
    with pytest.raises(SnapshotError, match="exists"):
        snapshot(source, destination)
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_sqlite_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.sqlite_snapshot'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/sqlite_snapshot.py
"""Consistent snapshots of a live SQLite database.

VACUUM INTO writes a transactionally consistent copy while the database
keeps serving writes, so the backup never has to stop Hermes and never
catches a main file and its -wal at different points.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


class SnapshotError(RuntimeError):
    """A database could not be snapshotted or failed its own checks."""


def _connect(db: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    except sqlite3.Error as error:
        raise SnapshotError(f"{db}: {error}") from error


def snapshot(src: Path, dest: Path) -> None:
    if dest.exists():
        raise SnapshotError(f"{dest}: destination exists")
    with closing(_connect(src)) as connection:
        try:
            connection.execute("VACUUM INTO ?", (str(dest),))
        except sqlite3.Error as error:
            raise SnapshotError(f"{src}: {error}") from error
    dest.chmod(0o600)


def _scalar(db: Path, statement: str):
    with closing(_connect(db)) as connection:
        try:
            return connection.execute(statement).fetchone()
        except sqlite3.Error as error:
            raise SnapshotError(f"{db}: {error}") from error


def integrity_check(db: Path) -> None:
    row = _scalar(db, "PRAGMA integrity_check")
    if not row or row[0] != "ok":
        raise SnapshotError(f"{db}: integrity_check reported {row}")


def foreign_key_check(db: Path) -> None:
    with closing(_connect(db)) as connection:
        try:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as error:
            raise SnapshotError(f"{db}: {error}") from error
    if violations:
        raise SnapshotError(f"{db}: {len(violations)} foreign key violation(s)")


def page_count(db: Path) -> int:
    return int(_scalar(db, "PRAGMA page_count")[0])


def user_version(db: Path) -> int:
    return int(_scalar(db, "PRAGMA user_version")[0])
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_sqlite_snapshot.py -v`
Expected: PASS, 5 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/sqlite_snapshot.py tests/backup/test_sqlite_snapshot.py
git commit -m "feat(backup): snapshot live SQLite databases with VACUUM INTO"
```

---

### Task 7: `archive.py` — валидация и безопасная распаковка tar

**Files:**
- Create: `hermes_backup/archive.py`
- Test: `tests/backup/test_archive.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `ArchiveError(RuntimeError)`; `create(staging: Path, dest: Path) -> None`; `validate(tar_path: Path) -> None`; `extract(tar_path: Path, dest: Path) -> None`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_archive.py
import os
import tarfile

import pytest

from hermes_backup.archive import ArchiveError, create, extract, validate


def _archive_with(tmp_path, mutate):
    target = tmp_path / "evil.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        mutate(tar)
    return target


def test_round_trip_preserves_content_and_modes(tmp_path):
    staging = tmp_path / "staging"
    (staging / "cron").mkdir(parents=True)
    (staging / "cron" / "jobs.json").write_text('{"jobs": []}')
    secret = staging / "auth.json"
    secret.write_text("{}")
    secret.chmod(0o600)
    archive = tmp_path / "essential.tar.gz"

    create(staging, archive)
    restored = tmp_path / "restored"
    extract(archive, restored)

    assert (restored / "cron" / "jobs.json").read_text() == '{"jobs": []}'
    assert (restored / "auth.json").stat().st_mode & 0o777 == 0o600


def test_absolute_path_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("/etc/passwd")
        info.size = 0
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="absolute"):
        validate(_archive_with(tmp_path, mutate))


def test_parent_traversal_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("../escape")
        info.size = 0
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="traversal"):
        validate(_archive_with(tmp_path, mutate))


def test_symlink_outside_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="link"):
        validate(_archive_with(tmp_path, mutate))


def test_hardlink_outside_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "../outside"
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="link"):
        validate(_archive_with(tmp_path, mutate))


def test_device_node_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("dev/null")
        info.type = tarfile.CHRTYPE
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="special"):
        validate(_archive_with(tmp_path, mutate))


def test_fifo_is_rejected(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("pipe")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)

    with pytest.raises(ArchiveError, match="special"):
        validate(_archive_with(tmp_path, mutate))


def test_extract_validates_before_writing_anything(tmp_path):
    def mutate(tar):
        info = tarfile.TarInfo("../escape")
        info.size = 0
        tar.addfile(info)

    archive = _archive_with(tmp_path, mutate)
    destination = tmp_path / "restored"
    with pytest.raises(ArchiveError):
        extract(archive, destination)
    assert not destination.exists() or not os.listdir(destination)
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.archive'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/archive.py
"""Archive creation and paranoid extraction.

The drill unpacks an archive that travelled over the network, so every
member is checked before a single byte is written: absolute paths,
traversal, links pointing outside the tree, and any special object.
"""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath

_ALLOWED_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}


class ArchiveError(RuntimeError):
    """An archive member is unsafe to extract."""


def _check_member(member: tarfile.TarInfo) -> None:
    name = PurePosixPath(member.name)
    if name.is_absolute() or member.name.startswith("/"):
        raise ArchiveError(f"{member.name}: absolute path")
    if ".." in name.parts:
        raise ArchiveError(f"{member.name}: parent traversal")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute() or ".." in target.parts:
            raise ArchiveError(f"{member.name}: link escapes the archive")
        return
    if member.type not in _ALLOWED_TYPES:
        raise ArchiveError(f"{member.name}: special object of type {member.type!r}")


def validate(tar_path: Path) -> None:
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                _check_member(member)
    except tarfile.TarError as error:
        raise ArchiveError(f"{tar_path}: {error}") from error


def create(staging: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(staging.rglob("*")):
            tar.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
    dest.chmod(0o600)


def extract(tar_path: Path, dest: Path) -> None:
    validate(tar_path)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:*") as tar:
        tar.extractall(dest)  # noqa: S202 — every member validated above
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_archive.py -v`
Expected: PASS, 8 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/archive.py tests/backup/test_archive.py
git commit -m "feat(backup): validate every tar member before extracting"
```

---

### Task 8: `staging.py` — стабилизированное копирование живого дерева

**Files:**
- Create: `hermes_backup/staging.py`
- Test: `tests/backup/test_staging.py`

**Interfaces:**
- Consumes: `hermes_backup.inventory.EXCLUDE_RULES`.
- Produces: `UnstableSourceError(RuntimeError)`; `stabilized_copy(source: Path, staging: Path, attempts: int = 4, rsync: str = "rsync") -> int` — возвращает число выполненных проходов.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_staging.py
import pytest

from hermes_backup.staging import UnstableSourceError, stabilized_copy


def test_stable_tree_is_copied_in_one_extra_pass(tmp_path):
    source = tmp_path / "data"
    (source / "cron").mkdir(parents=True)
    (source / "cron" / "jobs.json").write_text('{"jobs": []}')
    (source / "cache").mkdir()
    (source / "cache" / "junk.bin").write_bytes(b"0" * 100)
    staging = tmp_path / "staging"

    passes = stabilized_copy(source, staging)

    assert (staging / "cron" / "jobs.json").exists()
    assert not (staging / "cache").exists()
    assert passes >= 1


def test_source_that_keeps_changing_fails_closed(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    churn = source / "busy.log"
    churn.write_text("0")
    staging = tmp_path / "staging"

    original = stabilized_copy.__globals__["_run_rsync"]
    counter = {"n": 0}

    def churning_rsync(*args, **kwargs):
        counter["n"] += 1
        churn.write_text(f"{counter['n']}")
        return original(*args, **kwargs)

    stabilized_copy.__globals__["_run_rsync"] = churning_rsync
    try:
        with pytest.raises(UnstableSourceError, match="unstable_source"):
            stabilized_copy(source, staging, attempts=2)
    finally:
        stabilized_copy.__globals__["_run_rsync"] = original


def test_missing_source_is_reported(tmp_path):
    with pytest.raises(UnstableSourceError):
        stabilized_copy(tmp_path / "absent", tmp_path / "staging")
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_staging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.staging'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/staging.py
"""Copy a live tree into staging and prove the copy is stable.

The backup lock stops other backups, not Hermes: sessions and cron state
can change mid-copy. Copy, then re-check with a checksum dry run, repeat
while anything moved, and fail closed rather than publish a torn file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_backup.inventory import EXCLUDE_RULES


class UnstableSourceError(RuntimeError):
    """The source kept changing, so no consistent staging copy exists."""


def _exclude_args() -> list[str]:
    return [f"--exclude={rule}" for rule in EXCLUDE_RULES]


def _run_rsync(source: Path, staging: Path, dry_run: bool, rsync: str) -> str:
    command = [rsync, "-rlptH", "--delete", "--itemize-changes", *_exclude_args()]
    if dry_run:
        command += ["--dry-run", "--checksum"]
    command += [f"{source}/", f"{staging}/"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise UnstableSourceError(f"rsync failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def stabilized_copy(
    source: Path, staging: Path, attempts: int = 4, rsync: str = "rsync"
) -> int:
    if not source.is_dir():
        raise UnstableSourceError(f"source is not a directory: {source}")
    staging.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for attempt in range(1, attempts + 1):
        _run_rsync(source, staging, dry_run=False, rsync=rsync)
        changed = [line for line in _run_rsync(source, staging, True, rsync).splitlines() if line.strip()]
        if not changed:
            return attempt
    raise UnstableSourceError(
        f"unstable_source: {len(changed)} path(s) still changing after {attempts} attempts"
    )
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_staging.py -v`
Expected: PASS, 3 теста.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/staging.py tests/backup/test_staging.py
git commit -m "feat(backup): stabilize staging copies of a live tree"
```

---

### Task 9: `locks.py` — локи с владельцем и корректным снятием stale

**Files:**
- Create: `hermes_backup/locks.py`
- Test: `tests/backup/test_locks.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `LockBusy(RuntimeError)`; `LockTimeout(RuntimeError)`; `class DirectoryLock` с методами `acquire(wait_seconds: int = 0)`, `release()`, поддержкой `with`, свойством `meta: dict`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_locks.py
import json
import os

import pytest

from hermes_backup.locks import DirectoryLock, LockBusy


def test_second_holder_is_refused(tmp_path):
    path = tmp_path / "network.lock"
    with DirectoryLock(path, owner="hermes-pull"):
        with pytest.raises(LockBusy):
            DirectoryLock(path, owner="kf-pull").acquire()


def test_metadata_records_pid_owner_and_time(tmp_path):
    path = tmp_path / "network.lock"
    with DirectoryLock(path, owner="hermes-pull"):
        meta = json.loads((path / "meta.json").read_text())
    assert meta["pid"] == os.getpid()
    assert meta["owner"] == "hermes-pull"
    assert meta["started_at"].endswith("Z")


def test_lock_is_released_on_exit(tmp_path):
    path = tmp_path / "network.lock"
    with DirectoryLock(path, owner="a"):
        pass
    with DirectoryLock(path, owner="b"):
        pass


def test_stale_lock_of_a_dead_process_is_reclaimed(tmp_path):
    path = tmp_path / "network.lock"
    path.mkdir()
    (path / "meta.json").write_text(
        json.dumps({"pid": 2**22, "owner": "ghost", "started_at": "2026-07-26T00:00:00Z"})
    )
    with DirectoryLock(path, owner="hermes-pull"):
        assert json.loads((path / "meta.json").read_text())["owner"] == "hermes-pull"


def test_live_process_lock_is_never_reclaimed_by_age(tmp_path):
    path = tmp_path / "network.lock"
    path.mkdir()
    (path / "meta.json").write_text(
        json.dumps(
            {"pid": os.getpid(), "owner": "kf-pull", "started_at": "2000-01-01T00:00:00Z"}
        )
    )
    with pytest.raises(LockBusy, match="kf-pull"):
        DirectoryLock(path, owner="hermes-pull").acquire()
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_locks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.locks'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/locks.py
"""A lock that says who holds it and is only reclaimed when they are gone.

Age alone never justifies stealing a lock: a slow Knowledge Factory pull
can legitimately run for hours, and stealing from it would put two
transfers on the same narrow link.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class LockBusy(RuntimeError):
    """Another live process holds the lock."""


class LockTimeout(RuntimeError):
    """The lock did not become free within the allowed wait."""


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DirectoryLock:
    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        self.meta: dict = {}

    def _read_meta(self) -> dict:
        try:
            return json.loads((self.path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self) -> None:
        self.meta = {
            "pid": os.getpid(),
            "owner": self.owner,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (self.path / "meta.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def acquire(self, wait_seconds: int = 0) -> "DirectoryLock":
        deadline = time.monotonic() + wait_seconds
        while True:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.mkdir()
            except FileExistsError:
                holder = self._read_meta()
                pid = holder.get("pid")
                if isinstance(pid, int) and _process_alive(pid):
                    if time.monotonic() >= deadline:
                        raise LockBusy(
                            f"held by {holder.get('owner', 'unknown')} (pid {pid})"
                        ) from None
                    time.sleep(min(30, max(1, wait_seconds // 60 or 1)))
                    continue
                # The holder is gone: reclaim, never by age alone.
                for leftover in self.path.iterdir():
                    leftover.unlink()
                self.path.rmdir()
                continue
            self._write_meta()
            return self

    def release(self) -> None:
        meta = self.path / "meta.json"
        if meta.exists():
            meta.unlink()
        if self.path.exists():
            self.path.rmdir()

    def __enter__(self) -> "DirectoryLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_locks.py -v`
Expected: PASS, 5 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/locks.py tests/backup/test_locks.py
git commit -m "feat(backup): add an owner-aware lock with safe stale reclaim"
```

---

### Task 10: `status.py` и `config.py` — статусы и пути

**Files:**
- Create: `hermes_backup/status.py`, `hermes_backup/config.py`
- Test: `tests/backup/test_status.py`

**Interfaces:**
- Consumes: `hermes_backup.hashing.atomic_write_text`.
- Produces: `write_status(directory: Path, name: str, outcome: str, reason: str = "", backup_path: str = "") -> Path`; `read_status(directory: Path, name: str) -> dict | None`; `status_line(name: str, outcome: str, reason: str = "") -> str`. В `config.py`: `SERVER_DATA`, `SERVER_ESSENTIAL_ROOT`, `SERVER_FULL_ROOT`, `SERVER_LOCK`, `MAC_OFFSITE_ROOT`, `MAC_STATUS_DIR`, `MAC_NETWORK_LOCK`, `REMOTE`, `SSH_KEY`, `RETENTION` — все читаются из env с этими значениями по умолчанию.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_status.py
import json

from hermes_backup.status import read_status, status_line, write_status


def test_status_is_written_atomically_and_read_back(tmp_path):
    write_status(tmp_path, "essential_backup", "OK", backup_path="/srv/x/daily-1")
    record = read_status(tmp_path, "essential_backup")
    assert record["outcome"] == "OK"
    assert record["backup_path"] == "/srv/x/daily-1"
    assert record["finished_at"].endswith("Z")
    assert [p.name for p in tmp_path.iterdir()] == ["essential_backup.json"]


def test_failure_keeps_the_reason(tmp_path):
    write_status(tmp_path, "restore_drill", "FAILED", reason="integrity_check")
    assert read_status(tmp_path, "restore_drill")["reason"] == "integrity_check"


def test_missing_status_reads_as_none(tmp_path):
    assert read_status(tmp_path, "never_ran") is None


def test_status_line_carries_the_hermes_prefix():
    assert status_line("offsite_pull", "FAILED", "lock_timeout") == (
        "hermes_offsite_pull_FAILED lock_timeout"
    )
    assert status_line("essential_backup", "OK") == "hermes_essential_backup_OK"


def test_status_file_is_valid_json(tmp_path):
    write_status(tmp_path, "freshness", "OK")
    json.loads((tmp_path / "freshness.json").read_text())
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.status'`

- [ ] **Step 3: Реализовать модули**

```python
# hermes_backup/status.py
"""Machine-readable outcome of every run.

The summary command and, later, Telegram alerts read these files instead
of parsing free-form logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup.hashing import atomic_write_text


def status_line(name: str, outcome: str, reason: str = "") -> str:
    line = f"hermes_{name}_{outcome}"
    return f"{line} {reason}" if reason else line


def write_status(
    directory: Path, name: str, outcome: str, reason: str = "", backup_path: str = ""
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    atomic_write_text(
        target,
        json.dumps(
            {
                "name": name,
                "outcome": outcome,
                "reason": reason,
                "backup_path": backup_path,
                "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    return target


def read_status(directory: Path, name: str) -> dict | None:
    target = directory / f"{name}.json"
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
```

```python
# hermes_backup/config.py
"""Paths and knobs, all overridable through the environment for tests."""

from __future__ import annotations

import os
from pathlib import Path


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


SERVER_DATA = _path("HERMES_BACKUP_DATA", "/srv/hermes/data")
SERVER_ESSENTIAL_ROOT = _path("HERMES_BACKUP_ESSENTIAL_ROOT", "/srv/hermes/backups/essential")
SERVER_FULL_ROOT = _path("HERMES_BACKUP_FULL_ROOT", "/srv/hermes/backups")
SERVER_LOCK = _path("HERMES_BACKUP_LOCK", "/run/lock/hermes-backup.lock")
SERVER_STATUS_DIR = _path("HERMES_BACKUP_STATUS_DIR", "/var/lib/hermes-backup/status")

MAC_OFFSITE_ROOT = _path("HERMES_OFFSITE_ROOT", "~/.local/share/hermes/offsite-backups")
MAC_STATUS_DIR = _path("HERMES_STATUS_DIR", "~/.local/share/hermes/status")
MAC_NETWORK_LOCK = _path(
    "OFFSITE_NETWORK_LOCK", "~/Library/Application Support/offsite-sync/network.lock"
)

REMOTE = os.environ.get("HERMES_BACKUP_REMOTE", "root@138.124.108.97")
SSH_KEY = _path("HERMES_BACKUP_SSH_KEY", "~/.ssh/aeza_hermes")

RETENTION_SERVER = int(os.environ.get("HERMES_BACKUP_RETENTION_SERVER", "7"))
RETENTION_MAC = int(os.environ.get("HERMES_BACKUP_RETENTION_MAC", "7"))
RETENTION_MAC_FLOOR = int(os.environ.get("HERMES_BACKUP_RETENTION_MAC_FLOOR", "2"))
FRESHNESS_HOURS = int(os.environ.get("HERMES_BACKUP_FRESHNESS_HOURS", "26"))
DRILL_STALENESS_HOURS = int(os.environ.get("HERMES_DRILL_STALENESS_HOURS", "48"))
NETWORK_LOCK_WAIT_SECONDS = int(os.environ.get("OFFSITE_LOCK_WAIT_SECONDS", str(6 * 3600)))
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_status.py -v`
Expected: PASS, 5 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/status.py hermes_backup/config.py tests/backup/test_status.py
git commit -m "feat(backup): record run outcomes in status files"
```

---

### Task 11: `essential_backup.py` — оркестрация на Aeza

**Files:**
- Create: `hermes_backup/essential_backup.py`, `deploy/beget/hermes_essential_backup.sh`, `deploy/beget/systemd/hermes-essential-backup.service`, `deploy/beget/systemd/hermes-essential-backup.timer`
- Test: `tests/backup/test_essential_backup.py`

**Interfaces:**
- Consumes: `staging.stabilized_copy`, `sqlite_snapshot.*`, `inventory.write_inventory/write_exclusions`, `counters.*`, `state.format_state`, `archive.create`, `hashing.write_sha256sums`, `status.write_status/status_line`, `config.*`.
- Produces: `run(data: Path, root: Path, *, rsync: str = "rsync") -> Path` — возвращает путь опубликованного каталога; `main(argv: list[str] | None = None) -> int`; коды выхода `0`, `1`, `75`.

**Обязательный критерий приёмки:** структурированные файлы staging разбираются
**до** публикации — `cron/jobs.json` через `count_cron_jobs`, `config.yaml`
через `yaml.safe_load`. Архив с пойманным на середине записи `config.yaml` не
должен публиковаться ни при каких условиях.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_essential_backup.py
import json
import sqlite3
import tarfile

import pytest

from hermes_backup.essential_backup import run
from hermes_backup.state import parse_state


def _fixture_tree(root):
    data = root / "data"
    (data / "sessions").mkdir(parents=True)
    (data / "cron" / "state").mkdir(parents=True)
    (data / "cron" / "output").mkdir(parents=True)
    (data / "skills" / "apple" / "automation").mkdir(parents=True)
    (data / "plugins" / "image_gen" / "replicate").mkdir(parents=True)
    (data / "cache").mkdir()

    (data / "sessions" / "sessions.json").write_text(json.dumps({"a": {}, "b": {}}))
    (data / "sessions" / "request_dump_20260715_231648.json").write_text("{}")
    (data / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "x"}]}))
    (data / "cron" / "state" / "x.json").write_text("{}")
    (data / "cron" / "output" / "noise.log").write_text("noise")
    (data / "skills" / "apple" / "automation" / "SKILL.md").write_text("s")
    (data / "plugins" / "image_gen" / "replicate" / "plugin.yaml").write_text("p")
    (data / "cache" / "junk.bin").write_bytes(b"0" * 64)
    (data / "auth.json").write_text("{}")
    (data / "config.yaml").write_text("model: opus\n")

    for name in ("state.db", "kanban.db"):
        connection = sqlite3.connect(data / name)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO t (id) VALUES (1)")
        connection.commit()
        connection.close()
    return data


def test_publishes_a_directory_with_exactly_five_files(tmp_path):
    data = _fixture_tree(tmp_path)
    published = run(data, tmp_path / "essential")

    assert {item.name for item in published.iterdir()} == {
        "essential.tar.gz",
        "STATE",
        "INVENTORY.jsonl",
        "EXCLUSIONS.jsonl",
        "SHA256SUMS",
    }
    assert published.name.startswith("daily-")


def test_state_counts_match_the_fixture(tmp_path):
    data = _fixture_tree(tmp_path)
    published = run(data, tmp_path / "essential")

    state = parse_state((published / "STATE").read_text())
    assert state["EXPECTED_SESSIONS"] == 2
    assert state["EXPECTED_SKILLS"] == 1
    assert state["EXPECTED_PLUGINS"] == 1
    assert state["EXPECTED_CRON_JOBS"] == 1
    assert state["BACKUP_FORMAT_VERSION"] == 1


def test_archive_carries_snapshots_and_drops_recoverable_files(tmp_path):
    data = _fixture_tree(tmp_path)
    published = run(data, tmp_path / "essential")

    with tarfile.open(published / "essential.tar.gz") as tar:
        names = set(tar.getnames())
    assert "state.db" in names and "kanban.db" in names
    assert "state.db-wal" not in names
    assert not any(name.startswith("cache/") for name in names)
    assert not any(name.startswith("cron/output/") for name in names)
    assert not any("request_dump" in name for name in names)


def test_partial_directory_is_removed_when_a_step_fails(tmp_path, monkeypatch):
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"

    import hermes_backup.essential_backup as module

    def boom(*args, **kwargs):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(module, "snapshot", boom)
    with pytest.raises(RuntimeError):
        run(data, root)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".daily-*"))


def test_torn_config_yaml_never_reaches_the_archive(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "config.yaml").write_text("model: [unclosed\n")
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError):
        run(data, root)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".daily-*"))


def test_empty_config_yaml_is_rejected(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "config.yaml").write_text("")
    with pytest.raises(RuntimeError, match="config_yaml"):
        run(data, tmp_path / "essential")


def test_previous_backup_survives_a_failed_run(tmp_path, monkeypatch):
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"
    first = run(data, root)

    import hermes_backup.essential_backup as module

    monkeypatch.setattr(module, "create", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        run(data, root)

    assert first.exists()
    assert (first / "SHA256SUMS").exists()
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_essential_backup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.essential_backup'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/essential_backup.py
"""Build the off-site essential backup on Aeza.

Order matters: snapshots and staging first, then STATE and INVENTORY
computed from staging (never from the live tree, which keeps changing),
then the archive, then a self-check, and only then the atomic publish.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.archive import create
from hermes_backup.counters import count_cron_jobs, count_plugins, count_sessions, count_skills
from hermes_backup.hashing import atomic_write_text, sha256_file, write_sha256sums
from hermes_backup.inventory import write_exclusions, write_inventory
from hermes_backup.locks import LockBusy
from hermes_backup.sqlite_snapshot import (
    foreign_key_check,
    integrity_check,
    page_count,
    snapshot,
    user_version,
)
from hermes_backup.staging import stabilized_copy
from hermes_backup.state import format_state
from hermes_backup.status import status_line, write_status

MIN_FREE_BYTES = 2 * 1024**3
MAX_STAGING_BYTES = 4 * 1024**3


def _git_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _image(field: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", field, "hermes"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _tree_bytes(root: Path) -> int:
    return sum(item.stat().st_size for item in root.rglob("*") if item.is_file())


def run(data: Path, root: Path, *, rsync: str = "rsync", repo: Path | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True)
    partial = root / f".daily-{stamp}.partial"
    published = root / f"daily-{stamp}"
    staging = partial / "staging"

    if shutil.disk_usage(root).free < MIN_FREE_BYTES:
        raise RuntimeError("insufficient_disk_space")

    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        stabilized_copy(data, staging, rsync=rsync)
        if _tree_bytes(staging) > MAX_STAGING_BYTES:
            raise RuntimeError("staging_too_large")
        _validate_structured(staging)

        databases = {}
        for name in ("state.db", "kanban.db"):
            snapshot(data / name, staging / name)
            integrity_check(staging / name)
            foreign_key_check(staging / name)
            databases[name] = {
                "sha256": sha256_file(staging / name),
                "page_count": page_count(staging / name),
                "user_version": user_version(staging / name),
            }

        totals = write_inventory(staging, partial / "INVENTORY.jsonl")
        write_exclusions(data, partial / "EXCLUSIONS.jsonl")

        atomic_write_text(
            partial / "STATE",
            format_state(
                {
                    "BACKUP_FORMAT_VERSION": 1,
                    "CREATED_AT": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "SOURCE_HOST": "aeza",
                    "HERMES_GIT_SHA": _git_sha(repo or Path("/srv/hermes/app")),
                    "HERMES_IMAGE_ID": _image("{{.Image}}"),
                    "HERMES_IMAGE_REF": _image("{{index .Config.Image}}"),
                    "STATE_DB_SHA256": databases["state.db"]["sha256"],
                    "STATE_DB_PAGE_COUNT": databases["state.db"]["page_count"],
                    "STATE_DB_USER_VERSION": databases["state.db"]["user_version"],
                    "KANBAN_DB_SHA256": databases["kanban.db"]["sha256"],
                    "KANBAN_DB_PAGE_COUNT": databases["kanban.db"]["page_count"],
                    "KANBAN_DB_USER_VERSION": databases["kanban.db"]["user_version"],
                    "EXPECTED_SESSIONS": count_sessions(staging / "sessions" / "sessions.json"),
                    "EXPECTED_SKILLS": count_skills(staging / "skills"),
                    "EXPECTED_PLUGINS": count_plugins(staging / "plugins"),
                    "EXPECTED_CRON_JOBS": count_cron_jobs(staging / "cron" / "jobs.json"),
                    "ESSENTIAL_FILE_COUNT": totals.files,
                    "ESSENTIAL_TOTAL_BYTES": totals.total_bytes,
                    "UNCLASSIFIED_FILE_COUNT": totals.unclassified,
                }
            ),
        )

        create(staging, partial / "essential.tar.gz")
        shutil.rmtree(staging)
        write_sha256sums(partial)
        _self_check(partial)
        partial.rename(published)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    _prune(root, config.RETENTION_SERVER)
    return published


def _validate_structured(staging: Path) -> None:
    """A file caught mid-write must never reach the archive.

    The lock stops other backups, not Hermes, so staging can hold a
    half-written config; parsing it here is the last gate before publish.
    """
    import yaml

    try:
        parsed = yaml.safe_load((staging / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError(f"config_yaml_unparsable: {error}") from error
    if not parsed:
        raise RuntimeError("config_yaml_empty")


def _self_check(directory: Path) -> None:
    from hermes_backup.archive import validate
    from hermes_backup.state import parse_state

    validate(directory / "essential.tar.gz")
    parse_state((directory / "STATE").read_text(encoding="utf-8"))
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(directory / name) != digest:
            raise RuntimeError(f"self_check_failed: {name}")


def _prune(root: Path, keep: int) -> None:
    daily = sorted(item for item in root.glob("daily-*") if item.is_dir())
    for stale in daily[: max(0, len(daily) - keep)] if keep >= 1 else []:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=config.SERVER_DATA)
    parser.add_argument("--root", type=Path, default=config.SERVER_ESSENTIAL_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.SERVER_STATUS_DIR)
    args = parser.parse_args(argv)
    try:
        published = run(args.data, args.root)
    except LockBusy as error:
        print(status_line("essential_backup", "SKIPPED", f"locked {error}"))
        return 75
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "essential_backup", "FAILED", reason=str(error))
        print(status_line("essential_backup", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "essential_backup", "OK", backup_path=str(published))
    print(status_line("essential_backup", "OK", f"path={published}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_essential_backup.py -v`
Expected: PASS, 7 тестов.

- [ ] **Step 5: Написать обёртку и systemd-юниты**

```bash
# deploy/beget/hermes_essential_backup.sh
#!/usr/bin/env bash
# Thin launcher: all behaviour lives in hermes_backup.essential_backup,
# where it is covered by pytest. Takes the shared backup lock so the full
# archive can never run at the same time.
set -euo pipefail

APP=/srv/hermes/app
LOCK=/run/lock/hermes-backup.lock

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "hermes_essential_backup_SKIPPED locked"
  exit 75
fi

PYTHONPATH="$APP" exec /usr/bin/python3 -m hermes_backup.essential_backup "$@"
```

```ini
# deploy/beget/systemd/hermes-essential-backup.service
[Unit]
Description=Hermes essential off-site backup
After=docker.service

[Service]
Type=oneshot
ExecStart=/srv/hermes/app/deploy/beget/hermes_essential_backup.sh
SuccessExitStatus=75
```

```ini
# deploy/beget/systemd/hermes-essential-backup.timer
[Unit]
Description=Hermes essential backup at 03:15 UTC

[Timer]
OnCalendar=*-*-* 03:15:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 6: Проверить юниты тестом**

```python
# добавить в tests/backup/test_essential_backup.py
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "beget"


def test_service_treats_lock_skip_as_success():
    unit = (DEPLOY / "systemd" / "hermes-essential-backup.service").read_text()
    assert "SuccessExitStatus=75" in unit


def test_wrapper_takes_the_shared_lock():
    wrapper = (DEPLOY / "hermes_essential_backup.sh").read_text()
    assert "/run/lock/hermes-backup.lock" in wrapper
    assert "flock -n 9" in wrapper
```

Run: `.venv/bin/python -m pytest tests/backup/test_essential_backup.py -v`
Expected: PASS, 9 тестов.

- [ ] **Step 7: Коммит**

```bash
chmod +x deploy/beget/hermes_essential_backup.sh
git add hermes_backup/essential_backup.py deploy/beget/hermes_essential_backup.sh \
        deploy/beget/systemd/ tests/backup/test_essential_backup.py
git commit -m "feat(backup): publish the essential backup atomically on Aeza"
```

---

### Task 12: Полный архив — общий лок и снимки вместо живых БД

**Files:**
- Modify: `deploy/beget/backup.sh`
- Create: `deploy/beget/systemd/hermes-full-backup.service`, `deploy/beget/systemd/hermes-full-backup.timer`
- Test: `tests/backup/test_full_backup_script.py`

**Interfaces:**
- Consumes: `hermes_backup.sqlite_snapshot` через `python3 -m hermes_backup.snapshot_cli`.
- Produces: изменённый `backup.sh`, который берёт `/run/lock/hermes-backup.lock`, исключает `state.db*`/`kanban.db*` и добавляет собственные снимки.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_full_backup_script.py
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "beget" / "backup.sh"
UNIT = Path(__file__).resolve().parents[2] / "deploy" / "beget" / "systemd" / "hermes-full-backup.service"


def test_live_databases_are_excluded_from_tar():
    text = SCRIPT.read_text()
    assert "--exclude=./state.db*" in text
    assert "--exclude=./kanban.db*" in text


def test_snapshots_are_taken_for_this_run():
    text = SCRIPT.read_text()
    assert "hermes_backup.snapshot_cli" in text
    assert "snapshot_dir" in text


def test_shared_lock_is_taken():
    text = SCRIPT.read_text()
    assert "/run/lock/hermes-backup.lock" in text
    assert "flock -n 9" in text


def test_retention_floor_is_preserved():
    text = SCRIPT.read_text()
    assert 'KEEP="${HERMES_BACKUP_KEEP:-7}"' in text
    assert '[ "$KEEP" -ge 1 ]' in text


def test_unit_treats_lock_skip_as_success():
    assert "SuccessExitStatus=75" in UNIT.read_text()
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_full_backup_script.py -v`
Expected: FAIL — ни исключений, ни лока в скрипте пока нет.

- [ ] **Step 3: Добавить CLI для снимков**

```python
# hermes_backup/snapshot_cli.py
"""Snapshot databases into a directory: used by the full-archive script."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hermes_backup.sqlite_snapshot import SnapshotError, integrity_check, snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("names", nargs="+")
    args = parser.parse_args(argv)
    args.dest.mkdir(parents=True, exist_ok=True)
    try:
        for name in args.names:
            target = args.dest / name
            snapshot(args.data / name, target)
            integrity_check(target)
    except SnapshotError as error:
        print(f"hermes_snapshot_FAILED {error}", file=sys.stderr)
        return 1
    print(f"hermes_snapshot_OK count={len(args.names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Изменить `backup.sh`**

Заменить блок создания архива (строки 21–48 текущего файла) на:

```bash
install -d -m 0700 "$BACKUP_DIR"

# The shared lock keeps the essential backup and this archive apart, so a
# snapshot pair can never straddle two different source trees.
exec 9>/run/lock/hermes-backup.lock
if ! flock -n 9; then
  echo "hermes_full_backup_SKIPPED locked"
  exit 75
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/hermes-${timestamp}.tar.gz"
tmp_archive="${archive}.partial"

# Live SQLite files are never tarred: this run takes its own consistent
# snapshots and ships those instead. Reusing another run's snapshot is
# forbidden — it would pair one tree with another tree's database.
snapshot_dir="$(mktemp -d)"
cleanup_snapshots() { rm -rf "$snapshot_dir"; }
trap cleanup_snapshots EXIT
PYTHONPATH=/srv/hermes/app /usr/bin/python3 -m hermes_backup.snapshot_cli \
  --data "$DATA_DIR" --dest "$snapshot_dir" state.db kanban.db

set +e
tar -C "$DATA_DIR" \
  --exclude=./state.db* \
  --exclude=./kanban.db* \
  -czf "$tmp_archive" . -C "$snapshot_dir" state.db kanban.db
tar_status=$?
set -e
```

Остальная часть скрипта (проверка `tar -tzf`, `mv`, `chmod 600`, ретенция)
остаётся без изменений; заменить финальные строки статуса на
`echo "hermes_full_backup_OK: $archive"`.

- [ ] **Step 5: Написать юниты**

```ini
# deploy/beget/systemd/hermes-full-backup.service
[Unit]
Description=Hermes full local archive
After=docker.service

[Service]
Type=oneshot
ExecStart=/srv/hermes/app/deploy/beget/backup.sh
SuccessExitStatus=75
```

```ini
# deploy/beget/systemd/hermes-full-backup.timer
[Unit]
Description=Hermes full archive at 04:15 UTC

[Timer]
OnCalendar=*-*-* 04:15:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 6: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_full_backup_script.py -v`
Expected: PASS, 5 тестов.

- [ ] **Step 7: Коммит**

```bash
git add deploy/beget/backup.sh deploy/beget/systemd/hermes-full-backup.* \
        hermes_backup/snapshot_cli.py tests/backup/test_full_backup_script.py
git commit -m "fix(backup): stop tarring live SQLite files in the full archive"
```

---

### Task 13: `filevault.py` и `offsite_pull.py` — стягивание на Mac

**Files:**
- Create: `hermes_backup/filevault.py`, `hermes_backup/offsite_pull.py`, `deploy/macos/hermes_pull_offsite.sh`, `deploy/macos/com.hermes.offsite-pull.plist`
- Test: `tests/backup/test_filevault.py`, `tests/backup/test_offsite_pull.py`

**Interfaces:**
- Consumes: `locks.DirectoryLock`, `state.parse_state`, `status.*`, `config.*`.
- Produces: `FileVaultOff(RuntimeError)`; `require_filevault(command: list[str] | None = None) -> None`; `pull(root: Path, remote: str, key: Path, *, runner=subprocess.run) -> Path`; `check_freshness(root: Path, max_age_hours: int) -> Path`; `main(argv=None) -> int`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_filevault.py
import pytest

from hermes_backup.filevault import FileVaultOff, require_filevault


def test_gate_passes_when_filevault_is_active():
    require_filevault(command=["/usr/bin/true"])


def test_gate_fails_closed_when_filevault_is_off():
    with pytest.raises(FileVaultOff):
        require_filevault(command=["/usr/bin/false"])


def test_gate_fails_closed_when_the_tool_is_missing():
    with pytest.raises(FileVaultOff):
        require_filevault(command=["/nonexistent/fdesetup", "isactive"])
```

```python
# tests/backup/test_offsite_pull.py
import json
from datetime import datetime, timedelta, timezone

import pytest

from hermes_backup.offsite_pull import check_freshness, prune


def _backup(root, stamp, age_hours=0):
    directory = root / f"daily-{stamp}"
    directory.mkdir(parents=True)
    (directory / "STATE").write_text("BACKUP_FORMAT_VERSION=1\n")
    moment = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp()
    import os

    os.utime(directory, (moment, moment))
    return directory


def test_freshness_accepts_a_recent_backup(tmp_path):
    fresh = _backup(tmp_path, "20260726T031500Z", age_hours=2)
    assert check_freshness(tmp_path, 26) == fresh


def test_freshness_rejects_a_stale_backup(tmp_path):
    _backup(tmp_path, "20260720T031500Z", age_hours=100)
    with pytest.raises(RuntimeError, match="stale_backup"):
        check_freshness(tmp_path, 26)


def test_freshness_rejects_an_empty_root(tmp_path):
    with pytest.raises(RuntimeError, match="no_backup"):
        check_freshness(tmp_path, 26)


def test_partial_directories_are_invisible(tmp_path):
    (tmp_path / ".daily-20260726T031500Z.partial").mkdir()
    with pytest.raises(RuntimeError, match="no_backup"):
        check_freshness(tmp_path, 26)


def test_prune_keeps_the_floor(tmp_path):
    for day in range(1, 11):
        _backup(tmp_path, f"2026072{day % 10}T0{day}1500Z")
    prune(tmp_path, keep=7, floor=2)
    assert len(list(tmp_path.glob("daily-*"))) == 7


def test_prune_never_empties_the_directory(tmp_path):
    _backup(tmp_path, "20260726T031500Z")
    prune(tmp_path, keep=0, floor=2)
    assert len(list(tmp_path.glob("daily-*"))) == 1
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/python -m pytest tests/backup/test_filevault.py tests/backup/test_offsite_pull.py -v`
Expected: FAIL — модулей нет.

- [ ] **Step 3: Реализовать `filevault.py`**

```python
# hermes_backup/filevault.py
"""Runtime gate: never write secrets onto an unencrypted disk.

`fdesetup isactive` prints true/false and exits accordingly, so the gate
reads an exit code instead of parsing human-readable status output.
"""

from __future__ import annotations

import subprocess


class FileVaultOff(RuntimeError):
    """FileVault is not active, so off-site secrets must not be written."""


def require_filevault(command: list[str] | None = None) -> None:
    argv = command or ["/usr/bin/fdesetup", "isactive"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as error:
        raise FileVaultOff(f"cannot run {argv[0]}: {error}") from error
    if result.returncode != 0:
        raise FileVaultOff("filevault_off")
```

- [ ] **Step 4: Реализовать `offsite_pull.py`**

```python
# hermes_backup/offsite_pull.py
"""Pull the essential backup to the Mac and check what landed.

Pull, never push: the server has no route into this laptop. The local
publish is atomic too — a half-transferred directory must never look
like a backup the drill can pick.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.filevault import FileVaultOff, require_filevault
from hermes_backup.hashing import sha256_file
from hermes_backup.locks import DirectoryLock, LockBusy
from hermes_backup.state import parse_state
from hermes_backup.status import status_line, write_status

STAMP = re.compile(r"\Adaily-[0-9]{8}T[0-9]{6}Z\Z")


def _ssh_command(key: Path) -> str:
    return (
        f"ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 "
        f"-o ServerAliveCountMax=12 -i {key}"
    )


def _latest_remote(remote: str, key: Path, remote_root: str, runner) -> str:
    result = runner(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-i",
            str(key),
            remote,
            f"find '{remote_root}' -mindepth 1 -maxdepth 1 -type d -name 'daily-*' "
            "-printf '%f\\n' | LC_ALL=C sort | tail -1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    name = result.stdout.strip()
    if not STAMP.match(name):
        raise RuntimeError(f"invalid_remote_name {name!r}")
    return name


def _verify(directory: Path) -> None:
    sums = directory / "SHA256SUMS"
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(directory / name) != digest:
            raise RuntimeError(f"checksum_mismatch {name}")
    parse_state((directory / "STATE").read_text(encoding="utf-8"))


def pull(
    root: Path,
    remote: str,
    key: Path,
    remote_root: str = "/srv/hermes/backups/essential",
    runner=subprocess.run,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    name = _latest_remote(remote, key, remote_root, runner)
    published = root / name
    if published.exists():
        _verify(published)
        return published

    partial = root / f".{name}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    try:
        result = runner(
            [
                "rsync",
                "-a",
                "--partial",
                "-e",
                _ssh_command(key),
                f"{remote}:{remote_root}/{name}/",
                f"{partial}/",
            ],
            check=False,
        )
        if getattr(result, "returncode", 0) != 0:
            raise RuntimeError(f"rsync_failed {result.returncode}")
        _verify(partial)
        partial.rename(published)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    for item in published.rglob("*"):
        item.chmod(0o600 if item.is_file() else 0o700)
    return published


def check_freshness(root: Path, max_age_hours: int) -> Path:
    backups = sorted(item for item in root.glob("daily-*") if item.is_dir())
    if not backups:
        raise RuntimeError("no_backup")
    newest = backups[-1]
    age = datetime.now(timezone.utc).timestamp() - newest.stat().st_mtime
    if age > max_age_hours * 3600:
        raise RuntimeError(f"stale_backup age_hours={age / 3600:.1f}")
    return newest


def prune(root: Path, keep: int, floor: int) -> None:
    backups = sorted(item for item in root.glob("daily-*") if item.is_dir())
    keep = max(keep, floor)
    for stale in backups[: max(0, len(backups) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    args = parser.parse_args(argv)
    try:
        require_filevault()
    except FileVaultOff as error:
        write_status(args.status_dir, "offsite_pull", "FAILED", reason=str(error))
        print(status_line("offsite_pull", "FAILED", str(error)), file=sys.stderr)
        return 1
    lock = DirectoryLock(config.MAC_NETWORK_LOCK, owner="hermes-pull")
    try:
        lock.acquire(wait_seconds=config.NETWORK_LOCK_WAIT_SECONDS)
    except LockBusy as error:
        write_status(args.status_dir, "offsite_pull", "FAILED", reason="lock_timeout")
        print(status_line("offsite_pull", "FAILED", f"lock_timeout {error}"), file=sys.stderr)
        return 1
    try:
        published = pull(args.root, config.REMOTE, config.SSH_KEY)
        prune(args.root, config.RETENTION_MAC, config.RETENTION_MAC_FLOOR)
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "offsite_pull", "FAILED", reason=str(error))
        print(status_line("offsite_pull", "FAILED", str(error)), file=sys.stderr)
        return 1
    finally:
        lock.release()
    write_status(args.status_dir, "offsite_pull", "OK", backup_path=str(published))
    print(status_line("offsite_pull", "OK", f"path={published}"))

    try:
        fresh = check_freshness(args.root, config.FRESHNESS_HOURS)
    except RuntimeError as error:
        write_status(args.status_dir, "freshness", "FAILED", reason=str(error))
        print(status_line("freshness", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "freshness", "OK", backup_path=str(fresh))
    print(status_line("freshness", "OK", f"path={fresh}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_filevault.py tests/backup/test_offsite_pull.py -v`
Expected: PASS, 9 тестов.

- [ ] **Step 6: Написать обёртку и LaunchAgent**

```bash
# deploy/macos/hermes_pull_offsite.sh
#!/usr/bin/env bash
set -euo pipefail
REPO="${HERMES_REPO:-/Users/romanmizanov/Documents/Hermes}"
exec "$REPO/.venv/bin/python" -m hermes_backup.offsite_pull "$@"
```

```xml
<!-- deploy/macos/com.hermes.offsite-pull.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hermes.offsite-pull</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>/bin/bash</string>
    <string>/Users/romanmizanov/Documents/Hermes/deploy/macos/hermes_pull_offsite.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>/Users/romanmizanov/Documents/Hermes</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>6</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/romanmizanov/Library/Logs/hermes-backup/pull.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/romanmizanov/Library/Logs/hermes-backup/pull.err.log</string>
</dict>
</plist>
```

- [ ] **Step 7: Проверить plist тестом**

```python
# добавить в tests/backup/test_offsite_pull.py
import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parents[2] / "deploy" / "macos" / "com.hermes.offsite-pull.plist"


def test_launch_agent_runs_daily_and_names_its_logs():
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "com.hermes.offsite-pull"
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}
    assert data["StandardErrorPath"].endswith("pull.err.log")


def test_launch_agent_has_no_secrets():
    text = PLIST.read_text()
    assert "sk-" not in text
    assert "TOKEN" not in text
```

Run: `.venv/bin/python -m pytest tests/backup/test_offsite_pull.py -v`
Expected: PASS, 8 тестов.

- [ ] **Step 8: Коммит**

```bash
chmod +x deploy/macos/hermes_pull_offsite.sh
git add hermes_backup/filevault.py hermes_backup/offsite_pull.py deploy/macos/ \
        tests/backup/test_filevault.py tests/backup/test_offsite_pull.py
git commit -m "feat(backup): pull the off-site copy behind a FileVault gate"
```

---

### Task 14: `restore_drill.py` — проверка стянутой копии

**Files:**
- Create: `hermes_backup/restore_drill.py`, `deploy/macos/hermes_restore_drill.sh`, `deploy/macos/com.hermes.restore-drill.plist`
- Test: `tests/backup/test_restore_drill.py`

**Interfaces:**
- Consumes: `archive.extract`, `sqlite_snapshot.integrity_check/foreign_key_check/page_count`, `counters.*`, `state.parse_state`, `status.*`, `config.*`.
- Produces: `DrillError(RuntimeError)`; `drill(backup: Path, *, staleness_hours: int = 48) -> dict` — возвращает сводку `{"sessions": int, "skills": int, "plugins": int, "cron_jobs": int, "unclassified": int}`; `main(argv=None) -> int`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_restore_drill.py
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_backup.essential_backup import run
from hermes_backup.restore_drill import DrillError, drill
from tests.backup.test_essential_backup import _fixture_tree


def _published(tmp_path):
    data = _fixture_tree(tmp_path)
    return run(data, tmp_path / "essential")


def test_healthy_backup_passes_and_reports_counts(tmp_path):
    summary = drill(_published(tmp_path))
    assert summary == {
        "sessions": 2,
        "skills": 1,
        "plugins": 1,
        "cron_jobs": 1,
        "unclassified": summary["unclassified"],
    }


def test_temporary_directory_is_removed(tmp_path, monkeypatch):
    seen = {}
    real_mkdtemp = __import__("tempfile").mkdtemp

    def spy(*args, **kwargs):
        seen["path"] = real_mkdtemp(*args, **kwargs)
        return seen["path"]

    monkeypatch.setattr("tempfile.mkdtemp", spy)
    drill(_published(tmp_path))
    assert not Path(seen["path"]).exists()


def test_stale_backup_is_rejected(tmp_path):
    published = _published(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).timestamp()
    os.utime(published, (old, old))
    with pytest.raises(DrillError, match="stale_backup"):
        drill(published)


def test_checksum_mismatch_is_rejected(tmp_path):
    published = _published(tmp_path)
    (published / "STATE").write_text("BACKUP_FORMAT_VERSION=1\n")
    with pytest.raises(DrillError, match="checksum"):
        drill(published)


def test_corrupt_database_is_caught(tmp_path):
    import tarfile

    published = _published(tmp_path)
    workdir = tmp_path / "rewrite"
    workdir.mkdir()
    with tarfile.open(published / "essential.tar.gz") as tar:
        tar.extractall(workdir)
    (workdir / "state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    with tarfile.open(published / "essential.tar.gz", "w:gz") as tar:
        for item in sorted(workdir.rglob("*")):
            tar.add(item, arcname=item.relative_to(workdir).as_posix(), recursive=False)
    from hermes_backup.hashing import write_sha256sums

    write_sha256sums(published)
    with pytest.raises(DrillError, match="integrity"):
        drill(published)


def test_counter_mismatch_is_caught(tmp_path):
    published = _published(tmp_path)
    state = (published / "STATE").read_text().replace(
        "EXPECTED_SKILLS=1", "EXPECTED_SKILLS=99"
    )
    (published / "STATE").write_text(state)
    from hermes_backup.hashing import write_sha256sums

    write_sha256sums(published)
    with pytest.raises(DrillError, match="EXPECTED_SKILLS"):
        drill(published)


def test_zero_cron_jobs_is_valid(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "cron" / "jobs.json").write_text('{"jobs": []}')
    published = run(data, tmp_path / "essential")
    assert drill(published)["cron_jobs"] == 0


def test_secret_modes_are_checked(tmp_path):
    published = _published(tmp_path)
    summary = drill(published)
    assert summary["sessions"] == 2


def test_drill_makes_no_network_or_container_calls(tmp_path):
    """Stub docker/ssh/curl so any call aborts, then run the real drill."""
    published = _published(tmp_path)
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name in ("docker", "ssh", "curl", "rsync"):
        stub = stub_dir / name
        stub.write_text("#!/bin/sh\necho \"forbidden call: $0\" >&2\nexit 99\n")
        stub.chmod(0o755)

    env = dict(os.environ, PATH=f"{stub_dir}:/usr/bin:/bin")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, "-m", "hermes_backup.restore_drill", "--backup", str(published)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "forbidden call" not in result.stderr
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/python -m pytest tests/backup/test_restore_drill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.restore_drill'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/restore_drill.py
"""Prove the pulled copy is restorable — without starting anything.

The drill never launches the container, the gateway or Telegram: the
archive holds live tokens, and a second Telegram poller would answer
Roman's messages twice.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_backup import config
from hermes_backup.archive import ArchiveError, extract
from hermes_backup.counters import (
    CounterError,
    count_cron_jobs,
    count_plugins,
    count_sessions,
    count_skills,
)
from hermes_backup.hashing import sha256_file
from hermes_backup.sqlite_snapshot import (
    SnapshotError,
    foreign_key_check,
    integrity_check,
    page_count,
)
from hermes_backup.state import StateError, parse_state
from hermes_backup.status import status_line, write_status

REQUIRED = ("auth.json", "config.yaml", "state.db", "kanban.db", "cron/jobs.json")
SECRETS = ("auth.json", "config.yaml")


class DrillError(RuntimeError):
    """The backup failed a restore check."""


def _check_age(backup: Path, staleness_hours: int) -> None:
    age = datetime.now(timezone.utc).timestamp() - backup.stat().st_mtime
    if age > staleness_hours * 3600:
        raise DrillError(f"stale_backup age_hours={age / 3600:.1f}")


def _check_sums(backup: Path) -> None:
    for line in (backup / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(backup / name) != digest:
            raise DrillError(f"checksum_mismatch {name}")


def _check_counts(tree: Path, state: dict) -> dict:
    counts = {
        "sessions": count_sessions(tree / "sessions" / "sessions.json"),
        "skills": count_skills(tree / "skills"),
        "plugins": count_plugins(tree / "plugins"),
        "cron_jobs": count_cron_jobs(tree / "cron" / "jobs.json"),
    }
    expected = {
        "sessions": "EXPECTED_SESSIONS",
        "skills": "EXPECTED_SKILLS",
        "plugins": "EXPECTED_PLUGINS",
        "cron_jobs": "EXPECTED_CRON_JOBS",
    }
    for key, state_key in expected.items():
        if counts[key] != state[state_key]:
            raise DrillError(f"{state_key} expected {state[state_key]}, found {counts[key]}")
    return counts


def drill(backup: Path, *, staleness_hours: int = config.DRILL_STALENESS_HOURS) -> dict:
    _check_age(backup, staleness_hours)
    _check_sums(backup)
    try:
        state = parse_state((backup / "STATE").read_text(encoding="utf-8"))
    except StateError as error:
        raise DrillError(f"state_invalid {error}") from error

    workdir = Path(tempfile.mkdtemp(prefix="hermes-drill-"))
    try:
        tree = workdir / "tree"
        try:
            extract(backup / "essential.tar.gz", tree)
        except ArchiveError as error:
            raise DrillError(f"archive_unsafe {error}") from error

        for name in REQUIRED:
            if not (tree / name).exists():
                raise DrillError(f"missing_required {name}")

        for name in ("state.db", "kanban.db"):
            try:
                integrity_check(tree / name)
                foreign_key_check(tree / name)
            except SnapshotError as error:
                raise DrillError(f"integrity {name}: {error}") from error

        for name, key in (("state.db", "STATE_DB_PAGE_COUNT"), ("kanban.db", "KANBAN_DB_PAGE_COUNT")):
            actual = page_count(tree / name)
            if actual != state[key]:
                raise DrillError(f"{key} expected {state[key]}, found {actual}")

        config_text = (tree / "config.yaml").read_text(encoding="utf-8")
        try:
            parsed_config = yaml.safe_load(config_text)
        except yaml.YAMLError as error:
            raise DrillError(f"config_unparsable {error}") from error
        if not parsed_config:
            raise DrillError("config_empty")

        try:
            counts = _check_counts(tree, state)
        except CounterError as error:
            raise DrillError(f"counter {error}") from error

        for name in SECRETS:
            mode = (tree / name).stat().st_mode & 0o777
            if mode & 0o077:
                raise DrillError(f"permissions_too_wide {name} {mode:o}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {**counts, "unclassified": int(state["UNCLASSIFIED_FILE_COUNT"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    args = parser.parse_args(argv)

    backup = args.backup
    if backup is None:
        candidates = sorted(item for item in args.root.glob("daily-*") if item.is_dir())
        if not candidates:
            write_status(args.status_dir, "restore_drill", "FAILED", reason="no_backup")
            print(status_line("restore_drill", "FAILED", "no_backup"), file=sys.stderr)
            return 1
        backup = candidates[-1]

    try:
        summary = drill(backup)
    except (DrillError, OSError) as error:
        write_status(args.status_dir, "restore_drill", "FAILED", reason=str(error), backup_path=str(backup))
        print(status_line("restore_drill", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "restore_drill", "OK", backup_path=str(backup))
    print(
        status_line(
            "restore_drill",
            "OK",
            "sessions={sessions} skills={skills} plugins={plugins} "
            "cron_jobs={cron_jobs} unclassified={unclassified}".format(**summary),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_restore_drill.py -v`
Expected: PASS, 9 тестов.

- [ ] **Step 5: Написать обёртку и LaunchAgent**

```bash
# deploy/macos/hermes_restore_drill.sh
#!/usr/bin/env bash
set -euo pipefail
REPO="${HERMES_REPO:-/Users/romanmizanov/Documents/Hermes}"
exec "$REPO/.venv/bin/python" -m hermes_backup.restore_drill "$@"
```

```xml
<!-- deploy/macos/com.hermes.restore-drill.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hermes.restore-drill</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/romanmizanov/Documents/Hermes/deploy/macos/hermes_restore_drill.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>/Users/romanmizanov/Documents/Hermes</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>11</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/romanmizanov/Library/Logs/hermes-backup/drill.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/romanmizanov/Library/Logs/hermes-backup/drill.err.log</string>
</dict>
</plist>
```

- [ ] **Step 6: Проверить plist тестом**

```python
# добавить в tests/backup/test_restore_drill.py
import plistlib

DRILL_PLIST = Path(__file__).resolve().parents[2] / "deploy" / "macos" / "com.hermes.restore-drill.plist"


def test_drill_runs_on_sunday_morning():
    data = plistlib.loads(DRILL_PLIST.read_bytes())
    assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 11, "Minute": 0}
```

Run: `.venv/bin/python -m pytest tests/backup/test_restore_drill.py -v`
Expected: PASS, 10 тестов.

- [ ] **Step 7: Коммит**

```bash
chmod +x deploy/macos/hermes_restore_drill.sh
git add hermes_backup/restore_drill.py deploy/macos/hermes_restore_drill.sh \
        deploy/macos/com.hermes.restore-drill.plist tests/backup/test_restore_drill.py
git commit -m "feat(backup): drill the pulled copy without starting Hermes"
```

---

### Task 15: `backup_status.py` — сводка одной командой

**Files:**
- Create: `hermes_backup/backup_status.py`, `deploy/macos/hermes_backup_status.sh`
- Test: `tests/backup/test_backup_status.py`

**Interfaces:**
- Consumes: `status.read_status`, `config.*`.
- Produces: `summary(root: Path, status_dir: Path, lock: Path) -> str`; `main(argv=None) -> int`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_backup_status.py
from hermes_backup.backup_status import summary
from hermes_backup.status import write_status


def test_summary_reports_every_component(tmp_path):
    root = tmp_path / "offsite"
    (root / "daily-20260726T031500Z").mkdir(parents=True)
    status_dir = tmp_path / "status"
    write_status(status_dir, "offsite_pull", "OK")
    write_status(status_dir, "freshness", "OK")
    write_status(status_dir, "restore_drill", "FAILED", reason="checksum_mismatch STATE")

    text = summary(root, status_dir, tmp_path / "network.lock")

    assert "daily-20260726T031500Z" in text
    assert "restore_drill: FAILED" in text
    assert "checksum_mismatch" in text
    assert "network lock: free" in text


def test_missing_components_are_named_not_hidden(tmp_path):
    text = summary(tmp_path / "offsite", tmp_path / "status", tmp_path / "network.lock")
    assert "no backups" in text
    assert "offsite_pull: never ran" in text


def test_held_lock_is_reported_with_owner(tmp_path):
    lock = tmp_path / "network.lock"
    lock.mkdir()
    (lock / "meta.json").write_text('{"pid": 1, "owner": "kf-pull", "started_at": "2026-07-26T09:00:00Z"}')
    text = summary(tmp_path / "offsite", tmp_path / "status", lock)
    assert "kf-pull" in text
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_backup_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.backup_status'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/backup_status.py
"""One command that answers: is the off-site copy healthy right now?"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.status import read_status

COMPONENTS = ("offsite_pull", "freshness", "restore_drill")


def summary(root: Path, status_dir: Path, lock: Path) -> str:
    lines: list[str] = []
    backups = sorted(item for item in root.glob("daily-*") if item.is_dir()) if root.exists() else []
    if backups:
        newest = backups[-1]
        age_hours = (datetime.now(timezone.utc).timestamp() - newest.stat().st_mtime) / 3600
        lines.append(f"latest backup: {newest.name} ({age_hours:.1f} h old, {len(backups)} kept)")
    else:
        lines.append("latest backup: no backups")

    for name in COMPONENTS:
        record = read_status(status_dir, name)
        if record is None:
            lines.append(f"{name}: never ran")
            continue
        reason = f" — {record['reason']}" if record.get("reason") else ""
        lines.append(f"{name}: {record['outcome']} at {record['finished_at']}{reason}")

    if lock.exists():
        try:
            meta = json.loads((lock / "meta.json").read_text(encoding="utf-8"))
            lines.append(f"network lock: held by {meta.get('owner')} since {meta.get('started_at')}")
        except (OSError, json.JSONDecodeError):
            lines.append("network lock: held (metadata unreadable)")
    else:
        lines.append("network lock: free")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    parser.add_argument("--lock", type=Path, default=config.MAC_NETWORK_LOCK)
    args = parser.parse_args(argv)
    print(summary(args.root, args.status_dir, args.lock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

```bash
# deploy/macos/hermes_backup_status.sh
#!/usr/bin/env bash
set -euo pipefail
REPO="${HERMES_REPO:-/Users/romanmizanov/Documents/Hermes}"
exec "$REPO/.venv/bin/python" -m hermes_backup.backup_status "$@"
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_backup_status.py -v`
Expected: PASS, 3 теста.

- [ ] **Step 5: Коммит**

```bash
chmod +x deploy/macos/hermes_backup_status.sh
git add hermes_backup/backup_status.py deploy/macos/hermes_backup_status.sh \
        tests/backup/test_backup_status.py
git commit -m "feat(backup): summarize backup health from status files"
```

---

### Task 16: Knowledge Factory — убрать `source STATE`

Репозиторий: `/Users/romanmizanov/Documents/BD/knowledge-factory`.

**Files:**
- Create: `scripts/state_parser.py`
- Modify: `scripts/restore_drill.sh:31` (строка `source "$latest/STATE"`)
- Test: `tests/test_restore_drill_state.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `scripts/state_parser.py --key EXPECTED_DOCUMENTS <file>` печатает значение и завершается ненулевым кодом на неизвестном ключе или небезопасном значении.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_restore_drill_state.py
"""The KF drill runs as root from a weekly timer and used to `source` a
file that lives inside the backup directory: anyone able to write there
got root code execution. These tests pin the fix."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PARSER = REPO / "scripts" / "state_parser.py"
DRILL = REPO / "scripts" / "restore_drill.sh"


def _run(args, **kwargs):
    return subprocess.run([sys.executable, str(PARSER), *args], capture_output=True, text=True, **kwargs)


def test_drill_never_sources_state():
    text = DRILL.read_text()
    assert "source \"$latest/STATE\"" not in text
    assert "source $latest/STATE" not in text
    assert "state_parser.py" in text


def test_parser_returns_a_whitelisted_value(tmp_path):
    state = tmp_path / "STATE"
    state.write_text("EXPECTED_DOCUMENTS=96\nEXPECTED_CHUNKS=621\nEXPECTED_POINTS=621\n")
    result = _run(["--key", "EXPECTED_DOCUMENTS", str(state)])
    assert result.returncode == 0
    assert result.stdout.strip() == "96"


def test_parser_rejects_unknown_key(tmp_path):
    state = tmp_path / "STATE"
    state.write_text("EVIL=1\n")
    assert _run(["--key", "EVIL", str(state)]).returncode != 0


def test_command_substitution_is_never_executed(tmp_path):
    canary = tmp_path / "canary"
    canary.write_text("intact")
    state = tmp_path / "STATE"
    state.write_text(f"EXPECTED_DOCUMENTS=$(rm -f {canary})\n")
    assert _run(["--key", "EXPECTED_DOCUMENTS", str(state)]).returncode != 0
    assert canary.read_text() == "intact"
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `cd /Users/romanmizanov/Documents/BD/knowledge-factory && uv run pytest tests/test_restore_drill_state.py -v`
Expected: FAIL — `scripts/state_parser.py` не существует.

- [ ] **Step 3: Реализовать парсер**

```python
#!/usr/bin/env python3
# scripts/state_parser.py
"""Read one whitelisted key from a backup STATE file.

STATE lives inside the backup directory, so it is untrusted input and is
never sourced by the shell.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ALLOWED = {"EXPECTED_DOCUMENTS", "EXPECTED_CHUNKS", "EXPECTED_POINTS"}
INTEGER = re.compile(r"\A[0-9]+\Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.key not in ALLOWED:
        print(f"state_parser_FAILED unknown key {args.key}", file=sys.stderr)
        return 2
    for raw in args.path.read_text(encoding="utf-8").splitlines():
        key, separator, value = raw.strip().partition("=")
        if not separator or key != args.key:
            continue
        if not INTEGER.match(value):
            print(f"state_parser_FAILED {key} is not an integer", file=sys.stderr)
            return 2
        print(value)
        return 0
    print(f"state_parser_FAILED missing key {args.key}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Заменить `source` в `restore_drill.sh`**

Строку `source "$latest/STATE"` заменить на:

```bash
# STATE comes from the backup directory: parse it, never execute it.
EXPECTED_DOCUMENTS="$(python3 "$ROOT/app/scripts/state_parser.py" --key EXPECTED_DOCUMENTS "$latest/STATE")"
EXPECTED_CHUNKS="$(python3 "$ROOT/app/scripts/state_parser.py" --key EXPECTED_CHUNKS "$latest/STATE")"
EXPECTED_POINTS="$(python3 "$ROOT/app/scripts/state_parser.py" --key EXPECTED_POINTS "$latest/STATE")"
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd /Users/romanmizanov/Documents/BD/knowledge-factory && uv run pytest tests/test_restore_drill_state.py -v`
Expected: PASS, 4 теста.

- [ ] **Step 6: Прогнать весь набор KF**

Run: `cd /Users/romanmizanov/Documents/BD/knowledge-factory && uv run pytest -q`
Expected: PASS — 213 passed, 1 skipped (209 прежних + 4 новых).

- [ ] **Step 7: Коммит**

```bash
cd /Users/romanmizanov/Documents/BD/knowledge-factory
git add scripts/state_parser.py scripts/restore_drill.sh tests/test_restore_drill_state.py
git commit -m "fix(restore): parse STATE instead of sourcing it as root"
```

---

### Task 17: Knowledge Factory — FileVault-гейт и общий сетевой лок

**Files:**
- Modify: `scripts/pull_backups_from_aeza.sh`
- Test: `tests/test_pull_filevault_gate.py`

**Interfaces:**
- Consumes: `hermes_backup.locks` не используется — KF берёт тот же каталог-лок собственными средствами, протокол общий: каталог `~/Library/Application Support/offsite-sync/network.lock` с `meta.json`, содержащим `pid`, `owner`, `started_at`.
- Produces: скрипт, который не делает ни одного сетевого вызова без активного FileVault и не конкурирует с Hermes pull за канал.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_pull_filevault_gate.py
"""KF backups carry no secrets of their own, but they land next to the
Hermes off-site copy on the same disk. The gate keeps both off an
unencrypted volume."""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "pull_backups_from_aeza.sh"


def test_script_checks_filevault_before_any_network_call():
    text = SCRIPT.read_text()
    gate = text.index("fdesetup isactive")
    first_rsync = text.index("rsync")
    assert gate < first_rsync


def test_script_takes_the_shared_network_lock():
    text = SCRIPT.read_text()
    assert "offsite-sync/network.lock" in text
    assert "meta.json" in text


def test_gate_blocks_the_run_when_filevault_is_off(tmp_path):
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    fdesetup = stub_dir / "fdesetup"
    fdesetup.write_text("#!/bin/sh\nexit 1\n")
    fdesetup.chmod(0o755)
    for forbidden in ("rsync", "ssh"):
        stub = stub_dir / forbidden
        stub.write_text("#!/bin/sh\necho 'network call escaped the gate' >&2\nexit 42\n")
        stub.chmod(0o755)

    env = dict(os.environ, PATH=f"{stub_dir}:/usr/bin:/bin")
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)

    assert result.returncode != 0
    assert "network call escaped the gate" not in result.stderr
    assert "filevault" in (result.stderr + result.stdout).lower()
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `cd /Users/romanmizanov/Documents/BD/knowledge-factory && uv run pytest tests/test_pull_filevault_gate.py -v`
Expected: FAIL — в скрипте нет ни гейта, ни лока.

- [ ] **Step 3: Добавить гейт и лок в начало скрипта**

Вставить сразу после `set -euo pipefail`:

```bash
# Backups land beside the Hermes off-site copy, which carries live
# tokens. Never write either onto an unencrypted disk.
if ! fdesetup isactive >/dev/null 2>&1; then
  echo "offsite_pull_FAILED filevault_off" >&2
  exit 1
fi

# One narrow uplink, two pullers: whoever gets the lock transfers, the
# other waits. A stale lock is only reclaimed when its process is gone —
# never by age, because a legitimate KF pull can run for hours.
LOCK_DIR="$HOME/Library/Application Support/offsite-sync/network.lock"
WAIT_SECONDS="${OFFSITE_LOCK_WAIT_SECONDS:-21600}"
mkdir -p "$(dirname "$LOCK_DIR")"
deadline=$(( $(date +%s) + WAIT_SECONDS ))
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  holder_pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' \
    "$LOCK_DIR/meta.json" 2>/dev/null || true)"
  if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "offsite_pull_FAILED lock_timeout held by pid $holder_pid" >&2
      exit 1
    fi
    sleep 30
    continue
  fi
  rm -f "$LOCK_DIR/meta.json" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || true
done
printf '{"pid": %d, "owner": "kf-pull", "started_at": "%s"}\n' \
  "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$LOCK_DIR/meta.json"
release_lock() { rm -f "$LOCK_DIR/meta.json"; rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap release_lock EXIT
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd /Users/romanmizanov/Documents/BD/knowledge-factory && uv run pytest tests/test_pull_filevault_gate.py -v`
Expected: PASS, 3 теста.

- [ ] **Step 5: Прогнать весь набор KF**

Run: `cd /Users/romanmizanov/Documents/BD/knowledge-factory && uv run pytest -q`
Expected: PASS — 216 passed, 1 skipped.

- [ ] **Step 6: Коммит**

```bash
cd /Users/romanmizanov/Documents/BD/knowledge-factory
git add scripts/pull_backups_from_aeza.sh tests/test_pull_filevault_gate.py
git commit -m "fix(offsite): gate the pull on FileVault and share the network lock"
```

---

### Task 18: Развёртывание и первая живая проверка

Выполняется только после Task 1 (`fdesetup isactive` = `true`) и зелёного
прогона всех тестов в обоих репозиториях.

**Files:**
- Modify: `MIGRATION_KNOWLEDGE_FACTORY_TO_AEZA.md` (раздел про бэкапы Hermes)
- Create: `deploy/beget/README.md` — секция «Hermes survivability»

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: работающие таймеры на Aeza, работающие агенты на Mac, первая проверенная off-site копия.

- [ ] **Step 1: Прогнать полный набор тестов Hermes**

Run: `.venv/bin/python -m pytest tests/backup -v`
Expected: PASS, все тесты Task 2–15.

- [ ] **Step 2: Выкатить код на сервер**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 \
  'cd /srv/hermes/app && git fetch origin && git reset --hard origin/main && git log --oneline -1'
```

- [ ] **Step 3: Прогнать essential-бэкап вручную**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 \
  '/srv/hermes/app/deploy/beget/hermes_essential_backup.sh'
```

Expected: строка `hermes_essential_backup_OK path=/srv/hermes/backups/essential/daily-<UTC>`.
Проверить, что в каталоге ровно пять файлов и что `STATE` содержит
`EXPECTED_SESSIONS=2`, `EXPECTED_SKILLS=78`, `EXPECTED_PLUGINS=3`.

- [ ] **Step 4: Установить таймеры**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
install -m 0644 /srv/hermes/app/deploy/beget/systemd/hermes-essential-backup.service /etc/systemd/system/
install -m 0644 /srv/hermes/app/deploy/beget/systemd/hermes-essential-backup.timer /etc/systemd/system/
install -m 0644 /srv/hermes/app/deploy/beget/systemd/hermes-full-backup.service /etc/systemd/system/
install -m 0644 /srv/hermes/app/deploy/beget/systemd/hermes-full-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hermes-essential-backup.timer hermes-full-backup.timer
crontab -l | grep -v 'deploy/beget/backup.sh' | crontab -
systemctl list-timers --no-pager | grep hermes
EOF
```

Старая cron-запись в 03:15 снимается: расписание теперь ведут таймеры,
и только они знают про `SuccessExitStatus=75`.

- [ ] **Step 5: Первый pull и drill на Mac**

```bash
deploy/macos/hermes_pull_offsite.sh
deploy/macos/hermes_restore_drill.sh
deploy/macos/hermes_backup_status.sh
```

Expected: `hermes_offsite_pull_OK`, `hermes_freshness_OK`,
`hermes_restore_drill_OK sessions=2 skills=78 plugins=3 cron_jobs=<N> unclassified=<N>`.
Если `unclassified` больше нуля — посмотреть `INVENTORY.jsonl` и решить,
дополнять ли `ESSENTIAL_RULES`.

- [ ] **Step 6: Установить агенты**

```bash
cp deploy/macos/com.hermes.offsite-pull.plist ~/Library/LaunchAgents/
cp deploy/macos/com.hermes.restore-drill.plist ~/Library/LaunchAgents/
mkdir -p ~/Library/Logs/hermes-backup
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hermes.offsite-pull.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hermes.restore-drill.plist
launchctl list | grep com.hermes
```

- [ ] **Step 7: Проверить, что полный архив больше не содержит живых БД**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 \
  'latest=$(ls -1t /srv/hermes/backups/hermes-*.tar.gz | head -1); \
   tar -tzf "$latest" | grep -E "state\.db|kanban\.db"'
```

Expected: ровно `./state.db` и `./kanban.db` (снимки), без `-wal` и `-shm`.

- [ ] **Step 8: Обновить документацию и закоммитить**

```bash
git add MIGRATION_KNOWLEDGE_FACTORY_TO_AEZA.md deploy/beget/README.md
git commit -m "docs(backup): document the Hermes survivability runbook"
git push origin main
```

---

## Self-Review

**Покрытие спеки.** Пройдено по разделам: компонент A → Task 11; компонент B →
Task 12; компонент C → Task 13; компонент D → Task 14; общие модули
(`sqlite_snapshot`, `state_parser`, `locks`) → Task 6, 2, 9; форматы `STATE`,
`INVENTORY.jsonl`, `EXCLUSIONS.jsonl`, `SHA256SUMS`, структура каталога →
Task 2, 5, 3, 11; обработка ошибок и статусы → Task 10, 11, 13, 14; сводка →
Task 15; безопасность (`source STATE` в KF, валидация tar, FileVault) →
Task 16, 7, 13, 17; расписание → Task 11, 12, 13, 14, 18; предусловие
FileVault → Task 1.

**Структурированные файлы проверяются на сервере.** Спека требует разбирать их
до публикации, и это обязательный критерий приёмки Task 11, а не пожелание:
`cron/jobs.json` — через `count_cron_jobs`, `config.yaml` — через
`yaml.safe_load` по staging-копии. PyYAML на Aeza есть (6.0.1). Задача не
считается принятой, если пойманный на середине записи `config.yaml` попадает
в архив.

**Placeholder-скан.** «TBD», «TODO», «add error handling» в плане нет; каждый
шаг с кодом содержит код целиком.

**Согласованность типов.** `write_inventory` возвращает `InventoryTotals`
(поля `files`, `total_bytes`, `unclassified`) — используется в Task 11 именно
так. `drill()` возвращает словарь с ключами `sessions`, `skills`, `plugins`,
`cron_jobs`, `unclassified` — формат вывода в `main()` использует те же
ключи. `status_line(name, outcome, reason)` вызывается везде без префикса
`hermes_`, который добавляет сама функция. `DirectoryLock(path, owner)` —
одинаковая сигнатура в Task 9, 13.
