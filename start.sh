#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE="$APP_DIR/.env"

run_container() {
    shift

    if [ "$#" -gt 0 ]; then
        exec "$@"
    fi

    # В production миграции выполняет отдельный Compose-сервис ``migrate``
    # от имени владельца БД. Контейнер бота использует ограниченную DB-роль.
    exec python -u bot.py
}

ask_required() {
    prompt=$1
    while :; do
        printf '%s: ' "$prompt"
        IFS= read -r REPLY || exit 1
        [ -n "$REPLY" ] && return
        echo "Это поле обязательно."
    done
}

ask_default() {
    prompt=$1
    default=$2
    printf '%s [%s]: ' "$prompt" "$default"
    IFS= read -r REPLY || exit 1
    [ -n "$REPLY" ] || REPLY=$default
}

is_bot_token() {
    value=$1
    prefix=${value%%:*}
    suffix=${value#*:}

    [ "$prefix" != "$value" ] || return 1
    [ -n "$prefix" ] && [ -n "$suffix" ] || return 1
    [ "${suffix#*:}" = "$suffix" ] || return 1
    case "$prefix" in
        *[!0-9]*) return 1 ;;
    esac
    case "$suffix" in
        *[!A-Za-z0-9_-]*) return 1 ;;
    esac
}

ask_bot_token() {
    while :; do
        ask_required "Токен Telegram-бота (BOT_TOKEN)"
        if is_bot_token "$REPLY"; then
            return
        fi
        echo "Токен должен иметь вид цифры:буквы_цифры-дефисы, без пробелов и специальных символов."
    done
}

is_bot_username() {
    value=$1
    [ "${#value}" -ge 5 ] && [ "${#value}" -le 32 ] || return 1
    case "$value" in
        *[!A-Za-z0-9_]*|'') return 1 ;;
    esac
}

ask_bot_username() {
    while :; do
        ask_required "Username бота без @ (BOT_USERNAME)"
        if is_bot_username "$REPLY"; then
            return
        fi
        echo "Username: 5–32 латинских букв, цифр или _. Символ @ не нужен."
    done
}

is_telegram_id_list() {
    value=$1
    case "$value" in
        ''|,*|*,|*,,*|*[!0-9,]*) return 1 ;;
    esac
}

ask_telegram_id_list() {
    prompt=$1
    default=${2:-}
    while :; do
        if [ -n "$default" ]; then
            ask_default "$prompt" "$default"
        else
            ask_required "$prompt"
        fi
        if is_telegram_id_list "$REPLY"; then
            return
        fi
        echo "Укажите один или несколько числовых Telegram ID через запятую, без пробелов."
    done
}

is_safe_domain() {
    value=$1
    case "$value" in
        ''|.*|*.) return 1 ;;
    esac
    case "$value" in
        *[!A-Za-z0-9.-]*|*..*) return 1 ;;
    esac
}

ask_domain() {
    default=${1:-stars.example.com}
    while :; do
        ask_default "Публичный домен webhook" "$default"
        if is_safe_domain "$REPLY"; then
            DOMAIN=$REPLY
            return
        fi
        echo "Укажите домен без https://, пути, порта или пробелов."
    done
}

is_postgres_identifier() {
    value=$1
    case "$value" in
        ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) return 1 ;;
    esac
    [ "${#value}" -le 63 ]
}

ask_postgres_identifier() {
    prompt=$1
    default=$2
    while :; do
        ask_default "$prompt" "$default"
        if is_postgres_identifier "$REPLY"; then
            return
        fi
        echo "Разрешены латинские буквы, цифры и _, первый символ — буква или _. Максимум 63 символа."
    done
}

