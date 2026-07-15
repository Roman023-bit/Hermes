# Задание для Claude Code: развернуть Hermes на Beget Cloud

Ниже находится готовое техническое задание. Выполняй его поэтапно, показывай
результат проверок после каждого этапа и останавливайся только там, где
необходимы данные или действие владельца аккаунта Beget.

## Цель

Развернуть Hermes Agent из репозитория
`https://github.com/Roman023-bit/Hermes.git` на постоянно работающем VPS в
Beget Cloud, чтобы gateway, боты, cron, память, навыки и сессии продолжали
работать независимо от MacBook.

Страница проекта Beget:

`https://cp.beget.com/cloud/projects/4a0a8b84-d225-4244-bfde-b4497a02182d`

Локальный репозиторий:

`/Users/romanmizanov/Documents/Hermes`

Локальные пользовательские данные Hermes:

- `/Users/romanmizanov/.hermes/config.yaml`
- `/Users/romanmizanov/.hermes/.env`

Оба файла уже существуют. Никогда не печатай содержимое `.env`, токены,
ключи API, пароли или полные значения секретных полей из `config.yaml`.

## Важные факты о проекте

1. В репозитории уже есть production-ready `Dockerfile`. Не создавай новый
   Dockerfile без доказанной необходимости.
2. Образ уже включает Python, Node.js, npm, Playwright Chromium, ffmpeg,
   ripgrep, Git, Docker CLI и s6-overlay.
3. Контейнер хранит всё изменяемое состояние в `/opt/data`, которое должно
   быть постоянным bind mount с VPS.
4. Контейнер должен запускаться с оригинальным entrypoint `/init`. Никогда не
   переопределяй `entrypoint`: это отключит миграции, исправление прав и
   s6-supervision.
5. Для production нужен один контейнер Hermes с командой `gateway run`.
   Gateway и dashboard внутри этого контейнера контролируются s6.
6. Нельзя запускать два gateway с одинаковыми bot token одновременно. Перед
   переключением на VPS локальный gateway на Mac должен быть остановлен.

## Неприкосновенные требования безопасности

- Не добавляй `.env`, `config.yaml`, `auth.json`, сессии, память или бэкапы в
  Git и Docker build context.
- Не копируй секреты через GitHub Issues, Actions logs, командную строку
  Compose или публичные paste-сервисы.
- Не используй `docker compose down -v`: эта команда может уничтожить данные.
- Не публикуй порты 8642 и 9119 на публичный интерфейс по умолчанию.
- Не используй `HERMES_DASHBOARD_INSECURE=1` на VPS.
- Не подключай `/var/run/docker.sock`, если владелец явно не подтвердил, что
  Hermes должен управлять Docker-хостом. Такой mount практически равен root.
- Не включай `GATEWAY_ALLOW_ALL_USERS`. Проверь allowlist выбранной платформы.
- Не отключай firewall, SSH-защиту или проверку TLS.
- Не выводи секреты даже в замаскированном виде. Можно сообщать только имя
  переменной и факт `set`/`missing`.

## Предлагаемая архитектура

```text
MacBook                         Beget Cloud VPS
~/.hermes/                     /srv/hermes/data/
  config.yaml  -- rsync/SSH -->  config.yaml
  .env                           .env
  sessions/                      sessions/
  memories/                      memories/
  skills/                        skills/
                                |
GitHub repository ------------> /srv/hermes/app/
                                |
                                v
                         Docker container: hermes
                         /opt/data -> /srv/hermes/data
                         restart: unless-stopped
                         command: gateway run
```

Начальный безопасный режим:

- Telegram/Discord/Slack работают через исходящие подключения и обычно не
  требуют открытых входящих портов.
- Dashboard и API доступны только через `127.0.0.1` на VPS и SSH tunnel.
- Публичный домен, reverse proxy и HTTPS добавляются отдельным этапом после
  успешной проверки gateway.

## Этап 0. Preflight на Mac

Работай из `/Users/romanmizanov/Documents/Hermes`.

1. Прочитай полностью `AGENTS.md`.
2. Проверь:

   ```bash
   git status --short --branch
   git remote -v
   git rev-parse HEAD
   test -f "$HOME/.hermes/config.yaml"
   test -f "$HOME/.hermes/.env"
   stat -f '%Sp %N' "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env"
   ```

3. Не показывай содержимое `.env`. Проверь только имена определённых
   переменных и наличие пустых обязательных значений безопасным парсером.
4. Проверь, что секреты не отслеживаются Git:

   ```bash
   git ls-files | grep -E '(^|/)(\.env|config\.yaml|auth\.json)$' || true
   ```

