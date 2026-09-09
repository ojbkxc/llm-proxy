# CodeArts Agent × ws-proxy 接入方案

> 目标：让 CodeArts Agent（华为 CodeArts IDE 内置 Agent）的对话流量改走本机 ws-proxy
> （`proxy.py`，127.0.0.1:8787），从而复用 Workspace 模型池 + 安全合规层
> （账号拦截 / 敏感词 / PII 可逆脱敏 / 审计）。
> 本方案与 `proxy.py` 零耦合：不改 proxy.py 一行代码，只改 CodeArts 侧配置。

## 1. 原理（逆向自 agentkernelServer-win32-x64.exe 26.7.2）

CodeArts Agent 内核是 opencode 的 fork（agent-kernel）。LLM 通道分两类：

| 通道 | provider id | 上游 | 鉴权 |
|---|---|---|---|
| 官方模型（GLM-5.2 / openpangu 等） | `inferhub-provider` | `https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions` | `x-auth-token` + `app-id: CodeAgent3.0` + `is_confidential`（登录态 token，华为云配额） |
| **自定义模型**（设置面板"自定义模型"） | `openai-<hash16>` | `options.baseURL` + `/chat/completions` | 仅 `Authorization: Bearer <options.apiKey>`，**无任何企业 header** |

自定义模型链路（内核源码节选）：

```
loadAndRegisterCustomModels()
  → loadCustomProviders()            # 读 ~/.codeartsdoer/codearts-data/codearts.json
  → checkCustomModelPermission()     # v1/llm/tenant/settings（AK/SK 签名；失败视为免费版放行）
  → decryptProviderApiKeys()         # enc:v3: → AES-256-GCM 解密
  → registerCustomProviders()        # 注册进 GlobalConfig.provider，env.CUSTOM_PROVIDER_KEYS
  → createCustomProviderPlugins()    # 生成 openai-compatible provider：
                                     #   api.url = options.baseURL
                                     #   options.apiKey → Authorization: Bearer
```

### 1.1 配置文件

- 路径：`~/.codeartsdoer/codearts-data/codearts.json`（JSONC，容忍注释与尾逗号）
- schema：`https://opencode.ai/config.json`
- 内核启动（IDE 冷启动 / Agent 重载）时读取；`apiKey` 若为明文，
  `migratePlainTextApiKeys()` 会自动加密回写为 `enc:v3:`。
  **因此我们的脚本直接写明文 key 即可，加密交给内核。**

### 1.2 apiKey 本地加密格式（仅为逆向验证用，接入不需要实现）

`enc:v3:<base64>`，base64 解开后 = `iv(12B) || authTag(16B) || ciphertext`，
AES-256-GCM，key = `scrypt("<hostname>:<username>", "codeagent-custom-model-salt-v1", N=16384, r=8, p=1, 32)`。
本机 8 个既有 provider 全部用此法验证可解密。

### 1.3 请求/响应协议

自定义 provider 固定 OpenAI chat 协议（`@ai-sdk/openai-compatible`）：
- 请求 `POST {baseURL}/chat/completions`，`stream: true/false` 均支持（SSE）。
- ws-proxy 的 `/v1/chat/completions` 天然兼容，无需任何转换层。

## 2. 接入步骤（路线 A：ws-proxy 合规层，注册脚本已删除）

1. 确保 ws-proxy 已运行：`python proxy.py`（模型列表来自 Workspace 登录态）。
2. ~~运行 `codearts_setup.py` 注册~~（**该脚本已删除**，它只配合 ws-proxy 用，与 `codearts_proxy.py` 无关；如需注册 ws-proxy 到 CodeArts IDE 请从 git 历史恢复）

3. **重启 CodeArts IDE**（内核只在启动时加载自定义模型）。
4. CodeArts 设置 → 模型选择里会出现以 `ws/` 为前缀的模型（如 `ws/qwen3.8-max`），
   选中即可对话；流量路径：CodeArts → 127.0.0.1:8787 → 合规层 → Workspace 网关。

### 2.1 模型命名

- 自定义模型在 CodeArts 里按 `provider key + 模型 id` 展示。为避免与官方/已有
  自定义模型重名，脚本统一加 `ws/` 前缀（即 Workspace 的意思）。