ask_port() {
    default=${1:-18743}
    while :; do
        ask_default "Внутренний порт webhook" "$default"
        case "$REPLY" in
            *[!0-9]*|'') echo "Укажите число от 1024 до 65535." ;;
            80|443)
                echo "Порты 80 и 443 принадлежат Nginx. Выберите внутренний порт, например 18743."
                ;;
            *)
                if [ "$REPLY" -ge 1024 ] && [ "$REPLY" -le 65535 ]; then
                    PAYMENT_WEBHOOK_PORT=$REPLY
                    return
                fi
                echo "Укажите число от 1024 до 65535."
                ;;
        esac
    done
}

ask_positive_integer() {
    prompt=$1
    default=$2
    while :; do
        ask_default "$prompt" "$default"
        case "$REPLY" in
            *[!0-9]*|'') echo "Укажите целое число больше нуля." ;;
            *)
                if [ "$REPLY" -gt 0 ]; then
                    return
                fi
                echo "Укажите целое число больше нуля."
                ;;
        esac
    done
}

ask_urlsafe_password() {
    label=$1
    while :; do
        printf '%s\n' "$label: 1 — сгенерировать, 2 — ввести вручную"
        printf '%s' 'Выбор [1]: '
        IFS= read -r REPLY || exit 1
        case "${REPLY:-1}" in
            1)
                REPLY=$(generate_secret)
                return
                ;;
            2)
                ask_required "$label (только URL-safe символы)"
                case "$REPLY" in
                    *[!A-Za-z0-9._~-]*)
                        echo "Разрешены только буквы, цифры, точка, _, ~ и -."
                        ;;
                    *) return ;;
                esac
                ;;
            *) echo "Введите 1 или 2." ;;
        esac
    done
}

ensure_openssl() {
    if command -v openssl >/dev/null 2>&1; then
        return
    fi
    echo "Не найден openssl. Установите его командой: apt-get install -y openssl" >&2
    exit 1
}

generate_secret() {
    openssl rand -hex 32
}

protect_runtime_env_from_git() {
    # .env is tracked as a deliberately blank template. After filling it on a
    # host, make Git ignore local runtime values and enable the versioned
    # pre-commit guard. Neither setting affects the repository itself.
    if ! command -v git >/dev/null 2>&1; then
        return
    fi
    if ! git -C "$APP_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return
    fi
    # A GitHub clone normally has no .env because it is ignored. Only mark
    # the file skip-worktree when a user deliberately committed a blank
    # template; never fail setup for an untracked local secret file.
    if ! git -C "$APP_DIR" ls-files --error-unmatch .env >/dev/null 2>&1; then
        return
    fi
    git -C "$APP_DIR" update-index --skip-worktree .env || \
        echo "Предупреждение: не удалось включить локальную защиту .env в Git." >&2
    git -C "$APP_DIR" config core.hooksPath "$APP_DIR/.githooks" || \
        echo "Предупреждение: не удалось включить Git-проверку .env." >&2
}

set_env_value() {
    key=$1
    value=$2

    temp_file=$(mktemp "$ENV_FILE.tmp.XXXXXX")
    awk -v key="$key" -v value="$value" '
        BEGIN { prefix = key "="; replaced = 0 }
        index($0, prefix) == 1 {
            print prefix value
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) print prefix value
        }
    ' "$ENV_FILE" > "$temp_file"

    # Копирование поверх существующего файла сохраняет inode bind mount в
    # работающем контейнере. Атомарный rename здесь применять нельзя.
    cp "$temp_file" "$ENV_FILE"
    rm -f "$temp_file"
}

ensure_env_value() {
    key=$1
    value=$2
    if ! grep -q "^${key}=" "$ENV_FILE"; then
        set_env_value "$key" "$value"
    fi
}

read_env_value() {
    key=$1
    awk -v key="$key" '
        index($0, key "=") == 1 {
            print substr($0, length(key) + 2)
            exit
        }
    ' "$ENV_FILE"
}

