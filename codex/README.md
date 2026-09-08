# 通用 Claude / Codex 本地配置部署脚本

> 一个可在任意机器上分发运行的部署脚本，自动探测 Claude（含 Claude Code CLI / Claude Code Haha 桌面端 / yume）与 Codex 的安装目录，并完成统一接入配置（自定义网关、模型分档、子代理分工、环境变量、密钥写入、非官方模型注册）。

零第三方依赖，仅需 Python 标准库（建议 3.10+）。npm 仅用于可选的全局路径探测。

**配套脚本**：`Claude/setup_yume.py` —— yume 桌面版专用一键部署（Node 22 + 官方 CLI + yume 安装器 + 网关配置 + 模型注册 + 权限全放行，已装组件自动跳过）。

---

## ★ 双网关与模型假名（2026-09 起的核心用法）

模型分档统一使用 **gpt-\* 假名**，两个网关都认识同一套名字，**切换网关不用换模型名**，只改脚本顶部的 `DEFAULT_BASE_URL` 后重跑部署：

| 网关 | 地址 | 说明 |
|------|------|------|
| **cfapi 云网关** | `https://cfapi.1232333.xyz/v1` | 远程 CF 网关，假名原生支持 |
| **ws-proxy 本地** | `http://127.0.0.1:8787/v1` | `D:\GitHub\llm-proxy\proxy.py`，认证复用 Workspace 编辑器登录态，需保持 `python -X utf8 proxy.py` 常驻运行 |

五个假名在两网关的映射（脚本只发假名，映射由网关自己做）：

| 假名 | cfapi 上游 | ws-proxy 上游 |
|------|-----------|---------------|
| `gpt-6-astra` | 旗舰推理 | gpt-5.6-luna 上游 |
| `gpt-5.6-luna` | 写码主力 | 同名直通 |
| `gpt-5.6-luna-fast` | 快速 | qwen3.8-max |
| `gpt-5.6-sol` | 深度推理 | aliyun-glm-5.2 |
| `gpt-5.6-sol-fast` | 最快 | qwen3.7-plus |

> 换网关示例：打开 `deploy_ai_cli.py`，把 `DEFAULT_BASE_URL = "https://cfapi.1232333.xyz/v1"` 改成 `http://127.0.0.1:8787/v1`，重跑部署（或临时 `--base-url http://127.0.0.1:8787/v1`）。
> ws-proxy 附带合规层：违规词 403、密码/密钥/手机号等自动脱敏转发；本地不校验 API Key。
> ws-proxy 只暴露部分模型：`hw-glm-5` / `qwen3.8-max` / `qwen3.7-plus` 仅本地网关可用，cfapi 无对应上游。

---

## 〇、新电脑部署步骤（按顺序执行）

> Windows 全流程约 5 分钟。前置条件：能上网；Windows 需管理员 PowerShell。

### Windows

**第 1 步：装 Node.js**（已装可跳过，`node -v` 能出版本号即已装）

下载安装：https://nodejs.org/ （选 LTS 版本，一路下一步）

**第 2 步：装 Python**（已装可跳过，`python --version` 能出版本号即已装）

下载安装：https://www.python.org/downloads/ （3.10+；安装时勾选 **Add python.exe to PATH**）

**第 3 步：装最新版 Codex**（管理员 PowerShell）

```powershell
npm install -g @openai/codex@latest
codex --version   # 确认输出 codex-cli 0.15x.x
```

**第 4 步：拿到本项目**

```powershell
git clone <你的仓库地址> C:\GitHub\deploy_ai_cli
# 或者从旧电脑直接拷贝整个 deploy_ai_cli 文件夹过去
```

**第 5 步：右键 `deploy.cmd` →「以管理员身份运行」**

它会自动完成：管理员/Python/Codex 检查 → 写入 Claude 与 Codex 全部配置 → 修复 Windows 沙箱 Temp 权限（`--auto-fix`）→ 信任项目目录 → 打印后续步骤。

不想用 cmd 也可以手动跑（效果相同）：

