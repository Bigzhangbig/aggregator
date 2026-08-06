# Aggregator 功能增强路线图

> 5 个增强任务。基于现有代码调研 + subs-check 流媒体检测调研结论制定。

---

## 任务 1：节点测速排序（已弃）

> **弃用原因**：GitHub Action 运行在海外机房，测出的延迟不能代表国内用户的实际网络体验，按延迟排序无意义。若未来有国内节点执行环境可重新启用。

### 现状
- `clash.check(proxy, api_url, timeout, test_url, delay, strict) -> bool`（`subscribe/clash.py:778`）
- 内部已调用 mihomo 外部控制器 `GET /proxies/{name}/delay?timeout=&url=` 获取延迟值，但只返回 `bool`（alive/dead），**丢弃了 delay 数值**
- `process.py:609` 和 `collect.py:401` 并发检测后仅过滤 alive，不排序
- `filter_proxies`（`clash.py:67`）最终还 `random.shuffle` 打乱顺序

### 方案
1. **改 `clash.check` 返回 `int`**：dead 返回 `0`，alive 返回最后一次成功测试的 delay 值（ms）
   - 现有 `if masks[i]` 判断完全兼容（`0=False`，正数=`True`），调用方零改动即可保持原过滤行为
   - 在 `check` 内部循环 targets 时记录 `last_delay = data.get("delay", 0)`，`return last_delay if alive else 0`
   - 注意 chatgpt 检测分支（`proxy.pop("chatgpt")`）在返回 delay 前完成，互不干扰
2. **聚合处加排序**：
   - `process.py:621`：`availables = [checks[i] for i in ... if masks[i]]` 改为按 `masks[i]` 升序
   - `collect.py:413`：同上
   - 写法：`pairs = sorted([(checks[i], masks[i]) for i in range(len(checks)) if masks[i]], key=lambda x: x[1])`，取 `[p for p,_ in pairs]`
3. **可选增强**：节点名加延迟后缀（如 `香港 01 |120ms|`），受 `SKIP_REMARK` 控制

### 改动文件
- `subscribe/clash.py`（`check` 返回值类型 bool->int）
- `subscribe/process.py`（`aggregate` 排序）
- `subscribe/collect.py`（`aggregate` 排序）

### 风险
- `check` 被 2 处调用，均已定位；返回类型从 bool->int 向后兼容
- delay 测试受网络波动影响，可考虑取多次中位数（可选，后续优化）

---

## 任务 2：更多面板 + 签到（暂缓，待 agent 调研）

> **暂缓原因**：PMPanel 等面板的注册/签到 API 字段未确认，需派 agent 调研后再实现。

### 现状
- 已支持 V2Board / SSPanel / ProxyPanel 三类（`universal.py` 按 `panel_type` 分流）
- **Xboard** 兼容 V2Board API（`/api/v1/passport/auth/login`），但 V2Board 原版**无签到端点**（源码已确认），Xboard 二开版**可能**加了 `/api/v1/user/checkin`
- **PMPanel** 未支持：`/api/v1/auth/login` + `/api/v1/user/checkin`，Bearer Token

### 方案
1. **PMPanel handler**：`universal.py` 新增 `login_pmpanel` + `checkin_pmpanel`（Bearer，端点 `/api/v1/auth/login` + `/api/v1/user/checkin`）
2. **v2board 签到前探测**：登录后先 `GET /user/checkin`（或 OPTIONS），404/405 则标记无签到、跳过（减少无效请求与日志噪音）。当前 238 条 v2board 全失败即因原版无签到端点
3. **collect 注册分流**：`workflow.py execute()` 加 PMPanel 检测（嗅探 `/api/v1/auth/login`），`scaner.py` 加 `getsub_pmpanel`（注册+登录+取 subUrl）
4. **`_collect_checkin_entries`** 加 `panel_type == "pmpanel"` 分支

### 改动文件
- `.github/actions/checkin/universal.py`（PMPanel handler + v2board 探测）
- `subscribe/collect.py`（`_collect_checkin_entries`）
- `subscribe/workflow.py`（execute 分流）
- `subscribe/scripts/scaner.py`（PMPanel 注册）
- `subscribe/airport.py`（`ispmpanel` 嗅探函数）

### 风险
- PMPanel 注册 API 字段未确认，需先调研（参考已有 ProxyPanel 实现）
- v2board 探测增加 1 次请求/站点，可接受

---

## 任务 3：流媒体 / AI 解锁检测（P1）

