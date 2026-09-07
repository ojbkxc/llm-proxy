
# 多模型共用设计方案

> 目标：让 Codex / Claude / 多模型编排器 / MCP 工具族真正共用一套模型资源——一份配置、
> 一套命名、一条认证链、统一路由与计费，而不是每个脚本各自维护一份模型表。

---

## 一、现状盘点（codex 目录已有功能）

### 1. 接入层：deploy_ai_cli.py

- 自动探测 Claude / Codex 安装位置并写配置，支持 `--dry-run` / `--rollback` / `--force`。
- Codex 侧生成 `~/.codex/config.toml`（custom provider，`model_provider = "custom"`、
  `wire_api` 走 responses，自动补 `/v1` 防 404），并按 `DEFAULT_CODEX_PROFILES` 生成
  `~/.codex/<档位>.config.toml` 文件式 profile（`codex --profile fast/mid/code/deep`）。
- Claude 侧写 `settings.json`、`CLAUDE_MODEL_ENV` 环境变量、`MODEL_PICKER` 注册
  gpt-* 假名（behavesAs 官方模型，绕开官方 CLI 的模型名校验）、三个子代理分工。
- 写 `CF_GATEWAY_KEY` 等用户级环境变量，Windows 下自动修沙箱 Temp ACL。
- `install_env.py` 负责新机环境（Node/Python/codex/Git），`deploy.cmd` 一键衔接。

### 2. 网关层：两套网关 + gpt-* 假名

- **cfapi 云网关** `https://cfapi.1232333.xyz/v1`：假名原生支持，dashboard 管理。
- **ws-proxy 本地** `proxy.py`（`http://127.0.0.1:8787/v1`）：
  - `MODEL_ALIAS` 表把假名映射到 Workspace 上游真实模型（`gpt-6-astra → gpt-5.6-luna` 等）；
  - 认证复用 Workspace 编辑器登录态、自动续期；合规层（违规词 403、PII 脱敏还原）；
  - 三协议转换：OpenAI chat / Responses（codex 用）/ Anthropic messages，响应侧把
    model 字段改回客户端请求的假名，否则 Codex「请求模型==响应模型」校验失败。

### 3. 编排层：multi-model.py（核心）

- 五个角色共用五个假名：指挥官(gpt-6-astra)/分析(gpt-5.6-sol)/写码(gpt-5.6-luna)/
  快速(gpt-5.6-sol-fast)/快答(gpt-5.6-luna-fast)。
- 四种多模型用法：`ask` 单模型、`parallel` 真并行同问对比、`orchestrate` 指挥官拆解
  →并行→汇总、`team` 五阶段流水线（分析设计→写码落地→审查→迭代回改→汇总）。
- 自带工具层（list_dir/read_file/write_file/search/run_command）+ agent_loop，危险命令
  黑名单拦截；阶段边界状态原子落盘支持 `--resume`；`auto` 无人值守循环跑。
- `--engine codex` 可把写码阶段委托给本地 codex CLI。

### 4. 接入面：panel / MCP / 远程 / allin

- `panel.py`：Tk 图形面板包 multi-model。
- `mcp_server.py`：MCP stdio server，8 个工具已定义（multi_ask / multi_list_models /
  multi_team_start / multi_orchestrate_start / multi_parallel_start / multi_task_status /
  multi_task_result / multi_ping），但 `tools/call` 尚未实现（T7 占位错误）。
- `codex-remote-cli.py`：纯标准库 WebSocket JSON-RPC 客户端连远程 app-server。
- `win-code.py`：本地/远程 Codex 启动器，含旧版 `PROFILES` 表（真名 glm/kimi/deepseek）。
- `remote-multi.py`：SSH(paramiko) 到服务器跑 multi-model.py。
- `allin.py`：拉起 proxy.py + 等认证 + 转发给 multi-model/panel。
- 服务器侧 `codex-tm.py`：tmux 一场景一模型管理。

---

## 二、问题诊断：为什么还不算「真共用」

1. **模型表重复维护**：`deploy_ai_cli.py`（8 档）、`multi-model.py`（5 角色+别名）、
   `win-code.py`（旧真名档）、`proxy.py`（MODEL_ALIAS）、cfapi dashboard 五处各存一份
   映射，改一个模型要同步五处，目前只靠 README 约定。
2. **远程与本地隔离**：远程 app-server 的会话/模型由服务器 `~/.codex/config.toml`
   决定，本地切换模型无法作用到远程；`codex-tm.py` 是「一个 tmux 场景一个模型」，
   与假名体系不一致。
3. **路由是静态的**：所有角色/档位固定绑定假名，没有按任务意图、成本、健康度动态
   路由；并行只有「同问对比」，没有失败降级、hedged request 或竞速。
4. **MCP 半成品**：`tools/call` 未实现，MCP 面还不能真正调用多模型。
5. **无共用计费/审计面**：各入口直接打网关，用量、成本、错误无法跨入口汇总。

