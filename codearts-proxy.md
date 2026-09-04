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

## 2. 接入步骤（路线 A，推荐）

1. 确保 ws-proxy 已运行：`python proxy.py`（模型列表来自 Workspace 登录态）。
2. 运行本目录脚本（备份 → 写入 provider → 校验）：

   ```bash
   python codearts_setup.py            # 默认注册全部 MODEL_ALIAS 模型
   python codearts_setup.py --list     # 只查看当前 codearts.json 的 provider
   python codearts_setup.py --remove   # 移除本脚本写入的 provider（还原）
   ```

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
| 回滚 | `codearts_setup.py --remove` 或手工删除 `provider` 里 `openai-wsproxy-*` 键 |

## 4. 不采用的路线 B（仅记录）

把官方 `inferhub-provider` 通道转成本地 API（即对外提供 GLM-5.2/openpangu）：
- 需要 `x-auth-token`（`GlobalConfig.userToken` 或 env `W3_TOKEN`），随 IDE 登录态刷新；
- 上游是华为云 snap-access 网关，受华为云租户配额与审计管辖；
- `is_confidential`/`app-id` 等企业 header 属于租户管控语义，绕开即脱离管控。
→ 合规面不可控，不做。
