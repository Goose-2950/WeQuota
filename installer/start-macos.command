#!/bin/bash
# WeQuota 启动脚本（macOS）：优先启动 .app，没有则直接跑 onedir 二进制
cd "$(dirname "$0")/.." || exit 1
if [ -d "dist/WeQuota.app" ]; then
  open "dist/WeQuota.app"
elif [ -x "dist/WeQuota/WeQuota" ]; then
  ./dist/WeQuota/WeQuota "$@"
else
  echo "未找到打包产物，请先运行：python -m installer.build_release"
  exit 1
fi
