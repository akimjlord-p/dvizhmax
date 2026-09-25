#!/usr/bin/env bash
# Obtain the first Let's Encrypt certificate for the containerised nginx and start the stack.
# Run once from anywhere: ./deploy/init-letsencrypt.sh
# STAGING=1 uses the Let's Encrypt test CA (no rate limits, untrusted certificate).
set -euo pipefail

cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.yml -f docker-compose.webhook.yml)

env_value() {
    grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

domain="$(env_value DOMAIN)"
email="$(env_value LETSENCRYPT_EMAIL)"
if [[ -z "$domain" || -z "$email" ]]; then
    echo "Set DOMAIN and LETSENCRYPT_EMAIL in .env" >&2
    exit 1
fi
live="/etc/letsencrypt/live/$domain"

if "${compose[@]}" run --rm --entrypoint sh certbot -c "test -f $live/fullchain.pem && test -f /etc/letsencrypt/renewal/$domain.conf" 2>/dev/null; then
    echo "Certificate for $domain already exists; starting the stack."
    "${compose[@]}" up -d --build
    exit 0
fi

echo "### Creating a temporary self-signed certificate so nginx can start"
"${compose[@]}" run --rm --entrypoint sh certbot -c \
    "mkdir -p $live && openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
     -keyout $live/privkey.pem -out $live/fullchain.pem -subj /CN=localhost"

echo "### Building images and starting nginx, the bot and their dependencies"
"${compose[@]}" up -d --build nginx

echo "### Replacing it with a Let's Encrypt certificate"
"${compose[@]}" run --rm --entrypoint sh certbot -c \
    "rm -rf /etc/letsencrypt/live/$domain /etc/letsencrypt/archive/$domain /etc/letsencrypt/renewal/$domain.conf"
staging=()
if [[ "${STAGING:-0}" == "1" ]]; then
    staging=(--staging)
fi
"${compose[@]}" run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot -d "$domain" \
    --email "$email" --agree-tos --no-eff-email --rsa-key-size 2048 "${staging[@]}"

echo "### Reloading nginx and starting the rest of the stack"
"${compose[@]}" exec nginx nginx -s reload
"${compose[@]}" up -d
echo "Done: https://$domain$(env_value MAX_WEBHOOK_PATH || true)"