```powershell
cd C:\GitHub\deploy_ai_cli
python deploy_ai_cli.py --non-interactive --auto-fix `
  --trust-project "C:\GitHub\AIGX" `
  --trust-project "C:\GitHub\deploy_ai_cli"
```

**第 6 步：重开终端**（必须！让 `CF_GATEWAY_KEY`、`ANTHROPIC_*` 等用户级环境变量生效）

**第 7 步：验证**

```powershell
# Codex 对话测试
cd C:\GitHub\AIGX
codex exec "请回复：对话正常"

# 沙箱命令执行测试（应列出文件）
codex sandbox powershell -NoProfile -Command "ls | Select-Object -First 3"
```

两条都有正常输出即部署成功。

**日常使用姿势**（Windows 已知限制见第十章）：

```powershell
codex                                            # TUI 交互模式（推荐，可逐条审批命令）
codex exec --sandbox danger-full-access "任务"    # 自动化模式（无沙箱，信任任务用）
codex --profile fast                             # 切快模型（档位见第八章）
```

### macOS / Linux

```bash
# 1. 装 Node.js（brew 或官网）
brew install node

# 2. 装 Codex
npm install -g @openai/codex@latest

# 3. 跑部署脚本（无需管理员）
python3 deploy_ai_cli.py --non-interactive

# 4. 重开终端后验证
codex exec "请回复：对话正常"
```

> macOS/Linux 沙箱（Landlock/seccomp）原生可用，无需 `--auto-fix`，`workspace-write` 正常生效。

---

## 一、它能做什么

| 目标 | 配置内容 |
|------|----------|
| **Codex** | 生成 `~/.codex/config.toml`（custom provider + env_key）、8 个模型分档 `*.config.toml`（gpt-\* 假名 + qwen/hw 直名）、`auth.json`、用户级环境变量 `CF_GATEWAY_KEY`、`~/.codex/AGENTS.md`（多模型子代理委派指南，Codex 对话中可主动用 `spawn_agent` 把子任务派给其他模型） |
| **Claude** | 合并式写入 `~/.claude/settings.json`（保留你已有的字段）：三槽模型映射（opus=gpt-6-astra，sonnet=gpt-5.6-luna，haiku=gpt-5.6-luna-fast）、`modelPicker` 注册 13 个非官方模型名（gpt-\* 假名系 + 保留 glm/deepseek/kimi 旧条目，解决 `unrecognized_model`）、权限全放行 + `bypassPermissions`、语言中文；生成 3 个分工子代理 `~/.claude/agents/*.md`；写入 `ANTHROPIC_*` + `CLAUDE_DANGEROUS_MODE` 环境变量并清除致命的 `ANTHROPIC_MODEL` |

> Claude Code Haha 桌面端与官方 CLI 共用 `~/.claude`，以上配置它全部继承，重启即生效。

---

## 二、快速开始

```bash
# 最小运行（自动检测 + 交互询问缺失项）
python deploy_ai_cli.py

# 只探测、不写入（先看看检测结果）
python deploy_ai_cli.py --dry-run

# 全自动无人值守（需已能在现有配置里复用密钥）
python deploy_ai_cli.py --non-interactive --api-key sk-xxx

# 撤销最近一次写入
python deploy_ai_cli.py --rollback
```

---

