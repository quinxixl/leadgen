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
2. Передаёт ему владение `/opt/leadfinder`, включая `.git` и `.env`. Каталог `leadgen/data` остаётся за пользователем контейнера (uid 10001). Группа каталога — `leadfinder-deploy`, права `750`. Если каталога нет, установщик его создаёт. Файлы внутри не трогает.

`.dockerignore` устроен так, чтобы сборка вообще не открывала `leadgen/data`. BuildKit пропускает исключённый каталог, только если каждое исключение `!` — обычный путь, допускается лишь `/**` в конце. Одной строки вроде `!leadgen/*.py` или `!supabase/migrations/*.sql` достаточно, чтобы он полез во все каталоги, включая `leadgen/data`. Поэтому модули `leadgen/*.py` перечислены поимённо. Новый модуль нужно добавить в `.dockerignore`, иначе его не будет в образе. Тест `tests/test_deploy.py` это проверяет.
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

Бесплатный тариф Supabase упёрся в лимит диска, и база стала доступна только для чтения. Поэтому база переезжает на этот же VPS.

- **Сервис `db` в `compose.yaml`.** Образ `postgres:17.11-bookworm`, закреплён по тегу и по digest. Его используют оба стека (`-f compose.yaml` и `-f compose.yaml -f compose.web.yaml`).
  - Данные лежат в томе `pgdata`. Порт наружу не публикуется: контейнеры ходят к базе как `db:5432`.
  - Healthcheck — `pg_isready`, политика перезапуска — `restart: unless-stopped`.
  - `leadfinder` и `web` стартуют только после того, как база станет `healthy`.
  - Каталог `/opt/leadfinder/backups` подключён в контейнер как `/backups` только для чтения. Дампы восстанавливаются из файла, а не через stdin.
- **Пароли.** Хранятся в `.env.db` (права `600`, шаблон — `.env.db.example`), этот файл читает только контейнер `db`.
  - Приложение подключается ролью `leadgen`: она владелец базы, но не суперпользователь.
  - Суперпользователь `postgres` может войти только изнутри контейнера: через сокет или loopback. Вход по TCP извне для него запрещён правилом `reject` в `pg_hba.conf`.
  - Почему не `secrets:` из compose: файлы-секреты compose без Swarm монтируются с правами файла на хосте, а entrypoint образа читает их от uid 999. Файл `600` от root он не прочитает, а `644` раскрыл бы пароль всем.
- **Инициализация.** `deploy/postgres/init/10-leadgen.sh` выполняется при первом запуске пустого тома. Скрипт идемпотентен, пароли читает через `\getenv`, а не из командной строки. Он создаёт:
  - роли `anon` и `authenticated` без входа;
  - схему `extensions` с `pg_trgm` и `pg_stat_statements`;
  - роль приложения;
  - заглушку `public.rls_auto_enable()`, которую нужно отозвать по старой миграции;
  - схема `public` остаётся за `postgres`, у приложения на ней только `USAGE`.
- **Таймауты роли приложения** такие же, как были в Supabase: `statement_timeout=120s` и `idle_in_transaction_session_timeout=60s`. На них опирается курсор доставки: его задержка `LEAD_CURSOR_LAG_SECONDS=600` должна быть больше любой транзакции приложения (`leadgen/product.py`, `cursor_lag_seconds`).
  - `web_migrate` повышает эти лимиты сам, только внутри своих транзакций.
  - `pg_dump`/`pg_restore` выставляют свои лимиты.
  - `vacuumdb` и резервные копии работают от `postgres`, на них эти лимиты не действуют.
- **Память.** Рассчитана на 4 ГБ RAM без swap.
  - Параметры базы: `shared_buffers=512MB`, `effective_cache_size=2GB`, `work_mem=8MB`, `maintenance_work_mem=256MB`, `max_connections=50`.
  - У базы `oom_score_adj=-800`: при нехватке памяти ядро сначала завершит приложение, а не базу.
  - Лимиты приложения: `leadfinder` 768 МБ, `web` 512 МБ. Проверка — `docker stats --no-stream`.
