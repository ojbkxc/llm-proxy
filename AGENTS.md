# AGENTS.md — workspace-llm-proxy

给在本目录工作的 AI/自动化代理的操作守则。动手前先读完本文件。

## 项目概况

- 纯 Python 标准库实现的本地 LLM 代理（`proxy.py`），零第三方依赖，禁止引入 pip 依赖。
- 端点：`GET /v1/models`、`GET /health`、`POST /v1/chat/completions`、`POST /v1/responses`、`POST /v1/messages`、`POST /v1/messages/count_tokens`。
- 核心链路：账号拦截（`X-User-Id` 以 `ex_` 开头 → 403）→ 敏感词拦截（403）→ PII 可逆脱敏（`PH_<hash>` 占位符）→ 客户端名伪装（`Workspace_<hash>`）→ 转发 Workspace 网关 → 响应侧还原占位符。
- 认证：`_read_session` 在 accessToken 临期/过期时用 refreshToken 自动续期（`_refresh_token_via_refresh`），失败提示重新登录；独立脚本 `ws_auth.py` 解 VSCode 存储并支持主动续期（该脚本用 `cryptography`，主代理保持零依赖）。

## 红线（违反即停）

1. **禁止拿违规/敏感数据跑 proxy.py**。包括但不限于：真实手机号、身份证、银行卡、车牌、邮箱、密码、token、私钥、敏感词等任何真实 PII 或违规内容。
   - 本地验证一律使用 `test_proxy.py`（mock 模式，`WS_PROXY_MOCK=1`，不触网）。测试里出现的"13812345678"等是编造的假数据，新测试也必须用假数据。
   - 需要手工试代理时，只允许用无害占位内容（如 "hello"、"hi"）或测试脚本中已用的假数据。
   - 真实用户的 `audit_redact.jsonl` 是敏感审计文件：不要读取其内容做展示，不要把它复制进任何输出、报告或上下文。
2. **不得削弱合规层**。账号拦截、敏感词拦截、PII 脱敏、审计写入逻辑不许移除或绕过；改动不得让占位符映射（`PH_`/`Workspace_`）丢失还原能力。
3. **不外发数据**。代理只连 `BASE`（Workspace 网关）和本机回环；不得新增任何第三方外呼地址，不得把请求/响应体发往日志服务或遥测。

## 开发流程约定

- **改正式代码前先改测试**：任何对 `proxy.py` 的行为变更，先在 `test_proxy.py` 里加/改测试并跑通，再动 `proxy.py`，最后全量回归（`python test_proxy.py`）确认 0 失败。
- 测试不得触网：走 `WS_PROXY_MOCK=1` + 注入的 `_MockServer`。
- 并发语义保持：多线程正确性优先于微优化；所有共享状态改动都要考虑锁与竞态。
- Windows 环境注意：路径用 `os.path`，文件写入注意编码 `utf-8`，`_fmt_ts` 已处理 localtime 越界兜底。

## 文件速览

| 文件 | 用途 |
|---|---|
| `proxy.py` | 主程序（约 2000 行）：HTTP 服务、三协议转换、脱敏/伪装、审计、自动续期 |
| `test_proxy.py` | 离线测试：mock 上游 + 本地起服 + urllib 请求断言 |
| `ws_auth.py` | 独立认证脚本（status / refresh），依赖 `cryptography` |
| `audit_redact.jsonl` | 脱敏原文审计（敏感，勿外传） |
| `audit_pass.jsonl` | 放行请求元信息审计 |
| `proxy.log` | 运行日志 |

> 自动续期相关函数：`_load_session_from_db` / `_write_session_row` / `_refresh_token_via_refresh` /
> `_refresh_session_locked` / `_read_session`。`REFRESH_AHEAD_SEC = 3600` 为临期阈值。
