#!/bin/sh

if [ "$APIPROXY_AUTOCREATE_DB" = "true" ]; then
  echo "APIPROXY_AUTOCREATE_DB is true, initializing database..."

  export PGPASSWORD=${APIPROXY_DB_PASSWORD:-}
  export POSTGRE_HOST=${APIPROXY_DB_HOST:-postgres}
  export POSTGRE_PORT=${APIPROXY_DB_PORT:-5432}
  export POSTGRE_USER=${APIPROXY_DB_USER:-postgres}
  export POSTGRE_NAME=${APIPROXY_DB_NAME:-apiproxy}

  psql -h $POSTGRE_HOST -p $POSTGRE_PORT \
    -U $POSTGRE_USER \
    -lqt | cut -d \| -f 1 | grep -wq $POSTGRE_NAME >/dev/null 2>&1

  if [ $? -eq 0 ]; then
    echo "Database $POSTGRE_NAME already exists";
  else
    psql -h $POSTGRE_HOST -p $POSTGRE_PORT \
      -U $POSTGRE_USER \
      -c "CREATE DATABASE $POSTGRE_NAME" >/dev/null 2>&1 && \
    echo "Database $POSTGRE_NAME created";
  fi
  unset PGPASSWORD
  unset POSTGRE_HOST
  unset POSTGRE_PORT
  unset POSTGRE_USER
  unset POSTGRE_NAME
else
  echo "APIPROXY_AUTOCREATE_DB is not true, skipping database initialization."
  echo "To enable automatic database creation, set APIPROXY_AUTOCREATE_DB=true"
  exit 0
fi
