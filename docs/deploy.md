# Развёртывание

Все процессы — бот, миграции, catalog-worker и служебные инструменты — работают из
**одного Docker-образа** `dvizhmax-app` и отличаются только командой запуска. Одна команда
`docker compose up -d --build` собирает образ, применяет миграции и перезапускает бота
и worker. PostgreSQL и Redis тоже в Docker.

Способ доставки обновлений от MAX выбирается одним из трёх вариантов:

| Вариант | Когда | Команда запуска |
| --- | --- | --- |
| A. Long polling | Локально или на сервере без домена | `docker compose -f docker-compose.yml -f docker-compose.polling.yml up -d --build` |
| B. Webhook + Caddy | Чистый сервер с доменом, порты 80/443 свободны | `docker compose -f docker-compose.yml -f docker-compose.webhook.yml up -d --build` |
| C. Webhook + nginx на хосте | На сервере уже есть nginx (текущий прод) | `docker compose up -d --build` |

Проверено 2026-09-25 на Ubuntu x86_64, Docker Compose v5, в изолированном проекте:
образ собирается один раз (~40 с с нуля), миграции доходят до `0007_match_contacts`, бот
стартует от непривилегированного пользователя `app` во всех трёх режимах, инструменты
запускаются на том же образе. В варианте B `POST https://DOMAIN/max/webhook` через Caddy
доходит до бота: без секрета — 403, с секретом — 200; HTTP перенаправляется на HTTPS.

## Шаг 1. Общее для всех вариантов

1. Установить Docker Engine и Docker Compose v2.24 или новее.
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
   | `MAX_BOT_TOKEN` | Токен бота из кабинета MAX для партнёров |
   | `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Yandex AI Studio; без них события не размечаются и не попадают в ленту |
   | `MAX_WEBHOOK_SECRET` | Для вариантов B и C: 5–256 символов, латиница, цифры, `-`, `_` |

## Шаг 2. Выбрать вариант

### A. Long polling

MAX не отдаёт обновления через polling, пока у бота есть webhook-подписка. Для разработки
используйте отдельного тестового бота. С токеном боевого бота в логе будет
`БОТ ИГНОРИРУЕТ POLLING! Обнаружены установленные подписки`.

```bash
docker compose -f docker-compose.yml -f docker-compose.polling.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.polling.yml logs -f bot
```

Ожидаемо в логе: `Бот: @имя_бота` и `Зарегистрировано 11 обработчиков событий`.
Написать боту `/start` — ответ приходит сразу.

### B. Webhook + Caddy (всё в Docker)

Нужно: домен с A-записью на IP сервера, свободные порты 80 и 443.

1. Дописать в `.env` домен:

   ```env
   DOMAIN=bot.example.com
   ```

   `MAX_WEBHOOK_URL` задавать не нужно: он собирается как `https://DOMAIN/max/webhook`.

2. Запустить:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.webhook.yml up -d --build
   ```

   Caddy сам получает сертификат Let's Encrypt при первом запросе к домену, продлевает его
   и перенаправляет HTTP на HTTPS. Сертификаты хранятся в томе `caddy_data`. Бот при старте
   сам подписывается на `https://DOMAIN/max/webhook`. Конфиг прокси — `deploy/caddy/Caddyfile`.

3. Проверить:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://bot.example.com/max/webhook
   ```

   Ожидаемо `403`: бот отклоняет запрос без секрета, значит, Caddy до него проксирует.

Чтобы не писать `-f` каждый раз:

```bash
alias dc='docker compose -f docker-compose.yml -f docker-compose.webhook.yml'
```

### C. Webhook + nginx на хосте (текущий прод)

Бот публикует `127.0.0.1:8080`, nginx хоста проксирует на него `/max/webhook`.

1. Дописать в `.env`:

   ```env
   MAX_TRANSPORT=webhook
   MAX_WEBHOOK_URL=https://bot.example.com/max/webhook
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

## Шаг 3. Данные для демо

Первый импорт каталога начинается сразу после старта catalog-worker и занимает несколько
минут. Потом:

```bash
docker compose --profile tools run --rm demo-seed    # демо-событие и демо-анкеты
```

Для вариантов A и B добавляйте те же `-f`, что при запуске.

## Повседневные команды

Показаны для варианта C; для A и B добавьте свои `-f`.

```bash
docker compose ps                                        # статус
docker compose logs -f bot                               # логи бота
docker compose restart bot                               # перезапуск бота
docker compose down                                      # остановка; данные остаются в томах
docker compose --profile tools run --rm demo-reset       # сбросить демо-событие
docker compose --profile tools run --rm profile-reset    # удалить все реальные профили
```

Обновление кода:

```bash
git pull --ff-only
docker compose up -d --build
```

Эта команда пересобирает образ, применяет новые миграции (сервис `migrate` запускается
до бота) и пересоздаёт бота и worker. Инструменты при следующем запуске берут тот же
новый образ.

## Если что-то не работает

| Симптом | Причина |
| --- | --- |
| Polling: бот не отвечает, в логе `БОТ ИГНОРИРУЕТ POLLING` | У бота webhook-подписка; нужен другой токен |
| Webhook: на `/max/webhook` 502 | Бот не запущен или слушает другой порт (`MAX_WEBHOOK_PORT`) |
| Webhook: 404 на `/max/webhook` | Путь в прокси не совпадает с `MAX_WEBHOOK_PATH` |
| Caddy: нет сертификата | A-запись домена не указывает на сервер или закрыт порт 80/443; смотреть `logs caddy` |
| `No such image: dvizhmax-app` | Инструмент запущен до первой сборки; сначала `up -d --build` |
| Лента пустая | Каталог ещё импортируется или не заданы ключи Yandex |
