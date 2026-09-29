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

## 5. Проверить локальные секреты

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

## 6. Упаковать проект на Mac

Из каталога проекта:

```sh
tar \
  --exclude='.env' \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='outputs' \
  --exclude='leadgen/data' \
  --exclude='__pycache__' \
  --exclude='*.sqlite*' \
  -czf /tmp/leadfinder.tar.gz .
```

Архив содержит код и Docker-конфигурацию. Секреты копируются отдельно.

## 7. Передать файлы на сервер

На Mac заменить `SERVER_IP`:

```sh
ssh root@SERVER_IP 'mkdir -p /opt/leadfinder/leadgen/data'
scp /tmp/leadfinder.tar.gz root@SERVER_IP:/tmp/leadfinder.tar.gz
scp .env root@SERVER_IP:/opt/leadfinder/.env
scp leadgen/data/telegram_reader.session root@SERVER_IP:/opt/leadfinder/leadgen/data/telegram_reader.session
```

Если используется пользователь `ubuntu`, заменить `root` на `ubuntu`, а каталог `/opt/leadfinder` создать через `sudo`.

## 8. Распаковать и защитить секреты

На сервере:

```sh
cd /opt/leadfinder
sudo tar -xzf /tmp/leadfinder.tar.gz -C /opt/leadfinder
sudo chmod 600 .env
sudo chown -R 10001:10001 leadgen/data
sudo chmod 700 leadgen/data
sudo chmod 600 leadgen/data/telegram_reader.session
sudo rm /tmp/leadfinder.tar.gz
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
Мониторинг Telegram запущен.
```

Выйти из просмотра журнала можно через `Ctrl+C`: контейнер продолжит работать в фоне. Через одну-две минуты `sudo docker compose ps` должен показывать состояние `healthy`.

## 12. Проверить Telegram

1. Открыть бота с основного аккаунта из `TELEGRAM_CHAT_ID`.
2. Отправить `/start` и убедиться, что появилось меню.
3. Нажать «Статус» — мониторинг должен быть включён, бюджет должен быть от 10 000 ₽.
4. Дополнительные аккаунты из базы получают новые карточки лидов, но не управляют настройками.

Старые 21 отправленный лид повторно отправляться не должны. Новое уведомление придёт только после появления подходящей заявки.

## 13. Проверять работу

```sh
cd /opt/leadfinder
sudo docker compose ps
sudo docker compose logs --since 30m
sudo docker compose exec leadfinder python -m leadgen.app status
```

Контейнер использует `restart: unless-stopped`, а Docker включён в автозапуск. После перезагрузки сервера бот должен подняться автоматически.

## 14. Обновлять код

Сначала подготовить новый архив на Mac по шагу 6, затем передать его на сервер. `.env` и `telegram_reader.session` повторно копировать не нужно.

На сервере:

```sh
cd /opt/leadfinder
sudo tar -xzf /tmp/leadfinder.tar.gz -C /opt/leadfinder
sudo rm /tmp/leadfinder.tar.gz
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose logs --tail 100
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

Рабочая база находится в Supabase. На сервере критичны только `.env` и `leadgen/data/telegram_reader.session`. Хранить их резервную копию следует в зашифрованном хранилище. Telegram-сессия фактически даёт доступ к подключённому аккаунту.

## Типичные ошибки

### `Conflict: terminated by other getUpdates request`

Где-то работает второй экземпляр бота. Остановить локальный процесс и другие контейнеры, затем выполнить:

```sh
sudo docker compose restart
```

### `Монитор Telegram остановлен`

Проверить наличие и права файла:

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
