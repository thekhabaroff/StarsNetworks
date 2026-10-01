# Stars Networks

Stars Networks — бот для продажи Telegram Stars и подписок Telegram Premium. В
каталоге доступны виртуальные товары **Telegram Stars** и подарочные подписки
**Telegram Premium** на 3, 6 или 12 месяцев. После оплаты бот сохраняет заказ,
получателя и попытки доставки, затем отправляет выбранный товар через
Fragment; при временной ошибке доступна повторная отправка из карточки заказа.

## Быстрый запуск Docker

1. Запустите `./start.sh`: он создаст локальный `.env` и попросит заполнить
   Telegram, PostgreSQL, webhook и служебные параметры. Реквизиты ЮKassa,
   TON и других провайдеров подключаются позже из панели управления.
2. После заполнения `.env` соберите и запустите production-стек:

   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose logs -f bot
   ```

Контейнер бота всегда называется `starsnetworks`. Данные PostgreSQL и токен
Fragment хранятся в именованных томах `starsnetworks_postgres_data` и
`starsnetworks_data`; `docker compose down` их не удаляет.

## Подключение Fragment

Актуальный Fragment API не авторизует магазин прямым вызовом старого
`/auth/authenticate/`. Сначала один раз создайте подключение в
[https://fragment-api.com/dashboard](https://fragment-api.com/dashboard) → **Fragment Connections** → **Add
Connection**:

1. Укажите номер Telegram-аккаунта, который подключает кошелёк.
2. Вставьте ровно 24 слова TON Wallet recovery phrase. Это должна быть
   TON-native mnemonic с TON checksum, созданная в Tonkeeper, MyTonWallet или
   другом TON-кошельке. Обычная BIP-39/multichain-фраза от Ledger, Trust
   Wallet, MetaMask и подобных кошельков не подходит, даже если в ней тоже 24
   английских слова.
3. Выберите версию кошелька: `W5` для нового V5R1 (W5) или `V4R2` для старого.
4. Подтвердите Telegram OAuth по ссылке, которую выдаст кабинет.
5. Скопируйте **Token** созданного connection в `FRAGMENT_CONNECTION_TOKEN`.

## Автоматическая себестоимость

Для системных товаров Telegram Stars и Telegram Premium бот по умолчанию
использует автоматическую цену: получает актуальную стоимость с публичной
страницы Fragment, добавляет API-сбор из `FRAGMENT_API_FEE_PERCENT` (по
умолчанию 0,5%), переводит сумму в рубли по текущему курсу USD и применяет
процент наценки из админки. В редакторе товара нажмите «📈 Себестоимость + %»
и введите только процент, например `25`.

Котировка обновляется при открытии каталога, перед оформлением заказа и в
фоновом режиме. Если Fragment временно недоступен, последняя сохранённая цена
не затирается; новый заказ в автоматическом режиме не создаётся без свежей
котировки.

После этого перезапустите бота: `docker compose up -d --force-recreate bot`.
Мнемоника и телефон больше не нужны контейнеру для обычной доставки: удалите
их из `.env` после создания connection и ни при каких обстоятельствах не
передавайте в чат.

Не используйте для стороннего API-провайдера кошелёк с крупным балансом:
создайте отдельный операционный TON-кошелёк и держите на нём только рабочий
запас. Если recovery phrase уже попала на неизвестный сайт или в переписку,
переведите средства на новый кошелёк.

Telegram-бот для продажи цифровых товаров. Пользовательский интерфейс построен только на inline-клавиатурах; для покупателя нет reply-клавиатур и текстовых команд меню.

Production-развёртывание проекта поддерживается через Docker Compose: бот, PostgreSQL, постоянные тома, автоматические миграции и reverse proxy для HTTPS webhook.

`requirements.txt` намеренно содержит только основной список пакетов без
закреплённых версий. Docker-образ дополнительно устанавливает `greenlet` —
обязательный runtime-компонент асинхронного SQLAlchemy для работы бота и
Alembic.

## Способы оплаты

- Telegram Stars — штатный способ оплаты цифровых товаров в Telegram; по
  умолчанию включён, но может быть отключён администратором.
- Внутренний баланс — доступен всегда, если на нём достаточно средств.
- ЮKassa, ЮMoney, Heleket, Lava и CryptoBot — необязательные внешние способы. Каждый показывается только после настройки реквизитов и включения в пункте управления.

Тестовой/бесплатной оплаты и Robokassa в проекте нет. Если внешние провайдеры
не подключены, бот предлагает баланс и включённый Telegram Stars.

### Каталог виртуальных товаров

Миграция автоматически создаёт три Premium-тарифа с начальными ценами
`1200`, `1600` и `2900` ₽. Цены можно изменить в «Пункт управления → Каталог»;
тарифы нельзя удалить или заменить складскими аккаунтами. Для каждого товара
доступен режим «Себестоимость + %»: укажите полную себестоимость в рублях
(товар, сеть, API и прочие расходы) и наценку, после чего цена для пользователя
будет рассчитана автоматически. Уже созданные заказы сохраняют свою цену.

## Целостность платежей

- У платежа есть уникальная пара `(payment_method, payment_id)` и внутренний
  `external_order_id`; повторный webhook не списывает баланс и не выдаёт товар
  второй раз.
- Строки пользователя, заказа, платежа и зарезервированных аккаунтов
  блокируются в одном порядке. Оплата корзины **с баланса** выполняется одной
  транзакцией: либо оплачиваются все её заказы, либо ни один.
- После подтверждения Telegram Stars или создания внешнего счёта резерв не
  освобождается только по локальному таймауту: это защищает от оплаты старой
  ссылки после отмены. Внешний счёт освобождается после финальной отмены/
  истечения у самого провайдера.
- Активный товар и ожидающий оплаты заказ не могут иметь нулевую или
  отрицательную сумму: это дополнительно запрещено ограничениями PostgreSQL,
  а не только интерфейсом скидок.
- Любое изменение внутреннего баланса записывается в неизменяемый журнал в
  той же транзакции. В «Пункт управления → Журнал» видны общая сумма на
  балансах, остаток каждого пользователя и история движений.

## Архитектура production

```text
Telegram / платёжные провайдеры
             │ HTTPS :443
             ▼
  Nginx на хосте (TLS, public webhook URLs)
             │ HTTP 127.0.0.1:PAYMENT_WEBHOOK_PORT
             ▼
       bot container ───────────► Telegram / provider APIs
             │ app role, private Docker network
             ▼
      PostgreSQL container ◄──── migrate (owner role)
             ▲
             └────────────────── db-init (issues app DML grants)
             │
       named volume (persistent data)
