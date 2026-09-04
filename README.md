# Workspace (Midea) LLM 本地代理

把美的 Workspace 编辑器内部的 LLM 通道转成本地 **OpenAI 兼容 / Responses / Anthropic 兼容 API**，并内置外部大模型安全合规层。

## 使用

```bash
# 前提：已安装并登录 Workspace 编辑器（首次登录仍走它，之后 accessToken 自动续期，见「认证自动续期」）
python proxy.py
```

启动后本地可用接口（127.0.0.1:8787）：

| 接口 | 协议 | 说明 |
|---|---|---|
| `GET /v1/models` | OpenAI | 可用模型列表 |
| `POST /v1/chat/completions` | OpenAI | 对话（支持 stream 透传） |
| `POST /v1/responses` | OpenAI Responses | 新版协议（codex-cli 等，支持流式） |
| `POST /v1/messages` | Anthropic | Messages 对话（支持流式） |
| `POST /v1/messages/count_tokens` | Anthropic | token 估算（本地，不转发上游） |
| `GET /health` | - | 当前登录用户和 token 有效期 |

> 三种格式的请求体 / 响应体在代理内部统一转换为 OpenAI chat 格式调上游，再按客户端协议转换返回。流式也做同样的转换（OpenAI chat SSE → Responses SSE / Anthropic SSE）。
>
> Responses 转 OpenAI chat 时，`parallel_tool_calls` 仅在显式声明 `tools` 时透传；否则丢弃。
> 这是为了兼容 codex-cli：它即便不带 tools 也会发送 `parallel_tool_calls: false`，而上游
> litellm 网关只允许在声明 `tools` 时携带该字段（否则 400）。

## 对接示例

任意支持上述协议的工具里这样配：

- Base URL: `http://127.0.0.1:8787/v1`（Anthropic 客户端填 `http://127.0.0.1:8787`）
- API Key: 任意值（本地代理不校验）
- 模型名: 用 `GET /v1/models` 里返回的 id，如 `qwen3.8-max`、`gpt-5.6-luna`、`deepseek_v4` 等

```bash
# OpenAI 兼容
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-max","messages":[{"role":"user","content":"hi"}]}'

# Responses
curl http://127.0.0.1:8787/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-max","input":"hi"}'

# Anthropic
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-max","max_tokens":200,"messages":[{"role":"user","content":"hi"}]}'
```

## 安全合规层

针对外部大模型使用规范内置以下拦截/脱敏（可配置）：

| 规则 | 行为 | 配置位置 |
|---|---|---|
| 个人账号（`ex_` 开头的工号等特征） | 403 拦截，禁止调用外部大模型 | `PERSONAL_ACCOUNT_PATTERNS`（特征可扩展，拦截不可关闭） |
| 政治 / 宗教 / 违法关键词 | 403 拦截，统一提示"禁止向外部大模型传敏感信息" | `SENSITIVE_WORDS` |
| 手机号 / 身份证号 / 车牌号 / 银行卡号 | **可逆脱敏**：替换为 `PH_XXXX` 占位符后转发，响应回来时自动还原 | `_PII_PATTERN` |
| 邮箱（含个人邮箱如 `ex_xxx@partner.midea.com`） | **可逆脱敏**：替换为 `PH_XXXX` 占位符后转发，响应回来时自动还原 | `_EMAIL_PATTERN` |
| 密码 / API Key / Token / 私钥 / 连接串 / JWT | **可逆脱敏**：替换为 `PH_XXXX` 占位符后转发，响应回来时自动还原 | `REDACT_PATTERNS` |
| 含 `.github` 的路径 | 跳过全部检查，直接转发（CI 自动流程场景） | `IGNORE_PATH_PARTS` |
| 客户端名（Codex/Claude/CodeArts/Trae 等） | **可逆伪装**：替换为 `Workspace_<hash>` 占位符后转发，响应时自动还原 | `_CLIENT_SPOOF` |

### 脱敏原理

1. 请求里的 `password=Abc123`、`api_key=sk-xxx`、手机号、身份证号、车牌号、银行卡号、邮箱等可逆值 → 替换为占位符 `PH_XXXX`
2. 原文按 JSON 行写入 `audit_redact.jsonl`（审计 + 供响应还原）
3. 大模型回复里若引用了 `PH_XXXX` → 代理在响应里自动还原回原文
4. 同一请求里出现**多个**同类敏感值（如 2 个手机号、多个邮箱）会各自生成唯一占位符、全部替换；同一原文重复出现则共用同一占位符（哈希去重）
5. 流式输出中占位符即使被模型按 token 拆开（如 `PH_1CC0` + `E18DDC`），代理也会跨 chunk 拼回完整占位符再还原为原文
6. 政治/宗教/违法等**不可逆**关键词 → 直接 403 拦截，绝不外发