## 三、命令行参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--base-url` | 自定义网关地址（Codex 用它带 `/v1`；Claude 写入时自动剥掉 `/v1`，因为 SDK 自己拼）。cfapi 与 ws-proxy 都认 gpt-\* 假名，切网关只改这一项 | 脚本顶部 `DEFAULT_BASE_URL` |
| `--api-key` | 网关 API Key（优先级最高；不提供则依次尝试 `--key-file` / 脚本内置 `DEFAULT_API_KEY` / 复用现有配置 / 交互询问） | 自动 |
| `--key-file` | 从文件读取 API Key | — |
| `--codex-home` | Codex 配置目录 | `~/.codex` |
| `--claude-home` | Claude 配置目录 | `~/.claude` |
| `--codex-bin` | 覆盖 Codex 可执行文件路径 | 自动检测 |
| `--claude-bin` | 覆盖 Claude 可执行文件路径 | 自动检测 |
| `--models-file` | JSON 文件，覆盖 Codex 模型分档 | 内置 8 档 |
| `--skip-codex` | 跳过 Codex 配置 | 否 |
| `--skip-claude` | 跳过 Claude 配置 | 否 |
| `--dry-run` | 仅探测，不写任何配置 | 否 |
| `--non-interactive` | 非交互模式，检测失败直接报错 | 否 |
| `--force` | 覆盖已存在的 `config.toml` | 否 |
| `--rollback` | 回滚最近一次配置写入 | — |
| `--verbose` | 输出 DEBUG 日志 | 否 |
| `--log-file` | 日志输出文件路径 | 控制台 |

---

## 四、自动检测机制

脚本按「**多来源 + 优先级 + 回退**」三层策略探测安装目录：

### 1. 检测来源（按顺序）

| 优先级 | 来源 | 适用 |
|--------|------|------|
| 1 | 命令行覆盖 `--codex-bin` / `--claude-bin` | 手动指定 |
| 2 | `PATH` 环境变量（`shutil.which`） | 全平台 |
| 3 | Windows 注册表 `App Paths` | Windows |
| 4 | 常见默认安装路径 | 全平台 |
| 5 | npm 全局目录（`npm root -g`） | 全平台（npm 可选） |
| 6 | 受限目录模糊搜索（`os.walk` + 深度截断） | 回退 |

### 2. 各平台默认路径清单

**Codex**

- Windows：`%APPDATA%\npm\codex.cmd`、`%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe`、`%ProgramFiles%\OpenAI\Codex\bin\codex.exe`、注册表、npm root
- macOS：`~/.npm-global/bin/codex`、`~/.local/bin/codex`、`/opt/homebrew/bin/codex`、`/usr/local/bin/codex`、npm root
- Linux：`~/.npm-global/bin/codex`、`~/.local/bin/codex`、`/usr/local/bin/codex`、`/usr/bin/codex`、npm root

**Claude**

- Windows：`~/.claude/local/claude.exe`、`~/.local/bin/claude.exe`、npm root；桌面端 `%ProgramFiles%\Claude Code Haha\*.exe`（精确 + `*Haha*` 模糊）
- macOS：`~/.claude/local/claude`、`/usr/local/bin/claude`、`/opt/homebrew/bin/claude`；桌面端 `/Applications/*Haha*.app`
- Linux：`~/.claude/local/claude`、`~/.local/bin/claude`、`/usr/local/bin/claude`、`/opt/Claude Code Haha/`

### 3. 回退策略

- 自动检测失败 → 进入**交互式选择**：列出所有候选 + 模糊搜索结果，可手动输入绝对路径，或回车跳过；

- `--non-interactive` 模式下检测失败 → 直接报错退出（便于 CI/无人值守环境冒烟）。

---

## 五、配置流程

```
解析参数 → 依赖检查 → 路径探测（含回退）→ [dry-run 则结束]
     → 解析密钥（参数 > key-file > DEFAULT_API_KEY > 复用现有 auth.json/settings.json > 交互）
     → 写入 Codex 配置 → 写入 Claude 配置 → 输出摘要与后续提示
```

### 密钥从哪来（按优先级）

| 优先级 | 方式 | 适用场景 |
|--------|------|----------|
| 1 | `--api-key sk-xxx` 命令行 | 临时使用，不落盘 |
| 2 | `--key-file /path/to/key` 密钥文件 | 密钥与脚本分离保管 |
| 3 | **脚本内置 `DEFAULT_API_KEY`**（见 `deploy_ai_cli.py` 顶部常量区） | 分发部署，把密钥填进占位符即可免参数免交互 |
| 4 | 自动复用目标机器现有 `auth.json` / `settings.json` 中的密钥 | 二次运行或已有配置 |
| 5 | 运行时交互输入 | 首次手动部署 |