```

PostgreSQL не публикует порт на хост. Порт webhook бота привязан только к
`127.0.0.1:PAYMENT_WEBHOOK_PORT` (по умолчанию `18743`), поэтому внешние
запросы должны проходить через HTTPS reverse proxy.

## Требования

- Docker Engine 25+ и Docker Compose v2;
- домен с A/AAAA-записью на сервер для webhook-режима;
- Nginx (или совместимый HTTPS reverse proxy) на хосте;
- токен, созданный в [@BotFather](https://t.me/BotFather).

Для production используйте PostgreSQL из Compose. SQLite и запуск `python bot.py` на хосте не являются поддерживаемым способом развёртывания.

## Пошаговый деплой на чистый сервер

Инструкция рассчитана на Debian 12+/Ubuntu 22.04+ и запуск от `root`.
До начала создайте A-запись домена на IP сервера и откройте на уровне
провайдера/фаервола только `22/tcp`, `80/tcp` и `443/tcp`.

### 1. Подключитесь к серверу и установите Docker, Nginx и Certbot

```bash
ssh root@SERVER_IP

apt-get update
apt-get remove -y docker.io docker-compose docker-doc podman-docker containerd runc || true
apt-get install -y ca-certificates curl git nginx certbot openssl

install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker nginx
docker compose version
```

### 2. Клонируйте репозиторий

Репозиторий можно клонировать по HTTPS:

```bash
mkdir -p /opt
git clone https://github.com/thekhabaroff/StarsNetworks.git /opt/starsnetworks
cd /opt/starsnetworks
```

Если позднее репозиторий станет приватным, добавьте публичный SSH-ключ сервера
в **GitHub → Settings → Deploy keys** с правом только на чтение. Сначала
создайте ключ и выведите его публичную часть:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/starsnetworks -C "starsnetworks deploy" -N ""
cat /root/.ssh/starsnetworks.pub
```

