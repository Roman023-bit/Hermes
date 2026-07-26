# Перенос Knowledge Factory с Mac на Aeza

Статус документа: **Этап A выполнен 2026-07-25; production cutover завершён**.

Фактический результат:

- Hermes использует `http://knowledge-factory:8000/mcp` через закрытую
  Docker-сеть `hermes-internal`;
- PostgreSQL, Qdrant и MCP не публикуют порты на хост;
- восстановлено 96 документов, 620 чанков и 620 точек;
- `hermes mcp test knowledge_factory` прошёл 5/5 до перезагрузки и повторно
  после холодного старта VPS;
- контейнеры `hermes`, `knowledge-factory`, `kf-postgres`, `kf-qdrant`
  автоматически восстановились после reboot, restart count равен нулю;
- ежедневный application-consistent backup включён через
  `knowledge-factory-backup.timer`;
- изолированный restore drill включён ежемесячно и уже подтверждён на
  реальном backup: 96/620/620 плюс контрольный поиск;
- внутренний healthcheck выполняется каждые 10 минут;
- off-site копия хранится на Mac и обновляется LaunchAgent
  `com.knowledge-factory.backup-pull`;
- исходные документы автоматически зеркалируются с Mac на Aeza каждые
  10 минут через `com.knowledge-factory.sync-to-aeza`; после подтверждённого
  SHA-манифеста обе фабрики независимо выполняют `kf update`;
- локальный MCP на Mac оставлен работающим как rollback и для других
  проектов; локальный автоиндексатор отключён на время карантина;
- Этап B (Linux OCR, относительные пути и серверный scheduler обновлений)
  намеренно не активирован до прохождения 3–7 дней стабильности, как требует
  раздел 18.

Цель: разместить Knowledge Factory на том же VPS Aeza, где работает Hermes,
и убрать рабочую зависимость от Mac, Tailscale и внешнего HTTPS-маршрута.

Целевая схема:

```text
Hermes ── Docker network hermes-internal ── knowledge-factory:8000
                                                 │
                                      ┌──────────┴──────────┐
                                      │                     │
                                kf-postgres            kf-qdrant
```

PostgreSQL и Qdrant не публикуют порты на хост и доступны только приложению
Knowledge Factory. MCP тоже не публикуется наружу: его видит только Hermes
через Docker DNS.

## 1. Подтверждённое исходное состояние

### Mac

- код: `/Users/romanmizanov/Documents/BD/knowledge-factory`;
- ветка: `main`;
- текущий коммит: `24df35b`;
- Git remote не настроен;
- MCP запускается LaunchAgent `com.knowledge-factory.mcp`;
- автообновление запускается LaunchAgent
  `com.knowledge-factory.autoupdate`;
- расписание автообновления: ежедневно в 03:00;
- исходные документы:
  `/Users/romanmizanov/Documents/Цифровой мозг`;
- PostgreSQL: контейнер `kf-postgres`, образ `postgres:17`;
- Qdrant: контейнер `kf-qdrant`, образ `qdrant/qdrant:v1.18.2`;
- текущая база:
  - 96 документов;
  - 620 чанков;
  - 620 точек Qdrant;
  - 59 обычных документов;
  - 37 OCR-документов;
  - 40 OCR-страниц;
- размер PostgreSQL: около 9 МБ, custom-format `pg_dump` около 226 КБ;
- каталог Qdrant: около 179 МБ;
- dense-модель: около 2.1 ГБ;
- BM25-модель: менее 1 МБ;
- кеш отключённого reranker: около 1.1 ГБ — переносить не требуется при
  `RERANK=0`;
- после тех же исключений, которые применяет сканер:
  - 179 файлов, 205 195 460 байт всего;
  - 96 индексируемых файлов, 46 703 956 байт;
  - расширения: 64 PDF, 17 TXT, 14 MD, 1 DOCX.

### Aeza

- IPv4: `138.124.108.97`;
- ОС: Ubuntu 24.04;
- архитектура: `x86_64`;
- 4 vCPU;
- 7.8 ГБ RAM, сейчас доступно около 7.1 ГБ;
- swap сейчас отсутствует;
- свободно около 101 ГБ;
- Hermes использует около 125 МБ RAM в покое;
- фактический production Compose:
  `/srv/hermes/app/deploy/beget/compose.yaml`;
- Compose project: `beget`;
- контейнер Hermes: `hermes`;
- текущий MCP URL:
  `https://macbook-air-od.tail0483d9.ts.net/mcp`;
- секрет для MCP уже называется `KF_MCP_TOKEN` в
  `/srv/hermes/data/.env`;
- текущий Compose содержит временный `extra_hosts` для Mac.

## 2. Важные ограничения

### 2.1. `ocrmac` не работает в Linux

Сейчас `kf/ocr.py` безусловно импортирует `ocrmac`, а эта библиотека использует
Apple Vision. Простая установка проекта в Linux-контейнер либо упадёт при
импорте, либо не сможет обрабатывать сканированные PDF.

Поэтому перенос делится на два независимых этапа:

1. **Этап A — автономное чтение:** перенести готовые PostgreSQL/Qdrant,
   поднять `search_knowledge`, `ask` и `stats` на Aeza, запретить автоматическую
   переиндексацию. Это сразу убирает зависимость поиска от Mac.
