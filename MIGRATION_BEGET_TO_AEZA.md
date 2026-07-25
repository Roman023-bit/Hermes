# Миграция Hermes: Beget → Aeza (runbook)

> **Статус: планирование. Миграцию НЕ выполнять по этому документу автоматически.**
> Это проверенный пошаговый runbook. Серверы, Docker, конфигурацию и
> репозиторий он **не изменяет** — единственный созданный артефакт это сам
> файл `MIGRATION_BEGET_TO_AEZA.md`.

Каждая команда помечена местом выполнения:

- **[MAC]** — локальный MacBook Романа (`/Users/romanmizanov/Documents/Hermes`).
- **[BEGET]** — старый VPS (внутри SSH-сессии на Beget).
- **[AEZA]** — новый VPS (внутри SSH-сессии на Aeza).

Секреты (`.env`, bot token, API-ключи) **нигде не печатаются**. Проверяется
только факт `set`/`missing` и имена переменных.

---

## 0. Переменные окружения (определить ПЕРВЫМИ)

**[MAC]** — выполнить в начале каждой новой Mac-сессии. Все дальнейшие
Mac-команды используют эти переменные, а не «сырые» IP и пути.

```bash
OLD_IP="155.212.224.126"
NEW_IP="REPLACE_WITH_AEZA_IP"          # подставить реальный IPv4 Aeza
OLD_KEY="$HOME/.ssh/beget_hermes"
NEW_KEY="$HOME/.ssh/aeza_hermes"
MIG_STAGE="$HOME/hermes-migration"     # промежуточный каталог на Mac
```

Фиксированные пути одинаковы на **обоих** серверах, поэтому в блоках
**[BEGET]** и **[AEZA]** используются как литералы:

```text
APP_DIR   = /srv/hermes/app
DATA_DIR  = /srv/hermes/data
BACKUP_DIR= /srv/hermes/backups
COMPOSE   = deploy/beget/compose.yaml   # Compose-файл переиспользуется на Aeza без переименования
```

Компоненты, которые НЕЛЬЗЯ хардкодить:

- **Версия приложения (commit SHA)** — берётся с работающего Beget
  непосредственно перед миграцией (см. Фаза 6), а не с Mac и не из
  `origin/main`.
- **IP Aeza** — присваивается при заказе VPS (Фаза 2).

---

## Принципы (почему такой порядок)

1. **Данные — источник истины на Beget.** `/srv/hermes/data` (≈285 МБ:
   `config.yaml`, `.env`, sessions, memories, skills) на проде свежее, чем на
   Mac. Поэтому сначала — аварийный backup (Фаза 1), пока доступ жив.
2. **Один bot token = один gateway.** Telegram работает через polling; два
   gateway с одним токеном дают `getUpdates conflict`. Beget работает до
   последнего, Aeza включается только в момент cutover после остановки Beget.
3. **Проверка IP Aeza до переноса.** OpenRouter ранее блокировал IP другого
   VPS через Cloudflare (403). IP Aeza проверяется до миграции (Фаза 3).
4. **Нулевая потеря данных на переключении.** Финальный консистентный rsync
   делается уже после остановки контейнера Beget (Фаза 8).

---

## Фаза 1 — 🔴 Аварийный backup Beget (выполнить СЕГОДНЯ, пока доступ есть)

Различаем два backup:

- **Аварийная живая копия** — сейчас, при работающем Hermes (файловый срез;
  SQLite в WAL-режиме допускает небольшую несогласованность, приемлемо для
  disaster recovery).
- **Финальный консистентный backup** — позже, после остановки контейнера
  (Фаза 8), когда запись в `state.db` прекращена.

### 1.1 Живой backup на самом Beget

**[MAC]** — открыть SSH-сессию на Beget. Сама команда набирается на Mac; всё
последующее, помеченное **[BEGET]**, выполняется уже внутри этой сессии.

```bash
ssh -i "$OLD_KEY" root@"$OLD_IP"
```

**[BEGET]**

```bash
/srv/hermes/app/deploy/beget/backup.sh
```

Скрипт создаёт верифицированный `chmod 600` архив в `/srv/hermes/backups/`
(`hermes-<timestamp>.tar.gz`), сам проверяет его через `tar -tzf` и хранит
последние 7 копий.

### 1.2 Забрать архивы и живое дерево данных на Mac (off-site копия)

**[MAC]** — подготовить защищённый каталог с ограниченными правами (архивы
содержат `.env`):

```bash
install -d -m 0700 "$MIG_STAGE" "$MIG_STAGE/beget-backups" "$MIG_STAGE/data"
```

> **rsync на macOS — это openrsync.** Системный `/usr/bin/rsync` в актуальных
> macOS — не GNU rsync, а openrsync (`rsync --version` печатает
> `protocol version 29 / rsync version 2.6.9 compatible`). Он **не понимает**
> `--info=progress2` (флаг из GNU rsync 3.1+) и падает с
> `unrecognized option`. Поэтому в Mac-командах ниже используется `--progress`,
> который openrsync поддерживает. Если нужен именно GNU rsync —
> `brew install rsync` и вызывать `/opt/homebrew/bin/rsync`.
>
> Это касается **только команд [MAC]**. Серверные rsync (Фазы 7 и 9)
> инициируются с Ubuntu, где стоит GNU rsync, — там `-aHAX` и остальные
> GNU-флаги работают и менять их не нужно.

**[MAC]** — скачать готовые архивы:

```bash
rsync -a --progress -e "ssh -i $OLD_KEY" \
  root@"$OLD_IP":/srv/hermes/backups/ "$MIG_STAGE/beget-backups/"
```

**[MAC]** — снять «живое» дерево данных целиком, без исключений (ни `bin`, ни
`logs`): эта копия служит источником для fallback-переноса через Mac (Фазы 7.4,
9-bis), поэтому должна быть полным зеркалом:

```bash
rsync -a --progress \
  -e "ssh -i $OLD_KEY" \
  root@"$OLD_IP":/srv/hermes/data/ "$MIG_STAGE/data/"
```

### 1.3 Проверить целостность архива

**[MAC]** — определить последний архив и проверить:

```bash
ARCHIVE="$(ls -1t "$MIG_STAGE"/beget-backups/hermes-*.tar.gz | head -n1)"
echo "ARCHIVE=$ARCHIVE"
```

**[MAC]**

```bash
tar -tzf "$ARCHIVE" >/dev/null && echo "archive readable: OK"
```

**[MAC]**

```bash
shasum -a 256 "$ARCHIVE"
```

> Архив содержит `.env` с секретами. Хранить только в каталоге с правами
> `0700`, для длительного хранения — зашифровать (например `gpg -c`), не
> класть в облако/paste в открытом виде.

✅ **Критерий фазы:** архив читается, посчитан SHA-256, `config.yaml` и `.env`
присутствуют в `$MIG_STAGE/data/`. После этого потеря доступа к Beget уже не
приводит к потере данных.

---

## Фаза 2 — Заказ VPS на Aeza

Ручное действие владельца в панели Aeza. Рекомендуемая конфигурация:

