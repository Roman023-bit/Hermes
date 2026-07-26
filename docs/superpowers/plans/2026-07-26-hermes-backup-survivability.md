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
    # Only files: the repo-wide autouse fixture in tests/conftest.py puts a
    # hermes_test/ directory into every tmp_path.
    assert [item.name for item in tmp_path.iterdir() if item.is_file()] == ["STATE"]
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


def test_dot_prefixed_caches_are_excluded():
    """`du /srv/hermes/data/*` hid these: .npm and .cache held 150 MB."""
    assert excluded_by(".npm/_npx/abc/node_modules/x.js")
    assert excluded_by(".cache/uv/wheels-v6/pypi/tabulate/meta.json")
    assert excluded_by(".local/share/pki/nssdb/key4.db") is None
    assert excluded_by(".env") is None


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
    assert by_path["surprise.bin"]["type"] == "file"
    assert len(by_path["surprise.bin"]["sha256"]) == 64
    assert totals.files == 2
    assert totals.total_bytes == 6
    assert totals.unclassified == 1


def test_symlink_is_recorded_by_target_not_by_content(tmp_path):
    staging = tmp_path / "staging"
    (staging / "skills").mkdir(parents=True)
    (staging / "skills" / "real.md").write_text("body")
    (staging / "skills" / "alias.md").symlink_to("real.md")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    rows = {
        json.loads(line)["path"]: json.loads(line)
        for line in out.read_text().splitlines()
    }
    alias = rows["skills/alias.md"]
    assert alias["type"] == "symlink"
    assert alias["target"] == "real.md"
    assert "sha256" not in alias
    assert alias["classification"] == "essential"
    # The link contributes an entry but no bytes: only "body" is counted.
    assert totals.files == 2
    assert totals.total_bytes == 4


def test_broken_symlink_is_recorded_and_never_followed(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "dangling").symlink_to("nowhere/at/all")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    row = json.loads(out.read_text().splitlines()[0])
    assert row == {
        "path": "dangling",
        "type": "symlink",
        "target": "nowhere/at/all",
        "classification": "unclassified",
    }
    assert totals.files == 1


def test_symlinked_directory_is_recorded_but_not_descended(tmp_path):
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    (outside / "secret" / "leak.txt").write_text("should never be inventoried")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "escape").symlink_to(outside, target_is_directory=True)
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["path"] for row in rows] == ["escape"]
    assert rows[0]["type"] == "symlink"
    assert totals.files == 1


def test_excluded_symlink_lands_in_exclusions_with_its_target(tmp_path):
    source = tmp_path / "data"
    (source / ".npm" / "_npx" / "bin").mkdir(parents=True)
    (source / ".npm" / "_npx" / "bin" / "mcp-proxy").symlink_to("../lib/proxy.js")
    out = tmp_path / "EXCLUSIONS.jsonl"

    count = write_exclusions(source, out)

    row = json.loads(out.read_text().splitlines()[0])
    assert count == 1
    assert row["type"] == "symlink"
    assert row["target"] == "../lib/proxy.js"
    assert row["rule"] == ".npm/*"
    assert "size" not in row


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
"""What travels in the backup, what does not, and why.

Selection is "everything except the explicit exclusions", so an unknown
new file is backed up rather than silently lost. Classification is a
separate, purely descriptive step: unclassified files are counted so the
rules can be refreshed, never dropped.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path

from hermes_backup.hashing import sha256_file

EXCLUDE_RULES: tuple[str, ...] = (
    "cache/*",
    # Dot-prefixed caches: `du /srv/hermes/data/*` never listed them, so the
    # first rule set missed 150 MB of npx and uv downloads in .npm and .cache.
    ".npm/*",
    ".cache/*",
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


def _entries(root: Path):
    """Walk the tree without ever following a symlink.

    ``Path.rglob`` descends into symlinked directories, which can leave the
    tree, loop, or hash something that only looks local. Symlinks are
    recorded as themselves — dropping them would silently lose state, which
    is the one thing this backup must never do.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        linked_dirs = sorted(name for name in dirnames if (base / name).is_symlink())
        for name in linked_dirs:
            path = base / name
            yield path, path.relative_to(root).as_posix()
        dirnames[:] = sorted(set(dirnames) - set(linked_dirs))
        for name in sorted(filenames):
            path = base / name
            yield path, path.relative_to(root).as_posix()


def _describe(path: Path, rel: str, classification: str) -> dict:
    if path.is_symlink():
        # readlink, never resolve: the target may be absent, external, or a
        # directory, and none of those may be followed or hashed.
        return {
            "path": rel,
            "type": "symlink",
            "target": os.readlink(path),
            "classification": classification,
        }
    return {
        "path": rel,
        "type": "file",
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "classification": classification,
    }


def write_inventory(staging: Path, out: Path) -> InventoryTotals:
    files = total_bytes = unclassified = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _entries(staging):
            classification = classify(rel)
            record = _describe(path, rel, classification)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            files += 1
            total_bytes += record.get("size", 0)
            unclassified += classification == "unclassified"
    out.chmod(0o600)
    return InventoryTotals(
        files=files, total_bytes=total_bytes, unclassified=unclassified
    )


def write_exclusions(source: Path, out: Path) -> int:
    """Record what the exclusion rules removed, read from the live tree."""
    count = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _entries(source):
            rule = excluded_by(rel)
            if rule is None:
                continue
            if path.is_symlink():
                record = {
                    "path": rel,
                    "rule": rule,
                    "type": "symlink",
                    "target": os.readlink(path),
                    "classification": "excluded-recoverable",
                }
            else:
                # No SHA here: exclusions describe what was left behind, and
                # hashing the recoverable bulk is the expensive half.
                record = {
                    "path": rel,
                    "rule": rule,
                    "type": "file",
                    "size": path.stat().st_size,
                    "classification": "excluded-recoverable",
                }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    out.chmod(0o600)
    return count
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_inventory.py -v`
Expected: PASS, 10 тестов.

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


def test_internal_symlink_survives_the_round_trip(tmp_path):
    """Links inside the tree are legitimate state and must be preserved.

    Task 5 records them in INVENTORY.jsonl without dereferencing; the
    archive has to carry them as links, not as copies of their target.
    """
    staging = tmp_path / "staging"
    (staging / "skills").mkdir(parents=True)
    (staging / "skills" / "real.md").write_text("body")
    (staging / "skills" / "alias.md").symlink_to("real.md")
    archive = tmp_path / "essential.tar.gz"

    create(staging, archive)
    validate(archive)
    restored = tmp_path / "restored"
    extract(archive, restored)

    alias = restored / "skills" / "alias.md"
    assert alias.is_symlink()
    assert os.readlink(alias) == "real.md"


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
        # filter="tar" keeps modes and internal symlinks, which the backup
        # needs; safety comes from validate() above, not from the filter.
        # Naming it also pins behaviour across the 3.14 default change.
        tar.extractall(dest, filter="tar")  # noqa: S202 — every member validated above
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_archive.py -v`
Expected: PASS, 9 тестов.

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
- Produces: `UnstableSourceError(RuntimeError)`; `rsync_filter(rule: str) -> str`; `rsync_command(source: Path, staging: Path, *, dry_run: bool, rsync: str = "rsync") -> list[str]`; `stabilized_copy(source: Path, staging: Path, attempts: int = 4, rsync: str = "rsync") -> int` — возвращает число выполненных проходов.

**Обязательные критерии приёмки:**

1. **Владелец и группа сохраняются.** Флаги `-rlptgoH --numeric-ids`. Это `-a`
   минус `-D`: без `-o`/`-g` staging под root получил бы `root:root`, и
   восстановленные файлы перестали бы принадлежать владельцу данных (на Aeza —
   `10000:10000`). `-D` намеренно не берём: он копирует device nodes и FIFO, а
   `sha256_file` при построении inventory встанет на чтении FIFO навсегда —
   бэкап повиснет вместо того, чтобы упасть.
2. **Правила якорятся.** rsync сопоставляет неякорный шаблон с **концом** пути,
   поэтому `cache/*` удалил бы и `workspace/project/cache/`. Python-правила
   root-относительные, значит каждое корневое правило получает ведущий `/`, а
   `*/.DS_Store` превращается в `**/.DS_Store`.
3. **`--delete-excluded`**, иначе однажды скопированный, а затем исключённый
   файл останется в staging навсегда.
4. **Код возврата 24** («vanished source files») означает, что живое дерево
   изменилось под нами, — это повод повторить проход. Любой другой ненулевой
   код — немедленный отказ.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_staging.py
import pytest

import os

from hermes_backup.staging import (
    UnstableSourceError,
    changed_paths,
    rsync_command,
    rsync_filter,
    stabilized_copy,
)


def test_command_preserves_ownership_and_hardlinks():
    argv = rsync_command(Path("/srv/hermes/data"), Path("/tmp/staging"), dry_run=False)
    assert "-rlptgoH" in argv
    assert "--numeric-ids" in argv
    assert "--delete-excluded" in argv
    # -D would copy device nodes and FIFOs; hashing a FIFO would hang.
    assert "-a" not in argv


def test_root_rules_are_anchored_for_rsync():
    assert rsync_filter("cache/*") == "/cache/"
    assert rsync_filter(".npm/*") == "/.npm/"
    assert rsync_filter("state.db-*") == "/state.db-*"
    assert rsync_filter("sessions/request_dump_*.json") == "/sessions/request_dump_*.json"
    assert rsync_filter("*/.DS_Store") == "**/.DS_Store"
    assert rsync_filter("**/__pycache__/*") == "**/__pycache__/"


def test_command_carries_anchored_not_bare_rules():
    argv = rsync_command(Path("/srv/hermes/data"), Path("/tmp/staging"), dry_run=False)
    assert "--exclude=/cache/" in argv
    assert "--exclude=cache/*" not in argv


def test_root_cache_goes_but_a_nested_cache_stays(tmp_path):
    source = tmp_path / "data"
    (source / "cache").mkdir(parents=True)
    (source / "cache" / "junk.bin").write_bytes(b"0" * 32)
    (source / "workspace" / "project" / "cache").mkdir(parents=True)
    (source / "workspace" / "project" / "cache" / "important.bin").write_bytes(b"1" * 8)
    staging = tmp_path / "staging"

    stabilized_copy(source, staging)

    assert not (staging / "cache").exists()
    assert (staging / "workspace" / "project" / "cache" / "important.bin").read_bytes() == b"1" * 8


def test_stable_tree_is_copied(tmp_path):
    source = tmp_path / "data"
    (source / "cron").mkdir(parents=True)
    (source / "cron" / "jobs.json").write_text('{"jobs": []}')
    staging = tmp_path / "staging"

    passes = stabilized_copy(source, staging)

    assert (staging / "cron" / "jobs.json").exists()
    assert passes >= 1


def test_vanished_files_are_retried_not_fatal(tmp_path, monkeypatch):
    """Exit 24 means the live tree moved under us — that is churn, not failure."""
    source = tmp_path / "data"
    source.mkdir()
    (source / "keep.txt").write_text("x")
    staging = tmp_path / "staging"
    calls = {"n": 0}

    import hermes_backup.staging as module

    real = module._run_rsync

    def flaky(source_path, staging_path, dry_run, rsync):
        calls["n"] += 1
        if calls["n"] == 1:
            raise module.VanishedFiles("exit 24")
        return real(source_path, staging_path, dry_run, rsync)

    monkeypatch.setattr(module, "_run_rsync", flaky)
    assert stabilized_copy(source, staging) >= 1
    assert calls["n"] > 1


