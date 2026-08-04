# checkin 自动集成方案计划

## 目标

让 `collect.py` 注册成功的新机场账号，**自动**出现在 `.github/actions/checkin/universal.py` 的次日签到列表里，实现"发现 → 注册 → 签到"全链路闭环。

需要解决两个正交问题：
1. **数据流**：注册成功后的凭据怎么传给 checkin
2. **签到兼容性**：当前 `universal.py` 的签到方式能覆盖多少机场

---

## 第一阶段：市面上机场签到方式调研

### 需调研的机场框架

主流机场面板的签到实现差异大，需逐个审计才能让 `universal.py` 覆盖更多机场：

| 框架 | 登录端点 | 签到端点 | Cookie 模式 | 备注 |
|------|---------|---------|------------|------|
| **SSPanel-UIM** (legacy) | `POST /auth/login` | `POST /user/checkin` | Set-Cookie `uid/email/key/ip/expire_in` | `universal.py` 当前唯一支持的模式 |
| **SSPanel-UIM** (2024+) | `POST /auth/login` | `POST /user/checkin` | 响应 body 里的 `auth_data` JSON 字段 | 新版不再发 Set-Cookie，**universal.py 失效** |
| **v2board** | `POST /api/v1/passport/auth/login` | `POST /api/v1/user/checkin` | Bearer token | 完全不同 |
| **PMPanel** | `POST /api/v1/auth/login` | `POST /api/v1/user/checkin` | Bearer token | |
| **SSPanel-Malio** | `POST /auth/login` | `POST /user/checkin` | Set-Cookie + `retention` 字段 | 较新分支 |
| **ProxyPanel** | `POST /api/v1/user/login` | `POST /api/v1/user/checkin` | token in body | |

### 调研方法

1. 用 exa / firecrawl / gh CLI（按 CLAUDE.md 现代工具链）搜：
   - `SSPanel-UIM checkin API 2024`
   - `v2board checkin endpoint`
   - `机场签到接口实现`
   - 找 GitHub 上的机场框架源码，逐个看 `UserController::checkin` / `CheckinController` 实现
2. 抓取几个真实机场的登录/签到响应，对比响应格式（Set-Cookie vs JSON body）
3. 输出一份"机场框架 → 登录/签到端点/认证方式"映射表

### 预期产出

- 一份机场框架签到 API 差异表（覆盖 >80% 主流机场）
- 识别 `universal.py` 当前失败的具体框架
- 设计可扩展的 `flow()` 多框架支持架构

---

## 第二阶段：现有 actions 配合方案

### 现状（5 个 workflow）

| Workflow | 触发时机 | 输出 | 用到邮箱？ |
|----------|---------|------|-----------|
| `process.yaml` | cron 03:05 / 11:05 | 转换后的配置文件 + Gist | ❌ |
| `collect.yaml` | 每周一 00:00 | collect.py 跑结果 | ✅ 注册机场时 |
| `refresh.yaml` | 每 2 小时 | collect.py --refresh 只刷新 | ❌（只复用已有订阅） |
| `checkin.yml` | 每日 02:45 | 签到结果日志 | ❌（用 config.json 硬编码账号） |
| `delete.yaml` | 每周日 00:00 | 清旧 workflow run | ❌ |

### 配合方案选项

#### 方案 A：Gist 作 backing store（推荐）

```
collect.py (collect.yaml)
    ↓ 注册成功 → 提取 email/passwd
    ↓ 加密后 push 到 Gist 的 checkin-config.json file
    ↓
checkin.yml
    ↓ fetch Gist raw URL → 解密 → 写 .github/actions/checkin/config.json
    ↓ universal.py 读 config.json
    ↓
次日 02:45 自动签到新账号
```

**优势**：复用现有 `GIST_PAT`，不污染 repo，无 git history 泄漏
**劣势**：明文密码风险（需 GPG 加密缓解）

#### 方案 B：repo tracked 文件 + auto commit

```
collect.py 写入 data/checkin-config.json
    ↓ git add + commit + push (用 GH token with contents:write)
    ↓
checkin.yml 直接 checkout 读文件
```

**劣势**：密码进 git history；每次 collect 都产生 commit；需要额外权限

#### 方案 C：workflow_dispatch 链式触发

```
collect.py 完成 → POST /repos/.../actions/workflows/checkin.yml/dispatches
    ↓
checkin.yml 立即被触发（不等次日）
```