| Параметр | Значение | Почему |
|---|---|---|
| Тариф | **полноценный Shared VPS**, не Promo | Promo-тарифы Aeza имеют ограничения/оверселлинг и хуже подходят под постоянный прод |
| Оплата | **почасовая** на период проверки | если IP заблокирован Cloudflare/OpenRouter — пересоздать VPS без потери оплаты за месяц |
| Локация | **Европа, первый выбор — Warsaw** | низкая латентность к RU, обычно чистые IP; но локация **не гарантирует** отсутствие блокировки (см. Фаза 3) |
| ОС | **Ubuntu 24.04 x86_64** | как на Beget, совместимо с Docker-образом |
| vCPU | минимум **2** | сборка образа + Playwright/Chromium |
| RAM | **4 ГБ** | образ собирается на сервере; на 2 ГБ сборка падает по OOM |
| Диск | **минимум 60 ГБ NVMe** | исходники (~193 МБ) + build cache + слои образа + бэкапы; 30–40 ГБ на Beget уже заняты на ~70% (27/38 ГБ) — новый сервер не должен стартовать в том же дефиците |
| Сеть | публичный **IPv4** | outbound к провайдерам + inbound SSH |
| SSH | отдельный ключ `~/.ssh/aeza_hermes` | не переиспользуем `beget_hermes` |

**Почему не 2 ГБ RAM и не 30–40 ГБ диска:**

- 2 ГБ RAM — `docker compose build` образа Hermes (Python + Node + Playwright
  Chromium) уходит в OOM без swap; лечится swap-костылём, но 4 ГБ надёжнее.
- 30–40 ГБ диск — build cache + несколько тегов образа (нужны для rollback в
  `deploy.sh`) + растущие бэкапы быстро упираются в лимит; на Beget уже 27 ГБ
  из 38 занято.

Сгенерировать SSH-ключ Aeza заранее, **[MAC]**:

```bash
test -f "$NEW_KEY" || ssh-keygen -t ed25519 -a 64 -f "$NEW_KEY" -C "aeza-hermes"
```

От владельца после создания VPS нужны только: IP/hostname и подтверждение
входа по ключу. Пароль от панели Aeza в чат/файлы не передавать.

---

## Фаза 3 — Проверка нового IP Aeza ДО миграции (блокирующий гейт)

Как только VPS создан и доступен по SSH — проверить, что с IP Aeza доступны
все критичные сервисы. **Это блокирующий этап: при провале OpenRouter
миграцию не продолжать.**

**[MAC]** — открыть SSH-сессию на Aeza. Сама команда набирается на Mac; всё
последующее, помеченное **[AEZA]**, выполняется уже внутри этой сессии.

```bash
ssh -i "$NEW_KEY" root@"$NEW_IP"
```

### 3.1 OpenRouter (главная проверка — риск Cloudflare 403)

**[AEZA]**

```bash
curl -4 -sS \
  -o /tmp/openrouter-models.json \
  -w 'HTTP %{http_code}\n' \
  https://openrouter.ai/api/v1/models
```

**Критерий:** `HTTP 200` **и** валидный JSON со списком моделей:

**[AEZA]**

```bash
head -c 200 /tmp/openrouter-models.json; echo
```

Должно начинаться с `{"data":[` и содержать модели.

**Если получен Cloudflare `HTTP 403`:**

1. НЕ продолжать миграцию.
2. Сменить IP: пересоздать почасовой VPS (получить другой IP) **или** сменить
   локацию.
3. Повторить проверку 3.1.
4. Локация сама по себе **не гарантирует** отсутствие блокировки — критерий
   только фактический `HTTP 200`.

### 3.2 Остальные endpoint'ы

**[AEZA]**

```bash
for url in \
  https://api.telegram.org \
  https://github.com \
  https://registry-1.docker.io/v2/ \
  https://pypi.org/simple/ \
  https://registry.npmjs.org/ \
  https://api.perplexity.ai ; do
  code="$(curl -4 -sS -o /dev/null -w '%{http_code}' --max-time 15 "$url")"
  printf '%-45s HTTP %s\n' "$url" "$code"
done
```

**Критерий:** каждый endpoint отвечает по TCP/TLS без сетевой ошибки.
Коды `200/301/401/404` от самого сервиса допустимы (сервис достижим).
Недопустимы: таймаут, `000` (нет соединения), `403` от Cloudflare на
OpenRouter, connection refused.

---

## Фаза 4 — Подготовка Aeza (базовая настройка + Docker)

Все команды **[AEZA]**.

### 4.1 Обновление системы

```bash
apt-get update && apt-get -y upgrade
```