def test_other_rsync_failures_are_immediate(tmp_path, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    staging = tmp_path / "staging"

    import hermes_backup.staging as module

    def boom(*args, **kwargs):
        raise UnstableSourceError("rsync failed (23): permission denied")

    monkeypatch.setattr(module, "_run_rsync", boom)
    with pytest.raises(UnstableSourceError, match="23"):
        stabilized_copy(source, staging)


def test_source_that_keeps_changing_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    churn = source / "busy.log"
    churn.write_text("0")
    staging = tmp_path / "staging"

    import hermes_backup.staging as module

    real = module._run_rsync
    counter = {"n": 0}

    def churning(source_path, staging_path, dry_run, rsync):
        counter["n"] += 1
        churn.write_text(f"{counter['n']}")
        return real(source_path, staging_path, dry_run, rsync)

    monkeypatch.setattr(module, "_run_rsync", churning)
    with pytest.raises(UnstableSourceError, match="unstable_source"):
        stabilized_copy(source, staging, attempts=2)


def test_missing_source_is_reported(tmp_path):
    with pytest.raises(UnstableSourceError):
        stabilized_copy(tmp_path / "absent", tmp_path / "staging")


def test_informational_output_is_not_mistaken_for_churn():
    """rsync writes notes to stdout; only itemized lines mean a change."""
    output = "\n".join(
        [
            'skipping non-regular file "pipe"',
            ">f+++++++++ sessions/sessions.json",
            "*deleting   cache/junk.bin",
            "cd+++++++++ skills/",
            "",
        ]
    )
    assert changed_paths(output) == [
        ">f+++++++++ sessions/sessions.json",
        "*deleting   cache/junk.bin",
        "cd+++++++++ skills/",
    ]


def test_a_fifo_does_not_make_the_source_look_unstable(tmp_path):
    """A socket or FIFO would otherwise fail every attempt, forever."""
    source = tmp_path / "data"
    source.mkdir()
    (source / "keep.txt").write_text("payload")
    os.mkfifo(source / "pipe")
    staging = tmp_path / "staging"

    passes = stabilized_copy(source, staging)

    assert passes >= 1
    assert (staging / "keep.txt").read_text() == "payload"
    assert not (staging / "pipe").exists()
```

Файл тестов начинается с `from pathlib import Path` — он нужен в первых трёх тестах.

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

import re
import subprocess
from pathlib import Path

from hermes_backup.inventory import EXCLUDE_RULES

# An itemized line is eleven flag characters and a path: the first says how
# the entry changed, the second what kind of entry it is. rsync also writes
# notes like `skipping non-regular file "pipe"` to stdout, and counting those
# as churn would fail every single attempt for as long as a FIFO exists —
# and no retry can ever clear it.
_ITEMIZED = re.compile(r"\A(\*deleting|[<>ch.][fdLDS])")

# -rlptgoH is -a without -D: ownership and hardlinks are preserved, while
# device nodes and FIFOs are left behind — hashing a FIFO would hang the
# backup instead of failing it.
RSYNC_FLAGS = ("-rlptgoH", "--numeric-ids", "--delete", "--delete-excluded", "--itemize-changes")
VANISHED_EXIT = 24


class UnstableSourceError(RuntimeError):
    """The source kept changing, so no consistent staging copy exists."""


class VanishedFiles(RuntimeError):
    """rsync exit 24: files disappeared mid-transfer. Retryable churn."""


def rsync_filter(rule: str) -> str:
    """Translate a root-relative fnmatch rule into an rsync filter.

    rsync matches an unanchored pattern against the END of a path, so a
    bare `cache/*` would also delete workspace/project/cache. Python's
    rules are root-relative, so every rooted rule gains a leading slash.
    """
    if rule.startswith("**/"):
        return rule[:-1] if rule.endswith("/*") else rule
    if rule.startswith("*/"):
        return f"**/{rule[2:]}"
    if rule.endswith("/*"):
        return f"/{rule[:-1]}"
    return f"/{rule}"


def rsync_command(source: Path, staging: Path, *, dry_run: bool, rsync: str = "rsync") -> list[str]:
    command = [rsync, *RSYNC_FLAGS]
    command += [f"--exclude={rsync_filter(rule)}" for rule in EXCLUDE_RULES]
    if dry_run:
        command += ["--dry-run", "--checksum"]
    command += [f"{source}/", f"{staging}/"]
    return command


def _run_rsync(source: Path, staging: Path, dry_run: bool, rsync: str) -> str:
    result = subprocess.run(
        rsync_command(source, staging, dry_run=dry_run, rsync=rsync),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == VANISHED_EXIT:
        raise VanishedFiles(f"rsync exit {VANISHED_EXIT}: source files vanished")
    if result.returncode != 0:
        raise UnstableSourceError(
            f"rsync failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def changed_paths(output: str) -> list[str]:
    """Itemized changes only, dropping rsync's informational chatter."""
    return [line for line in output.splitlines() if _ITEMIZED.match(line)]


def stabilized_copy(
    source: Path, staging: Path, attempts: int = 4, rsync: str = "rsync"
) -> int:
    if not source.is_dir():
        raise UnstableSourceError(f"source is not a directory: {source}")
    staging.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            _run_rsync(source, staging, False, rsync)
            changed = changed_paths(_run_rsync(source, staging, True, rsync))
        except VanishedFiles:
            # The tree moved under us: that is exactly what the retry is for.
            continue
        if not changed:
            return attempt
    raise UnstableSourceError(
        f"unstable_source: {len(changed)} path(s) still changing after {attempts} attempts"
    )
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_staging.py -v`
Expected: PASS, 11 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/staging.py tests/backup/test_staging.py
git commit -m "feat(backup): stabilize staging copies of a live tree"
```

---

### Task 9: `locks.py` — файловый лок через `fcntl.flock`

**Files:**
- Create: `hermes_backup/locks.py`
- Test: `tests/backup/test_locks.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `LockBusy(RuntimeError)`; `LockTimeout(RuntimeError)`; `class FileLock` с `acquire(wait_seconds: int = 0)`, `release()`, поддержкой `with`; `held_by(path: Path) -> dict | None` — метаданные держателя, если лок занят, иначе `None`.

**Почему не каталог-лок.** Протокол на `mkdir` имеет две гонки: между
`mkdir()` и записью `meta.json` другой процесс видит лок без метаданных и
может счесть его stale; между проверкой мёртвого PID и `rmdir` каталог успевает
занять третий процесс, и удаление снимает чужой живой лок. `fcntl.flock` обеих
гонок не имеет: лок привязан к открытому описанию файла и снимается ядром при
смерти процесса, поэтому ручной stale-reclaim не нужен вовсе.

**Обязательные критерии приёмки:**

1. `network.lock` — постоянный обычный файл, который **никогда не удаляется**.
2. `LOCK_EX | LOCK_NB` в цикле до дедлайна; `wait_seconds=0` даёт `LockBusy`
   сразу, истёкшее положительное ожидание — `LockTimeout`.
3. `meta.json` — отдельный sidecar, записываемый атомарно. Метаданные
   информационные, источник истины — сам `flock`.
4. `release()` освобождает только дескриптор этого экземпляра: чужой объект
   `FileLock` на тот же путь снять лок не может.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_locks.py
"""Lock behaviour is only meaningful across processes, so every contention
test drives a real child process rather than a second object in-process."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_backup.locks import FileLock, LockBusy, LockTimeout, held_by

REPO = Path(__file__).resolve().parents[2]

HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from hermes_backup.locks import FileLock

    lock = FileLock(Path(sys.argv[1]), owner="child")
    lock.acquire()
    print("held", flush=True)
    time.sleep(60)
    """
)


def _holder(path: Path) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(repo=str(REPO)), str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "held"
    return process


def test_second_process_cannot_take_a_held_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        with pytest.raises(LockBusy):
            FileLock(path, owner="hermes-pull").acquire()
    finally:
        holder.kill()
        holder.wait()


def test_killing_the_owner_frees_the_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    # The kernel drops the flock with the process: no stale reclaim needed.
    with FileLock(path, owner="hermes-pull"):
        assert held_by(path)["owner"] == "hermes-pull"


def test_metadata_age_never_frees_a_live_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        meta = path.with_name(path.name + ".meta.json")
        ancient = time.time() - 86400 * 30
        os.utime(meta, (ancient, ancient))
        with pytest.raises(LockBusy):
            FileLock(path, owner="hermes-pull").acquire()
    finally:
        holder.kill()
        holder.wait()


def test_positive_wait_raises_timeout_after_waiting(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        started = time.monotonic()
        with pytest.raises(LockTimeout):
            FileLock(path, owner="hermes-pull").acquire(wait_seconds=1)
        assert time.monotonic() - started >= 1
    finally:
        holder.kill()
        holder.wait()


def test_a_foreign_instance_cannot_release_the_lock(tmp_path):
    path = tmp_path / "network.lock"
    holder = _holder(path)
    try:
        FileLock(path, owner="impostor").release()
        assert held_by(path) is not None
    finally:
        holder.kill()
        holder.wait()


def test_lock_file_survives_release(tmp_path):
    path = tmp_path / "network.lock"
    with FileLock(path, owner="hermes-pull"):
        pass
    assert path.exists()
    assert held_by(path) is None


def test_failed_metadata_write_does_not_leave_the_lock_held(tmp_path, monkeypatch):
    """A lock nobody can see is worse than no lock at all."""
    import hermes_backup.locks as module

    path = tmp_path / "network.lock"
    monkeypatch.setattr(
        module, "_meta_path", lambda p: tmp_path / "absent-dir" / "meta.json"
    )
    with pytest.raises(OSError):
        module.FileLock(path, owner="hermes-pull").acquire()
    monkeypatch.undo()
    assert held_by(path) is None


def test_double_acquire_on_one_instance_is_rejected(tmp_path):
    path = tmp_path / "network.lock"
    lock = FileLock(path, owner="hermes-pull")
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()
    finally:
        lock.release()


def test_metadata_records_pid_owner_and_time(tmp_path):
    path = tmp_path / "network.lock"
    with FileLock(path, owner="hermes-pull"):
        meta = json.loads(path.with_name(path.name + ".meta.json").read_text())
    assert meta["pid"] == os.getpid()
    assert meta["owner"] == "hermes-pull"
    assert meta["started_at"].endswith("Z")
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_locks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.locks'`

- [ ] **Step 3: Реализовать модуль**

```python
# hermes_backup/locks.py
"""One narrow uplink, two pullers: whoever holds the lock transfers.

flock is used rather than a lock directory because the kernel releases it
when the holder dies. A directory protocol has to decide whether a lock
is stale, and every such decision races: between mkdir and writing the
metadata a new lock looks abandoned, and between reading a dead pid and
removing the directory a third process can take it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_POLL_SECONDS = 5
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


class LockBusy(RuntimeError):
    """Another process holds the lock and no waiting was requested."""


class LockTimeout(RuntimeError):
    """The lock stayed held for the whole allowed wait."""


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def read_meta(path: Path) -> dict | None:
    try:
        return json.loads(_meta_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def held_by(path: Path) -> dict | None:
    """Metadata of the current holder, or None when the lock is free."""
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return read_meta(path) or {}
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


class FileLock:
    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        self._fd: int | None = None

    def acquire(self, wait_seconds: int = 0) -> "FileLock":
        if self._fd is not None:
            raise RuntimeError("lock already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The file itself is permanent; only the flock on it comes and goes.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        # O_CREAT's mode only applies when the file is new, and this one
        # outlives every run: fix the mode on an inherited file too.
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                # Only contention is retryable. EBADF, ENOLCK and friends
                # mean something is wrong with the file, not that someone
                # else holds it, and must not masquerade as a busy lock.
                if error.errno not in _CONTENDED:
                    os.close(fd)
                    raise
                holder = read_meta(self.path) or {}
                if wait_seconds <= 0:
                    os.close(fd)
                    raise LockBusy(f"held by {holder.get('owner', 'unknown')}") from None
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeout(
                        f"still held by {holder.get('owner', 'unknown')} after {wait_seconds}s"
                    ) from None
                time.sleep(min(_POLL_SECONDS, max(0.1, deadline - time.monotonic())))
        self._fd = fd
        try:
            self._write_meta()
        except OSError:
            # Never return holding a lock the caller does not know about.
            self.release()
            raise
        return self

    def _write_meta(self) -> None:
        meta = _meta_path(self.path)
        tmp = meta.with_name(f".{meta.name}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "owner": self.owner,
                    "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, meta)

    def release(self) -> None:
        # Only ever release a descriptor this instance owns: another object
        # pointing at the same path must not be able to free someone else.
        if self._fd is None:
            return
        _meta_path(self.path).unlink(missing_ok=True)  # missing after a failed write
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_locks.py -v`
Expected: PASS, 9 тестов.

- [ ] **Step 5: Коммит**

```bash
git add hermes_backup/locks.py tests/backup/test_locks.py
git commit -m "feat(backup): guard the shared uplink with an flock file lock"
```

---

### Task 10: `status.py` и `config.py` — статусы и настройки

**Files:**
- Create: `hermes_backup/status.py`, `hermes_backup/config.py`
- Test: `tests/backup/test_status.py`, `tests/backup/test_config.py`

**Interfaces:**
- Consumes: `hermes_backup.hashing.atomic_write_text`.
- Produces: `write_status(directory, name, outcome, reason="", backup_path="") -> Path`; `read_status(directory, name) -> dict | None`; `status_line(name, outcome, reason="") -> str`; `StatusError(ValueError)`. В `config.py`: константы путей `SERVER_DATA`, `SERVER_ESSENTIAL_ROOT`, `SERVER_FULL_ROOT`, `SERVER_LOCK`, `SERVER_STATUS_DIR`, `SERVER_CONFIG`, `MAC_OFFSITE_ROOT`, `MAC_STATUS_DIR`, `MAC_NETWORK_LOCK`, `MAC_CONFIG`, `REMOTE`, `SSH_KEY`; `@dataclass BackupSettings`; `load_settings(path: Path) -> BackupSettings`; `ConfigError(ValueError)`.

**Обязательный критерий приёмки: никаких новых `HERMES_*` env-переменных.**
`AGENTS.md:102` запрещает их для несекретной конфигурации, `AGENTS.md:610`
требует держать пороги и таймауты в `config.yaml`. Пути — константы с
безопасными значениями по умолчанию, переопределяются аргументами CLI (это уже
предусмотрено в Task 11 и 13–15). Поведенческие параметры читаются из секции
`backup:` файла `config.yaml`: на сервере `/srv/hermes/data/config.yaml`, на
Mac `~/.hermes/config.yaml`. Env остаётся только для настоящих секретов; путь к
SSH-ключу секретом не является и живёт константой.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/backup/test_config.py
import pytest

from hermes_backup.config import ConfigError, load_settings


def test_missing_file_yields_documented_defaults(tmp_path):
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.retention_server == 7
    assert settings.retention_mac == 7
    assert settings.retention_mac_floor == 2
    assert settings.freshness_hours == 26
    assert settings.drill_staleness_hours == 48
    assert settings.network_lock_wait_seconds == 21600


def test_config_without_a_backup_section_yields_defaults(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("model: opus
")
    assert load_settings(target).retention_mac == 7


def test_backup_section_overrides_defaults(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: 14\n  freshness_hours: 30\n")
    settings = load_settings(target)
    assert settings.retention_mac == 14
    assert settings.freshness_hours == 30
    assert settings.retention_server == 7


def test_unknown_key_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mars: 14\n")
    with pytest.raises(ConfigError, match="unknown"):
        load_settings(target)


def test_non_integer_value_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: plenty\n")
    with pytest.raises(ConfigError, match="retention_mac"):
        load_settings(target)


def test_booleans_are_not_integers(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: true\n")
    with pytest.raises(ConfigError, match="retention_mac"):
        load_settings(target)


def test_non_positive_value_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: 0\n")
    with pytest.raises(ConfigError, match="positive"):
        load_settings(target)


def test_floor_above_retention_is_rejected(tmp_path):
    """Keeping fewer copies than the floor demands would delete the floor."""
    target = tmp_path / "config.yaml"
    target.write_text("backup:\n  retention_mac: 2\n  retention_mac_floor: 5\n")
    with pytest.raises(ConfigError, match="floor"):
        load_settings(target)


def test_backup_section_must_be_a_mapping(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup: [1, 2]\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_settings(target)


def test_broken_yaml_is_rejected_not_ignored(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("backup: [unclosed\n")
    with pytest.raises(ConfigError):
        load_settings(target)
```

```python
# tests/backup/test_status.py
import json

import pytest

from hermes_backup.status import StatusError, read_status, status_line, write_status


def test_status_is_written_atomically_and_read_back(tmp_path):
    write_status(tmp_path, "essential_backup", "OK", backup_path="/srv/x/daily-1")
    record = read_status(tmp_path, "essential_backup")
    assert record["outcome"] == "OK"
    assert record["backup_path"] == "/srv/x/daily-1"
    assert record["finished_at"].endswith("Z")
    # Only files: tests/conftest.py seeds every tmp_path with hermes_test/.
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["essential_backup.json"]


def test_status_directory_is_private(tmp_path):
    target = tmp_path / "status"
    write_status(target, "freshness", "OK")
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "freshness.json").stat().st_mode & 0o777 == 0o600


def test_failure_keeps_the_reason(tmp_path):
    write_status(tmp_path, "restore_drill", "FAILED", reason="integrity_check")
    assert read_status(tmp_path, "restore_drill")["reason"] == "integrity_check"


def test_missing_status_reads_as_none(tmp_path):
    assert read_status(tmp_path, "never_ran") is None


def test_malformed_status_reads_as_none(tmp_path):
    (tmp_path / "essential_backup.json").write_text("[1, 2, 3]")
    assert read_status(tmp_path, "essential_backup") is None


def test_status_missing_required_fields_reads_as_none(tmp_path):
    (tmp_path / "freshness.json").write_text(json.dumps({"outcome": "OK"}))
    assert read_status(tmp_path, "freshness") is None


@pytest.mark.parametrize("name", ["../escape", "a/b", "with space", "", "x" * 65, "Upper"])
def test_unsafe_names_are_rejected(tmp_path, name):
    with pytest.raises(StatusError):
        write_status(tmp_path, name, "OK")


def test_unknown_outcome_is_rejected(tmp_path):
    with pytest.raises(StatusError, match="outcome"):
        write_status(tmp_path, "freshness", "MAYBE")


def test_status_line_carries_the_hermes_prefix():
    assert status_line("offsite_pull", "FAILED", "lock_timeout") == (
        "hermes_offsite_pull_FAILED lock_timeout"
    )
    assert status_line("essential_backup", "OK") == "hermes_essential_backup_OK"


def test_status_line_stays_one_line():
    """A multi-line traceback in the reason must not fake extra statuses."""
    line = status_line("restore_drill", "FAILED", "boom\nhermes_restore_drill_OK")
    assert "\n" not in line
    assert line.count("hermes_restore_drill") == 2
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `.venv/bin/python -m pytest tests/backup/test_config.py tests/backup/test_status.py -v`
Expected: FAIL — модулей `hermes_backup.config` и `hermes_backup.status` нет.

- [ ] **Step 3: Реализовать `config.py`**

```python
# hermes_backup/config.py
"""Paths and backup settings.

No new HERMES_* environment variables: AGENTS.md reserves .env for
credentials and puts every threshold and timeout in config.yaml. Paths
are constants here and are overridden by CLI arguments where a test or an
operator needs a different location.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SERVER_DATA = Path("/srv/hermes/data")
SERVER_ESSENTIAL_ROOT = Path("/srv/hermes/backups/essential")
SERVER_FULL_ROOT = Path("/srv/hermes/backups")
SERVER_LOCK = Path("/run/lock/hermes-backup.lock")
SERVER_STATUS_DIR = Path("/var/lib/hermes-backup/status")
SERVER_CONFIG = SERVER_DATA / "config.yaml"

MAC_OFFSITE_ROOT = Path("~/.local/share/hermes/offsite-backups").expanduser()
MAC_STATUS_DIR = Path("~/.local/share/hermes/status").expanduser()
MAC_NETWORK_LOCK = Path(
    "~/Library/Application Support/offsite-sync/network.lock"
).expanduser()
MAC_CONFIG = Path("~/.hermes/config.yaml").expanduser()

REMOTE = "root@138.124.108.97"
SSH_KEY = Path("~/.ssh/aeza_hermes").expanduser()

DEFAULTS: dict[str, int] = {
    "retention_server": 7,
    "retention_mac": 7,
    "retention_mac_floor": 2,
    "freshness_hours": 26,
    "drill_staleness_hours": 48,
    "network_lock_wait_seconds": 6 * 3600,
}


class ConfigError(ValueError):
    """The backup section of config.yaml is malformed."""


@dataclass(frozen=True)
class BackupSettings:
    retention_server: int
    retention_mac: int
    retention_mac_floor: int
    freshness_hours: int
    drill_staleness_hours: int
    network_lock_wait_seconds: int


def load_settings(path: Path) -> BackupSettings:
    values = dict(DEFAULTS)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"{path}: {error}") from error

    section = raw.get("backup", {}) if isinstance(raw, dict) else None
    if section is None or not isinstance(section, dict):
        raise ConfigError(f"{path}: backup must be a mapping")

    for key, value in section.items():
        if key not in DEFAULTS:
            raise ConfigError(f"{path}: unknown backup key {key!r}")
        # bool is an int in Python, and `retention_mac: true` is a typo,
        # not a setting.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: {key} expects an integer")
        if value <= 0:
            raise ConfigError(f"{path}: {key} must be positive")
        values[key] = value

    if values["retention_mac_floor"] > values["retention_mac"]:
        raise ConfigError(
            f"{path}: retention_mac_floor exceeds retention_mac — the floor would be pruned"
        )
    return BackupSettings(**values)
```

- [ ] **Step 4: Реализовать `status.py`**

```python
# hermes_backup/status.py
"""Machine-readable outcome of every run.

The summary command and, later, Telegram alerts read these files instead
of parsing free-form logs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup.hashing import atomic_write_text

OUTCOMES = frozenset({"OK", "FAILED", "SKIPPED"})
_SAFE_NAME = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_REQUIRED_FIELDS = ("name", "outcome", "reason", "backup_path", "finished_at")


class StatusError(ValueError):
    """A status name or outcome is not one this module will write."""


def status_line(name: str, outcome: str, reason: str = "") -> str:
    line = f"hermes_{name}_{outcome}"
    if not reason:
        return line
    # Keep it one line: a multi-line traceback in the reason would look
    # like several status lines to whoever greps the log.
    return f"{line} {' '.join(reason.split())}"


def write_status(
    directory: Path, name: str, outcome: str, reason: str = "", backup_path: str = ""
) -> Path:
    if not _SAFE_NAME.match(name):
        raise StatusError(f"unsafe status name: {name!r}")
    if outcome not in OUTCOMES:
        raise StatusError(f"unknown outcome: {outcome!r}")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
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
    if not _SAFE_NAME.match(name):
        raise StatusError(f"unsafe status name: {name!r}")
    try:
        record = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or any(field not in record for field in _REQUIRED_FIELDS):
        return None
    if record["outcome"] not in OUTCOMES:
        return None
    return record
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_config.py tests/backup/test_status.py -v`
Expected: PASS, 25 тестов (10 config + 15 status — `test_unsafe_names_are_rejected` параметризован шестью именами).

- [ ] **Step 6: Коммит**

```bash
git add hermes_backup/status.py hermes_backup/config.py \
        tests/backup/test_status.py tests/backup/test_config.py
git commit -m "feat(backup): read backup settings from config.yaml, not env vars"
```

---

### Task 11: `essential_backup.py` — оркестрация на Aeza

**Files:**
- Create: `hermes_backup/essential_backup.py`, `hermes_backup/snapshot_cli.py`, `deploy/beget/hermes_essential_backup.sh`, `deploy/beget/systemd/hermes-essential-backup.service`, `deploy/beget/systemd/hermes-essential-backup.timer`
- Modify: `hermes_backup/state.py` (ключ `EXCLUDED_SPECIAL_COUNT`, валидация в `format_state`), `hermes_backup/inventory.py` (спецфайлы), `tests/backup/test_state.py`, `tests/backup/test_inventory.py`
- Test: `tests/backup/test_essential_backup.py`

**Interfaces:**
- Consumes: `staging.stabilized_copy`, `sqlite_snapshot.*`, `inventory.write_inventory/write_exclusions`, `counters.*`, `state.format_state`, `archive.create/validate`, `hashing.write_sha256sums/sha256_file`, `status.write_status/status_line`, `config.load_settings`.
- Produces: `run(data, root, *, rsync="rsync", repo=None, settings=None, snapshot_runner=None) -> Path`; `owner_of(path) -> tuple[int, int]`; `require_single_owner(paths) -> tuple[int, int]`; `snapshot_command(uid, gid, data, dest, names) -> list[str]`; `main(argv=None) -> int`; коды выхода `0`, `1`, `75`.

#### Обязательные критерии приёмки

1. **`config.yaml` разбирается до публикации.** `cron/jobs.json` — через
   `count_cron_jobs`, `config.yaml` — через `yaml.safe_load` по staging-копии.
   Архив с пойманным на середине записи конфигом не публикуется никогда.
2. **`format_state` валидирует то, что пишет.** `parse_state` проверяет
   строковые значения регуляркой, `format_state` — нет; `HERMES_IMAGE_REF` и
   `HERMES_GIT_SHA` приходят из вывода `docker` и `git`, то есть извне.
3. **Снимки снимаются с понижением привилегий** через `snapshot_runner`.
4. **Спецфайлы фиксируются, а не исчезают.**

#### Правки в готовых модулях

- [ ] **Step 1: Добавить `EXCLUDED_SPECIAL_COUNT` и валидацию в `state.py`**

В `INT_KEYS` добавить `"EXCLUDED_SPECIAL_COUNT"`. `format_state` заменить на:

```python
def format_state(values: Mapping[str, int | str]) -> str:
    missing = ALL_KEYS - set(values)
    if missing:
        raise StateError(f"missing key: {sorted(missing)[0]}")
    unknown = set(values) - ALL_KEYS
    if unknown:
        raise StateError(f"unknown key: {sorted(unknown)[0]}")
    for key in sorted(STR_KEYS):
        # Values arrive from `docker inspect` and `git`: reject a bad one
        # where it is produced, not three steps later in the self-check.
        # str() is deliberately not applied — a number here means the
        # caller mixed up its keys, and coercion would hide that.
        value = values[key]
        if not isinstance(value, str) or not _SAFE_STR.match(value):
            raise StateError(f"{key} has an unsafe value")
    for key in sorted(INT_KEYS):
        if isinstance(values[key], bool) or not isinstance(values[key], int):
            raise StateError(f"{key} expects an integer")
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))
```

Тесты в `tests/backup/test_state.py`: добавить `"EXCLUDED_SPECIAL_COUNT": 0` в
фикстуру `VALID` и два теста:

```python
def test_format_rejects_an_unsafe_string_value():
    hostile = dict(VALID, HERMES_IMAGE_REF="hermes:latest\nEXPECTED_SKILLS=0")
    with pytest.raises(StateError, match="HERMES_IMAGE_REF"):
        format_state(hostile)


def test_format_rejects_a_non_integer_count():
    with pytest.raises(StateError, match="EXPECTED_SKILLS"):
        format_state(dict(VALID, EXPECTED_SKILLS="78"))


def test_format_rejects_a_number_where_a_string_belongs():
    """A bare 123 means the caller mixed up keys; coercion would hide it."""
    with pytest.raises(StateError, match="HERMES_GIT_SHA"):
        format_state(dict(VALID, HERMES_GIT_SHA=123))
```

- [ ] **Step 2: Научить `inventory.py` спецфайлам**

Спецфайлы не доезжают до staging: Task 8 копирует без `-D`, потому что
`sha256_file` на FIFO повис бы навсегда. Но исчезать молча они не должны.
Классификация делается по `lstat`, файл при этом не открывается.

```python
import stat
from dataclasses import dataclass


@dataclass(frozen=True)
class ExclusionTotals:
    files: int
    specials: int


def _kind(path: Path) -> str:
    """Classify by lstat alone: opening a FIFO would block forever."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    return "special"
```

`_describe` и `write_inventory` используют `_kind` вместо `path.is_symlink()`:
запись типа `special` получает `"type": "special"` без размера и без SHA.

`write_exclusions` переписывается так, чтобы возвращать `ExclusionTotals` и
записывать каждый спецфайл независимо от правил исключения:

```python
def write_exclusions(source: Path, out: Path) -> ExclusionTotals:
    """Record what never reached the archive, read from the live tree.

    Specials are recorded whether or not a rule matches them: rsync leaves
    them behind by design, and an unrecorded loss is exactly what this
    backup must not produce.
    """
    files = specials = 0
    with out.open("w", encoding="utf-8") as handle:
        for path, rel in _entries(source):
            kind = _kind(path)
            rule = excluded_by(rel)
            if kind == "special":
                record = {
                    "path": rel,
                    "rule": rule or "special-object",
                    "type": "special",
                    "classification": "excluded-special",
                }
                specials += 1
            elif rule is None:
                continue
            elif kind == "symlink":
                record = {
                    "path": rel,
                    "rule": rule,
                    "type": "symlink",
                    "target": os.readlink(path),
                    "classification": "excluded-recoverable",
                }
                files += 1
            else:
                # No SHA here: exclusions describe what was left behind, and
                # hashing the recoverable bulk is the expensive half.
                record = {
                    "path": rel,
                    "rule": rule,
                    "type": "file",
                    "size": path.stat().st_size,
                    "classification": "excluded-recoverable",
                }
                files += 1
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    out.chmod(0o600)
    return ExclusionTotals(files=files, specials=specials)
```

Тесты в `tests/backup/test_inventory.py` — поправить два существующих на новый
тип возврата (`write_exclusions(...).files`) и добавить:

```python
def test_fifo_is_recorded_as_special_and_never_opened(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    os.mkfifo(source / "pipe")
    out = tmp_path / "EXCLUSIONS.jsonl"

    totals = write_exclusions(source, out)

    row = json.loads(out.read_text().splitlines()[0])
    assert totals.specials == 1
    assert row["type"] == "special"
    assert row["classification"] == "excluded-special"
    assert "sha256" not in row and "size" not in row


def test_inventory_does_not_hash_a_special(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    os.mkfifo(staging / "pipe")
    out = tmp_path / "INVENTORY.jsonl"

    totals = write_inventory(staging, out)

    row = json.loads(out.read_text().splitlines()[0])
    assert row["type"] == "special"
    assert "sha256" not in row
    assert totals.files == 1
```

#### Оркестратор

- [ ] **Step 3: Написать падающий тест**

```python
# tests/backup/test_essential_backup.py
import json
import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest

from hermes_backup.essential_backup import (
    require_single_owner,
    run,
    snapshot_command,
)
from hermes_backup.sqlite_snapshot import snapshot
from hermes_backup.state import parse_state

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "beget"


def _direct_runner(uid, gid, data, dest, names):
    """Tests cannot setpriv; take the snapshots in-process instead."""
    for name in names:
        snapshot(data / name, dest / name)


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
    (data / ".env").write_text("TELEGRAM_TOKEN=x\n")
    # Secrets are 0600 on the server; the drill rejects anything wider, so a
    # fixture built under the default umask would not resemble production.
    for secret in ("auth.json", "config.yaml", ".env", "sessions/sessions.json"):
        (data / secret).chmod(0o600)

    for name in ("state.db", "kanban.db"):
        connection = sqlite3.connect(data / name)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO t (id) VALUES (1)")
        connection.commit()
        connection.close()
    return data


def _run(data, root, **kwargs):
    kwargs.setdefault("snapshot_runner", _direct_runner)
    return run(data, root, **kwargs)


def test_publishes_a_directory_with_exactly_five_files(tmp_path):
    published = _run(_fixture_tree(tmp_path), tmp_path / "essential")

    assert {item.name for item in published.iterdir()} == {
        "essential.tar.gz",
        "STATE",
        "INVENTORY.jsonl",
        "EXCLUSIONS.jsonl",
        "SHA256SUMS",
    }
    assert published.name.startswith("daily-")
    assert published.stat().st_mode & 0o777 == 0o700


def test_state_counts_match_the_fixture(tmp_path):
    published = _run(_fixture_tree(tmp_path), tmp_path / "essential")

    state = parse_state((published / "STATE").read_text())
    assert state["EXPECTED_SESSIONS"] == 2
    assert state["EXPECTED_SKILLS"] == 1
    assert state["EXPECTED_PLUGINS"] == 1
    assert state["EXPECTED_CRON_JOBS"] == 1
    assert state["EXCLUDED_SPECIAL_COUNT"] == 0
    assert state["BACKUP_FORMAT_VERSION"] == 1


def test_archive_carries_snapshots_and_drops_recoverable_files(tmp_path):
    published = _run(_fixture_tree(tmp_path), tmp_path / "essential")

    with tarfile.open(published / "essential.tar.gz") as tar:
        names = set(tar.getnames())
    assert "state.db" in names and "kanban.db" in names
    assert "state.db-wal" not in names
    assert not any(name.startswith("cache/") for name in names)
    assert not any(name.startswith("cron/output/") for name in names)
    assert not any("request_dump" in name for name in names)


def test_fifo_is_counted_in_state_and_never_archived(tmp_path):
    data = _fixture_tree(tmp_path)
    os.mkfifo(data / "pipe")

    published = _run(data, tmp_path / "essential")

    state = parse_state((published / "STATE").read_text())
    assert state["EXCLUDED_SPECIAL_COUNT"] == 1
    rows = [
        json.loads(line)
        for line in (published / "EXCLUSIONS.jsonl").read_text().splitlines()
    ]
    assert any(row["classification"] == "excluded-special" for row in rows)
    with tarfile.open(published / "essential.tar.gz") as tar:
        assert "pipe" not in set(tar.getnames())


def test_torn_config_yaml_never_reaches_the_archive(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "config.yaml").write_text("model: [unclosed\n")
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError, match="config_yaml"):
        _run(data, root)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".daily-*"))


def test_empty_config_yaml_is_rejected(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "config.yaml").write_text("")
    with pytest.raises(RuntimeError, match="config_yaml"):
        _run(data, tmp_path / "essential")


def test_a_failing_snapshot_runner_publishes_nothing(tmp_path):
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"

    def broken(uid, gid, source, dest, names):
        raise RuntimeError("snapshot_failed (1): setpriv exploded")

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        _run(data, root, snapshot_runner=broken)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".daily-*"))


def test_a_silent_snapshot_runner_is_caught(tmp_path):
    """A runner that exits zero without producing files must not pass."""
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError, match="snapshot_missing"):
        _run(data, root, snapshot_runner=lambda *args: None)

    assert not list(root.glob("daily-*"))


def test_previous_backup_survives_a_failed_run(tmp_path, monkeypatch):
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"
    first = _run(data, root)

    import hermes_backup.essential_backup as module

    monkeypatch.setattr(
        module, "create", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        _run(data, root)

    assert first.exists()
    assert (first / "SHA256SUMS").exists()


def test_missing_database_fails_before_anything_is_copied(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "kanban.db").unlink()
    root = tmp_path / "essential"

    with pytest.raises(RuntimeError, match="missing_database"):
        _run(data, root)

    assert not root.exists() or not list(root.glob(".daily-*"))


def test_split_ownership_fails_closed(tmp_path, monkeypatch):
    import hermes_backup.essential_backup as module

    first = tmp_path / "state.db"
    first.write_text("x")
    second = tmp_path / "kanban.db"
    second.write_text("x")

    real = module.owner_of
    monkeypatch.setattr(
        module,
        "owner_of",
        lambda path: (0, 0) if path.name == "kanban.db" else real(path),
    )
    with pytest.raises(RuntimeError, match="owner_mismatch"):
        require_single_owner([first, second])


def test_snapshot_child_can_traverse_the_partial_directory(tmp_path, monkeypatch):
    """A 0700 root:root parent would deny the unprivileged child."""
    import stat as stat_module

    import hermes_backup.essential_backup as module

    chowns: list[tuple[str, int, int]] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        module.os,
        "chown",
        lambda path, uid, gid: chowns.append((Path(path).name, uid, gid)),
    )
    seen: dict[str, int] = {}

    def spy_runner(uid, gid, data, dest, names):
        seen["partial"] = stat_module.S_IMODE(dest.parent.stat().st_mode)
        seen["snapshots"] = stat_module.S_IMODE(dest.stat().st_mode)
        _direct_runner(uid, gid, data, dest, names)

    published = _run(
        _fixture_tree(tmp_path), tmp_path / "essential", snapshot_runner=spy_runner
    )

    assert seen["partial"] == 0o710
    assert seen["snapshots"] == 0o700
    assert any(name.startswith(".daily-") for name, _, _ in chowns)
    # The traversal grant must not survive the snapshot step.
    assert published.stat().st_mode & 0o777 == 0o700


def test_traversal_is_revoked_even_when_the_snapshot_fails(tmp_path, monkeypatch):
    import hermes_backup.essential_backup as module

    revoked: list[str] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda *args: None)
    real_revoke = module._revoke_traversal
    monkeypatch.setattr(
        module,
        "_revoke_traversal",
        lambda partial: (revoked.append(partial.name), real_revoke(partial))[1],
    )

    def broken(uid, gid, data, dest, names):
        raise RuntimeError("snapshot_failed (1): boom")

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        _run(_fixture_tree(tmp_path), tmp_path / "essential", snapshot_runner=broken)

    assert revoked


def test_snapshot_command_drops_privileges_to_the_file_owner():
    argv = snapshot_command(
        10000, 10000, Path("/srv/hermes/data"), Path("/tmp/s"), ["state.db"]
    )
    assert argv[:4] == [
        "/usr/bin/setpriv",
        "--reuid=10000",
        "--regid=10000",
        "--clear-groups",
    ]
    assert "hermes_backup.snapshot_cli" in argv
    assert argv[-1] == "state.db"


def test_publishing_twice_in_one_second_does_not_overwrite(tmp_path, monkeypatch):
    """Two runs in the same second must not silently replace each other."""
    data = _fixture_tree(tmp_path)
    root = tmp_path / "essential"
    import hermes_backup.essential_backup as module

    frozen = "20260726T031500Z"
    monkeypatch.setattr(module, "_stamp", lambda: frozen)
    _run(data, root)
    with pytest.raises(RuntimeError, match="already_published"):
        _run(data, root)


def test_tree_bytes_does_not_follow_symlinks(tmp_path):
    import hermes_backup.essential_backup as module

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"0" * 4096)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "small.txt").write_bytes(b"0" * 10)
    (tree / "link").symlink_to(outside, target_is_directory=True)

    assert module._tree_bytes(tree) == 10


def test_service_treats_lock_skip_as_success():
    unit = (DEPLOY / "systemd" / "hermes-essential-backup.service").read_text()
    assert "SuccessExitStatus=75" in unit
    assert "UMask=0077" in unit


def test_wrapper_takes_the_shared_lock():
    wrapper = (DEPLOY / "hermes_essential_backup.sh").read_text()
    assert "/run/lock/hermes-backup.lock" in wrapper
    assert "flock -n 9" in wrapper
```

- [ ] **Step 4: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_essential_backup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.essential_backup'`

- [ ] **Step 5: Реализовать `snapshot_cli.py`**

```python
#!/usr/bin/env python3
# hermes_backup/snapshot_cli.py
"""Snapshot databases into a directory.

Runs as a separate process so the orchestrator can drop privileges for
this step alone: the databases belong to the Hermes uid, and a root
connection that creates -wal/-shm would lock Hermes out of its own data.
"""

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

- [ ] **Step 6: Реализовать `essential_backup.py`**

```python
# hermes_backup/essential_backup.py
"""Build the off-site essential backup on Aeza.

Order matters: snapshots and staging first, then STATE and INVENTORY
computed from staging (never from the live tree, which keeps changing),
then the archive, then a self-check, and only then the atomic publish.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_backup import config
from hermes_backup.archive import create, validate
from hermes_backup.config import DEFAULTS, BackupSettings, load_settings
from hermes_backup.counters import count_cron_jobs, count_plugins, count_sessions, count_skills
from hermes_backup.hashing import atomic_write_text, sha256_file, write_sha256sums
from hermes_backup.inventory import write_exclusions, write_inventory
from hermes_backup.sqlite_snapshot import (
    foreign_key_check,
    integrity_check,
    page_count,
    user_version,
)
from hermes_backup.staging import stabilized_copy
from hermes_backup.state import format_state, parse_state
from hermes_backup.status import status_line, write_status

APP_ROOT = Path("/srv/hermes/app")
DATABASES = ("state.db", "kanban.db")
SIDECARS = ("-wal", "-shm")
MAX_STAGING_BYTES = 4 * 1024**3
FREE_SPACE_MARGIN = 512 * 1024**2


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def owner_of(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_uid, info.st_gid


def database_paths(data: Path) -> list[Path]:
    """Every artefact whose owner must agree, main databases required."""
    paths = [data]
    for name in DATABASES:
        main = data / name
        if not main.exists():
            raise RuntimeError(f"missing_database: {main}")
        paths.append(main)
        paths.extend(data / f"{name}{suffix}" for suffix in SIDECARS)
    return paths


def require_single_owner(paths: Sequence[Path]) -> tuple[int, int]:
    """One owner for the whole set, or we stop.

    A split owner means a previous run already wrote as the wrong user;
    chowning a live tree under a running Hermes would be worse than
    refusing to back up.
    """
    owners = {path: owner_of(path) for path in paths if path.exists()}
    distinct = set(owners.values())
    if len(distinct) != 1:
        raise RuntimeError(f"owner_mismatch: {owners}")
    return distinct.pop()


def snapshot_command(
    uid: int, gid: int, data: Path, dest: Path, names: Sequence[str]
) -> list[str]:
    # Drop privileges for this child only: the orchestrator still needs
    # docker inspect and root-only directories.
    return [
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "/usr/bin/python3",
        "-m",
        "hermes_backup.snapshot_cli",
        "--data",
        str(data),
        "--dest",
        str(dest),
        *names,
    ]


def _setpriv_runner(
    uid: int, gid: int, data: Path, dest: Path, names: Sequence[str]
) -> None:
    result = subprocess.run(
        snapshot_command(uid, gid, data, dest, names),
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(APP_ROOT), "PATH": "/usr/bin:/bin"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"snapshot_failed ({result.returncode}): {result.stderr.strip()}"
        )


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
    """Size of regular files only, never following a symlink out of the tree."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
        for name in filenames:
            info = (base / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _grant_traversal(partial: Path, snapshots: Path, uid: int, gid: int) -> None:
    """Let the unprivileged snapshot child reach its own directory.

    The child runs as the Hermes uid, so a 0700 root:root parent would
    deny it before SQLite is even opened. 0710 with the child's group
    grants traversal and nothing else: the directory stays unreadable and
    unlistable, and the grant lasts only while the snapshot runs.
    """
    if os.geteuid() != 0:
        return
    os.chown(snapshots, uid, gid)
    snapshots.chmod(0o700)
    os.chown(partial, 0, gid)
    partial.chmod(0o710)


def _revoke_traversal(partial: Path) -> None:
    if os.geteuid() != 0:
        return
    os.chown(partial, 0, 0)
    partial.chmod(0o700)


def _validate_structured(staging: Path) -> None:
    """A file caught mid-write must never reach the archive.

    The lock stops other backups, not Hermes, so staging can hold a
    half-written config; parsing it here is the last gate before publish.
    """
    try:
        parsed = yaml.safe_load((staging / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError(f"config_yaml_unparsable: {error}") from error
    if not parsed:
        raise RuntimeError("config_yaml_empty")


def run(
    data: Path,
    root: Path,
    *,
    rsync: str = "rsync",
    repo: Path | None = None,
    settings: BackupSettings | None = None,
    snapshot_runner=None,
) -> Path:
    settings = settings or BackupSettings(**DEFAULTS)
    runner = snapshot_runner or _setpriv_runner
    paths = database_paths(data)
    uid, gid = require_single_owner(paths)

    stamp = _stamp()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    partial = root / f".daily-{stamp}.partial"
    published = root / f"daily-{stamp}"
    if published.exists():
        # Two runs inside one second would otherwise overwrite each other.
        raise RuntimeError(f"already_published: {published}")

    source_bytes = _tree_bytes(data)
    free = shutil.disk_usage(root).free
    # Staging holds a copy and the archive is written beside it.
    if free < source_bytes * 2 + FREE_SPACE_MARGIN:
        raise RuntimeError(
            f"insufficient_disk_space: free={free} needed={source_bytes * 2 + FREE_SPACE_MARGIN}"
        )

    staging = partial / "staging"
    snapshots = partial / "snapshots"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True, mode=0o700)
    try:
        stabilized_copy(data, staging, rsync=rsync)
        staging_bytes = _tree_bytes(staging)
        if staging_bytes > MAX_STAGING_BYTES:
            raise RuntimeError(f"staging_too_large: {staging_bytes}")
        if shutil.disk_usage(root).free < staging_bytes + FREE_SPACE_MARGIN:
            raise RuntimeError("insufficient_disk_space_for_archive")
        _validate_structured(staging)

        snapshots.mkdir(mode=0o700)
        _grant_traversal(partial, snapshots, uid, gid)
        try:
            runner(uid, gid, data, snapshots, DATABASES)
        finally:
            # Give the private directory back even when the child failed:
            # a group-traversable partial must not outlive the snapshot.
            _revoke_traversal(partial)
        missing = [name for name in DATABASES if not (snapshots / name).exists()]
        if missing:
            raise RuntimeError(f"snapshot_missing: {missing}")
        # The child touched the live databases: prove it left them alone.
        require_single_owner(paths)

        databases = {}
        for name in DATABASES:
            source = snapshots / name
            integrity_check(source)
            foreign_key_check(source)
            databases[name] = {
                "sha256": sha256_file(source),
                "page_count": page_count(source),
                "user_version": user_version(source),
            }
            shutil.move(str(source), str(staging / name))
        shutil.rmtree(snapshots)

        totals = write_inventory(staging, partial / "INVENTORY.jsonl")
        exclusions = write_exclusions(data, partial / "EXCLUSIONS.jsonl")

        atomic_write_text(
            partial / "STATE",
            format_state(
                {
                    "BACKUP_FORMAT_VERSION": 1,
                    "CREATED_AT": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "SOURCE_HOST": "aeza",
                    "HERMES_GIT_SHA": _git_sha(repo or APP_ROOT),
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
                    "EXCLUDED_SPECIAL_COUNT": exclusions.specials,
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
    _prune(root, settings.retention_server)
    return published


def _self_check(directory: Path) -> None:
    validate(directory / "essential.tar.gz")
    state = parse_state((directory / "STATE").read_text(encoding="utf-8"))
    recorded = sum(
        1
        for line in (directory / "EXCLUSIONS.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["classification"] == "excluded-special"
    )
    if recorded != state["EXCLUDED_SPECIAL_COUNT"]:
        raise RuntimeError(
            f"special_count_mismatch: STATE={state['EXCLUDED_SPECIAL_COUNT']} file={recorded}"
        )
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if sha256_file(directory / name) != digest:
            raise RuntimeError(f"self_check_failed: {name}")


def _prune(root: Path, keep: int) -> None:
    if keep < 1:
        return
    daily = sorted(item for item in root.glob("daily-*") if item.is_dir())
    for stale in daily[: max(0, len(daily) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=config.SERVER_DATA)
    parser.add_argument("--root", type=Path, default=config.SERVER_ESSENTIAL_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.SERVER_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.SERVER_CONFIG)
    args = parser.parse_args(argv)
    try:
        published = run(args.data, args.root, settings=load_settings(args.config))
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "essential_backup", "FAILED", reason=str(error))
        print(status_line("essential_backup", "FAILED", str(error)), file=sys.stderr)
        return 1
    state = parse_state((published / "STATE").read_text(encoding="utf-8"))
    write_status(
        args.status_dir, "essential_backup", "OK", backup_path=str(published)
    )
    print(
        status_line(
            "essential_backup",
            "OK",
            f"path={published} files={state['ESSENTIAL_FILE_COUNT']} "
            f"unclassified={state['UNCLASSIFIED_FILE_COUNT']} "
            f"specials={state['EXCLUDED_SPECIAL_COUNT']}",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Написать обёртку и systemd-юниты**

```bash
# deploy/beget/hermes_essential_backup.sh
#!/usr/bin/env bash
# Thin launcher: all behaviour lives in hermes_backup.essential_backup,
# where it is covered by pytest. Takes the shared backup lock so the full
# archive can never run at the same time.
set -euo pipefail
umask 0077

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
UMask=0077
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

- [ ] **Step 8: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup -v`
Expected: PASS — 16 тестов Task 11 плюс весь ранее написанный набор с учётом
правок в `test_state.py` и `test_inventory.py`.

- [ ] **Step 9: Коммит**

```bash
chmod +x deploy/beget/hermes_essential_backup.sh
git add hermes_backup/essential_backup.py hermes_backup/snapshot_cli.py \
        hermes_backup/state.py hermes_backup/inventory.py \
        deploy/beget/hermes_essential_backup.sh deploy/beget/systemd/ \
        tests/backup/test_essential_backup.py tests/backup/test_state.py \
        tests/backup/test_inventory.py
git commit -m "feat(backup): publish the essential backup atomically on Aeza"
```


---

### Task 12: Полный архив — Python-логика, общий лок и снимки вместо живых БД

Полный архив переезжает на ту же схему, что и essential: вся логика в
тестируемом Python, `backup.sh` становится тонким запускателем. Иначе в bash
пришлось бы воспроизвести определение владельца, `setpriv`, чтение настроек,
атомарную запись статуса и `EXIT` trap — то есть ровно то, что уже написано и
покрыто тестами.

**Files:**
- Create: `hermes_backup/full_backup.py`, `deploy/beget/systemd/hermes-full-backup.service`, `deploy/beget/systemd/hermes-full-backup.timer`
- Modify: `deploy/beget/backup.sh` (становится обёрткой), `hermes_backup/essential_backup.py` (вынести общие помощники — см. Step 1)
- Test: `tests/backup/test_full_backup.py`

**Interfaces:**
- Consumes: `essential_backup.database_paths/require_single_owner/snapshot_command/setpriv_runner`, `config.load_settings`, `status.write_status/status_line`, `sqlite_snapshot.integrity_check`.
- Produces: `hermes_backup/full_backup.py`: `run(data, backup_dir, *, settings=None, snapshot_runner=None) -> Path`; `main(argv=None) -> int`; коды выхода `0`, `1`, `75`.

**Обязательные критерии приёмки:**

1. **Снимки снимаются не от root.** Владелец `data`, обеих БД и всех
   существующих `-wal`/`-shm` определяется и сверяется до снимка; каталог
   снимков создаётся с `<uid>:<gid> 0700`; `snapshot_cli` запускается через
   `setpriv --reuid --regid --clear-groups`; после снимка владельцы
   проверяются повторно; при любом расхождении архив не публикуется.
2. **Никакого `HERMES_BACKUP_KEEP`.** Ретенция читается из
   `backup.retention_server` через `load_settings()`.
3. **Статусы по спеке.** Общий `EXIT` trap в обёртке, строки
   `hermes_full_backup_OK|FAILED|SKIPPED`, атомарный status-файл через
   `status.write_status`. При сбое предыдущий архив и ретенция не трогаются.
4. **Усиленная проверка.** `PrivateTmp=true` и `UMask=0077` в юните; тесты
   проверяют `setpriv`, pre/post owner-check, отсутствие env-настройки и
   наличие trap.

- [ ] **Step 1: Сделать помощники снимков переиспользуемыми**

В `hermes_backup/essential_backup.py` переименовать `_setpriv_runner` в
`setpriv_runner` (публичное имя, его теперь импортирует второй потребитель) и
оставить псевдоним `_setpriv_runner = setpriv_runner` не нужно — заменить
единственное использование внутри `run()`. Ничего больше не меняется:
`database_paths`, `require_single_owner`, `snapshot_command`,
`_grant_traversal`, `_revoke_traversal` уже публичны или используются только
внутри модуля.

В `tests/backup/test_essential_backup.py` ничего менять не требуется.

- [ ] **Step 2: Написать падающий тест**

```python
# tests/backup/test_full_backup.py
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest

from hermes_backup.full_backup import run
from hermes_backup.sqlite_snapshot import snapshot
from hermes_backup.status import read_status

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "beget"


def _direct_runner(uid, gid, data, dest, names):
    """Tests cannot setpriv; take the snapshots in-process instead."""
    for name in names:
        snapshot(data / name, dest / name)


def _fixture_tree(root):
    data = root / "data"
    (data / "cache").mkdir(parents=True)
    (data / "cache" / "junk.bin").write_bytes(b"0" * 32)
    (data / "sessions").mkdir()
    (data / "sessions" / "sessions.json").write_text("{}")
    (data / "config.yaml").write_text("model: opus\n")
    for name in ("state.db", "kanban.db"):
        connection = sqlite3.connect(data / name)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO t (id) VALUES (1)")
        connection.commit()
        connection.close()
    return data


def _run(data, backup_dir, **kwargs):
    kwargs.setdefault("snapshot_runner", _direct_runner)
    return run(data, backup_dir, **kwargs)


def test_archive_holds_snapshots_and_no_live_databases(tmp_path):
    data = _fixture_tree(tmp_path)
    archive = _run(data, tmp_path / "backups")

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "./state.db" in names or "state.db" in names
    assert "./kanban.db" in names or "kanban.db" in names
    assert not any(name.endswith(("-wal", "-shm")) for name in names)
    # The full archive keeps everything else, caches included.
    assert any("cache/junk.bin" in name for name in names)


def test_snapshot_in_the_archive_is_readable(tmp_path):
    data = _fixture_tree(tmp_path)
    archive = _run(data, tmp_path / "backups")

    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("state.db"))
        extracted = tar.extractfile(member).read()
    restored = tmp_path / "restored.db"
    restored.write_bytes(extracted)
    connection = sqlite3.connect(restored)
    assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    connection.close()


def test_retention_comes_from_config_not_the_environment(tmp_path, monkeypatch):
    from hermes_backup.config import DEFAULTS, BackupSettings

    monkeypatch.setenv("HERMES_BACKUP_KEEP", "1")
    data = _fixture_tree(tmp_path)
    backups = tmp_path / "backups"
    settings = BackupSettings(**{**DEFAULTS, "retention_server": 3})
    for _ in range(5):
        _run(data, backups, settings=settings)

    assert len(list(backups.glob("hermes-*.tar.gz"))) == 3


def test_retention_never_empties_the_directory(tmp_path):
    from hermes_backup.config import DEFAULTS, BackupSettings

    data = _fixture_tree(tmp_path)
    backups = tmp_path / "backups"
    settings = BackupSettings(**{**DEFAULTS, "retention_server": 1})
    _run(data, backups, settings=settings)
    _run(data, backups, settings=settings)

    assert len(list(backups.glob("hermes-*.tar.gz"))) == 1


def test_a_failing_snapshot_leaves_the_previous_archive_alone(tmp_path):
    data = _fixture_tree(tmp_path)
    backups = tmp_path / "backups"
    first = _run(data, backups)

    def broken(uid, gid, source, dest, names):
        raise RuntimeError("snapshot_failed (1): boom")

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        _run(data, backups, snapshot_runner=broken)

    assert first.exists()
    assert not list(backups.glob("*.partial"))


def test_missing_database_fails_before_any_archive_is_written(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "kanban.db").unlink()
    backups = tmp_path / "backups"

    with pytest.raises(RuntimeError, match="missing_database"):
        _run(data, backups)

    assert not backups.exists() or not list(backups.glob("hermes-*.tar.gz"))


def test_owner_mismatch_after_the_snapshot_fails_closed(tmp_path, monkeypatch):
    """The child touched the live databases: prove it left them alone."""
    import hermes_backup.full_backup as module

    data = _fixture_tree(tmp_path)
    calls = {"n": 0}
    real = module.require_single_owner

    def drifting(paths):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("owner_mismatch: after snapshot")
        return real(paths)

    monkeypatch.setattr(module, "require_single_owner", drifting)
    with pytest.raises(RuntimeError, match="owner_mismatch"):
        _run(data, tmp_path / "backups")
    assert calls["n"] == 2


def _main_args(tmp_path, data, status_dir):
    return [
        "--data", str(data),
        "--backup-dir", str(tmp_path / "backups"),
        "--status-dir", str(status_dir),
        "--config", str(tmp_path / "absent.yaml"),
    ]


def test_status_records_success(tmp_path):
    from hermes_backup.full_backup import main

    data = _fixture_tree(tmp_path)
    status_dir = tmp_path / "status"

    code = main(_main_args(tmp_path, data, status_dir), snapshot_runner=_direct_runner)

    assert code == 0
    record = read_status(status_dir, "full_backup")
    assert record["outcome"] == "OK"
    assert record["backup_path"].endswith(".tar.gz")


def test_status_records_failure(tmp_path):
    from hermes_backup.full_backup import main

    data = _fixture_tree(tmp_path)
    (data / "state.db").unlink()
    status_dir = tmp_path / "status"

    code = main(_main_args(tmp_path, data, status_dir), snapshot_runner=_direct_runner)

    assert code == 1
    record = read_status(status_dir, "full_backup")
    assert record["outcome"] == "FAILED"
    assert "missing_database" in record["reason"]


def test_record_skip_writes_a_skipped_status_and_exits_75(tmp_path):
    from hermes_backup.full_backup import main

    status_dir = tmp_path / "status"
    code = main(
        _main_args(tmp_path, tmp_path / "data", status_dir) + ["--record-skip"]
    )

    assert code == 75
    record = read_status(status_dir, "full_backup")
    assert record["outcome"] == "SKIPPED"
    assert record["reason"] == "locked"


def test_no_cli_flag_can_bypass_setpriv():
    """A production-reachable switch around privilege dropping is the one
    thing this module must not offer."""
    source = (
        Path(__file__).resolve().parents[2] / "hermes_backup" / "full_backup.py"
    ).read_text()
    assert "--in-process-snapshots" not in source


def test_snapshots_are_gone_before_the_archive_is_published(tmp_path, monkeypatch):
    """A published archive must never coexist with loose database copies."""
    import hermes_backup.full_backup as module

    seen: dict[str, bool] = {}
    real_replace = Path.replace

    def spy_replace(self, target):
        seen["snapshot_dirs"] = any(
            Path("/tmp").glob("hermes-full-snapshots-*")
        ) or bool(list(Path(tempfile.gettempdir()).glob("hermes-full-snapshots-*")))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    _run(_fixture_tree(tmp_path), tmp_path / "backups")
    assert seen["snapshot_dirs"] is False


def test_a_stray_sidecar_in_the_archive_is_rejected():
    from hermes_backup.full_backup import _require_snapshot_layout

    with pytest.raises(RuntimeError, match="sidecars"):
        _require_snapshot_layout("./config.yaml\nstate.db\nkanban.db\nstate.db-wal\n")


def test_a_duplicated_database_in_the_archive_is_rejected():
    from hermes_backup.full_backup import _require_snapshot_layout

    with pytest.raises(RuntimeError, match="copies of state.db"):
        _require_snapshot_layout("./state.db\nstate.db\nkanban.db\n")


def test_wrapper_is_a_thin_launcher_with_a_trap():
    wrapper = (DEPLOY / "backup.sh").read_text()
    assert "flock -n 9" in wrapper
    assert "/run/lock/hermes-backup.lock" in wrapper
    assert "hermes_backup.full_backup" in wrapper
    assert "trap" in wrapper
    assert "--record-skip" in wrapper
    # exec hands the process to Python: the wrapper's trap must not survive
    # to print a second, contradictory status line.
    assert "exec /usr/bin/python3" in wrapper
    assert '[ "$code" -eq 75 ]' in wrapper
    # Behaviour lives in config.yaml, not in the environment.
    assert "HERMES_BACKUP_KEEP" not in wrapper


def test_full_backup_uses_setpriv_for_snapshots():
    source = (Path(__file__).resolve().parents[2] / "hermes_backup" / "full_backup.py").read_text()
    assert "setpriv_runner" in source
    assert "require_single_owner" in source


def test_unit_is_hardened():
    unit = (DEPLOY / "systemd" / "hermes-full-backup.service").read_text()
    assert "SuccessExitStatus=75" in unit
    assert "UMask=0077" in unit
    assert "PrivateTmp=true" in unit
```

- [ ] **Step 3: Прогнать тест и убедиться, что он падает**

Run: `.venv/bin/python -m pytest tests/backup/test_full_backup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_backup.full_backup'`

- [ ] **Step 4: Реализовать `full_backup.py`**

```python
# hermes_backup/full_backup.py
"""The full local archive on Aeza: everything, with consistent databases.

This tier is the safety net for files the essential classification never
knew about, so it keeps caches and junk. What it must not keep is a live
SQLite file: the databases are replaced by snapshots taken by an
unprivileged child, exactly as the essential backup does.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.config import DEFAULTS, BackupSettings, load_settings
from hermes_backup.essential_backup import (
    DATABASES,
    database_paths,
    require_single_owner,
    setpriv_runner,
)
from hermes_backup.sqlite_snapshot import foreign_key_check, integrity_check
from hermes_backup.status import status_line, write_status


def run(
    data: Path,
    backup_dir: Path,
    *,
    settings: BackupSettings | None = None,
    snapshot_runner=None,
) -> Path:
    settings = settings or BackupSettings(**DEFAULTS)
    runner = snapshot_runner or setpriv_runner
    paths = database_paths(data)
    uid, gid = require_single_owner(paths)

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    # Microseconds, not seconds: two runs in the same second would other-
    # wise write the same name and the second would replace the first.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    archive = backup_dir / f"hermes-{stamp}.tar.gz"
    if archive.exists():
        raise RuntimeError(f"already_published: {archive}")
    # with_suffix would turn hermes-x.tar.gz into hermes-x.tar.tar.gz.partial.
    partial = Path(f"{archive}.partial")

    # PrivateTmp keeps this out of the host's /tmp; the child needs to own
    # it, so it cannot live inside the root-only backup directory.
    snapshots = Path(tempfile.mkdtemp(prefix="hermes-full-snapshots-"))
    snapshots_removed = False
    try:
        import os

        if os.geteuid() == 0:
            os.chown(snapshots, uid, gid)
        snapshots.chmod(0o700)
        runner(uid, gid, data, snapshots, DATABASES)
        missing = [name for name in DATABASES if not (snapshots / name).exists()]
        if missing:
            raise RuntimeError(f"snapshot_missing: {missing}")
        for name in DATABASES:
            integrity_check(snapshots / name)
            foreign_key_check(snapshots / name)
        # The child touched the live databases: prove it left them alone.
        require_single_owner(paths)

        result = subprocess.run(
            [
                "tar",
                "-C",
                str(data),
                "--exclude=./state.db*",
                "--exclude=./kanban.db*",
                "-czf",
                str(partial),
                ".",
                "-C",
                str(snapshots),
                *DATABASES,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # GNU tar exits 1 when a file changed while being read, which is
        # expected against a live Hermes; only >=2 is fatal.
        if result.returncode >= 2:
            raise RuntimeError(f"tar_failed ({result.returncode}): {result.stderr.strip()}")

        verify = subprocess.run(
            ["tar", "-tzf", str(partial)], capture_output=True, text=True, check=False
        )
        if verify.returncode != 0:
            raise RuntimeError("archive_unreadable")
        _require_snapshot_layout(verify.stdout)

        # Remove the snapshots before publishing, and fail if that does not
        # work: an archive must never be announced as done while a readable
        # copy of both databases is still lying around.
        shutil.rmtree(snapshots)
        if snapshots.exists():
            raise RuntimeError(f"snapshot_cleanup_failed: {snapshots}")
        snapshots_removed = True

        partial.replace(archive)
        archive.chmod(0o600)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if not snapshots_removed:
            shutil.rmtree(snapshots, ignore_errors=True)

    _prune(backup_dir, settings.retention_server)
    return archive


def _require_snapshot_layout(listing: str) -> None:
    """The archive must carry one snapshot per database and no sidecars."""
    names = [line.strip().lstrip("./") for line in listing.splitlines() if line.strip()]
    for name in DATABASES:
        if names.count(name) != 1:
            raise RuntimeError(f"archive_layout: {names.count(name)} copies of {name}")
    strays = [
        name
        for name in names
        if any(name.startswith(f"{database}-") for database in DATABASES)
    ]
    if strays:
        raise RuntimeError(f"archive_layout: live sidecars in archive: {strays}")


def _prune(backup_dir: Path, keep: int) -> None:
    # Never prune to nothing: if retention is misconfigured, keep history.
    if keep < 1:
        return
    archives = sorted(backup_dir.glob("hermes-*.tar.gz"))
    for stale in archives[: max(0, len(archives) - keep)]:
        stale.unlink(missing_ok=True)


def main(argv: list[str] | None = None, *, snapshot_runner=None) -> int:
    """Entry point.

    ``snapshot_runner`` is a keyword argument rather than a CLI flag on
    purpose: a command-line switch would be a production-reachable way to
    bypass setpriv, and only pytest has any business passing one.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=config.SERVER_DATA)
    parser.add_argument("--backup-dir", type=Path, default=config.SERVER_FULL_ROOT)
    parser.add_argument("--status-dir", type=Path, default=config.SERVER_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.SERVER_CONFIG)
    parser.add_argument(
        "--record-skip",
        action="store_true",
        help="record that the shared lock was busy and exit 75",
    )
    args = parser.parse_args(argv)
    if args.record_skip:
        write_status(args.status_dir, "full_backup", "SKIPPED", reason="locked")
        print(status_line("full_backup", "SKIPPED", "locked"))
        return 75
    try:
        archive = run(
            args.data,
            args.backup_dir,
            settings=load_settings(args.config),
            snapshot_runner=snapshot_runner,
        )
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "full_backup", "FAILED", reason=str(error))
        print(status_line("full_backup", "FAILED", str(error)), file=sys.stderr)
        return 1
    write_status(args.status_dir, "full_backup", "OK", backup_path=str(archive))
    print(status_line("full_backup", "OK", f"path={archive}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Заменить `backup.sh` на обёртку**

```bash
#!/usr/bin/env bash
# Thin launcher for the full local archive. All behaviour lives in
# hermes_backup.full_backup, where pytest covers it; this file only takes
# the shared lock and guarantees a machine-readable status line.
#
#   /srv/hermes/app/deploy/beget/backup.sh
#
# Safe to run while the hermes container is up — it does not stop it.
set -euo pipefail
umask 0077

APP=/srv/hermes/app
LOCK=/run/lock/hermes-backup.lock

# The trap only covers this wrapper failing before Python takes over: on a
# successful exec the shell — and this trap with it — is replaced, and the
# only status line comes from Python. Exit 75 is the documented "lock was
# busy" result and must not be reported as a failure.
trap 'code=$?; [ "$code" -eq 0 ] || [ "$code" -eq 75 ] ||
  echo "hermes_full_backup_FAILED wrapper exit=$code" >&2' EXIT

exec 9>"$LOCK"
if ! flock -n 9; then
  PYTHONPATH="$APP" exec /usr/bin/python3 -m hermes_backup.full_backup --record-skip "$@"
fi

PYTHONPATH="$APP" exec /usr/bin/python3 -m hermes_backup.full_backup "$@"
```

- [ ] **Step 6: Написать юниты**

```ini
# deploy/beget/systemd/hermes-full-backup.service
[Unit]
Description=Hermes full local archive
After=docker.service

[Service]
Type=oneshot
UMask=0077
PrivateTmp=true
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

- [ ] **Step 7: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup -v`
Expected: PASS — 17 тестов Task 12 плюс весь предыдущий набор.

- [ ] **Step 8: Коммит**

```bash
git add hermes_backup/full_backup.py hermes_backup/essential_backup.py \
        deploy/beget/backup.sh deploy/beget/systemd/hermes-full-backup.* \
        tests/backup/test_full_backup.py
git commit -m "fix(backup): stop tarring live SQLite files in the full archive"
```


---

### Task 13: `filevault.py` и `offsite_pull.py` — стягивание на Mac

**Files:**
- Create: `hermes_backup/filevault.py`, `hermes_backup/offsite_pull.py`, `deploy/macos/hermes_pull_offsite.sh`, `deploy/macos/com.hermes.offsite-pull.plist`
- Test: `tests/backup/test_filevault.py`, `tests/backup/test_offsite_pull.py`

**Interfaces:**
- Consumes: `locks.FileLock`, `state.parse_state`, `status.*`, `config.*`.
- Produces: `FileVaultOff(RuntimeError)`; `require_filevault(command=None) -> None`; `BACKUP_FILES: frozenset[str]`; `verify_backup(directory: Path) -> dict`; `pull(root, remote, key, remote_root=..., runner=subprocess.run) -> Path`; `check_freshness(root, max_age_hours) -> Path`; `prune(root, keep, floor) -> None`; `main(argv=None) -> int`.

**Обязательные критерии приёмки:**

1. **Структура каталога проверяется, а не подразумевается.** Ровно пять
   объектов, все — обычные файлы; имена совпадают с `BACKUP_FILES`;
   `SHA256SUMS` содержит ровно четыре строки без дубликатов, без `/` и `..` в
   именах и без неизвестных файлов; каждый digest — ровно 64 hex-символа.
2. **Свежесть считается по `CREATED_AT` из проверенного `STATE`.** Локальный
   `mtime` говорит, когда мы скачали, а не когда сняли: старый серверный архив,
   притянутый сегодня, иначе выглядел бы свежим.
3. **Коды обоих сетевых процессов проверяются.** Ненулевой SSH — отказ;
   ошибка rsync включает stderr; после прерванного переноса не остаётся
   видимого `daily-*`; `verify_backup()` выполняется до `rename`.
4. **Права.** `.daily-*.partial` и опубликованный каталог — `0700`, все пять
   файлов — `0600`; проверяется тестом после публикации.
5. **Никакого окружения в LaunchAgent.** Ни `HERMES_REPO`, ни `PYTHONPATH`:
   обёртка вычисляет корень репозитория относительно собственного пути,
   переходит в него и запускает `.venv/bin/python`.

- [ ] **Step 1: Написать падающие тесты**

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
import hashlib
import json
import plistlib
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_backup.offsite_pull import (
    BACKUP_FILES,
    check_freshness,
    prune,
    pull,
    verify_backup,
)

REPO = Path(__file__).resolve().parents[2]
PLIST = REPO / "deploy" / "macos" / "com.hermes.offsite-pull.plist"
WRAPPER = REPO / "deploy" / "macos" / "hermes_pull_offsite.sh"
STAMP = "20260726T031500Z"


def _state_text(created_at: str) -> str:
    values = {
        "BACKUP_FORMAT_VERSION": 1,
        "CREATED_AT": created_at,
        "SOURCE_HOST": "aeza",
        "HERMES_GIT_SHA": "abc1234",
        "HERMES_IMAGE_ID": "sha256:abc",
        "HERMES_IMAGE_REF": "hermes:latest",
        "STATE_DB_SHA256": "a" * 64,
        "STATE_DB_PAGE_COUNT": 10,
        "STATE_DB_USER_VERSION": 0,
        "KANBAN_DB_SHA256": "b" * 64,
        "KANBAN_DB_PAGE_COUNT": 2,
        "KANBAN_DB_USER_VERSION": 0,
        "EXPECTED_SESSIONS": 2,
        "EXPECTED_SKILLS": 78,
        "EXPECTED_PLUGINS": 3,
        "EXPECTED_CRON_JOBS": 4,
        "ESSENTIAL_FILE_COUNT": 900,
        "ESSENTIAL_TOTAL_BYTES": 1000,
        "UNCLASSIFIED_FILE_COUNT": 0,
        "EXCLUDED_SPECIAL_COUNT": 0,
    }
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def _make_backup(directory: Path, created_at: str | None = None) -> Path:
    """A well-formed backup directory, exactly as the server publishes it."""
    directory.mkdir(parents=True)
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "essential.tar.gz": b"archive-bytes",
        "STATE": _state_text(created_at).encode(),
        "INVENTORY.jsonl": b'{"path": "auth.json", "type": "file"}\n',
        "EXCLUSIONS.jsonl": b'{"path": "cache/x", "type": "file"}\n',
    }
    lines = []
    for name, blob in payload.items():
        (directory / name).write_bytes(blob)
        (directory / name).chmod(0o600)
        lines.append(f"{hashlib.sha256(blob).hexdigest()}  {name}\n")
    (directory / "SHA256SUMS").write_text("".join(sorted(lines)))
    (directory / "SHA256SUMS").chmod(0o600)
    directory.chmod(0o700)
    return directory


class _Runner:
    """Stands in for ssh and rsync, and remembers what was called."""

    def __init__(self, fixture: Path, *, ssh_code: int = 0, rsync_code: int = 0):
        self.fixture = fixture
        self.ssh_code = ssh_code
        self.rsync_code = rsync_code
        self.calls: list[str] = []

    def __call__(self, argv, **kwargs):
        program = Path(argv[0]).name
        self.calls.append(program)
        if program == "ssh":
            return subprocess.CompletedProcess(
                argv,
                self.ssh_code,
                stdout=f"daily-{STAMP}\n" if self.ssh_code == 0 else "",
                stderr="" if self.ssh_code == 0 else "ssh: connect timed out",
            )
        destination = Path(argv[-1])
        if self.rsync_code == 0:
            shutil.copytree(self.fixture, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(
            argv,
            self.rsync_code,
            stdout="",
            stderr="" if self.rsync_code == 0 else "rsync: connection unexpectedly closed",
        )


def test_pull_publishes_atomically_with_private_modes(tmp_path):
    fixture = _make_backup(tmp_path / "fixture")
    root = tmp_path / "offsite"
    runner = _Runner(fixture)

    published = pull(root, "root@host", tmp_path / "key", runner=runner)

    assert published.name == f"daily-{STAMP}"
    assert not list(root.glob(".*partial"))
    assert published.stat().st_mode & 0o777 == 0o700
    for name in BACKUP_FILES:
        assert (published / name).stat().st_mode & 0o777 == 0o600
    assert runner.calls == ["ssh", "rsync"]


def test_ssh_failure_is_reported_and_publishes_nothing(tmp_path):
    fixture = _make_backup(tmp_path / "fixture")
    root = tmp_path / "offsite"
    runner = _Runner(fixture, ssh_code=255)

    with pytest.raises(RuntimeError, match="ssh_failed"):
        pull(root, "root@host", tmp_path / "key", runner=runner)

    assert runner.calls == ["ssh"]
    assert not list(root.glob("daily-*"))


def test_rsync_failure_carries_stderr_and_leaves_no_visible_backup(tmp_path):
    fixture = _make_backup(tmp_path / "fixture")
    root = tmp_path / "offsite"
    runner = _Runner(fixture, rsync_code=12)

    with pytest.raises(RuntimeError, match="connection unexpectedly closed"):
        pull(root, "root@host", tmp_path / "key", runner=runner)

    assert not list(root.glob("daily-*"))
    assert not list(root.glob(".*partial"))


def test_a_partial_manifest_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    lines = (directory / "SHA256SUMS").read_text().splitlines()[:3]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    with pytest.raises(RuntimeError, match="manifest"):
        verify_backup(directory)


def test_path_traversal_in_the_manifest_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    text = (directory / "SHA256SUMS").read_text()
    (directory / "SHA256SUMS").write_text(text + f"{'c' * 64}  ../escape\n")

    with pytest.raises(RuntimeError, match="manifest"):
        verify_backup(directory)


def test_a_bogus_digest_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    lines = (directory / "SHA256SUMS").read_text().splitlines()
    lines[0] = "not-a-digest  STATE"
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    with pytest.raises(RuntimeError, match="manifest"):
        verify_backup(directory)


def test_an_extra_file_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    (directory / "surprise.txt").write_text("x")

    with pytest.raises(RuntimeError, match="unexpected"):
        verify_backup(directory)


def test_a_symlinked_member_is_rejected(tmp_path):
    directory = _make_backup(tmp_path / "daily-x")
    (directory / "STATE").unlink()
    (directory / "STATE").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(RuntimeError, match="regular file"):
        verify_backup(directory)


def test_freshness_uses_created_at_not_local_mtime(tmp_path):
    """A week-old archive pulled today is stale, however new the directory."""
    root = tmp_path / "offsite"
    old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _make_backup(root / f"daily-{STAMP}", created_at=old)

    with pytest.raises(RuntimeError, match="stale_backup"):
        check_freshness(root, 26)


def test_freshness_accepts_a_recent_created_at(tmp_path):
    root = tmp_path / "offsite"
    directory = _make_backup(root / f"daily-{STAMP}")
    assert check_freshness(root, 26) == directory


def test_freshness_rejects_an_empty_root(tmp_path):
    with pytest.raises(RuntimeError, match="no_backup"):
        check_freshness(tmp_path, 26)


def test_partial_directories_are_invisible(tmp_path):
    (tmp_path / f".daily-{STAMP}.partial").mkdir()
    with pytest.raises(RuntimeError, match="no_backup"):
        check_freshness(tmp_path, 26)


def test_prune_keeps_the_floor(tmp_path):
    for day in range(1, 11):
        (tmp_path / f"daily-2026072{day % 10}T0{day}1500Z").mkdir()
    prune(tmp_path, keep=7, floor=2)
    assert len(list(tmp_path.glob("daily-*"))) == 7


def test_prune_never_empties_the_directory(tmp_path):
    (tmp_path / f"daily-{STAMP}").mkdir()
    prune(tmp_path, keep=0, floor=2)
    assert len(list(tmp_path.glob("daily-*"))) == 1


def test_filevault_off_makes_no_network_call(tmp_path, monkeypatch):
    import hermes_backup.offsite_pull as module

    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "require_filevault",
        lambda: (_ for _ in ()).throw(module.FileVaultOff("filevault_off")),
    )
    monkeypatch.setattr(
        module, "pull", lambda *a, **k: calls.append("pull") or Path("/nowhere")
    )

    code = module.main(
        [
            "--root", str(tmp_path / "offsite"),
            "--status-dir", str(tmp_path / "status"),
            "--config", str(tmp_path / "absent.yaml"),
        ]
    )

    assert code == 1
    assert calls == []


def test_launch_agent_carries_no_environment():
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "com.hermes.offsite-pull"
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}
    # The wrapper finds the repository itself; env in a plist goes stale.
    assert "EnvironmentVariables" not in data
    text = PLIST.read_text()
    assert "PYTHONPATH" not in text
    assert "HERMES_REPO" not in text


def test_wrapper_locates_the_repository_relative_to_itself():
    text = WRAPPER.read_text()
    assert "BASH_SOURCE" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    assert "PYTHONPATH" not in text
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
from hermes_backup.config import ConfigError, load_settings
from hermes_backup.filevault import FileVaultOff, require_filevault
from hermes_backup.hashing import sha256_file
from hermes_backup.locks import FileLock, LockBusy, LockTimeout
from hermes_backup.state import StateError, parse_state
from hermes_backup.status import status_line, write_status

BACKUP_FILES = frozenset(
    {
        "essential.tar.gz",
        "STATE",
        "INVENTORY.jsonl",
        "EXCLUSIONS.jsonl",
        "SHA256SUMS",
    }
)
MANIFEST_NAME = "SHA256SUMS"
STAMP = re.compile(r"\Adaily-[0-9]{8}T[0-9]{6}Z\Z")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


def _ssh_command(key: Path) -> str:
    return (
        f"ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 "
        f"-o ServerAliveCountMax=12 -i {key}"
    )


def _read_text(path: Path) -> str:
    """Read a backup file as text, refusing rather than raising.

    These bytes arrived over the network: a truncated or corrupted file can
    be invalid UTF-8, and a UnicodeDecodeError escaping from here would take
    down whichever caller asked — including the status summary, whose whole
    job is to describe a broken backup instead of dying with it.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(f"unreadable {path.name}: {error}") from error


def verify_backup(directory: Path) -> dict:
    """Prove the directory is a complete, self-consistent backup.

    Everything here is checked before the directory becomes visible as a
    backup: a truncated transfer that happens to carry a valid STATE must
    not be mistaken for a copy worth restoring from.
    """
    entries = list(directory.iterdir())
    names = {entry.name for entry in entries}
    if names != set(BACKUP_FILES):
        raise RuntimeError(f"unexpected contents: {sorted(names ^ set(BACKUP_FILES))}")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise RuntimeError(f"not a regular file: {entry.name}")

    listed: set[str] = set()
    for line in _read_text(directory / MANIFEST_NAME).splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not _DIGEST.match(digest):
            raise RuntimeError(f"manifest line malformed: {line!r}")
        if "/" in name or name in {"..", "."} or name not in BACKUP_FILES - {MANIFEST_NAME}:
            raise RuntimeError(f"manifest names an unexpected file: {name!r}")
        if name in listed:
            raise RuntimeError(f"manifest lists {name!r} twice")
        listed.add(name)
        if sha256_file(directory / name) != digest:
            raise RuntimeError(f"checksum_mismatch {name}")
    if listed != BACKUP_FILES - {MANIFEST_NAME}:
        raise RuntimeError(f"manifest incomplete: missing {sorted(BACKUP_FILES - {MANIFEST_NAME} - listed)}")

    try:
        return parse_state(_read_text(directory / "STATE"))
    except StateError as error:
        raise RuntimeError(f"state_invalid {error}") from error


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
    if result.returncode != 0:
        raise RuntimeError(f"ssh_failed ({result.returncode}): {result.stderr.strip()}")
    name = result.stdout.strip()
    if not STAMP.match(name):
        raise RuntimeError(f"invalid_remote_name {name!r}")
    return name


def _apply_modes(directory: Path) -> None:
    directory.chmod(0o700)
    for item in directory.iterdir():
        item.chmod(0o600)


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
        verify_backup(published)
        return published

    partial = root / f".{name}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(mode=0o700)
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
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rsync_failed ({result.returncode}): {result.stderr.strip()}"
            )
        _apply_modes(partial)
        # Verify before the rename: nothing may become visible as a backup
        # until it has proven itself complete.
        verify_backup(partial)
        partial.rename(published)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return published


def check_freshness(root: Path, max_age_hours: int) -> Path:
    backups = sorted(item for item in root.glob("daily-*") if item.is_dir())
    if not backups:
        raise RuntimeError("no_backup")
    newest = backups[-1]
    state = verify_backup(newest)
    # CREATED_AT, not mtime: mtime says when we downloaded it, and a
    # week-old archive fetched today would look brand new.
    created = datetime.strptime(str(state["CREATED_AT"]), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(f"stale_backup age_hours={age_hours:.1f}")
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
    parser.add_argument("--config", type=Path, default=config.MAC_CONFIG)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        require_filevault()
    except (FileVaultOff, ConfigError) as error:
        write_status(args.status_dir, "offsite_pull", "FAILED", reason=str(error))
        print(status_line("offsite_pull", "FAILED", str(error)), file=sys.stderr)
        return 1

    lock = FileLock(config.MAC_NETWORK_LOCK, owner="hermes-pull")
    try:
        lock.acquire(wait_seconds=settings.network_lock_wait_seconds)
    except (LockBusy, LockTimeout) as error:
        write_status(args.status_dir, "offsite_pull", "FAILED", reason="lock_timeout")
        print(status_line("offsite_pull", "FAILED", f"lock_timeout {error}"), file=sys.stderr)
        return 1
    try:
        published = pull(args.root, config.REMOTE, config.SSH_KEY)
        prune(args.root, settings.retention_mac, settings.retention_mac_floor)
    except BaseException as error:  # noqa: BLE001 — status must always be emitted
        write_status(args.status_dir, "offsite_pull", "FAILED", reason=str(error))
        print(status_line("offsite_pull", "FAILED", str(error)), file=sys.stderr)
        return 1
    finally:
        lock.release()
    write_status(args.status_dir, "offsite_pull", "OK", backup_path=str(published))
    print(status_line("offsite_pull", "OK", f"path={published}"))

    try:
        fresh = check_freshness(args.root, settings.freshness_hours)
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
Expected: PASS, 20 тестов (3 filevault + 17 offsite_pull).

- [ ] **Step 6: Написать обёртку и LaunchAgent**

```bash
# deploy/macos/hermes_pull_offsite.sh
#!/usr/bin/env bash
# The repository root is derived from this file's own location: a path in
# a plist's environment goes stale the moment the checkout moves.
set -euo pipefail
umask 0077
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
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

- [ ] **Step 7: Коммит**

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
- Consumes: `offsite_pull.verify_backup`, `archive.extract`, `sqlite_snapshot.*`, `counters.*`, `inventory.write_inventory`, `hashing.sha256_file`, `config.load_settings`, `status.*`.
- Produces: `DrillError(RuntimeError)`; `drill(backup: Path, *, staleness_hours: int = …) -> dict`; `main(argv=None) -> int`.

**Обязательные критерии приёмки:**

1. **Никакой собственной проверки сумм.** Используется строгая
   `offsite_pull.verify_backup()`: пять обычных файлов, полный манифест без
   дубликатов и посторонних имён, настоящие digest'ы.
2. **Свежесть — по `CREATED_AT` из `STATE`**, не по `mtime` каталога.
3. **Сверяется всё, что `STATE` обещает:** `BACKUP_FORMAT_VERSION == 1`,
   счётчики, `page_count`, `user_version` и SHA-256 обеих БД, а также
   пересчитанный `INVENTORY.jsonl` — число файлов, суммарные байты и
   `unclassified`.
4. **Обязательные объекты — обычные файлы**, а не каталоги и не ссылки.
5. **Права проверяются у всех секретов:** `auth.json`, `config.yaml`, `.env*`,
   `sessions/sessions.json`.
6. **Очистка fail-closed:** успешный drill не возвращает `OK`, если временный
   каталог с секретами удалить не удалось.
7. **`load_settings()` внутри обработки ошибок:** битый конфиг даёт
   `hermes_restore_drill_FAILED`, а не traceback без статуса.
8. **Контейнер, gateway и Telegram не запускаются нигде.**

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_restore_drill.py
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_backup.essential_backup import run
from hermes_backup.hashing import write_sha256sums
from hermes_backup.restore_drill import DrillError, drill
from tests.backup.test_essential_backup import _direct_runner, _fixture_tree

REPO = Path(__file__).resolve().parents[2]
PLIST = REPO / "deploy" / "macos" / "com.hermes.restore-drill.plist"
WRAPPER = REPO / "deploy" / "macos" / "hermes_restore_drill.sh"


def _published(tmp_path: Path) -> Path:
    """A real backup, built by the real orchestrator with in-process snapshots."""
    return run(
        _fixture_tree(tmp_path),
        tmp_path / "essential",
        snapshot_runner=_direct_runner,
    )


def _rewrite(published: Path, mutate) -> Path:
    """Rebuild a published backup after mutating its extracted tree or STATE.

    Checksums are recomputed, so the result is a *valid* backup that differs
    only in what the mutation changed — otherwise every test would fail on
    the manifest instead of on the property under test.
    """
    workdir = Path(tempfile.mkdtemp())
    tree = workdir / "tree"
    with tarfile.open(published / "essential.tar.gz") as tar:
        tar.extractall(tree, filter="tar")
    mutate(tree, published)
    (published / "essential.tar.gz").unlink()
    with tarfile.open(published / "essential.tar.gz", "w:gz") as tar:
        for item in sorted(tree.rglob("*")):
            tar.add(item, arcname=item.relative_to(tree).as_posix(), recursive=False)
    (published / "essential.tar.gz").chmod(0o600)
    write_sha256sums(published)
    shutil.rmtree(workdir, ignore_errors=True)
    return published


def _set_state(published: Path, key: str, value: str) -> None:
    lines = (published / "STATE").read_text().splitlines()
    updated = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines]
    (published / "STATE").write_text("\n".join(updated) + "\n")
    write_sha256sums(published)


def test_healthy_backup_passes_and_reports_counts(tmp_path):
    summary = drill(_published(tmp_path))
    assert summary["sessions"] == 2
    assert summary["skills"] == 1
    assert summary["plugins"] == 1
    assert summary["cron_jobs"] == 1
    assert summary["unclassified"] >= 0


def test_temporary_directory_is_removed(tmp_path, monkeypatch):
    seen = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        seen["path"] = real_mkdtemp(*args, **kwargs)
        return seen["path"]

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    drill(_published(tmp_path))
    assert not Path(seen["path"]).exists()


def test_a_directory_that_cannot_be_removed_fails_the_drill(tmp_path, monkeypatch):
    """The temporary tree holds live tokens: leaving it behind is a failure."""
    import hermes_backup.restore_drill as module

    # Build the backup first: module.shutil is the shutil module itself, so
    # stubbing rmtree would also stop the orchestrator cleaning its staging.
    published = _published(tmp_path)
    monkeypatch.setattr(module.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(DrillError, match="cleanup_failed"):
        drill(published)


def test_stale_created_at_is_rejected(tmp_path):
    published = _published(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_state(published, "CREATED_AT", old)
    with pytest.raises(DrillError, match="stale_backup"):
        drill(published)


def test_local_mtime_does_not_make_an_old_backup_look_fresh(tmp_path):
    published = _published(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_state(published, "CREATED_AT", old)
    os.utime(published, None)  # touched right now
    with pytest.raises(DrillError, match="stale_backup"):
        drill(published)


def test_checksum_mismatch_is_rejected(tmp_path):
    published = _published(tmp_path)
    (published / "STATE").write_text("BACKUP_FORMAT_VERSION=1\n")
    with pytest.raises(DrillError, match="checksum|manifest|state"):
        drill(published)


def test_an_extra_file_in_the_directory_is_rejected(tmp_path):
    published = _published(tmp_path)
    (published / "surprise.txt").write_text("x")
    with pytest.raises(DrillError, match="unexpected"):
        drill(published)


def test_unknown_backup_format_version_is_rejected(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "BACKUP_FORMAT_VERSION", "2")
    with pytest.raises(DrillError, match="format_version"):
        drill(published)


def test_corrupt_database_is_caught(tmp_path):
    def break_db(tree: Path, published: Path) -> None:
        (tree / "state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    published = _rewrite(_published(tmp_path), break_db)
    with pytest.raises(DrillError, match="integrity|sha256"):
        drill(published)


def test_database_sha_mismatch_is_caught(tmp_path):
    """A readable database that is not the one STATE recorded is a failure."""

    def rewrite_db(tree: Path, published: Path) -> None:
        connection = sqlite3.connect(tree / "kanban.db")
        connection.execute("INSERT INTO t (id) VALUES (99)")
        connection.commit()
        connection.close()

    published = _rewrite(_published(tmp_path), rewrite_db)
    with pytest.raises(DrillError, match="KANBAN_DB_SHA256|page_count"):
        drill(published)


def test_counter_mismatch_is_caught(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "EXPECTED_SKILLS", "99")
    with pytest.raises(DrillError, match="EXPECTED_SKILLS"):
        drill(published)


def test_inventory_totals_are_recomputed(tmp_path):
    published = _published(tmp_path)
    _set_state(published, "ESSENTIAL_FILE_COUNT", "9999")
    with pytest.raises(DrillError, match="ESSENTIAL_FILE_COUNT"):
        drill(published)


def test_zero_cron_jobs_is_valid(tmp_path):
    data = _fixture_tree(tmp_path)
    (data / "cron" / "jobs.json").write_text('{"jobs": []}')
    published = run(data, tmp_path / "essential", snapshot_runner=_direct_runner)
    assert drill(published)["cron_jobs"] == 0


def test_a_required_path_that_is_not_a_regular_file_is_rejected(tmp_path):
    def replace_with_directory(tree: Path, published: Path) -> None:
        (tree / "auth.json").unlink()
        (tree / "auth.json").mkdir()

    published = _rewrite(_published(tmp_path), replace_with_directory)
    with pytest.raises(DrillError, match="not_a_regular_file|missing_required"):
        drill(published)


def test_world_readable_secret_is_rejected(tmp_path):
    def loosen(tree: Path, published: Path) -> None:
        (tree / "auth.json").chmod(0o644)

    published = _rewrite(_published(tmp_path), loosen)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        drill(published)


def test_world_readable_env_file_is_rejected(tmp_path):
    def loosen(tree: Path, published: Path) -> None:
        # Loosen the existing .env rather than adding one: a new file would
        # change the inventory totals and fail that check first.
        (tree / ".env").chmod(0o644)

    published = _rewrite(_published(tmp_path), loosen)
    with pytest.raises(DrillError, match="permissions_too_wide"):
        drill(published)


def test_broken_config_yields_a_failed_status_not_a_traceback(tmp_path):
    from hermes_backup.restore_drill import main

    published = _published(tmp_path)
    broken = tmp_path / "config.yaml"
    broken.write_text("backup: [unclosed\n")
    status_dir = tmp_path / "status"

    code = main(
        [
            "--backup", str(published),
            "--status-dir", str(status_dir),
            "--config", str(broken),
        ]
    )

    assert code == 1
    from hermes_backup.status import read_status

    assert read_status(status_dir, "restore_drill")["outcome"] == "FAILED"


def test_drill_makes_no_network_or_container_calls(tmp_path):
    """Stub docker/ssh/curl so any call aborts, then run the real drill."""
    published = _published(tmp_path)
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name in ("docker", "ssh", "curl", "rsync"):
        stub = stub_dir / name
        stub.write_text('#!/bin/sh\necho "forbidden call: $0" >&2\nexit 99\n')
        stub.chmod(0o755)

    env = dict(os.environ, PATH=f"{stub_dir}:/usr/bin:/bin")
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [
            sys.executable, "-m", "hermes_backup.restore_drill",
            "--backup", str(published),
            "--status-dir", str(tmp_path / "status"),
            "--config", str(tmp_path / "absent.yaml"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "forbidden call" not in result.stderr


def test_drill_runs_on_sunday_morning():
    data = plistlib.loads(PLIST.read_bytes())
    assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 11, "Minute": 0}
    assert "EnvironmentVariables" not in data
    assert "PYTHONPATH" not in PLIST.read_text()


def test_wrapper_locates_the_repository_relative_to_itself():
    text = WRAPPER.read_text()
    assert "BASH_SOURCE" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    assert "HERMES_REPO" not in text
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

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
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermes_backup import config
from hermes_backup.archive import ArchiveError, extract
from hermes_backup.config import DEFAULTS, ConfigError, load_settings
from hermes_backup.counters import (
    CounterError,
    count_cron_jobs,
    count_plugins,
    count_sessions,
    count_skills,
)
from hermes_backup.hashing import sha256_file
from hermes_backup.inventory import write_inventory
from hermes_backup.offsite_pull import verify_backup
from hermes_backup.sqlite_snapshot import (
    SnapshotError,
    foreign_key_check,
    integrity_check,
    page_count,
    user_version,
)
from hermes_backup.status import status_line, write_status

SUPPORTED_FORMAT = 1
REQUIRED = ("auth.json", "config.yaml", "state.db", "kanban.db", "cron/jobs.json")
SECRETS = ("auth.json", "config.yaml", "sessions/sessions.json")
DATABASES = {
    "state.db": ("STATE_DB_SHA256", "STATE_DB_PAGE_COUNT", "STATE_DB_USER_VERSION"),
    "kanban.db": ("KANBAN_DB_SHA256", "KANBAN_DB_PAGE_COUNT", "KANBAN_DB_USER_VERSION"),
}


class DrillError(RuntimeError):
    """The backup failed a restore check."""


def _check_age(state: dict, staleness_hours: int) -> None:
    # CREATED_AT, not mtime: mtime says when the copy landed here, so an
    # old server archive pulled today would look brand new.
    try:
        created = datetime.strptime(
            str(state["CREATED_AT"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError as error:
        # An unparsable timestamp is a failed drill, not a traceback: the
        # status file must say what happened.
        raise DrillError(f"created_at_invalid {state['CREATED_AT']!r}") from error
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_hours > staleness_hours:
        raise DrillError(f"stale_backup age_hours={age_hours:.1f}")


def _require_regular_files(tree: Path) -> None:
    for name in REQUIRED:
        path = tree / name
        if not path.exists():
            raise DrillError(f"missing_required {name}")
        if path.is_symlink() or not path.is_file():
            raise DrillError(f"not_a_regular_file {name}")


def _check_databases(tree: Path, state: dict) -> None:
    for name, (sha_key, pages_key, version_key) in DATABASES.items():
        path = tree / name
        try:
            integrity_check(path)
            foreign_key_check(path)
        except SnapshotError as error:
            raise DrillError(f"integrity {name}: {error}") from error
        actual_sha = sha256_file(path)
        if actual_sha != state[sha_key]:
            raise DrillError(f"{sha_key} mismatch: {actual_sha} != {state[sha_key]}")
        if page_count(path) != state[pages_key]:
            raise DrillError(f"{pages_key} expected {state[pages_key]}")
        if user_version(path) != state[version_key]:
            raise DrillError(f"{version_key} expected {state[version_key]}")


def _check_counts(tree: Path, state: dict) -> dict:
    try:
        counts = {
            "sessions": count_sessions(tree / "sessions" / "sessions.json"),
            "skills": count_skills(tree / "skills"),
            "plugins": count_plugins(tree / "plugins"),
            "cron_jobs": count_cron_jobs(tree / "cron" / "jobs.json"),
        }
    except CounterError as error:
        raise DrillError(f"counter {error}") from error
    for key, state_key in (
        ("sessions", "EXPECTED_SESSIONS"),
        ("skills", "EXPECTED_SKILLS"),
        ("plugins", "EXPECTED_PLUGINS"),
        ("cron_jobs", "EXPECTED_CRON_JOBS"),
    ):
        if counts[key] != state[state_key]:
            raise DrillError(f"{state_key} expected {state[state_key]}, found {counts[key]}")
    return counts


def _check_inventory(backup: Path, tree: Path, workdir: Path, state: dict) -> None:
    """Recount the tree and compare it with what the backup claims.

    Totals alone are not enough: a substituted INVENTORY.jsonl can keep the
    same file count and byte total while lying about every checksum, so the
    recorded rows are compared with the recomputed ones entry by entry.
    """
    recomputed_path = workdir / "inventory-recomputed.jsonl"
    totals = write_inventory(tree, recomputed_path)
    for actual, state_key in (
        (totals.files, "ESSENTIAL_FILE_COUNT"),
        (totals.total_bytes, "ESSENTIAL_TOTAL_BYTES"),
        (totals.unclassified, "UNCLASSIFIED_FILE_COUNT"),
    ):
        if actual != state[state_key]:
            raise DrillError(f"{state_key} expected {state[state_key]}, found {actual}")


def _secret_paths(tree: Path):
    for name in SECRETS:
        path = tree / name
        if path.exists():
            yield path
    yield from sorted(tree.glob(".env*"))


def _require_private_modes(tree: Path) -> None:
    for path in _secret_paths(tree):
        mode = stat.S_IMODE(path.stat().st_mode)
        # Anything outside owner read/write is wrong for a secret, execute
        # included: 0700 is not "no wider than 0600", it is a different mode
        # nothing in this tree should ever have.
        if mode & ~0o600:
            raise DrillError(f"permissions_too_wide {path.relative_to(tree)} {mode:o}")


def drill(
    backup: Path, *, staleness_hours: int = DEFAULTS["drill_staleness_hours"]
) -> dict:
    try:
        state = verify_backup(backup)
    except RuntimeError as error:
        raise DrillError(str(error)) from error
    if int(state["BACKUP_FORMAT_VERSION"]) != SUPPORTED_FORMAT:
        raise DrillError(f"format_version {state['BACKUP_FORMAT_VERSION']} unsupported")
    _check_age(state, staleness_hours)

    workdir = Path(tempfile.mkdtemp(prefix="hermes-drill-"))
    try:
        tree = workdir / "tree"
        try:
            extract(backup / "essential.tar.gz", tree)
        except ArchiveError as error:
            raise DrillError(f"archive_unsafe {error}") from error

        _require_regular_files(tree)
        _check_databases(tree, state)

        try:
            parsed_config = yaml.safe_load((tree / "config.yaml").read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise DrillError(f"config_unparsable {error}") from error
        if not parsed_config:
            raise DrillError("config_empty")

        counts = _check_counts(tree, state)
        _check_inventory(backup, tree, workdir, state)
        _require_private_modes(tree)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    # The extracted tree holds live tokens: a drill that cannot remove it
    # has not finished, however healthy the backup turned out to be.
    shutil.rmtree(workdir, ignore_errors=True)
    if workdir.exists():
        raise DrillError(f"cleanup_failed: {workdir}")
    return {**counts, "unclassified": int(state["UNCLASSIFIED_FILE_COUNT"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=config.MAC_OFFSITE_ROOT)
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--status-dir", type=Path, default=config.MAC_STATUS_DIR)
    parser.add_argument("--config", type=Path, default=config.MAC_CONFIG)
    args = parser.parse_args(argv)

    backup = args.backup
    try:
        settings = load_settings(args.config)
        if backup is None:
            candidates = sorted(item for item in args.root.glob("daily-*") if item.is_dir())
            if not candidates:
                raise DrillError("no_backup")
            backup = candidates[-1]
        summary = drill(backup, staleness_hours=settings.drill_staleness_hours)
    except (DrillError, ConfigError, OSError) as error:
        write_status(
            args.status_dir,
            "restore_drill",
            "FAILED",
            reason=str(error),
            backup_path=str(backup) if backup else "",
        )
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
Expected: PASS, 23 теста.

- [ ] **Step 5: Написать обёртку и LaunchAgent**

```bash
# deploy/macos/hermes_restore_drill.sh
#!/usr/bin/env bash
# The repository root is derived from this file's own location: a path in
# a plist's environment goes stale the moment the checkout moves.
set -euo pipefail
umask 0077
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
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

- [ ] **Step 6: Коммит**

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
- Consumes: `status.read_status`, `offsite_pull.verify_backup`, `locks.held_by`, `config.*`.
- Produces: `summary(root: Path, status_dir: Path, lock: Path) -> str`; `main(argv=None) -> int`.

**Обязательные критерии приёмки:**

1. **Возраст — из `STATE.CREATED_AT`**, не из `mtime`: сводка должна показывать
   возраст самого бэкапа, а не времени его загрузки.
2. **Свежайший каталог проверяется через `verify_backup()`.** Непригодная копия
   должна называться непригодной, а не показываться как обычная.
3. **`UNCLASSIFIED_FILE_COUNT` выводится** — этого требует спека, это сигнал
   обновить классификацию.
4. **Обёртка через `BASH_SOURCE`**, без `HERMES_REPO`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/backup/test_backup_status.py
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_backup.backup_status import summary
from hermes_backup.locks import FileLock
from hermes_backup.status import write_status
from tests.backup.test_offsite_pull import _make_backup

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "deploy" / "macos" / "hermes_backup_status.sh"
STAMP = "20260726T031500Z"


def test_summary_reports_every_component(tmp_path):
    root = tmp_path / "offsite"
    _make_backup(root / f"daily-{STAMP}")
    status_dir = tmp_path / "status"
    write_status(status_dir, "offsite_pull", "OK")
    write_status(status_dir, "freshness", "OK")
    write_status(status_dir, "restore_drill", "FAILED", reason="checksum_mismatch STATE")

    text = summary(root, status_dir, tmp_path / "network.lock")

    assert f"daily-{STAMP}" in text
    assert "restore_drill: FAILED" in text
    assert "checksum_mismatch" in text
    assert "network lock: free" in text


def test_age_comes_from_created_at_not_from_the_download_time(tmp_path):
    """A week-old archive pulled a minute ago is a week old."""
    root = tmp_path / "offsite"
    old = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    directory = _make_backup(root / f"daily-{STAMP}", created_at=old)
    os.utime(directory, None)

    text = summary(root, tmp_path / "status", tmp_path / "network.lock")

    assert "168." in text or "167." in text
    assert "0.0 h" not in text


def test_unclassified_count_is_shown(tmp_path):
    root = tmp_path / "offsite"
    _make_backup(root / f"daily-{STAMP}")
    text = summary(root, tmp_path / "status", tmp_path / "network.lock")
    assert "unclassified files: 0" in text


def test_an_unusable_backup_is_named_as_such(tmp_path):
    root = tmp_path / "offsite"
    directory = _make_backup(root / f"daily-{STAMP}")
    (directory / "surprise.txt").write_text("x")

    text = summary(root, tmp_path / "status", tmp_path / "network.lock")

    assert "UNUSABLE" in text
    assert "unexpected" in text


def test_missing_components_are_named_not_hidden(tmp_path):
    text = summary(tmp_path / "offsite", tmp_path / "status", tmp_path / "network.lock")
    assert "no backups" in text
    assert "offsite_pull: never ran" in text


def test_held_lock_is_reported_with_owner(tmp_path):
    lock = tmp_path / "network.lock"
    with FileLock(lock, owner="kf-pull"):
        text = summary(tmp_path / "offsite", tmp_path / "status", lock)
    assert "kf-pull" in text
    assert "held by" in text


def test_wrapper_locates_the_repository_relative_to_itself():
    text = WRAPPER.read_text()
    assert "BASH_SOURCE" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    assert "HERMES_REPO" not in text
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
from datetime import datetime, timezone
from pathlib import Path

from hermes_backup import config
from hermes_backup.locks import held_by
from hermes_backup.offsite_pull import verify_backup
from hermes_backup.status import read_status

COMPONENTS = ("offsite_pull", "freshness", "restore_drill")


def _describe_latest(root: Path) -> list[str]:
    backups = (
        sorted(item for item in root.glob("daily-*") if item.is_dir())
        if root.exists()
        else []
    )
    if not backups:
        return ["latest backup: no backups"]
    newest = backups[-1]
    try:
        state = verify_backup(newest)
    except RuntimeError as error:
        return [f"latest backup: {newest.name} — UNUSABLE: {error}"]
    try:
        created = datetime.strptime(
            str(state["CREATED_AT"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return [f"latest backup: {newest.name} — UNUSABLE: created_at_invalid"]
    # Age of the backup, not of the download: a week-old archive pulled a
    # minute ago is a week old.
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    return [
        f"latest backup: {newest.name} ({age_hours:.1f} h old, {len(backups)} kept)",
        f"unclassified files: {state['UNCLASSIFIED_FILE_COUNT']}",
    ]


def summary(root: Path, status_dir: Path, lock: Path) -> str:
    lines = _describe_latest(root)

    for name in COMPONENTS:
        record = read_status(status_dir, name)
        if record is None:
            lines.append(f"{name}: never ran")
            continue
        reason = f" — {record['reason']}" if record.get("reason") else ""
        lines.append(f"{name}: {record['outcome']} at {record['finished_at']}{reason}")

    # Existence proves nothing: the lock file is permanent. Ask flock.
    holder = held_by(lock)
    if holder is None:
        lines.append("network lock: free")
    elif holder:
        lines.append(
            f"network lock: held by {holder.get('owner')} since {holder.get('started_at')}"
        )
    else:
        lines.append("network lock: held (metadata unreadable)")
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
# The repository root is derived from this file's own location, exactly as
# the pull and drill wrappers do.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
exec "$REPO/.venv/bin/python" -m hermes_backup.backup_status "$@"
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m pytest tests/backup/test_backup_status.py -v`
Expected: PASS, 7 тестов.

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

**Обязательный критерий приёмки: валидируется весь `STATE`, а не одна строка.**
Неизвестный ключ, повтор, malformed-строка и отсутствие любого из трёх
обязательных ключей отвергаются — даже если запрошен другой ключ. Иначе
вредоносное значение спокойно доедет в файле рядом с тем, что мы читаем.

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
VALID = "EXPECTED_DOCUMENTS=96\nEXPECTED_CHUNKS=621\nEXPECTED_POINTS=621\n"


def _run(args, **kwargs):
    return subprocess.run(
        [sys.executable, str(PARSER), *args], capture_output=True, text=True, **kwargs
    )


def test_drill_never_sources_state():
    text = DRILL.read_text()
    assert 'source "$latest/STATE"' not in text
    assert "source $latest/STATE" not in text
    assert "state_parser.py" in text


def test_parser_returns_a_whitelisted_value(tmp_path):
    state = tmp_path / "STATE"
    state.write_text(VALID)
    result = _run(["--key", "EXPECTED_DOCUMENTS", str(state)])
    assert result.returncode == 0
    assert result.stdout.strip() == "96"


def test_parser_rejects_unknown_key_request(tmp_path):
    state = tmp_path / "STATE"
    state.write_text(VALID)
    assert _run(["--key", "EVIL", str(state)]).returncode != 0


def test_an_unknown_key_anywhere_in_the_file_is_rejected(tmp_path):
    """The whole file is validated: a hostile line next to the one we read
    must not travel along unnoticed."""
    state = tmp_path / "STATE"
    state.write_text(VALID + "EVIL=$(rm -rf /)\n")
    result = _run(["--key", "EXPECTED_DOCUMENTS", str(state)])
    assert result.returncode != 0
    assert "unknown key" in result.stderr


def test_command_substitution_is_never_executed(tmp_path):
    canary = tmp_path / "canary"
    canary.write_text("intact")
    state = tmp_path / "STATE"
    state.write_text(f"EXPECTED_DOCUMENTS=$(rm -f {canary})\nEXPECTED_CHUNKS=1\nEXPECTED_POINTS=1\n")
    assert _run(["--key", "EXPECTED_DOCUMENTS", str(state)]).returncode != 0
    assert canary.read_text() == "intact"


def test_duplicate_key_is_rejected(tmp_path):
    state = tmp_path / "STATE"
    state.write_text(VALID + "EXPECTED_POINTS=999\n")
    result = _run(["--key", "EXPECTED_POINTS", str(state)])
    assert result.returncode != 0
    assert "duplicate" in result.stderr


def test_a_missing_required_key_is_rejected(tmp_path):
    state = tmp_path / "STATE"
    state.write_text("EXPECTED_DOCUMENTS=96\nEXPECTED_CHUNKS=621\n")
    result = _run(["--key", "EXPECTED_DOCUMENTS", str(state)])
    assert result.returncode != 0
    assert "missing" in result.stderr


def test_a_malformed_line_is_rejected(tmp_path):
    state = tmp_path / "STATE"
    state.write_text(VALID + "just-some-noise\n")
    assert _run(["--key", "EXPECTED_DOCUMENTS", str(state)]).returncode != 0


def test_a_non_integer_value_is_rejected(tmp_path):
    state = tmp_path / "STATE"
    state.write_text("EXPECTED_DOCUMENTS=many\nEXPECTED_CHUNKS=621\nEXPECTED_POINTS=621\n")
    assert _run(["--key", "EXPECTED_DOCUMENTS", str(state)]).returncode != 0


def test_corrupted_bytes_are_rejected(tmp_path):
    state = tmp_path / "STATE"
    state.write_bytes(b"EXPECTED_DOCUMENTS=96\n\xff\xfe\n")
    assert _run(["--key", "EXPECTED_DOCUMENTS", str(state)]).returncode != 0
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
never sourced by the shell. The whole file is validated, not just the
requested line: a hostile value sitting next to the one we want must not
travel along unnoticed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED = ("EXPECTED_DOCUMENTS", "EXPECTED_CHUNKS", "EXPECTED_POINTS")
INTEGER = re.compile(r"\A[0-9]+\Z")


class StateError(ValueError):
    """STATE is malformed, incomplete, or carries an unexpected key."""


def parse_state(text: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise StateError(f"line {number}: expected KEY=VALUE")
        if key not in REQUIRED:
            raise StateError(f"line {number}: unknown key {key!r}")
        if key in parsed:
            raise StateError(f"line {number}: duplicate key {key!r}")
        if not INTEGER.match(value):
            raise StateError(f"line {number}: {key} expects an integer")
        parsed[key] = int(value)
    missing = set(REQUIRED) - set(parsed)
    if missing:
        raise StateError(f"missing key: {sorted(missing)[0]}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.key not in REQUIRED:
        print(f"state_parser_FAILED unknown key {args.key}", file=sys.stderr)
        return 2
    try:
        values = parse_state(args.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        print(f"state_parser_FAILED unreadable: {error}", file=sys.stderr)
        return 2
    except StateError as error:
        print(f"state_parser_FAILED {error}", file=sys.stderr)
        return 2
    print(values[args.key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Заменить `source` в `restore_drill.sh`**

Строку `source "$latest/STATE"` заменить на:

```bash
# STATE comes from the backup directory: parse it, never execute it.
PARSER="$ROOT/app/scripts/state_parser.py"
EXPECTED_DOCUMENTS="$(python3 "$PARSER" --key EXPECTED_DOCUMENTS "$latest/STATE")"
EXPECTED_CHUNKS="$(python3 "$PARSER" --key EXPECTED_CHUNKS "$latest/STATE")"
EXPECTED_POINTS="$(python3 "$PARSER" --key EXPECTED_POINTS "$latest/STATE")"
```

- [ ] **Step 5: Проверить синтаксис и прогнать тесты**

```bash
cd /Users/romanmizanov/Documents/BD/knowledge-factory
bash -n scripts/restore_drill.sh
uv run pytest tests/test_restore_drill_state.py -v
uv run pytest -q
```

Expected: `bash -n` молчит; 10 новых тестов проходят; полный набор — 219 passed,
1 skipped (209 прежних + 10 новых).

- [ ] **Step 6: Локальный коммит**

На Aeza пока не разворачивать: установка исправленного скрипта входит в Task 18.

```bash
cd /Users/romanmizanov/Documents/BD/knowledge-factory
git add scripts/state_parser.py scripts/restore_drill.sh tests/test_restore_drill_state.py
git commit -m "fix(restore): parse STATE instead of sourcing it as root"
```

### Task 17: Knowledge Factory — FileVault-гейт и общий сетевой лок

**Files:**
- Create: `scripts/offsite_lock.py`
- Modify: `scripts/pull_backups_from_aeza.sh`
- Test: `tests/test_pull_filevault_gate.py`

**Interfaces:**
- Consumes: тот же файл `~/Library/Application Support/offsite-sync/network.lock`, что и Hermes. Протокол общий и не зависит от реализации: `fcntl.flock(LOCK_EX)` на постоянном файле, `meta.json` рядом — информационный sidecar. KF держит лок из Python-обёртки, запускающей скрипт дочерним процессом; Hermes — через `hermes_backup.locks.FileLock`.
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

- [ ] **Step 3: Добавить гейт и переиспользовать общий flock**

Вставить сразу после `set -euo pipefail`:

```bash
# Backups land beside the Hermes off-site copy, which carries live
# tokens. Never write either onto an unencrypted disk.
if ! fdesetup isactive >/dev/null 2>&1; then
  echo "offsite_pull_FAILED filevault_off" >&2
  exit 1
fi

# One narrow uplink, two pullers. The lock is the same fcntl.flock file
# Hermes uses, held by a wrapper that runs this script as its child, so
# the kernel frees it if we die. macOS has no flock(1), hence Python.
if [ -z "${KF_PULL_LOCKED:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  exec env KF_PULL_LOCKED=1 python3 "$SCRIPT_DIR/offsite_lock.py" \
    --owner kf-pull \
    --wait 21600 \
    -- "${BASH_SOURCE[0]}" "$@"
fi
```

`SCRIPT_DIR` — каталог самого скрипта, поэтому обёртка берётся оттуда же, куда
Task 18 её установит.

Создать `scripts/offsite_lock.py`:

```python
#!/usr/bin/env python3
"""Run a command while holding the shared off-site uplink lock.

The same lock file guards the Hermes pull, which takes it through
fcntl.flock directly. flock is used rather than a lock directory because
the kernel releases it when the holder dies, so a killed pull never
leaves the uplink blocked. macOS ships no flock(1).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Constant, not an environment variable: the path is behaviour, not a
# secret, and tests point elsewhere with --lock.
DEFAULT_LOCK = Path.home() / "Library/Application Support/offsite-sync/network.lock"
CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


def _acquire(lock: Path, wait_seconds: int) -> int:
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    # O_CREAT's mode applies only to a new file, and this one is permanent.
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as error:
            # Only contention is retryable: EBADF or ENOLCK mean the file is
            # wrong, not that somebody else holds it.
            if error.errno not in CONTENDED:
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                print("offsite_pull_FAILED lock_timeout", file=sys.stderr)
                raise SystemExit(1) from None
            time.sleep(5)


def _write_meta(lock: Path, owner: str) -> Path:
    meta = lock.with_name(lock.name + ".meta.json")
    tmp = meta.with_name(f".{meta.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "owner": owner,
                    "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, meta)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--wait", type=int, default=21600)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        print("offsite_lock_FAILED no command", file=sys.stderr)
        return 2

    fd = _acquire(args.lock, args.wait)
    meta = None
    try:
        meta = _write_meta(args.lock, args.owner)
        # Hand the descriptor to the child: if this wrapper is killed, the
        # lock must stay held while the transfer it started is still
        # running, or a second puller would join it on one narrow uplink.
        return subprocess.run(command, check=False, pass_fds=(fd,)).returncode
    finally:
        if meta is not None:
            meta.unlink(missing_ok=True)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
```

Тест в `tests/test_pull_filevault_gate.py` дополнить:

```python
def test_script_reexecs_itself_under_the_shared_flock():
    text = SCRIPT.read_text()
    assert "offsite_lock.py" in text
    assert "KF_PULL_LOCKED" in text
    # No home-grown mkdir protocol: the lock must be the shared flock file.
    assert 'mkdir "$LOCK_DIR"' not in text


def test_wrapper_only_retries_contention():
    source = (REPO / "scripts" / "offsite_lock.py").read_text()
    assert "CONTENDED" in source
    assert "os.fchmod(fd, 0o600)" in source
    assert "meta.json" in source


def test_wrapper_releases_the_lock_when_the_child_finishes(tmp_path):
    import subprocess
    import sys

    lock = tmp_path / "network.lock"
    wrapper = REPO / "scripts" / "offsite_lock.py"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(wrapper), "--owner", "test", "--wait", "1",
             "--lock", str(lock), "--", "true"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0


def test_lock_outlives_a_killed_wrapper_while_the_child_runs(tmp_path):
    """SIGKILL on the wrapper must not hand the uplink to a second puller
    while the rsync it launched is still transferring."""
    import os
    import signal
    import subprocess
    import sys
    import time

    lock = tmp_path / "network.lock"
    pidfile = tmp_path / "child.pid"
    wrapper = REPO / "scripts" / "offsite_lock.py"
    holder = subprocess.Popen(
        [
            sys.executable, str(wrapper), "--owner", "kf-pull", "--lock", str(lock),
            "--", "sh", "-c", f"echo $$ > {pidfile}; sleep 30",
        ]
    )
    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        child_pid = int(pidfile.read_text().strip())
        holder.send_signal(signal.SIGKILL)
        holder.wait()

        contender = subprocess.run(
            [sys.executable, str(wrapper), "--owner", "hermes-pull", "--wait", "1",
             "--lock", str(lock), "--", "true"],
            capture_output=True,
            text=True,
        )
        assert contender.returncode == 1
        assert "lock_timeout" in contender.stderr
    finally:
        # Kill exactly the child we started, never a pattern match.
        os.kill(child_pid, signal.SIGKILL)
```

- [ ] **Step 4: Прогнать тесты**

```bash
cd /Users/romanmizanov/Documents/BD/knowledge-factory
bash -n scripts/pull_backups_from_aeza.sh
uv run pytest tests/test_pull_filevault_gate.py -v
uv run pytest -q
```

Expected: `bash -n` молчит; 6 тестов гейта проходят; полный набор — 227 passed,
1 skipped.

- [ ] **Step 5: Локальный коммит**

```bash
cd /Users/romanmizanov/Documents/BD/knowledge-factory
git add scripts/pull_backups_from_aeza.sh scripts/offsite_lock.py \
        tests/test_pull_filevault_gate.py
git commit -m "fix(offsite): gate the pull on FileVault and share the network lock"
```

Живой LaunchAgent сейчас запускает **копию** скрипта из
`~/.local/share/knowledge-factory/`, а не файл репозитория — установка обеих
частей входит в Task 18.

---

### Task 18: Развёртывание и первая живая проверка

Выполняется только после Task 1 (`fdesetup isactive` = `true`) и зелёного
прогона обоих наборов тестов.

**Топология развёртывания — проверена 2026-07-27, не такая, как считалось:**

- `~/Documents/BD` — это git-репозиторий, а `knowledge-factory` — обычный
  подкаталог в нём. Отдельного репозитория у Knowledge Factory нет, remote тоже
  нет.
- На Aeza `/srv/knowledge-factory/app` — клон **bundle'а**, а его история
  получена `git subtree split --prefix=knowledge-factory`. Поэтому хеши на
  сервере отличаются от локальных, хотя сообщения совпадают.
- Проверено воспроизведением: `split(8260272)` = `4b31d82a1a9…` — ровно текущий
  HEAD Aeza; `split(198a760)` = `45827a7e3bf6ad3d2cd67e72b7292957ea6b6088`, и он
  потомок серверного HEAD, значит `merge --ff-only` пройдёт.
- `/srv/hermes/app` — обычный клон GitHub, обновляется `fetch` + `merge --ff-only`.

Порядок фаз важен: фаза A закрывает активную уязвимость и идёт первой.

#### Фаза A. Срочная выкатка Knowledge Factory

На Aeza до сих пор `restore_drill.sh` делает `source` файла из каталога
бэкапов, и делает это от root по таймеру.

Документированная в runbook команда `git bundle create --all` здесь **не
годится**: git-корень — `/Users/romanmizanov/Documents/BD`, поэтому она упакует
монорепозиторий целиком, а не серверную историю Knowledge Factory. Нужен именно
subtree split.

Каждая проверка ниже — условие выхода, а не печать. Шаг, который «прошёл»,
ничего не проверив, хуже отсутствующего шага.

- [ ] **Step A1: Собрать bundle из subtree split**

```bash
set -euo pipefail
cd /Users/romanmizanov/Documents/BD
test -z "$(git status --porcelain)"

# Не затирать одноимённую пользовательскую ветку молча.
if git show-ref --verify --quiet refs/heads/kf-main; then
  echo "ветка kf-main уже существует — разобраться, чья она" >&2
  exit 1
fi

SPLIT="$(git subtree split --prefix=knowledge-factory HEAD)"
echo "split: $SPLIT"
test "$SPLIT" = 45827a7e3bf6ad3d2cd67e72b7292957ea6b6088

# Временная ветка нужна лишь как имя для bundle: git bundle пакует ссылки,
# а не голые SHA. Удаляется в Step A8.
git branch kf-main "$SPLIT"
rm -f /tmp/kf-deploy.bundle
git bundle create /tmp/kf-deploy.bundle kf-main
git bundle verify /tmp/kf-deploy.bundle
LOCAL_SHA="$(shasum -a 256 /tmp/kf-deploy.bundle | awk '{print $1}')"
echo "LOCAL_SHA=$LOCAL_SHA"
```

`LOCAL_SHA` понадобится в Step A3 — сохранить значение.

- [ ] **Step A2: Остановить таймер, отказавшись работать при активном drill**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
if systemctl is-active --quiet knowledge-factory-restore-drill.service; then
  echo "drill выполняется прямо сейчас — обновлять код под ним нельзя" >&2
  exit 1
fi
systemctl stop knowledge-factory-restore-drill.timer
if systemctl is-active --quiet knowledge-factory-restore-drill.timer; then
  echo "таймер не остановился" >&2
  exit 1
fi
echo "таймер остановлен, drill не выполняется"
EOF
```

- [ ] **Step A3: Передать во временный файл и сверить контрольную сумму**

```bash
scp -i ~/.ssh/aeza_hermes /tmp/kf-deploy.bundle \
  root@138.124.108.97:/srv/knowledge-factory/staging/kf-deploy.bundle.incoming

ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 "LOCAL_SHA='$LOCAL_SHA' bash -s" <<'EOF'
set -euo pipefail
cd /srv/knowledge-factory/staging
REMOTE_SHA="$(sha256sum kf-deploy.bundle.incoming | awk '{print $1}')"
echo "remote: $REMOTE_SHA"
echo "local:  $LOCAL_SHA"
test "$REMOTE_SHA" = "$LOCAL_SHA"
git -C /srv/knowledge-factory/app bundle verify \
  /srv/knowledge-factory/staging/kf-deploy.bundle.incoming
echo "bundle доставлен без искажений"
EOF
```

Канонический `knowledge-factory.bundle` пока не трогаем: он станет актуальным
только после успешного drill'а, в Step A7.

- [ ] **Step A4: Обновить `main` только fast-forward**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
cd /srv/knowledge-factory/app
test -z "$(git status --porcelain)"
echo "HEAD до: $(git rev-parse HEAD)"

git fetch /srv/knowledge-factory/staging/kf-deploy.bundle.incoming kf-main
test "$(git rev-parse FETCH_HEAD)" = 45827a7e3bf6ad3d2cd67e72b7292957ea6b6088

# Существующая история совместима: проверяем это, а не переклонируем.
git merge-base --is-ancestor 4b31d82a1a9ee5ae174ccd7304114e809dd09aee FETCH_HEAD

git merge --ff-only FETCH_HEAD
test "$(git rev-parse HEAD)" = 45827a7e3bf6ad3d2cd67e72b7292957ea6b6088
echo "HEAD после: $(git rev-parse HEAD)"
git log --oneline -3
EOF
```

Никакого `reset --hard` и никакого повторного `clone`: если fast-forward
невозможен, это повод разбираться, а не перезаписывать сервер.

- [ ] **Step A5: Проверить, что уязвимость закрыта**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
cd /srv/knowledge-factory/app
bash -n scripts/restore_drill.sh
if grep -Fq 'source "$latest/STATE"' scripts/restore_drill.sh; then
  echo "source всё ещё присутствует" >&2
  exit 1
fi
test -f scripts/state_parser.py
echo "source удалён, парсер на месте"

latest=$(ls -1dt /srv/knowledge-factory/data/backups/daily-* | head -1)
echo "свежий бэкап: $latest"
for key in EXPECTED_DOCUMENTS EXPECTED_CHUNKS EXPECTED_POINTS; do
  value="$(python3 scripts/state_parser.py --key "$key" "$latest/STATE")"
  printf '%s = %s\n' "$key" "$value"
  test -n "$value"
done
EOF
```

- [ ] **Step A6: Прогнать drill вручную и вернуть таймер**

`systemctl status` под `pipefail` возвращает 3 даже для успешно завершённого
`Type=oneshot`, поэтому исход читается из свойств юнита, а строка успеха — из
журнала именно этого запуска.

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
unit=knowledge-factory-restore-drill.service
systemctl start "$unit"

result="$(systemctl show -p Result --value "$unit")"
status="$(systemctl show -p ExecMainStatus --value "$unit")"
invocation="$(systemctl show -p InvocationID --value "$unit")"
echo "Result=$result ExecMainStatus=$status Invocation=$invocation"
test "$result" = success
test "$status" = 0

journalctl "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager | tail -20
journalctl "_SYSTEMD_INVOCATION_ID=$invocation" --no-pager | grep -q restore_drill_OK
echo "drill прошёл на новой версии"
EOF
```

Таймер возвращается **только после** этого:

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
systemctl start knowledge-factory-restore-drill.timer
systemctl is-active --quiet knowledge-factory-restore-drill.timer
systemctl list-timers --no-pager | grep restore-drill
EOF
```

- [ ] **Step A7: Атомарно обновить канонический bundle**

`origin` рабочего дерева указывает на
`/srv/knowledge-factory/staging/knowledge-factory.bundle`. Пока файл не
обновлён, `origin` описывает историю, которой на сервере уже нет.

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
STAGING=/srv/knowledge-factory/staging
APP=/srv/knowledge-factory/app

# Сохранить прежний канонический bundle, если он есть, — до конца проверок.
if [ -f "$STAGING/knowledge-factory.bundle" ]; then
  cp -p "$STAGING/knowledge-factory.bundle" "$STAGING/knowledge-factory.bundle.previous"
fi

# Одна файловая система, поэтому mv атомарен: origin никогда не увидит
# наполовину записанный bundle.
mv -f "$STAGING/kf-deploy.bundle.incoming" "$STAGING/knowledge-factory.bundle"
chmod 600 "$STAGING/knowledge-factory.bundle"

# Абсолютный путь: с -C относительный искался бы внутри app.
git -C "$APP" bundle verify "$STAGING/knowledge-factory.bundle"

# В bundle ссылка называется kf-main — научить origin её видеть.
git -C "$APP" config remote.origin.fetch '+refs/heads/kf-main:refs/remotes/origin/main'
git -C "$APP" fetch origin
test "$(git -C "$APP" rev-parse refs/remotes/origin/main)" \
  = 45827a7e3bf6ad3d2cd67e72b7292957ea6b6088
echo "origin снова описывает развёрнутую историю"
EOF
```

- [ ] **Step A8: Убрать временную локальную ссылку**

```bash
set -euo pipefail
cd /Users/romanmizanov/Documents/BD
git branch -D kf-main
if git show-ref --verify --quiet refs/heads/kf-main; then
  echo "ветка kf-main не удалилась" >&2
  exit 1
fi
rm -f /tmp/kf-deploy.bundle
echo "временная ветка и локальный bundle убраны"
```

- [ ] **Step A9: Установить исправленные скрипты на Mac**

LaunchAgent запускает копию в `~/.local/share/knowledge-factory/`, а не файл
репозитория. Обёртка ищет `offsite_lock.py` рядом с собой, поэтому обе части
ставятся в один каталог.

```bash
set -euo pipefail
KF=/Users/romanmizanov/Documents/BD/knowledge-factory
RUNTIME="$HOME/.local/share/knowledge-factory"

launchctl bootout "gui/$(id -u)/com.knowledge-factory.backup-pull" 2>/dev/null || true
if pgrep -f pull_backups_from_aeza >/dev/null; then
  echo "стягивание ещё идёт — подменять скрипт под ним нельзя" >&2
  exit 1
fi

install -m 0755 "$KF/scripts/pull_backups_from_aeza.sh" "$RUNTIME/pull_backups_from_aeza.sh"
install -m 0755 "$KF/scripts/offsite_lock.py" "$RUNTIME/offsite_lock.py"
diff -q "$KF/scripts/pull_backups_from_aeza.sh" "$RUNTIME/pull_backups_from_aeza.sh"
diff -q "$KF/scripts/offsite_lock.py" "$RUNTIME/offsite_lock.py"
echo "обе части установлены"
```

Агент вернётся в Step C3, после миграции лока.

#### Фаза B. Выкатка Hermes на Aeza

- [ ] **Step B1: Запушить Hermes**

```bash
cd /Users/romanmizanov/Documents/Hermes
git status --short
git push origin main
git rev-list --count origin/main..main
```

Expected: `0` — локально ничего не осталось неотправленным.

- [ ] **Step B2: Обновить сервер fast-forward**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
cd /srv/hermes/app
test -z "$(git status --porcelain)"
git rev-parse --short HEAD
git fetch origin
git merge --ff-only origin/main
git rev-parse --short HEAD
EOF
```

Expected: до — `6876f3fa9`, после — HEAD пуша. Если рабочее дерево грязное,
разобраться, что там изменено вручную, а не затирать.

- [ ] **Step B3: Прогнать essential-бэкап вручную**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 \
  '/srv/hermes/app/deploy/beget/hermes_essential_backup.sh'
```

Expected: строка `hermes_essential_backup_OK path=… files=… unclassified=…
specials=…`. Это первая живая проверка понижения привилегий: в выводе не должно
быть `Permission denied` и `snapshot_failed`.

- [ ] **Step B4: Проверить опубликованный каталог и владельцев БД**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
latest=$(ls -1dt /srv/hermes/backups/essential/daily-* | head -1)
echo "каталог: $latest"
stat -c "%U:%G %a %n" "$latest"
ls -1 "$latest" | sort | tr '\n' ' '; echo
grep -E "EXPECTED_SESSIONS|EXPECTED_SKILLS|EXPECTED_PLUGINS|EXCLUDED_SPECIAL_COUNT" "$latest/STATE"
echo "--- владельцы живых баз:"
stat -c "%U:%G %a %n" /srv/hermes/data /srv/hermes/data/state.db* /srv/hermes/data/kanban.db*
EOF
```

Expected: `root:root 700`, ровно пять файлов, `EXPECTED_SESSIONS=2`,
`EXPECTED_SKILLS=78`, `EXPECTED_PLUGINS=3`; владельцы живых баз и каталога
данных — прежние `10000:10000`, ни одной записи с `root`.

- [ ] **Step B5: Прогнать полный архив и проверить его состав**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
/srv/hermes/app/deploy/beget/backup.sh
latest=$(ls -1t /srv/hermes/backups/hermes-*.tar.gz | head -1)
echo "архив: $latest"
tar -tzf "$latest" | grep -E '(^|/)(state|kanban)\.db' || true
stat -c "%U:%G %a %n" /srv/hermes/data/state.db* /srv/hermes/data/kanban.db*
EOF
```

Expected: `hermes_full_backup_OK`; в архиве ровно `state.db` и `kanban.db` без
`-wal`/`-shm`; владельцы живых баз не изменились.

- [ ] **Step B6: Установить таймеры и снять старый cron**

```bash
ssh -i ~/.ssh/aeza_hermes root@138.124.108.97 'bash -s' <<'EOF'
set -euo pipefail
for unit in hermes-essential-backup hermes-full-backup; do
  install -m 0644 "/srv/hermes/app/deploy/beget/systemd/$unit.service" /etc/systemd/system/
  install -m 0644 "/srv/hermes/app/deploy/beget/systemd/$unit.timer" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now hermes-essential-backup.timer hermes-full-backup.timer

# Снять старую cron-запись, не падая, если crontab пуст или отсутствует.
current="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$current" | grep -q 'deploy/beget/backup.sh'; then
  printf '%s\n' "$current" | grep -v 'deploy/beget/backup.sh' | crontab -
  echo "cron-запись снята"
else
  echo "cron-записи не было"
fi
crontab -l 2>/dev/null || echo "crontab пуст"
systemctl list-timers --no-pager | grep hermes
EOF
```

Расписание теперь ведут таймеры, и только они знают про `SuccessExitStatus=75`.

#### Фаза C. Mac: лок, первый pull, drill, агенты

- [ ] **Step C1: Мигрировать старый каталог-лок**

Ранние черновики использовали каталог `network.lock`; протокол заменён на
`fcntl.flock` по обычному файлу, и `os.open` на каталоге падает с `EISDIR`.

```bash
set -euo pipefail
launchctl bootout "gui/$(id -u)/com.knowledge-factory.backup-pull" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.hermes.offsite-pull" 2>/dev/null || true
launchctl list | grep -E "knowledge-factory.backup-pull|com.hermes.offsite-pull" \
  && echo "ВНИМАНИЕ: агент ещё загружен" || echo "оба агента выгружены"
pgrep -f "pull_backups_from_aeza|hermes_backup.offsite_pull" \
  && echo "ВНИМАНИЕ: стягивание идёт" || echo "процессов стягивания нет"

LOCK="$HOME/Library/Application Support/offsite-sync/network.lock"
mkdir -p "$(dirname "$LOCK")"
if [ -d "$LOCK" ]; then
  rm -f "$LOCK/meta.json"
  rmdir "$LOCK"
fi
[ -e "$LOCK" ] || : >"$LOCK"
chmod 600 "$LOCK"
ls -l "$LOCK"
```

- [ ] **Step C2: Первый pull, drill и сводка**

```bash
cd /Users/romanmizanov/Documents/Hermes
deploy/macos/hermes_pull_offsite.sh
deploy/macos/hermes_restore_drill.sh
deploy/macos/hermes_backup_status.sh
```

Expected: `hermes_offsite_pull_OK`, `hermes_freshness_OK`,
`hermes_restore_drill_OK sessions=2 skills=78 plugins=3 cron_jobs=… unclassified=…`.
Если `unclassified` больше нуля — посмотреть `INVENTORY.jsonl` и решить,
дополнять ли `ESSENTIAL_RULES`. Стягивание идёт по узкому каналу и может занять
десятки минут.

- [ ] **Step C3: Установить и вернуть агенты**

```bash
set -euo pipefail
mkdir -p ~/Library/Logs/hermes-backup
cp /Users/romanmizanov/Documents/Hermes/deploy/macos/com.hermes.offsite-pull.plist ~/Library/LaunchAgents/
cp /Users/romanmizanov/Documents/Hermes/deploy/macos/com.hermes.restore-drill.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.hermes.offsite-pull.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.hermes.restore-drill.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.knowledge-factory.backup-pull.plist
launchctl list | grep -E "com.hermes|knowledge-factory"
```

- [ ] **Step C4: Обновить документацию и закоммитить**

```bash
cd /Users/romanmizanov/Documents/Hermes
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
`hermes_`, который добавляет сама функция. `FileLock(path, owner)` —
одинаковая сигнатура в Task 9 и 13; `held_by(path)` из того же модуля читает
Task 15. Knowledge Factory в Task 17 держит тот же файл через `scripts/offsite_lock.py`,
поэтому протокол общий, а реализации независимы.