内置方式用法：打开脚本找到这一行，填入真实密钥即可——

```python
DEFAULT_API_KEY = ""
```

> ⚠️ 安全提示：填好密钥的脚本副本请仅在受控范围分发，**不要提交到公开仓库**。密钥解析遵循上面的优先级，`--api-key` / `--key-file` 会覆盖内置值。

**幂等与安全**：

- `settings.json` 采用**合并式写入**：只增改 `env`/`modelPicker`/`permissions` 等字段，`proxy`/`network` 等你已有的字段原样保留；
- 写任何已存在文件前，先备份为 `<file>.bak.<时间戳>`，并记录进 `.deploy_backup_manifest.json`；
- `--rollback` 依清单恢复最近一批备份；
- 重复运行不会破坏已有配置（已存在的 profile/agent 自动跳过）。

### 非官方模型为什么能通过 CLI 校验（modelPicker 机制）

官方 Claude Code CLI 会在本地校验 `--model` / `/model` 传入的模型名，非官方名（`gpt-5.6-luna` 等）直接报 `unrecognized_model`。解法是在 `~/.claude/settings.json` 写：

```json
"modelPicker": {
  "options": [
    { "model": "gpt-6-astra", "behavesAs": "claude-opus-4-8" },
    { "model": "gpt-5.6-luna", "behavesAs": "claude-opus-4-8" },
    { "model": "gpt-5.6-luna-fast", "behavesAs": "claude-haiku-4-5" },
    { "model": "gpt-5.6-sol", "behavesAs": "claude-sonnet-4-6" },
    { "model": "gpt-5.6-sol-fast", "behavesAs": "claude-haiku-4-5" }
  ]
}
```

- `behavesAs` 指定这个陌生模型按哪个官方模型处理（能力探测、effort 默认值等客户端行为）；
- 实际请求仍把原始模型名（`gpt-5.6-luna`）发给网关，由网关映射到真实上游；
- 本脚本默认注册 13 个：gpt-6-astra / gpt-5.6-luna / gpt-5.6-luna-fast / gpt-5.6-sol / gpt-5.6-sol-fast（假名系）+ glm-5.3 / glm-5.3-flash / glm-5.2 / deepseek-v4-pro-0813 / deepseek-v4-flash-0731 / kimi-k2.7-code / kimi-k2.6 / glm-4.7-flash（旧条目保留兼容），全部实测通过。

**仍然不能用的**：环境变量 `ANTHROPIC_MODEL`——设了它照样报 `unrecognized_model`（CLI 对这个变量不走 modelPicker 白名单），脚本会主动从注册表清掉它。模型映射只走 `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL` 三槽。

---

## 六、错误处理与日志

- 日志分 `DEBUG / INFO / WARNING / ERROR` 四级，默认 INFO；
- 密钥缺失、路径探测失败、备份清单写入失败等均不中断主流程，按可回退策略降级处理；
- 所有报错会给出明确原因与下一步建议（例如提示加 `--codex-bin` 覆盖）；
- 完整日志可 `--log-file out.log` 落盘排查。

---

## 七、各平台运行示例

### Windows（PowerShell）

```powershell
# 干跑看检测结果
python .\deploy_ai_cli.py --dry-run

# 全自动部署（密钥自动复用，无需任何输入）
python .\deploy_ai_cli.py --non-interactive

# 覆盖 Codex 路径 + 指定网关
python .\deploy_ai_cli.py --codex-bin "D:\tools\codex\codex.exe" --base-url "https://my-gw.example.com"
```

### macOS

```bash
python3 deploy_ai_cli.py --dry-run
python3 deploy_ai_cli.py --api-key sk-xxx
```

### Linux

```bash
python3 deploy_ai_cli.py --dry-run
python3 deploy_ai_cli.py --key-file /etc/ai-gateway.key --non-interactive
```

---

## 八、部署完成后怎么用

