#!/bin/bash

set -xu

export APIPROXY_WORKERS=${APIPROXY_WORKERS:-16}
export APIPROXY_PORT=${APIPROXY_PORT:-11434}

uvicorn --host 0.0.0.0 --port ${APIPROXY_PORT} --workers ${APIPROXY_WORKERS} --factory openaiproxy.main:setup_app

exit $?
