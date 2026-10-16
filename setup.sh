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
python3 - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
keys = ("STATUS_API_TOKEN", "STATUS_ADMIN_TOKEN")
for key in keys:
    valid = False
    for line in lines:
        if line.startswith(key + "="):
            valid = len(line.split("=", 1)[1].strip()) >= 32
            break
    if not valid:
        value = secrets.token_urlsafe(48)
        for index, line in enumerate(lines):
            if line.startswith(key + "="):
                lines[index] = key + "=" + value
                break
        else:
            lines.append(key + "=" + value)
        print(f"已生成/修复 {key}。")
path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
path.chmod(0o600)
PY

if ! grep -Eq '^STATUS_API_TOKEN=.{32,}$' .env; then
  echo "错误：.env 中的 STATUS_API_TOKEN 不存在或长度不足 32 个字符。" >&2
  exit 1
fi
if ! grep -Eq '^STATUS_ADMIN_TOKEN=.{32,}$' .env; then
  echo "错误：.env 中的 STATUS_ADMIN_TOKEN 不存在或长度不足 32 个字符。" >&2
  exit 1
fi

docker compose up -d --build

echo
echo "服务已启动。健康检查：curl http://127.0.0.1:8090/healthz"
echo "令牌仅保存在本机 .env（权限 600）。以下输出请勿复制到聊天、截图或日志："
grep -E '^STATUS_(API|ADMIN)_TOKEN=' .env
echo
echo "设备管理页面请通过 HTTPS 域名访问：https://YOUR_DOMAIN/admin/devices"
echo "Android 使用 STATUS_API_TOKEN；网页管理使用 STATUS_ADMIN_TOKEN。两者不可混用。"