configure_database_values() {
    existing_db=$(read_env_value POSTGRES_DB)
    existing_owner=$(read_env_value POSTGRES_USER)
    existing_owner_password=$(read_env_value POSTGRES_PASSWORD)
    existing_app=$(read_env_value POSTGRES_APP_USER)
    existing_app_password=$(read_env_value POSTGRES_APP_PASSWORD)

    if [ -z "$existing_db$existing_owner$existing_owner_password$existing_app$existing_app_password" ]; then
        # First installation: the PostgreSQL volume does not exist yet, so it
        # is safe to ask for and write all database identifiers/secrets.
        ask_postgres_identifier "Имя базы PostgreSQL" "starsnetworks"
        POSTGRES_DB=$REPLY
        ask_postgres_identifier "Пользователь-владелец PostgreSQL" "starsnetworks"
        POSTGRES_USER=$REPLY
        while :; do
            ask_postgres_identifier "Ограниченный пользователь приложения PostgreSQL" "${POSTGRES_USER}_app"
            POSTGRES_APP_USER=$REPLY
            if [ "$POSTGRES_APP_USER" != "$POSTGRES_USER" ]; then
                break
            fi
            echo "Пользователь приложения должен отличаться от пользователя-владельца БД."
        done
        ask_urlsafe_password "Пароль владельца PostgreSQL"
        POSTGRES_PASSWORD=$REPLY
        ask_urlsafe_password "Пароль пользователя приложения PostgreSQL"
        POSTGRES_APP_PASSWORD=$REPLY
        return
    fi

    if [ -z "$existing_db" ] || [ -z "$existing_owner" ] || [ -z "$existing_owner_password" ]; then
        echo "В .env частично заполнены параметры владельца PostgreSQL. Скрипт не меняет их, чтобы не потерять доступ к существующей БД." >&2
        echo "Либо заполните POSTGRES_DB, POSTGRES_USER и POSTGRES_PASSWORD вручную, либо очистите все три только для новой пустой установки." >&2
        exit 1
    fi

    POSTGRES_DB=$existing_db
    POSTGRES_USER=$existing_owner
    POSTGRES_PASSWORD=$existing_owner_password

    if [ -z "$existing_app$existing_app_password" ]; then
        # Safe upgrade path from an older installation: retain the owner
        # credentials that PostgreSQL already knows and create only the new
        # limited role credentials.
        echo "Найдена существующая PostgreSQL-конфигурация. Имя БД и пароль владельца сохранены; будет настроена только ограниченная роль приложения."
        while :; do
            ask_postgres_identifier "Ограниченный пользователь приложения PostgreSQL" "${POSTGRES_USER}_app"
            POSTGRES_APP_USER=$REPLY
            if [ "$POSTGRES_APP_USER" != "$POSTGRES_USER" ]; then
                break
            fi
            echo "Пользователь приложения должен отличаться от пользователя-владельца БД."
        done
        ask_urlsafe_password "Пароль пользователя приложения PostgreSQL"
        POSTGRES_APP_PASSWORD=$REPLY
        return
    fi

    if [ -z "$existing_app" ] || [ -z "$existing_app_password" ]; then
        echo "В .env частично заполнены параметры ограниченной роли PostgreSQL. Скрипт остановлен без изменений." >&2
        exit 1
    fi
    if [ "$existing_app" = "$POSTGRES_USER" ]; then
        echo "POSTGRES_APP_USER не должен совпадать с POSTGRES_USER. Исправьте .env вручную, затем повторите запуск." >&2
        exit 1
    fi

    POSTGRES_APP_USER=$existing_app
    POSTGRES_APP_PASSWORD=$existing_app_password
    echo "Параметры PostgreSQL уже заполнены — start.sh их не меняет. Для смены пароля используйте отдельную процедуру ALTER ROLE из README."
}

