# llm-proxy — 本地 LLM 代理合集

把官方 AI 编程客户端的模型通道转成本地 **OpenAI / Responses / Anthropic 三协议兼容 API**，零第三方依赖（仅 `cryptography`）。

## 代理一览

| 代理 | 客户端 | 默认端口 | 多账号 | 自动续期 | 跨平台 |
|---|---|---|---|---|---|
| `codearts_proxy.py` | 华为 CodeArts Agent | 8788 | ✅ 池轮询 | ✅ DPoP+STS | ✅ Win 抓取 / 任意平台运行 |
| `trae_proxy.py` | 字节 TRAE SOLO CN | 8790 | — | ✅ refresh | ✅ |

---

## codearts_proxy.py — 华为 CodeArts Agent

把 CodeArts Agent 的官方模型通道（inferhub / snap-access）转成本地 API，认证复用客户端登录态。

### 快速开始

```bash
# Windows 上（有 CodeArts Agent 客户端已登录）
python codearts_proxy.py --capture          # 抓取当前登录态入池
python codearts_proxy.py                    # 启动代理 → http://127.0.0.1:8788
```

### 模型

| model id | 上下文 | 说明 |
|---|---|---|
| `GLM-5.2` | 307200 | 最新旗舰模型，专为长程任务打造 |
| `glm-5.2-sft-harmony` | 196608 | 基于 GLM-5.2 增训鸿蒙代码与开发知识 |
| `openpangu-2.0-pro` | 512000 | 最新旗舰模型，复杂工程稳定交付 |
| `openpangu-2.0-flash` | 512000 | 均衡推理效果与性能 |

别名：`glm-5.2` / `glm-5.2-harmony` / `pangu-pro` / `pangu-flash`。

> 注：所有模型上游 `max_tokens` 请求参数上限为 65536（input+output 总 token 预算另计，
> 超限返回 limit_err 81027/81001，代理会钳制并转成 413 提示）。

### 端点

| 端点 | 协议 | 说明 |
|---|---|---|
| `GET /health` | — | 健康检查（各账号过期状态） |
| `GET /v1/models` | OpenAI | 模型列表 |
| `POST /v1/chat/completions` | OpenAI | Chat Completions（流式/非流式） |
| `POST /v1/responses` | Responses | Responses API（codex-cli 兼容） |
| `POST /v1/messages` | Anthropic | Messages API（流式/非流式） |
| `POST /v1/messages/count_tokens` | Anthropic | Token 计数 |

### 多账号池

```bash
python codearts_proxy.py --capture           # 抓取当前客户端登录态入池
python codearts_proxy.py --list              # 查看池
python codearts_proxy.py --remove LABEL      # 移除指定账号
python codearts_proxy.py --export 路径.json  # 导出池（跨电脑迁移）
python codearts_proxy.py --import 路径.json  # 导入池（合并，同标签覆盖）
```

池非空时自动 round-robin 轮询，过期账号跳过，401 自愈换号。每个账号独立 session id（N 账号 = N×3 并发会话额度）。

### 跨电脑迁移

```bash
# Windows 上抓取并导出
python codearts_proxy.py --capture
python codearts_proxy.py --export accounts.json

# Linux / 其他电脑上导入并运行
pip install cryptography
python codearts_proxy.py --import accounts.json
python codearts_proxy.py
```

**网盘共享**：设 `CODEARTS_PROXY_ACCOUNTS_FILE` 指向网盘同一份池文件，多机共享，mtime 热加载自动生效。

### 登录态自动续期

securitytoken 是临时凭证（≈2h），临期 1 小时自动调 STS OAuth 端点续期（DPoP ES256 签名），refresh_token 有效期 ≈24 天，期间无需人工干预。续期后新凭证写回池文件，代理自洽管理。

> **已知限制**：续期不回写客户端 `state.vscdb`，refresh_token 轮换后客户端当前 session 失效（池账号不受影响）。`CLIENT_ID` 默认 `codearts-agent`，可用 `CODEARTS_PROXY_CLIENT_ID` 覆盖。