5. Найди абсолютные macOS-пути в `config.yaml`, но в отчёте показывай только
   YAML-ключ и тип проблемы, не секретные значения. В серверной копии замени:

   - `/Users/romanmizanov/...` на подходящий контейнерный путь;
   - `terminal.cwd` на `/opt/data/workspace`, если старый путь указывает на Mac;
   - ссылки на локальные приложения, сокеты и localhost-сервисы Mac отключи
     или перенастрой;
   - timezone оставь соответствующим требованиям пользователя.

6. Если SSH-ключ для Beget отсутствует, предложи создать отдельный ключ:

   ```bash
   ssh-keygen -t ed25519 -a 64 -f "$HOME/.ssh/beget_hermes" -C "beget-hermes"
   ```

   Не перезаписывай существующий ключ. Публичный ключ можно показать
   владельцу, приватный ключ нельзя выводить или передавать.

## Этап 1. Создание VPS в Beget

Этот этап может потребовать ручного действия владельца в панели Beget.
Не пытайся обходить вход, 2FA или CAPTCHA.

Попроси владельца создать в указанном проекте VPS со следующими параметрами:

- готовое решение Beget **Docker**;
- Ubuntu 24.04;
- минимум 2 vCPU;
- рекомендуемо 4 GB RAM;
- минимум 30–40 GB NVMe для исходников, build cache и образов;
- публичный IPv4;
- SSH-аутентификация отдельным публичным ключом;
- имя, например `hermes-prod`.

Если планируется только запуск готового образа, 2 GB RAM обычно достаточно.
Если образ будет собираться на самом VPS, предпочесть 4 GB RAM и при
необходимости добавить swap. Не уменьшать диск после создания.

Для продолжения попроси только:

- IP или hostname VPS;
- SSH user (сначала обычно `root`);
- подтверждение, что Docker и Docker Compose установились.

Никогда не проси присылать пароль от Beget в чат или сохранять его в файле.

## Этап 2. Первичная проверка VPS

Подключаться по SSH, а не через браузерную консоль:

```bash
ssh -i "$HOME/.ssh/beget_hermes" root@BEGET_IP
```

На сервере выполнить:

```bash
set -euo pipefail
uname -a
cat /etc/os-release
docker version
docker compose version
systemctl is-enabled docker
systemctl is-active docker
df -h /
free -h
```

Ожидается активный Docker. Не продолжать, если свободного диска меньше 15 GB.

Создать каталоги:

```bash
install -d -m 0755 /srv/hermes/app
install -d -m 0700 /srv/hermes/data
install -d -m 0700 /srv/hermes/backups
```

Настроить firewall. На первом этапе открыть только SSH:

```bash
ufw allow OpenSSH
ufw --force enable
ufw status verbose
```

Не отключать текущую SSH-сессию до проверки нового подключения во второй
сессии. Публичные 8642/9119 не открывать.

## Этап 3. Подготовка deployment-файлов в репозитории

Создай каталог `deploy/beget/` со следующими файлами:

- `compose.yaml`;
- `.env.example` только с несекретными Compose-параметрами;
- `README.md` с командами эксплуатации;
- `deploy.sh` для безопасного обновления;
- `backup.sh` для резервного копирования persistent data.

Добавь `deploy/beget/.env` в `.gitignore`. Не помещай пользовательский
`~/.hermes/.env` рядом с Compose-файлом.

Базовый `compose.yaml` должен соответствовать этому контракту:

```yaml
services:
  hermes:
    build:
      context: ../..
      dockerfile: Dockerfile
      args:
        HERMES_GIT_SHA: ${HERMES_GIT_SHA:-unknown}
    image: roman023-hermes:${HERMES_IMAGE_TAG:-local}
    container_name: hermes
    restart: unless-stopped
    command: ["gateway", "run"]
    volumes:
      - /srv/hermes/data:/opt/data
    environment:
      HERMES_UID: "${HERMES_UID:-10000}"
      HERMES_GID: "${HERMES_GID:-10000}"
      HERMES_DASHBOARD: "${HERMES_DASHBOARD:-0}"
    ports:
      - "127.0.0.1:8642:8642"
      - "127.0.0.1:9119:9119"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    stop_grace_period: 45s
```

Не добавляй `entrypoint`, `privileged`, `network_mode: host` или Docker socket.
Не используй Compose `env_file` для `/srv/hermes/data/.env`: Hermes сам читает
его из `/opt/data/.env`, а bind mount является единственным источником данных.

Если API server не нужен, порт 8642 можно вообще убрать после проверки.
Публикация на `127.0.0.1` не делает его доступным из интернета.

Перед commit выполнить:

```bash
docker compose -f deploy/beget/compose.yaml config
git diff --check
git status --short
```

Если Docker на Mac отсутствует, синтаксическую проверку Compose выполнить на
VPS до первого запуска.

## Этап 4. Получение исходников на VPS

На VPS:

```bash
rm -rf /srv/hermes/app.empty 2>/dev/null || true
git clone --filter=blob:none --single-branch --branch main \
  https://github.com/Roman023-bit/Hermes.git /srv/hermes/app
cd /srv/hermes/app
git status --short --branch
git rev-parse HEAD
```

Не использовать `git reset --hard` при наличии неизвестных локальных
изменений. Обновления должны выполняться через `git pull --ff-only`.

## Этап 5. Резервная копия и перенос данных

Сначала на Mac создать локальный архив без установленных runtime-компонентов:

```bash
mkdir -p "$HOME/hermes-backups"
tar \
  --exclude='./bin' \
  --exclude='./node' \
  --exclude='./logs' \
  --exclude='./.update_check' \
  -C "$HOME/.hermes" \
  -czf "$HOME/hermes-backups/hermes-before-beget-$(date +%Y%m%d-%H%M%S).tar.gz" \
  .
```

Передавать persistent data только по SSH. `rsync` должен включать скрытые
файлы `.env`, но исключать локальные Python/Node runtime:

```bash
rsync -a --info=progress2 \
  --exclude='/bin/' \
  --exclude='/node/' \
  --exclude='/logs/' \
  --exclude='/.update_check' \
  -e "ssh -i $HOME/.ssh/beget_hermes" \
  "$HOME/.hermes/" root@BEGET_IP:/srv/hermes/data/
```

Не использовать `--delete` при первом переносе.

На VPS проверить без чтения секретов:

```bash
test -s /srv/hermes/data/config.yaml
test -s /srv/hermes/data/.env
chmod 600 /srv/hermes/data/.env
chmod 640 /srv/hermes/data/config.yaml
install -d -m 0750 /srv/hermes/data/workspace
```

Проверь YAML безопасным парсером и сообщи только `valid`/`invalid` и имена
верхнеуровневых ключей. Не печатай значения.

## Этап 6. Проверка конфигурации перед запуском

Проверь серверную копию `config.yaml` на:

- отсутствие путей `/Users/romanmizanov/...`;
- `terminal.cwd: /opt/data/workspace`;
- настроенный провайдер модели;
- наличие allowlist для используемого gateway;
- корректную timezone;
- отсутствие привязки к локальному Ollama/LM Studio на Mac;
- отсутствие настроек, которые требуют GUI macOS;
- включение только реально используемых платформ.

Проверь только наличие обязательных переменных в `.env`, не значения:

- ключ выбранного model provider или сохранённый Portal auth;
- token выбранной messaging-платформы;
- allowlist/owner ID, если он задаётся через env;
- API/dashboard credentials только если эти сервисы включаются.

Если dashboard нужен, не включай его без auth provider. Для первого запуска
оставь `HERMES_DASHBOARD=0` и используй gateway через Telegram/Discord/etc.

Если нужен API server, добавь в `/srv/hermes/data/.env`:

```dotenv
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_KEY=<случайный секрет минимум 32 байта>
```

Секрет генерировать на VPS через `openssl rand -hex 32`, но не выводить его в
отчёт. Порт на host всё равно оставлять привязанным к `127.0.0.1`.

## Этап 7. Сборка и первый запуск

На VPS:

```bash
cd /srv/hermes/app
export HERMES_GIT_SHA="$(git rev-parse HEAD)"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose -f deploy/beget/compose.yaml config
docker compose -f deploy/beget/compose.yaml build --pull
docker compose -f deploy/beget/compose.yaml up -d
```

Сборка может занять несколько минут и потребовать несколько гигабайт диска.
Не прерывать её только из-за отсутствия вывода; проверять процесс и свободное
место. Если сборка падает по OOM, увеличить RAM или временно добавить swap,
а не менять зависимости проекта случайным образом.

## Этап 8. Проверка контейнера