Добавьте показанный ключ в GitHub, затем настройте SSH и клонируйте проект:

```bash
cat > /root/.ssh/config <<'EOF'
Host github.com
    HostName github.com
    User git
    IdentityFile /root/.ssh/starsnetworks
    IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
ssh -T git@github.com

mkdir -p /opt
git clone git@github.com:thekhabaroff/StarsNetworks.git /opt/starsnetworks
cd /opt/starsnetworks
```

Для приватного репозитория также можно использовать HTTPS URL и GitHub token
вместо пароля при `git clone`.

### 3. Заполните `.env`

После клонирования `.env` создаётся локально автоматически и никогда не
публикуется в GitHub. Скрипт заполнит его: Telegram, PostgreSQL,
webhook и служебные параметры; реквизиты платёжных систем и ссылки
взаимодействия позднее задаются в админ-панели. Он создаст два независимых
пароля БД: для владельца/миграций и для ограниченной роли приложения.

```bash
cd /opt/starsnetworks
chmod 755 start.sh
./start.sh
```

Для PostgreSQL выберите генерацию паролей, если нет собственных URL-safe
паролей. После заполнения `.env` становится локальным файлом с секретами:
`start.sh` включает `skip-worktree` и версионируемую pre-commit-проверку, чтобы
его нельзя было случайно закоммитить. В GitHub остаётся только исходный код и шаблоны без секретов.

### 4. Выпустите TLS-сертификат

Certbot будет использовать webroot, поэтому Nginx не останавливается при
первичном выпуске и последующих продлениях. Домен берётся из уже заполненного
`.env`; укажите только почту, затем временно включите HTTP-only конфигурацию
для ACME challenge.

```bash
DOMAIN=$(awk -F= '$1 == "PAYMENT_PUBLIC_BASE_URL" { sub(/^https:\/\//, "", $2); print $2; exit }' .env)
test -n "$DOMAIN" || { echo "Сначала заполните .env через ./start.sh" >&2; exit 1; }
export CERTBOT_EMAIL=admin@example.com

install -d -m 755 /var/www/html
cat > /etc/nginx/sites-available/starsnetworks-bootstrap <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 404;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/starsnetworks-bootstrap \
  /etc/nginx/sites-enabled/starsnetworks
nginx -t && systemctl reload nginx

certbot certonly --webroot -w /var/www/html --non-interactive --agree-tos \
  --email "$CERTBOT_EMAIL" -d "$DOMAIN"
```

### 5. Установите Nginx-конфигурацию и включите автопродление сертификата

`nginx.conf` — шаблон. `start.sh` сам берёт домен и внутренний порт из `.env`
и создаёт итоговый файл. Поэтому шесть `proxy_pass` не нужно менять вручную и
порт не сможет разойтись с настройкой контейнера.

```bash
./start.sh --render-nginx /etc/nginx/sites-available/starsnetworks
ln -sfn /etc/nginx/sites-available/starsnetworks \
  /etc/nginx/sites-enabled/starsnetworks
nginx -t
systemctl enable --now nginx

install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
printf '%s\n' '#!/bin/sh' 'systemctl reload nginx' \
  > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
chmod 700 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
systemctl enable --now certbot.timer
certbot renew --dry-run
```

### 6. Соберите и запустите бота

```bash
cd /opt/starsnetworks
docker compose up -d --build
docker compose ps
docker compose logs --tail=200 bot
PAYMENT_WEBHOOK_PORT=$(awk -F= '$1 == "PAYMENT_WEBHOOK_PORT" { print $2; exit }' .env)
curl -fsS "http://127.0.0.1:${PAYMENT_WEBHOOK_PORT}/health"
```

