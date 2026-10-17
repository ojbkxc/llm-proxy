# llm-proxy — 本地 LLM 代理合集

把官方 AI 编程客户端的模型通道转成本地 **OpenAI / Responses / Anthropic 三协议兼容 API**，零第三方依赖（仅 `cryptography`）。

## 代理一览

| 代理 | 客户端 | 默认端口 | 多账号 | 自动续期 | 跨平台 |
|---|---|---|---|---|---|
| `codearts_proxy.py` | 华为 CodeArts Agent | 8788 | ✅ 池轮询 | ✅ DPoP+STS | ✅ Win 抓取 / 任意平台运行 |
| `trae_proxy.py` | 字节 TRAE SOLO CN | 8790 | — | ✅ refresh | ✅ |
| `qoder_proxy.py` | 阿里 Qoder CN | 8791 | ✅ 池轮询 | ✅ refresh | 需 Node + worker bundle；凭证 `--capture`（Win 客户端）或 `--login`（浏览器设备码，任意机器） |

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

## qoder_proxy.py — 阿里 Qoder CN

把 Qoder CN（阿里 AI IDE）客户端的官方模型通道转成本地三协议兼容 API。

**上游只用带 Cosy 签名的 `/algo` 通道**。签名与请求体编码原先在客户端的 `qoder_auth_wasm` 里，
现已逐字节还原成 **纯 Python**（`qoder_cosy.py`：自定义 base64 + 分块重排、AES-128-CBC、
RSA-1024 PKCS#1 v1.5、MD5 签名原像），并用插桩 WASM 的确定性回放 + 网关真机请求验收。
代理默认 **不需要 Node**：`QODER_PROXY_SIGN_BACKEND=auto` 优先纯 Python，异常才回落
`qoder_sign.mjs` sidecar（`py` / `node` 可强制单一后端）。只有 `--login` 的设备码流程仍用 Node
驱动客户端 bundle。整个运行过程不启动、不连接 Qoder CN 客户端进程。
无签名的 OpenAI 兼容通道 `POST /model/v1/chat/completions` 实测**没有被网关路由**：
所有 `/model/*` 变体都返回 ALB 503 HTML（**带真实 `device_token` 和不带鉴权完全一样**，
2026-10-17 复测；`gateway.qoder.com.cn` 就是客户端 `kgi()` 在 CN prod 用的同一个 host，
不是选错了域名），而 `/algo/*` 返回 403 `{"code":"101","message":"Signature invalid"}`——说明只有 `/algo` 到达了应用层。

### 快速开始

```bash
# 方式一：本机装过 Qoder CN 并登录过（Windows）
python qoder_proxy.py --capture    # DPAPI + AES-256-GCM 解客户端 auth.v1.dat，入池

# 方式二：完全没有客户端 —— 跳浏览器授权（对齐 codearts_proxy.py 的 --login 用法）
python qoder_proxy.py --login      # 打印 https://qoder.cn/device/selectAccounts?... 授权链接
                                   # → 你在浏览器里确认 → 轮询拿 device_token → 自动入池
                                   # 可连续登多个账号

python qoder_proxy.py              # 启动代理（tty 时弹交互菜单，可重复登录/签到/启动）
python qoder_proxy.py --serve      # 跳过菜单直接启动
python qoder_proxy.py --check      # 脱客户端自检：网关/账号池/签名器/模型清单
```

签名/代理运行不需要 Node；`--login` 需要 Node + worker bundle，`--capture` 需要 `cryptography`。

### 模型

模型清单**运行时从签名端点** `GET /algo/api/v2/model/list` 拉取并缓存 1h（`--models` 直接打印），
默认模型 `auto`。实测 14 个：

| model key | 名称 | 上下文 | 推理 | 价格系数 |
|---|---|---|---|---|
| `auto` | Auto | 200000 | ✅ | 0.5 |
| `qmodel_38max` | Qwen3.8-Max | 1000000 | ✅ | 0.2 |
| `qfmodel` | Qwen3.8-Flash | 1000000 | ✅ | 0.0 |
| `qmodel_latest` / `qmodel` / `q37fmodel` | Qwen3.7-Max / Plus / Flash | 1000000 | ✅ | 0.5 / 0.04 / 0.1 |
| `dmodel` / `dfmodel` | DeepSeek-V4-Pro / Flash | 1000000 | ✅ / — | 0.5 / 0.1 |
| `gmodel` / `gfmodel` / `gm51model` | GLM-5.3 / 5.3-Flash / 5.2 | 1000000 | ✅ | 0.8 / 0.1 / 0.6 |
| `kmodel_latest` / `kmodel` | Kimi-K3 / K2.8-Preview | 1000000 | — / ✅ | 1.4 / 0.8 |
| `mmodel` | MiniMax-M2.7 | 200000 | — | 0.2 |