Выполнить:

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml ps
docker inspect -f '{{.State.Status}} restart={{.RestartCount}}' hermes
docker logs --tail=200 hermes
docker exec hermes hermes --version
docker exec hermes hermes doctor
docker exec hermes hermes gateway status
```

В логах не должно быть traceback, бесконечного restart loop, ошибок прав на
`/opt/data`, конфликтов bot polling или сообщений о пустом allowlist.

Если API включён:

```bash
curl --fail --silent http://127.0.0.1:8642/health
```

Ожидается JSON со статусом `ok`. Не отправлять API key в shell history без
необходимости.

После этого владелец должен отправить тестовое сообщение боту. Проверить, что:

1. сообщение дошло;
2. Hermes ответил;
3. новая сессия появилась в `/srv/hermes/data/sessions`;
4. после `docker restart hermes` бот снова отвечает;
5. после перезагрузки VPS контейнер автоматически запустился.

## Этап 9. Переключение с Mac на VPS

Перед production-запуском остановить локальный gateway на Mac:

```bash
cd /Users/romanmizanov/Documents/Hermes
source .venv/bin/activate
hermes gateway stop || true
```

Проверить, что на Mac не осталось процесса, использующего тот же bot token.
Только после этого выполнить на VPS:

```bash
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml up -d
```

Если Telegram сообщает conflict `getUpdates`, найти второй активный экземпляр,
а не регенерировать token без необходимости.

## Этап 10. Безопасный доступ к dashboard

На первом этапе не публиковать dashboard в интернет. После настройки auth
включить его через несекретный файл `/srv/hermes/app/deploy/beget/.env`:

```dotenv
HERMES_DASHBOARD=1
```

Файл должен быть `chmod 600` и исключён из Git. Перезапустить контейнер:

```bash
docker compose -f deploy/beget/compose.yaml up -d
```

С Mac открыть SSH tunnel:

```bash
ssh -i "$HOME/.ssh/beget_hermes" \
  -L 9119:127.0.0.1:9119 \
  root@BEGET_IP
```

Dashboard будет доступен на `http://127.0.0.1:9119` только пока работает
tunnel. Для публичного домена требуется отдельный этап: DNS, Caddy/Nginx,
HTTPS и OAuth/OIDC. Не публиковать dashboard с basic auth прямо в интернет.

## Этап 11. Backup

`backup.sh` должен:

1. использовать `set -euo pipefail`;
2. создавать timestamped архив `/srv/hermes/backups`;
3. включать `/srv/hermes/data`;
4. не останавливать контейнер надолго;
5. хранить ограниченное число локальных копий;
6. никогда не удалять единственный успешный backup;
7. проверять архив через `tar -tzf`;
8. выставлять права `600`.

Минимальная ручная команда:

```bash
tar -C /srv/hermes/data \
  -czf "/srv/hermes/backups/hermes-$(date +%Y%m%d-%H%M%S).tar.gz" .
chmod 600 /srv/hermes/backups/hermes-*.tar.gz
```

Секреты находятся внутри backup, поэтому off-site копии должны быть
зашифрованы. Дополнительно предложить владельцу включить snapshots VPS в
Beget, но не считать snapshot единственной резервной копией.

## Этап 12. Обновление и откат

`deploy.sh` должен выполнять безопасную последовательность:

1. проверить чистоту Git checkout;
2. создать backup данных;
3. запомнить текущий commit и image tag;
4. `git fetch origin main`;
5. `git pull --ff-only origin main`;
6. собрать новый образ с `HERMES_GIT_SHA`;
7. выполнить `docker compose up -d`;
8. проверить container status, logs и gateway;
9. при провале вернуть предыдущий image/commit без удаления data volume.

Никогда не выполнять автоматический `git reset --hard`, `docker system prune
-a` или `docker compose down -v`.

Обновление вручную:

```bash
cd /srv/hermes/app
git status --short
git pull --ff-only origin main
export HERMES_GIT_SHA="$(git rev-parse HEAD)"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose -f deploy/beget/compose.yaml build --pull
docker compose -f deploy/beget/compose.yaml up -d
docker exec hermes hermes --version
docker exec hermes hermes gateway status
```

## Этап 13. Финальный отчёт

В финале сообщи владельцу:

- IP/hostname VPS;
- commit SHA и image tag;
- статус контейнера и restart policy;
- какие gateway-платформы включены, без токенов;
- результат `hermes doctor` и `gateway status`;
- какие порты слушаются и на каких интерфейсах;
- путь persistent data и backup;
- команду просмотра логов;
- команду обновления;
- способ SSH tunnel для dashboard;
- оставшиеся риски или ручные действия.

Не объявляй задачу завершённой, пока не выполнены тестовое сообщение боту и
проверка восстановления после `docker restart hermes`.

## Операционные команды для владельца

```bash
# Статус
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml ps
docker exec hermes hermes gateway status

# Логи
docker logs --tail=200 -f hermes

# Перезапуск
docker restart hermes

# Остановка/запуск
docker compose -f deploy/beget/compose.yaml stop
docker compose -f deploy/beget/compose.yaml up -d

# Hermes CLI внутри контейнера
docker exec -it hermes hermes

# Проверка версии
docker exec hermes hermes --version
```

## Официальные справочные материалы

- Beget Docker marketplace: `https://beget.com/ru/cloud/marketplace/docker`
- Beget VPS manual: `https://beget.com/ru/kb/manual/virtual-servers`
- Hermes Docker guide: `website/docs/user-guide/docker.md`
- Hermes security guide: `website/docs/user-guide/security.md`
- Hermes API server: `website/docs/user-guide/features/api-server.md`
- Hermes dashboard: `website/docs/user-guide/features/web-dashboard.md`

