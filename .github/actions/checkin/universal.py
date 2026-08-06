#!/usr/bin/env python
# -*- coding: utf-8 -*-

# @Author  : wzdnzd
# @Time    : 2018-04-25

import re
import warnings
import urllib
import urllib.request
import urllib.parse
import multiprocessing
import os
import ssl
import json
import sys
import base64

warnings.filterwarnings("ignore")

HEADER = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.5060.53 Safari/537.36 Edg/103.0.1264.37",
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "zh-CN,zh;q=0.9",
    "dnt": "1",
    "Connection": "keep-alive",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PATH = os.path.abspath(os.path.dirname(__file__))


def extract_domain(url) -> str:
    if not url or not re.match(
        r"^(https?://(([a-zA-Z0-9]+-?)+\.)+[a-zA-Z]+)(:\d+)?(/.*)?(\?.*)?(#.*)?$", url
    ):
        return ""

    start = url.find("//")
    if start == -1:
        start = -2

    end = url.find("/", start + 2)
    if end == -1:
        end = len(url)

    return url[:end]


def login(url, params, headers, retry) -> str:
    try:
        data = urllib.parse.urlencode(params).encode(encoding="UTF8")

        request = urllib.request.Request(url, data=data, headers=headers, method="POST")

        response = urllib.request.urlopen(request, timeout=10, context=CTX)
        print(response.read().decode("unicode_escape"))

        if response.getcode() == 200:
            return response.getheader("Set-Cookie")

        return ""

    except Exception as e:
        print(str(e))
        retry -= 1

        if retry > 0:
            return login(url, params, headers, retry)

        print("[LoginError] URL: {}".format(extract_domain(url)))
        return ""


def checkin(url, headers, retry) -> None:
    try:
        request = urllib.request.Request(url, headers=headers, method="POST")

        response = urllib.request.urlopen(request, timeout=10, context=CTX)
        data = response.read().decode("unicode_escape")
        print(
            "[CheckInFinished] URL: {}\t\tResult:{}".format(extract_domain(url), data)
        )

    except Exception as e:
        print(str(e))
        retry -= 1

        if retry > 0:
            checkin(url, headers, retry)

        print("[CheckInError] URL: {}".format(extract_domain(url)))