2. **Этап B — автономное пополнение:** добавить Linux OCR, проверить его на
   реальных сканах и только после этого включить `kf update` по расписанию.

Этап A нельзя выдавать за полностью завершённую фабрику, если требуется
автоматически индексировать новые сканированные PDF.

### 2.2. В базе сохранены абсолютные пути Mac

PostgreSQL и payload Qdrant содержат пути вида:

```text
/Users/romanmizanov/Documents/Цифровой мозг/...
```

Идентификаторы документов и чанков также первоначально вычислялись из этого
пути. Если сразу задать Linux-путь `/data/sources` и запустить `kf update`,
файлы будут восприняты как новые. Возможны дубликаты и удаление старых записей
механизмом prune.

Для безопасного первого cutover каталог Aeza
`/srv/knowledge-factory/data/sources` следует смонтировать **внутри
контейнера** по прежнему абсолютному пути Mac:

```text
/Users/romanmizanov/Documents/Цифровой мозг
```

Это временный слой совместимости. Перевод базы на относительные пути выполняется
отдельной миграцией после стабилизации.

### 2.3. Нельзя копировать живые каталоги БД через `rsync`

Запрещено переносить работающие:

```text
data/postgres/
data/qdrant/
```

как обычные файлы. PostgreSQL переносится через `pg_dump`, Qdrant — через
collection snapshot. Перед снятием финальных снимков останавливается только
автоиндексация; MCP на Mac может продолжать обслуживать чтение.

### 2.4. ARM-образы Mac нельзя переносить на x86_64

Локальные образы PostgreSQL и Qdrant имеют архитектуру `arm64`, Aeza —
`x86_64`. На Aeza образы нужно скачать заново с теми же зафиксированными
тегами. Переносятся данные и кеш моделей, но не Docker images с Mac.

## 3. Целевая структура на Aeza

```text
/srv/knowledge-factory/
├── app/                       # Git checkout приложения
├── data/
│   ├── sources/               # документы
│   ├── models/                # dense + sparse model cache
│   ├── postgres/              # bind mount PostgreSQL
│   ├── qdrant/                # bind mount Qdrant
│   └── backups/               # временные локальные logical backups
├── secrets/
│   └── .env                   # chmod 600, только секреты
└── staging/                   # bundle, dumps, snapshots; удалить после приёмки
```

Код и данные принадлежат отдельному системному пользователю, например
`knowledge-factory` с постоянным UID/GID. Сервисы запускаются не от root, если
базовый образ это поддерживает; для официальных PostgreSQL/Qdrant сохраняются
их штатные пользователи и корректные владельцы bind mount.

## 4. Docker-сети

Создать один внешний bridge:

```bash
docker network create hermes-internal
```

В отдельном Compose Knowledge Factory:

- `knowledge-factory` подключён к `hermes-internal` и `kf-backend`;
- `kf-postgres` подключён только к `kf-backend`;
- `kf-qdrant` подключён только к `kf-backend`;
- `kf-backend` объявлен `internal: true`;
- ни у одного сервиса нет `ports:`.

В production Compose Hermes добавить:

```yaml
services:
  hermes:
    networks:
      - default
      - hermes-internal

networks:
  hermes-internal:
    external: true
```

Существующую сеть `default`, bind mounts, localhost-порты и остальные параметры
Hermes сохранить.

`extra_hosts` для Mac не удалять до окончания карантина.

## 5. Изменения в коде Knowledge Factory до сборки

Все изменения сначала выполняются и тестируются в репозитории
`/Users/romanmizanov/Documents/BD/knowledge-factory`.

### 5.1. Сделать OCR платформенным

Нужно:

1. Пометить `ocrmac` зависимостью только для macOS либо вынести её в отдельный
   optional dependency group.
2. Убрать безусловный импорт `ocrmac` из пути импорта MCP-сервера.
3. Ввести явный backend:
   - `apple_vision` — текущая реализация на Mac;
   - `disabled` — только для Этапа A, с fail-closed ошибкой при попытке OCR;
   - `tesseract` или другой локальный Linux backend — Этап B.
4. Запретить плановый `kf update`, если backend `disabled`, а среди новых или
   изменённых PDF есть страница без достаточного текстового слоя.
5. Не менять уже рабочие чанки при ошибке OCR.

Минимальные тесты:

- импорт `mcp_server.py` на Linux без `ocrmac`;
- `search_knowledge` и `stats` работают при `OCR_BACKEND=disabled`;
- новый сканированный PDF не помечается успешно обработанным;
- ошибка OCR не удаляет старые чанки изменённого документа;
- macOS backend сохраняет текущее поведение.

### 5.2. Разрешить безопасный bind внутри Docker

Текущий HTTP-код разрешает слушать только loopback и требует MagicDNS host.
Для Docker нужен bind `0.0.0.0:8000`, иначе другой контейнер не подключится.

Нельзя просто удалить проверку loopback. Нужен явный режим Docker:

- по умолчанию политика остаётся loopback-only;
- non-loopback bind разрешается только при явном deployment mode;
- Bearer token остаётся обязательным;
- allowed hosts включают `knowledge-factory` и `knowledge-factory:*`;
- порт не публикуется на хост;
- запрос без Bearer возвращает 401;
- неверный Host отклоняется;
- token не попадает в логи.

