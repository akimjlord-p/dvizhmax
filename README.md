# ДвижМАКС — бот для хакатона MAX

Бот в MAX для поиска мероприятий и компании на них. Он получает события из KudaGo,
размечает их через Yandex AI Studio и подбирает по интересам пользователя.

## Возможности

- Онбординг для Москвы: согласие на обработку данных, интересы, гостевой режим или анкета 18+.
- Редактирование анкеты; фото и описание необязательны.
- Лента рекомендаций с inline-кнопками: URL фотографий событий передаётся в MAX напрямую; для событий без фото есть встроенная заглушка.
- Понравившиеся события и планы: листание, снятие лайка, отмена похода.
- Поиск компании на конкретное событие, лайки людей и сохранение взаимных совпадений.
- При мэтче бот сразу отправляет обоим участникам ссылки на профили MAX; односторонние лайки группируются в уведомление раз в минуту.
- Импорт московской афиши KudaGo, обновление каталога каждые 12 часов и ИИ-разметка новых событий.
- В карточке показывается актуальный ближайший сеанс, период проведения или статус «идёт сейчас»; события с неподтверждёнными данными помечаются предупреждением.

| Команда | Раздел |
| --- | --- |
| `/start` | Онбординг |
| `/feed` | Афиша |
| `/liked` | Понравившиеся |
| `/plans` | Мои планы и поиск компании |
| `/profile` | Анкета |
| `/demo` | Тестовый путь до мэтча |

## Стек

Python 3.12, maxapi, SQLAlchemy 2 с AsyncSession, psycopg 3, PostgreSQL,
Alembic, httpx, Yandex AI Studio SDK, Docker.

## Конфигурация

Шаблон: [`.env.example`](.env.example). Бот и worker загружают `.env` при запуске.

| Переменная | Назначение / значение по умолчанию |
| --- | --- |
| `DATABASE_URL` | PostgreSQL: `postgresql+psycopg://USER:PASSWORD@HOST:5432/DB` |
| `MAX_BOT_TOKEN` | Токен бота MAX |
| `MAX_WEBHOOK_URL` | Публичный HTTPS-адрес webhook |
| `MAX_WEBHOOK_SECRET` | Секрет webhook |
| `MAX_WEBHOOK_HOST` | `0.0.0.0` |
| `MAX_WEBHOOK_PORT` | `8080` |
| `MAX_WEBHOOK_PATH` | `/max/webhook` |
| `YANDEX_API_KEY` | API-ключ Yandex AI Studio |
| `YANDEX_FOLDER_ID` | Идентификатор каталога Yandex Cloud |
| `YANDEX_MODEL_URI` | Необязательный URI модели |
| `KUDAGO_CITIES` | JSON-массив городов; в MVP — только Москва |
| `CATALOG_REFRESH_INTERVAL_SECONDS` | Интервал обновления; `43200` |
| `LOG_LEVEL` | Уровень логирования; `INFO` |

Конфигурация MVP:

```env
KUDAGO_CITIES=[{"location":"msk","name":"Москва","timezone":"Europe/Moscow"}]
```

## Структура проекта

```text
bot/                         Обработчики MAX, онбординг, лента и навигация
integrations/                Клиенты KudaGo и Yandex AI Studio
infrastructure/db/           Модели SQLAlchemy и подключение к БД
  repositories/              Запросы и изменения данных
  migrations/                Миграции Alembic
workers/catalog_worker.py    Импорт и разметка каталога
tests/                       Тесты
docs/                        Схема БД и словарь тегов
Dockerfile.bot               Образ бота
Dockerfile.catalog-worker    Образ импорта и разметки
```

## Разработка

```bash
python -m unittest discover -s tests -v
```

Для интеграционных тестов задайте `TEST_DATABASE_URL` с адресом отдельной
PostgreSQL-базы, имя которой заканчивается на `_test`. Без этой переменной
интеграционные тесты пропускаются.

Миграции:

```bash
python -m alembic upgrade head
python -m alembic revision --autogenerate -m "describe change"
python -m alembic check
```

## Деплой

`docker-compose.yml` поднимает отдельные контейнеры PostgreSQL, Redis, миграций, бота и обновления каталога. В продакшн-файле `.env` нужно задать `POSTGRES_PASSWORD`, `DATABASE_URL` с тем же паролем, токены и MAX webhook:

```env
MAX_TRANSPORT=webhook
MAX_WEBHOOK_URL=https://akimjlord.space/max/webhook
MAX_WEBHOOK_PATH=/max/webhook
MAX_WEBHOOK_PORT=8080
```

Бот публикует только `127.0.0.1:8080`; Nginx должен проксировать `/max/webhook` на этот адрес. Запуск:

```bash
docker compose up -d --build
```

Для тестового сценария до мэтча после первой загрузки каталога:

```bash
docker compose --profile tools run --rm demo-seed
```

Сброс демо-события: удаляются планы, реакции, лайки, показы и мэтчи всех
пользователей на нём, затем демо-анкеты и профили из `DEMO_TEAM_MAX_USER_IDS`
снова ищут компанию:

```bash
docker compose --profile tools run --rm demo-reset
```
