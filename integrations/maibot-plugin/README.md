# MaiStatus MaiBot 插件

将 Android MyStatus/MaiStatus 上传的状态提供给 MaiBot 查询。适配本项目服务端 API：FastAPI + SQLite。

## 功能

- `/mystatus`：查看最新音乐、活动和位置上报摘要
- `/mystatus music`：只看音乐播放状态
- `/mystatus activity`：只看活动识别状态
- `/mystatus location`：查看位置上报是否更新（默认隐藏坐标）
- `/mystatus history`：查看近期事件
- `/mystatus ping`：检查健康接口
- LLM 工具 `maistatus_get_latest`：自然语言查询最新状态
- LLM 工具 `maistatus_get_history`：按类别筛选历史事件

## 安装

1. 将 `maistatus` 插件目录放进 MaiBot 的插件目录。
2. 重载插件，在配置页设置「状态服务器」：
   - `base_url`: `https://maistatus.hhyyyy.cn`
   - `read_token`: 服务器 API 的 Bearer Token
3. 先在私聊里运行 `/mystatus ping`，再试 `/mystatus`。

## 安全

- 插件只发送 GET 请求，不会创建、修改或删除状态数据。
- Token 只配置在 MaiBot 本地，不要提交到 Git。
- 精确位置默认隐藏。仅当你明确需要时才将 `include_exact_location` 改为 true。
- 不建议在群聊中主动披露个人位置；位置工具调用最好限定在私聊或可信会话。

## API 契约

- `GET /healthz`
- `GET /api/v1/status/latest` 返回顶层 `location`、`activity`、`music`、`last_event_time`
- `GET /api/v1/events?limit=10&event_type=music` 返回 `count` 和 `events`，每条事件包含 `event_time`、`event_type`、`data`

## 版本与验证

版本 1.1.0。代码按服务端 `main.py` 当前返回结构编写。请在实际 MaiBot 容器中完成插件加载、Token 验证和命令联调；仓库 API 连接器不能代替你的线上环境测试。