render_nginx_config() {
    output_file=$1
    domain=${2:-}
    webhook_port=${3:-}

    if [ -z "$domain" ]; then
        public_base_url=$(read_env_value PAYMENT_PUBLIC_BASE_URL)
        case "$public_base_url" in
            https://*) domain=${public_base_url#https://} ;;
            *)
                echo "PAYMENT_PUBLIC_BASE_URL должен содержать https://домен для генерации Nginx-конфигурации." >&2
                exit 1
                ;;
        esac
    fi
    if [ -z "$webhook_port" ]; then
        webhook_port=$(read_env_value PAYMENT_WEBHOOK_PORT)
    fi

    if ! is_safe_domain "$domain"; then
        echo "Некорректный домен для Nginx-конфигурации." >&2
        exit 1
    fi
    case "$webhook_port" in
        *[!0-9]*|'')
            echo "PAYMENT_WEBHOOK_PORT должен быть числом от 1024 до 65535, кроме 80 и 443." >&2
            exit 1
            ;;
        80|443)
            echo "Порты 80 и 443 принадлежат Nginx." >&2
            exit 1
            ;;
        *)
            if [ "$webhook_port" -lt 1024 ] || [ "$webhook_port" -gt 65535 ]; then
                echo "PAYMENT_WEBHOOK_PORT должен быть числом от 1024 до 65535, кроме 80 и 443." >&2
                exit 1
            fi
            ;;
    esac

    output_dir=$(dirname "$output_file")
    if [ ! -d "$output_dir" ]; then
        echo "Каталог для Nginx-конфигурации не существует: $output_dir" >&2
        exit 1
    fi

    temp_file=$(mktemp "$output_dir/.starsnetworks-nginx.XXXXXX")
    if ! sed \
        -e "s|__DOMAIN__|$domain|g" \
        -e "s|__PAYMENT_WEBHOOK_PORT__|$webhook_port|g" \
        "$APP_DIR/nginx.conf" > "$temp_file"; then
        rm -f "$temp_file"
        echo "Не удалось сгенерировать Nginx-конфигурацию." >&2
        exit 1
    fi
    if grep -q '__DOMAIN__\|__PAYMENT_WEBHOOK_PORT__' "$temp_file"; then
        rm -f "$temp_file"
        echo "В Nginx-конфигурации остались незаменённые параметры." >&2
        exit 1
    fi
    chmod 644 "$temp_file"
    mv "$temp_file" "$output_file"
    echo "Nginx-конфигурация создана: $output_file"
}