У `db` должен быть статус `healthy`, у `migrate` и `db-init` — `exited (0)`,
у `bot` — `running` или `healthy`. Сначала отдельный сервис `migrate`
применяет Alembic от имени владельца БД, затем `db-init` выдаёт приложению
только DML-права. Telegram webhook регистрируется ботом при первом старте.

### Автозапуск после перезагрузки сервера

Контейнеры имеют `restart: unless-stopped`, поэтому Docker Compose автоматически поднимет их после перезагрузки Docker или сервера.

## Переменные окружения

Все переменные перечислены ниже. После клонирования `start.sh` создаёт локальный `.env` с правами `600`. Рабочий `.env` с ключами нельзя коммитить, передавать или включать в бэкапы.

### Telegram и webhook

| Переменная              | Обязательна | Назначение                                                                                                                                                                                      |
| --------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`                     | Да                   | Токен BotFather. Хранится только в`.env`.                                                                                                                                           |
| `BOT_USERNAME`                  | Да                   | Username бота без`@`.                                                                                                                                                                            |
| `ADMIN_IDS`                     | Да                   | Telegram ID администраторов через запятую.                                                                                                                                     |
| `DEVELOPER_IDS`                 | Нет                 | Telegram ID разработчиков через запятую.                                                                                                                                         |
| `WEBHOOK_URL`                   | Production             | Полный URL Telegram webhook, например`https://bot.example.com/webhook/telegram`.                                                                                                          |
| `PAYMENT_PUBLIC_BASE_URL`       | Production             | Публичный HTTPS origin без пути, например`https://bot.example.com`; используется для callback URL внешних провайдеров.                         |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | Production             | Независимый случайный секрет для заголовка Telegram webhook. Сгенерируйте отдельное значение командой`openssl rand -hex 32`. |
| `PAYMENT_WEBHOOK_PORT`          | Нет                 | Внутренний HTTP-порт бота, по умолчанию`18743`. Он доступен только через Nginx на хосте.                                                       |
| `SUPPORT_ID`                    | Нет                 | Ссылка на поддержку; меняется через «Настройки → Взаимодействие».                                                                                 |
| `COMMUNITY_ID`                  | Нет                 | Ссылка на сообщество; меняется через «Настройки → Взаимодействие».                                                                               |
| `TERMS_OF_SERVICE`              | Нет                 | Ссылка на пользовательское соглашение; меняется через «Настройки → Взаимодействие».                                              |
| `PRIVACY_POLICY`                | Нет                 | Ссылка на политику конфиденциальности; меняется через «Настройки → Взаимодействие».                                              |

Telegram передаёт этот secret token в специальном HTTP-заголовке. Его проверка не позволяет постороннему отправить в webhook поддельное обновление, например фальшивое сообщение администратора или платёжное событие.

### PostgreSQL

| Переменная      | Обязательна | Назначение                                                                                                                                                                                 |
| ------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POSTGRES_DB`           | Да                   | Имя базы, создаваемой контейнером PostgreSQL.                                                                                                                           |
| `POSTGRES_USER`         | Да                   | Владелец БД: используется только PostgreSQL и сервисом`migrate`.                                                                                              |
| `POSTGRES_PASSWORD`     | Да                   | Пароль владельца БД; не используется приложением для запросов.                                                                                  |
| `POSTGRES_APP_USER`     | Да                   | Ограниченная роль приложения без superuser, CREATE DATABASE/ROLE и DDL.                                                                                                |
| `POSTGRES_APP_PASSWORD` | Да                   | Независимый URL-safe пароль роли приложения.                                                                                                                          |
| `DATABASE_URL`          | Да                   | URL ограниченной роли вида`postgresql+asyncpg://APP_USER:APP_PASSWORD@db:5432/DB`. Хостом внутри Compose всегда является `db`, не `localhost`. |