Добавить безопасный `/healthz`, который не возвращает конфигурацию, пути,
ключи или содержимое базы. Он должен проверять как минимум готовность процесса;
готовность PostgreSQL/Qdrant отдельно проверяется acceptance-тестом `stats`.

### 5.3. Добавить production Dockerfile

Рекомендуемая база: Python 3.12 slim для `linux/amd64`.

Требования:

- установка зависимостей строго по `uv.lock`;
- `uv sync --frozen --no-dev`;
- системные библиотеки ONNX и OCR фиксируются явно;
- приложение запускается одним процессом Uvicorn/FastMCP;
- не включать несколько Uvicorn workers: каждый worker загрузит свою копию
  dense-модели;
- запуск от непривилегированного пользователя;
- source tree монтируется read-only;
- model cache монтируется отдельно;
- `no-new-privileges`;
- `cap_drop: [ALL]`, если тесты подтверждают работу;
- логирование в stdout/stderr, Docker log rotation;
- healthcheck с разумным `start_period`, потому что первый прогрев модели
  медленный.

### 5.4. Добавить production Compose

Сервисы и фиксированные версии:

- `knowledge-factory`;
- `postgres:17`;
- `qdrant/qdrant:v1.18.2`.

Не использовать `latest`.

Зависимости запуска:

- приложение ждёт healthy PostgreSQL;
- приложение ждёт healthy Qdrant;
- restart policy: `unless-stopped`;
- `stop_grace_period` не меньше 30 секунд.

### 5.5. Linux-тест до переноса данных

Собрать образ локально как `linux/amd64` либо непосредственно на Aeza и
выполнить:

```bash
docker build --platform linux/amd64 -t knowledge-factory:test .
docker run --rm --entrypoint python knowledge-factory:test \
  -c 'import mcp_server; print("linux_import_OK")'
```

Критерий: `linux_import_OK`, без импорта Apple frameworks.

## 6. Подготовка Aeza

Эта фаза не останавливает Mac и не меняет Hermes.

```bash
export AEZA_IP=138.124.108.97
export AEZA_SSH_KEY="$HOME/.ssh/aeza_hermes"

ssh -i "$AEZA_SSH_KEY" root@"$AEZA_IP"
```

Повторный preflight:

```bash
set -euo pipefail
nproc
free -h
df -h /srv
docker version
docker compose version
docker ps
```

Минимальный gate:

- не менее 4 CPU;
- не менее 6 ГБ available RAM до запуска;
- не менее 15 ГБ свободного диска;
- Hermes остаётся `running`;
- Docker и Compose отвечают без ошибок.

### 6.1. Добавить swap как страховку

Dense-модель занимает около 2.1 ГБ на диске, а пиковое потребление памяти
нужно измерить уже на `x86_64`. На VPS сейчас нет swap. До прогрева создать
4 ГБ swap:

```bash
set -euo pipefail
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile ' /etc/fstab ||
  printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show
```

После команды обязательно проверить, что Hermes продолжает работать.

### 6.2. Создать каталоги и сеть

```bash
set -euo pipefail
install -d -m 0750 /srv/knowledge-factory
install -d -m 0750 /srv/knowledge-factory/app
install -d -m 0750 /srv/knowledge-factory/data
install -d -m 0750 /srv/knowledge-factory/data/sources
install -d -m 0750 /srv/knowledge-factory/data/models
install -d -m 0750 /srv/knowledge-factory/data/postgres
install -d -m 0750 /srv/knowledge-factory/data/qdrant
install -d -m 0700 /srv/knowledge-factory/data/backups
install -d -m 0700 /srv/knowledge-factory/secrets
install -d -m 0700 /srv/knowledge-factory/staging

docker network inspect hermes-internal >/dev/null 2>&1 ||
  docker network create hermes-internal
```

## 7. Перенос кода без GitHub

Поскольку у Knowledge Factory нет remote, сохранить историю через Git bundle.

На Mac:

```bash
set -euo pipefail
cd "/Users/romanmizanov/Documents/BD/knowledge-factory"
test -z "$(git status --porcelain)"
git bundle create knowledge-factory.bundle --all
git bundle verify knowledge-factory.bundle
sha256sum knowledge-factory.bundle
```

На macOS вместо `sha256sum` может использоваться:

```bash
shasum -a 256 knowledge-factory.bundle
```

Передать bundle:

```bash
scp -i "$HOME/.ssh/aeza_hermes" \
  knowledge-factory.bundle \
  root@138.124.108.97:/srv/knowledge-factory/staging/
```

На Aeza:

```bash
set -euo pipefail
git clone /srv/knowledge-factory/staging/knowledge-factory.bundle \
  /srv/knowledge-factory/app
cd /srv/knowledge-factory/app
git checkout main
git status --short --branch
git rev-parse HEAD
```

Критерии:

- рабочее дерево чистое;
- HEAD равен протестированному коммиту;
- Dockerfile и production Compose присутствуют.

После стабилизации рекомендуется создать отдельный приватный GitHub-репозиторий
для Knowledge Factory. Это не должно блокировать миграцию.

## 8. Перенос исходных документов

Сначала dry-run. Исключения должны совпадать с `kf/sources.py`:

