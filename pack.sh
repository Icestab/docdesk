#!/usr/bin/env bash
# 把 docdesk 打成可搬运的 tar.gz（不含 .venv，首次运行会自动重建）
#   ./pack.sh                 -> ~/docdesk-YYYYMMDD.tar.gz
#   ./pack.sh /tmp/xxx.tar.gz
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="$(basename "$HERE")"
OUT="${1:-$HOME/$NAME-$(date +%Y%m%d).tar.gz}"

tar -czf "$OUT" -C "$(dirname "$HERE")" \
    --exclude="$NAME/.venv" \
    --exclude="$NAME/__pycache__" \
    --exclude="$NAME/.git" \
    "$NAME"

echo "打包完成: $OUT"
echo "解包后直接 ./docdesk.sh —— 没有 .venv 会自动建（需要联网装 4 个库）"
ls -lh "$OUT" | awk '{print "大小: "$5}'