`migrate` всегда запускается до `bot` и выполняет Alembic от имени владельца
БД. После него `db-init` создаёт/обновляет ограниченную роль: ей разрешены
только `SELECT`, `INSERT`, `UPDATE`, `DELETE` для таблиц приложения и
`USAGE`/`SELECT` для последовательностей. Для `balance_ledger` у неё только
`SELECT`/`INSERT`; БД также запрещает изменение и удаление записей триггером.
Роль не владеет схемой или таблицами и не может менять миграции.

### Платёжные провайдеры

Для ЮMoney, ЮKassa, Heleket и Lava обязательно заполните общий `PAYMENT_PUBLIC_BASE_URL`. Для CryptoBot публичный HTTPS URL нужен для webhook, но сам способ оплаты может работать и без него благодаря фоновой сверке статуса через API.

| Способ   | Дополнительные обязательные переменные                                                          | Когда доступен                                                                                                              |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Telegram Stars | Нет ключей в`.env`                                                                                                      | Когда включён`PAYMENT_STARS_ENABLED`; invoice отправляется в валюте `XTR`.                            |
| Баланс   | Нет                                                                                                                              | Всегда, при достаточном балансе.                                                                              |
| ЮKassa        | Реквизиты указываются в пункте управления                                                      | Только если заполнены реквизиты и система включена.                                          |
| ЮMoney        | Номер кошелька и секрет HTTP-уведомлений указываются в пункте управления | Только если заполнены реквизиты, задан публичный HTTPS URL и система включена. |
| Heleket        | Реквизиты указываются в пункте управления                                                      | Только если заполнены оба значения.                                                                        |
| Lava           | Реквизиты указываются в пункте управления                                                      | Только если заполнены ID, секретный ключ и один ключ для webhook.                            |
| CryptoBot      | API token Crypto Pay указывается в пункте управления                                                    | Только если указан API token; webhook доступен при заданном публичном HTTPS URL.             |

`LAVA_SECRET_KEY` используется для подписи запроса создания счёта. `LAVA_ADDITIONAL_KEY` используется для проверки подписи webhook Lava; `LAVA_WEBHOOK_SECRET` оставлен как совместимый резервный ключ для ранее настроенных webhook. Срок счёта Lava составляет 15 минут и задан внутри приложения.

Для ЮMoney в личном кабинете включите HTTP-уведомления и укажите URL `https://ваш-домен/webhook/yoomoney`; секрет из кабинета сохраните в поле «Секрет HTTP-уведомлений». QuickPay-ссылка создаётся на каждый локальный платёж с уникальной меткой, а зачисление выполняется только после корректной подписи, валюты и точного совпадения суммы. Для CryptoBot используйте токен Crypto Pay и webhook `https://ваш-домен/webhook/cryptobot`; бот дополнительно сверяет итоговый статус счёта через API.

Флаги доступности оплаты (`PAYMENT_YOOKASSA_ENABLED`, `PAYMENT_YOOMONEY_ENABLED`, `PAYMENT_LAVA_ENABLED`, `PAYMENT_HELEKET_ENABLED`, `PAYMENT_CRYPTOBOT_ENABLED`, `PAYMENT_STARS_ENABLED`) берутся из `.env` как начальные значения, а изменения из «Пункт управления → Настройки → Платёжные системы» сохраняются в БД и переживают перезапуск контейнера. Дополнительные настройки из `.env`: `ORDER_RESERVATION_MINUTES` и `BROADCAST_THROTTLE`.

### Настройка чата поддержки

Добавьте бота в нужную группу поддержки. Администратор или разработчик из `.env` должен один раз отправить в этой группе `/set_support_chat`. Бот сохранит ID именно этого чата.

После этого ответ пользователю передаётся только если он является reply на служебное сообщение бота, написан в настроенной группе и отправлен доверенным администратором/разработчиком. Это исключает ситуацию, когда посторонний участник другой группы выдаёт себя за поддержку и пишет покупателю от имени магазина.

## Nginx, HTTPS и webhook