```bash
rsync -aHAXnc --numeric-ids --delete --itemize-changes \
  --exclude='.git/' \
  --exclude='node_modules/' \
  --exclude='venv/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='dist/' \
  --exclude='build/' \
  --exclude='.pytest_cache/' \
  --exclude='_System/' \
  -e "ssh -i $HOME/.ssh/aeza_hermes" \
  "/Users/romanmizanov/Documents/Цифровой мозг/" \
  root@138.124.108.97:/srv/knowledge-factory/data/sources/
```

Затем повторить без `n`:

```bash
rsync -aHAXc --numeric-ids --delete --info=progress2 \
  --exclude='.git/' \
  --exclude='node_modules/' \
  --exclude='venv/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='dist/' \
  --exclude='build/' \
  --exclude='.pytest_cache/' \
  --exclude='_System/' \
  -e "ssh -i $HOME/.ssh/aeza_hermes" \
  "/Users/romanmizanov/Documents/Цифровой мозг/" \
  root@138.124.108.97:/srv/knowledge-factory/data/sources/
```

Контрольный проход:

```bash
set -euo pipefail
out="$(
  rsync -aHAXnc --numeric-ids --delete --itemize-changes \
    --exclude='.git/' \
    --exclude='node_modules/' \
    --exclude='venv/' \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='dist/' \
    --exclude='build/' \
    --exclude='.pytest_cache/' \
    --exclude='_System/' \
    -e "ssh -i $HOME/.ssh/aeza_hermes" \
    "/Users/romanmizanov/Documents/Цифровой мозг/" \
    root@138.124.108.97:/srv/knowledge-factory/data/sources/ 2>&1
)"
rc=$?
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then
  echo "sources_checksum_mirror_OK"
else
  echo "sources_checksum_mirror_FAILED rc=$rc"
  printf '%s\n' "$out"
  exit 1
fi
```

Gate: только `sources_checksum_mirror_OK`.

## 9. Перенос кеша моделей

При `RERANK=0` переносить только:

```text
models--qdrant--multilingual-e5-large-onnx
models--Qdrant--bm25
CACHEDIR.TAG
```

Не переносить 1.1 ГБ:

```text
models--jinaai--jina-reranker-v2-base-multilingual
```

Команда с Mac:

```bash
rsync -aHAXc --info=progress2 \
  -e "ssh -i $HOME/.ssh/aeza_hermes" \
  "/Users/romanmizanov/Documents/BD/knowledge-factory/data/models/models--qdrant--multilingual-e5-large-onnx" \
  "/Users/romanmizanov/Documents/BD/knowledge-factory/data/models/models--Qdrant--bm25" \
  "/Users/romanmizanov/Documents/BD/knowledge-factory/data/models/CACHEDIR.TAG" \
  root@138.124.108.97:/srv/knowledge-factory/data/models/
```

После переноса построить SHA-256 manifest на обеих сторонах и сравнить.
Если кеш окажется непереносимым, удалить только повреждённую копию на Aeza и
дать FastEmbed скачать модель заново. Не переносить Python `.venv`.

## 10. Секреты

Сгенерировать на Aeza:

- новый `KF_MCP_TOKEN`;
- новый сильный `POSTGRES_PASSWORD`;
- отдельный OpenRouter key с ограничением расходов либо существующий key,
  если разделение не требуется.

Не печатать значения в терминал, логи или чат. Файл:

```text
/srv/knowledge-factory/secrets/.env
```

с правами:

```bash
chown root:root /srv/knowledge-factory/secrets/.env
chmod 600 /srv/knowledge-factory/secrets/.env
```

В нём только секреты:

```dotenv
KF_MCP_TOKEN=...
POSTGRES_PASSWORD=...
OPENROUTER_API_KEY=...
```

Несеcretные параметры задаются в production Compose:

```dotenv
COLLECTION=knowledge
DENSE_MODEL=intfloat/multilingual-e5-large
SPARSE_MODEL=Qdrant/bm25
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
RERANK=0
OCR_BACKEND=disabled
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8000
MCP_PUBLIC_HOST=knowledge-factory
MODEL_CACHE_DIR=/data/models
QDRANT_URL=http://kf-qdrant:6333
```

Для первого cutover:

```dotenv
KNOWLEDGE_ROOT=/Users/romanmizanov/Documents/Цифровой мозг
```

Compose передаёт:

```dotenv
MCP_AUTH_TOKEN=${KF_MCP_TOKEN}
PG_DSN=postgresql://kf:${POSTGRES_PASSWORD}@kf-postgres:5432/kf
```

Тот же новый `KF_MCP_TOKEN` безопасно записать в
`/srv/hermes/data/.env`, сохранив владельца и режим `0640`.

## 11. Сборка и запуск пустого стека

На Aeza:

```bash
set -euo pipefail
cd /srv/knowledge-factory/app
docker compose -f compose.production.yaml config --quiet
docker compose -f compose.production.yaml build --pull
docker compose -f compose.production.yaml up -d kf-postgres kf-qdrant
docker compose -f compose.production.yaml ps
```

На этом шаге приложение ещё не подключать к Hermes.

Проверить:

- оба хранилища healthy;
- портов `5432`, `6333`, `6334`, `8000` нет в публичном LISTEN;
- UFW по-прежнему разрешает только SSH;
- Hermes работает без рестарта.

