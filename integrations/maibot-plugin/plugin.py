from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from maibot_sdk import MaiBotPlugin, Command, Tool, PluginConfigBase, Field
from maibot_sdk.types import ToolParameterInfo, ToolParamType


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件设置"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="启用插件")
    config_version: str = Field(default="1.1.0", description="插件配置版本")


class ServerSection(PluginConfigBase):
    __ui_label__ = "状态服务器"
    __ui_order__ = 1
    base_url: str = Field(default="https://maistatus.hhyyyy.cn", description="状态服务器根地址")
    read_token: str = Field(default="", description="只读 Bearer Token")
    timeout_seconds: int = Field(default=8, description="请求超时秒数（3-20）")
    history_limit: int = Field(default=10, description="历史默认条数（1-50）")
    include_exact_location: bool = Field(default=False, description="允许输出精确坐标（默认关闭）")


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    server: ServerSection = Field(default_factory=ServerSection)


def safe(value: Any, max_len: int = 180) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("\r", " ")[:max_len]


def format_time(value: Any) -> str:
    try:
        # MyStatus server timestamps are Unix milliseconds.
        stamp = int(value) / 1000
        return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return safe(value, 60) or "时间未知"


class MaiStatusPlugin(MaiBotPlugin):
    config_model = Config

    async def on_load(self) -> None:
        self.ctx.logger.info("MaiStatus 插件已加载")

    def _request(self, path: str) -> dict[str, Any]:
        cfg = self.config.server
        base = cfg.base_url.strip().rstrip("/")
        if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
            raise ValueError("服务器地址必须使用 HTTPS（本机回环地址除外）")
        if not cfg.read_token.strip():
            raise ValueError("请先配置状态服务器只读 Token")
        request = urllib.request.Request(
            base + path,
            headers={"Authorization": "Bearer " + cfg.read_token.strip(),
                     "Accept": "application/json", "User-Agent": "MaiStatus-MaiBot/1.1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(3, min(int(cfg.timeout_seconds), 20))) as response:
                payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("服务器返回的 JSON 不是对象")
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError("鉴权失败，请检查只读 Token") from exc
            raise RuntimeError(f"服务器 HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接服务器：{safe(exc.reason, 100)}") from exc

    def _latest_text(self, payload: dict[str, Any], category: str = "all") -> str:
        labels = {"location": "位置", "activity": "活动", "music": "音乐"}
        categories = [category] if category in labels else ["music", "activity", "location"]
        lines = ["📡 个人状态摘要"]
        found = False
        for key in categories:
            item = payload.get(key)
            if not isinstance(item, dict):
                continue
            found = True
            when = format_time(item.get("event_time"))
            lines.append(f"\n【{labels[key]}】更新时间：{when}")
            if key == "music":
                state = safe(item.get("state"), 30) or "未知"
                title = safe(item.get("title"), 120) or "未知曲目"
                artist = safe(item.get("artist"), 100)
                package = safe(item.get("package"), 100)
                lines.append(f"播放状态：{state}；曲目：{title}" + (f"；歌手：{artist}" if artist else ""))
                if package:
                    lines.append(f"播放器：{package}")
            elif key == "activity":
                lines.append(f"活动类型代码：{safe(item.get('activity_type'), 20) or '未知'}；置信度：{safe(item.get('confidence'), 8) or '未知'}%")
            elif key == "location":
                if self.config.server.include_exact_location:
                    lat, lon = item.get("latitude"), item.get("longitude")
                    if lat is not None and lon is not None:
                        lines.append(f"坐标：{safe(lat, 24)}, {safe(lon, 24)}")
                else:
                    lines.append("已收到位置上报（精确坐标默认隐藏）")
                accuracy = item.get("accuracy_meters")
                if accuracy is not None:
                    lines.append(f"定位精度约：{safe(accuracy, 20)} 米")
        if not found:
            lines.append("服务器尚无该类别的状态记录。")
        last = payload.get("last_event_time")
        if last:
            lines.append(f"\n最近一次上报：{format_time(last)}")
        lines.append("\n隐私提示：位置坐标默认隐藏。")
        return "\n".join(lines)

    def _history_text(self, payload: dict[str, Any]) -> str:
        events = payload.get("events", [])
        if not isinstance(events, list) or not events:
            return "暂时没有历史上报记录。"
        lines = [f"🗂️ 最近上报记录（{len(events)} 条）"]
        for index, event in enumerate(events, 1):
            if not isinstance(event, dict):
                continue
            kind = safe(event.get("event_type"), 30) or "未知"
            when = format_time(event.get("event_time"))
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            summary = ""
            if kind == "music":
                summary = safe(data.get("title"), 100)
                artist = safe(data.get("artist"), 80)
                if artist:
                    summary += " - " + artist
                state = safe(data.get("state"), 20)
                if state:
                    summary += f" ({state})"
            elif kind == "activity":
                summary = f"活动代码 {safe(data.get('activity_type'), 20)}，置信度 {safe(data.get('confidence'), 8)}%"
            elif kind == "location":
                summary = "位置事件（坐标已隐藏）"
                if self.config.server.include_exact_location:
                    summary = f"坐标 {safe(data.get('latitude'), 20)}, {safe(data.get('longitude'), 20)}"
            lines.append(f"{index}. {when}｜{kind}" + (f"｜{summary}" if summary else ""))
        return "\n".join(lines)

    @Tool("maistatus_get_latest", brief_description="查询用户上传到个人状态服务器的最新音乐、活动和位置状态。")
    async def tool_get_latest(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        try:
            return {"content": self._latest_text(self._request("/api/v1/status/latest"))}
        except Exception as exc:
            self.ctx.logger.warning("MaiStatus latest query failed: %s", exc)
            return {"content": "查询个人状态失败：" + safe(exc, 180)}

    @Tool(
        "maistatus_get_history",
        brief_description="查询个人状态服务器的近期上报记录，可按音乐、活动或位置筛选。",
        parameters=[
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="记录条数，1-50", required=False),
            ToolParameterInfo(name="event_type", param_type=ToolParamType.STRING, description="可选：music、activity、location", required=False),
        ],
    )
    async def tool_get_history(self, limit: int = 10, event_type: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        try:
            limit = max(1, min(int(limit), 50))
            params: dict[str, Any] = {"limit": limit}
            if event_type in {"music", "activity", "location"}:
                params["event_type"] = event_type
            payload = self._request("/api/v1/events?" + urllib.parse.urlencode(params))
            return {"content": self._history_text(payload)}
        except Exception as exc:
            self.ctx.logger.warning("MaiStatus history query failed: %s", exc)
            return {"content": "查询历史记录失败：" + safe(exc, 180)}

    @Command("mystatus", description="查看个人状态：最新状态、音乐、活动、位置或历史", pattern=r"^/mystatus(?:\s+(?P<action>music|activity|location|history|ping))?\s*$", aliases=["/我的状态"])
    async def command_mystatus(self, **kwargs: Any):
        stream_id = kwargs.get("stream_id", "")
        action = ((kwargs.get("matched_groups") or {}).get("action") or "").strip().lower()
        try:
            if action == "ping":
                payload = self._request("/healthz")
                text = "✅ 状态服务器可访问：" + safe(payload.get("service"), 80) + " v" + safe(payload.get("version"), 30)
            elif action == "history":
                limit = max(1, min(int(self.config.server.history_limit), 50))
                payload = self._request("/api/v1/events?" + urllib.parse.urlencode({"limit": limit}))
                text = self._history_text(payload)
            else:
                payload = self._request("/api/v1/status/latest")
                text = self._latest_text(payload, action or "all")
        except Exception as exc:
            text = "❌ MaiStatus 查询失败：" + safe(exc, 180)
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2


def create_plugin() -> MaiStatusPlugin:
    return MaiStatusPlugin()
