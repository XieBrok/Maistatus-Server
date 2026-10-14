#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：没有找到 docker，请先安装 Docker Engine 和 Docker Compose 插件。" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "错误：当前 Docker 未安装 Compose 插件（需要 docker compose）。" >&2
  exit 1
fi

mkdir -p data
if [ ! -f .env ]; then
  python3 - <<'PY'
from pathlib import Path
import secrets
Path('.env').write_text('STATUS_API_TOKEN=' + secrets.token_urlsafe(48) + '\n', encoding='utf-8')
Path('.env').chmod(0o600)
PY
  echo "已生成 .env 和随机 API Token（文件权限已设为 600）。"
else
  chmod 600 .env
  if ! grep -Eq '^STATUS_API_TOKEN=.{32,}$' .env; then
    echo "错误：.env 中的 STATUS_API_TOKEN 不存在或长度不足 32 个字符。" >&2
    echo "请编辑 .env 后重新运行。" >&2
    exit 1
  fi
fi

docker compose up -d --build

echo
echo "服务已启动。健康检查：curl http://127.0.0.1:8090/healthz"
echo "服务器令牌（请粘贴到 Android App 的设备写入凭证，勿发给他人）："
python3 - <<'PY'
from pathlib import Path
for line in Path('.env').read_text(encoding='utf-8').splitlines():
    if line.startswith('STATUS_API_TOKEN='):
        print(line.split('=', 1)[1])
        break
PY