> 审计文件：`audit_redact.jsonl`（与 proxy.py 同目录）。**拦截事件与脱敏原文都在这里**，满足"所有与外部大模型的交互均有审计记录、支持事后追查"的要求。

## 请求身份

代理默认从当前 Workspace 登录态（JWT 里的 preferred_username）取用户工号做合规判断。
若你的工具能带自定义身份，可加请求头 `X-User-Id: <工号>` 覆盖；命中个人账号特征
（默认 `ex_` 开头的工号，如 `ex_shenyk4`）的会被拦截。特征在
`PERSONAL_ACCOUNT_PATTERNS` 中配置（可扩展，但"是否拦截"是固定逻辑）。

## 认证自动续期

proxy.py 会在 accessToken **临期（剩余 < 1 小时）或已过期**时，自动用
refreshToken 调 `GET /api/login-server/v1/auth/refresh-token` 续期，并把新
accessToken / refreshToken 写回 opencode.db（对 Workspace 编辑器透明）。

- accessToken 每次续期约 +24h，refreshToken 约 +48h（Keycloak 会话内续期）。
- 只要刷新能成功，就无需再手动打开编辑器；**只有 SSO 会话总寿命到期**（刷新返回
  `success=false`）时才需要重新登录，此时请求会返回
  「登录态已失效，请打开 Workspace 编辑器重新登录后再试」。
- 续期结果写入 `proxy.log`（`[session] accessToken 已自动续期 ...`）。

> opencode.db 里存有 `refreshToken` 字段。若你手动用 Workspace 编辑器重新登录，
> 它可能覆盖掉这行（不带 refreshToken），导致自动续期失效退回提示；此时跑一次
> `python ws_auth.py refresh` 把 refreshToken 补回即可。

### ws_auth.py（独立认证脚本，不参与代理运行）

```bash
python ws_auth.py status     # 只读展示 accessToken/refreshToken 有效期 + 两处存储一致性
python ws_auth.py refresh    # 主动续期并双写（VSCode 存储 + opencode.db）
```

> 该脚本用 `cryptography`（AES-GCM）解 VSCode 存储的完整 session；主代理
> `proxy.py` 保持零第三方依赖，不依赖它。

## 日志与审计

同目录生成 3 个文件：

| 文件 | 内容 |
|---|---|
| `proxy.log` | 运行日志（请求访问 + 启动/退出 + 自动续期）。环境变量 `WS_PROXY_LOG` 改路径，空字符串关闭 |
| `audit_redact.jsonl` | 拦截事件 + 脱敏原文（密码/密钥/客户端名等占位符原文），用于审计追查 + 响应还原 |
| `audit_pass.jsonl` | 放行请求记录（谁、何时、协议、模型、token 数，不写敏感原文），方便事后分析用量 |
| `ws_auth.py` | 独立认证脚本（status / refresh），解 VSCode 存储 + 主动续期双写 |

## 客户端指纹伪装

上游（litellm 网关）可能检测调用方客户端。代理会把请求里出现的 `Codex`、`Claude`、
`Anthropic`、`CodeArts Agent`、`TraeCode`、`Cline`、`Cursor`、`ZCode` 等第三方工具名，
替换成 `Workspace_<hash>` 占位符后转发（仍是 Workspace 前缀，防检测一致）；响应回来时再精确还原成原名。
同样，转发上游的 `User-Agent` 统一设为 `Workspace`。

> `OpenAI`、`MCP` 属于公司名 / 协议名，不是第三方客户端标识，不参与伪装，避免上游在
> 被改写的 prompt 上推理（例如 "用 MCP 做 X" 不会被替换成 "用 Workspace_xxx 做 X"）。

## 工作原理

1. Workspace 编辑器登录后会把 `{accessToken,label,id}` 用随机 key XOR 加密存进
   `~/.local/share/workspace-code-prd/opencode.db` 的 `workspace_session` 表
2. 本代理从该 db 解出 accessToken（JWT，Keycloak 签发，约 24h 有效）
3. 用它调 `https://workspace-prd.midea.com/api/cn-control/ide/list-organizations`
   和 `list-assistants` 拿到模型清单（真实推理网关是
   `https://apiprod.midea.com/llm/f-devops-python-litellm/v1`）
4. 请求时带上 `Authorization: Bearer <jwt>`、`TEAM: workspace-dev`、`SCENE: workspace-local`
   （与 ws.exe 内置 fetch wrapper 完全一致），流式/非流式直接透传

> token 过期（JWT exp）会报 500，打开 Workspace 编辑器让它自动刷新后再试。
> 现在 proxy.py 会先尝试用 refreshToken 自动续期（见「认证自动续期」）；只有续期失败
> （SSO 会话到期）才需要人工重新登录。

## 模型别名

`proxy.py` 顶部 `MODEL_ALIAS` 表可自定义"本地想用的名字 → Workspace 真实模型 id"，
例如 `deepseek-v4-pro` → `deepseek_v4`。加别名只改这张表即可。