- **Журнал базы.** Запросы дольше 1 с и ожидания блокировок пишутся в журнал. Значения параметров запросов туда не попадают (`log_parameter_max_length=0`). Размер журнала контейнера ограничен: 3 файла по 10 МБ.
- **Автообновление.** `deploy/leadfinder-update.sh` запускает базу командой `up -d --no-recreate --wait db` до `web_migrate`. Пересоздаются только `leadfinder` и `web` (`--no-deps`). Обновление кода никогда не перезапускает базу. Изменения в настройках `db` применяются вручную (см. «Обслуживание»).
- **Резервные копии.** `leadfinder-backup.timer` каждую ночь в 03:30 МСК делает `pg_dump -Fc` схемы `leadgen` в `backups/leadgen-<время>.dump`.
  - Архив проверяется полным чтением (`pg_restore -f /dev/null`).
  - Копии старше 14 дней удаляются, но только после успешной новой копии.
  - При сбое `leadfinder-alert@.service` отправляет Telegram-алерт на `ERROR_ALERT_CHAT_ID` тем же механизмом, что и ошибки приложения. Токен остаётся в `.env`.

Учёт миграций ведётся в `leadgen.web_schema_versions`: имя файла и SHA-256 его содержимого. Таблица лежит в схеме `leadgen` и попадает в дамп. Поэтому после восстановления применяются только миграции, которых ещё не было в Supabase. Если уже применённый файл изменён, `web_migrate` остановится и ничего не изменит.

### Переезд: команды по порядку

Всё выполняется от root на сервере, по одному блоку. Пользователя `leadfinder-deploy` может ещё не быть, поэтому до шага 11 не используйте `sudo -u`, `chown` и `install -o`. Не включайте `set -e` в интерактивной оболочке: при ошибке она закроется. Вместо этого каждый блок заканчивается проверкой. Если проверка не прошла, остановитесь и смотрите «Откат».

**Не запускайте `deploy/install-auto-update.sh` до шага 11.** Он сразу стартует `leadfinder-update.service`, а тот поднимет приложение с текущим `.env`.

**0. Подготовка.** Нужно не меньше 5 ГБ свободного места на диске.

```sh
sudo -i
cd /opt/leadfinder
F="-f compose.yaml -f compose.web.yaml"   # только бот: F="-f compose.yaml"
PGIMG=postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826
df -h /opt /var/lib/docker
free -h
```

**1. Остановить автообновление и приложение; запомнить, что откатывать.**

```sh
systemctl stop leadfinder-update.timer leadfinder-update.service
systemctl stop leadfinder-renew.timer 2>/dev/null || true
systemctl is-active leadfinder-update.timer leadfinder-update.service   # обе строки: inactive
cp .leadfinder-deployed-sha /root/leadfinder-rollback.sha && cat /root/leadfinder-rollback.sha
git -C /opt/leadfinder cat-file -e "$(cat /root/leadfinder-rollback.sha)^{commit}" && echo "Коммит для отката найден"
install -m 600 .env /root/env-supabase.bak && ls -l /root/env-supabase.bak
docker compose $F stop && docker compose $F ps
```

Откатываться нужно на код, который сейчас запущен. Его записывает автообновление в `.leadfinder-deployed-sha`. `git rev-parse HEAD` для этого не годится: если последнее обновление упало на сборке или миграции, checkout уже впереди работающего кода. Если файла `.leadfinder-deployed-sha` нет, автообновление ни разу не завершилось. Тогда возьмите коммит, с которого собирали работающий образ, и запишите его в `/root/leadfinder-rollback.sha` вручную.

**2. Получить новый код** (после слияния PR).

```sh
git -C /opt/leadfinder pull --ff-only && git -C /opt/leadfinder log -1 --oneline
```

**3. Создать `.env.db` со случайными паролями и каталог копий.**

```sh
(umask 077; printf 'POSTGRES_PASSWORD=%s\nLEADGEN_DB_USER=leadgen\nLEADGEN_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env.db)
install -d -m 700 backups
ls -l .env.db && ls -ld backups   # -rw------- и drwx------
```

**4. Запустить базу.**

```sh
docker compose $F up -d --wait db
docker compose $F exec db psql -U postgres -d leadgen -c '\du' -c '\dx'
docker compose $F exec db psql -U postgres -d leadgen -Atc "SELECT rolconfig FROM pg_roles WHERE rolname='leadgen'"
```

