#!/usr/bin/env sh
set -eu
: "${MYSQL_HOST:=localhost}"
: "${MYSQL_PORT:=3306}"
: "${MYSQL_USER:=root}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is required}"
: "${MYSQL_DATABASE:=aienglish}"
for file in "$(dirname "$0")"/migrations/*.sql; do
  mysql --protocol=tcp -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" < "$file"
done
echo "database migrations applied"