## 12. Финальный снимок данных на Mac

Эта фаза кратко замораживает **только запись** в Knowledge Factory.
Поиск через Mac продолжает работать.

### 12.1. Остановить автообновление

На Mac:

```bash
launchctl bootout \
  "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.knowledge-factory.autoupdate.plist"
```

Проверить, что процесс `kf update` не идёт. MCP LaunchAgent не останавливать.

Снять эталон:

```bash
cd "/Users/romanmizanov/Documents/BD/knowledge-factory"
./.venv/bin/kf status
```

Эталон перед переносом:

```text
Документов: 96
Чанков: 620
Точек Qdrant: 620
failed: 0
```

Если числа изменились из-за новых документов, зафиксировать новые значения и
использовать их во всех дальнейших gate.

### 12.2. PostgreSQL dump

```bash
set -euo pipefail
cd "/Users/romanmizanov/Documents/BD/knowledge-factory"
mkdir -p staging
docker exec kf-postgres \
  pg_dump -U kf -d kf --format=custom --no-owner --no-acl \
  > staging/kf-postgres.dump
test -s staging/kf-postgres.dump
```

Проверка:

```bash
docker exec -i kf-postgres pg_restore --list < staging/kf-postgres.dump \
  >/dev/null
echo "postgres_dump_OK"
```

### 12.3. Qdrant snapshot

Создать snapshot коллекции `knowledge` через REST API текущего
`qdrant/qdrant:v1.18.2`, получить имя из JSON и скачать snapshot-файл через
REST API. Не угадывать путь внутри volume.

Проверить:

- HTTP API вернул status `ok`;
- snapshot-файл непустой;
- его имя и SHA-256 записаны в manifest.

### 12.4. Manifest и передача

В manifest включить:

- `kf-postgres.dump`;
- Qdrant snapshot;
- commit SHA кода;
- эталон `kf status`;
- время снимка UTC.

Передать:

```bash
scp -i "$HOME/.ssh/aeza_hermes" \
  staging/kf-postgres.dump \
  staging/knowledge.snapshot \
  staging/SHA256SUMS \
  root@138.124.108.97:/srv/knowledge-factory/staging/
```

На Aeza проверить SHA-256. При любом несовпадении остановиться.

## 13. Восстановление на Aeza

### 13.1. PostgreSQL

Восстанавливать в пустую базу:

```bash
set -euo pipefail
cd /srv/knowledge-factory/app
docker compose -f compose.production.yaml exec -T kf-postgres \
  dropdb -U kf --if-exists kf
docker compose -f compose.production.yaml exec -T kf-postgres \
  createdb -U kf kf
docker compose -f compose.production.yaml exec -T kf-postgres \
  pg_restore -U kf -d kf --no-owner --no-acl \
  < /srv/knowledge-factory/staging/kf-postgres.dump
```

Если container name/service name отличается, использовать фактическое имя из
Compose. Не выполнять команды до проверки `docker compose config`.

### 13.2. Qdrant

Восстановить snapshot официальным recovery endpoint версии `v1.18.2`.
Snapshot должен быть доступен контейнеру через отдельный read-only bind mount
или загрузку через API.

После recovery:

```bash
curl -fsS http://kf-qdrant:6333/collections/knowledge
```

выполняется из контейнера Knowledge Factory либо одноразового контейнера в
`kf-backend`, поскольку порт Qdrant не публикуется на хост.

Gate:

- collection status `green`;
- `points_count=620` либо новый зафиксированный эталон.

## 14. Совместимое монтирование source tree

В первый production Compose:

```yaml
volumes:
  - type: bind
    source: /srv/knowledge-factory/data/sources
    target: /Users/romanmizanov/Documents/Цифровой мозг
    read_only: true
  - type: bind
    source: /srv/knowledge-factory/data/models
    target: /data/models
```

Это намеренно сохраняет старое пространство имён. Не заменять target на
`/data/sources` до отдельной миграции путей.

## 15. Shadow-запуск на Aeza

Поднять приложение, но пока не менять Hermes:

```bash
set -euo pipefail
cd /srv/knowledge-factory/app
docker compose -f compose.production.yaml up -d knowledge-factory
docker compose -f compose.production.yaml ps
docker compose -f compose.production.yaml logs \
  --since=10m knowledge-factory
```

Проверки из Docker network:

1. `/healthz` отвечает 200.
2. `/mcp` без токена отвечает 401.
3. `/mcp` с неверным токеном отвечает 401.
4. MCP с правильным токеном перечисляет ровно:
   - `search_knowledge`;
   - `ask`;
   - `stats`.
5. `stats`:
   - 96 документов;
   - 620 чанков;
   - 620 точек;
   - 37 OCR-документов сохраняются в PostgreSQL.

### 15.1. Прогрев и RAM gate

Выполнить минимум три поиска, включая кириллицу. Одновременно:

```bash
docker stats --no-stream
free -h
dmesg --ctime | grep -Ei 'oom|out of memory|killed process' || true
```

Gate:

- ни одного OOM;
- swap не расходуется постоянно;
- после прогрева available RAM остаётся не менее 1.5 ГБ;
- контейнер не перезапускается;
- первый запрос и последующие запросы имеют измеренное приемлемое время.

Если gate не пройден:

