#!/bin/bash

set -xu

export APIPROXY_WORKERS=${APIPROXY_WORKERS:-16}
export APIPROXY_PORT=${APIPROXY_PORT:-11434}

uvicorn --host 0.0.0.0 --port ${TAIYIFLOW_PORT} --workers ${TAIYIFLOW_WORKERS} --factory openaiproxy.main:setup_app

exit $?