Ожидаемый результат: роли `leadgen`, `anon`, `authenticated`; `pg_trgm` и `pg_stat_statements` в схеме `extensions`; у роли `leadgen` таймауты `{statement_timeout=120s,idle_in_transaction_session_timeout=60s}`.

**5. Снять дамп из Supabase.** Адрес старой базы передаётся контейнеру через переменную окружения, а не аргументом команды.

- Нужен **Session pooler**, порт `5432`. Transaction pooler (`6543`) не подходит.
- Дамп снимается и с базы, которая доступна только для чтения.
- Версия сервера должна быть 17.x или ниже. Если она 18 или выше, остановитесь: понадобится образ той же старшей версии.

```sh
export PGURL="$(grep '^DATABASE_URL=' /root/env-supabase.bak | cut -d= -f2- | sed 's|:6543/|:5432/|')"
docker run --rm -e PGURL "$PGIMG" sh -c 'exec psql "$PGURL" -X -At -c "SHOW server_version"'
(umask 077; docker run --rm -e PGURL "$PGIMG" sh -c 'exec pg_dump "$PGURL" --schema=leadgen --no-owner --no-privileges -Fc' > backups/supabase-final.dump)
ls -lh backups/supabase-final.dump
```

Проверить, что в дампе нет чужих для этой базы объектов: политик RLS, event-триггеров, расширений, ссылок на `auth.` или `public.`.

```sh
docker compose $F exec db pg_restore --list /backups/supabase-final.dump | grep -c 'TABLE DATA'
docker compose $F exec db pg_restore --list /backups/supabase-final.dump | grep -E 'POLICY|EVENT TRIGGER|EXTENSION' || echo "Чужих объектов нет"
docker compose $F exec db pg_restore --schema-only -f - /backups/supabase-final.dump | grep -nE 'auth\.|public\.' || echo "Ссылок на auth./public. нет"
```

Если `grep` что-то нашёл, не восстанавливайте дамп, пока не разберётесь, что это за объекты.

**6. Восстановить дамп в локальную базу и собрать статистику.** Восстановление идёт от роли приложения, одной транзакцией: при ошибке в базе ничего не останется.

```sh
docker compose $F exec db pg_restore -U leadgen -d leadgen --no-owner --no-privileges --single-transaction --exit-on-error /backups/supabase-final.dump && echo RESTORE_OK
docker compose $F exec db vacuumdb -U postgres -d leadgen --analyze-in-stages
docker compose $F exec db psql -U postgres -d leadgen -c 'VACUUM (ANALYZE) leadgen.leads' -c 'VACUUM (ANALYZE) leadgen.user_leads'
```

**7. Сверить количество строк в каждой таблице.**

```sh
docker run --rm -i -e PGURL "$PGIMG" sh -c 'exec psql "$PGURL" -X -At' < deploy/postgres/row-counts.sql > /root/counts-supabase.txt
docker compose $F exec -T db psql -X -At -U leadgen -d leadgen < deploy/postgres/row-counts.sql > /root/counts-local.txt
diff /root/counts-supabase.txt /root/counts-local.txt && echo "Количество строк совпадает"
unset PGURL
docker compose $F exec db psql -U leadgen -d leadgen -c 'SELECT count(*) AS applied, max(name) AS last FROM leadgen.web_schema_versions'
```

**8. Переключить `DATABASE_URL` на локальную базу.** Пароль не попадает в аргументы команд: `grep -v` и встроенный `printf`.

```sh
(umask 077; { grep -v -e '^DATABASE_URL=' -e '^DB_PREPARE_THRESHOLD=' /root/env-supabase.bak; printf 'DATABASE_URL=postgresql://leadgen:%s@db:5432/leadgen\nDB_PREPARE_THRESHOLD=\n' "$(grep '^LEADGEN_DB_PASSWORD=' .env.db | cut -d= -f2-)"; } > .env.new) && mv .env.new .env
grep -c '^DATABASE_URL=' .env                         # 1
grep '^DATABASE_URL=' .env | sed 's/:[^:@]*@/:***@/'  # ...leadgen:***@db:5432/leadgen
```