- не переключать Hermes;
- сначала уменьшить память/модель либо вынести фабрику на отдельный VPS;
- не лечить OOM бесконечным swap.

### 15.2. Сравнение результатов Mac и Aeza

Подготовить не менее 10 read-only запросов:

- 4 семантических;
- 3 с точными именами/терминами;
- 2 по OCR-документам;
- 1 заведомо отсутствующий.

Сравнить:

- наличие ожидаемого документа в top results;
- `via_ocr`;
- отсутствие пустых ответов;
- отсутствие абсолютных Linux-путей;
- отсутствие утечки токена;
- `ask` возвращает связный ответ и ссылки на источники.

Допустимы небольшие различия порядка при равных score, но не потеря
релевантных документов.

## 16. Подключение Hermes

Перед изменением сохранить резервную копию:

```bash
cp -a /srv/hermes/data/config.yaml \
  "/srv/hermes/data/config.yaml.pre-kf-cutover-$(date -u +%Y%m%dT%H%M%SZ)"
```

Заменить только URL Knowledge Factory:

```yaml
mcp_servers:
  knowledge_factory:
    url: http://knowledge-factory:8000/mcp
    headers:
      Authorization: Bearer ${KF_MCP_TOKEN}
```

Сохранить текущие `timeout`, `connect_timeout` и
`supports_parallel_tool_calls`, если они уже присутствуют.

Проверить YAML и env placeholder до рестарта. Не печатать token.

Пересоздать Hermes после добавления `hermes-internal`:

```bash
set -euo pipefail
cd /srv/hermes/app/deploy/beget
docker compose config --quiet
docker compose up -d --no-deps hermes
docker inspect -f \
  '{{.State.Status}} running={{.State.Running}} restarts={{.RestartCount}}' \
  hermes
```

Проверить DNS:

```bash
docker exec hermes getent hosts knowledge-factory
```

Основной тест:

```bash
docker exec hermes hermes mcp test knowledge_factory
```

Выполнить 5 раз. Требование: 5/5 подключений, 3 инструмента.

Функциональные проверки:

- прямой `stats`;
- read-only `search_knowledge`;
- один `ask`;
- один `delegate_task` с Knowledge Factory только на чтение;
- Telegram принимает сообщение и доставляет ответ;
- в логах нет traceback, 401, 403, 5xx, timeout, OOM и рестартов.

## 17. Cutover и rollback

### Успешный cutover

Cutover считается завершённым только если одновременно:

- новый MCP test 5/5;
- `stats` совпадает с эталоном;
- golden queries пройдены;
- Telegram работает;
- контейнеры stable не менее 30 минут;
- RAM gate пройден;
- публичные порты 5432/6333/8000 отсутствуют.

### Мгновенный rollback

До конца карантина Mac MCP и Tailscale оставляются включёнными.

При проблеме:

1. вернуть прежний URL
   `https://macbook-air-od.tail0483d9.ts.net/mcp`;
2. вернуть прежний token, если он был одновременно ротирован;
3. выполнить `docker compose up -d --no-deps hermes`;
4. проверить `hermes mcp test knowledge_factory`;
5. не запускать `kf update` одновременно на Mac и Aeza.

Rollback не требует обратного переноса данных, пока Aeza работает в режиме
read-only serving и автоиндексация выключена.

## 18. Этап B — полностью автономная индексация

После минимум 3–7 дней стабильного поиска перейти к Linux OCR.

### 18.1. Backend

Рекомендуемый первый кандидат — локальный Tesseract с русским и английским
языковыми пакетами из-за предсказуемого контейнерного развёртывания и умеренной
RAM. Если качество ниже допустимого, отдельно оценить PaddleOCR.

Backend должен:

- работать без внешнего OCR API;
- поддерживать русский и английский;
- возвращать текст, confidence и структурированную ошибку;
- не отправлять документы третьим лицам;
- ограничивать DPI и память;
- иметь timeout на страницу;
- не ронять весь ingest из-за одной страницы.

### 18.2. Проверка качества

Сформировать эталон минимум из 10 страниц текущих 37 OCR-документов:

- простой печатный текст;
- таблица;
- мелкий шрифт;
- смешанный русский/английский;
- плохой контраст.

Сравнить с Apple Vision:

- потеря ключевых слов;
- читаемость;
- confidence;
- результаты тех же поисковых запросов;
- время и пиковая RAM.

Linux OCR принимается только если не ухудшает критические golden queries.

### 18.3. Портативные относительные пути

Отдельным коммитом:

1. хранить document path относительно `KNOWLEDGE_ROOT`;
2. перед миграцией проверить отсутствие коллизий;
3. сделать logical backup PostgreSQL и Qdrant snapshot;
4. обновить `documents.path`;
5. обновить payload `path` у всех точек Qdrant;
6. сохранить существующие `doc_id` и point IDs;
7. изменить scan/ingest/prune/search на единый relative path contract;
8. добавить E2E-тест перемещения одного и того же snapshot между двумя разными
   абсолютными roots;
9. только после проверки изменить mount target на `/data/sources`.

После этого:

```dotenv
KNOWLEDGE_ROOT=/data/sources
```

Первый `kf update` после path migration запускается вручную. Gate:

- `added=0`;
- `changed=0`;
- `moved=0`;
- `deleted=0`;
- `failed=0`;
- 96 документов / 620 чанков / 620 точек остаются без изменений.

