#!/usr/bin/env bash
# Aggregator docker 镜像统一入口
#
# 用法:
#   docker run ghcr.io/wzdnzd/aggregator <command> [args...]
#
# 支持的 command:
#   process   跑 subscribe/process.py
#   collect   跑 subscribe/collect.py（默认）
#   refresh   跑 subscribe/collect.py --all --refresh --overwrite --skip
#   checkin   跑 .github/actions/checkin/universal.py
#
# 默认 command = collect + collect.py 默认参数

set -euo pipefail

CMD="${1:-collect}"
shift || true

case "$CMD" in
  process)
    cd /aggregator/subscribe
    exec python -u process.py "$@"
    ;;
  collect)
    cd /aggregator/subscribe
    exec python -u collect.py "$@"
    ;;
  refresh)
    cd /aggregator/subscribe
    exec python -u collect.py --all --refresh --overwrite --skip "$@"
    ;;
  checkin)
    exec python -u /aggregator/.github/actions/checkin/universal.py "$@"
    ;;
  -h|--help|help)
    echo "Usage: docker run <image> <command> [args...]"
    echo "Commands: process | collect | refresh | checkin"
    exit 0
    ;;
  *)
    echo "Unknown command: $CMD" >&2
    echo "Supported: process | collect | refresh | checkin" >&2
    exit 2
    ;;
esac
