# ---- Dependency stage ----
FROM composer:2 AS vendor

WORKDIR /app

COPY composer.json composer.lock ./
RUN composer install \
    --no-dev \
    --no-interaction \
    --no-scripts \
    --prefer-dist \
    --optimize-autoloader \
    --ignore-platform-reqs

# ---- Runtime stage (FrankenPHP) ----
FROM dunglas/frankenphp:1-php8.3-alpine

# PHP extensions required by Laravel 13 + Filament + Sanctum + PostgreSQL
RUN install-php-extensions \
    pdo_pgsql \
    pgsql \
    redis \
    intl \
    zip \
    bcmath \
    pcntl \
    opcache \
    exif \
    gd

# Server-level environment for FrankenPHP / Caddy
ENV SERVER_NAME=":80" \
    APP_ENV=production \
    OCTANE_SERVER=frankenphp

WORKDIR /var/www/html

# Copy pre-built vendor from the composer stage
COPY --from=vendor /app/vendor ./vendor

# PHP upload / execution limits override
COPY php/uploads.ini /usr/local/etc/php/conf.d/uploads.ini

# Copy application source
COPY . .

# Bootstrap the application
# Note: config:cache is intentionally omitted — env vars are injected at
# container runtime via docker-compose, so caching at build time would bake
# in wrong values (e.g. FRONTEND_URL). route:cache and view:cache are safe.
RUN php artisan storage:link \
    && php artisan route:cache \
    && php artisan view:cache \
    && chown -R www-data:www-data storage bootstrap/cache

EXPOSE 80

CMD ["frankenphp", "run", "--config", "/etc/caddy/Caddyfile"]