### 现状
- `clash.check` 有简易 chatgpt 检测（`proxy.pop("chatgpt")` 后测 `chat.openai.com/favicon.ico` + `api.openai.com/v1/engines`），仅判通/不通
- 无 Netflix / Disney+ / YouTube / TikTok 等流媒体解锁检测

### 方案（基于 subs-check 调研结论）

#### 检测机制：两种路径适配 aggregator
aggregator 已在跑 clash 二进制（外部控制器 `127.0.0.1:9090`），复用现有机制：
1. **轻量状态码检测**：`GET /proxies/{name}/delay?url={target}&expected={code}` - 只判状态码，无需切换节点，并发友好。这是现有 chatgpt 检测的扩展模式
2. **完整响应体检测**：切换 clash selector 到目标节点（`PUT /proxies/{selector}` body `{"name": node}`），通过 `mixed-port` 代理请求拿响应体关键字。适用于需解析 body 的检测

> subs-check 自己起 http.Client + mihomo 出口（`proxy.DialContext`）统计流量，不用 delay API。aggregator 已有 clash 控制器，复用 delay API 做状态码检测成本最低；响应体检测走 mixed-port 代理。

#### 检测项 + URL + 判定逻辑（来自 subs-check）

| 平台 | 检测 URL | 判定逻辑 | 标签 |
|------|----------|----------|------|
| **Netflix** | `GET /title/81280792`(非自制) + `/title/70143836`(自制) | 403=禁；非自制 200=Full；非自制 404+自制 200=OriginalsOnly。地区码从 `/title/80018499` 重定向 Location 抓 | `NF-US` / `NF`(仅自制) |
| **Disney+** | 三步 token：`POST disney.api.edge.bamgrid.com/devices` -> `/token` -> `/graph/v1/device/graphql` | 403=禁；graphql 提取 `countryCode` + `inSupportedLocation`；JP 强制解锁 | `D+-US` |
| **ChatGPT** | `GET api.openai.com/compliance/cookie_requirements` + `ios.chat.openai.com` | 否定关键字 `unsupported_country`/`vpn`/`disallowed isp`。双端通过=Full，单端=Web。地区从 `chat.openai.com/cdn-cgi/trace` 的 `loc=` 抓 | `GPT+-US` / `GPT-US` |
| **Gemini** | `GET gemini.google.com/` | 正则 `,2,1,200,"([A-Z]{3})"` 抓三字码，封禁列表 CHN/RUS/BLR/CUB/IRN/PRK/SYR/HKG/MAC | `GM-US` |
| **Claude** | `GET claude.ai/cdn-cgi/trace` | `loc=([A-Z]{2})`，封禁列表 AF/BY/CN/CU/HK/IR/KP/MO/RU/SY | `CL-US` |
| **YouTube** | `GET /premium?hl=en` | 关键字 `ad-free`/`browseid":"spunlimited`=解锁；`premium is not available`=禁。地区从 `INNERTUBE_CONTEXT_GL` 等多个正则择优 | `YT-US` |
| **TikTok** | `GET /cdn-cgi/trace` | 403/451=禁；`"region":"([a-zA-Z-]+)"` 抓地区；失败回退主页 | `TK-US` |
| **Spotify** | `GET /api/content/v1/country-selector?platform=web&format=json` | 403/451=禁；重定向 path 或 body `countryCode` 抓地区 | `SP-US` |

#### 集成方式
- `clash.check` 活性确认后串行调用 streaming 检测（或独立阶段）
- 结果标注到节点名：`香港 01 [NF-US][GPT-US]`，受 `SKIP_REMARK` 控制
- 环境变量 `SKIP_STREAMING_CHECK` 开关，默认可关（避免拖慢 CI）

### 改动文件
- `subscribe/streaming.py`（新增 - 各平台检测函数 + 标签生成 + 并发调度）
- `subscribe/clash.py`（`generate_config` 确保 mixed-port 开启；`check` 集成检测入口）
- `subscribe/process.py` / `subscribe/collect.py`（调用 streaming 检测）

### 设计取舍与风险
- 流媒体检测显著增加流水线时间（每节点 8 平台），**必须**并发 + 超时（单平台 10s）+ 可开关
- 响应体检测需切换 selector，影响并发 - 考虑独立 clash 实例或串行子阶段
- Disney+ 三步 token 流程复杂，可降级为仅状态码检测（403=禁）

---

## 任务 4：更多协议支持（P2）