- 别名表直接复用 `proxy.py` 的 `MODEL_ALIAS`（脚本 import 之，保持单一来源）。

### 2.2 context window

按 Workspace 各模型实际能力填（默认 200k/16k，同现有自定义模型条目风格）。
`maxTokens=0 / truncateLength=0` 表示由服务端决定，保持 0。

## 3. 风险与边界

| 项 | 说明 |
|---|---|
| CodeArts 升级 | 只重写内核二进制，不动 `codearts.json`；provider 配置保留 |
| token 失效 | ws-proxy 依赖 Workspace 登录态；现在 proxy.py 会自动续期（refreshToken），仅 SSO 会话到期才需重新登录 |
| 合规层 | 流量全部经过 ws-proxy 的拦截/脱敏/审计，规则与 Workspace 直连完全一致 |
| 官方 inferhub 通道 | 不受影响；仅新增可选模型，不改变默认模型 |
| 回滚 | 手工删除 `codearts.json` 里 `provider` 的 `openai-wsproxy-*` 键 |

## 4. 路线 B：官方 inferhub 通道转本地 API（推荐，已由 codearts_proxy.py 独立实现）

> 原计划不做（合规面考虑），后经确认：CodeArts 官方通道**不需要管控层**，
> 故路线 B 已独立实现为 `codearts_proxy.py`（端口 8788，与路线 A 的 ws-proxy 8787 互不影响）。

把官方 `inferhub-provider` 通道转成本地 API（对外提供 GLM-5.2/openpangu）：
- 凭证完全复用 CodeArts Agent 客户端登录态（state.vscdb，DPAPI + AES-256-GCM 解出 AK/SK/securitytoken）；
- 上游是华为云 snap-access 网关，受华为云租户配额与审计管辖；
- 本代理不做内容拦截/脱敏/审计，不写日志文件（用户明确）。

### 4.1 多账号轮询（codearts_proxy.py）

客户端一次只能登录一个华为账号，因此用「快照」方式建账号池：

```
客户端登录账号 A  →  python codearts_proxy.py --capture   （池里加入 A）
客户端退出登录、登录账号 B  →  python codearts_proxy.py --capture   （池里加入 B）
……需要几个加几个
```

之后照常启动 `python codearts_proxy.py`，代理行为：
- 池非空时请求按 **round-robin 轮询**，过期账号自动跳过（全部过期才报错提示重新抓取）；
- 每个账号使用**独立 session id**——上游按 `x-ot-session-id` 计并发会话数（上限 3），
  N 个账号等效 N×3 个并发会话额度；
- 上游 401 时自动把该账号标记失效并换下一个账号重试一次（自愈）；
- 池文件（`codearts_accounts.json`）被 `--capture` 更新后，运行中的服务通过 mtime 热加载，无需重启；
- 客户端此时登录谁都行——代理用的是池里的快照。

| 命令 | 作用 |
|---|---|
| `python codearts_proxy.py --capture` | 把**当前**客户端登录态加入账号池（同账号重复执行=更新凭证） |
| `python codearts_proxy.py --list` | 查看池内账号与过期状态 |
| `python codearts_proxy.py --remove LABEL` | 移除指定账号 |
| `python codearts_proxy.py` | 启动代理（池空=直接用客户端登录态） |

**跨电脑迁移**：账号池就是一份明文 json（`codearts_accounts.json`），可在多台电脑间迁移/共享：

| 命令 | 作用 |
|---|---|
| `python codearts_proxy.py --export 路径.json` | 导出当前账号池到指定 json 文件（明文，不加密） |
| `python codearts_proxy.py --import 路径.json` | 从 json 文件导入账号，合并到本地池（同标签覆盖） |

典型流程：A 机 `--capture` 建好池 → `--export accounts.json` → 拷到 B 机 → `--import accounts.json`，
B 机即可直接启动代理用同一批账号，无需在 B 机重新登录客户端抓取。

**网盘共享**：设环境变量 `CODEARTS_PROXY_ACCOUNTS_FILE` 指向网盘里的同一份池文件，
多台电脑的代理即可共享同一账号池（任一机 `--capture` 更新后其他机通过 mtime 热加载自动生效）。

