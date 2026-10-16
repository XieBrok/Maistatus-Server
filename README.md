# Maistatus Server 接收端

> **English:** A lightweight FastAPI backend that receives personal status events (`location` / `activity` / `music`) from the [Maistatus Android client](https://github.com/XieBrok/Maistatus-android). It persists events in SQLite, authenticates every request with a Bearer token, deduplicates by `event_id`, and ships with Docker Compose deployment and an Nginx reverse-proxy example.

一个与 [`XieBrok/Maistatus-android`](https://github.com/XieBrok/Maistatus-android) Kotlin 客户端上传逻辑配套的轻量服务端：**FastAPI + SQLite + Docker Compose**，适合部署在 Ubuntu/Debian 服务器上，通过 HTTPS 反向代理对外提供接口。

> 注意：上游仓库 README 展示过一个汇总式 JSON 示例，但当前 Android `UploadWorker.kt` 实际发出的是**单事件 JSON**。本服务端按实际代码实现：一次 `POST` 对应一个 `location`、`activity` 或 `music` 事件。

---

## 目录

1. [功能与架构](#1-功能与架构)
2. [支持的事件类型](#2-支持的事件类型)
3. [API 接口说明](#3-api-接口说明)
4. [Docker Compose 部署](#4-docker-compose-部署)
5. [环境变量](#5-环境变量)
6. [Token 生成、轮换与保管](#6-token-生成轮换与保管)
7. [HTTPS 与反向代理](#7-https-与反向代理)
8. [Android 客户端如何配置](#8-android-客户端如何配置)
9. [健康检查与故障排查](#9-健康检查与故障排查)
10. [数据持久化、备份与恢复](#10-数据持久化备份与恢复)
11. [本地测试](#11-本地测试)
12. [安全与隐私](#12-安全与隐私)
13. [项目结构](#13-项目结构)
14. [设备管理面板](#14-设备管理面板)
15. [客户端上传记录](#15-客户端上传记录)

---

## 1. 功能与架构

```
 Android App (Kotlin, HTTPS 必需)
        │  POST /api/v1/events   Bearer Token + Idempotency-Key
        ▼
 反向代理 (Nginx / Nginx Proxy Manager / Cloudflare Tunnel)   ← TLS 终止
        │  http://127.0.0.1:8090
        ▼
 FastAPI 应用 (uvicorn, 容器内 0.0.0.0:8000)
        │
        ▼
 SQLite 数据库  /data/status.db  (WAL 模式, Docker 卷持久化)
```

组件：

| 组件 | 说明 |
| --- | --- |
| `main.py` | FastAPI 应用：认证、校验、去重、写入与查询 |
| `Dockerfile` | 基于 `python:3.12-slim`，以非 root 用户（uid 10001）运行 |
| `docker-compose.yml` | 单服务编排，容器只绑定到宿主 `127.0.0.1:8090` |
| `setup.sh` | 生成随机 Token、写 `.env`（权限 600）、构建并启动 |
| `nginx-location.conf` | Nginx 反向代理 `location` 片段（HTTPS server 块内使用） |
| `tests/smoke_test.py` | 无依赖的契约自检脚本 |

特性：

- 每个 API 请求（除健康检查外）都用常量时间比较校验 Bearer Token。
- 按 `event_id` 去重：重复上报返回 `2xx`，客户端据此删除本地待上传队列，不产生重复记录。
- 请求正文**不写日志**，避免坐标、歌曲、凭证泄漏到日志。
- 服务默认只监听 `127.0.0.1`，需由反向代理终止 TLS 后对外提供 HTTPS。

## 2. 支持的事件类型

每个事件是一个 JSON 对象，均包含三个公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | string | 事件唯一 ID（客户端为 UUID），≤128 字符 |
| `event_time` | int | Unix **毫秒**时间戳，必须 > 0 |
| `event_type` | string | `location` / `activity` / `music` |

**location（位置）**

```json
{
  "latitude": 31.2, "longitude": 121.5,
  "accuracy_meters": 18.0, "provider": "fused",
  "event_id": "8d40a14e-3e7e-43ef-9f9e-a963cdb24ac1",
  "event_time": 1720000000000, "event_type": "location"
}
```
`latitude` / `longitude` 必填且需在有效范围内；`accuracy_meters` 可选（≥0）；`provider` 可选（如 `fused` / `gps`）。

**activity（活动识别）**

```json
{ "activity_type": 3, "confidence": 87,
  "event_id": "...", "event_time": 1720000000000, "event_type": "activity" }
```
`activity_type` 为整数（对应 Android `DetectedActivity` 类型），`confidence` 为 0–100 的整数。

**music（音乐播放）**

```json
{ "state": "playing", "title": "Song", "artist": "Artist",
  "package": "com.example.player",
  "event_id": "...", "event_time": 1720000000000, "event_type": "music" }
```
`state` 必须是 `playing` / `paused` / `stopped` 之一；`title` / `artist` / `package` 可选，取决于客户端能否读取。

## 3. API 接口说明

除 `/healthz`、管理页面静态外壳与管理登录入口外，设备 API 都需要请求头 `Authorization: Bearer <STATUS_API_TOKEN>`。管理面板使用独立的 `STATUS_ADMIN_TOKEN` 登录，管理数据 API 不接受设备写入令牌。

| 方法 | 路径 | 用途 | 认证 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 健康检查，不返回个人状态 | 否 |
| `POST` | `/api/v1/events` | 接收上报事件 | 是 |
| `GET` | `/api/v1/status/latest` | 返回最新位置/活动/音乐状态 | 是 |
| `GET` | `/api/v1/events?limit=50[&event_type=music]` | 查看最近事件 | 是 |
| `POST` | `/api/v1/devices/heartbeat` | 更新设备最近通信时间 | 是 |
| `GET` | `/admin/api/devices` | 管理端设备列表与统计 | 管理员 |
| `GET` | `/admin/api/events?limit=50&offset=0` | 管理端分页查询上传记录 | 管理员 |

**上报（POST /api/v1/events）**

请求头：

```http
Content-Type: application/json; charset=utf-8
Authorization: Bearer <设备写入凭证>
Idempotency-Key: <event_id>
```

`Idempotency-Key` 必须与正文中的 `event_id` 一致，否则返回 `400`。

响应：

```json
// 首次写入
{ "ok": true, "duplicate": false, "event_id": "...", "received_at": 1720000000123 }
// 重复事件（同 event_id）
{ "ok": true, "duplicate": true, "event_id": "..." }
```

**最新状态（GET /api/v1/status/latest）**

```json
{
  "ok": true,
  "location": { "latitude": 31.2, "longitude": 121.5, "accuracy_meters": 18.0,
                "provider": "fused", "event_id": "...", "event_time": 1720000000000, "received_at": 1720000000123 },
  "activity": { "...": "..." },
  "music":    { "...": "..." },
  "last_event_time": 1720000000000
}
```
未被上报过的类别为 `null`。

**状态码一览**

| 状态码 | 含义 |
| --- | --- |
| `200` | 写入成功，或重复事件被去重 |
| `400` | JSON 非法，或 `Idempotency-Key` 与 `event_id` 不一致 |
| `401` | 缺少或错误的 Bearer Token |
| `409` | `Idempotency-Key` 已被另一个事件占用 |
| `413` | 请求正文超过 64 KiB |
| `422` | 字段校验失败（类型/范围不正确） |

## 4. Docker Compose 部署

前置条件：目标服务器已安装 Docker Engine 与 Docker Compose 插件（`docker compose`）。

```bash
# 1. 上传项目到服务器（示例路径）
sudo mkdir -p /opt/maistatus-server
sudo chown "$USER":"$USER" /opt/maistatus-server
# 将本项目文件复制到 /opt/maistatus-server

# 2. 进入目录并运行安装脚本
cd /opt/maistatus-server
chmod +x setup.sh
./setup.sh
```

`setup.sh` 会：

1. 校验 `docker` 与 `docker compose` 是否可用；
2. 若不存在 `.env`，用 `secrets.token_urlsafe(48)` 生成随机 Token，写入 `.env` 并把权限设为 `600`；
3. 执行 `docker compose up -d --build` 构建并启动服务；
4. 在可信终端显示 `STATUS_API_TOKEN` 和 `STATUS_ADMIN_TOKEN`；前者填入 Android，后者用于网页登录。终端输出属于敏感信息。

服务只绑定宿主 `127.0.0.1:8090`，不会直接监听公网。查看状态：

```bash
docker compose ps
curl http://127.0.0.1:8090/healthz
docker compose logs --tail=100 maistatus-api
```

常用运维命令：

```bash
cd /opt/maistatus-server
docker compose restart               # 重启
docker compose up -d --build         # 更新代码后重建
docker compose down                  # 停止（不会删除 ./data）
```

## 5. 环境变量

配置通过项目根目录的 `.env` 文件传入（`docker-compose.yml` 中 `env_file: .env`）。参考 `.env.example`：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `STATUS_API_TOKEN` | 是 | 设备写入凭证，至少 32 字符；长度不足应用会拒绝启动 |
| `STATUS_ADMIN_TOKEN` | 是 | 网页管理令牌，必须与设备写入令牌分开，至少 32 字符 |
| `STATUS_DB_PATH` | 否 | 数据库路径，默认容器内 `/data/status.db`（由 compose 设定） |
| `TZ` | 否 | 容器时区，默认 `Asia/Shanghai`（由 compose 设定） |

`.env.example` 中的值只是**占位符**，必须由 `setup.sh` 或你自己生成真实值，切勿直接照抄。

## 6. Token 生成、轮换与保管

- **生成**：`setup.sh` 自动生成；也可手动生成后写入 `.env`：
  ```bash
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
  ```
- **保管**：Token 只保存在服务器 `.env`（权限 `600`）与 Android App 中。不要提交到 Git、不要发到公开群聊、不要写入日志。
- **查看**：`grep -E '^STATUS_(API|ADMIN)_TOKEN=' /opt/maistatus-server/.env`（只在可信终端查看）
- **轮换**：
  ```bash
  # 编辑 .env 修改 STATUS_API_TOKEN 后：
  cd /opt/maistatus-server && docker compose up -d --force-recreate
  ```
  随后在 Android App 中同步更新“设备写入凭证”。

## 7. HTTPS 与反向代理

Android 客户端会拒绝非 HTTPS 地址，正式使用必须经 HTTPS 域名访问。服务本身监听 `127.0.0.1:8090`，由反向代理终止 TLS 并转发。

**Nginx（宿主机安装）**：在对应域名的 **HTTPS `server {}`** 块内加入 [`nginx-location.conf`](nginx-location.conf) 的内容：

```nginx
location /maistatus/ {
    client_max_body_size 64k;
    proxy_pass http://127.0.0.1:8090/;      # 尾部斜杠会把 /maistatus/<path> 映射为 /<path>
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_read_timeout 15s;
    proxy_send_timeout 15s;
}
```

对应客户端端点：`https://status.example.com/maistatus/api/v1/events`。

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**Nginx Proxy Manager / Cloudflare Tunnel**：把 HTTPS 域名转发到 `http://127.0.0.1:8090`。注意 **`127.0.0.1` 的含义取决于代理运行位置**：若代理本身是容器，容器内的 `127.0.0.1` 指向它自己而非宿主机——此时应让后端与代理处于同一 Docker 网络，并按容器名转发（例如 `http://maistatus-api:8000`）。可让代理以独立子域名的根路径直接转发（如 `https://status.example.com/` → `http://maistatus-api:8000/`），此时客户端端点就是 `https://status.example.com/api/v1/events`。

无论采用哪种方式，**对外必须使用 HTTPS**，且客户端填写的完整路径要与服务端路由一致。

## 8. Android 客户端如何配置

在 App 的“服务器 HTTPS URL”中填写**完整上传端点**，而不是只填域名：

```text
https://status.example.com/api/v1/events
```

（若使用上文的子路径反向代理，则为 `https://status.example.com/maistatus/api/v1/events`。）

- “设备写入凭证”填 `setup.sh` 输出的 `STATUS_API_TOKEN`。
- 保存后按需授予定位、活动识别、音乐通知访问权限并开始采集。
- 客户端对上传地址的处理：仅接受 `https://` 开头的完整 URL；收到 **2xx** 后删除本地待上传事件；**408 / 429 / 5xx** 会重试；其它 **4xx** 会丢弃该事件。因此服务端必须先成功持久化再返回 2xx。

## 9. 健康检查与故障排查

健康检查（预期返回 `{"ok":true,"service":"maistatus-receiver","version":"1.2.0"}`）：

```bash
curl http://127.0.0.1:8090/healthz
```

常见问题：

| 现象 | 可能原因与处理 |
| --- | --- |
| 容器起不来，日志提示 Token 长度不足 | `.env` 中 `STATUS_API_TOKEN` 少于 32 字符，重新运行 `setup.sh` 或手动修正 |
| 容器 `unhealthy` / 反复重启 | 查看 `docker compose logs maistatus-api`；多为 `.env` 缺失或数据库目录不可写 |
| 外网访问 502 / 526 | 反向代理未指向 `127.0.0.1:8090`，或代理在容器内不理解 `127.0.0.1`（见第 7 节） |
| 客户端报「必须使用 HTTPS」 | 填的是非 https 地址，或只填了域名而非完整端点 |
| 上报返回 401 | App 中的凭证与服务器 `.env` 不一致 |
| 上报返回 422 | 字段类型/范围不符（如 `event_time` 不是毫秒整数） |
| 上报返回 400 | `Idempotency-Key` 与 `event_id` 不一致 |
| 数据 `./data` 不可写 | 绑定目录属主需与容器内 uid 10001 一致：`sudo chown -R 10001:10001 ./data` |

## 10. 数据持久化、备份与恢复

- 数据库位于 `./data/status.db`，通过 `docker-compose.yml` 的 `./data:/data` 绑定挂载持久化；`docker compose down` 不会删除它。
- **备份**（一致性备份，无需停机）：
  ```bash
  docker exec maistatus-api python -c \
    "import sqlite3;s=sqlite3.connect('/data/status.db');d=sqlite3.connect('/tmp/backup.db');s.backup(d);d.close()"
  docker cp maistatus-api:/tmp/backup.db ./maistatus-backup-$(date +%F).db
  ```
- **恢复**：
  ```bash
  docker compose down
  cp maistatus-backup-YYYY-MM-DD.db ./data/status.db
  rm -f ./data/status.db-wal ./data/status.db-shm
  docker compose up -d
  ```
- 备份文件包含个人状态数据，请存放在访问受控的位置。

## 11. 本地测试

不依赖服务器/数据库，可在本地快速自检：

```bash
# 语法检查
python -m py_compile main.py

# 契约自检（无第三方依赖）
python tests/smoke_test.py
```

如需本地起服务（需自行安装依赖）：

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
STATUS_API_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  STATUS_ADMIN_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  STATUS_DB_PATH="./data/status.db" \
  uvicorn main:app --host 127.0.0.1 --port 8090
```

## 12. 安全与隐私

- 位置、活动与音乐均为个人敏感信息，请限制服务器访问与备份范围。
- 设备 API 使用 `STATUS_API_TOKEN` Bearer Token；管理数据 API 使用独立的 `STATUS_ADMIN_TOKEN` 或签名会话 Cookie；令牌比较采用常量时间比较。
- 服务端**不记录请求正文**，避免日志写入精确坐标、歌曲或凭证。
- 保持后端只监听 loopback，前面必须有终止 TLS 的反向代理；不要将 `8090` 直接暴露到公网。
- 不要提交或公开 `.env`、数据库、Token、真实经纬度或含凭证的截图。
- 更多说明见 [SECURITY.md](SECURITY.md)。

## 13. 项目结构

```text
.
├── main.py                 # FastAPI 应用
├── Dockerfile              # 容器镜像定义
├── docker-compose.yml      # 编排（仅绑定 127.0.0.1:8090）
├── requirements.txt        # Python 依赖
├── setup.sh                # 生成 Token + 构建启动
├── .env.example            # 环境变量模板（占位值）
├── .gitignore
├── nginx-location.conf     # Nginx 反向代理片段
├── README.md
├── SECURITY.md
├── data/
│   └── .gitkeep            # 保持 data 目录存在（数据库文件被忽略）
└── tests/
    └── smoke_test.py       # 契约自检
```


## 14. 设备管理面板

服务端包含一个独立的设备管理页面，可通过 HTTPS 域名访问：

- 页面：`https://YOUR_DOMAIN/admin/devices`
- 若使用本仓库 Nginx 子路径代理：`https://YOUR_DOMAIN/maistatus/admin/devices`
- 管理端 API：`GET /admin/api/devices`（需要管理会话或 `Authorization: Bearer <STATUS_ADMIN_TOKEN>`）
- 登录：页面输入 `STATUS_ADMIN_TOKEN`，服务端设置 HttpOnly、Secure、SameSite=Strict 会话 Cookie，有效期 8 小时。
- 管理令牌与 Android 的 `STATUS_API_TOKEN` 分开生成、分开保管；不要把令牌放进 URL、截图或日志。

`./setup.sh` 会在 `.env` 中分别生成两枚至少 32 字符的令牌，并将文件权限设为 600。旧版本已有 `.env` 时，脚本会补齐缺失的管理令牌，不需要删除现有数据或重置设备写入凭证。

新版本 Android 客户端会在上报事件中发送持久化的 `device_id`，并定期调用 `POST /api/v1/devices/heartbeat`。服务端据此记录设备名称、平台、首次/最近通信时间及事件总数。最近 45 分钟内收到心跳或状态上报的设备标记为“最近活跃”；这不是长连接状态。旧客户端没有设备 ID，其事件会显示在“旧客户端未识别事件”统计中，而不会被错误地拆分成虚假设备。

页面和 API 复用现有 HTTPS 反向代理，无需开放容器的 8090 端口。使用 Nginx Proxy Manager 或 Cloudflare Tunnel 时，请确保 HTTPS 域名转发到当前接收端，并让代理传递 `X-Forwarded-Proto: https`。设备列表 API 只返回聚合统计与设备元数据；上传记录由独立管理员接口读取。


## 15. 客户端上传记录

在设备管理页面 `/admin/devices` 中，登录后可以查看“客户端上传记录”，无需额外部署页面。

- 表格展示服务器接收时间、客户端事件时间、设备名称、事件类型、事件 ID 与简要摘要。
- 支持按 `location`（位置）、`activity`（活动识别）、`music`（音乐）筛选，也支持按已识别设备筛选。
- 每页最多 200 条，默认每页 50 条；支持上一页/下一页。结果按服务端接收时间倒序排列。
- “查看详情”会展开对应事件的完整 JSON。位置事件可能包含精确坐标，音乐事件可能包含歌曲和艺术家名称，因此整个区域仅能通过 HTTPS 与管理员会话访问。
- 接口：`GET /admin/api/events?limit=50&offset=0&event_type=music&device_id=<设备ID>`。所有筛选参数均可省略；`limit` 最大为 200。接口要求管理员会话或单独的 `STATUS_ADMIN_TOKEN`，不会接受客户端写入令牌。
- 只展示已经成功写入 SQLite 的事件。验证失败的请求、网络失败、服务端拒绝的请求和被事件 ID 去重的重复上传不会成为额外记录；此版本没有新增请求正文日志。