**Codex 模型分档切换**（8 档假名，两网关通用；直接跑 `codex` 不带 profile 时用主模型 `gpt-5.6-luna`）：

```bash
codex --profile fast     # gpt-5.6-luna-fast  日常快改
codex --profile sfast    # gpt-5.6-sol-fast   最快
codex --profile mid      # gpt-5.6-sol        均衡主力
codex --profile qwen     # qwen3.8-max        长上下文
codex --profile qwenp    # qwen3.7-plus       轻量
codex --profile hw       # hw-glm-5           （仅 ws-proxy 有此模型）
codex --profile code     # gpt-5.6-luna       写码
codex --profile deep     # gpt-6-astra        全力
```

**Claude 子代理分工**（一次任务多模型接力）：

| 子代理 | 绑定模型 | 用途 |
|--------|---------|------|
| `code-reviewer` | gpt-5.6-sol | 代码审查 |
| `fast-writer` | gpt-5.6-luna-fast | 文档/文案/小修补 |
| `architect` | gpt-6-astra | 方案设计 |

**Claude 对话中手动切模型**（`/model <名字>`，已在 modelPicker 注册）：

```
gpt-6-astra            旗舰推理（opus 档行为）
gpt-5.6-luna           写码主力（opus 档行为）
gpt-5.6-luna-fast      快速（haiku 档行为）
gpt-5.6-sol            深度推理（sonnet 档行为）
gpt-5.6-sol-fast       最快（haiku 档行为）
```

> 旧条目 glm-5.3 / glm-5.3-flash / glm-5.2 / deepseek-v4-pro-0813 / deepseek-v4-flash-0731 / kimi-k2.7-code / kimi-k2.6 / glm-4.7-flash 仍保留在 modelPicker 兼容历史配置，但分档与子代理已全部切换到假名体系。

**Codex 子代理委派**（自定义 API，无需原生 spawn_agent）：

部署会自动写 `~/.codex/AGENTS.md` 并注册 MCP 工具 `spawn_agent`。在 Codex 对话里直接说
「把代码审查交给 分析 子代理」或「让 写码 子代理实现这个函数」，主模型就会把子任务
委派给对应模型（全部走你的自定义网关），拿到结果后自行汇总。

| 子代理 | 模型 | 适合委派的任务 |
|--------|------|----------------|
| 指挥官 | gpt-6-astra | 方案设计、横向对比、任务拆解 |
| 分析 | gpt-5.6-sol | 代码审查、架构分析、疑难 bug |
| 写码 | gpt-5.6-luna | 实现函数、按规格落地代码 |
| 快速 | gpt-5.6-sol-fast | 小修补、批量机械修改 |
| 快答 | gpt-5.6-luna-fast | 文档、注释、commit 说明 |

也支持别名：astra / sol / luna / sol-fast / luna-fast。脚本侧可用
`python multi-model.py` 里的 `spawn_agent(agent, task)` / `spawn_many(delegations)`
直接并行委派。

---

## 九、自定义模型分档（`--models-file`）

传入 JSON 文件覆盖默认分档，格式 `{"档位名": ["模型名", "effort"]}`：

```json
{
  "fast": ["gpt-5.6-luna-fast", "low"],
  "deep": ["gpt-6-astra", "high"]
}
```

```bash
python deploy_ai_cli.py --models-file ./my_models.json
```

---

## 十、Windows 已知问题与内置规避（2026-09 实测）

本脚本在 Windows 上已内置以下规避与体检，无需手动处理：

### 1. 网关 404（`base_url` 缺 `/v1`）

codex 的 `wire_api = "responses"` 直接拼 `base_url + "/responses"`。网关地址不带 `/v1` 时请求打到不存在的端点，模型连接直接 404。

**内置规避**：`_build_codex_config` 会自动给 `base_url` 补 `/v1` 后缀，无需手动处理。

### 2. Git Bash 在受限沙箱内崩溃（`CreateFileMapping error 5`）