Любое массовое добавление/удаление означает ошибку миграции путей.

### 18.4. Канал добавления документов

Полная автономность требует определить, как новые документы попадают на VPS.
Рекомендуемый минимальный вариант:

- SFTP/rsync в `/srv/knowledge-factory/data/sources`;
- отдельный непривилегированный SSH user;
- ключ ограничен только этим назначением;
- после загрузки ежедневный `kf update`.

Mac может быть одним из клиентов загрузки, но не должен быть обязательным для
работы сервиса.

### 18.5. Расписание

Не помещать scheduler внутрь основного MCP-процесса. Использовать systemd timer
или отдельный одноразовый Compose service:

```text
knowledge-factory-update
```

Требования:

- тот же image и volumes;
- advisory lock уже защищает от двух writer;
- timeout;
- логи;
- ненулевой exit code при degraded/error;
- уведомление при failed;
- ни при каких условиях не запускать второй scheduler на Mac.

## 19. Бэкапы

Снапшот Aeza полезен, но не заменяет application-consistent backup.

Ежедневно:

1. `pg_dump --format=custom`;
2. Qdrant collection snapshot;
3. архив source tree;
4. manifest SHA-256;
5. шифрование;
6. копия вне Aeza;
7. ротация, например 7 daily + 4 weekly.

Model cache можно не включать в off-site backup: он восстанавливается из
фиксированных model IDs. Код восстанавливается из Git bundle/private remote.

Раз в месяц выполнять restore drill в изолированный Compose project и
подтверждать:

- PostgreSQL restore;
- Qdrant recovery;
- exact stats;
- один реальный поиск.

## 20. Мониторинг

Минимум:

- Docker restart count;
- health status трёх контейнеров;
- `stats` и равенство chunks/points;
- свободная RAM/swap;
- свободный диск;
- время последнего успешного `kf update`;
- HTTP 401 без токена как security check;
- отсутствие публичных портов БД/MCP.

Не мониторить `/mcp` внешним публичным сервисом: endpoint намеренно внутренний.

## 21. Завершение карантина

Через 3–7 дней стабильной работы:

1. снять финальный backup Mac;
2. отключить Mac LaunchAgent MCP;
3. убедиться, что при выключенном Mac:
   - `hermes mcp test knowledge_factory` проходит;
   - search/ask/stats работают;
   - Telegram отвечает;
4. удалить `extra_hosts` Mac из Hermes Compose;
5. повторно пересоздать и проверить Hermes;
6. удалить Tailscale-маршрут только если он больше нигде не используется;
7. отозвать старый MCP token;
8. удалить staging-файлы с dump/snapshot после подтверждённого off-site backup.

## 22. Финальные acceptance criteria

- [x] Knowledge Factory image собирается для `linux/amd64`.
- [x] `ocrmac` не требуется для запуска MCP в Linux.
- [x] PostgreSQL и Qdrant не имеют published ports.
- [x] MCP не имеет published port.
- [x] Bearer обязателен.
- [x] Hermes резолвит `knowledge-factory` через Docker DNS.
- [x] `hermes mcp test knowledge_factory`: 5/5.
- [x] Доступны ровно `search_knowledge`, `ask`, `stats`.
- [x] Документы/чанки/точки совпадают с эталоном.
- [x] Golden queries сохраняют ожидаемые источники: 7/10 ответов совпали
  побайтово, в трёх изменился только порядок/текст без потери эталонных
  документов.
- [x] Нет OOM и неожиданных рестартов.
- [x] Отключение локального MCP на Mac не влияет на `search`/`ask`/`stats`:
  при полностью выгруженном LaunchAgent сервер прошёл `mcp test` 5/5, после
  чего локальный MCP был возвращён для других проектов.
- [ ] Для полной автономной индексации принят Linux OCR.
- [x] Первый Linux `kf update` не создаёт дубликаты: изменён один Markdown,
  итог остался 96 документов / 620 чанков / 620 точек.
- [x] Настроены logical backups и проверен restore.
- [x] Есть проверенный rollback на Mac на время карантина.

## 23. Рекомендуемая последовательность коммитов

Чтобы не смешивать риски:

1. `refactor(ocr): make OCR backend platform-specific`;
2. `feat(deploy): add internal Docker MCP mode`;
3. `build(docker): add linux production image and compose`;
4. `test(deploy): add Linux import, auth and network tests`;
5. после успешного cutover:
   `refactor(paths): store source paths relative to knowledge root`;
6. затем:
   `feat(ocr): add and validate Linux OCR backend`;
7. затем:
   `feat(update): add server-side scheduled ingestion`.

Не объединять path migration, Linux OCR и первый production cutover в один
коммит или один необратимый шаг.

## 24. Синхронное пополнение Mac и Aeza

Реализовано 2026-07-26. Mac остаётся единственным источником истины для
документов:

```text
Цифровой мозг на Mac
   ├── локальный kf update
   └── filtered rsync по SSH
          └── incoming-sources на Aeza
                 └── SHA-манифест совпал
                        └── общий lock с backup
                               ├── atomic mirror в production sources
                               └── kf update на Aeza
```

PostgreSQL и Qdrant между машинами не копируются. Каждая фабрика строит свой
индекс из одинакового дерева источников.

