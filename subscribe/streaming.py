# -*- coding: utf-8 -*-

# 流媒体/AI 解锁检测：通过 clash 外部控制器 127.0.0.1:9090 的 /proxies/{name}/delay
# 接口，配合 expected= 参数，校验目标 URL 在节点出口的实际 HTTP 状态码。
# 仅 status code 检测存在地区误判（YouTube/Disney+/Gemini 等营销页/通用 trace 端点
# 对封锁地区也返回 200），故本模块只保留能真实区分地区的两项：Netflix（两个 title
# 端点状态不同）+ ChatGPT（favicon + engines 双端验证）。其他平台走 ROADMAP 任务 3
# 规划的"切换 selector → mixed-port body 解析"路径，单独迭代。

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import utils
from logger import logger


# 检测顺序：决定输出标签的拼接顺序，与 ROADMAP 任务 3 表头一致
PLATFORMS = ["netflix", "chatgpt"]

# clash delay API 的外层 HTTP 请求超时（秒）
_DELAY_HTTP_TIMEOUT = 10

# 第二阶段 mixed-port 代理请求相关常量
# clash 默认 mixed-port，与 clash.generate_config 里的 "mixed-port": 7890 保持一致
_MIXED_PORT = 7890
# 默认 selector 名（clash.filter_proxies 里 hard-code 的 🌐 Proxy）
_DEFAULT_SELECTOR = "🌐 Proxy"
# 单个 mixed-port 请求超时（秒），单平台 8s 兼顾"够用"和"CI 不拖慢"
_BODY_HTTP_TIMEOUT = 8
# selector 切换后等待生效的 sleep（秒）
_SELECTOR_SLEEP = 0.3
# 仅保留首段（取 - 之前的部分）
_GEMINI_BANNED = {"CHN", "RUS", "BLR", "CUB", "IRN", "PRK", "SYR", "HKG", "MAC"}
_CLAUDE_BANNED = {"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MO", "RU", "SY"}
# 三字码 -> 二字码，覆盖常见解锁地区
_GEMINI_ISO3_TO_ISO2 = {
    "USA": "US", "CAN": "CA", "MEX": "MX", "BRA": "BR", "ARG": "AR",
    "GBR": "GB", "DEU": "DE", "FRA": "FR", "NLD": "NL", "ITA": "IT",
    "ESP": "ES", "SWE": "SE", "NOR": "NO", "FIN": "FI", "DNK": "DK",
    "CHE": "CH", "AUT": "AT", "BEL": "BE", "IRL": "IE", "PRT": "PT",
    "POL": "PL", "CZE": "CZ", "HUN": "HU", "GRC": "GR", "ROU": "RO",
    "JPN": "JP", "KOR": "KR", "CHN": "CN", "TWN": "TW", "HKG": "HK",
    "SGP": "SG", "MYS": "MY", "THA": "TH", "VNM": "VN", "PHL": "PH",
    "IDN": "ID", "IND": "IN", "AUS": "AU", "NZL": "NZ",
    "TUR": "TR", "SAU": "SA", "ARE": "AE", "ISR": "IL", "EGY": "EG",
    "ZAF": "ZA", "RUS": "RU",
}
# 平台检测时使用的桌面浏览器 User-Agent（与 utils.USER_AGENT 区分：mixed-port
# 请求更容易被反爬识别，加 sec-ch-ua 等可提升命中率）
_BODY_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


def _delay_status(proxy_name: str, api_url: str, target: str, expected: int, timeout: int) -> bool:
    """通过 clash delay API 校验目标 URL 状态码是否匹配 expected。返回 True 表示 status == expected 且 delay > 0。"""
    if not proxy_name or not api_url or not target:
        return False

    try:
        encoded_name = urllib.parse.quote(proxy_name, safe="")
        # safe="/" 是 urllib 默认值，保留 URL 路径分隔符，与 clash.check 行为一致
        encoded_target = urllib.parse.quote(target)
        url = (
            f"http://{api_url}/proxies/{encoded_name}/delay"
            f"?timeout={timeout}&url={encoded_target}&expected={expected}"
        )
        content = utils.http_get(url=url, retry=1, timeout=_DELAY_HTTP_TIMEOUT)
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