codex 的 Windows 受限 token 沙箱会剥离 `SeCreateGlobalPrivilege`，而 Git Bash（msys/cygwin 运行时）启动时必须用该权限创建以用户 SID 命名的共享内存段 → `CreateFileMapping` error 5 → 所有 Git 工具（`ls`/`bash`/`whoami`）在沙箱内崩溃。

**内置规避**：生成的 `config.toml` 指定 PowerShell 作为执行 shell：

```toml
[shell]
windows_default = "powershell"
```

### 3. 沙箱用户 Temp 权限丢失

删除 `~/.codex` 后重装，沙箱用户（`CodexSandboxOnline/Offline`）对 `C:\Windows\Temp` 的写权限可能丢失，同样触发 `CreateFileMapping error 5`。

**部署后体检**：脚本会运行 `codex sandbox` 测试原生 PowerShell，失败时打印可直接复制的修复命令（需管理员执行）：

```powershell
icacls C:\Windows\Temp /grant "CodexSandboxOnline:(OI)(CI)F"
icacls C:\Windows\Temp /grant "CodexSandboxOffline:(OI)(CI)F"
```

### 4. `codex exec` 忽略 sandbox 配置（codex 0.153.x Windows 已知问题）

Windows 上 `sandbox_mode = "workspace-write"` 配置与 `-s workspace-write` 参数均不生效，`exec` 恒为 `read-only` + `approval never`，所有命令被 `blocked by policy` 拒绝。

**当前可用方案**：唯一有效的是 `--sandbox danger-full-access`（跳过受限 token，以当前用户身份执行）：

```powershell
cd C:\path\to\repo
codex exec --sandbox danger-full-access "你的任务指令"
```

> ⚠️ `danger-full-access` 无沙箱隔离，命令直接以你的用户身份执行，仅用于信任的任务。等 codex 上游修复 Windows 沙箱后可切回 workspace-write。

---

## MCP：codex 会话内多模型协作

`mcp_server.py` 是一个 MCP stdio server，把 multi-model.py 的多模型团队能力暴露为 8 个 MCP 工具，注入 codex 会话后即可在对话中调用。

### 注册

部署时自动注册（默认行为）：
```bash
python deploy_ai_cli.py
```
会在 `~/.codex/config.toml` 追加：
```toml
[mcp_servers.multi_model]
command = "C:\\Python313\\python.exe"
args = ["C:\\GitHub\\llm-proxy\\codex\\mcp_server.py"]
```

跳过注册：`python deploy_ai_cli.py --skip-mcp`

### 验证

```bash
python mcp_server.py --self-check
```
输出 8 个工具清单表示就绪。

### 工具清单

| 工具 | 类型 | 用途 |
|------|------|------|
| multi_ask | 同步 | 单模型问答（55s 预算） |
| multi_list_models | 同步 | 列出可用模型 |
| multi_ping | 同步 | 探活 |
| multi_team_start | 异步 | 启动五阶段团队流水线 |
| multi_orchestrate_start | 异步 | 启动指挥官拆解+并行 |
| multi_parallel_start | 异步 | 启动多模型同问对比 |
| multi_task_status | 查询 | 轮询任务进度（<2s） |
| multi_task_result | 查询 | 取最终交付 |

### 两段式用法示例（codex 会话内）

codex 在对话中可以调用这些工具。典型工作流：

1. **启动团队任务**：
   ```
   multi_team_start(task="给 utils.py 增加 XXX 并附测试", workdir="C:/GitHub/myproject")
   → 返回 {"task_id": "team-20260907-193012-a3f4", "status": "queued"}
   ```

2. **轮询进度**（codex 自主决定何时查）：
   ```
   multi_task_status(task_id="team-20260907-193012-a3f4")
   → 返回 {"phase": 3, "phase_name": "审查", "round": 1, "files_written": ["src/utils.py"]}
   ```

3. **取最终交付**：
   ```
   multi_task_result(task_id="team-20260907-193012-a3f4")
   → 返回最终汇总 + 文件清单 + 审查结论
   ```

### 桌面端覆写恢复