Конфигурация [nginx.conf](nginx.conf) рассчитана на Nginx на хосте и является
шаблоном. Перед установкой выполните
`./start.sh --render-nginx /etc/nginx/sites-available/starsnetworksprod`:
скрипт читает из `.env` только `PAYMENT_PUBLIC_BASE_URL` и
`PAYMENT_WEBHOOK_PORT`, проверяет их и подставляет `__DOMAIN__` и
`__PAYMENT_WEBHOOK_PORT__`. Nginx проксирует только следующие пути на
`127.0.0.1` и выбранный внутренний порт:

| Сервис                        | URL для настройки                                   |
| ----------------------------------- | --------------------------------------------------------------- |
| Telegram                            | `https://bot.example.com/webhook/telegram`                    |
| ЮKassa                             | `https://bot.example.com/webhook/yookassa`                    |
| ЮMoney                             | `https://bot.example.com/webhook/yoomoney`                    |
| Heleket                             | `https://bot.example.com/webhook/heleket`                     |
| Lava                                | `https://bot.example.com/webhook/lava`                        |
| CryptoBot                           | `https://bot.example.com/webhook/cryptobot`                   |
| Проверка состояния | Только с сервера:`http://127.0.0.1:PORT/health` |

Если меняете `PAYMENT_WEBHOOK_PORT`, перезапустите контейнер с новым портом,
затем перегенерируйте и перечитайте Nginx:

```bash
docker compose up -d --force-recreate bot
./start.sh --render-nginx /etc/nginx/sites-available/starsnetworks
nginx -t && systemctl reload nginx
curl -fsS "http://127.0.0.1:$(awk -F= '$1 == "PAYMENT_WEBHOOK_PORT" {print $2; exit}' .env)/health"
```

Не копируйте шаблон напрямую: Nginx не понимает значения `__DOMAIN__` и
`__PAYMENT_WEBHOOK_PORT__`.

Сертификат выпускается через Certbot в режиме `webroot`; поэтому стандартный
`certbot.timer` продлевает его без остановки Nginx. В firewall должны быть
открыты только `22/tcp` (если нужен SSH), `80/tcp` и `443/tcp`; выбранный
внутренний webhook-порт и PostgreSQL наружу не открывайте.

В панели каждого включённого провайдера настройте соответствующий URL из таблицы. Обрабатывайте только события, нужные для подтверждённых платежей. Проверка подписи провайдера и идемпотентность выполняются приложением; Nginx не заменяет эти проверки.

## Миграции базы данных

Миграции применяются автоматически отдельным сервисом `migrate` **до** старта
бота. Он использует владельца БД; приложение намеренно не имеет DDL-прав.
Для проверки или ручного запуска используйте:

```bash
docker compose run --rm migrate alembic -c config.ini current
docker compose run --rm migrate alembic -c config.ini upgrade head
docker compose run --rm migrate alembic -c config.ini history
```

Перед обновлением production сделайте резервную копию базы. Обновление выполняется так:

```bash
docker compose up -d --build --remove-orphans
docker compose logs -f bot
```

При обновлении старой установки добавьте в её `.env` до первого `docker compose up`
новые `POSTGRES_APP_USER` и `POSTGRES_APP_PASSWORD`, затем сформируйте
`DATABASE_URL` с этими значениями. После этого `migrate` сохранит владельца
схемы за `POSTGRES_USER`, а `db-init` выдаст новой роли только необходимые
права. Не переводите `DATABASE_URL` обратно на `POSTGRES_USER`.

Папку `migrations/versions/` нужно хранить в публичном production-репозитории:
это история преобразований структуры БД, а не данные или секреты. В миграции
нельзя добавлять реальные токены, URL с паролями или выгрузки пользователей.

Не используйте `docker compose down -v` на production: команда удаляет named volumes, включая данные PostgreSQL.

Перед первым обновлением дождитесь завершения либо вручную сверяйте все старые
внешние счета в статусе `PENDING`: у них ещё нет нового безопасного
`external_order_id`, поэтому приложение намеренно не зачисляет такой webhook
автоматически. Новые счета после миграции связаны с конкретной записью платежа.

## Резервное копирование