**凭证过期怎么办**：securitytoken 是临时凭证（一般 12~24h）。某个账号过期后会被自动跳过；
要续期它：在客户端重新登录该账号 → `--capture`（同标签覆盖更新）。`/health` 可随时查看各账号状态。

其他：移除了日志子系统（用户明确不要日志）；顺带修复 Anthropic 流式多 tool 块 index 错位、
404 路径 keep-alive 残留 body 两个 bug。

### 4.2 登录态自动续期（codearts_proxy.py）

securitytoken 是临时凭证（12~24h），过期前代理会自动续期，无需人工干预。续期复用
登录时保存的 `refresh_token` + PKCE `code_verifier` + DPoP 密钥对，调华为云 STS OAuth
端点换一组新凭证（ak/sk/securitytoken + 轮换后的新 refresh_token）。

**触发逻辑**（`_maybe_refresh`，每次 `_read_session` 取凭证时调用）：

- 检查 `exp - now`，若 ≤ `REFRESH_AHEAD_SEC`（=3600s，对齐客户端 `safelyRenewTokenInterval`）则触发；否则原样返回。
- 并发去重：`_refreshing` 集合按 `ot_session_id`（客户端态用 `"client"`）加锁去重，同一账号同时只有一个刷新在飞，其他线程先用旧凭证。
- 续期成功：池账号（`pool=True`）→ 更新内存 + 写回 `codearts_accounts.json` 对应条目（保留 `ot_session_id` 等池元数据）；客户端登录态（`pool=False`）→ 只更新内存 `_session_cache`。
- 续期失败：池账号被标记失效（轮询自动跳过）；客户端态保留旧凭证，等上游 401 触发自愈换号。stderr 输出 `自动续期失败/成功(...)`。

**STS 请求**（`_refresh_session`）：

```
POST https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens
Content-Type: application/x-www-form-urlencoded
DPoP: <ES256 JWS>

client_id=<CLIENT_ID>&code_verifier=<pkce.codeVerifier>&grant_type=refresh_token&refresh_token=<旧 refresh_token>
```

响应 `credentials` 含新的 `access_key_id`/`secret_access_key`/`security_token`/`expiration`，
以及**轮换后的新 `refresh_token`**（旧的一次性失效）。PKCE / DPoP 密钥对不变，复用下次续期。

**DPoP 签名**（`_dpop_sign`，RFC9449）：生成 ES256 JWS，`typ=dpop+jwt`，header 带
`jwk=publicKeyJwk`，payload `{htm, htu, iat, jti}`（`jti` 每次随机），用
`dpopKeyPair.privateKeyJwk` 的 P-256 ECDSA-SHA256 签名，签名输出裸 `r||s`（32+32 字节）base64url。
复刻客户端 `plugin.js generateDpopJWE`。

**CLIENT_ID**：默认 `codearts-agent`（= 客户端 `env.uriScheme`），可用环境变量
`CODEARTS_PROXY_CLIENT_ID` 覆盖。池内所有账号共用同一个 `CLIENT_ID`。

**重要副作用（已知限制）**：刷新成功后**只更新代理内存和池文件 `codearts_accounts.json`，
不回写客户端 `state.vscdb`**。由于 STS 会轮换 `refresh_token`（旧的失效），客户端当前
session 里持有的 `refresh_token` 会变成死 token——**客户端侧需要重新登录**才能恢复自续能力。

- 池账号：代理自洽，续期不依赖客户端，无影响；
- 客户端登录态（池空时）：代理续期成功后，客户端 IDE 里的登录态仍是旧 `refresh_token`，
  下次客户端自续会 401，需重新登录。建议要么用 `--capture` 把登录态导入池（之后由代理独管续期），
  要么不接受代理自动续期、改由客户端自续。

**手动触发**：`REFRESH_AHEAD_SEC` 是常量 3600s，无环境变量覆盖。要立即续期某账号：
池账号 → 在客户端重新登录该账号 → `python codearts_proxy.py --capture`（同标签覆盖更新）；
客户端态 → 重启代理并在临期内发任意请求（`_read_session` 会调 `_maybe_refresh`），或直接重新登录客户端。
`/health` 可随时查看各账号 `exp` 与剩余有效期。
