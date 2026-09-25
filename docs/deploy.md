# Развёртывание

Три способа поднять бота. Во всех PostgreSQL, Redis, миграции, бот и catalog-worker
работают в Docker; различается только то, как MAX доставляет обновления.

| Вариант | Когда | Файлы compose |
| --- | --- | --- |
| A. Long polling | Локально или на сервере без домена | `docker-compose.yml` + `docker-compose.polling.yml` |
| B. Webhook, всё в Docker | Чистый сервер с доменом, порты 80/443 свободны | `docker-compose.yml` + `docker-compose.webhook.yml` |
| C. Webhook, nginx на хосте | На сервере уже есть nginx (текущий прод) | `docker-compose.yml` + `deploy/host-nginx/dvizhmax.conf` |

Проверено 2026-09-25 на Ubuntu x86_64, Docker Compose v5: чистая сборка образов около 40 секунд,
миграции до `0007_match_contacts`, бот стартует в обоих режимах; в варианте B запрос
`POST https://DOMAIN/max/webhook` через nginx доходит до бота (без секрета — 403, с секретом — 200).

## Общее для всех вариантов

1. Установить Docker Engine и Docker Compose v2.24+ (нужен для `!reset` в override-файлах).
2. Получить код и создать `.env`:

   ```bash
   git clone https://github.com/akimjlord-p/dvizhmax.git
   cd dvizhmax
   cp .env.example .env
   ```

3. Заполнить в `.env`:

   | Переменная | Значение |
   | --- | --- |
   | `POSTGRES_PASSWORD` | Любой длинный пароль |
   | `DATABASE_URL` | `postgresql+psycopg://dvizhmax:ТОТ_ЖЕ_ПАРОЛЬ@postgres:5432/dvizhmax` |
   | `MAX_BOT_TOKEN` | Токен бота из MAX для партнёров |
   | `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Доступ к Yandex AI Studio; без них события не размечаются и не попадают в ленту |
   | `MAX_WEBHOOK_SECRET` | Только для webhook: 5–256 символов, латиница, цифры, `-`, `_` |

4. После первого запуска подождать импорт каталога (несколько минут), затем по желанию:

   ```bash
   docker compose --profile tools run --rm demo-seed
   ```

## A. Long polling

MAX не отдаёт обновления через polling, пока у бота есть webhook-подписка. Для локальной
разработки используйте отдельного тестового бота. Если запустить polling с токеном
боевого бота, в логе будет `БОТ ИГНОРИРУЕТ POLLING! Обнаружены установленные подписки`.

```bash
docker compose -f docker-compose.yml -f docker-compose.polling.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.polling.yml logs -f bot
```

Ожидаемо в логе бота: `Бот: @имя_бота` и `Зарегистрировано 11 обработчиков событий`.
Написать боту `/start` — ответ приходит сразу.

Без Docker (нужны свои PostgreSQL и, по желанию, Redis):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
python -m alembic upgrade head
MAX_TRANSPORT=long_polling python -m bot.main
python -m workers.catalog_worker        # в отдельном терминале
```

## B. Webhook, nginx и Let's Encrypt в Docker

Нужно: домен с A-записью на IP сервера, свободные порты 80 и 443.

1. Дописать в `.env`:

   ```env
   DOMAIN=bot.example.com
   LETSENCRYPT_EMAIL=you@example.com
   MAX_WEBHOOK_SECRET=...
   ```

   `MAX_WEBHOOK_URL` задавать не нужно: он собирается как `https://DOMAIN/max/webhook`.

2. Один раз получить сертификат и запустить всё:

   ```bash
   ./deploy/init-letsencrypt.sh
   ```

   Скрипт создаёт временный сертификат, чтобы стартовал nginx, получает настоящий
   через HTTP-01 (webroot), перезагружает nginx и поднимает остальные сервисы.
   Для пробного прогона без лимитов Let's Encrypt: `STAGING=1 ./deploy/init-letsencrypt.sh`
   (сертификат будет недоверенным, потом запустить без `STAGING`).

3. Проверить:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.webhook.yml ps
   docker compose -f docker-compose.yml -f docker-compose.webhook.yml logs bot | grep Webhook
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://bot.example.com/max/webhook
   ```

   Ожидаемо: `Webhook сервер запущен на http://0.0.0.0:8080/max/webhook` и код `403`
   (бот отклоняет запрос без секрета, значит, nginx его проксирует). При старте бот сам
   подписывается на `https://DOMAIN/max/webhook`.

Сертификат продлевается контейнером `certbot` (проверка каждые 12 часов), nginx
перечитывает его каждые 6 часов. Конфиг nginx — `deploy/nginx/templates/dvizhmax.conf.template`.

Обычная работа:

```bash
alias dc='docker compose -f docker-compose.yml -f docker-compose.webhook.yml'
dc up -d --build        # запуск / применение изменений
dc logs -f bot          # логи
dc restart bot          # перезапуск бота
dc down                 # остановка; данные и сертификаты остаются в volumes
```

## C. Webhook, nginx на хосте (текущий прод)

Бот публикует `127.0.0.1:8080`, nginx хоста проксирует на него `/max/webhook`.

1. В `.env`:

   ```env
   MAX_TRANSPORT=webhook
   MAX_WEBHOOK_URL=https://bot.example.com/max/webhook
   MAX_WEBHOOK_SECRET=...
   ```

2. Настроить nginx и сертификат:

   ```bash
   sudo cp deploy/host-nginx/dvizhmax.conf /etc/nginx/sites-available/dvizhmax.conf
   sudo sed -i 's/bot.example.com/ВАШ_ДОМЕН/g' /etc/nginx/sites-available/dvizhmax.conf
   sudo ln -s /etc/nginx/sites-available/dvizhmax.conf /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d ВАШ_ДОМЕН
   ```

   Если у домена уже есть сайт, достаточно добавить в него блок `location = /max/webhook`
   из `deploy/host-nginx/dvizhmax.conf`.

3. Запустить:

   ```bash
   docker compose up -d --build
   ```

Обновление кода:

```bash
git pull --ff-only
docker compose build bot catalog-worker migrate
docker compose --profile tools build
docker compose run --rm migrate
docker compose up -d --no-deps --force-recreate bot catalog-worker
```

`migrate` и инструменты собираются в свои образы: без `build` миграция запустится на старом коде.

## Если что-то не работает

| Симптом | Причина |
| --- | --- |
| Polling: бот не отвечает, в логе `БОТ ИГНОРИРУЕТ POLLING` | У бота webhook-подписка; нужен другой токен |
| Webhook: `curl` на `/max/webhook` отдаёт 502 | Бот не запущен или слушает другой порт (`MAX_WEBHOOK_PORT`) |
| Webhook: 404 от nginx | Путь в nginx не совпадает с `MAX_WEBHOOK_PATH` |
| Лента пустая | Каталог ещё импортируется или не заданы ключи Yandex (события без разметки не показываются) |
| `init-letsencrypt.sh`: certbot не проходит проверку | A-запись домена не указывает на сервер или порт 80 закрыт |