Данные PostgreSQL хранятся в named volume `postgres_data`. Логи приложения
пишутся в stdout/stderr контейнера; Docker хранит и ограничивает их размер
согласно настройке `logging` в `docker-compose.yml`.

Пример дампа в каталог `backups` на хосте:

```bash
mkdir -p backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backups/starsnetworks-$(date +%F-%H%M%S).sql
```

Проверяйте восстановление дампов на отдельной базе. Не включайте `.env` в резервные копии, которые передаются третьим лицам.

По текущему требованию `account_data` и дампы базы не шифруются приложением. Поэтому ограничьте доступ к Docker host, PostgreSQL volume и каталогу с дампами: в них находятся данные цифровых товаров в открытом виде.

## Операции и диагностика

| Задача                                                                                            | Команда                                                                                                        |
| ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Состояние сервисов                                                                     | `docker compose ps`                                                                                                 |
| Логи бота                                                                                       | `docker compose logs -f --tail=200 bot`                                                                             |
| Логи PostgreSQL                                                                                     | `docker compose logs -f --tail=200 db`                                                                              |
| Логи миграции/DB-роли                                                                   | `docker compose logs --tail=200 migrate db-init`                                                                    |
| Перезапуск бота                                                                           | `docker compose restart bot`                                                                                        |
| Проверка итоговой Compose-конфигурации без вывода секретов | `docker compose config --quiet`                                                                                     |
| Проверка readiness                                                                              | `PORT=$(awk -F= '$1 == "PAYMENT_WEBHOOK_PORT" {print $2; exit}' .env); curl -fsS "http://127.0.0.1:${PORT}/health"` |

Если bot container не запускается, сначала проверьте `docker compose logs migrate db-init bot`. Типичные причины: не заполнена обязательная переменная, `DATABASE_URL` не содержит роль `POSTGRES_APP_USER`, база ещё не готова или миграция завершилась ошибкой.

Если Telegram не доставляет обновления, проверьте HTTPS URL, доступность `/webhook/telegram` извне, соответствие `TELEGRAM_WEBHOOK_SECRET_TOKEN` и логи reverse proxy. Не отправляйте токен BotFather в диагностических сообщениях и не включайте его в логи.

## Ручная проверка покупки

После развёртывания можно провести проверку без отдельного тестового платёжного режима:

1. Начислите тестовому пользователю небольшой баланс через админ-панель.
2. Создайте тестовый товар и загрузите одну учётную запись.
3. Купите товар с баланса.
4. Убедитесь, что сумма списалась один раз, заказ завершён, а данные доступны для повторной выгрузки.

## Состав проекта

```text
Dockerfile             immutable image бота, запускаемый от root
.dockerignore          исключает `.env`, ключи, сертификаты и дампы из image
.githooks/pre-commit   не даёт закоммитить заполненный `.env`
docker-compose.yml     bot + PostgreSQL + изолированные migrate/db-init сервисы
start.sh               интерактивное создание и заполнение локального .env
.env                   локальный файл с секретами (не хранится в Git и не попадает в image)
nginx.conf             шаблон reverse proxy для webhook и локального health endpoint
database/bootstrap_app_role.sh  выдаёт приложению ограниченные DML-права
migrations/            миграции базы данных
```

## Безопасность

- Храните `BOT_TOKEN` в `.env`, а ключи провайдеров — только в настройках бота и PostgreSQL; после прежнего попадания токена в логи перевыпустите его в BotFather.
- Используйте отдельные случайные секреты для Telegram webhook и Lava webhook.
- Не публикуйте PostgreSQL и порт контейнера бота напрямую в интернете.
- Баланс, заказы, реферальные ссылки, каталог и пункт управления работают
  только в личном чате с ботом. Групповой доступ оставлен исключительно для
  настроенного чата поддержки.
- Обновляйте образ и зависимости регулярно, предварительно делая дамп базы.
- Ограничьте доступ к серверу и named volumes; цифровые товары и данные заказов являются чувствительными.

## Лицензия

Проект предназначен для внутреннего использования.