**9. Применить новые миграции.** `20261014140000_typed_lead_columns.sql` переписывает таблицу `leads` (около 0,6 ГБ), поэтому лимит времени поднят до 2 часов. Во второй SSH-сессии можно следить за диском и памятью: `watch -n 10 'df -h /var/lib/docker; docker stats --no-stream'`.

```sh
docker compose $F build --pull
docker compose $F run --rm --no-deps -e LEADGEN_MIGRATE_STATEMENT_TIMEOUT=2h -e LEADGEN_MIGRATE_LOCK_TIMEOUT=1min leadfinder python -m leadgen.web_migrate
docker compose $F exec db vacuumdb -U postgres -d leadgen --analyze-in-stages
docker compose $F exec db psql -U postgres -d leadgen -c 'VACUUM (ANALYZE) leadgen.leads' -c 'VACUUM (ANALYZE) leadgen.user_leads'
```

Ожидаемый результат: «Применена миграция …» только для тех файлов, которых не было в `web_schema_versions`, и в конце «Схема веб-кабинета готова.».

**10. Запустить приложение и проверить.**

```sh
docker compose $F up -d --wait --wait-timeout 300
docker compose $F ps
docker compose $F exec leadfinder python -m leadgen.app status
docker compose $F logs --since 10m leadfinder | tail -n 50
```

Проверьте `/start` у бота и вход в веб-кабинет. Если что-то не так, смотрите «Откат», вариант А.

**11. Только теперь: автообновление, права и резервные копии.** Установщик создаёт `leadfinder-deploy`, передаёт ему каталог, ставит таймеры и сразу запускает обновление. `LEADFINDER_WEB` укажите как раньше (`/etc/systemd/system/leadfinder-update.service.d/environment.conf`, если файл есть).

```sh
LEADFINDER_WEB=1 sh deploy/install-auto-update.sh   # только бот: LEADFINDER_WEB=0
systemctl start leadfinder-backup.service && journalctl -u leadfinder-backup.service -n 20 --no-pager
ls -lh backups/
systemctl start leadfinder-renew.timer 2>/dev/null || true   # если продление было включено
ls -ld leadgen/data   # drwxr-x--- 10001 leadfinder-deploy
journalctl -u leadfinder-update.service -n 30 --no-pager   # сборка без «open leadgen/data: permission denied»
```

Установщик открывает `leadgen/data` группе `leadfinder-deploy` на чтение (владелец 10001, группа `leadfinder-deploy`, права `750`). Со старым `.dockerignore` без этого `docker compose build` от `leadfinder-deploy` падал с ошибкой `open leadgen/data: permission denied`. Текущий `.dockerignore` в каталог не заходит, права оставлены как вторая страховка. Права файлов внутри каталога, включая `telegram_reader.session`, установщик не меняет.

**12. Через 1–2 недели, когда откат точно не понадобится.**

- Смените пароль базы в Supabase или поставьте проект на паузу и удалите его.
- Удалите локальные следы старой базы:

```sh
rm -f /root/env-supabase.bak /root/counts-supabase.txt /root/counts-local.txt /root/leadfinder-rollback.sha /opt/leadfinder/backups/supabase-final.dump
```

### Откат

**А. До шага 10 или сразу после него, если приложение на новой базе не заработало.** Возвращаются старый код и старый `.env`. Приложение будет работать как до переезда, то есть с Supabase только на чтение. Контейнер `db` остаётся как «осиротевший» сервис и ничему не мешает. Не используйте `--remove-orphans`.

```sh
cd /opt/leadfinder
docker compose $F stop
git -C /opt/leadfinder checkout --detach "$(cat /root/leadfinder-rollback.sha)"
install -m 600 /root/env-supabase.bak .env
docker compose $F build && docker compose $F up -d --wait
```

Чтобы потом повторить переезд: `git -C /opt/leadfinder checkout main` и снова шаг 2.

**Б. После переключения, когда в локальной базе уже есть новые данные.** Старый код с этими данными работать не может: в них уже типизированные колонки. Пока Supabase доступна только для чтения, вернуться на неё нельзя. Когда запись там снова открыта (платный тариф или освобождено место), новые данные переносятся дампом при новом коде:

```sh
cd /opt/leadfinder
docker compose $F stop leadfinder web   # только бот: docker compose $F stop leadfinder
(umask 077; docker compose $F exec -T db pg_dump -U postgres -d leadgen --schema=leadgen --no-owner --no-privileges -Fc > /root/local-final.dump)
export PGURL="$(grep '^DATABASE_URL=' /root/env-supabase.bak | cut -d= -f2-)"
docker run --rm -e PGURL "$PGIMG" sh -c 'exec psql "$PGURL" -X -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions" -c "DROP SCHEMA leadgen CASCADE"'
docker run --rm -i -e PGURL "$PGIMG" sh -c 'exec pg_restore -d "$PGURL" --no-owner --no-privileges --single-transaction --exit-on-error' < /root/local-final.dump
unset PGURL
install -m 600 /root/env-supabase.bak .env
docker compose $F up -d --wait
```

`DROP SCHEMA leadgen CASCADE` удаляет старые данные в Supabase. Выполняйте его только после того, как файл `/root/local-final.dump` создан и не пустой.

### Восстановление из резервной копии

Текущая схема переименовывается и удаляется только после проверки, поэтому до конца процедуры к ней можно вернуться. На время процедуры нужно вдвое больше места под базу.

```sh
cd /opt/leadfinder
systemctl start leadfinder-backup.service       # свежая копия текущего состояния
ls -lh backups/
docker compose $F stop leadfinder web           # только бот: docker compose $F stop leadfinder
docker compose $F exec db psql -U leadgen -d leadgen -c 'ALTER SCHEMA leadgen RENAME TO leadgen_old'
docker compose $F exec db pg_restore -U leadgen -d leadgen --no-owner --no-privileges --single-transaction --exit-on-error /backups/leadgen-ДАТА.dump && echo RESTORE_OK
docker compose $F exec -T db psql -X -At -U leadgen -d leadgen < deploy/postgres/row-counts.sql
docker compose $F run --rm --no-deps leadfinder python -m leadgen.web_migrate
docker compose $F exec db vacuumdb -U postgres -d leadgen --analyze-in-stages
docker compose $F up -d --wait
docker compose $F exec db psql -U leadgen -d leadgen -c 'DROP SCHEMA leadgen_old CASCADE'   # после проверки
```

Если восстановление не удалось, верните прежнюю схему: `ALTER SCHEMA leadgen_old RENAME TO leadgen`. Если частично созданная схема `leadgen` всё же осталась, сначала удалите её. Если потерян весь том `pgdata`, выполните `docker compose $F up -d --wait db`: инициализация создаст роли и схему `extensions`. Затем сразу переходите к `pg_restore`.

Пустую базу без дампа (например, для теста) можно собрать из файлов миграций: три начальных файла, затем `web_migrate`.

```sh
for f in 20260929134042_create_leadgen_schema 20260930092451_add_multiuser_product 20260930093818_harden_multiuser_schema; do
    docker compose $F exec -T db psql -X -q -v ON_ERROR_STOP=1 -U leadgen -d leadgen < supabase/migrations/$f.sql
done
docker compose $F run --rm --no-deps leadfinder python -m leadgen.web_migrate
```

Необязательная еженедельная проверка: свежая копия восстанавливается во временную базу, затем база удаляется.

```sh
f=$(ls -t backups/leadgen-*.dump | head -n 1); echo "$f"
docker compose $F exec db createdb -U postgres -O leadgen leadgen_check
docker compose $F exec db psql -U postgres -d leadgen_check -c 'CREATE SCHEMA extensions' -c 'CREATE EXTENSION pg_trgm WITH SCHEMA extensions' -c 'GRANT USAGE ON SCHEMA extensions TO leadgen'
docker compose $F exec db pg_restore -U leadgen -d leadgen_check --no-owner --no-privileges --single-transaction --exit-on-error "/backups/$(basename "$f")" && echo RESTORE_OK
docker compose $F exec db dropdb -U postgres leadgen_check
```

**Копия вне сервера (рекомендуется, вручную).** Если сервер потерян, локальные копии пропадут вместе с ним. Зашифруйте копию паролем, который хранится отдельно, и унесите к себе:

