#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Patchright 人机验证求解池。

提供同步接口 solve_challenge(url)，供 aggregator 多线程同步代码调用。
内部用独立线程跑 asyncio 事件循环，单 Chromium 实例 + 多 Context，信号量控制并发。
惰性初始化：首次调用 solve_challenge 才启动 Chromium，不影响不用 Patchright 的流程。

详见 docs/PATCHRIGHT_INTEGRATION_PLAN.md
"""

import asyncio
import os
import threading

from logger import logger

# patchright 是 Playwright 的反检测修改版，API 兼容
try:
    from patchright.async_api import async_playwright
except ImportError:
    async_playwright = None
    logger.warning("patchright not installed, challenge solving disabled")


class _PatchrightPool:
    """单 Chromium 实例 + 多 Context 求解池，独立线程跑事件循环。"""

    def __init__(self, max_concurrency: int = 4):
        if async_playwright is None:
            raise RuntimeError("patchright not installed")

        self._max = max_concurrency
        self._sem = None
        self._pw = None
        self._browser = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._init_error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="patchright-loop")
        self._thread.start()
        # 阻塞至 Chromium 启动完成或失败
        self._ready.wait()
        if self._init_error:
            raise RuntimeError(f"failed to init patchright: {self._init_error}")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init())
        except Exception as e:
            self._init_error = e
        finally:
            self._ready.set()
        if self._init_error is None:
            self._loop.run_forever()

    async def _init(self):
        self._sem = asyncio.Semaphore(self._max)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",  # GitHub Actions / Docker 必需，防止共享内存不足崩溃
                "--disable-gpu",
            ],
        )
        logger.info(f"patchright chromium launched, max_concurrency={self._max}")

    async def _solve(self, url: str, timeout: int = 25) -> dict:
        async with self._sem:
            context = await self._browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # 留 2.5 秒供 Cloudflare Turnstile / 5s 盾自动完成
                await asyncio.sleep(5)  # 留 5 秒供 Cloudflare 5s 盾 / Turnstile 自动完成
                cookies = await context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                return {
                    "success": True,
                    "cookie": cookie_str,
                    "content": await page.content(),
                }
            except Exception as e:
                return {"success": False, "error": str(e)}
            finally:
                await page.close()
                await context.close()

    def solve_sync(self, url: str, timeout: int = 25) -> dict:
        """提交求解任务到事件循环线程，阻塞等待结果。"""
        future = asyncio.run_coroutine_threadsafe(self._solve(url, timeout), self._loop)
        return future.result(timeout=timeout + 15)


_pool = None
_pool_lock = threading.Lock()


def is_available() -> bool:
    """检查 patchright 是否可用（包已安装）。不启动 Chromium，无副作用。"""
    return async_playwright is not None


def solve_challenge(url: str, timeout: int = 25) -> dict:
    """同步接口：供 aggregator 多线程代码调用。

    惰性初始化单例池（线程安全）。返回 {"success": bool, "cookie": str, "content": str, "error": str}。
    若 patchright 未安装或初始化失败，返回 {"success": False, "error": "..."}，不抛异常。
    """
    global _pool
    if async_playwright is None:
        return {"success": False, "error": "patchright not installed"}

    with _pool_lock:
        if _pool is None:
            try:
                max_concurrency = int(os.environ.get("PATCHRIGHT_MAX_CONCURRENCY", "4"))
                _pool = _PatchrightPool(max_concurrency=max_concurrency)
            except Exception as e:
                logger.error(f"failed to init patchright pool: {e}")
                return {"success": False, "error": str(e)}

    try:
        return _pool.solve_sync(url, timeout)
    except Exception as e:
        logger.error(f"patchright solve_challenge failed, url={url}, error={e}")
        return {"success": False, "error": str(e)}
