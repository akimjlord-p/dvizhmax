# ДвижМАКС

Бот в MAX для поиска мероприятий и компании на них. Он получает события из KudaGo,
размечает их через Yandex AI Studio и подбирает по интересам пользователя.

## Возможности

- Онбординг: согласие на обработку данных, город, интересы, гостевой режим или анкета.
- Редактирование анкеты; фото и описание необязательны.
- Лента рекомендаций с фотографиями и inline-кнопками.
- Понравившиеся события и планы: листание, снятие лайка, отмена похода.
- Поиск компании на конкретное событие, лайки людей и сохранение взаимных совпадений.
- Импорт нескольких городов, обновление каталога каждые 12 часов и ИИ-разметка новых событий.

| Команда | Раздел |
| --- | --- |
| `/start` | Онбординг |
| `/feed` | Афиша |
| `/liked` | Понравившиеся |
| `/plans` | Мои планы и поиск компании |
| `/profile` | Анкета |

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
| `KUDAGO_CITIES` | JSON-массив городов |
| `CATALOG_REFRESH_INTERVAL_SECONDS` | Интервал обновления; `43200` |
| `LOG_LEVEL` | Уровень логирования; `INFO` |

Пример нескольких городов:

```env
KUDAGO_CITIES=[{"location":"msk","name":"Москва","timezone":"Europe/Moscow"},{"location":"spb","name":"Санкт-Петербург","timezone":"Europe/Moscow"}]
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

Для бота и каталога подготовлены отдельные Dockerfile:

```bash
docker build -f Dockerfile.bot -t dvizhmax-bot .
docker build -f Dockerfile.catalog-worker -t dvizhmax-catalog-worker .
```

Оба контейнера используют общую PostgreSQL и переменные из `.env`.
Бот слушает порт `8080`; HTTPS-прокси должен направлять webhook на этот порт.

## TODO

- Отправка контактов обоим пользователям при мэтче.
- Worker уведомлений о входящих лайках с группировкой от трёх человек.
- Проверка исчезнувших событий и обновление статуса доступности источника.
- Работа с повторяющимися событиями, выбором сеанса и совместимостью дат участников.
- Отображение прошедших и изменившихся событий в сохранённых планах.