def login_v2board(url, params, headers, retry) -> tuple:
    """V2Board: POST /api/v1/passport/auth/login -> {"data":{"token","auth_data"}} + Set-Cookie"""
    try:
        data = urllib.parse.urlencode(params).encode(encoding="UTF8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        response = urllib.request.urlopen(request, timeout=10, context=CTX)
        if response.getcode() == 200:
            body = json.loads(response.read().decode("unicode_escape"))
            data_dict = body.get("data", {})
            token = ""
            if isinstance(data_dict, dict):
                token = data_dict.get("auth_data") or data_dict.get("token", "")
            cookie = response.getheader("Set-Cookie") or ""
            if token or cookie:
                return token, cookie
        return "", ""
    except Exception as e:
        print(str(e))
        retry -= 1
        if retry > 0:
            return login_v2board(url, params, headers, retry)
        print("[LoginError] URL: {}".format(extract_domain(url)))
        return "", ""


def checkin_v2board(url, headers, token, retry) -> None:
    """V2Board: POST /api/v1/user/checkin with Authorization: Bearer <token>

    注意：V2Board/Xboard 原版无签到功能（源码确认 PassportRoute/UserRoute），
    此分支兼容第三方签到插件，预期大部分 V2Board 机场会 404。
    有签到的面板见 SSPanel(/user/checkin) 和 ProxyPanel(/api/v1/doCheckIn) 分支。
    """
    try:
        if token:
            headers["Authorization"] = "Bearer {}".format(token)
        request = urllib.request.Request(url, headers=headers, method="POST")
        response = urllib.request.urlopen(request, timeout=10, context=CTX)
        data = response.read().decode("unicode_escape")
        print("[CheckInFinished] URL: {}\t\tResult:{}".format(extract_domain(url), data))
    except Exception as e:
        print(str(e))
        retry -= 1
        if retry > 0:
            checkin_v2board(url, headers, token, retry)
        else:
            print("[CheckInError] URL: {}".format(extract_domain(url)))


def login_proxypanel(url, params, headers, retry) -> str:
    """ProxyPanel: POST /api/v1/login -> {data:{token}}（Laravel Sanctum Bearer）"""
    try:
        data = urllib.parse.urlencode(params).encode(encoding="UTF8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        response = urllib.request.urlopen(request, timeout=10, context=CTX)
        if response.getcode() == 200:
            body = json.loads(response.read().decode("unicode_escape"))
            return body.get("data", {}).get("token", "")
        return ""
    except Exception as e:
        print(str(e))
        retry -= 1
        if retry > 0:
            return login_proxypanel(url, params, headers, retry)
        print("[LoginError] URL: {}".format(extract_domain(url)))
        return ""


def checkin_proxypanel(url, headers, token, retry) -> None:
    """ProxyPanel: POST /api/v1/doCheckIn with Authorization: Bearer <token>"""
    try:
        headers["Authorization"] = "Bearer {}".format(token)
        request = urllib.request.Request(url, headers=headers, method="POST")
        response = urllib.request.urlopen(request, timeout=10, context=CTX)
        data = response.read().decode("unicode_escape")
        print("[CheckInFinished] URL: {}\t\tResult:{}".format(extract_domain(url), data))
    except Exception as e:
        print(str(e))
        retry -= 1
        if retry > 0:
            checkin_proxypanel(url, headers, token, retry)
        else:
            print("[CheckInError] URL: {}".format(extract_domain(url)))


def get_cookie(text) -> str:
    regex = "(__cfduid|uid|email|key|ip|expire_in)=(.+?);"
    if not text:
        return ""

    content = re.findall(regex, text)
    cookie = ";".join(["=".join(x) for x in content]).strip()

    return cookie


def config_load(filename) -> dict:
    if not os.path.exists(filename) or os.path.isdir(filename):
        return None

    config = open(filename, "r").read()
    return json.loads(config)


def flow(domain, params, headers) -> bool:
    domain = extract_domain(domain.strip())
    if not domain:
        print("cannot checkin because domain is invalidate")
        return False

    login_path = params.get("login", "/auth/login")
    checkin_path = params.get("checkin", "/user/checkin")
    checkin_type = params.get("type", "")

    # 无 type 自动检测（兼容旧配置）
    if not checkin_type:
        if login_path == "/api/v1/login" or login_path.endswith("/doCheckIn"):
            checkin_type = "proxypanel"
        elif "/api/v1/" in login_path or "/api?scheme=" in login_path:
            checkin_type = "v2board"
        else:
            checkin_type = "sspanel"

    print("start to checkin, domain: {}\ttype: {}".format(domain, checkin_type))
    login_url = domain + login_path
    checkin_url = domain + checkin_path
    headers["origin"] = domain
    headers["referer"] = login_url

    if checkin_type in ("v2board", "proxypanel"):
        user_info = {"email": params.get("email", ""), "password": params.get("passwd", "")}
    else:
        user_info = {"email": params.get("email", ""), "passwd": params.get("passwd", "")}

    if checkin_type == "v2board":
        token, cookie = login_v2board(login_url, user_info, headers, 3)
        if not token:
            return False
        if cookie:
            headers["cookie"] = cookie
        checkin_v2board(checkin_url, headers, token, 3)
    elif checkin_type == "proxypanel":
        token = login_proxypanel(login_url, user_info, headers, 3)
        if not token:
            return False
        checkin_proxypanel(checkin_url, headers, token, 3)
    else:
        text = login(login_url, user_info, headers, 3)
        if not text:
            return False
        cookie = get_cookie(text)
        if len(cookie) <= 0:
            return False
        headers["referer"] = domain + "/user"
        headers["cookie"] = cookie
        checkin(checkin_url, headers, 3)

    # 签到成功后尝试续期（仅 v2board，renewal.add_traffic_flow 用 V2Board API）
    if checkin_type == "v2board":
        try_renew(domain, params)

    return True


def try_renew(domain: str, params: dict) -> None:
    """签到成功后对 V2Board 机场尝试续期/重置流量。

    开关：ENABLE_RENEW=true/1/yes 启用（默认关闭，不影响现有签到）。
    续期失败仅 print 异常，不抛回 flow()（签到结果不受影响）。
    renewal.add_traffic_flow 的 email/passwd 需 base64 编码（checkin-config.json 是明文）。
    """
    flag = os.environ.get("ENABLE_RENEW", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return

    try:
        # 推导项目根目录: .github/actions/checkin/universal.py -> 4 级 dirname -> <root>
        root = os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        )
        if root and root not in sys.path:
            sys.path.insert(0, root)
        from subscribe.renewal import add_traffic_flow
    except Exception as e:
        print(f"[RenewError] import renewal failed: {type(e).__name__}: {e}")
        return

    try:
        email_b64 = base64.b64encode(str(params.get("email", "")).encode("utf-8")).decode("ascii")
        passwd_b64 = base64.b64encode(str(params.get("passwd", "")).encode("utf-8")).decode("ascii")
        renew_params = {
            "email": email_b64,
            "passwd": passwd_b64,
            "api_prefix": params.get("api_prefix", "/api/v1/"),
            "coupon_code": params.get("coupon_code", "") or "",
            "enable": bool(params.get("enable_renew", True)),
        }
        sub_url = add_traffic_flow(domain, renew_params, jsonify=False)
        if sub_url:
            print(f"[RenewFinished] domain: {domain}\tsub: {sub_url}")
        else:
            print(f"[RenewSkipped] domain: {domain}\tno sub_url returned (skip or fail)")
    except Exception as e:
        print(f"[RenewError] domain: {domain}\t{type(e).__name__}: {e}")


def wrapper(args) -> bool:
    return flow(args.get("domain", ""), args.get("param", {}), HEADER)


def main() -> None:
    config = config_load(os.path.join(PATH, "config.json"))
    params = config.get("domains", [])

    cpu_count = multiprocessing.cpu_count()
    num = len(params) if len(params) <= cpu_count else cpu_count

    pool = multiprocessing.Pool(num)
    pool.map(wrapper, params)
    pool.close()


if __name__ == "__main__":
    main()