### 现状
- `clash.py verify()` 已支持：ss / ssr / vmess / vless / trojan / snell / tuic / hysteria / hysteria2 / anytls
- **VLESS-Reality 已支持**（`reality-opts` 验证 + `flow=xtls-rprx-vision`，`clash.py:601`）
- `proxies_exists`（`clash.py:137`）去重按协议字段匹配
- **缺**：WireGuard、ShadowTLS

### 方案
1. **WireGuard**：
   - `verify()` 加 `type == "wireguard"` 分支：校验 `private-key`、`public-key`（或 `peers[].public-key`）、`ip`、`mtu`、`dns`
   - `proxies_exists` 加 wireguard：按 `public-key` 去重
   - 参考 mihomo 文档：https://wiki.metacubex.one/config/proxies/wireguard/
2. **ShadowTLS**：
   - `verify()` 加 `type == "shadowtls"` 分支：校验 `password`、`version`（1/2/3）、`tls`/`sni`
   - `proxies_exists` 加 shadowtls：按 `password` 去重
   - 参考：https://wiki.metacubex.one/config/proxies/shadowtls/

### 改动文件
- `subscribe/clash.py`（`verify` + `proxies_exists`）

### 风险
- WireGuard 的 `peers` 数组结构较复杂，需对照 mihomo schema 仔细校验
- 这两个协议在免费池中罕见，收益相对低，放最后

---

## 任务 5：流量续期自动化放 checkin（P1）

### 现状
- `renewal.py` 有完整续期链路：`add_traffic_flow(domain, params, jsonify)` -> `login` -> `get_subscribe_info` -> 按条件 `flow(reset)` / `flow(renew)` / `submit_ticket`
- 续期触发条件：`expired_days <= 5` 或 `used_rate >= 0.8`
- 数据结构：`SubscribeInfo`（plan_id / renew_enable / reset_enable / used_rate / expired_days / package / sub_url / reset_day）
- **未集成到 checkin**；checkin 在容器 `ghcr.io/bigzhangbig/aggregator` 内运行，工作目录 `/aggregator`，**有完整 subscribe/ 源码可 import**

### 方案
1. **`universal.py` 签到后加续期**：`flow()` 签到成功后，对 v2board 类型调用续期
2. **复用 `renewal.add_traffic_flow`**：容器内 `sys.path.insert(0, "/aggregator")` 后 `from subscribe.renewal import add_traffic_flow`
3. **`checkin-config.json` 加续期字段**：`coupon_code`、`ticket`、`enable_renew`（由 `collect.py _collect_checkin_entries` 从 TaskConfig 透传）
4. **免费机场无 coupon 的降级**：
   - 优先 `reset_price <= 0` 的流量重置（`flow(reset=True)`，不需 coupon）
   - 其次 `submit_ticket`（工单重置流量）
   - 都不支持则跳过并日志记录

### 改动文件
- `.github/actions/checkin/universal.py`（集成续期步骤）
- `subscribe/collect.py`（`_collect_checkin_entries` 透传续期字段）
- `.github/workflows/checkin.yml`（确认容器内依赖可用）

### 风险
- 续期强依赖 `coupon_code`，免费机场常无；需明确降级路径
- `add_traffic_flow` 参数含 base64 编码的 email/passwd，需与 `checkin-config.json` 的明文凭据做转换
- 续期失败不应影响签到结果（独立 try/except）

---

## 优先级与执行顺序

| 顺序 | 任务 | 依赖 | 理由 |
|------|------|------|------|
| 1 | 任务 5 续期自动化 | 无 | 独立，复用现有 renewal.py，checkin 价值直接 |
| 2 | 任务 3 流媒体检测 | 无 | subs-check 调研已到位，8 平台 URL+判定齐备 |
| 3 | 任务 4 更多协议 | 无 | 收益较低（免费池罕见协议），放最后 |
| 弃 | 任务 1 测速排序 | 已弃 | GitHub Action 海外机房测延迟不代表国内体验 |
| 缓 | 任务 2 更多面板 | 暂缓 | 待 agent 调研 PMPanel 等面板 API |

## 整体原则
- 分批提交，每批跑通验证再进下一批，不破坏现有 collect/process/checkin 流水线
- 所有增强受环境变量开关控制（如 `SKIP_STREAMING_CHECK`、`ENABLE_RENEW`），默认行为可回退
- 遵循现有代码风格：相对 import、dataclass 配置、`utils.multi_thread_run` 并发
