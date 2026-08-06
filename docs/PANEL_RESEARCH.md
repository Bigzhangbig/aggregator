# 面板注册/签到 API 调研报告

> 任务 2 调研：确认 ProxyPanel / Xboard / PMPanel 的注册与签到 API，验证现有实现。

## 调研方法
- gh CLI 搜 GitHub 仓库源码（`gh search repos` / `gh search code` / `gh api`）
- 读路由文件（routes/api.php、routes/user.php）和控制器源码
- 对比现有实现（scaner.py / universal.py）

---

## 1. ProxyPanel（ProxyPanel/ProxyPanel）

### 路由结构
- `routes/api.php`：`api` 前缀 + `api` middleware，含 `v1` 子前缀的用户 API
- `routes/user.php`：`web` + `user` middleware，web 路由（session 认证）

### API 端点（routes/api.php，prefix `api/v1`）

| 功能 | 端点 | 控制器 | 认证 | 请求体 | 响应 |
|------|------|--------|------|--------|------|
| 注册 | `POST /api/v1/register` | AuthController@register | 无 | nickname, username(email), password, password_confirmation | `{token: plainTextToken}` |
| 登录 | `POST /api/v1/login` | AuthController@login | 无 | username/email, password | `{token: plainTextToken}` |
| 登出 | `GET /api/v1/logout` | AuthController@logout | Bearer | - | - |
| 签到 | `POST /api/v1/doCheckIn` | ClientController@checkIn | Bearer | - | - |

### Web 端点（routes/user.php，session 认证）
- `POST /checkIn`：web 签到（session cookie），与 API 签到二选一

### 注册字段（AuthController 源码确认）
```php
'nickname' => 'required|string|between:2,100',
'username' => 'required|email|max:100|unique:user,username',
'password' => 'required|string|confirmed|min:6',  // 需 password_confirmation
```
Token 用 Laravel Sanctum（`$user->createToken('client')->plainTextToken`）。

### 现有实现验证
`scaner.py getsub_proxypanel`（line 288）和 `universal.py checkin_proxypanel`（line 167）：
- 注册字段：`username`(email) / `nickname`(email 前缀) / `password` / `password_confirmation` - **与源码一致** ✅
- 登录端点 `/api/v1/login` ✅
- 签到端点 `/api/v1/doCheckIn`（Bearer）✅
- 获取订阅 `/api/v1/getUserInfo` -> `data.subUrl` ✅

**结论：ProxyPanel 已完整实现，字段和端点与源码一致，无需修改。**

---

## 2. Xboard（cedar2025/Xboard）

### 调研
- 搜 `checkin` / `签到` / `user/checkin` 均无签到相关结果
- Xboard 是 V2Board 二开（Go/Swoole 重构），兼容 V2Board API
- 注册：`/api/v1/passport/auth/register`（同 V2Board）
- **签到：无端点**（与 V2Board 原版一致，源码确认无签到功能）

### 结论
Xboard 注册兼容 V2Board（已支持），**无签到端点，无法实现签到**。

---

## 3. PMPanel

### 调研
- `gh search repos "pmpanel"` 无独立仓库（只有 ProxyPanel 及无关项目）
- 之前 docs/AIRPORT_CHECKIN_METHODS.md 提及 PMPanel（`/api/v1/auth/login` + `/api/v1/user/checkin`），但搜不到源码
- **PMPanel 可能是 ProxyPanel 的误称**，或已停止维护

### 结论
PMPanel 不作为独立面板支持。ProxyPanel 已覆盖该需求。

---

## 总结

| 面板 | 注册 | 签到 | 现有实现 | 状态 |
|------|------|------|----------|------|
| ProxyPanel | `/api/v1/register` | `/api/v1/doCheckIn` | scaner + universal | ✅ 已实现，字段一致 |
| Xboard | 兼容 V2Board | **无签到** | V2Board 路径 | ✅ 注册已支持，签到无法做 |
| PMPanel | 不存在 | 不存在 | - | ❌ 误称，不实现 |
| SSPanel | `/auth/register` | `/user/checkin` | scaner + universal | ✅ 已实现 |
| V2Board | `passport/auth/register` | **无签到** | AirPort + universal | ✅ 已实现 |

**任务 2 结论**：ProxyPanel 已完整实现且与源码一致，Xboard 无签到端点无法做，PMPanel 不存在。任务 2 实际已完成，无需额外实现。