Codex 桌面版运行时会重写 `~/.codex/config.toml`，可能丢失 MCP 注册。恢复方法：
```bash
python deploy_ai_cli.py --force
```
或手动在 config.toml 追加 `[mcp_servers.multi_model]` 段。

### 故障排查

| 症状 | 排查 |
|------|------|
| codex 会话内看不到 multi_* 工具 | 检查 config.toml 是否有 `[mcp_servers.multi_model]` 段 |
| multi_ping 返回错误 | `python mcp_server.py --self-check` 确认 8 工具就绪 |
| multi_ask 报网关连接失败 | `python allin.py` 完成 SSO 认证；确认 proxy.py 在运行 |
| multi_team_start 返回"任务数达上限" | 等待现有任务完成或重启 codex（server 退出即清理） |
| 任务句柄失效 | server 重启后 JOBS 清空；用 `python multi-model.py team --resume <task_id>` 从状态文件续跑 |

### 环境变量

| 变量 | 默认 | 用途 |
|------|------|------|
| MM_SYNC_BUDGET_SEC | 55 | 同步工具（multi_ask）执行预算（秒） |

---

## 远程 Codex app-server 客户端（codex-remote-cli.py）

`codex-remote-cli.py` 是一个**零依赖**（纯标准库）的 WebSocket + JSON-RPC 客户端，直接连远程 Codex app-server，在本地管理远程会话 / 发消息 / 切模型，无需进入 TUI。

### 基本用法

```bash
python codex-remote-cli.py list                          # 列出远程会话
python codex-remote-cli.py info                          # 远程 server 信息
python codex-remote-cli.py model                         # 列出远程可用模型
python codex-remote-cli.py model set gpt-6-astra         # 切模型（写远程 config）
python codex-remote-cli.py start --cwd /opt/Codex "任务" # 新建会话 + 首条消息
python codex-remote-cli.py send --thread <id> "追加"      # 空闲会话追加消息
python codex-remote-cli.py read --thread <id>            # 读会话元数据
python codex-remote-cli.py items --thread <id> [--turn <id>]  # 列 items
```

凭据优先级：`--token` > 环境变量 `CODEX_WS_TOKEN` > `codex-remote-config.json` 的 token。
连接地址优先级：`--host`/`--port` > `CODEX_WS_HOST`/`CODEX_WS_PORT` > config 文件。

### ★ 中途介入：不打断 vs 打断（重点）

会话**进行中**想实时改变方向，有两条命令，语义截然不同：

| 命令 | 底层方法 | 是否打断 | 效果 |
|------|---------|---------|------|
| `steer` | `turn/steer` | **不打断** | 给进行中的 turn 注入新指令，turn 保持 `inProgress` 继续跑，最终 `completed` |
| `interrupt` | `turn/interrupt` | 打断 | 显式终止当前 turn，状态变 `interrupted` |

```bash
# 想「改方向但别停」——用 steer（不打断）：
python codex-remote-cli.py steer --thread <id> --turn <id> "换个思路，改成 XX"

# 想「叫停」——才用 interrupt：
python codex-remote-cli.py interrupt --thread <id> --turn <id>
```

- `steer` 的 `--turn` 必须是**当前活跃 turn 的 id**，服务端校验不符会拒绝（避免打到旧 turn）。
- `send` 走 `turn/start`，只在空闲会话上开新 turn，同样不打断。
- 协议依据：`TurnStatus` 只有 `completed / interrupted / failed / inProgress` 四种，无独立的「steered」状态——steer 后 turn 仍是 `inProgress`，因此不产生中断。

### 模型切换语义

- `model set` 走协议 `config/value/write`（写远程 `config.toml` 的 `model` 字段），**不重启服务**，只影响之后新建的 turn。
- 若远程环境需要「改写配置 + 重启 app-server」的切换方式，可改用同目录 `remote-model.py`（SSH 调用）。
- 给**单个会话/单次 turn** 指定模型：`start` / `send` / `steer` 均支持 `--model <假名>` 覆盖。