
# 多模型共用 · GitHub 大佬对标头脑风暴

> 用 GitHub 真实项目校准 `codex/` 的多模型共用方向。数据来源：GitHub Search API
> （按 stars 排序，排除 fork/归档，检索时间 2026-09-07）。
> 每条都给出「大佬怎么做 → 我们能抄什么 → 放哪个文件」。

---

## 一、对标项目全景（真实数据）

| 项目 | Stars | 一句话 | 对我们最有价值的点 |
|------|------:|--------|-------------------|
| songquanpeng/one-api | 36.8k | LLM API 管理+分发，统一适配 | 渠道/模型/价格中心、二次分发 |
| openai/openai-agents-python | 29.2k | 多智能体工作流框架 | handoff 交接、guardrails、agent 抽象 |
| decolua/9router | 27.3k | 给 Codex/Claude Code 用的免费路由 | 订阅→低价→免费三级降级、token 压缩 |
| deepset-ai/haystack | 26.4k | 管道式 AI 编排 | Router/Retriever/Generator 组件化 |
| tashfeenahmed/freellmapi | 24.7k | 34 家免费 API 聚合成一个 /v1 | 全自动 failover、额度追踪、模型目录远程更新 |
| Portkey-AI/gateway | 12.9k | <1ms 网关，1600+ 模型 | fallback/load balancing/conditional routing/guardrails |
| coaidev/coai | 9.3k | 一站式 AI+计费 | 优先级路由、内置计费、模型缓存 |
| katanemo/plano | 7.0k | Rust 写的 agent 数据面代理 | 智能路由、可观测、编排 |
| BlockRunAI/ClawRouter | 6.6k | agent 原生 LLM 路由 | <1ms 本地路由、钱包计费 |
| vllm-project/semantic-router | 5.6k | Mixture-of-Models 可编程路由 | 按信号/偏好/策略选或组合模型 |
| lm-sys/RouteLLM | 5.5k | 路由框架：省钱不降质 | 强弱模型自动路由、成本/质量权衡 |
| AgentOps-AI/agentops | 5.8k | Agent 监控/成本追踪 | 用量、成本、错误统一观测 |
| alfredolopez80/multi-agent-ralph-loop | 146 | Claude Code 多智能体编排 | 4 层记忆、6 队友、4 级质量门 |
| Lightning-AI/litAI | 52 | 纯 Python 路由+轻 agent | retry+fallback+统一计费，零魔法 |

**规律**：大佬们全部收敛到同一件事——**客户端只说假名/统一 API，路由、降级、
计费、观测全部收在中间层**。这正是 codex/ 已走对的方向，缺的是把中间层做深。

---

## 二、按现有架构逐层对标

### 1. 网关层（对照 proxy.py / cfapi）

**大佬做法**
- 9router：三层降级链（Subscription → Cheap → Free），配额耗尽自动降级，
  工具输出压缩省 20-40% token。
- freellmapi：34 家 provider 自动 failover + 每 key 额度追踪，模型目录靠签名
  feed 远程更新，不用 git pull。
- Portkey：fallback + load balancing + conditional routing + guardrails，
  <1ms 转发。
- one-api/coai：渠道组、优先级路由、价格中心、计费预扣。

**可抄清单（按收益排序）**
- **P0**：`proxy.py` 的 `MODEL_ALIAS` 增加 per-别名 `fallback` 列表 + 优先级，
  上游 429/5xx/超时自动走下一级（参考 9router 三层思想，但只连 Workspace 上游，
  不新增外呼）。
- **P0**：响应侧把 `usage` 与耗时写入审计行（已有 audit jsonl 基础），
  为成本看板攒数据。
- **P1**：模型目录改成 JSON feed（对应「模型目录远程更新」，我们落
  `models.json`，本地优先）。
- **P1**：Portkey 式 conditional routing：按请求里 `effort`/意图字段选上游档位，
  而非只按假名。

### 2. 编排层（对照 multi-model.py）

**大佬做法**
- openai-agents：Agent 抽象 + **handoff**（一个 agent 把上下文结构化交接给
  另一个，不是截断拼接）。
- ralph-loop：6 个专业队友 + **4 级质量门**（correctness/quality/security/
  consistency，逐级阻塞）+ 4 层记忆（L0 身份/L1 规则/L2 项目规则/L3 知识库，
  关键发现：**选择优于压缩**——挑少量规则比编码压缩更省 token）。
- semantic-router / RouteLLM：按请求信号（意图、成本偏好）选或组合模型；
  弱模型先答、强模型只在不确定时上（成本-质量权衡）。
- haystack：Router/Retriever/Generator 组件化管道。

**可抄清单**
- **P0**：把 `multi-model.py` 的阶段间「截断拼接」（`_handoff_*`）升级成
  **handoff 协议**：每阶段输出结构化 `{决策, 文件清单, 遗留问题, 验证方式}`，
  下游只读结构字段，上限收紧（ralph 的「选择优于压缩」直接照搬）。
