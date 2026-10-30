FROM php:8.3-apache-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libsqlite3-dev \
    && docker-php-ext-install pdo_sqlite \
    && a2enmod rewrite \
    && sed -i 's/Listen 80/Listen 8080/' /etc/apache2/ports.conf \
    && sed -i 's/:80>/:8080>/; s|/var/www/html|/app/public|' /etc/apache2/sites-available/000-default.conf \
    && printf '<Directory /app/public>\nAllowOverride All\nRequire all granted\n</Directory>\nServerName localhost\n' > /etc/apache2/conf-available/llmban.conf \
    && a2enconf llmban \
    && chown -R www-data:www-data /var/run/apache2 /var/lock/apache2 /var/log/apache2
WORKDIR /app
COPY docker/php.ini /usr/local/etc/php/conf.d/llmban.ini
COPY public/ public/
COPY migrations/ migrations/
USER www-data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s CMD php -r 'exit(@fsockopen("127.0.0.1",8080) ? 0 : 1);'
