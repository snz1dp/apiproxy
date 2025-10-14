#!/bin/bash

set -xe

declare SERVER_APIKEY=${SERVER_APIKEY:-""}
declare SERVER_PORT=${SERVER_PORT:-8008}
declare SERVER_STRATEGY=${SERVER_STRATEGY:-min_expected_latency}
declare SERVER_LOG_LEVEL=${SERVER_LOG_LEVEL:-INFO}

python3 -m openaiproxy.main \
  --server_name 0.0.0.0 \
  --server_port ${SERVER_PORT} \
  --strategy ${SERVER_STRATEGY} \
  --api_keys "${SERVER_APIKEY}" \
  --log_level ${SERVER_LOG_LEVEL}