def _chatgpt(proxy_name: str, api_url: str, timeout: int) -> str:
    if not _delay_status(proxy_name, api_url, "https://chat.openai.com/favicon.ico", 200, timeout):
        return ""
    if not _delay_status(proxy_name, api_url, "https://api.openai.com/v1/engines", 401, timeout):
        return ""
    return "[GPT]"


_CHECKERS = {
    "netflix": _netflix,
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
    """稳定的代理标识：server:port，跨 rename 不变。location.regularize 仅改 name
    字段，不动 server/port，故可作为跨 regularize 前后匹配的稳定 key。"""
    if not proxy or not isinstance(proxy, dict):
        return ""
    return f"{proxy.get('server', '')}:{proxy.get('port', '')}"


def store_tags(proxy: dict, tags: str) -> None:
    """暂存标签到全局表和 proxy 字段。regularize 之后再调用 apply_pending_tags 写入节点名。
    优先用外部表（按 server:port 索引，跨 rename 稳定），proxy 字段作为备份（应对
    process_query_results 的 copy() 后字段丢失或外部表被清空的场景）。"""
    if not proxy or not isinstance(proxy, dict) or not tags:
        return
    key = _proxy_key(proxy)
    if key:
        _PENDING_TAGS[key] = tags
    proxy[_PENDING_TAGS_FIELD] = tags


def apply_pending_tags(proxies: list) -> None:
    """对列表中每个 proxy，按 server:port 查找暂存的标签并追加到当前 name 末尾。
    处理完毕清空暂存表（无论 proxy 是否在列表中），防止字段泄漏到下游 yaml 序列化。
    注：proxy 上的 _streaming_tags 字段无论走哪条路径都会无条件 pop，避免泄漏到 yaml。"""
    if not proxies or not isinstance(proxies, list):
        # 即便没有 proxy，也清空 dict 防止累积
        _PENDING_TAGS.clear()
        return
    try:
        for proxy in proxies:
            if not isinstance(proxy, dict):
                continue
            # 无条件清理字段（即使 dict 路径命中也要删，防止泄漏到 yaml）
            proxy.pop(_PENDING_TAGS_FIELD, None)
            tag = ""
            key = _proxy_key(proxy)
            if key and key in _PENDING_TAGS:
                tag = _PENDING_TAGS.pop(key)
            if tag:
                apply_tags(proxy, tag)
    finally:
        # 兜底清空：未在列表中匹配到的 key 也清掉，避免跨调用残留
        _PENDING_TAGS.clear()


def get_pending_tags_snapshot() -> dict:
    """返回当前 _PENDING_TAGS 的浅拷贝，供第二阶段读取 base_tags 用。
    不清空原表（由 apply_pending_tags 统一清空）。"""
    return dict(_PENDING_TAGS)


def overwrite_pending_tags(merged: dict) -> None:
    """用第二阶段合并后的标签覆盖 _PENDING_TAGS。key=server:port。
    注意：不会清空已有 key（_PENDING_TAGS.apply_pending_tags 会兜底清空）。"""
    if not isinstance(merged, dict):
        return
    for k, v in merged.items():
        if k and v:
            _PENDING_TAGS[k] = v


# ---------- 第二阶段：mixed-port 响应体检测 ----------
# 适用场景：YouTube/TikTok/Gemini/Claude/Disney+ 等平台用 status code 区分不了
# 封锁/未封锁（封锁地区 trace 端点也回 200），必须读响应体提取地区码或关键字。
# 思路：切换 clash selector 🌐 Proxy 到目标节点 → 通过 mixed-port(7890) 代理请求
# 目标 URL → 解析响应体。selector 切换是全局副作用，因此必须按节点串行：一次切换
# 后连续请求所有平台，再切下一个节点。

_BODY_PLATFORMS = ("youtube", "tiktok", "gemini", "claude", "disney")


def _is_body_check_enabled() -> bool:
    return utils.trim(os.environ.get("STREAMING_BODY_CHECK", "false")).lower() in ["true", "1"]


def _body_limit() -> int:
    """去重后限制检测的 IP 数；0 或负数表示不限制。环境变量 STREAMING_BODY_LIMIT。"""
    try:
        return max(0, int(os.environ.get("STREAMING_BODY_LIMIT", "0") or "0"))
    except Exception:
        return 0


def _switch_selector(api_url: str, selector_name: str, node_name: str) -> bool:
    """PUT /proxies/{selector} 切换 selector 到目标节点。返回是否切换成功。"""
    if not api_url or not selector_name or not node_name:
        return False
    try:
        encoded = urllib.parse.quote(selector_name, safe="")
        url = f"http://{api_url}/proxies/{encoded}"
        body = json.dumps({"name": node_name}).encode("utf-8")
        req = urllib.request.Request(
            url=url, data=body, method="PUT", headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=_DELAY_HTTP_TIMEOUT, context=utils.CTX).read()
        return True
    except Exception as e:
        logger.debug(f"switch selector failed, selector: {selector_name}, node: {node_name}, error: {str(e)}")
        return False


def _build_mixed_opener(mixed_port: int) -> urllib.request.OpenerDirector:
    """构造走 mixed-port 代理的局部 opener（不全局 install，避免 clash terminate
    后 7890 关闭导致后续 HTTP 请求走死代理）。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {"http": f"http://127.0.0.1:{mixed_port}", "https": f"http://127.0.0.1:{mixed_port}"}
        ),
        urllib.request.HTTPSHandler(context=utils.CTX),
    )
    return opener


def _fetch_via_mixed(
    mixed_port: int,
    url: str,
    timeout: int = _BODY_HTTP_TIMEOUT,
    headers: dict = None,
    allow_redirects: bool = True,
) -> tuple:
    """通过 mixed-port 代理请求 URL，返回 (status_code, body)。body 解码失败时回退 gzip。
    allow_redirects=False 时仅抓首个响应（含 Location 头），用于 Netflix 地区码检测。"""
    if not url:
        return (0, "")
    opener = _build_mixed_opener(mixed_port)
    req_headers = {"User-Agent": _BODY_USER_AGENT, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    try:
        # 不跟随重定向时用自定义 opener + HTTPRedirectHandler disable
        if not allow_redirects:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def http_error_302(self, req, fp, code, msg, headers):  # noqa: ARG002
                    return fp
                http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

            no_redirect_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(
                    {
                        "http": f"http://127.0.0.1:{mixed_port}",
                        "https": f"http://127.0.0.1:{mixed_port}",
                    }
                ),
                urllib.request.HTTPSHandler(context=utils.CTX),
                _NoRedirect,
            )
            req = urllib.request.Request(url=url, headers=req_headers)
            resp = no_redirect_opener.open(req, timeout=timeout)
            status = resp.getcode()
            raw = resp.read(64 * 1024)
            location = resp.headers.get("Location", "")
        else:
            req = urllib.request.Request(url=url, headers=req_headers)
            resp = opener.open(req, timeout=timeout)
            status = resp.getcode()
            raw = resp.read(256 * 1024)
            location = ""
    except urllib.error.HTTPError as e:
        # 403/451 等"目标存在但拒绝"是判定信号，捕获并保留 body
        try:
            raw = e.read(64 * 1024)
        except Exception:
            raw = b""
        status = e.code
        location = e.headers.get("Location", "") if hasattr(e, "headers") else ""
    except Exception as e:
        logger.debug(f"mixed-port request failed, url: {url}, error: {str(e)}")
        return (0, "")

    # 解码
    try:
        body = raw.decode("utf-8", errors="ignore")
    except Exception:
        try:
            body = gzip.decompress(raw).decode("utf-8", errors="ignore")
        except Exception:
            body = ""

    if not allow_redirects and location:
        # 把 Location 拼到 body 头部，方便正则抓地区码
        body = f"Location: {location}\n{body}"

    return (status, body)


def _extract_region(body: str, patterns: list, upper: bool = True) -> str:
    """按顺序试每个正则，命中即返回（默认转大写）。"""
    if not body or not patterns:
        return ""
    for p in patterns:
        try:
            m = re.search(p, body)
        except Exception:
            m = None
        if m:
            v = m.group(1) or ""
            return v.upper() if upper else v
    return ""


def _youtube_body(mixed_port: int, timeout: int) -> str:
    """YouTube：响应体含 ad-free / spunlimited = 解锁；地区码按优先级试正则。"""
    status, body = _fetch_via_mixed(
        mixed_port,
        "https://www.youtube.com/premium?hl=en",
        timeout=timeout,
    )
    if status in (403, 451):
        return ""
    if "premium is not available in your country" in body or "not available in your country" in body:
        return ""
    if "ad-free" not in body and "spunlimited" not in body and '"browseid":"spunlimited"' not in body:
        return ""
    region = _extract_region(
        body,
        [
            r'"INNERTUBE_CONTEXT_GL"\s*:\s*"([A-Za-z]{2})"',
            r'"GL"\s*:\s*"([A-Za-z]{2})"',
            r'"countryCode"\s*:\s*"([A-Za-z]{2})"',
        ],
    )
    return f"YT-{region}" if region else "YT"


def _tiktok_body(mixed_port: int, timeout: int) -> str:
    """TikTok：cdn-cgi/trace 抓 region；403/451 或 fall back 到首页。"""
    status, body = _fetch_via_mixed(
        mixed_port,
        "https://www.tiktok.com/cdn-cgi/trace",
        timeout=timeout,
    )
    if status in (403, 451):
        return ""
    if not body:
        status, body = _fetch_via_mixed(mixed_port, "https://www.tiktok.com/", timeout=timeout)
        if status in (403, 451):
            return ""
    region = _extract_region(body, [r'"region"\s*:\s*"([a-zA-Z-]+)"'])
    if region:
        # 形如 "us-east" 取首段大写
        region = region.split("-")[0].upper()
        return f"TK-{region}"
    return "TK"


def _gemini_body(mixed_port: int, timeout: int) -> str:
    """Gemini：响应体 ,2,1,200,"XXX" 抓三字码，封禁列表 + 转换为二字码。"""
    status, body = _fetch_via_mixed(mixed_port, "https://gemini.google.com/", timeout=timeout)
    if status in (403, 451):
        return ""
    region3 = _extract_region(body, [r",2,1,200,\"([A-Z]{3})\""])
    if not region3:
        # 部分地区回退：尝试宽松三字码
        region3 = _extract_region(body, [r'"countryCode3"\s*:\s*"([A-Z]{3})"'])
    if not region3 or region3 in _GEMINI_BANNED:
        return ""
    region2 = _GEMINI_ISO3_TO_ISO2.get(region3, "")
    if not region2:
        return ""
    return f"GM-{region2}"


def _claude_body(mixed_port: int, timeout: int) -> str:
    """Claude：cdn-cgi/trace 抓 loc=XX 地区码，封禁列表命中即不可用。"""
    status, body = _fetch_via_mixed(
        mixed_port,
        "https://claude.ai/cdn-cgi/trace",
        timeout=timeout,
    )
    if status in (403, 451):
        return ""
    region = _extract_region(body, [r"loc=([A-Z]{2})"])
    if not region or region in _CLAUDE_BANNED:
        return ""
    return f"CL-{region}"


def _disney_body(mixed_port: int, timeout: int) -> str:
    """Disney+ 简化版：200 视为可访问，403 视为禁。完整三步 token 检测留 TODO。"""
    status, _ = _fetch_via_mixed(mixed_port, "https://www.disneyplus.com/", timeout=timeout)
    if status == 200:
        return "D+"
    if status in (403, 451):
        return ""
    # 其它状态码（重定向到登录页等）也按可访问处理
    return "D+" if status and status < 400 else ""


def _netflix_body(mixed_port: int, timeout: int) -> str:
    """Netflix 地区码升级：抓 title Location 头拿地区码（大写）。仅返回地区码。"""
    _, body = _fetch_via_mixed(
        mixed_port,
        "https://www.netflix.com/title/80018499",
        timeout=timeout,
        allow_redirects=False,
    )
    region = _extract_region(body, [r"/([a-z]{2})/title/"])
    return region.upper() if region else ""


def _chatgpt_body(mixed_port: int, timeout: int) -> str:
    """ChatGPT 地区码升级：抓 cdn-cgi/trace 拿地区码。仅返回地区码。"""
    status, body = _fetch_via_mixed(
        mixed_port,
        "https://chat.openai.com/cdn-cgi/trace",
        timeout=timeout,
    )
    if status in (403, 451):
        return ""
    region = _extract_region(body, [r"loc=([A-Z]{2})"])
    return region if region else ""


_BODY_CHECKERS = {
    "youtube": _youtube_body,
    "tiktok": _tiktok_body,
    "gemini": _gemini_body,
    "claude": _claude_body,
    "disney": _disney_body,
}


def _merge_body_tags(base_tags: str, body: str) -> str:
    """把第二阶段的平台标签合并到第一阶段结果中：Netflix/ChatGPT 用地区码升级，
    其他平台只追加；并保持输出顺序 NF, GPT, YT, TK, GM, CL, D+。
    body 形如 [NF-US][GPT-US][YT-JP][D+]...，每项含完整平台前缀。"""
    base = base_tags or ""
    body = body or ""

    def _find_with_region(text: str, prefix: str) -> str:
        """找 [prefix-XX] 这种带地区码的标签，返回完整带方括号串。"""
        if not prefix or not text:
            return ""
        m = re.search(rf"\[{re.escape(prefix)}([A-Z0-9]{{2,5}})\]", text)
        return m.group(0) if m else ""

    def _find_exact(text: str, token: str) -> str:
        """找 [token] 严格匹配。"""
        if not token or not text:
            return ""
        m = re.search(rf"\[{re.escape(token)}\]", text)
        return m.group(0) if m else ""

    # 1) Netflix 升级：第一阶段标记 (NF/NF*) 缺失则不输出，否则用对应 kind 升级
    nf_kind = ""
    if "[NF*]" in base:
        nf_kind = "NF*"
    elif "[NF]" in base:
        nf_kind = "NF"
    if nf_kind:
        upgraded = _find_with_region(body, "NF-")
        new_tag = upgraded.replace("NF-", f"{nf_kind}-", 1) if upgraded else f"[{nf_kind}]"
    else:
        new_tag = ""

    # 2) ChatGPT 升级：第一阶段标记 [GPT] 缺失则不输出；body 中 [GPT-XX] 统一升为 [GPT+-XX]（Full）
    if "[GPT]" in base:
        upgraded = _find_with_region(body, "GPT-")
        gpt_tag = upgraded.replace("GPT-", "GPT+-", 1) if upgraded else "[GPT]"
    else:
        gpt_tag = ""

    # 3) 其他第二阶段平台
    other_tags = []
    for prefix in ("YT", "TK", "GM", "CL"):
        t = _find_with_region(body, prefix + "-")
        if not t:
            t = _find_exact(body, prefix)
        if t:
            other_tags.append(t)
    dplus = _find_exact(body, "D+")
    if dplus:
        other_tags.append(dplus)

    return new_tag + gpt_tag + "".join(other_tags)


def check_streaming_body(
    proxy_name: str, api_url: str, mixed_port: int, selector_name: str, timeout: int, base_tags: str = ""
) -> str:
    """对单个节点切换 selector → 串行请求所有第二阶段平台 → 返回升级后的标签。
    base_tags 是第一阶段已暂存的标签（[NF]/[NF*]/[GPT]），用于升级地区码。
    任何异常均不影响返回（单个平台失败跳过）。"""
    if not proxy_name or not api_url:
        return ""

    switched = _switch_selector(api_url, selector_name or _DEFAULT_SELECTOR, proxy_name)
    if not switched:
        return ""
    time.sleep(_SELECTOR_SLEEP)

    parts = []
    try:
        region = _netflix_body(mixed_port, timeout)
        if region:
            parts.append(f"NF-{region}")
    except Exception as e:
        logger.debug(f"netflix body failed, proxy: {proxy_name}, error: {str(e)}")

    try:
        region = _chatgpt_body(mixed_port, timeout)
        if region:
            parts.append(f"GPT-{region}")
    except Exception as e:
        logger.debug(f"chatgpt body failed, proxy: {proxy_name}, error: {str(e)}")

    for platform in _BODY_PLATFORMS:
        try:
            checker = _BODY_CHECKERS.get(platform)
            if not checker:
                continue
            tag = checker(mixed_port, timeout)
            if tag:
                parts.append(tag)
        except Exception as e:
            logger.debug(f"{platform} body failed, proxy: {proxy_name}, error: {str(e)}")

    body = "".join(f"[{p}]" for p in parts)
    return _merge_body_tags(base_tags, body)


def check_streaming_body_batch(
    proxies: list, api_url: str, mixed_port: int, selector_name: str, timeout: int, base_tags_map: dict = None
) -> dict:
    """按 server:port 去重后，串行检测每个唯一 IP；第二阶段结果覆盖第一阶段。
    返回 {server:port: merged_tags} 供调用方写入 _PENDING_TAGS。
    base_tags_map: 第一阶段 store_tags 留下的暂存标签（key=server:port），用于升级。
    selector 切换是串行的：每次切换后连续请求所有平台，不重复切换。
    """
    if not proxies or not isinstance(proxies, list):
        return {}

    base_tags_map = base_tags_map if isinstance(base_tags_map, dict) else {}

    # 1) 按 server 去重（流媒体能力按出口 IP 区分，端口不影响）
    ip_to_proxies = {}
    ip_to_name = {}
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        server = str(proxy.get("server", ""))
        port = str(proxy.get("port", ""))
        key = f"{server}:{port}"
        name = utils.trim(str(proxy.get("name", "")))
        if not server or not name:
            continue
        ip_to_proxies.setdefault(key, []).append(proxy)
        ip_to_name.setdefault(key, name)

    unique_keys = list(ip_to_proxies.keys())

    # 2) 可选限制
    limit = _body_limit()
    if limit > 0 and len(unique_keys) > limit:
        unique_keys = unique_keys[:limit]

    results = {}
    selector = selector_name or _DEFAULT_SELECTOR
    for key in unique_keys:
        node_name = ip_to_name[key]
        base_tags = base_tags_map.get(key, "")
        merged = check_streaming_body(
            proxy_name=node_name,
            api_url=api_url,
            mixed_port=mixed_port,
            selector_name=selector,
            timeout=timeout,
            base_tags=base_tags,
        )
        if merged:
            results[key] = merged

    # 3) 复制到同 IP 的其他节点
    for key, group in ip_to_proxies.items():
        tag = results.get(key, "")
        if not tag:
            continue
        for _ in group:
            pass  # 结果按 server:port 索引回写，不就地修改 proxy

    return results