**劣势**：inputs 太长塞不下；仍需 A 或 B 作 backing store

### 决策点（待用户拍板）

1. **存储方案**：A / B / C
2. **加密强度**：明文 / secret gist / secret gist + GPG / GitHub Encrypted Secrets
3. **触发时机**：checkin 只在每日 02:45 / collect 完成后立即 dispatch / 两者都要
4. **universal.py 兼容性**：先支持当前 SSPanel-UIM 一种 / 重构为多框架插件化

---

## 第三阶段：实施步骤（待方案确定后细化）

无论选哪个方案，必做的最小改动（基于已派 agent 的研究结论）：

| 步骤 | 文件 | 改动 | 行数 |
|------|------|------|------|
| 1 | `subscribe/workflow.py` | `TaskConfig` 加 `email=""`, `passwd=""` 字段 | +2 |
| 2 | `subscribe/workflow.py` | `execute()` 在 `get_subscribe` 成功后写入 `task_conf.email/passwd` | +2 |
| 3 | `subscribe/airport.py` | 失败路径复位 `self.username/self.password`（避免脏数据） | +2 |
| 4 | `subscribe/collect.py` | `aggregate()` 末尾收集新注册凭据 + merge 已有 + push 到 Gist | +25 |
| 5 | `.github/workflows/checkin.yml` | 加 step 从 Gist fetch 配置（明文或解密后）写 `config.json` | +15 |
| 6（可选）| `subscribe/scripts/` 或新文件 | GPG 加密/解密逻辑 | +30 |
| 7（可选）| `.github/actions/checkin/universal.py` | 重构为多框架插件化 | +100 |

### 风险清单

| 风险 | 严重度 | 缓解 |
|------|--------|------|
| 明文密码进 Gist = 公开泄露 | 🔴 高 | 必做 GPG 加密或 secret gist |
| universal.py 仅支持 SSPanel-UIM 旧版 | 🟡 中 | 第一阶段调研后再决定是否重构 |
| 同一机场被多次注册导致凭据漂移 | 🟢 低 | domain-keyed merge（dict 去重） |
| Gist API 限流（collect 完成 + checkin fetch 两次请求） | 🟢 低 | 无需特别处理 |
| universal.py 登录路径/cookie 解析与机场不匹配 → 签到失败 | 🟡 中 | 失败仅打印错误，不中断 workflow |

---

## 第四阶段：验证

### 端到端测试场景

1. 本地手动：手工调用 `collect.py` 注册一个测试机场 → 验证 Gist 文件被更新 → 手动跑 `universal.py` 看能否签到
2. CI 流程：推一次 commit → 等 `collect.yaml` 跑（如果 cron 没触发用 `workflow_dispatch`）→ 检查 Gist → 等 `checkin.yml` 跑（次日 02:45 或 dispatch）→ 看签到日志

### 验收标准

- ✅ collect.py 成功注册的机场，email/passwd 出现在 Gist 的 checkin-config.json
- ✅ checkin.yml 启动时能从 Gist 拉到该文件
- ✅ universal.py 能用拉到的账号登录成功（至少一个 SSPanel-UIM 旧版机场）
- ✅ Gist 配置含 5+ 机场账号，签到成功率 > 60%

---

## 优先级与依赖

```
第一阶段（调研）       ← 独立，可立即开始
   ↓ 输出"框架-端点"映射表
第二阶段（方案设计）    ← 调研产出决定方案选型
   ↓ 用户拍板存储方案 + 加密强度
第三阶段（实施）       ← 按最小改动分 PR
   ↓
第四阶段（验证）       ← 端到端测试
```

**建议节奏**：第一阶段和第二阶段并行准备，第三阶段等用户拍板后启动。

---

## 关联文件

- `subscribe/airport.py` — `AirPort.register` 写 self.username/password（line 305-306）
- `subscribe/workflow.py` — `TaskConfig` dataclass + `execute()`
- `subscribe/collect.py` — aggregate 末尾 push 到 Gist（line 401-409）
- `subscribe/push.py` — `PushToGist.push_to` 支持多文件 payload（line 72-77）
- `.github/actions/checkin/universal.py` — 当前 SSPanel-UIM 旧版签到实现
- `.github/actions/checkin/config.json` — 当前硬编码账号列表
- `.github/workflows/checkin.yml` — 待加 fetch step
- `subscribe/mailtm.py` — TempMailC / MailTM / SnapMail 邮箱池
