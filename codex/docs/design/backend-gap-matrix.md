# AIGX 后端能力差距矩阵

> 目标：以 `new-api` 的能力域为参照，为 AIGX 制定可落地的增量方案。本文是设计基线，不代表当前工作目录已包含 AIGX 服务端源码。

| 能力域 | new-api 参考能力 | AIGX 当前基线 | 采用决策 | 工作量估算 |
|---|---|---|---|---|
| 渠道适配器 | 50+ 渠道适配器、统一路由 | 已有 cf/openai/Anthropic/gemini/zai 5 类 | 采用 trait + registry；优先接入 DashScope、ERNIE、Hunyuan、Doubao、MiniMax、Moonshot、SiliconFlow、Ollama、Cohere、Mistral、OpenRouter、Perplexity；Dify/Coze 延后 | 3–4 周 / 15 个优先适配器 |
| 多 API 形态 | Responses、Realtime WebSocket、Rerank、Embedding、Audio、Image、Video | 主要是 Chat Completions / Messages | P0 设计统一 API trait；P1 先做 Embedding、Rerank、Image；Realtime/Video 延后 | 2–3 周（首批） |
| 异步任务 | Midjourney、Suno、Video 任务及计费 | 暂无通用任务框架 | P2 引入 submit/poll/cancel/store 通用任务抽象，再实现具体适配器 | 2 周（框架，不含具体渠道） |
| 计费深度 | 预扣与结算、缓存差异计费、价格中心、比例、订阅、违规费用 | 已有输入/输出/缓存价格、模型与分组比例、多币种 | P0 采用预扣+结算与缓存差异计费；订阅及违规费用延后 | 2 周 |
| 认证安全 | Passkey/WebAuthn、2FA、OIDC、多 OAuth、会话限制、Casbin RBAC | 密码、GitHub/Google OAuth、JWT | P0/P1 做 TOTP 2FA、会话撤销/上限、通用 OIDC；Passkey、地区性 OAuth、Casbin 延后 | 2 周 |
| 模型治理 | 元数据同步、缺失检测、归属、排行、prefill 分组 | 价格配置中有基础模型列表 | P0/P1 做渠道模型同步、缺失检测、模型归属；排行与 prefill 延后 | 1 周 |
| 运营数据 | 用量看板、性能指标、ClickHouse、系统任务框架 | 基础 metrics/monitor、Prometheus、用量统计 | 增强成本、缓存、延迟、错误率看板；单体部署暂不引入 ClickHouse；补充 Rust 定时任务 | 1 周 |
| 渠道运维 | 连通性测试、自动更新、亲和模板、约束表达式、自动分组 | scheduler/balancer/circuit breaker/health manager/prober | 将 prober 正式化为 API；增加规则分组和亲和模板；约束 DSL/自动更新延后 | 1 周 |

## 统一落地原则

1. **协议与供应商解耦**：以 `ProviderAdapter`、`RequestTranslator`、`ResponseNormalizer` 三层抽象避免渠道分支污染核心路由。
2. **可观测性先行**：所有新 API 和适配器必须输出 request id、渠道、模型、首 token 延迟、总延迟、token 用量、错误分类。
3. **计费原子性**：预扣、上游结算、失败退款必须具备幂等键；任何重试不得重复扣费。
4. **能力声明**：适配器以 capability flags 声明支持的 API 形态，路由层在发送前拒绝不支持的请求。
5. **兼容优先**：优先扩展 OpenAI-compatible 入口，新增协议通过版本化 endpoint 进入，避免破坏现有客户端。

## 验收要点

- 渠道适配器可通过 registry 注册，不需要修改核心路由分支。
- 同一请求的预扣、结算、退款在重试和超时场景下保持幂等。
- 管理端可执行渠道连通性测试，并查看模型同步结果与最近失败原因。
- Embedding、Rerank、Image 的 API 契约有集成测试；未支持的 capability 返回明确错误码。