> 别名大小写不敏感；清单里没有的模型名直接 404 并列出可用 id。上下文取
> `max_input_tokens` 与 `context_config` 里的较大值。

### 思考深度

三协议均可选择思考深度，代理会转成上游字段（顶层 `reasoningEffort` +
`parameters.reasoning_effort` / `enable_thinking` / `reasoning_budget_tokens`，与客户端
bundle 里的下发形状一致）：

| 客户端写法 | 效果 |
|---|---|
| OpenAI `reasoning_effort: "low"/"medium"/"high"/...` | 直接选档；`minimal`→`low`，`off`/`disabled`→`none` |
| OpenAI `thinking_budget: 4096`（Qwen 风格）/ `reasoning_budget_tokens` | 按预算反查档位（≤1K low、≤8K medium、≤24K high、≤49K xhigh，再高 max），预算随参上送 |
| OpenAI `enable_thinking: false` | 等同 `reasoning_effort: "none"`（真机实测思考 token 归零） |
| Responses `reasoning: {effort: "..."}` | 同上 |
| Anthropic `thinking: {type: "enabled", budget_tokens: N}` / `{type: "disabled"}` | 按预算反查档位 / 关闭思考 |
| Anthropic `preserve_thinking` / OpenAI `preserve_thinking` | 透传 |

档位以模型声明为准：`/v1/models` 的 `metadata.thinking_config` 透出每个模型声明的
`enabled.efforts` 与 `disabled`（例如 `qfmodel` 为 low/medium/xhigh + 可关）。请求了模型
未声明的档位时按预算距离**就近调整**（同距取小；只有 none 可用而要求思考时不注入，
走模型默认），调整会写进 `QODER_PROXY_LOG`。

### 每日 Credits 签到

```bash
python qoder_proxy.py --checkin
```

- **多账号**：遍历池中所有账号，逐个领取今天的 campaign。
- 先 `GET /sash/api/v1/me/campaigns`（Bearer + Cosy 设备头，无签名）；`claimable=false`
  直接返回 0（已领/未到窗口都是 no-op，可安全重复执行）。
- 有可领项时 `POST /sash/api/v1/me/campaigns/{id}/claim`（body `{}`），随后再查一次确认状态。
- 活动每天 **10:00（北京）** 开新窗口、错过不补，每笔 30 天有效。
- Windows 计划任务示例（每天 10:05，用 `pythonw.exe` 可避免弹控制台）：

```bat
schtasks /Create /TN "QoderCheckin" /SC DAILY /ST 10:05 ^
  /TR "\"<python.exe 或 pythonw.exe 的绝对路径>\" \"C:\GitHub\llm-proxy\qoder_proxy.py\" --checkin"
```

> 只用于你自己的账号。多账号批量领取属于刷活动，本工具不提供也不支持。

### 端点

| 端点 | 协议 | 说明 |
|---|---|---|
| `GET /health` | — | 只读本地状态：账号池 + 签名器 peek（不启动 node、不打上游，秒回） |
| `GET /v1/models` | OpenAI | 模型列表（签名拉取 + 缓存） |
| `POST /v1/chat/completions` | OpenAI | Chat Completions（流式/非流式；上游只吐 SSE，非流式由代理聚合） |
| `POST /v1/responses` | Responses | Responses API（codex-cli 兼容，含 `response.in_progress` 等完整事件序列） |
| `POST /v1/messages` | Anthropic | Messages API（流式/非流式，`thinking` + `text` 块） |
| `POST /v1/messages/count_tokens` | Anthropic | 本地估算，不打上游 |

`--api-key` / `QODER_PROXY_API_KEY` 非空时**所有**端点（含 `/health`）都要带 `Authorization: Bearer` 或 `X-Api-Key`。

### 凭证与续期

**凭证来源**（Electron safeStorage 双层加密，只在 `--capture` 时用一次）：

```
Local State.os_crypt.encrypted_key → DPAPI → 32B AES key
→ AES-256-GCM 解 auth.v1.dat → {token, refreshToken, expiresAt, user.id, ...}
+ ~/.qoder-cn/.auth/machine_id + umid-cache.json（machineToken/Type/Code）
+ ~/.qoder-cn/.cache/endpoint-cache.json（自动发现 gateway / openapi 域名）
→ 写入账号池 ~/.qoder-cn-proxy/accounts.json
```

