# ДвижМАКС

Бот для MAX: афиша Москвы (KudaGo, теги от YandexGPT) и поиск компании на событие.
Взаимный интерес → мэтч → обмен контактом по согласию.

Бот: [@t110_hakaton_max_bot](https://max.ru/t110_hakaton_max_bot)

Команды: `/start`, `/feed`, `/liked`, `/plans`, `/profile`, `/demo`.

## Запуск

```bash
git clone https://github.com/akimjlord-p/dvizhmax.git && cd dvizhmax
cp .env.example .env   # заполнить токены и пароль, см. комментарии в файле
```

| Режим | Команда |
| --- | --- |
| Long polling | `docker compose -f docker-compose.yml -f docker-compose.polling.yml up -d --build` |
| Webhook + Caddy (нужен `DOMAIN`) | `docker compose -f docker-compose.yml -f docker-compose.webhook.yml up -d --build` |
| Webhook + nginx на хосте | `docker compose up -d --build`, конфиг — `deploy/host-nginx/dvizhmax.conf` |

Демо-данные: `docker compose --profile tools run --rm demo-seed`.
Обновление: `git pull && docker compose up -d --build`. Остановка: `docker compose down`.

Long polling не работает, пока у бота есть webhook, — нужен отдельный тестовый бот.

## Флоу пользователя

1. `/start`: согласие на обработку данных, анкета (18+) с интересами или режим «Только афиша».
2. Афиша: по одному событию, «Нравится» / «Не моё» / «Хочу пойти».
3. «Хочу пойти» → «Найти компанию»: анкеты тех, кто идёт на то же событие.
4. Лайк в ответ на лайк — мэтч, после него можно поделиться контактом.

`/demo` проводит по этому пути с тестовыми анкетами — мэтч с ними наступает сразу.

## Рекомендации

Интерес из анкеты даёт тегу вес 1.0. «Нравится» +0.1, «Хочу пойти» +0.5, «Не моё» 0.
Оценка события — сумма весов тегов (вторичные ×0.4). Каждая четвёртая карточка случайная.

## Разработка

```bash
pip install -r requirements.txt -c constraints.txt
python -m unittest discover -s tests
```

Для интеграционных тестов нужна отдельная база: `TEST_DATABASE_URL=...<имя>_test`.
Схема данных, путь пользователя и импорт описаны в `docs/`.

## Ограничения

- Только Москва и только KudaGo.
- Ссылки на профиль MAX не открываются, поэтому контакт передаётся вручную после мэтча.
- Нет модерации, жалоб и общего чата.
- Если отменить поход, второй участник мэтча об этом не узнает.