### 24.1. Локальная автоматика

- LaunchAgent:
  `~/Library/LaunchAgents/com.knowledge-factory.sync-to-aeza.plist`;
- исходный plist:
  `deploy/macos/com.knowledge-factory.sync-to-aeza.plist`;
- установщик:
  `scripts/install_sync_launchagent.sh`;
- рабочая копия скриптов вне TCC-защищённой папки Documents:
  `~/.local/share/knowledge-factory/sync-runtime`;
- поведенческая конфигурация:
  `~/.config/knowledge-factory/sync.conf`;
- закрытый ключ:
  `~/.ssh/kf_sync_aeza`, mode 0600;
- stdout/stderr:
  `~/Library/Logs/knowledge-factory/kf-sync.{out,err}.log`;
- интервал: 600 секунд.

LaunchAgent запускается через `~/.hermes/bin/uv`: этот runtime уже имеет
разрешение macOS на чтение Documents. Запуск нового `/bin/bash` напрямую из
Documents блокируется TCC с `Operation not permitted`.

Старый `com.knowledge-factory.autoupdate` остаётся disabled. Его функцию
выполняет новый sync-wrapper, который сначала обновляет локальную фабрику.

### 24.2. Фильтр и целостность

Передаются только расширения, которые понимает `kf.sources.scan`:
`.md`, `.txt`, `.pdf`, `.docx`, `.csv`. Исключаются `.git`, virtualenv,
`node_modules`, cache и build-каталоги.

`scripts/source_manifest.py` строит детерминированный SHA-256 manifest.
Серверный update не запускается, пока локальный и удалённый manifests не
совпадут побайтово.

Rsync никогда не пишет прямо в production source tree. Он заполняет
`/srv/knowledge-factory/data/incoming-sources`; затем server wrapper под
`knowledge-factory-update.lock` зеркалирует incoming в
`/srv/knowledge-factory/data/sources` и запускает индексатор. Ежедневный backup
берёт тот же lock, поэтому не может сохранить новые документы со старым
PostgreSQL/Qdrant индексом.

Удаление включено через `--delete-delay`, но fail-closed:

- источник обязан содержать карту базы и минимум 50 поддерживаемых файлов;
- максимум 10 удаляемых документов за один цикл;
- максимум 20% от удалённого manifest;
- число удалений считается по путям двух manifests, а не по строкам rsync;
- при превышении лимита rsync не меняет сервер;
- каталоги защищены от удаления, поэтому неиндексируемые вложения не создают
  шум и ложный расход deletion budget.

### 24.3. Ограниченный SSH-контур

Автоматика не использует основной root-ключ. На Aeza создан пользователь
`kf-sync` и отдельный forced-command ключ с `restrict`.

Dispatcher `/usr/local/sbin/kf-sync-dispatch` разрешает только:

1. принимающий rsync строго в
   `/srv/knowledge-factory/data/incoming-sources/`;
2. `kf-manifest`;
3. `kf-update` через единственную sudo-команду
   `/usr/local/sbin/kf-update-production`.

Произвольная команда возвращает exit code 126. Wrapper обновления использует
`flock`, проверяет Compose, source count, последний статус update и полный
production healthcheck. Занятый lock возвращает exit code 75, поэтому
LaunchAgent не выдаёт пропущенный update за успешную синхронизацию и повторяет
цикл через 10 минут.

### 24.4. Подтверждённые сценарии

- добавление Markdown: обе базы 97 документов / 621 чанк;
- изменение: старый чанк заменён новым на обеих машинах;
- перенос без delete: остановка на различии manifests, серверный update не
  запущен;
- перенос и удаление с delete: оба индекса обновлены;
- после удаления тестового знания обе базы вернулись к 96/620/620;
- 11 удалений при лимите 10 остановлены до изменения сервера;
- недоступная Aeza завершает цикл ошибкой до локального update/rsync;
- параллельный запуск пропускается по lock;
- серверный update при занятом backup/update lock возвращает 75;
- LaunchAgent прошёл реальный цикл с exit code 0.

### 24.5. Проверка и rollback

```bash
launchctl print gui/$(id -u)/com.knowledge-factory.sync-to-aeza
tail -100 ~/Library/Logs/knowledge-factory/kf-sync.out.log
tail -100 ~/Library/Logs/knowledge-factory/kf-sync.err.log
```

На Aeza:

```bash
docker exec knowledge-factory kf stats
/srv/knowledge-factory/app/scripts/healthcheck_production.sh
```

Остановить синхронизацию без остановки обеих фабрик:

```bash
launchctl bootout \
  gui/$(id -u)/com.knowledge-factory.sync-to-aeza
```

Для временного запрета удалений установить `ALLOW_DELETE=0` в
`~/.config/knowledge-factory/sync.conf`. Резервные копии конфигурации первого
включения:

- Mac:
  `~/.local/share/knowledge-factory/sync-backups/20260726T083402Z`;
- Aeza:
  `/srv/knowledge-factory/backups/sync-config-20260726T083402Z`.

Linux OCR по-прежнему выключен. Новые Markdown/TXT/DOCX/CSV и PDF с текстовым
слоем синхронизируются автоматически; новые сканированные PDF будут полностью
одинаково индексироваться только после приёмки Этапа B.