**账号池**：多个账号自动 round-robin；上游 401/403 时把该账号标记失败并冷却
`FAIL_COOLDOWN_SEC = 300`，然后自动换下一个账号。池文件里是真 token，所以默认路径放在
仓库外（`~/.qoder-cn-proxy/accounts.json`，可用 `QODER_PROXY_ACCOUNTS` 覆盖）——`llm-proxy`
是 git 工作区且没有 `.gitignore`，放脚本旁边很容易被一次 `git add .` 提交出去。

**自动续期**：token 临期 1 天内（`QODER_PROXY_REFRESH_AHEAD`，默认 86400）或请求前扫到期，
自动调 `POST openapi.qoder.com.cn/api/v1/deviceToken/refresh`（body 带 `refresh_token` /
`machine_id` / `machine_token`，**不需要 Authorization**），并采纳轮换后的新
`device_token` + `refresh_token` 写回池文件。access token 约 30 天、refresh token 约 1 年，
期间无需人工干预；续期链彻底失效（400/401/403）时把账号标记 `refresh_dead`，不再反复重试。

**脱客户端运行**：客户端进程不需要开着，签名 sidecar 只读磁盘上的登录态与安装目录里的
worker runtime 文件。拿不到客户端登录态（换了机器、没装客户端）就用 `--login` 走浏览器设备码
授权，入池之后与 `--capture` 的账号完全等价。`--export` / `--import` 做跨机迁移（目标机不需要
Windows/DPAPI，但仍需 Node + 一份 `qoder-worker-runtime.obf.mjs`，用 `QODER_WORKER_BUNDLE` 指路）。

```bash
python qoder_proxy.py --login                 # 浏览器设备码登录入池（不需要客户端）
python qoder_proxy.py --capture               # 从本机客户端提取凭证入池（Windows）
python qoder_proxy.py --list              # 账号池一览（token 只显示前缀 + 掩码）
python qoder_proxy.py --remove LABEL      # 移除某个账号
python qoder_proxy.py --export pack.json  # 导出凭证包（跨机迁移）
python qoder_proxy.py --import pack.json  # 导入凭证包
python qoder_proxy.py --models            # 签名拉取模型清单
python qoder_proxy.py --check             # 网关/账号池/签名器/模型 四项自检
python qoder_proxy.py --test-chat "hi"    # 一次最小真实对话（手工验证请用无害占位内容）
```

### 设备码登录 `--login`

和 `--capture` 殊途同归，但**本机可以完全没有 Qoder CN 客户端**（Linux / 新机器 / 容器都行）：

```
node qoder_sign.mjs --login --site https://qoder.cn --openapi https://openapi.qoder.com.cn
  ├─ 复用客户端 bundle 里的 startDeviceFlow(openapi, site, machineId, isProd, signal, machineToken)
  │    · PKCE：43~128 字符 verifier，challenge = base64url(sha256(verifier))，method=S256
  │    · 授权页 https://qoder.cn/device/selectAccounts?challenge=..&nonce=..&machine_id=..&client_id=..
  ├─ {"event":"auth_url"} → 代理打印链接并尝试开浏览器
  └─ 每 1s GET openapi /api/v1/deviceToken/poll?nonce=..&verifier=..&challenge_method=S256
       · 404 = 还没授权，继续等；5 分钟超时；拿到 {token, refresh_token, user_id, expires_at} → {"event":"token"}
```

几个踩过的坑：

- **`client_id` 必须让 bundle 自己给**。它是 `_$d()` 混淆常量（实测
  `e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb`），只在 esbuild 的懒初始化包装 `Jli=S(()=>{…})` 里赋值。
  直接调 `startDeviceFlow` 会拿到字面量 `client_id=undefined` 的 URL。所以 `qoder_sign.mjs`
  在打补丁时按语义锚点（`|localhost|::1` 代理探测正则 + `1e3`/`3e5` 两个常量）定位该包装函数，
  注入 `export const __qoderDeviceInit = () => Jli();`，`--login` 先调它再发起流程。
  混淆变量名换版本会变，因此补丁带了 `PATCH_REV`，缓存文件名里的 hash 会随之失效重建。
- 调试：`node qoder_sign.mjs --print-client-id` 只生成授权 URL 并打印 `client_id`，不联网轮询。
- 没有客户端登录态时 `machine_id` 用随机 UUID，**没有** `machine_token`。这种账号的 token
  到期后 `deviceToken/refresh` 可能被拒（`--login` 会显式提示），临近过期重新 `--login` 即可。
- 授权动作必须人在浏览器里完成；代理不会、也无法替你点确认。
- 这条链接本身就是一个"登录请求"：谁在浏览器里打开并确认，token 就发给持 verifier 的轮询方
  （也就是这台机器）。所以别把它转发出去——别人点一下会把**他的**账号塞进你的池子，
  但拿不走你的凭证。链接里还带 `machine_token`，不要贴到公开渠道。

