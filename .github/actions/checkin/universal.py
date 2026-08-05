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
    """V2Board: POST /api/v1/user/checkin with Authorization: Bearer <token>"""
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

    # 无 type 自动检测（兼容旧配置：login 路径含 /api/v1/ 或 /api?scheme= 视为 v2board）
    if not checkin_type:
        checkin_type = "v2board" if "/api/v1/" in login_path or "/api?scheme=" in login_path else "sspanel"

    print("start to checkin, domain: {}\ttype: {}".format(domain, checkin_type))
    login_url = domain + login_path
    checkin_url = domain + checkin_path
    headers["origin"] = domain
    headers["referer"] = login_url

    if checkin_type == "v2board":
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

    return True


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
