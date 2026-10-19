# Полный запуск IT Lead Finder на сервере

Инструкция рассчитана на чистый VPS с Ubuntu 24.04 LTS. Бот работает через исходящие соединения и не требует домена, HTTPS-сертификата или открытого веб-порта.

## 1. Выбрать сервер

Рекомендуемая минимальная конфигурация:

- Ubuntu 24.04 LTS, 64-bit;
- 1–2 vCPU;
- 2 ГБ RAM;
- 20 ГБ SSD;
- постоянный публичный IP;
- регион Европы;
- разрешённые исходящие подключения TCP 443 и 5432 и DNS.

Во входящем направлении нужен только SSH, обычно порт 22. Один экземпляр бота должен работать только на одном сервере.

## 2. Подключиться по SSH

На Mac заменить `SERVER_IP` на IP сервера:

```sh
ssh root@SERVER_IP
```

Если хостинг выдал другого пользователя, например `ubuntu`, использовать его и добавлять `sudo` к административным командам.

## 3. Обновить Ubuntu и включить firewall

На сервере:

```sh
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl ufw
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

Не открывать порты 80, 443 или 5432 во входящем направлении: они боту не нужны.

## 4. Установить Docker Engine и Compose

На сервере:

```sh
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```sh
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

```sh
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker.service containerd.service
sudo docker run --rm hello-world
sudo docker compose version
```

Последние две команды должны завершиться без ошибки.

## 5. Скачать проект из GitHub

На сервере:

```sh
sudo apt install -y git
cd /opt
sudo git clone https://github.com/quinxixl/leadgen.git leadfinder
sudo mkdir -p /opt/leadfinder/leadgen/data
```

## 6. Проверить локальные секреты

На Mac, в каталоге проекта:

```sh
cd "/путь/к/проекту/leadgen"
grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_API_ID|TELEGRAM_API_HASH|TELEGRAM_PHONE|DATABASE_URL)=' .env | sed 's/=.*/=<задано>/'
```

Должны быть показаны шесть строк. В `DATABASE_URL` должна использоваться строка **Session pooler**, порт `5432`, с окончанием `?sslmode=require`.

Проверить наличие Telegram-сессии:

```sh
test -s leadgen/data/telegram_reader.session && echo "Telegram-сессия найдена"
```

Перед копированием остановить локального бота сочетанием `Ctrl+C`. Это нужно, чтобы файл Telegram-сессии был закрыт корректно и чтобы два процесса не начали одновременно получать обновления одного бота.

## 7. Передать файлы на сервер

На Mac заменить `SERVER_IP`:

```sh
scp .env root@SERVER_IP:/opt/leadfinder/.env
scp leadgen/data/telegram_reader.session root@SERVER_IP:/opt/leadfinder/leadgen/data/telegram_reader.session
```

Если используется пользователь `ubuntu`, заменить `root` на `ubuntu`, а каталог `/opt/leadfinder` создать через `sudo`.

## 8. Защитить секреты

На сервере:

```sh
cd /opt/leadfinder
sudo chmod 600 .env
sudo chown -R 10001:10001 leadgen/data
sudo chmod 700 leadgen/data
sudo chmod 600 leadgen/data/telegram_reader.session
```

Проверить наличие файлов без вывода секретов:

```sh
test -s .env && echo ".env найден"
test -s leadgen/data/telegram_reader.session && echo "Telegram-сессия найдена"
test -s compose.yaml && echo "compose.yaml найден"
```

## 9. Собрать контейнер

На сервере из `/opt/leadfinder`:

```sh
sudo docker compose build --pull
```

Сборка установит Python 3.12, Telethon, PostgreSQL-драйвер и остальные зависимости. Ошибок в конце быть не должно.

## 10. Проверить Supabase до запуска бота

```sh
sudo docker compose run --rm --no-deps leadfinder python -m leadgen.app status
```

В результате должны быть видны примерно такие контрольные значения:

- `telegram_enabled`: `8015`;
- `active`: `9016`;
- лиды: `3611 rejected`, `6 review`, `21 sent`.

Если появляется `Password authentication failed`, проверить пароль и URL-кодирование специальных символов в `DATABASE_URL`. Если появляется `Tenant or user not found`, заново скопировать Session pooler URI из Supabase Connect.

## 11. Запустить бота

```sh
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail 100 -f
```

В журнале должны появиться строки:

```text
Бот работает. Откройте Telegram и отправьте /start. Ctrl+C — выход.
Мониторинг Telegram запущен в реальном времени без чтения истории.
```

Выйти из просмотра журнала можно через `Ctrl+C`: контейнер продолжит работать в фоне. Через одну-две минуты `sudo docker compose ps` должен показывать состояние `healthy`.

## 12. Проверить Telegram

1. Открыть бота и отправить `/start`.
2. Для нового Telegram-пользователя нажать «Зарегистрироваться».
3. Настроить услуги, бюджет, запросы без бюджета и возможные потребности.
4. Нажать «Начать» и убедиться, что мониторинг включён.
5. У существующих получателей аккаунты и тестовые подписки создаются миграцией автоматически.

Старые 21 отправленный лид повторно отправляться не должны. Новое уведомление придёт только после появления подходящей заявки.

## 13. Проверять работу

```sh
cd /opt/leadfinder
sudo docker compose ps
sudo docker compose logs --since 30m
sudo docker compose exec leadfinder python -m leadgen.app status
```

Контейнер использует `restart: unless-stopped`, а Docker включён в автозапуск. После перезагрузки сервера бот должен подняться автоматически.

Алерты об ошибках в Telegram: с рабочего аккаунта отправьте боту `/start`, узнайте свой ID
(например, у @userinfobot) и добавьте в `.env` `ERROR_ALERT_CHAT_ID=<ваш id>` (несколько — через запятую;
`ALERT_ON_START=1` — сообщение при запуске). Пусто — выключено. Одна ошибка приходит не чаще раза в 10 минут
(потом с числом повторов), не больше 20 алертов в час на процесс. Проверка — та же команда `--test` ниже.

Необязательно: отправка ошибок в Sentry или self-hosted GlitchTip. Добавьте в `.env` `SENTRY_DSN`
(и при желании `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_RELEASE`, см. `.env.example`),
перезапустите контейнер и проверьте доставку тестового события:

```sh
sudo docker compose exec leadfinder python -m leadgen.observability --test
```

Без `SENTRY_DSN` ничего не отправляется. Персональные данные и токены вычищаются до отправки.

## 14. Автоматически обновлять код из GitHub

После первого запуска установить `systemd`-таймер:

```sh
cd /opt/leadfinder
sudo chmod +x deploy/*.sh
sudo deploy/install-auto-update.sh
```

Таймер каждые пять минут сравнивает локальный коммит с `origin/main`. Если в GitHub появился новый коммит, сервер выполняет fast-forward, собирает образ и перезапускает контейнер. `.env` и `leadgen/data/telegram_reader.session` не затрагиваются.

### Обновление идёт не от root

Сервисы `leadfinder-update` и `leadfinder-renew` работают от системного пользователя `leadfinder-deploy`. У него нет пароля и входа в оболочку. Он состоит в группе `docker`, а домашний каталог у него `/var/lib/leadfinder-deploy`. Systemd дополнительно ограничивает сервисы. `NoNewPrivileges` запрещает повышать права через sudo и setuid. `ProtectSystem=strict` делает файловую систему доступной только для чтения, кроме `/opt/leadfinder` и домашнего каталога. `PrivateTmp` даёт сервису собственный `/tmp`.

Установщик выполняет эти шаги сам, и запускать его повторно безопасно:

1. Создаёт `leadfinder-deploy`, если такого пользователя ещё нет, и добавляет его в группу `docker`.
2. Передаёт ему владение `/opt/leadfinder`, включая `.git` и `.env`. Каталог `leadgen/data` остаётся за пользователем контейнера (uid 10001).
3. Ставит обновлённые юниты. Если `leadfinder-renew.service` уже был установлен, юнит заменяется, а состояние таймера сохраняется.

Что сделать на сервере, где автообновление уже работало от root:

```sh
cd /opt/leadfinder
sudo git pull --ff-only          # последний раз от root, чтобы получить новый установщик
sudo deploy/install-auto-update.sh
sudo systemctl start leadfinder-update.service
sudo journalctl -u leadfinder-update.service -n 50 --no-pager
sudo systemctl start leadfinder-renew.service   # только если продление уже было включено
sudo journalctl -u leadfinder-renew.service -n 50 --no-pager
```

Проверьте ещё два момента:

- `git -C /opt/leadfinder remote get-url origin` должен начинаться с `https://`. Если используется SSH-ключ из `/root/.ssh`, сервис его больше не увидит. Положите deploy-ключ в `/var/lib/leadfinder-deploy/.ssh/` с владельцем `leadfinder-deploy` и правами `600`.
- Ручные git-команды в `/opt/leadfinder` выполняйте от владельца: `sudo -u leadfinder-deploy git -C /opt/leadfinder status`. Иначе git откажет с ошибкой `dubious ownership`.

Членство в группе `docker` по сути даёт права root через Docker API. Отдельный пользователь не делает Docker безопаснее. Зато скрипт из репозитория больше не исполняется напрямую от root, не может менять системные файлы вне разрешённых путей и не получает новые привилегии.

Проверка таймера и журнала обновлений:

```sh
sudo systemctl status leadfinder-update.timer --no-pager
sudo systemctl list-timers leadfinder-update.timer --no-pager
sudo journalctl -u leadfinder-update.service -n 100 --no-pager
```

Запустить проверку обновлений немедленно:

```sh
sudo systemctl start leadfinder-update.service
```

## 15. Остановка и восстановление

Перезапуск:

```sh
cd /opt/leadfinder
sudo docker compose restart
```

Полная остановка:

```sh
sudo docker compose down
```

Повторный запуск:

```sh
sudo docker compose up -d
```

После переезда (раздел 16) рабочая база — контейнер `db` на этом же сервере, данные в Docker-томе `pgdata`. Не выполняйте `docker compose down -v`: флаг `-v` удаляет том с базой. Кроме базы и её резервных копий в `/opt/leadfinder/backups` критичны `.env`, `.env.db` и `leadgen/data/telegram_reader.session`. Их копии храните в зашифрованном хранилище, желательно вне сервера. Telegram-сессия фактически даёт доступ к подключённому аккаунту.

## 16. Свой Postgres на сервере вместо Supabase

### Зачем и как устроено

Бесплатный тариф Supabase упёрся в лимит диска, и база перешла в режим только для чтения. Поэтому база переезжает на этот же VPS:

- Сервис `db` в `compose.yaml` — официальный образ `postgres:17.11-bookworm` с точной версией. Его используют оба стека: `-f compose.yaml` и `-f compose.yaml -f compose.web.yaml`. Порт наружу не публикуется: контейнеры обращаются к базе по адресу `db:5432` внутри сети compose. Администрирование: `docker compose exec db psql -U postgres -d leadgen`.
- Данные лежат в именованном томе `pgdata`. Сервис перезапускается сам (`restart: unless-stopped`) и проверяется через `pg_isready`. `leadfinder` и `web` стартуют только после того, как база станет `healthy`.
- Пароли хранятся в отдельном файле `.env.db` (шаблон — `.env.db.example`). Его читает только контейнер `db`, поэтому пароль суперпользователя `postgres` не попадает в контейнеры приложения. Приложение подключается ролью `leadgen`: это владелец базы без прав суперпользователя.
- При первом запуске пустого тома `deploy/postgres/init/10-leadgen.sh` создаёт то, что раньше давал Supabase: роли `anon` и `authenticated` без входа, схему `extensions` с `pg_trgm` и `pg_stat_statements`, роль приложения.
- Память рассчитана на сервер с 4 ГБ RAM без swap: `shared_buffers=512MB`, `effective_cache_size=2GB`, `work_mem=8MB`, `maintenance_work_mem=256MB`, `max_connections=50`. Запросы дольше 1 с и ожидания блокировок пишутся в журнал. Журнал контейнера ограничен тремя файлами по 10 МБ.
- `deploy/leadfinder-update.sh` перед миграциями сначала поднимает `db` и ждёт статуса `healthy`. Это нужно, потому что `run --no-deps` зависимости не запускает.
- Каждую ночь в 03:30 МСК таймер `leadfinder-backup.timer` делает `pg_dump -Fc` схемы `leadgen` в `/opt/leadfinder/backups/leadgen-<время>.dump`. Перед сохранением архив проверяется через `pg_restore --list`. Копии старше 14 дней удаляются, но только после успешной новой копии.

Учёт миграций: `web_migrate` записывает применённые файлы в таблицу `leadgen.web_schema_versions` (имя и SHA-256 содержимого). Таблица лежит в схеме `leadgen` и попадает в дамп. Поэтому после восстановления применяются только миграции, которых ещё не было в Supabase. Если содержимое уже применённого файла изменилось, `web_migrate` остановится с ошибкой и ничего не изменит.

### Переезд: команды по порядку

Все команды выполняются на сервере от root. Строки после `#` — пояснения, их копировать не нужно.

**0. Подготовка.** Нужно не меньше 5 ГБ свободного места: база около 0,9 ГБ, плюс дамп, индексы и WAL. Выберите набор compose-файлов: с веб-кабинетом используется вариант с двумя файлами, только с ботом — с одним.

```sh
sudo -i
cd /opt/leadfinder
df -h /opt /var/lib/docker
cat /etc/systemd/system/leadfinder-update.service.d/environment.conf   # LEADFINDER_WEB=1 → веб-кабинет включён
F="-f compose.yaml -f compose.web.yaml"   # только бот: F="-f compose.yaml"
```

**1. Остановить автообновление и приложение.** Делается до загрузки нового кода.

```sh
systemctl stop leadfinder-update.timer
systemctl stop leadfinder-renew.timer 2>/dev/null || true
docker compose $F stop
```

**2. Получить новый код.** Делается после слияния PR.

```sh
sudo -u leadfinder-deploy git -C /opt/leadfinder pull --ff-only
```

**3. Создать `.env.db` со случайными паролями.** Права `600`, владелец `leadfinder-deploy`.

```sh
install -m 600 -o leadfinder-deploy -g leadfinder-deploy /dev/null .env.db
printf 'POSTGRES_PASSWORD=%s\nLEADGEN_DB_USER=leadgen\nLEADGEN_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env.db
install -d -m 700 -o leadfinder-deploy -g leadfinder-deploy backups
```

**4. Запустить базу.** Пустой том инициализируется скриптом `deploy/postgres/init/10-leadgen.sh`.

```sh
docker compose $F up -d --wait db
docker compose $F exec db psql -U postgres -d leadgen -c '\du' -c '\dx'
```

Ожидаемый результат: роли `leadgen`, `anon`, `authenticated`; расширения `pg_trgm` и `pg_stat_statements` в схеме `extensions`.

**5. Снять дамп из Supabase.** Клиент берётся той же версии, что и сервер `db`. Дамп работает и на базе в режиме только для чтения. `DATABASE_URL` должен указывать на **Session pooler**, порт `5432`. Transaction pooler (порт `6543`) для `pg_dump` не подходит: если в `.env` указан он, замените в переменной `:6543/` на `:5432/`.

```sh
OLD_DATABASE_URL=$(grep '^DATABASE_URL=' .env | cut -d= -f2-)
docker run --rm postgres:17.11-bookworm psql "$OLD_DATABASE_URL" -X -At -c 'SHOW server_version'   # должно быть 17.x или ниже
docker run --rm postgres:17.11-bookworm pg_dump "$OLD_DATABASE_URL" --schema=leadgen --no-owner --no-privileges -Fc > backups/supabase-final.dump
ls -lh backups/supabase-final.dump
docker compose $F exec -T db pg_restore --list < backups/supabase-final.dump | grep -c 'TABLE DATA'
```

Если `SHOW server_version` вернул 18 или выше, остановитесь: тогда в `compose.yaml` и в этих командах нужен образ той же старшей версии.

**6. Восстановить дамп в локальную базу.** Восстановление идёт от роли приложения, поэтому все объекты будут принадлежать `leadgen`. Всё выполняется одной транзакцией: при ошибке в базе ничего не останется, и команду можно повторить.

```sh
docker compose $F exec -T db pg_restore -U leadgen -d leadgen --no-owner --no-privileges --single-transaction --exit-on-error < backups/supabase-final.dump
```

Права (`REVOKE … FROM anon, authenticated`) повторно выдавать не нужно. Таблицы закрыты для этих ролей с самого начала, а владелец `leadgen` имеет полный доступ.

**7. Сверить количество строк с Supabase.** Сравнивается каждая таблица схемы `leadgen`.

```sh
docker run --rm -i postgres:17.11-bookworm psql "$OLD_DATABASE_URL" -X -At < deploy/postgres/row-counts.sql > /root/counts-supabase.txt
docker compose $F exec -T db psql -X -At -U leadgen -d leadgen < deploy/postgres/row-counts.sql > /root/counts-local.txt
diff /root/counts-supabase.txt /root/counts-local.txt && echo "Количество строк совпадает"
docker compose $F exec db psql -U leadgen -d leadgen -c 'SELECT count(*) AS applied, max(name) AS last FROM leadgen.web_schema_versions'
```

`diff` не должен ничего вывести. В `web_schema_versions` должны быть миграции, которые уже применялись в Supabase.

**8. Переключить `DATABASE_URL` на локальную базу.** Старый `.env` сохраняется для отката.

```sh
cp -p .env backups/env-supabase.bak
APP_PW=$(grep '^LEADGEN_DB_PASSWORD=' .env.db | cut -d= -f2-)
sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://leadgen:${APP_PW}@db:5432/leadgen|" .env
sed -i 's/^DB_PREPARE_THRESHOLD=none$/DB_PREPARE_THRESHOLD=/' .env   # prepared statements снова можно использовать
chown leadfinder-deploy:leadfinder-deploy .env && chmod 600 .env
grep '^DATABASE_URL=' .env | sed 's/:[^:@]*@/:***@/'
```

**9. Применить новые миграции.** Таймауты увеличены, потому что `20261014140000_typed_lead_columns.sql` переписывает таблицу `leads` (около 0,6 ГБ). Индексы `CONCURRENTLY` строятся без ограничения по времени.

```sh
docker compose $F build --pull
docker compose $F run --rm --no-deps -e LEADGEN_MIGRATE_STATEMENT_TIMEOUT=30min -e LEADGEN_MIGRATE_LOCK_TIMEOUT=1min leadfinder python -m leadgen.web_migrate
```

Ожидаемый результат: строки «Применена миграция …» только для файлов, которых ещё не было в `web_schema_versions`, и в конце «Схема веб-кабинета готова.».

**10. Запустить приложение и проверить.**

```sh
docker compose $F up -d --remove-orphans --wait --wait-timeout 300
docker compose $F ps
docker compose $F exec leadfinder python -m leadgen.app status
docker compose $F logs --since 10m leadfinder | tail -n 50
```

Проверьте в Telegram `/start` у бота. Если веб-кабинет включён, откройте сайт и войдите.

**11. Вернуть автообновление и включить резервные копии.** Значение `LEADFINDER_WEB` должно совпадать с шагом 0. Установщик теперь ставит и таймер резервных копий.

```sh
LEADFINDER_WEB=1 deploy/install-auto-update.sh   # только бот: LEADFINDER_WEB=0
systemctl start leadfinder-backup.service
journalctl -u leadfinder-backup.service -n 20 --no-pager
ls -lh backups/
systemctl start leadfinder-renew.timer 2>/dev/null || true   # если продление было включено
```

`backups/supabase-final.dump` и `backups/env-supabase.bak` автоматически не удаляются. Удалите их вручную, когда откат больше не понадобится.

### Откат на Supabase

Откат возможен, пока Supabase доступна на запись: после перехода на платный тариф или после освобождения места. Всё, что бот записал в локальную базу после шага 10, в Supabase не попадёт. Чтобы перенести эти данные, снимите дамп локальной базы (как в «Восстановлении из резервной копии» ниже) и восстановите его в Supabase.

```sh
cd /opt/leadfinder
docker compose $F stop leadfinder web   # только бот: docker compose $F stop leadfinder
cp -p backups/env-supabase.bak .env && chown leadfinder-deploy:leadfinder-deploy .env && chmod 600 .env
docker compose $F up -d --wait
docker compose $F exec leadfinder python -m leadgen.app status
```

Контейнер `db` при этом продолжает работать, приложению он не мешает. Новый код совместим с Supabase: Supabase-роли и схема `extensions` там уже есть.

### Восстановление из резервной копии

Перед восстановлением сделайте свежую копию текущего состояния: `systemctl start leadfinder-backup.service`. Затем:

```sh
cd /opt/leadfinder
ls -lh backups/
docker compose $F stop leadfinder web   # только бот: docker compose $F stop leadfinder
docker compose $F exec db psql -U postgres -d leadgen -c 'DROP SCHEMA leadgen CASCADE'
docker compose $F exec -T db pg_restore -U leadgen -d leadgen --no-owner --no-privileges --single-transaction --exit-on-error < backups/leadgen-ДАТА.dump
docker compose $F run --rm --no-deps leadfinder python -m leadgen.web_migrate
docker compose $F up -d --wait
```

Если потерян весь том `pgdata`, выполните `docker compose $F up -d --wait db`: скрипт инициализации создаст роли и схему `extensions` заново. После этого нужны только команды `pg_restore` и следующие за ней.

Пустую базу без дампа (например, для теста) можно собрать из файлов миграций: сначала три начальных файла, затем `web_migrate`.

```sh
for f in 20260929134042_create_leadgen_schema 20260930092451_add_multiuser_product 20260930093818_harden_multiuser_schema; do
    docker compose $F exec -T db psql -X -q -v ON_ERROR_STOP=1 -U leadgen -d leadgen < supabase/migrations/$f.sql
done
docker compose $F run --rm --no-deps leadfinder python -m leadgen.web_migrate
```

### Наблюдение за базой

```sh
docker compose $F exec db psql -U leadgen -d leadgen -c "SELECT pg_size_pretty(pg_database_size('leadgen'))"
docker compose $F exec db psql -U leadgen -d leadgen -c 'SELECT calls, round(mean_exec_time) AS ms, left(query, 80) FROM extensions.pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10'
docker compose $F logs --since 1h db | grep -E 'duration|lock|ERROR' | tail -n 30
docker stats --no-stream
```

## Типичные ошибки

### `Conflict: terminated by other getUpdates request`

Где-то работает второй экземпляр бота. Остановить локальный процесс и другие контейнеры, затем выполнить:

```sh
sudo docker compose restart
```

### `Сессия служебного аккаунта не авторизована`

Файл `leadgen/data/telegram_reader.session` есть, но Telegram отозвал сессию. Монитор не падает: он проверяет сессию раз в 10 минут, а `status` показывает `"telegram_reader": {"state": "unauthorized"}`. Войти заново по QR-коду (код по SMS на серверный IP часто не приходит):

```sh
cd /opt/leadfinder
sudo docker compose stop leadfinder
sudo docker compose run --rm --no-deps leadfinder python -m leadgen.app setup-telegram --qr
sudo docker compose start leadfinder
sudo docker compose run --rm --no-deps leadfinder python -m leadgen.app status
```

Отсканировать QR в Telegram служебного аккаунта: «Настройки → Устройства → Подключить устройство». QR обновляется каждые ~30 секунд; при двухэтапной проверке команда спросит облачный пароль. После запуска в логах должно появиться `Мониторинг Telegram запущен`, а в `status` — `"state": "running"`.

### `Монитор Telegram остановлен`

Монитор перезапускается сам с паузой 5 с → 5 мин; причина (тип и текст ошибки) есть в той же строке лога и в `telegram_reader.last_error` команды `status`. Если строка повторяется постоянно, проверить наличие и права файла:

```sh
ls -l leadgen/data/telegram_reader.session
sudo chown -R 10001:10001 leadgen/data
sudo chmod 600 leadgen/data/telegram_reader.session
sudo docker compose restart
```

### Контейнер `unhealthy` или постоянно перезапускается

```sh
sudo docker compose ps
sudo docker compose logs --tail 200
sudo docker compose run --rm --no-deps leadfinder python -m leadgen.app status
```

### Нет уведомлений

Проверить кнопку «Статус», значение `active`, бюджет и включённые услуги. Уведомления появляются только для новых заявок, которые проходят фильтры.
