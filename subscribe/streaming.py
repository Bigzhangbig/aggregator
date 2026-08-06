# -*- coding: utf-8 -*-

import json
import os
import urllib.parse

import utils
from logger import logger


# 检测顺序：决定输出标签的拼接顺序
# 规范：NF, GPT, YT, TK, GM, CL, D+
PLATFORMS = ["netflix", "chatgpt", "youtube", "tiktok", "gemini", "claude", "disney"]


def _delay_status(proxy_name: str, api_url: str, target: str, expected: int, timeout: int) -> bool:
    """通过 clash delay API 校验目标 URL 状态码是否匹配 expected。返回 True 表示 status == expected 且 delay > 0。"""
    if not proxy_name or not api_url or not target:
        return False

    try:
        encoded_name = urllib.parse.quote(proxy_name, safe="")
        # 保持 / 不编码（与 clash.check 行为一致），但 : 仍会被编码
        encoded_target = urllib.parse.quote(target)
        url = (
            f"http://{api_url}/proxies/{encoded_name}/delay"
            f"?timeout={timeout}&url={encoded_target}&expected={expected}"
        )
        content = utils.http_get(url=url, retry=1, timeout=10)
        if not content:
            return False
        data = json.loads(content)
        return data.get("delay", -1) > 0
    except Exception:
        return False


def _netflix(proxy_name: str, api_url: str, timeout: int) -> str:
    if _delay_status(proxy_name, api_url, "https://www.netflix.com/title/81280792", 200, timeout):
        return "[NF]"
    if _delay_status(proxy_name, api_url, "https://www.netflix.com/title/70143836", 200, timeout):
        return "[NF*]"
    return ""


def _youtube(proxy_name: str, api_url: str, timeout: int) -> str:
    if _delay_status(proxy_name, api_url, "https://www.youtube.com/premium", 200, timeout):
        return "[YT]"
    return ""


def _tiktok(proxy_name: str, api_url: str, timeout: int) -> str:
    if _delay_status(proxy_name, api_url, "https://www.tiktok.com/cdn-cgi/trace", 200, timeout):
        return "[TK]"
    return ""


def _gemini(proxy_name: str, api_url: str, timeout: int) -> str:
    if _delay_status(proxy_name, api_url, "https://gemini.google.com/", 200, timeout):
        return "[GM]"
    return ""


def _claude(proxy_name: str, api_url: str, timeout: int) -> str:
    if _delay_status(proxy_name, api_url, "https://claude.ai/cdn-cgi/trace", 200, timeout):
        return "[CL]"
    return ""


def _disney(proxy_name: str, api_url: str, timeout: int) -> str:
    if _delay_status(proxy_name, api_url, "https://www.disneyplus.com/", 200, timeout):
        return "[D+]"
    return ""


def _chatgpt(proxy_name: str, api_url: str, timeout: int) -> str:
    if not _delay_status(proxy_name, api_url, "https://chat.openai.com/favicon.ico", 200, timeout):
        return ""
    if not _delay_status(proxy_name, api_url, "https://api.openai.com/v1/engines", 401, timeout):
        return ""
    return "[GPT]"


_CHECKERS = {
    "netflix": _netflix,
    "youtube": _youtube,
    "tiktok": _tiktok,
    "gemini": _gemini,
    "claude": _claude,
    "disney": _disney,
    "chatgpt": _chatgpt,
}


def _check_one(platform: str, proxy_name: str, api_url: str, timeout: int) -> str:
    func = _CHECKERS.get(platform)
    if not func:
        return ""
    try:
        return func(proxy_name, api_url, timeout)
    except Exception as e:
        logger.debug(f"streaming check failed, platform: {platform}, proxy: {proxy_name}, error: {str(e)}")
        return ""


def check_streaming(proxy: dict, api_url: str, timeout: int = 5000) -> str:
    """对单个节点并发检测所有平台，返回拼接后的标签字符串（如 "[NF][GPT]"），无解锁返回空串。
    受环境变量 SKIP_STREAMING_CHECK 控制（true/1 时跳过）。"""
    if not proxy or not isinstance(proxy, dict):
        return ""

    if utils.trim(os.environ.get("SKIP_STREAMING_CHECK", "false")).lower() in ["true", "1"]:
        return ""

    proxy_name = utils.trim(proxy.get("name", ""))
    if not proxy_name:
        return ""

    tasks = [[p, proxy_name, api_url, timeout] for p in PLATFORMS]
    results = utils.multi_thread_run(func=_check_one, tasks=tasks, num_threads=len(tasks))
    return "".join(t for t in results if t)


def apply_tags(proxy: dict, tags: str) -> None:
    """将标签字符串追加到节点名末尾（原地修改）。"""
    if not proxy or not isinstance(proxy, dict) or not tags:
        return
    proxy["name"] = f"{proxy.get('name', '')}{tags}"


# 跨 regularize 的临时标签存储：用 server:port 索引，regularize 重写 name 后仍能找回
_PENDING_TAGS: dict[str, str] = {}
_PENDING_TAGS_FIELD = "_streaming_tags"


def _proxy_key(proxy: dict) -> str:
    """稳定的代理标识：server:port，跨 rename 不变。"""
    if not proxy or not isinstance(proxy, dict):
        return ""
    return f"{proxy.get('server', '')}:{proxy.get('port', '')}"


def store_tags(proxy: dict, tags: str) -> None:
    """暂存标签到全局表和 proxy 字段。regularize 之后再调用 apply_pending_tags 写入节点名。
    优先用外部表，proxy 字段作为备份（应对 process_query_results 的 copy()）。"""
    if not proxy or not isinstance(proxy, dict) or not tags:
        return
    key = _proxy_key(proxy)
    if key:
        _PENDING_TAGS[key] = tags
    proxy[_PENDING_TAGS_FIELD] = tags


def apply_pending_tags(proxies: list) -> None:
    """对列表中每个 proxy，按 server:port 查找暂存的标签并追加到当前 name 末尾。
    调用一次后清空暂存表。"""
    if not proxies or not isinstance(proxies, list):
        return
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        tags = ""
        key = _proxy_key(proxy)
        if key and key in _PENDING_TAGS:
            tags = _PENDING_TAGS.pop(key)
        elif proxy.get(_PENDING_TAGS_FIELD):
            tags = proxy.pop(_PENDING_TAGS_FIELD)
        if tags:
            apply_tags(proxy, tags)