### 4.2 Docker Engine + Compose plugin

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
docker version && docker compose version
```

```bash
systemctl enable --now docker
```

### 4.3 Firewall (UFW) — наружу только SSH

Сначала явно задать политики по умолчанию — не полагаться на то, что дистрибутив
уже выставил их правильно:

```bash
ufw default deny incoming
ufw default allow outgoing
```

```bash
ufw allow OpenSSH
```

```bash
ufw --force enable
```

```bash
ufw status verbose
```

> Порты Hermes `8642`/`9119` наружу НЕ открывать — они публикуются только на
> `127.0.0.1` (см. `compose.yaml`) и доступны через SSH-туннель.

⚠️ **Перед закрытием текущей SSH-сессии** открыть **вторую** сессию и
убедиться, что вход работает — иначе можно потерять доступ:

**[MAC]** (в отдельном терминале, не закрывая первую сессию):

```bash
ssh -i "$NEW_KEY" root@"$NEW_IP" 'echo "second session OK"'
```

### 4.4 Fail2ban + unattended-upgrades (как на Beget)

```bash
apt-get install -y fail2ban unattended-upgrades
```

```bash
systemctl enable --now fail2ban
```

```bash
dpkg-reconfigure -f noninteractive unattended-upgrades
```

### 4.5 Отключить парольный SSH-вход — ТОЛЬКО после проверки ключа

Убедившись, что вход по ключу работает (4.3):

Закрываем не только парольный вход, но и смежные пути: `keyboard-interactive`
(отдельный механизм, через который пароль может приниматься в обход
`PasswordAuthentication`) и вход root по паролю.

```bash
cat > /etc/ssh/sshd_config.d/99-hermes-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
```

```bash
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
```

```bash
sshd -t && systemctl reload ssh
```

> Дроп-ин назван `99-*`, но имя само по себе приоритета не даёт: в sshd
> выигрывает **первое** встреченное значение, а `Include` стоит в начале
> основного файла — поэтому дроп-ины и перекрывают его. Если в каталоге уже
> лежит `50-cloud-init.conf` с `PasswordAuthentication yes`, он идёт раньше по
> алфавиту и победит. Именно поэтому ниже проверяется фактический результат, а
> не факт записи файла.
>
> `PermitRootLogin prohibit-password` оставляет вход root по ключу (он здесь
> используется) и запрещает по паролю.

⚠️ **Правки основного файла может быть недостаточно — обязательно проверить
эффективное значение.** В Ubuntu 24.04 `/etc/ssh/sshd_config` начинается с
`Include /etc/ssh/sshd_config.d/*.conf`, а облачные образы кладут туда
`50-cloud-init.conf` с `PasswordAuthentication yes`. sshd берёт **первое**
встреченное значение, поэтому `sed` по основному файлу окажется no-op, а
парольный вход останется включённым.

```bash
sshd -T | grep -iE '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin)'
```

**Критерий:** ровно три строки —

```text
passwordauthentication no
kbdinteractiveauthentication no
permitrootlogin without-password
```

> ⚠️ Третья строка — **не опечатка**. В конфиге пишется
> `PermitRootLogin prohibit-password`, но `sshd -T` нормализует значение к
> устаревшему синониму и печатает `without-password`. Это одно и то же
> (проверено на Aeza, OpenSSH из Ubuntu 24.04). Ждать в выводе
> `prohibit-password` — значит завалить проверку на ровном месте.
>
> Дополнительно полезно увидеть `pubkeyauthentication yes` — без него
> отключение пароля означало бы потерю доступа.

Если что-то из этого отличается — найти и обезвредить дроп-ин, который
перекрывает настройку:

```bash
grep -rniE 'passwordauthentication|kbdinteractive|permitrootlogin' \
  /etc/ssh/sshd_config.d/ 2>/dev/null
```

```bash
sed -i -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
       -e 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' \
       /etc/ssh/sshd_config.d/50-cloud-init.conf
sshd -t && systemctl reload ssh
sshd -T | grep -iE '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin)'
```

> Не закрывать текущую SSH-сессию, пока `sshd -T` не показал `no` **и** вторая
> сессия по ключу (проверка из 4.3) не подтвердила вход.

Учесть: `sshd -T` читает **файлы**, а не состояние работающего демона. Если
конфиг правили, но не перезагрузили сервис, вывод покажет новые значения,
которых в памяти демона ещё нет. Поэтому после правок — обязательный reload и проверка
живой сессией:

```bash
sshd -t && systemctl reload ssh && systemctl is-active ssh
```

**[MAC]** — позитивный тест (доступ не потерян) и негативный (пароль больше не
принимается):

```bash
ssh -i "$NEW_KEY" -o BatchMode=yes root@"$NEW_IP" 'echo key_access_OK'
```

```bash
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password \
    -o NumberOfPasswordPrompts=1 -o BatchMode=yes \
    root@"$NEW_IP" 'echo THIS_SHOULD_NOT_PRINT' 2>&1 | tail -1
```

**Критерий:** первая команда печатает `key_access_OK`, вторая —
`Permission denied (publickey).`. Только вывод второй команды доказывает, что
парольный вход действительно закрыт: наличие строки в конфиге само по себе не
доказывает ничего.

### 4.6 Swap 2 ГБ (при необходимости для сборки)

Если RAM 4 ГБ и сборка всё равно рискует OOM, либо на всякий случай:

```bash
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show
```

### 4.7 Каталоги (идентичны Beget)

```bash
install -d -m 0755 /srv/hermes/app
```

```bash
install -d -m 0700 /srv/hermes/data
```

```bash
install -d -m 0700 /srv/hermes/backups
```

> **Запрещённые команды на всех этапах:** `docker system prune -a`,
> `git reset --hard`, `docker compose down -v`. Первая удалит образы, нужные
> для rollback; вторая перезапишет неизвестные изменения; третья уничтожит
> данные.

---

## Фаза 5 — Проверка диска перед сборкой

**[AEZA]**

```bash
df -h /
```

```bash
free -h
```

**Критерий:** свободно ≥15 ГБ на `/`, иначе сборку не начинать.

---

## Фаза 6 — Версия приложения (источник — работающий Beget)

Версию берём с прода, а не с Mac и не из `origin/main`.

### 6.1 Получить SHA с Beget

**[BEGET]** — сначала убедиться, что прод-дерево чистое. Одного совпадения SHA
недостаточно: незакоммиченные правки на Beget в clone на Aeza не попадут, и при
формально одинаковом `HEAD` соберётся другой образ.

```bash
git -C /srv/hermes/app status --short
```

**Критерий:** вывод пустой. Единственное, что допустимо, — отсутствие
gitignored-файлов в выводе (`deploy/beget/.env` игнорируется и здесь не
показывается; он переносится отдельно в Фазе 7.5).

Если вывод НЕ пустой — остановиться и разобраться: зафиксировать, что именно
изменено (`git -C /srv/hermes/app diff`), и либо закоммитить и запушить эти
правки в `origin/main`, либо перенести их на Aeza вручную после clone. Не
продолжать, пока расхождение не учтено.

**[BEGET]**

```bash
OLD_SHA="$(git -C /srv/hermes/app rev-parse HEAD)"
echo "OLD_SHA=$OLD_SHA"
```

Записать это значение — оно понадобится на Aeza. (Можно передать файлом:)

**[BEGET]**

```bash
echo "$OLD_SHA" > /srv/hermes/OLD_SHA.txt
```

**[MAC]**

```bash
scp -i "$OLD_KEY" root@"$OLD_IP":/srv/hermes/OLD_SHA.txt "$MIG_STAGE/OLD_SHA.txt"
scp -i "$NEW_KEY" "$MIG_STAGE/OLD_SHA.txt" root@"$NEW_IP":/srv/hermes/OLD_SHA.txt
```

### 6.2 Клонировать репозиторий и checkout точного SHA на Aeza

**[AEZA]**

```bash
git clone --filter=blob:none --single-branch --branch main \
  https://github.com/Roman023-bit/Hermes.git /srv/hermes/app
```

**[AEZA]** — переставить ветку `main` на прод-коммит и встать на неё. Ключевое
здесь `-B`: обычный `git checkout <SHA>` оставил бы репозиторий в detached HEAD
навсегда, и локальная `main` продолжала бы указывать на коммит времени clone —
через несколько обновлений она отстала бы на десятки коммитов, а случайный
`git checkout main` молча откатил бы прод назад.

```bash
cd /srv/hermes/app
git checkout -B main "$(cat /srv/hermes/OLD_SHA.txt)"
```

**[AEZA]**

```bash
git -C /srv/hermes/app status --short --branch
git -C /srv/hermes/app rev-parse HEAD
```

**Критерий:** `HEAD` совпадает с `OLD_SHA` с Beget, рабочее дерево чистое, и
`status` показывает ветку — `## main...origin/main [behind N]`, а **не**
`## HEAD (no branch)`. Отставание на N коммитов здесь ожидаемо и правильно:
Aeza стартует ровно на той версии, что работает на проде, а не на свежем
`origin/main`.

> Hermes во время миграции **не обновлять**. Обновление (`deploy/beget/deploy.sh`)
> — отдельная операция уже после стабилизации на Aeza (Фаза 13). Оно штатно
> подтянет `origin/main` через `git pull --ff-only` и переведёт ветку вперёд.

### 6.3 Инвентаризация окружения Beget (crontab + timezone)

Переносится не только `/srv/hermes/data`. Это состояние живёт на уровне ОС и
через rsync НЕ попадёт на Aeza — снять его надо, пока Beget работает.

**[BEGET]** — все хостовые cron-задачи root:

```bash
crontab -l 2>/dev/null || echo "(root crontab пуст)"
```

```bash
ls -1 /etc/cron.d/ 2>/dev/null
```

Записать вывод. Фаза 12.2 восстанавливает на Aeza только ночной `backup.sh` —
если здесь обнаружилось что-то ещё, эти задания нужно перенести на Aeza
отдельно, иначе они будут молча потеряны.

**[BEGET]** — timezone хоста и контейнера:

```bash
timedatectl show -p Timezone --value
docker exec hermes date +'%Z %z'
```

**[AEZA]** — то же самое (контейнер ещё не запущен, поэтому только хост):

```bash
timedatectl show -p Timezone --value
```

**Критерий:** timezone хостов совпадает. От неё зависят расписания встроенного
планировщика Hermes (`cron/scheduler.py`) — при расхождении все задания
поедут по времени, причём молча, без ошибок в логах.

Если не совпадает — привести Aeza к значению Beget. Timezone подставляется через
переменную, а не угловыми скобками: `<...>` shell воспринимает как
перенаправление ввода-вывода, и команда сломается.

```bash
BEGET_TIMEZONE="Etc/UTC"   # подставить фактический вывод timedatectl с Beget
timedatectl set-timezone "$BEGET_TIMEZONE"
```

```bash
timedatectl show -p Timezone --value   # проверка: совпадает с Beget
```

> Замеренные значения на текущем проде: хост Beget — `Etc/UTC`, контейнер —
> `UTC +0000`. Если на Aeza по умолчанию окажется то же самое, шаг превращается
> в подтверждение, а не в изменение.

> `compose.yaml` не задаёт `TZ`, поэтому контейнер использует TZ образа (UTC).
> Проверка хостов нужна для cron-задач уровня ОС и для читаемости логов;
> расписания Hermes считаются от TZ контейнера и совпадут автоматически, если
> обе стороны собраны из одного образа.

---

## Фаза 7 — Перенос данных Beget → Aeza (предварительный)

Основной способ — прямой `rsync` Beget → Aeza по временному migration-ключу.
Fallback через Mac (tar с numeric-ownership) — в разделе 7.4.

### 7.1 Создать временный migration-ключ

**[MAC]**

```bash
ssh-keygen -t ed25519 -a 64 -f "$MIG_STAGE/migration_key" -C "hermes-migration-temp" -N ""
```

⚠️ **Канал настраивается сразу в ОБЕ стороны, пока оба сервера живы.**
Прямой перенос (Фазы 7, 9) идёт Beget → Aeza, но откат по Варианту B требует
обратного направления Aeza → Beget. Настраивать доступ в момент аварийного
отката — худший возможный момент: если Aeza деградировала, а канала назад нет,
откат становится невыполним. Поэтому приватный ключ кладётся на **оба**
сервера, а публичный прописывается в `authorized_keys` **обоих**, каждый со
своим `from=`.

Публичную часть добавить на Aeza, ограничив источником — только IP Beget
(`from=`), чтобы ключ работал лишь для этой операции:

**[MAC]**

```bash
PUB="$(cat "$MIG_STAGE/migration_key.pub")"
ssh -i "$NEW_KEY" root@"$NEW_IP" \
  "printf 'restrict,from=\"%s\" %s\n' '$OLD_IP' '$PUB' >> ~/.ssh/authorized_keys"
```

Симметрично — публичную часть на Beget, ограниченную IP Aeza (нужна только для
отката):

**[MAC]**

```bash
ssh -i "$OLD_KEY" root@"$OLD_IP" \
  "printf 'restrict,from=\"%s\" %s\n' '$NEW_IP' '$PUB' >> ~/.ssh/authorized_keys"
```

> **Про `restrict`.** Префикс `restrict` (OpenSSH 7.2+, есть в Ubuntu 24.04)
> отключает для этого ключа всё лишнее: проброс портов, agent- и
> X11-forwarding, выделение pty. Временный ключ используется только для
> неинтерактивного `rsync`/`tar` по SSH, которым ничего из этого не нужно, —
> поэтому ограничение ничего не ломает, но резко сужает то, что можно сделать
> ключом, если он утечёт. Вместе с `from=` получается ключ, работающий строго с
> одного IP и строго на выполнение команды.

Приватную часть положить на **оба** сервера — на Beget для прямого переноса, на
Aeza для отката:

**[MAC]**

```bash
scp -i "$OLD_KEY" "$MIG_STAGE/migration_key" root@"$OLD_IP":/root/.ssh/migration_key
ssh -i "$OLD_KEY" root@"$OLD_IP" 'chmod 600 /root/.ssh/migration_key'
```

**[MAC]**

```bash
scp -i "$NEW_KEY" "$MIG_STAGE/migration_key" root@"$NEW_IP":/root/.ssh/migration_key
ssh -i "$NEW_KEY" root@"$NEW_IP" 'chmod 600 /root/.ssh/migration_key'
```

Проверить оба направления **до** cutover — канал отката, который не проверен,
считается несуществующим:

**[MAC]**

```bash
ssh -i "$OLD_KEY" root@"$OLD_IP" \
  "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new \
   -o BatchMode=yes root@$NEW_IP 'echo beget_to_aeza_OK'"
```

**[MAC]**

```bash
ssh -i "$NEW_KEY" root@"$NEW_IP" \
  "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new \
   -o BatchMode=yes root@$OLD_IP 'echo aeza_to_beget_OK'"
```

**Критерий:** команды печатают ровно `beget_to_aeza_OK` и `aeza_to_beget_OK`.
Если вторая не работает — не начинать cutover: отката не будет (либо
использовать fallback через Mac, Фазы 9-bis и Rollback-bis).

> ⚠️ В маркерах намеренно нет символов `>` и `-`. Строка вида
> `echo beget->aeza OK` после двух вложенных SSH доходит до целевого shell как
> `echo beget- > aeza OK`, то есть `>` срабатывает как перенаправление:
> команда молча создаёт файл `aeza`, не печатает **ничего** и завершается с
> кодом 0. Проверка при этом выглядит «пройденной», хотя ничего не проверила.
> Маркеры должны состоять только из букв, цифр и `_`.

### 7.2 Предварительный dry-run прямого переноса

**[BEGET]** — сначала показать, что изменится (trailing slash обязателен: с
источника `/srv/hermes/data/` в назначение `/srv/hermes/data/`):

```bash
rsync -aHAX --numeric-ids --itemize-changes --dry-run \
  -e "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new" \
  /srv/hermes/data/ root@NEW_IP_HERE:/srv/hermes/data/
```

> Заменить `NEW_IP_HERE` на реальный IP Aeza (внутри SSH-сессии Beget
> переменных Mac нет). Не исключаем **ничего** — ни `bin`, ни `logs`;
> `--numeric-ids` сохраняет владельца `10000:10000`.
>
> **Почему логи переносятся, а не исключаются.** Ранее во всех rsync стоял
> `--exclude='/logs/'`. Это ломало проверку из Фазы 9, Шаг 9: `du -sb` считает
> и логи, поэтому размеры на Beget и Aeza заведомо расходились, и критерий
> «совпадают» превращался в оценку на глаз. Логи на текущем проде занимают
> ~3,7 МБ при ~265 МБ данных — перенос стоит порядка одного процента объёма и
> при этом делает `/srv/hermes/data` точным зеркалом, сравнение — строгим
> равенством, а история диагностики сохраняется. Исключения не должны
> появляться ни в одном rsync-переносе данных.

### 7.3 Предварительный реальный перенос

**[BEGET]**

```bash
rsync -aHAX --numeric-ids \
  -e "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new" \
  /srv/hermes/data/ root@NEW_IP_HERE:/srv/hermes/data/
```

> На этом (предварительном) шаге `--delete-delay` НЕ используем — Beget ещё
> пишет, полное зеркалирование делаем только в финальном cutover (Фаза 8).

### 7.4 Fallback: перенос через Mac (если прямое соединение невозможно)

Обычный `rsync -a` через macOS исказит Linux-владельца (`10000`), поэтому
используем tar с сохранением numeric ownership.

**[BEGET]** — упаковать с numeric-owner, целиком и без исключений (`bin` и
`logs` включены):

```bash
tar --numeric-owner -C /srv/hermes/data \
  -czf /srv/hermes/data-migration.tar.gz .
```

**[MAC]**

```bash
scp -i "$OLD_KEY" root@"$OLD_IP":/srv/hermes/data-migration.tar.gz "$MIG_STAGE/"
scp -i "$NEW_KEY" "$MIG_STAGE/data-migration.tar.gz" root@"$NEW_IP":/srv/hermes/
```

**[AEZA]** — распаковать с сохранением numeric ownership:

```bash
tar --numeric-owner -xzf /srv/hermes/data-migration.tar.gz -C /srv/hermes/data
```

### 7.5 Перенести `deploy/beget/.env` (gitignored — в clone его нет)

`deploy/beget/.env` содержит только несекретные Compose-параметры
(`HERMES_UID/GID`, `HERMES_DASHBOARD`), но он gitignored, поэтому после clone
его на Aeza нет.

**[MAC]**

```bash
scp -i "$OLD_KEY" root@"$OLD_IP":/srv/hermes/app/deploy/beget/.env "$MIG_STAGE/deploy.env"
scp -i "$NEW_KEY" "$MIG_STAGE/deploy.env" root@"$NEW_IP":/srv/hermes/app/deploy/beget/.env
```

**[AEZA]**

```bash
chmod 600 /srv/hermes/app/deploy/beget/.env
```

Если файла на Beget не оказалось — создать на Aeza из шаблона:

**[AEZA]**

```bash
cp /srv/hermes/app/deploy/beget/.env.example /srv/hermes/app/deploy/beget/.env
chmod 600 /srv/hermes/app/deploy/beget/.env
# значения по умолчанию: HERMES_UID=10000 HERMES_GID=10000 HERMES_DASHBOARD=0
```

### 7.6 Зафиксировать права данных на Aeza

**[AEZA]**

```bash
chmod 600 /srv/hermes/data/.env
chmod 640 /srv/hermes/data/config.yaml
install -d -m 0750 /srv/hermes/data/workspace
```

---

## Фаза 8 — Сборка образа на Aeza ДО cutover (gateway НЕ запускать)

⚠️ **В перенесённом `/srv/hermes/data/.env` уже лежит боевой Telegram
token.** Любой запуск `gateway run` начнёт polling и даст конфликт с
работающим Beget. Поэтому здесь — только сборка и smoke-тесты, которые
**не** стартуют gateway.

**[AEZA]**

```bash
cd /srv/hermes/app
export HERMES_GIT_SHA="$(git rev-parse HEAD)"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
```

**[AEZA]** — валидация Compose:

```bash
docker compose -f deploy/beget/compose.yaml config
```

**[AEZA]** — сборка образа:

```bash
docker compose -f deploy/beget/compose.yaml build --pull
```

**[AEZA]** — smoke-тесты через `run --rm` (одноразовый контейнер, gateway/polling НЕ стартует):

```bash
docker compose -f deploy/beget/compose.yaml run --rm hermes hermes --version
```

**[AEZA]**

```bash
docker compose -f deploy/beget/compose.yaml run --rm hermes hermes doctor
```

> Не выполнять здесь `up -d`, `gateway run`, `gateway start` или любую команду,
> запускающую polling. Только `config`, `build`, `run --rm ... --version`,
> `run --rm ... doctor`.

---

## Фаза 9 — Строгий cutover (ровно в этом порядке)

Нельзя допустить одновременную работу двух gateway.

**Шаг 1.** Убедиться, что Aeza собран, но gateway не запущен.

**[AEZA]**

```bash
docker ps --filter name=hermes --format '{{.Names}} {{.Status}}'
# ожидается: пусто (постоянного контейнера ещё нет)
```

**Шаг 2.** Повторно проверить OpenRouter с IP Aeza (гейт из Фазы 3.1).

**[AEZA]**

```bash
curl -4 -sS -o /tmp/openrouter-models.json -w 'HTTP %{http_code}\n' \
  https://openrouter.ai/api/v1/models
```

Продолжать только при `HTTP 200`.

**Шаг 3.** Предварительный rsync уже сделан (Фаза 7.3) — данные почти
синхронны.

**Шаг 4.** Остановить Hermes на Beget.

**[BEGET]**

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml stop
```

**Шаг 5.** Убедиться, что gateway-процесса на Beget не осталось.

**[BEGET]**

```bash
docker ps --filter name=hermes --format '{{.Names}} {{.Status}}'
# ожидается: пусто или "Exited"
```

**Шаг 6.** Финальный консистентный backup (контейнер уже остановлен).

**[BEGET]**

```bash
/srv/hermes/app/deploy/beget/backup.sh
```

**Шаг 6a.** Немедленно вывезти этот архив на Mac и погасить cron на Beget.

Это самый ценный артефакт миграции — единственный снимок, снятый при
остановленной записи в `state.db`. Пока он лежит только на Beget, он уязвим:
`backup.sh` держит `KEEP=7` и удаляет самые старые, а ночной cron на Beget (если
он обнаружен в Фазе 6.3) за неделю карантина сделает семь бэкапов уже
неактуальных данных и вытеснит именно этот. Тогда в момент, когда он понадобится
для отката, его не будет ни на одном сервере: off-site копия из Фазы 1.2 снята
**до** cutover и содержит более старое состояние.

**[BEGET]** — остановить ночной backup на время карантина:

```bash
crontab -l 2>/dev/null | grep -F -v 'deploy/beget/backup.sh' | crontab -
crontab -l 2>/dev/null || echo "(root crontab пуст)"
```

**[MAC]** — забрать финальный архив:

```bash
install -d -m 0700 "$MIG_STAGE/beget-final"
rsync -a --progress -e "ssh -i $OLD_KEY" \
  root@"$OLD_IP":/srv/hermes/backups/ "$MIG_STAGE/beget-final/"
```

**[MAC]** — убедиться, что свежий архив на месте и читается:

```bash
FINAL="$(ls -1t "$MIG_STAGE"/beget-final/hermes-*.tar.gz | head -n1)"
echo "FINAL=$FINAL"
tar -tzf "$FINAL" >/dev/null && echo "final_backup_readable_OK"
shasum -a 256 "$FINAL"
```

**Критерий:** `final_backup_readable_OK`, метка времени в имени соответствует
только что сделанному бэкапу (а не архиву из Фазы 1).

**Шаг 7.** Dry-run финального зеркалирующего rsync.

**[BEGET]**

```bash
rsync -aHAX --numeric-ids --delete-delay --itemize-changes --dry-run \
  -e "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new" \
  /srv/hermes/data/ root@NEW_IP_HERE:/srv/hermes/data/
```

**Шаг 8.** Финальный rsync Beget → Aeza.

**[BEGET]**

```bash
rsync -aHAX --numeric-ids --delete-delay \
  -e "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new" \
  /srv/hermes/data/ root@NEW_IP_HERE:/srv/hermes/data/
```

**Шаг 9.** Сравнить размеры и число ключевых файлов.

**[BEGET]**

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
find /srv/hermes/data/sessions -type f | wc -l
```

**[AEZA]**

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
find /srv/hermes/data/sessions -type f | wc -l
```

**Критерий:** **строгое равенство** всех трёх чисел на обоих серверах — байты,
общее число файлов, число файлов сессий. Никаких исключений в переносе больше
нет, поэтому расхождение — это реальная потеря данных, а не «шум логов».
Расхождение = не запускать Aeza, разбираться.

**Шаг 10.** Проверить владельцев и права на Aeza.

**[AEZA]**

```bash
stat -c '%U:%G %u:%g %a %n' /srv/hermes/data /srv/hermes/data/.env /srv/hermes/data/config.yaml
```

**Критерий:** `.env` → `600`, `config.yaml` → `640`, владелец числовой
`10000:10000` (или соответствующее имя пользователя контейнера).

**Шаг 11.** Только теперь запустить Aeza.

**[AEZA]**

```bash
cd /srv/hermes/app
export HERMES_GIT_SHA="$(git rev-parse HEAD)"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose -f deploy/beget/compose.yaml up -d
```

**Шаг 12.** Beget оставить **остановленным** (не удалять — Фаза 14).

---

## Фаза 9-bis — Финальный перенос через Mac (fallback без прямого канала)

Выполнять **вместо Шагов 7–8 Фазы 9**, если прямой SSH Beget → Aeza невозможен.

Это не гипотетическая ветка: если проверка канала в Фазе 7.1 не дала
`beget_to_aeza_OK`, прямого пути нет вообще, и без этого раздела runbook
упирается в тупик ровно в тот момент, когда Beget уже остановлен. Проверка в
7.1 стоит до cutover именно затем, чтобы выбор ветки был сделан заранее, а не в
аварийном режиме.

Шаги 1–6 и 9–12 Фазы 9 выполняются без изменений — заменяется только сам
перенос.

### 9-bis.1 Упаковать данные на остановленном Beget

**[BEGET]** — контейнер уже остановлен (Шаг 4), поэтому архив консистентен.
Без исключений:

```bash
rm -f /srv/hermes/data-final.tar.gz
tar --numeric-owner -C /srv/hermes/data -czf /srv/hermes/data-final.tar.gz .
chmod 600 /srv/hermes/data-final.tar.gz
tar -tzf /srv/hermes/data-final.tar.gz >/dev/null && echo "final_archive_readable_OK"
```

**[BEGET]** — зафиксировать эталонные числа для последующей сверки:

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
find /srv/hermes/data/sessions -type f | wc -l
```

### 9-bis.2 Перевезти архив через Mac

**[MAC]**

```bash
scp -i "$OLD_KEY" root@"$OLD_IP":/srv/hermes/data-final.tar.gz "$MIG_STAGE/"
```

**[MAC]**

```bash
scp -i "$NEW_KEY" "$MIG_STAGE/data-final.tar.gz" root@"$NEW_IP":/srv/hermes/
```

### 9-bis.3 Заменить данные на Aeza (со сносом каталога назначения)

⚠️ **Ключевое отличие от rsync.** `rsync --delete-delay` удаляет на приёмнике
файлы, которых больше нет на источнике. `tar -x` так **не умеет** — он только
добавляет и перезаписывает. Если распаковать поверх, на Aeza останутся файлы,
удалённые на Beget (устаревшие сессии, старые скиллы, отозванные креды), и
`/srv/hermes/data` не будет зеркалом. Поэтому каталог назначения сносится
явно.

Каталог не удаляем, а **отодвигаем** — это бесплатная страховка на случай
неудачной распаковки:

**[AEZA]**

```bash
mv /srv/hermes/data "/srv/hermes/data.pre-cutover-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 /srv/hermes/data
```

**[AEZA]** — распаковать с сохранением численного владельца:

```bash
tar --numeric-owner -xzf /srv/hermes/data-final.tar.gz -C /srv/hermes/data
```

**[AEZA]** — сверить с эталонными числами из 9-bis.1:

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
find /srv/hermes/data/sessions -type f | wc -l
```

**Критерий:** строгое равенство всех трёх чисел значениям с Beget. Только после
этого — Шаг 10 Фазы 9 (права) и Шаг 11 (запуск).

### 9-bis.4 Убрать за собой

Архивы содержат `.env` с боевыми секретами.

**[MAC]**

```bash
rm -f "$MIG_STAGE/data-final.tar.gz"
```

**[AEZA]** — удалить архив сразу после успешной сверки:

```bash
shred -u /srv/hermes/data-final.tar.gz 2>/dev/null || rm -f /srv/hermes/data-final.tar.gz
```

**[BEGET]** — удалить архив **только после** успешного запуска Aeza (до этого
он остаётся дополнительной страховкой):

```bash
shred -u /srv/hermes/data-final.tar.gz 2>/dev/null || rm -f /srv/hermes/data-final.tar.gz
```

> Каталог `/srv/hermes/data.pre-cutover-*` на Aeza удалить в конце карантина
> (Фаза 14), не раньше — он занимает место, но остаётся быстрым локальным
> откатом на случай, если что-то всплывёт в первые дни.

---

## Фаза 10 — Проверки после запуска Aeza

Все команды контейнера — **[AEZA]**.

### 10.1 Контейнер

```bash
docker compose -f /srv/hermes/app/deploy/beget/compose.yaml ps
```

```bash
docker inspect -f '{{.State.Status}} restart={{.RestartCount}}' hermes
```

**Критерий:** `running`, `restart=0` (нет restart-loop).

### 10.2 Логи — нет новых ошибок

```bash
docker logs --tail=200 hermes
```

**Критерий:** нет `Traceback`, HTTP `400/401/403`, `request_dump`, конфликта
polling, ошибок прав на `/opt/data`, пустого allowlist.

```bash
docker logs --since=10m hermes 2>&1 \
  | grep -Ei 'traceback|error|request_dump|conflict|\b40[013]\b' \
  || echo "no matching errors"
```

> Границы слова `\b` вокруг `40[013]` обязательны. Без них шаблон срабатывает
> на любое число, содержащее `400`/`401`/`403` внутри себя — `4013 bytes`,
> `session 1403`, `403912 tokens`, `tool_calls: 2403`, — и вывод забивается
> ложными срабатываниями, среди которых теряется реальная ошибка. С `\b`
> ловятся `HTTP/1.1 401`, `status_code=403`, `Error code: 400`, но не числовой
> шум.

### 10.3 Hermes health

```bash
docker exec hermes hermes --version
```

```bash
docker exec hermes hermes doctor
```

```bash
docker exec hermes hermes gateway status
```

### 10.4 Функциональные проверки (через диалог с ботом)

Отправить боту в Telegram сообщения, проверяющие ключевые интеграции, и
подтвердить корректный ответ + отсутствие ошибок в логах:

- **Telegram in/out** — простое сообщение, дождаться ответа.
- **OpenRouter (основная модель)** — обычный ответ модели без 4xx в логах.
- **Perplexity `web_search`** — задать вопрос, требующий свежего веб-поиска;
  убедиться, что сработал web_search, а не отказ.
- **GitHub MCP** — выполнить чтение из GitHub **не менее 3 раз подряд**
  (после недавнего фикса бесконечного reconnect) — все 3 должны отработать
  без обрыва соединения.
- **`delegate_task` (read-only)** — запустить делегирование с **двумя**
  субагентами в режиме только чтения; оба должны вернуть результат.
- **Cron scheduler** — проверить список задач планировщика и что он живой:

```bash
docker exec hermes hermes gateway status
docker logs hermes 2>&1 | grep -Ei 'cron|scheduler' | tail -20
```

После каждой проверки:

```bash
docker logs --since=5m hermes 2>&1 \
  | grep -Ei 'traceback|request_dump|\b40[013]\b' \
  || echo "clean"
```

### 10.5 Проверка восстановления

```bash
docker restart hermes
```

```bash
sleep 5 && docker inspect -f '{{.State.Status}} restart={{.RestartCount}}' hermes
```

Затем снова отправить боту сообщение — ответ должен прийти.

### 10.6 Reboot VPS

Выполнять `reboot` **только после** успешной первичной проверки (10.1–10.5) и
наличия backup:

**[AEZA]**

```bash
reboot
```

`reboot` **разрывает текущую SSH-сессию**, поэтому следующую команду в ней
выполнить уже нельзя — нужно дождаться возврата хоста и подключиться заново.

**[MAC]** — дождаться, пока SSH снова начнёт отвечать:

```bash
until ssh -i "$NEW_KEY" -o ConnectTimeout=5 -o BatchMode=yes \
      root@"$NEW_IP" 'true' 2>/dev/null; do
  printf '.'
  sleep 5
done
echo " aeza_is_back"
```

**[MAC]** — контейнер должен подняться сам (`restart: unless-stopped`):

```bash
ssh -i "$NEW_KEY" root@"$NEW_IP" \
  "docker inspect -f '{{.State.Status}} restart={{.RestartCount}}' hermes"
```

**Критерий:** `running`. Затем отправить боту сообщение и убедиться, что ответ
приходит после перезагрузки.

---

## Фаза 11 — Tailscale на Aeza

Aeza регистрируется как **новый** узел — состояние со старого сервера не
копируем.

**[AEZA]**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

**[AEZA]** — поднять узел (интерактивная авторизация по ссылке; узел назвать явно):

```bash
tailscale up --hostname=hermes-aeza
```

> **НЕ копировать** `/var/lib/tailscale` со старого сервера — это привело бы к
> дублю identity узла. Регистрируем свежий узел.

**[AEZA]** — проверить статус и SSH-доступ по ACL:

```bash
tailscale status
```

Проверить в админке Tailscale, что ACL и SSH-политики покрывают новый узел
`hermes-aeza`. Старый узел Beget из tailnet удалить **только после карантина**
(Фаза 14).

---

## Фаза 12 — Backup на Aeza (после миграции)

### 12.1 Ручной backup + проверка архива

**[AEZA]**

```bash
/srv/hermes/app/deploy/beget/backup.sh
```

**[AEZA]** — проверить валидность свежего архива:

```bash
LATEST="$(ls -1t /srv/hermes/backups/hermes-*.tar.gz | head -n1)"
tar -tzf "$LATEST" >/dev/null && echo "verify OK: $LATEST"
```

### 12.2 Ночной cron (без дублей и без удаления существующих заданий)

**[AEZA]** — безопасное добавление строки (удаляет прежнюю такую же, если
была, сохраняет остальные):

```bash
( crontab -l 2>/dev/null | grep -F -v 'deploy/beget/backup.sh'; \
  echo '15 3 * * * /srv/hermes/app/deploy/beget/backup.sh >> /var/log/hermes-backup.log 2>&1' ) \
  | crontab -
```

**[AEZA]** — проверить результат:

```bash
crontab -l
```

Целевая строка:

```cron
15 3 * * * /srv/hermes/app/deploy/beget/backup.sh >> /var/log/hermes-backup.log 2>&1
```

### 12.3 Дополнительно

- Включить **snapshot Aeza** в панели провайдера (дополнение, не замена).
- Держать минимум **одну зашифрованную off-site копию** вне Aeza (например на
  Mac, `gpg -c`).

---

## Фаза 13 — Обновление приложения (отдельно, после стабилизации)

Только когда Aeza стабильно отработала (сутки+). Безопасный скрипт делает
backup → pull → build → verify → авто-rollback без удаления данных:

**[AEZA]**

```bash
cd /srv/hermes/app
deploy/beget/deploy.sh
```

---

## Фаза 14 — Карантин и вывод Beget из эксплуатации

1. Держать Beget-контейнер **остановленным** 3–7 дней (не удалять VPS).
2. Ночной `backup.sh` на Beget уже отключён в Шаге 6a Фазы 9 — проверить, что
   он действительно не работает (`crontab -l` на Beget), иначе ротация вытеснит
   финальный консистентный архив.
3. В течение карантина подтвердить на Aeza работу: Telegram, cron, GitHub MCP,
   Perplexity, OpenRouter, Tailscale.
4. Убедиться в наличии **проверенного off-site backup** (зашифрован) — как
   минимум финального архива, вывезенного на Mac в Шаге 6a.
5. Удалить временные migration-ключи (см. ниже).
6. Удалить узел Beget из Tailscale.
7. Убрать промежуточные каталоги и архивы, оставшиеся от fallback-переноса,
   если он применялся:

   **[AEZA]**

   ```bash
   ls -d /srv/hermes/data.pre-cutover-* 2>/dev/null
   rm -rf /srv/hermes/data.pre-cutover-*
   ```

   **[MAC]** — стереть промежуточные копии с секретами (архивы содержат `.env`):

   ```bash
   ls -la "$MIG_STAGE"
   rm -f "$MIG_STAGE"/data-final.tar.gz "$MIG_STAGE"/data-rollback.tar.gz \
         "$MIG_STAGE"/data-migration.tar.gz
   ```

   > `$MIG_STAGE/beget-final/` и `$MIG_STAGE/beget-backups/` — это и есть
   > off-site копии, их **не удалять**. Для длительного хранения зашифровать
   > (`gpg -c`).

8. Затем удалить VPS Beget в панели и отключить оплату.
9. **Ротацию API-ключей/токенов** рекомендовать, **если есть подозрение на
   компрометацию** старого сервера. Не выполнять автоматически.

### 14.1 Удалить временный migration-ключ с обоих серверов

Ключ был установлен симметрично (Фаза 7.1), поэтому вычищать надо тоже с обеих
сторон: и приватную часть, и строку в `authorized_keys`.

**[BEGET]**

```bash
shred -u /root/.ssh/migration_key 2>/dev/null || rm -f /root/.ssh/migration_key
sed -i '/hermes-migration-temp/d' ~/.ssh/authorized_keys
```

**[AEZA]**

```bash
shred -u /root/.ssh/migration_key 2>/dev/null || rm -f /root/.ssh/migration_key
sed -i '/hermes-migration-temp/d' ~/.ssh/authorized_keys
```

Проверить, что не осталось ни одной ссылки. Один и тот же read-only блок
выполняется на **[BEGET]**, затем на **[AEZA]** — по очереди, в своей сессии:

```bash
grep -c 'hermes-migration-temp' ~/.ssh/authorized_keys || echo "0 — чисто"
ls -l /root/.ssh/migration_key 2>/dev/null || echo "приватный ключ удалён"
```

> Выполнять **только** после карантина (пункт 4 выше). Пока карантин идёт, ключ
> нужен: это единственный канал отката на Beget.

**[MAC]** — удалить локальные копии:

```bash
rm -f "$MIG_STAGE/migration_key" "$MIG_STAGE/migration_key.pub"
```

---

## Rollback

**Никогда не запускать Beget, пока Aeza не остановлена** (иначе два gateway).

### Вариант A — Aeza ещё не приняла сообщений и не меняла данные

**[AEZA]**

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml down
```

**[BEGET]**

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml up -d
```

Бот снова на Beget. Данные Beget не тронуты.

### Вариант B — Aeza уже работала и записала новые данные

Порядок: остановить Aeza → backup Aeza → синхронизировать данные Aeza обратно
на Beget → проверить → только затем запустить Beget.

**[AEZA]** — остановить:

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml stop
```

**[AEZA]** — backup текущего состояния Aeza:

```bash
/srv/hermes/app/deploy/beget/backup.sh
```

**[AEZA]** — обратная синхронизация Aeza → Beget. Канал в эту сторону подготовлен
и проверен заранее (Фаза 7.1): приватный ключ лежит на Aeza, публичный прописан
в `authorized_keys` Beget с `from="$NEW_IP"`. Если карантин уже завершён и ключ
удалён (Фаза 14.1) — восстановить его по процедуре Фазы 7.1 перед откатом.
Сначала dry-run:

```bash
rsync -aHAX --numeric-ids --delete-delay --itemize-changes --dry-run \
  -e "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new" \
  /srv/hermes/data/ root@OLD_IP_HERE:/srv/hermes/data/
```

**[AEZA]** — реальная обратная синхронизация:

```bash
rsync -aHAX --numeric-ids --delete-delay \
  -e "ssh -i /root/.ssh/migration_key -o StrictHostKeyChecking=accept-new" \
  /srv/hermes/data/ root@OLD_IP_HERE:/srv/hermes/data/
```

**[BEGET]** — проверить данные и права после обратного переноса:

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
stat -c '%u:%g %a %n' /srv/hermes/data/.env /srv/hermes/data/config.yaml
```

**[BEGET]** — только теперь запустить Beget:

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml up -d
```

> Для обратной синхронизации нужен рабочий двусторонний канал. Он создаётся и
> **проверяется** в Фазе 7.1 (обе команды печатают `beget_to_aeza_OK` и
> `aeza_to_beget_OK`) — до cutover, пока оба сервера заведомо живы.
> Непроверенный канал отката считается несуществующим. Если прямого канала нет
> — откат выполняется по Варианту B-bis ниже.

### Вариант B-bis — откат через Mac (нет прямого канала Aeza → Beget)

Тот же Вариант B, но перенос идёт через Mac. Логика та же, что в Фазе 9-bis, в
обратную сторону. Beget всё это время остаётся **остановленным**.

**[AEZA]** — остановить Aeza и сделать backup (шаги Варианта B без изменений):

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml stop
/srv/hermes/app/deploy/beget/backup.sh
```

**[AEZA]** — упаковать текущее состояние целиком:

```bash
rm -f /srv/hermes/data-rollback.tar.gz
tar --numeric-owner -C /srv/hermes/data -czf /srv/hermes/data-rollback.tar.gz .
chmod 600 /srv/hermes/data-rollback.tar.gz
tar -tzf /srv/hermes/data-rollback.tar.gz >/dev/null && echo "rollback_archive_readable_OK"
```

**[AEZA]** — эталонные числа для сверки:

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
find /srv/hermes/data/sessions -type f | wc -l
```

**[MAC]** — перевезти:

```bash
scp -i "$NEW_KEY" root@"$NEW_IP":/srv/hermes/data-rollback.tar.gz "$MIG_STAGE/"
scp -i "$OLD_KEY" "$MIG_STAGE/data-rollback.tar.gz" root@"$OLD_IP":/srv/hermes/
```

**[BEGET]** — отодвинуть текущие данные и распаковать на чистое место. Снос
каталога обязателен по той же причине, что и в 9-bis.3: `tar -x` не удаляет
файлы, которых больше нет на источнике.

```bash
mv /srv/hermes/data "/srv/hermes/data.pre-rollback-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 /srv/hermes/data
tar --numeric-owner -xzf /srv/hermes/data-rollback.tar.gz -C /srv/hermes/data
```

**[BEGET]** — сверить с эталоном Aeza и проверить права:

```bash
du -sb /srv/hermes/data
find /srv/hermes/data -type f | wc -l
find /srv/hermes/data/sessions -type f | wc -l
stat -c '%u:%g %a %n' /srv/hermes/data/.env /srv/hermes/data/config.yaml
```

**Критерий:** три числа совпадают с эталоном Aeza, `.env` → `600`,
`config.yaml` → `640`. Только после этого:

**[BEGET]**

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml up -d
```

Удалить архивы с секретами после успешного отката — **на каждой машине
отдельно**. Блоки намеренно разделены: команды для разных хостов в одном блоке
провоцируют скопировать его целиком не туда.

**[MAC]**

```bash
rm -f "$MIG_STAGE/data-rollback.tar.gz"
```

**[BEGET]**

```bash
shred -u /srv/hermes/data-rollback.tar.gz 2>/dev/null || rm -f /srv/hermes/data-rollback.tar.gz
```

**[AEZA]**

```bash
shred -u /srv/hermes/data-rollback.tar.gz 2>/dev/null || rm -f /srv/hermes/data-rollback.tar.gz
```

---

## Acceptance criteria (самопроверка документа)

- [x] Все fenced shell-блоки синтаксически цельные, без обрывов строк.
- [x] Все переменные (`OLD_IP`, `NEW_IP`, `OLD_KEY`, `NEW_KEY`, `MIG_STAGE`,
      `OLD_SHA`, `ARCHIVE`) определены до использования.
- [x] Пути rsync источник/назначение имеют корректный trailing slash
      (`/srv/hermes/data/` → `/srv/hermes/data/`).
- [x] Нет фиктивных путей вида `/~/...`.
- [x] Нет одновременно запущенных gateway: cutover сначала останавливает Beget,
      rollback сначала останавливает Aeza.
- [x] Rollback (Вариант B) учитывает новые данные Aeza и переносит их обратно
      до запуска Beget.
- [x] Канал отката (Aeza → Beget) создан и проверен ДО cutover, а не в момент
      аварии: приватный ключ на обоих серверах, публичный в `authorized_keys`
      обоих с соответствующим `from=` (Фаза 7.1).
- [x] Mac-команды используют только флаги, поддерживаемые openrsync
      (`--progress`, не `--info=progress2`); GNU-флаги остались только в
      серверных rsync.
- [x] Переносы данных идут **без исключений** — ни `/logs/`, ни `bin`, ни в
      rsync, ни в tar. `/srv/hermes/data` — точное зеркало, поэтому сверка в
      Шаге 9 Фазы 9 является строгим равенством, а не оценкой на глаз.
- [x] Чистота рабочего дерева проверяется на ОБОИХ серверах: на Beget до снятия
      SHA (6.1), на Aeza после checkout (6.2).
- [x] Состояние уровня ОС, которое не переносится через rsync (crontab, TZ),
      инвентаризовано на Beget до cutover (6.3).
- [x] Отключение парольного SSH подтверждено эффективным значением `sshd -T` по
      трём параметрам сразу, а не только правкой файла (4.5).
- [x] Есть полный fallback без прямого канала между серверами: предварительный
      перенос (7.4), финальный cutover (Фаза 9-bis) и откат (Вариант B-bis).
      Во всех tar-ветках каталог назначения сносится перед распаковкой, иначе
      удалённые на источнике файлы остались бы на приёмнике.
- [x] Финальный консистентный backup вывозится на Mac сразу после создания, а
      ночной cron на Beget гасится, чтобы ротация `KEEP=7` его не вытеснила
      (Шаг 6a Фазы 9).
- [x] Маркеры проверок не содержат символов `>` и `-`: после двух вложенных SSH
      `>` срабатывает как перенаправление, и проверка молча «проходит», ничего
      не проверив.
- [x] Нет команд с угловыми скобками-плейсхолдерами (`<значение>`) внутри
      `bash`-блоков — подстановка только через переменные.
- [x] Команды после `reboot` выполняются с Mac в новой сессии, а не в
      разорванной (10.6).
- [x] Временные migration-ключи ограничены `restrict,from="IP"` и удаляются с
      обеих сторон после карантина (14.1).
- [x] Секреты (`.env`, токены, ключи) нигде не печатаются; промежуточные
      tar-архивы с `.env` удаляются на всех трёх машинах после переноса.
- [x] Каждая команда помечена `[MAC]`, `[BEGET]` или `[AEZA]`.
- [x] Документ выполняется сверху вниз; значения, которые нельзя знать заранее
      (`NEW_IP`, `OLD_SHA`, `NEW_IP_HERE`/`OLD_IP_HERE` внутри серверных
      сессий), явно помечены как подстановка.
- [x] Изменён только `MIGRATION_BEGET_TO_AEZA.md`.

> Замечание по подстановкам: внутри SSH-сессий **[BEGET]**/**[AEZA]** локальные
> Mac-переменные недоступны, поэтому в rsync-строках, инициируемых с сервера,
> IP задан плейсхолдером `NEW_IP_HERE`/`OLD_IP_HERE` — подставить вручную
> перед запуском. Это не «догадка», а явно обозначенная точка подстановки.
