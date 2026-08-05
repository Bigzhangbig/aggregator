# Patchright 人机验证绕过集成计划

## 背景与目标

aggregator 遇 Cloudflare Turnstile/reCAPTCHA/5s 盾直接跳过：
- `airport.py:444-450`：注册遇 `rr.recaptcha` -> `self.available = False; return "", ""`
- `crawl.py:1859`：爬虫遇 `rr.recaptcha` -> skip
- `crawl.py:1318`：遇 `e.code in [403, 503]` -> 当前直接放弃

导致大量启用人机验证的机场被丢弃。Patchright（反检测 Playwright）能自动过 Turnstile/5s 盾。

**目标**：HTTP 优先 + Patchright 降级双轨模式，遇人机验证不再跳过，而是用 Patchright 求解后继续。

## 架构：双轨降级

```
urllib 请求 ──200 OK──► 继续常规流程
            │
            └─403/503/recaptcha──► Patchright 求解 ──► 拿 Cookie/Token ──► 回传 urllib 继续
```

## 阶段1：基础设施（patchright_driver.py + Docker + CI）

### 1a. 新建 `subscribe/patchright_driver.py`

异步求解池 + 同步接口（桥接 aggregator 的多线程同步代码）。

**关键设计**：aggregator 用 ThreadPoolExecutor（同步），Patchright 是 asyncio。用独立线程跑事件循环，主线程通过 `run_coroutine_threadsafe` 提交任务，线程安全。

```python
import asyncio, threading
from patchright.async_api import async_playwright

class _PatchrightPool:
    def __init__(self, max_concurrency=4):
        self._max = max_concurrency
        self._sem = None
        self._pw = None
        self._browser = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()  # 阻塞至 Chromium 启动完成

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init())
        self._ready.set()
        self._loop.run_forever()

    async def _init(self):
        self._sem = asyncio.Semaphore(self._max)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )

    async def _solve(self, url, timeout=25):
        async with self._sem:
            ctx = await self._browser.new_context()
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
                await asyncio.sleep(2.5)  # 留时间给 Turnstile 自动完成
                cookies = await ctx.cookies()
                return {"success": True,
                        "cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
                        "content": await page.content()}
            except Exception as e:
                return {"success": False, "error": str(e)}
            finally:
                await page.close()
                await ctx.close()

    def solve_sync(self, url, timeout=25):
        fut = asyncio.run_coroutine_threadsafe(self._solve(url, timeout), self._loop)
        return fut.result(timeout=timeout+15)

_pool = None

def solve_challenge(url, timeout=25):
    """同步接口：供 aggregator 多线程代码调用。惰性初始化单例池。"""
    global _pool
    if _pool is None:
        _pool = _PatchrightPool()
    return _pool.solve_sync(url, timeout)
```

- 单 Chromium 实例 + 多 Context（信号量并发 4，报告推荐）
- 惰性初始化（首次调用才启动 Chromium，不影响不用 Patchright 的流程）
- 模块级单例，多线程安全

### 1b. requirements.txt 加 patchright

```
patchright
```

### 1c. Dockerfile 装 Chromium 依赖

```dockerfile
# 系统依赖：Chromium 运行时依赖（patchright install --with-deps 装的）
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq curl \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

# 装 patchright + Chromium
RUN pip install --no-cache-dir patchright \
    && patchright install chromium --with-deps
```

**风险点**：多架构（amd64+arm64）构建。Chromium arm64 支持需验证。若 arm64 失败，docker.yml 临时退回 amd64-only。

### 1d. docker.yml 缓存 Patchright 浏览器

```yaml
- name: Cache Patchright Browsers
  uses: actions/cache@v4
  with:
    path: ~/.cache/patchright
    key: ${{ runner.os }}-patchright-${{ hashFiles('requirements.txt') }}
```

## 阶段2：crawl.py 爬虫降级（遇 403/503）

**场景**：爬虫遇 Cloudflare 5s 盾（403/503），用 Patchright 过盾拿 Cookie，重试 HTTP。

改 `crawl.py:1318` 附近：
```python
if not expired and e.code in [403, 503]:
    # 新增：尝试 Patchright 降级
    try:
        from patchright_driver import solve_challenge
        result = solve_challenge(url)
        if result.get("success"):
            cookie = result["cookie"]
            # 带 Cookie 重试 HTTP 请求
            headers["cookie"] = cookie
            return utils.http_get(url, headers=headers, ...)
    except Exception:
        pass
    # Patchright 失败则原逻辑
```

**复杂度**：低。只拿 Cookie 重试，不操作 DOM。

## 阶段3：airport.py 注册降级（遇 recaptcha）

**场景**：注册遇 Turnstile，用 Patchright 完成注册（填表单 + 过验证 + 提交）。

改 `airport.py:444-450`：当 `chuck and rr.recaptcha` 时不跳过，调用 Patchright 注册。

**复杂度**：高。需要 Patchright 操作 DOM：
1. 打开注册页
2. 填 email/password/invite_code
3. 等 Turnstile 自动完成
4. 点提交
5. 解析响应拿订阅链接

不同面板（V2Board/SSPanel）注册页 DOM 不同，需适配。

**建议**：阶段3作为后续迭代，先做阶段1+2 验证基础设施。

## 阶段4：数据中心 IP 风控应对

GitHub Actions IP 属于 Azure 数据中心，Cloudflare 可能要求额外交互。
**方案**：Patchright 访问时挂代理（用 aggregator 已提取的可用节点）。`new_context(proxy=...)`。

可选，先不实施，观察阶段2的成功率再决定。

## 风险与应对

| 风险 | 应对 |
|------|------|
| 异步/同步桥接复杂 | 独立线程事件循环 + run_coroutine_threadsafe（已设计） |
| Docker arm64 Chromium | 先验证，失败则退 amd64-only |
| GitHub Actions 2核 CPU | 并发 4（报告推荐），信号量控制 |
| 数据中心 IP 风控 | 阶段4 挂代理（可选） |
| Chromium 镜像体积增大 | 缓存 + 多阶段构建 |
| 注册流程 DOM 适配 | 阶段3 后续迭代，先做爬虫降级 |

## 实施顺序

1. **阶段1**（基础设施）：patchright_driver.py + Dockerfile + requirements + CI 缓存
2. **阶段2**（爬虫降级）：crawl.py 遇 403/503 调用 Patchright
3. **验证**：collect 触发，确认能爬到之前 403 的源
4. **阶段3**（注册降级）：airport.py 遇 recaptcha 用 Patchright 注册（后续迭代）

## 验证方法

1. 本地：`python -c "from patchright_driver import solve_challenge; print(solve_challenge('https://nowsecure.nl'))"` 测试过盾
2. CI：collect 触发，对比节点数量（之前 403 跳过的源现在能爬）
3. Docker：镜像构建成功，容器内 patchright install chromium 正常