- **P0**：审查阶段从「pass/fix」两态升级为 ralph 式**四级质量门**：
  correctness → quality → security → consistency，任一未过不回改目标，不判 pass。
- **P1**：RouteLLM 式**分级路由**：orchestrate 拆解时给子任务打难度分，
  低难度→fast 档，高难度/不确定→astra 档；`parallel` 增加 `--hedge` 双飞，
  首完成者胜出，预算封顶（父目录 AIGX roadmap 已列为 P2 方向）。
- **P1**：ralph 式**轻量记忆**：`~/.multi-model/L1_rules.json` 存跨任务学到的
  规则（约 800 token 上限），每次 team 启动注入，避免重复踩坑。
- **P2**：组件化：把 `agent_loop / call / handoff / review` 拆成可替换组件，
  对应 haystack 管道（零依赖前提下内部重构）。

### 3. 接入层（对照 deploy / win-code / codex-remote / mcp_server）

**大佬做法**
- 9router：所有 CLI（Claude Code / Codex / Cursor / Cline）指向同一个
  `localhost:20128/v1`，模型名带 provider 前缀（`kr/claude-sonnet-4.5`）。
- litAI：统一 `model("provider/model")` 命名 + retries + fallbacks +
  unified billing，纯 Python 零魔法。
- Portkey MCP Gateway：MCP server 的鉴权与观测走网关。
- agentops：所有 agent 框架的成本/延迟/错误统一追踪。

**可抄清单**
- **P0**：单一事实源 `codex/models.json`（roles/profiles/aliases/upstream/fallback/
  price），deploy / multi-model / win-code / proxy 四脚本统一读；缺文件回退内置。
- **P0**：统一模型命名 `provider/model` 或保留 gpt-* 假名但**强制唯一入口**
  解析（alias 解析函数只写一处，各脚本 import）。
- **P0**：补全 `mcp_server.py` 的 `tools/call`（T7），让 Codex 通过 MCP 直接
  调度多模型团队；MCP 工具加 `multi_usage` 汇报成本。
- **P1**：远程 app-server 也指向同一 `models.json`/网关，`codex-tm.py` 场景
  改按 profile 启动，消灭「一场景一模型」的旧模式。
- **P1**：agentops 式观测：统一 `usage.jsonl`（每模型 token/延迟/成本/错误），
  panel.py 画简单看板。

### 4. 计费/观测（对照父目录 AIGX roadmap）

- one-api/coai 的「渠道优先级 + 计费预扣 + 价格中心」正好对上 AIGX P0；
  本地侧先做「价格表 + usage 采集」，等 AIGX 服务端就绪后接 request id 对账。
- freellmapi 的「远程目录 feed」启发：模型/价格表放 `models.json`，
  `--sync` 从 git 拉最新，实现无 git pull 更新。

---

## 三、对比结论：我们 vs 大佬

| 维度 | 大佬主流 | codex/ 现状 | 差距 |
|------|---------|------------|------|
| 统一命名 | 假名/前缀路由 | 已有 gpt-* 假名 ✅ | 小（解析分散在多文件） |
| 自动降级 | 标准配置 | 无（orchestrate 失败只记录） | 大 |
| 质量门 | ralph 四级门 | pass/fix 两态 | 中 |
| 交接 | 结构化 handoff | 截断拼接 | 中 |
| 记忆 | L0-L3 分层 | 无跨任务记忆 | 大 |
| 计费观测 | agentops/one-api | 无 | 大 |
| 意图路由 | RouteLLM/semantic-router | 静态角色绑定 | 大 |
| MCP 面 | Portkey MCP GW | 工具已定义未接线 | 小 |

**一句话战略**：大佬们证明「路由+降级+计费收中间层」是正确方向，我们已有骨架，
接下来把**自动降级、结构化 handoff、四级质量门、usage 采集**四个 P0 做进
`multi-model.py` + `proxy.py`，就达到主流开源网关 80% 的能力。

---

## 四、落地路线（修正上一版方案）

- **P0（1-2 周）**：models.json 单一事实源；proxy fallback 链；multi-model 的
  handoff 协议 + 四级质量门；usage.jsonl 采集；mcp_server tools/call 接线。
- **P1（2-3 周）**：RouteLLM 式分级路由 + hedged；ralph 式 L1 规则记忆；
  远程会话/模型统一；panel 成本视图。
- **P2（对齐 AIGX）**：接 AIGX 计费/价格中心；模型目录 feed 同步；组件化编排。

**风险红线不变**：不新增外呼（9router/freellmapi 的多 provider 聚合暂不做，
只路由到既有 Workspace 网关）；合规层（拦截/脱敏/审计）只增不减；
零 pip 依赖。
