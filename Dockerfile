# One image for every application process: the bot, migrations, the catalog
# worker and the maintenance tools differ only by their command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt -c constraints.txt

COPY alembic.ini ./
COPY assets ./assets
COPY infrastructure ./infrastructure
COPY integrations ./integrations
COPY workers ./workers
COPY bot ./bot

RUN useradd --system --no-create-home app
USER app

EXPOSE 8080
CMD ["python", "-m", "bot.main"]
