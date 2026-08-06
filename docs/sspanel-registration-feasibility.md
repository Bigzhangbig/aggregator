# SSPanel 注册支持调研结论

## 结论
**AirPort 类不支持 SSPanel 注册；SSPanel 已通过旁路（tempairport → scaner）独立实现，无需扩展。**

## 现状
- `airport.py:172-177` `__init__` 硬编码 V2Board 路径：`self.reg = {site}{api_prefix}passport/auth/register`、`self.fetch = .../user/server/fetch`、`self.send_email = .../passport/comm/sendEmailVerify`
- `airport.py:272-343` `register()` payload 字段（`email/password/invite_code/email_code`）和响应解析（`data.token` + `data.auth_data`）都是 V2Board 形态
- `airport.py:557` `get_subscribe_info` 走 `/user/getSubscribe` 也是 V2Board
- `airport.py:119-139` `issspanel()` 探测 `/auth/login` vs `/api/v1/passport/auth/login` 区分面板
- `airport.py:106` `RegisterRequire.sspanel` 字段为死代码（无读写）
- SSPanel 注册/取订阅在 `scripts/tempairport.py:28` → `scripts/scaner.py:267 getsub` 走旁路：POST `/auth/register` → POST `/auth/login` 取 cookie（uid/email/key/expire_in）→ GET `/getuserinfo` 取 `info.subUrl + info.ssrSubToken` 拼接
- 签到侧 `universal.py:172` 与 `collect.py:239` 也按 `api_prefix` 是否含 `/api/v1/` 分流 v2board/sspanel
- `crawl.py:1875 validate_domain` 不调用 `issspanel`，仅依赖 `get_register_require` 探测 `/api/v1/guest/comm/config`；对纯 SSPanel 域会返回 `api_prefix=""` 但不显式标记，靠 `tempairport.py` 兜底

## 关键文件
- `subscribe/airport.py:142-192` `AirPort.__init__`（V2Board 硬编码）
- `subscribe/airport.py:272-343` `register()`（V2Board-only）
- `subscribe/airport.py:119-139` `issspanel()`（仅 `tempairport.py:28` 调用）
- `subscribe/scripts/tempairport.py:19-42` `register()`（SSPanel 入口）
- `subscribe/scripts/scaner.py:267-285` `getsub()`（SSPanel 完整注册→订阅链路）
- `subscribe/scripts/scaner.py:151-171` `register()`（SSPanel POST /auth/register）
- `subscribe/scripts/scaner.py:185-224` `fetch_nodes`（SSPanel 登录 + /getuserinfo）
- `subscribe/collect.py:220-253`（按 api_prefix 选 v2board/sspanel 端点）
- `.github/actions/checkin/universal.py:99-172`（按 login_path 选端点）

## SSPanel-UIM vs V2Board 接口差异
| 维度 | V2Board | SSPanel-UIM |
|---|---|---|
| 注册端点 | POST `/api/v1/passport/auth/register` | POST `/auth/register` |
| 注册 payload | `email/password/invite_code/email_code` | `name/email/passwd/repasswd/code/imtype/wechat/emailcode` |
| 注册响应 | `{data: {token, auth_data}}` | `{ret: 0\|1, msg}`（不返 token） |
| 鉴权 | `Authorization: Bearer <token>` | 仅 Cookie（uid/email/key/expire_in） |
| 登录 | `/api/v1/passport/auth/login` 返 token | `/auth/login` 返 Set-Cookie |
| 邮箱验证 | `/api/v1/passport/comm/sendEmailVerify` | `/auth/sendVerify` |
| 订阅链接 | `/user/getSubscribe.subscribe_url` | `/getuserinfo.info.subUrl + .ssrSubToken` 拼接 |
| 免费套餐 | `/user/plan/fetch` + `/user/order/*` | 无自动 free plan |
| CAPTCHA | recaptcha/Turnstile | recaptcha 或 geetest |

## 扩展可行性
**不推荐**把 SSPanel 注册合并进 `AirPort` 类：
- 注册响应不带 token，订阅要 `subUrl + ssrSubToken` 拼接，与 V2Board `?token=` 路径不兼容
- `order_plan` / `renewal.flow` / `get_free_plan` 全是 V2Board 路径，SSPanel 无对应
- CAPTCHA 走 geetest 时需要 `geetest_challenge/validate/seccode` 三参数分支
- 注册 payload 字段（`name/repasswd/imtype/wechat`）不通用

**现有架构是正确设计**：`issspanel()` 在 `tempairport.py` 早判分流，绕过 `AirPort.register()` 走 `scaner.getsub`。已验证可用，不动即可。

## 改动方案
**无必需要改的代码。** SSPanel 注册链路完整工作（旁路路径），签到链路也已分流。

可选小清理（不主动做）：
- 删 `airport.py:106` `RegisterRequire.sspanel` 死字段
- `collect.py:230-239` 用 `api_prefix` 隐式判别可改为显式字段（破坏持久化格式，非必要）
- 真要升级 SSPanel 体验，应改 `scaner.getsub` 加 `enable_geetest_reg` 预探测与跳过，不动 `airport.py`