### 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `QODER_PROXY_PORT` | 8791 | 监听端口（`--port` 覆盖） |
| `QODER_PROXY_API_KEY` | (空) | API Key 鉴权（空=不鉴权；`Authorization: Bearer` 或 `X-Api-Key`） |
| `QODER_PROXY_ACCOUNTS` | `~/.qoder-cn-proxy/accounts.json` | 账号池文件 |
| `QODER_PROXY_SIGN_BACKEND` | `auto` | 签名后端：`auto`=纯 Python 优先、异常回落 node；`py`=只用纯 Python；`node`=只用 sidecar |
| `QODER_SIGN_JS` | 脚本同目录 `qoder_sign.mjs` | Node sidecar 路径（`--login` / node 回落用） |
| `QODER_PROXY_NODE` | `node` | Node 可执行文件（`--login` / node 回落用） |
| `QODER_WORKER_BUNDLE` | 客户端安装目录 | `qoder-worker-runtime.obf.mjs`（`--login` / node 回落用，找不到时报错并提示设置） |
| `QODER_PROXY_GATEWAY` / `QODER_PROXY_OPENAPI` | endpoint-cache 自动发现 | 覆盖上游域名 |
| `QODER_PROXY_MODEL` | `auto` | 默认模型 |
| `QODER_PROXY_SCENE` | `chat` | 业务场景（model/list 的场景过滤） |
| `QODER_PROXY_TIMEOUT` | 600 | 上游超时（秒） |
| `QODER_PROXY_MODELS_TTL` | 3600 | 模型清单缓存（秒） |
| `QODER_PROXY_REFRESH_AHEAD` | 86400 | 提前多久续期（秒） |
| `QODER_PROXY_SITE` | `https://qoder.cn` | 设备码授权页 base（`--site` 覆盖；国际版为 `https://qoder.com`） |
| `QODER_PROXY_LOGIN_TIMEOUT` | 330 | `--login` 等待浏览器授权的超时（秒，服务端轮询本身 5 分钟超时） |
| `QODER_PROXY_LOG` | (空=关闭) | 日志文件路径 |
| `QODER_PROXY_LOG_MAX_MB` | 5 | 日志轮转上限（MB） |

请求体上限 8MB（防 OOM，对齐 codearts/trae_proxy）。

### 平台支持

| 平台 | 代理运行 | `--capture` | 说明 |
|---|---|---|---|
| Windows | ✅ | ✅ | 全功能（DPAPI 解密客户端登录态） |
| Linux / macOS | ✅ | ❌ | `--import` 导入凭证包后即可运行，代理与签名不需要 Node；`--login` 需要 Node + worker bundle |

---

## 共同特性

- **三协议兼容**：OpenAI Chat / Responses / Anthropic Messages 统一转换
- **真流式**：边收边写，TTFB ≈ 0s
- **自动续期**：token 临期自动刷新，无需手动登录
- **API Key 鉴权**：可选，`Authorization: Bearer` 或 `X-Api-Key`
- **body 限制**：8MB 上限防 OOM
- **零依赖**：运行路径只用 Python 标准库（`python -S` 关掉 site-packages 实测可跑）；
  `cryptography` 只有 `--capture` 解 DPAPI/AES-GCM 时才需要（缺包时给一句可执行的提示，
  可改用 `--login` 或 `--import` 拿凭证），`qoder_proxy.py` 额外需要一个 Node 签名 sidecar

## 依赖

- Python 3.8+
- `cryptography`（AES-256-GCM 解密 / ES256 DPoP 签名）
- Node 18+（仅 `qoder_proxy.py`：签名 sidecar 要驱动客户端的 WASM 模块）

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

├── qoder_proxy.py             # 阿里 Qoder CN 代理（Cosy 签名通道 + 账号池 + 自动续期）
├── qoder_cosy.py              # 纯 Python Cosy 签名器（WASM 逐字节复刻，默认后端）
├── qoder_sign.mjs             # Node 签名 sidecar（--login 设备码 / 签名回落）
├── test_qoder_proxy.py        # qoder_proxy 离线测试（全 mock，不触网）
├── test_qoder_cosy.py         # qoder_cosy 离线测试（WASM 回放向量）
├── .reverse/                  # Qoder CN 逆向笔记（协议/签名/凭证）
├── proxy.py                   # ws-proxy 合规层（账号拦截/脱敏/审计）
├── test_proxy.py              # ws-proxy 测试
├── ws_auth.py                 # ws-proxy 认证脚本
├── config.example.json        # trae_proxy 配置模板
└── AGENTS.md                  # 项目操作守则
```