### 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `CODEARTS_PROXY_PORT` | 8788 | 监听端口 |
| `CODEARTS_PROXY_API_KEY` | (空) | API Key 鉴权（空=不鉴权，支持 `Authorization: Bearer` 和 `X-Api-Key`） |
| `CODEARTS_PROXY_CLIENT_ID` | codearts-agent | STS OAuth client_id |
| `CODEARTS_PROXY_ACCOUNTS_FILE` | 脚本同目录 | 池文件路径（网盘共享用） |

请求体上限 8MB（防 OOM，对齐 trae_proxy）。

### 测试

```bash
python test_codearts_proxy.py        # 9 项离线测试（不触网）
```

### 平台支持

| 平台 | 代理运行 | `--capture` | 说明 |
|---|---|---|---|
| Windows | ✅ | ✅ | 全功能（DPAPI 解密客户端登录态） |
| Linux / macOS | ✅ | ❌ | 池非空时全功能；`--import` 导入池后即可运行 |

---

## trae_proxy.py — 字节 TRAE SOLO CN

把 TRAE SOLO CN 客户端的官方模型通道转成本地三协议兼容 API。

### 快速开始

```bash
# 确保 TRAE SOLO CN 客户端已登录（token 有效期内）
python trae_proxy.py
```

代理启动在 `http://127.0.0.1:8790`，自动复用客户端登录态。

### 端点

| 端点 | 协议 | 说明 |
|---|---|---|
| `GET /health` | — | 健康检查（token 有效期、模型数、缓存状态） |
| `GET /v1/models` | OpenAI | 模型列表（动态拉取 + 1h 缓存） |
| `POST /v1/chat/completions` | OpenAI | Chat Completions（流式/非流式） |
| `POST /v1/responses` | Responses | Responses API（codex-cli 兼容） |
| `POST /v1/messages` | Anthropic | Messages API（流式/非流式） |
| `POST /v1/messages/count_tokens` | Anthropic | Token 计数 |

### 配置

优先级：环境变量 > `config.json` > 代码默认值。

```bash
export TRAE_PROXY_PORT=8790
export TRAE_PROXY_API_KEY="your-secret-key"   # 空 = 不鉴权
cp config.example.json config.json            # 或用配置文件
```

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| port | `TRAE_PROXY_PORT` | 8790 | 监听端口 |
| api_key | `TRAE_PROXY_API_KEY` | (空) | API Key 鉴权 |
| max_body_mb | `TRAE_PROXY_MAX_BODY_MB` | 8 | 请求体大小上限 |
| models_ttl | `TRAE_PROXY_MODELS_TTL` | 3600 | 模型列表缓存 TTL（秒） |
| refresh_ahead_sec | `TRAE_PROXY_REFRESH_AHEAD_SEC` | 3600 | token 临期续期阈值（秒） |
| default_model | `TRAE_PROXY_DEFAULT_MODEL` | glm-5.2 | 默认模型 |

### 测试

```bash
python test_trae_proxy.py                       # 离线单元测试
python test_trae_proxy.py --integration         # 单元 + 集成测试
```

---

## 共同特性

- **三协议兼容**：OpenAI Chat / Responses / Anthropic Messages 统一转换
- **真流式**：边收边写，TTFB ≈ 0s
- **自动续期**：token 临期自动刷新，无需手动登录
- **API Key 鉴权**：可选，`Authorization: Bearer` 或 `X-Api-Key`
- **body 限制**：8MB 上限防 OOM
- **零依赖**：纯 Python 标准库 + `cryptography`

## 依赖

- Python 3.8+
- `cryptography`（AES-256-GCM 解密 / ES256 DPoP 签名）

```bash
pip install cryptography
```

## 项目结构

```
llm-proxy/
├── codearts_proxy.py          # 华为 CodeArts Agent 代理（多账号 + 自动续期）
├── codearts-proxy.md          # codearts_proxy 设计文档（逆向细节）
├── test_codearts_proxy.py     # codearts_proxy 测试套件（9 项）

├── trae_proxy.py              # 字节 TRAE SOLO CN 代理
├── test_trae_proxy.py         # trae_proxy 测试套件
├── trae_login.py              # TRAE 登录辅助
├── proxy.py                   # ws-proxy 合规层（账号拦截/脱敏/审计）
├── test_proxy.py              # ws-proxy 测试
├── ws_auth.py                 # ws-proxy 认证脚本
├── config.example.json        # trae_proxy 配置模板
└── AGENTS.md                  # 项目操作守则
```