fill_env() {
    if [ ! -f "$ENV_FILE" ]; then
        umask 077
        : > "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "Создан локальный .env. Секреты останутся вне GitHub." \
            >&2
    fi
    if [ ! -t 0 ]; then
        echo "Для настройки нужен интерактивный терминал: ./start.sh" >&2
        exit 1
    fi
    ensure_openssl
    # Защищаем файл до первой записи секрета, а не только в конце настройки.
    chmod 600 "$ENV_FILE"

    echo "Настройка Stars Networks"
    echo "Скрипт заполняет существующий .env и не изменяет настройки взаимодействия, уведомлений и реквизиты платёжных систем."
    ask_bot_token
    BOT_TOKEN=$REPLY
    ask_bot_username
    BOT_USERNAME=$REPLY
    ask_telegram_id_list "Telegram ID администраторов через запятую (ADMIN_IDS)"
    ADMIN_IDS=$REPLY
    ask_telegram_id_list "Telegram ID разработчиков через запятую (DEVELOPER_IDS)" "$ADMIN_IDS"
    DEVELOPER_IDS=$REPLY
    existing_public_base_url=$(read_env_value PAYMENT_PUBLIC_BASE_URL)
    existing_domain=${existing_public_base_url#https://}
    if ! is_safe_domain "$existing_domain"; then
        existing_domain=stars.example.com
    fi
    ask_domain "$existing_domain"
    configure_database_values

    TELEGRAM_WEBHOOK_SECRET_TOKEN=$(read_env_value TELEGRAM_WEBHOOK_SECRET_TOKEN)
    case "$TELEGRAM_WEBHOOK_SECRET_TOKEN" in
        ''|replace-with-*)
            TELEGRAM_WEBHOOK_SECRET_TOKEN=$(generate_secret)
            ;;
    esac

    existing_port=$(read_env_value PAYMENT_WEBHOOK_PORT)
    case "$existing_port" in
        *[!0-9]*|''|80|443) existing_port=18743 ;;
    esac
    ask_port "$existing_port"
    existing_reservation=$(read_env_value ORDER_RESERVATION_MINUTES)
    case "$existing_reservation" in
        *[!0-9]*|'') existing_reservation=15 ;;
    esac
    ask_positive_integer "Время резерва заказа в минутах" "$existing_reservation"
    ORDER_RESERVATION_MINUTES=$REPLY

    set_env_value BOT_TOKEN "$BOT_TOKEN"
    set_env_value BOT_USERNAME "$BOT_USERNAME"
    set_env_value ADMIN_IDS "$ADMIN_IDS"
    set_env_value DEVELOPER_IDS "$DEVELOPER_IDS"
    set_env_value WEBHOOK_URL "https://$DOMAIN/webhook/telegram"
    set_env_value TELEGRAM_WEBHOOK_SECRET_TOKEN "$TELEGRAM_WEBHOOK_SECRET_TOKEN"
    set_env_value POSTGRES_DB "$POSTGRES_DB"
    set_env_value POSTGRES_USER "$POSTGRES_USER"
    set_env_value POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
    set_env_value POSTGRES_APP_USER "$POSTGRES_APP_USER"
    set_env_value POSTGRES_APP_PASSWORD "$POSTGRES_APP_PASSWORD"
    set_env_value DATABASE_URL "postgresql+asyncpg://${POSTGRES_APP_USER}:${POSTGRES_APP_PASSWORD}@db:5432/${POSTGRES_DB}"
    set_env_value PAYMENT_PUBLIC_BASE_URL "https://$DOMAIN"
    set_env_value PAYMENT_WEBHOOK_PORT "$PAYMENT_WEBHOOK_PORT"
    set_env_value ORDER_RESERVATION_MINUTES "$ORDER_RESERVATION_MINUTES"

    # Флаги оплаты всегда живут в .env. Переключение в админ-панели обновляет
    # именно эти строки; реквизиты остаются настройкой платёжных систем в БД.
    ensure_env_value PAYMENT_YOOKASSA_ENABLED false
    ensure_env_value PAYMENT_YOOMONEY_ENABLED false
    ensure_env_value PAYMENT_LAVA_ENABLED false
    ensure_env_value PAYMENT_HELEKET_ENABLED false
    ensure_env_value PAYMENT_CRYPTOBOT_ENABLED false
    ensure_env_value PAYMENT_STARS_ENABLED true
    chmod 600 "$ENV_FILE"
    protect_runtime_env_from_git
    render_nginx_config "$APP_DIR/nginx.generated.conf" "$DOMAIN" "$PAYMENT_WEBHOOK_PORT"

    echo
    echo ".env заполнен. Взаимодействие, уведомления и реквизиты платёжных систем настраиваются в пункте управления."
    echo "Создан nginx.generated.conf с выбранными доменом и внутренним портом. Установите его в Nginx по README."
}

if [ "${1:-}" = "--container" ]; then
    run_container "$@"
fi

if [ "${1:-}" = "--render-nginx" ]; then
    if [ "$#" -gt 2 ]; then
        echo "Использование: ./start.sh --render-nginx [путь-к-конфигурации]" >&2
        exit 2
    fi
    render_nginx_config "${2:-$APP_DIR/nginx.generated.conf}"
    exit
fi

if [ "$#" -gt 0 ]; then
    echo "Использование: ./start.sh" >&2
    exit 2
fi

fill_env

printf '%s\n' \
    '' \
    'Следующие команды выполните на сервере из папки проекта:' \
    '' \
    '  docker compose up -d --build' \
    '  docker compose ps' \
    '  docker compose logs -f bot' \
    '' \
    'Сначала установите сгенерированный nginx.generated.conf и HTTPS-сертификат по README.' \
    'После запуска укажите URL webhook в настройках платёжных провайдеров при их подключении.'
