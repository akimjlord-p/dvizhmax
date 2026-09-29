# ДвижМАКС

Чат-бот для мессенджера MAX, трек «Досуг и развлечения». Показывает афишу Москвы из KudaGo
с рекомендациями по интересам и помогает найти компанию на событие: пользователи,
запланировавшие одно событие, видят анкеты друг друга, взаимный лайк создаёт мэтч, после
которого участники могут обменяться контактами.

Рабочий бот: [@t110_hakaton_max_bot](https://max.ru/t110_hakaton_max_bot) (мобильный MAX и web.max.ru).

Стек: Python 3.12, maxapi, PostgreSQL 17, Redis 7, SQLAlchemy 2 (async) + psycopg 3, Alembic,
Docker Compose. События размечаются тегами через YandexGPT.

## Быстрый старт

Требования: Docker с Compose v2, свободный порт `127.0.0.1:8080`, исходящий доступ в интернет,
токен бота MAX, API-ключ и ID каталога Yandex AI Studio.

```bash
git clone https://github.com/akimjlord-p/dvizhmax.git && cd dvizhmax
cp .env.example .env    # заполнить MAX_BOT_TOKEN, YANDEX_API_KEY, YANDEX_FOLDER_ID
docker compose up -d --build
```

Compose собирает образ, поднимает PostgreSQL и Redis, применяет миграции (`migrate`) и запускает
`bot` и `catalog-worker`. Бот получает обновления через long polling, публичный адрес не нужен.
Демо-данные создаются при старте воркера, первый импорт афиши Москвы занимает несколько минут.

MAX не отдаёт обновления через long polling боту с активной webhook-подпиской: в логе `bot`
будет `БОТ ИГНОРИРУЕТ POLLING`. Рабочий бот подписан на webhook сервера команды, поэтому для
локального запуска нужен токен другого бота.

### Остановка и повторный запуск

```bash
docker compose ps                          # состояние сервисов
docker compose logs -f bot catalog-worker  # логи
docker compose down                        # остановить; данные остаются в томах postgres_data и redis_data
docker compose up -d                       # запустить снова
git pull && docker compose up -d --build   # обновить; migrate применит новые миграции
docker compose down -v                     # остановить и удалить данные
```

## Деплой с webhook

Контейнер `bot` публикует порт 8080 на `127.0.0.1`; HTTPS завершает nginx на хосте.

1. Дописать в `.env`. Пароль БД задаётся до первого запуска: потом он уже записан в том.

   ```env
   MAX_TRANSPORT=webhook
   MAX_WEBHOOK_URL=https://bot.example.com/max/webhook
   MAX_WEBHOOK_SECRET=<openssl rand -hex 32>
   POSTGRES_PASSWORD=<пароль>
   ```

2. Подключить сайт nginx и выпустить сертификат:

   ```bash
   sudo cp deploy/nginx.conf /etc/nginx/sites-available/dvizhmax.conf
   sudo sed -i 's/bot.example.com/<домен>/' /etc/nginx/sites-available/dvizhmax.conf
   sudo ln -s /etc/nginx/sites-available/dvizhmax.conf /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d <домен>
   ```

3. `docker compose up -d --build`. При старте бот подписывается на `MAX_WEBHOOK_URL`, если
   подписки ещё нет. `curl -i -X POST https://<домен>/max/webhook` должен вернуть `403`.

## Состав и архитектура

```text
               MAX Bot API
                 ▲     │ обновления: long polling или webhook
                 │     ▼
┌───────┐     ┌─────┐     ┌──────────┐     ┌────────────────┐ ──▶ KudaGo API
│ redis │ ◀── │ bot │ ──▶ │ postgres │ ◀── │ catalog-worker │
└───────┘     └─────┘     └──────────┘     └────────────────┘ ──▶ Yandex AI Studio
```

| Сервис | Запуск | Роль |
| --- | --- | --- |
| `postgres` | `postgres:17-alpine` | Основное хранилище |
| `redis` | `redis:7-alpine` | Очередь карточек ленты каждого пользователя |
| `migrate` | `alembic upgrade head` | Применяет миграции и завершается; `bot` и `catalog-worker` стартуют после него |
| `bot` | `python -m bot.main` | Обработчики MAX. Фоновая задача раз в минуту рассылает сводки лайков и повторяет недоставленные уведомления о мэтчах |
| `catalog-worker` | `python -m workers.catalog_worker` | Цикл раз в 12 часов: демо-данные, импорт KudaGo, разметка новых событий |
| `demo-reset`, `profile-reset` | `workers/reset_*.py` | Служебные команды, запускаются вручную |

`bot` и `catalog-worker` друг к другу не обращаются, общее состояние только в PostgreSQL.
Все процессы приложения работают из одного образа `dvizhmax-app` (`Dockerfile`), собирает его
сервис `bot`.

```text
bot/
  main.py            запуск: long polling или webhook, регистрация обработчиков
  onboarding.py      согласие, анкета, интересы, профиль, удаление аккаунта
  feed.py            лента, понравившиеся, планы, поиск компании, /demo
  contacts.py        обмен контактами после мэтча
  notifications.py   сводки лайков, уведомления о мэтчах, повтор доставки
  navigation.py      меню, обработка ошибок, неизвестные команды
infrastructure/
  db/models.py, db/social_models.py   модели SQLAlchemy
  db/repositories/                    запросы и правила; UserError — ошибка, текст которой видит пользователь
  db/migrations/                      миграции Alembic
  cache/feed_buffer.py                очередь ленты в Redis
integrations/
  kudago.py          клиент KudaGo API и нормализация событий
  yandex_tagger.py   разметка событий через YandexGPT
workers/             catalog_worker.py, demo_seed.py, reset_demo.py, reset_profiles.py
deploy/nginx.conf    сайт nginx для webhook
docs/                user_flow.md, data_model.md, catalog_update.md
tests/               unittest
```

## Флоу пользователя

| Шаг | Код | Таблицы |
| --- | --- | --- |
| `/start` → согласие на обработку данных → город | `bot/onboarding.py` | `users`, `user_consents` |
| Анкета: имя, пол, возраст 18+, описание и фото (необязательны), от 3 интересов. Либо «Только афиша» без анкеты | `bot/onboarding.py` | `users`, `user_tag_weights` |
| Лента по одному событию: «Нравится», «Не моё», «Хочу пойти» | `bot/feed.py` | `event_reactions`, `event_plans`, `user_tag_weights`, Redis |
| «Найти компанию» на запланированном событии: анкеты других людей с планом на него | `bot/feed.py` | `companion_views` |
| «Пойти вместе» — лайк; встречный лайк — мэтч и уведомление обоим | `bot/feed.py`, `bot/notifications.py` | `companion_interests`, `matches` |
| После мэтча пользователь пишет контакт, подтверждает, бот пересылает его второму участнику | `bot/contacts.py` | `match_contacts` |

Команды: `/start`, `/feed`, `/liked`, `/plans`, `/profile`, `/demo`. Тексты и кнопки каждого
шага — в `docs/user_flow.md`.

## Переменные окружения

Шаблон — `.env.example`. `DATABASE_URL` и `REDIS_URL` задаёт `docker-compose.yml`, значения
из `environment` важнее `.env`; сами они нужны только при запуске без Docker.

| Переменная | Обязательна | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `MAX_BOT_TOKEN` | да | — | Токен бота MAX |
| `YANDEX_API_KEY` | для ленты | — | Ключ Yandex AI Studio. Без него события импортируются, но не размечаются и в ленту не попадают |
| `YANDEX_FOLDER_ID` | для ленты | — | ID каталога Yandex Cloud |
| `YANDEX_MODEL_URI` | нет | `gpt://<YANDEX_FOLDER_ID>/yandexgpt-5-lite` | Другая модель |
| `MAX_TRANSPORT` | нет | `long_polling` | `long_polling` или `webhook` |
| `MAX_WEBHOOK_URL` | для webhook | — | Публичный HTTPS-адрес webhook |
| `MAX_WEBHOOK_SECRET` | для webhook | — | Сверяется с заголовком `X-Max-Bot-Api-Secret`; без него бот в режиме webhook не стартует |
| `MAX_WEBHOOK_HOST`, `MAX_WEBHOOK_PORT`, `MAX_WEBHOOK_PATH` | нет | `0.0.0.0`, `8080`, `/max/webhook` | Где слушает webhook-сервер в контейнере |
| `POSTGRES_PASSWORD` | нет | `dvizhmax` | Пароль БД, compose подставляет его в `DATABASE_URL` |
| `DATABASE_URL` | без Docker | задаёт compose | `postgresql+psycopg://…` |
| `REDIS_URL` | нет | задаёт compose | Без него очередь ленты хранится в памяти процесса |
| `KUDAGO_CITIES` | нет | Москва (`msk`) | JSON-массив `{"location", "name", "timezone"}` |
| `CATALOG_REFRESH_INTERVAL_SECONDS` | нет | `43200` | Период цикла `catalog-worker` |
| `PRIVACY_POLICY_VERSION` | нет | `2026-09-20` | Версия согласия; при смене бот запрашивает согласие заново |
| `LOG_LEVEL` | нет | `INFO` | Уровень логирования |

## Используемые порты

| Порт | Сервис | Публикация |
| --- | --- | --- |
| 8080/tcp | `bot` | `127.0.0.1:8080` на хосте для nginx; слушается только при `MAX_TRANSPORT=webhook` |
| 5432/tcp | `postgres` | Не публикуется, доступен только в сети compose |
| 6379/tcp | `redis` | Не публикуется |

Исходящие HTTPS-запросы: API MAX, KudaGo, Yandex AI Studio.

## Зависимости

`requirements.txt` — прямые зависимости с точными версиями: SQLAlchemy 2.0.54, alembic 1.20.0,
psycopg[binary] 3.3.6, httpx 0.28.1, python-dotenv 1.2.3, yandex-ai-studio-sdk 0.22.1,
maxapi 1.2.2, redis 5.3.1. `constraints.txt` фиксирует транзитивные зависимости; установка —
`pip install -r requirements.txt -c constraints.txt`. Образы: `python:3.12-slim`,
`postgres:17-alpine`, `redis:7-alpine`.

## Внешние сервисы и интеграции

| Сервис | Кто использует | Что нужно | При недоступности |
| --- | --- | --- | --- |
| MAX Bot API | `bot` | `MAX_BOT_TOKEN`; для webhook — домен с HTTPS | Бот не получает и не отправляет сообщения; уведомления о мэтчах досылаются повторно |
| KudaGo API v1.4 | `catalog-worker` | Ничего, API открытый | Импорт города завершается ошибкой, статусы событий не меняются, повтор в следующем цикле |
| Yandex AI Studio, YandexGPT 5 Lite | `catalog-worker` | `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Событие остаётся `pending` или получает `failed` и не попадает в ленту; разметка повторяется в следующем цикле |

Эти сервисы в Docker не воспроизводятся: для проверки нужны интернет, токен бота и ключ
Yandex AI Studio. Без ключа Yandex бот и демо работают, но лента пустая.

## Работа с данными

**PostgreSQL.** Схема меняется только миграциями Alembic (`infrastructure/db/migrations`),
21 таблица:

- каталог: `cities`, `events`, `event_sources`, `places`, `event_images`, `event_schedules`,
  `tags`, `event_tags`, `import_runs`;
- пользователи: `users`, `user_consents`, `user_tag_weights`, `event_reactions`, `event_plans`;
- поиск компании: `companion_views`, `companion_interests`, `matches`, `match_contacts`,
  `user_blocks`; `notifications` и `notification_interests` зарезервированы и не используются.

Поля и ограничения — `docs/data_model.md`.

**Redis** хранит только очередь ленты: `dvizhmax:feed:{user_id}:queue` (подготовленные карточки)
и `dvizhmax:feed:{user_id}:shown` (показанные без реакции), TTL 24 часа с последнего обращения.
Потеря Redis сбрасывает только очередь.

**Импорт афиши** (`docs/catalog_update.md`). KudaGo → `normalize_event` → upsert по
`(source, external_id)` → разметка новых событий YandexGPT по фиксированному словарю тегов
из `integrations/yandex_tagger.py`. В ленту попадает событие, которое размечено, имеет будущий
сеанс и возрастной порог не выше возраста пользователя. Детские события (категория KudaGo `kids`
или признаки в названии) исключены. Событие, пропавшее из выдачи KudaGo, получает статус
`uncertain` и показывается с предупреждением, через 3 дня — `unavailable` и из ленты убирается.

**Рекомендации** (`infrastructure/db/repositories/feed.py`). Оценка события — сумма весов его
тегов у пользователя, первичные теги с множителем 1.0, вторичные — 0.4. Вес тега: интерес из
анкеты 1.0, «Нравится» +0.1, «Хочу пойти» +0.5, «Не моё» 0. Каждая четвёртая карточка случайная;
три карточки подряд с общим первичным тегом не показываются.

**Персональные данные.** MAX ID, город, интересы, реакции и планы; при анкете — имя, пол, возраст,
описание, фото. Контакт сохраняется, только если пользователь сам написал и подтвердил его после
мэтча, и отправляется только второму участнику мэтча. «Мой профиль» → «Удалить профиль» удаляет
пользователя и все связанные записи.

## Тестовые данные

События — реальные данные KudaGo. Синтетические только данные демо-сценария. Их создаёт
`workers/demo_seed.py` в начале каждого цикла `catalog-worker`, повторный запуск ничего не дублирует.

| Объект | Идентификатор | Поведение |
| --- | --- | --- |
| «Демо-встреча ДвижМАКС» | `events.id = 8e066374-2932-5bf6-885c-125180b6ab5e` | Не показывается в ленте, открывается командой `/demo`; дата — через неделю от последнего цикла воркера |
| «Демо Лёша» | `max_user_id = 9000000003` | Первая анкета, общих интересов нет, лайк не взаимный |
| «Демо Саша» | `max_user_id = 9000000002` | При `/demo` получает первые три интереса пользователя, лайк не взаимный |
| «Демо Катя» | `max_user_id = 9000000001` | Уже лайкнула пользователя: ответный лайк сразу даёт мэтч; на контакт отвечает демо-контактом |

Демо-анкеты помечены «Демо-анкета», сообщения на их MAX ID не отправляются. После них показываются
реальные пользователи, которые сами включили поиск компании на демо-событии.

Сброс:

- «🔄 Сбросить демо» — просмотры, лайки, мэтчи и контакты текущего пользователя
  на демо-событии;
- `docker compose run --rm demo-reset` — то же для всех пользователей, затем демо создаётся заново;
- `docker compose run --rm profile-reset` — удаляет всех реальных пользователей, демо остаётся.

## Пошаговый сценарий проверки

| # | Действие | Ожидаемый результат |
| --- | --- | --- |
| 1 | `docker compose up -d --build`, затем `docker compose ps` | `migrate` — `Exited (0)`; `postgres`, `redis` — `healthy`; `bot`, `catalog-worker` — `Up` |
| 2 | `docker compose logs bot` | Строка `Бот: @<ник бота>`, без `Traceback` |
| 3 | `docker compose logs catalog-worker` после первого цикла | `Catalog refresh finished: created=… updated=… failed=0` |
| 4 | Боту: `/start` → «Согласен» → «Продолжить с Москвой» → «Создать профиль», заполнить анкету | «Профиль сохранён…» и меню |
| 5 | «Афиша» → «👍 Нравится» | Карточка помечена «✅ Нравится», пришла следующая; запись в `event_reactions` |
| 6 | `/demo` → «Хочу пойти» → «Найти компанию» | Анкета «Демо Лёша» |
| 7 | «👍 Пойти вместе» на Лёше, затем на Саше | «Лайк отправлен…»; у Саши строка «Общие интересы: …» |
| 8 | «👍 Пойти вместе» на Кате | «Есть мэтч 🎉»; запись в `matches` |
| 9 | «Поделиться контактом» → написать контакт → «Да, отправить» | «Контакт отправлен: Демо Катя», в ответ её демо-контакт; `match_contacts.status = 'sent'` |
| 10 | Два аккаунта MAX планируют одно событие, первый лайкает второго | Второму в течение минуты приходит сводка «…хочет пойти 1 человек»; ответный лайк — «Есть мэтч 🎉» обоим |

Состояние БД:

```bash
docker compose exec postgres psql -U dvizhmax -c "select tagging_status, count(*) from events group by 1"
docker compose exec postgres psql -U dvizhmax -c "select event_id, notify_attempts from matches"
```

## Примеры ожидаемого поведения

| Ситуация | Поведение |
| --- | --- |
| Повторное нажатие уже обработанной кнопки | Дублей в БД нет; на оценённой карточке ленты — «Эта карточка уже оценена» |
| Ошибка ввода: возраст меньше 18, текст вместо кнопки, неизвестная команда | Объяснение и актуальные кнопки, состояние не меняется |
| Исключение в обработчике | Трейс в логе `bot`; пользователю «Не получилось выполнить действие…» с кнопками «Повторить» и «В меню»; сохранённое не теряется |
| Картинка отправлена файлом на шаге фото | Просьба отправить её как фотографию или пропустить шаг |
| MAX не загрузил фото события по URL | Карточка отправляется с заглушкой `assets/images/event-no-image.jpg` |
| Уведомление о мэтче не доставлено | Повтор раз в минуту, до 10 попыток, только тому, кто не получил |
| Сбой KudaGo | Импорт города завершается ошибкой в `import_runs`, статусы событий не меняются |
| Сбой YandexGPT на событии | `tagging_status = failed`, повтор в следующем цикле |
| Webhook-запрос без верного `X-Max-Bot-Api-Secret` | `403`, обновление не обрабатывается |
| `MAX_TRANSPORT=webhook` без `MAX_WEBHOOK_SECRET` | `bot` не стартует с `RuntimeError` |
| Перезапуск контейнеров | Данные в томах сохраняются, `migrate` применяет только новые миграции |

## Известные ограничения

- Один город — Москва — и один источник событий — KudaGo.
- Без YandexGPT новые события не попадают в ленту.
- Long polling и webhook для одного бота взаимоисключающие.
- Фоновая рассылка идёт внутри процесса `bot`; несколько реплик `bot` не предусмотрены.
- Для обмена контактом принимается только публичная ссылка на профиль MAX в формате `https://max.ru/u/...`;
  @ник, телефон и карточка контакта не поддерживаются.
- Пока бот ждёт контакт (до часа), следующее сообщение пользователя считается контактом.
- Нет модерации анкет и фото, жалоб и блокировки в интерфейсе.
- При отмене похода второй участник мэтча не уведомляется.
- Карточки, показанные без реакции, исключаются из ленты на 24 часа.

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
python -m unittest discover -s tests
```

Тесты с PostgreSQL запускаются, если задан `TEST_DATABASE_URL`, иначе пропускаются. Имя базы
должно оканчиваться на `_test`: тесты применяют к ней миграции и откатывают каждую транзакцию.

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/dvizhmax_test python -m unittest discover -s tests
```

Без Docker нужны PostgreSQL и Redis; в `.env` задать `DATABASE_URL` и `REDIS_URL`, затем:

```bash
python -m alembic upgrade head
python -m workers.catalog_worker   # отдельный процесс
python -m bot.main
```