```sh
gpg --symmetric --cipher-algo AES256 --output /root/leadgen-ДАТА.dump.gpg /opt/leadfinder/backups/leadgen-ДАТА.dump
# на своём компьютере: scp root@SERVER_IP:/root/leadgen-ДАТА.dump.gpg . && ssh root@SERVER_IP rm /root/leadgen-ДАТА.dump.gpg
```

Никогда не храните и не копируйте дампы вместе с `.env`, `.env.db`, Telegram-сессией или ключами: вместе они дают полный доступ к данным и аккаунтам.

### Обслуживание

**Изменить настройки `db`** (параметры `command`, память) или **обновить минорную версию**. Для обновления поменяйте тег и digest в `compose.yaml` и `PGIMG` в этом разделе; digest берётся из `docker buildx imagetools inspect postgres:<тег>`. Автообновление базу не пересоздаёт (`--no-recreate`), поэтому изменения применяются вручную. База будет недоступна несколько секунд.

```sh
cd /opt/leadfinder
systemctl start leadfinder-backup.service
docker compose $F stop leadfinder web   # только бот: docker compose $F stop leadfinder
docker compose $F up -d --force-recreate --wait db
docker compose $F up -d --wait
```

Раз в квартал проверяйте, не вышла ли новая минорная версия 17.x (https://www.postgresql.org/support/versioning/). Обновления безопасности выходят только в минорных версиях.

**Сменить пароль роли приложения.**

```sh
cd /opt/leadfinder
NEW=$(openssl rand -hex 32)
(umask 077; { grep -v '^LEADGEN_DB_PASSWORD=' .env.db; printf 'LEADGEN_DB_PASSWORD=%s\n' "$NEW"; } > .env.db.new) && mv .env.db.new .env.db
(umask 077; { grep -v '^DATABASE_URL=' .env; printf 'DATABASE_URL=postgresql://leadgen:%s@db:5432/leadgen\n' "$NEW"; } > .env.new) && mv .env.new .env
unset NEW
chown leadfinder-deploy:leadfinder-deploy .env .env.db
docker compose $F up -d --force-recreate --wait db
docker compose $F exec db sh /docker-entrypoint-initdb.d/10-leadgen.sh
docker compose $F up -d --force-recreate --wait
```

Пароль `postgres` по TCP не используется: вход по TCP для этой роли запрещён. При необходимости его меняют интерактивно: `docker compose $F exec db psql -U postgres -c '\password postgres'`, затем обновляют `POSTGRES_PASSWORD` в `.env.db`.

**Swap (по желанию владельца, вручную).** На сервере с 4 ГБ без swap страхует от OOM при пиках.

```sh
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf && sysctl --system
```

**Наблюдение.**

```sh
docker compose $F exec db psql -U leadgen -d leadgen -c "SELECT pg_size_pretty(pg_database_size('leadgen'))"
docker compose $F exec db psql -U leadgen -d leadgen -c 'SELECT calls, round(mean_exec_time) AS ms, left(query, 80) FROM extensions.pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10'
docker compose $F logs --since 1h db | grep -E 'duration|lock|ERROR' | tail -n 30
docker stats --no-stream
systemctl list-timers leadfinder-backup.timer --no-pager
```

**Технический долг.** Роль `leadgen` сейчас и владеет схемой, и используется приложением. Следующий шаг — разделить их: роль-владелец выполняет миграции через отдельный `MIGRATE_DATABASE_URL`, а роль, под которой работает приложение, получает только `SELECT/INSERT/UPDATE/DELETE`.

## 17. Nginx для веб-кабинета (HTTPS, сжатие, статика, лимиты)

Полная конфигурация сайта лежит в `deploy/nginx/signalid.ru.conf` (nginx 1.24 из Ubuntu 24.04). В ней:

- редирект `http://` → `https://` для `signalid.ru` и `www.signalid.ru`;
- HTTPS с HTTP/2 и сертификатом certbot. Кэш TLS-сессий и `ssl_session_tickets off` уже задаёт
  `/etc/letsencrypt/options-ssl-nginx.conf`, поэтому в файле их нет: повтор директивы nginx не примет.
  OCSP stapling не включён: Let's Encrypt больше не поддерживает OCSP;
- gzip для CSS, JS, SVG, JSON и текста (`app.css` — примерно 53 КБ → 13 КБ);
- `/web_static/` и `/favicon.ico` nginx отдаёт сам из `/opt/leadfinder/leadgen/web_static`, без gunicorn.
  Файлы с `?v=…` кэшируются на год (`immutable`), без версии — на час, иконка — на сутки;
- `X-Forwarded-Host` ставит сам nginx: заголовок клиента в приложение не попадает;
- лимиты на IP: вход (`/login`, `/tg/auth`, подключение Telegram-аккаунта: телефон, код, пароль) — 10 запросов
  в минуту с запасом 10, `/api/` — 60 в минуту с запасом 30. Сверх лимита — ответ 429;
- соединения к приложению переиспользуются (`keepalive`).

`default_server` из Ubuntu этот файл не трогает. Автообновление конфигурацию nginx не ставит: после изменений
в `deploy/nginx/` повторите шаги ниже вручную.

**Первая установка на новом сервере.** Сначала выпустите сертификат, потом ставьте файл — без сертификата
`nginx -t` не пройдёт:

```sh
sudo apt install -y nginx certbot python3-certbot-nginx
sudo certbot certonly --nginx -d signalid.ru -d www.signalid.ru
```

**Установка и обновление.** Все команды — на сервере, после того как автообновление подтянуло код
(`git -C /opt/leadfinder log -1 --oneline` показывает нужный коммит).

1. Узнать имя файла сайта и сделать копию текущей версии:

   ```sh
   ls -l /etc/nginx/sites-enabled/
   SITE=leadfinder          # имя из вывода ls, если оно другое — подставьте своё
   SITE_FILE=$(readlink -f /etc/nginx/sites-enabled/$SITE)
   echo "$SITE_FILE"
   sudo cp "$SITE_FILE" /root/nginx-signalid.bak
   ```

2. Проверить, что nginx (`www-data`) может читать статику. Каталоги по пути должны быть открыты на проход
   (`x` для всех), файлы — на чтение (`644`):

   ```sh
   sudo namei -l /opt/leadfinder/leadgen/web_static/app.css
   sudo -u www-data test -r /opt/leadfinder/leadgen/web_static/app.css && echo OK
   ```

   Если нет `OK`, откройте только проход по каталогам и чтение статики. `.env` и `leadgen/data` остаются закрытыми:

   ```sh
   sudo chmod o+x /opt/leadfinder /opt/leadfinder/leadgen
   sudo find /opt/leadfinder/leadgen/web_static -type d -exec chmod 755 {} + -o -type f -exec chmod 644 {} +
   ```

3. Поставить новый файл, проверить и применить:

   ```sh
   sudo install -m 0644 /opt/leadfinder/deploy/nginx/signalid.ru.conf "$SITE_FILE"
   sudo nginx -t && sudo systemctl reload nginx
   ```

   `reload` не обрывает текущие соединения. Если `nginx -t` выдал ошибку, ничего не применилось — верните копию
   (шаг «Откат») и пришлите вывод.

4. Проверить:

   ```sh
   curl -sI --http2 https://signalid.ru/ | head -1                      # HTTP/2 200
   curl -sI -H 'Accept-Encoding: gzip' https://signalid.ru/web_static/app.css | grep -i -E 'content-encoding|cache-control'
   # content-encoding: gzip, cache-control: public, max-age=3600
   curl -sI -H 'Accept-Encoding: gzip' 'https://signalid.ru/web_static/app.css?v=1' | grep -i cache-control
   # cache-control: public, max-age=31536000, immutable
   curl -sI https://signalid.ru/favicon.ico | grep -i -E '^HTTP|cache-control'   # 200, max-age=86400
   curl -sI http://www.signalid.ru/ | grep -i -E '^HTTP|location'      # 301 на https://www.signalid.ru/
   curl -s -o /dev/null -w '%{http_code}\n' https://signalid.ru/login    # 200
   sudo tail -n 20 /var/log/nginx/error.log
   ```

   Затем откройте кабинет в браузере, войдите и убедитесь, что стили и скрипты загрузились.

**Откат.**

```sh
SITE_FILE=$(readlink -f /etc/nginx/sites-enabled/leadfinder)   # имя из шага 1
sudo cp /root/nginx-signalid.bak "$SITE_FILE"
sudo nginx -t && sudo systemctl reload nginx
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
