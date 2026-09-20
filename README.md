# ДвижМАКС: каталог и слой хранения

Модели каталога, пользователей, поиска компании, уведомлений и миграции PostgreSQL. Каталог получает
карточки KudaGo, сохраняет их в PostgreSQL и размечает новые события через Yandex AI Studio.

## Структура

- `infrastructure/db/base.py` — declarative base и общие поля.
- `infrastructure/db/models.py` — девять моделей каталога.
- `infrastructure/db/social_models.py` — одиннадцать моделей пользователей, поиска компании и уведомлений.
- `infrastructure/db/session.py` — асинхронный engine и фабрика сессий.
- `infrastructure/db/repositories/catalog.py` — сохранение и обновление каталога.
- `infrastructure/db/migrations/` — независимые миграции Alembic.
- `workers/catalog_worker.py` — фоновый импорт KudaGo и AI-разметка.
- [Полный отчёт по схеме](docs/social_schema_report.md).
- [Все столбцы и ограничения](docs/schema_reference.md).
- [Словарь тегов событий](docs/tag_dictionary.md).

Проверки без БД: `python -m unittest discover -s tests -v`.
Миграция `0002_social` следует за `0001_catalog`, а `0003_multiple_primary_tags`
снимает ограничение на один первичный тег у события. Откат `0002_social` к
`0001_catalog` удаляет только пользовательские и социальные таблицы.

## Получение событий KudaGo

`integrations/kudago.py` содержит асинхронный `KudaGoClient` и чистый
`normalize_event`. Клиент постранично следует по `next` ссылкам и отдаёт
сырые карточки по одной. Нормализатор возвращает `EventDraft` с данными для
`events`, `event_sources`, `places`, `event_images` и `event_schedules`.
Он не пишет в БД напрямую: это делает `catalog-worker` через repository.

Пример:

```python
from integrations.kudago import KudaGoClient, normalize_event

async with KudaGoClient() as client:
    async for payload in client.iter_events(location="msk"):
        draft = normalize_event(payload, city_id=city_id)
        await repository.upsert_event(draft)
```

Клиент использует максимум 100 карточек на страницу, повторяет временные
ошибки API, не следует по ссылкам на другой домен и не загружает весь каталог
в память. `normalize_event` не придумывает цену, возраст или место: неизвестные
значения остаются `None`, а полный ответ источника сохраняется в `raw_payload`.

Модели описывают хранение, repository выполняет запросы, а worker управляет импортом.

## ИИ-разметка событий

`integrations/yandex_tagger.py` использует официальный Yandex AI Studio SDK и
YandexGPT Lite 5 для разметки новых карточек. Модель получает название,
описание и исходные категории KudaGo и возвращает JSON с кодами из
фиксированного словаря. Ответ проверяется до сохранения через repository:
неизвестные коды и неправильный формат отбрасываются ошибкой.

Для запуска укажите в `.env` `YANDEX_API_KEY` и `YANDEX_FOLDER_ID`. Вместо
идентификатора каталога можно задать полный `YANDEX_MODEL_URI`.

```python
from integrations import KudaGoClient, YandexTagger, normalize_event

async with KudaGoClient() as kudago, YandexTagger() as tagger:
    async for payload in kudago.iter_events(location="msk"):
        draft = normalize_event(payload, city_id=city_id)
        tags = await tagger.tag_event(draft)
        await repository.upsert_event(draft, tags=tags)
```

## Подготовка и миграции (PowerShell)

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@localhost:5432/dvizhmax'
.\.venv\Scripts\python.exe -m alembic upgrade head
```

База должна уже существовать. Секреты не записываются в alembic.ini.
Для просмотра SQL без подключения: `python -m alembic upgrade head --sql`.
Откат начальной миграции удаляет таблицы и данные: `python -m alembic downgrade base`.

## Catalog worker

Контейнер называется `catalog-worker` и запускает модуль:

```text
python -m workers.catalog_worker
```

После старта он сразу обновляет KudaGo, затем повторяет цикл через 12 часов.
Интервал задаётся переменной `CATALOG_REFRESH_INTERVAL_SECONDS`; стандартное
значение `43200`. Падение или отсутствие Yandex не прерывает обновление
каталога: новые события остаются с `tagging_status=pending` и будут размечены
при следующем цикле.

Города задаются в `KUDAGO_CITIES` как JSON-массив. Worker импортирует их
последовательно, поэтому ошибка одного города не останавливает остальные:

```env
KUDAGO_CITIES=[{"location":"msk","name":"Москва","timezone":"Europe/Moscow"},{"location":"spb","name":"Санкт-Петербург","timezone":"Europe/Moscow"}]
```

Собрать образ:

```powershell
docker build -f Dockerfile.catalog-worker -t dvizhmax-catalog-worker .
```

При запуске через Compose сервис должен называться `catalog-worker`, а его
конфигурация должна содержать `restart: unless-stopped`; команда уже задана в
образе. Worker и бот используют одну PostgreSQL-базу;
в Docker-сети укажите в `DATABASE_URL` хост `postgres`.

```yaml
catalog-worker:
  build:
    context: .
    dockerfile: Dockerfile.catalog-worker
  env_file: .env
  restart: unless-stopped
```

## Правила импорта

- ИИ вызывается для новой карточки; при технической ошибке она остаётся `pending`/`failed` и повторяется, новые теги не меняют старые карточки.
- Обновляются цена, бесплатность, доступность, дата, время и место; текст и теги сохраняются.
- Недоступность/сомнения показываются плашкой, а не утверждением об отмене.
- Бесплатность хранится в `is_free`, не назначается ИИ как тег.
- Основной источник задаётся явно. БД допускает не более одного, сервис обеспечивает его наличие.
- У события бывает не больше двух первичных тегов: например, `board_games` и `party` одновременно.
- ИИ получает только теги из фиксированного словаря; новые теги добавляются для новых карточек и не меняют старую разметку.
- Фото хранится по URL с атрибуцией и необязательным кешем вложения MAX; при ошибке отправляется текст.
- UUID создаются SQLAlchemy на стороне приложения; при прямом SQL их нужно передавать явно.
- `updated_at` обновляется SQLAlchemy при UPDATE, это не PostgreSQL-триггер.
- JSONB изменяется присваиванием нового значения (вложенные in-place изменения не отслеживаются).
- Удаление связанных записей ограничено FK: автоматического каскадного удаления каталога нет.
- Точное исполнение повторяющихся расписаний и часовых поясов реализуется сервисом.