---

## 三、目标形态：四层架构

```text
┌────────────────────────────────────────────────────────────┐
│ 接入面  codex CLI / Claude / panel.py / MCP server / 远程  │
├────────────────────────────────────────────────────────────┤
│ 编排层  multi-model.py（角色→任务，路由→模型）             │
├────────────────────────────────────────────────────────────┤
│ 网关层  ws-proxy(本地) ⇄ cfapi(云)  —— 假名统一命名空间    │
├────────────────────────────────────────────────────────────┤
│ 上游    真实模型（gpt-5.6-luna / glm / deepseek / kimi…）   │
└────────────────────────────────────────────────────────────┘
```

核心原则：**客户端只说假名，路由收敛在编排层，映射只存在网关层。**

---

## 四、分阶段方案

### P0：单一事实源 + MCP 打通（改代码）

**1. 模型注册表收敛到一个 JSON**

新增 `codex/models.json`：

```json
{
  "roles": {
    "指挥官": {"model": "gpt-6-astra",     "effort": "high", "temp": 0.4},
    "分析":   {"model": "gpt-5.6-sol",     "effort": "medium", "temp": 0.3},
    "写码":   {"model": "gpt-5.6-luna",    "effort": "high", "temp": 0.2},
    "快速":   {"model": "gpt-5.6-sol-fast","effort": "low", "temp": 0.5},
    "快答":   {"model": "gpt-5.6-luna-fast","effort": "low", "temp": 0.5}
  },
  "profiles": {
    "fast":  ["gpt-5.6-luna-fast", "low"],
    "mid":   ["gpt-5.6-sol", "medium"],
    "code":  ["gpt-5.6-luna", "high"],
    "deep":  ["gpt-6-astra", "high"]
  },
  "aliases": {"astra": "gpt-6-astra", "sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna"}
}
```

- `multi-model.py` 启动时 `models.json` 优先、内置 `ROLES` 兜底（不破坏无文件运行）；
- `deploy_ai_cli.py` 的 `DEFAULT_CODEX_PROFILES` / `CLAUDE_AGENT_SPECS` 改为读同一文件；
- `win-code.py` 的旧 `PROFILES` 表合并进注册表，删除真名档或只留兼容别名；
- 网关映射（`proxy.py` 的 `MODEL_ALIAS`）也从该文件生成（可加 `upstream` 字段），
  cfapi dashboard 的假名表作为云侧副本。

**2. 补全 mcp_server.py 的 T7**

- 实现 `tools/call` 分发：把 8 个工具接到 multi-model 的 `ask/parallel/orchestrate/team`
  与状态查询（读 `state-*.json`），start 类工具立即返回 task_id。
- 接 Codex 的 `mcp_servers` 配置（`~/.codex/config.toml` 注册 stdio server），
  使 Codex 自身变成「多模型团队的入口」。

**3. 统一远程会话的模型切换**

- `codex-remote-cli.py` 支持发 `config/set model=xxx` 类 JSON-RPC 或新增按会话启动参数，
  让远程 app-server 也用假名体系；`codex-tm.py` 场景改为按 profile 而非硬编码模型。

### P1：动态路由 + 失败降级

- **意图路由**：orchestrate 拆解阶段由指挥官输出子任务「意图」标签，编排层按意图
  与各角色 effort/成本选假名（写码→luna，深度→astra，杂活→fast）。
- **hedged requests / 竞速**：`parallel` 增加 `--hedge` 模式，同请求发 2 个健康模型，
  首 token 或首完成者胜出，其余取消；加预算上限（单请求额外成本封顶）。
- **失败降级链**：`call()` 层加 per-model 降级表（如 luna 失败→sol-fast），
  错误分类（429/5xx/超时）触发不同降级策略，全部记入 stage_errors。

### P2：共用计费与审计 + 面板升级

- 编排层加统一用量采集（每模型 token/延迟/成本），落 `~/.multi-model/usage.jsonl`，
  `multi_list_models` 增补健康度与成本字段。
- `panel.py` 加模型健康/成本视图；远程 dashboard 接入同一审计流。
- 与父目录 AIGX 路线图（P0 计费预扣、P1 成本看板）对齐，网关侧保留 request id 链路。

---

## 五、风险与红线

- **不削弱合规层**：所有改动不得绕过 `proxy.py` 的账号拦截、违规词 403、PII 脱敏
  还原与审计写入（见 `llm-proxy/AGENTS.md`）。
- **零 pip 依赖**：codex 目录全部脚本保持 Python 标准库（remote-multi 的 paramiko
  是例外，保持不变）。
- **假名兼容**：`models.json` 缺失时各脚本必须回退内置默认，保证单文件分发仍可用。
- **密钥安全**：`DEFAULT_API_KEY` 已明文在 `deploy_ai_cli.py`，注册表文件不得再
  存放新密钥；网关切换保持「改一处」原则。
