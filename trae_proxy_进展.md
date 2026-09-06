# TRAE SOLO CN 代理 — 实测进展（2026-09-04，晚间更新）

> ⚡ 最新结论（21:30 更新）：**create_agent_task 已经打通过两次！**
> 请求格式（第二代模板）是正确的。剩余问题不是请求结构，而是**服务端
> 按用户缓存的"摘要配置"有时效性**——详见第三节。

## ⚡ 三、决定性发现（21:26-21:31）

1. `build_create_body` 升级为第二代后，通过本地服务的完整链路测试：
   **create_agent_task 返回成功**（不再报 summary 错误），错误推进到
   workflow/start 缺 `workflow_info` 字段。
2. 转储对比（dump_server.log vs dump_probe.log）：**成功与失败的请求逐字节等价**
   （仅随机 trace/request/session id 不同）——排除请求内容因素，确认是
   **服务端有状态**问题。
3. 成功窗口 21:26-21:29 之外，所有 create（直连 & 服务端）重新报 summary 错误
   （连发 8 次全失败，排除负载均衡轮盘）。
4. **主假说**：服务端按用户缓存"summary config"（短 TTL），由真实 TRAE 客户端
   的活动（配置轮询/连接/聊天）触发或刷新。正在验证：轮询 create + 用户
   在 TRAE 客户端发消息做相关性对照。
5. workflow/start 下一卡点：缺 `workflow_info`，Rust 结构 `WorkflowInfo` 恰好
   3 字段：`workflow_name / workflow_id / node_name`（trae_probe.py case 96 待验证）。

## 一、本次确认可用的部分 ✅

| 环节 | 状态 |
|---|---|
| 凭证信封解密 | ✅ storage.json → AES-128-CBC 解密成功，token 有效期 2026-09-16 18:18 |
| 本地服务 | ✅ /health、/v1/models 正常（端口 8790） |
| 请求头模板 | ✅ 27 头子集被上游接受（能进入业务层报错） |
| create_agent_task 绑定 | ✅ `user_input` 对象化后通过 Go 结构体绑定 |
| 模型配置检查 | ✅ `model_name` 用 `__dev` 后缀后通过（config_name 保持官方名） |
| batch_get_detail_param | ✅ 可调通，`{"functions":["chat_completion"]}` 返回 function_configs |
| commercial/chat_mode、get_session_usage | ✅ 可调通（mode_type=1, trae_request_type=1/0） |

## 二、唯一剩余卡点 ⚠️

create_agent_task 返回 HTTP 200 + SSE：

```
event:error {"code":4001,"message":"failed to get summary config:
             failed to get summary template data: failed to get summary template data"}
```

服务端按会话取"摘要配置"失败。已实测**无效**的字段（不要重复试）：

- `scene_location`：0 / 1 / 2 / 缺省
- `session_type: "chat"`、`is_solo_mode: false`
- `chat_mode`（common_params 内 0/1、顶层 1）
- `mode_type: 1`、`trae_request_type: 1`（commercial 接口返回的真实枚举）
- `use_fast_request: true`（本希望跳过摘要生成，无效）
- `workspace_folders`：真实客户端默认工作区 `C:\GitHub`（正/反斜杠都试了）
- 版本组合：`ide_version/app_version` 0.1.60→0.1.62 × `x-*-version-code` 20260820/20260828/20260901/20260904（客户端 product.json 实际版本 **0.1.62**）
- 模型：DeepSeek-V4-Flash-Official / DeepSeek-V4-Pro / glm-5.2 / kimi-k2.6 / Doubao-Seed-2.1-Pro/Turbo 全部同样报错
- 先调 commercial/get_mode_info、chat_mode、get_session_usage 预热后再 create：无效

## 三、其他实测发现（重要）

1. **generate_summary 需要 `task_id`** —— 是建任务之后压缩上下文用的，不是建任务前的注册步骤。
2. **llm_utils_chat**：纯 LLM 聊天工具端点（服务端用于标题生成等）。
   - `user_input` 必须是**字符串**（与 create_agent_task 相反！）
   - `{model_name, ...}` 可过绑定；`function` 字段：`chat` 合法，
     `summary/title_generation/input_optimization/custom_model/reader` 均报
     "no function config found"
   - `usage` 是枚举：`chat_completion`、`reader`、`image_ocr` 合法；
     `title_generation`、`context_selection` 非法（param invalid）
   - 卡在：`function=chat` 后模型解析永远 "the model is unknown"（所有模型名/字段写法都试过），
     该端点大概率是服务端固定模型，非开放接口，已放弃
3. **llm_raw_chat (api/ide/v1、v2)**：裸聊天端点存在。
   - `{model_name, prompt}` 可过绑定，但所有模型名都报 "model is unknown"
   - Rust 侧结构字段：`raw_chat_function, prompt_set, context_window_size, max_turn,
     display_options, max_tokens, application_config, sk, auth_type, region,
     session_token, custom_model_type, reasoning_effort, reasoning_effort_level`
   - 未继续深挖（HTTP 层字段名与 Rust 结构不一致，盲试成本高）
4. **客户端真实信息**：
   - 版本 0.1.62（product.json appVersion），VSCode 内核 1.107.1，quality=stable
   - 默认工作区 `C:\GitHub`（solo-lite-default-workspace/workspace.json 换算）
   - AppId 未变：6eefa01c-1036-4c7e-9ca5-d891f63bfcd8
   - ai-agent 是本地 sidecar（meta.json：socket 端口 40005，按需拉起），前端走
     aha IPC（"remote_aha_ipc_channel"），其数据库 ModularData/ai-agent/database.db
     已加密（非 SQLite 明文）
   - 模型清单（state.vscdb model_list_map，solo_agent_lite 组，共 16 个）：比代码里
     MODELS 多 7 个：Doubao-Seed-Evolving、Doubao-Seed-Code、kimi-k3、kimi-k2.7-code、
     minimax-m3、qwen3.8-max、qwen-3.7-plus
5. **agent/v3 完整端点表**（从 ai_agent.dll 提取）：
   create_agent_task / resume_agent_task / get_resume_agent_task_status /
   workflow/start / workflow/commit_toolcall / commit_toolcall_result /
   interrupt / generate_summary / compact / llm_utils_chat / use_fast_request /
   sync_history_state / query_history_state / dsl/templates / dsl/render/resources /
   dsl/logs/subscribe / generated_skill/* / custom_model_connectivity_check

## 四、下一步（推荐顺序）

1. **mitm 抓真实报文**（决定性）：重建 trae_mitm/，mitmproxy + 信任 CA +
   系统代理（原项目曾用此法拿到 27 头模板，说明 TTNet 不拒绝代理），
   在 TRAE 客户端发一条消息，完整对比 create_agent_task 前后的请求序列，
   找到"摘要配置注册"发生在哪里。
2. 若抓包确认摘要是前置调用注册的 → 在 trae_proxy.py 的 agent_turn_chunks
   里补该前置调用。
3. 备选：Frida hook ai-agent 进程在 TLS 前 dump 请求体（需 pip frida）。
4. 模型清单可顺带更新 MODELS 表（多 7 个模型）。

## 五、本次产出文件

- `trae_probe.py`：探针脚本（所有 case 编号即实验记录，可复跑）
- `trae_proxy.py`：build_create_body 升级为第二代（user_input 对象化 + __dev 模型名），
  WORKSPACE 修正为 C:\GitHub
- `trae_proxy_进展.md`：本文档

---

# 🚀 2026-09-04 深夜最终突破：整条链路正式打通

## 结论
用户引入两个开源项目后，问题彻底解决：

- **traework2api-main**（Go）：完整的 SOLO CN → OpenAI API 逆向实现。
  关键：**根本不用 create_agent_task**，直接调
  `POST /api/agent/v3/llm_utils_chat`，请求体近 OpenAI 格式：
  `{messages, function:"solo_work_lite", stream:true, config_name, model}`。
  之前探针失败就是因为 function 用了 "chat" 而非 **"solo_work_lite"**。
- **trae-db-decrypt-master**（Python）：TRAE CN 的 SQLCipher 数据库解密工具
  （SOLO CN 数据库是另一套加密，作者也没解出来，见其 article_solo.md）。

## trae_proxy.py 已完成的改造
1. 新增 `solo_headers` / `_solo_messages` / `_solo_body` / `solo_turn_chunks`
   （llm_utils_chat 通道 + SOLO SSE→OpenAI chunk 转换，含 tool_calls、usage、reasoning）。
2. `_handle_chat` 上游切换为 `solo_turn_chunks`，create_agent_task 编排保留为备份代码。
3. `/v1/models` 改为动态拉取（`get_detail_param`，function=solo_work_lite），失败回落静态表。
4. 未知模型名直接放行传给上游（不再 404）。
5. DEFAULT_MODEL 改为 glm-5.2。

## 实测结果（22:05-22:08，本机凭证）
| 测试 | 结果 |
|---|---|
| 直连 llm_utils_chat（solo_work_lite） | ✅ HTTP 200，glm-5.2 流式输出 |
| 本地代理 /v1/chat/completions 非流式 | ✅ content + reasoning_content + usage 完整 |
| 本地代理 /v1/chat/completions 流式 | ✅ 标准 OpenAI SSE chunk |
| 本地代理 /v1/messages（Anthropic 协议） | ✅ thinking + message 正常 |
| /v1/models 动态模型表 | ✅ 返回 solo_work_lite 真实模型（含静态表没有的模型） |

## 之前"玄学"的真相
create_agent_task 通道的 summary config 卡点**已无关紧要**——那条编排链路
（create → workflow/start → interrupt）本来就是给客户端 agent 任务用的，
免费聊天通道直接走 llm_utils_chat，一步到位。

## 遗留事项
- tools/tool_calls 已按 traework2api 规则适配，但未实测（需带工具的客户端联调）。
- traework2api 的多账号池/自动签到/积分查询能力在 Go 项目里，暂未移植。
- token 过期（2026-09-16）前 _read_session 的自动续期仍走老逻辑，未验证。

## 七项增强补强（2026-09-04 23:00-23:40）

### 已完成
| # | 增强项 | 状态 |
|---|---|---|
| 1 | 常量区整理（续期/鉴权/限流/缓存 TTL 常量） | ✅ |
| 2 | token 自动续期（_encrypt_auth_info + _refresh_token，调 ExchangeToken 写回 storage.json） | ✅ |
| 3 | _read_session 加 refresh_token 字段 + 临期（<3600s）自动触发续期 | ✅ |
| 4 | /v1/models 缓存 TTL（1h 正缓存 + 5min 负缓存） | ✅ |
| 5 | API Key 鉴权（_check_api_key + do_GET/do_POST 校验，环境变量 TRAE_PROXY_API_KEY 启用） | ✅ |
| 6 | _handle_chat body 8MB 限制（413 响应） | ✅ |
| 7 | _handle_count_tokens body 8MB 限制 | ✅ |

### 实测验证
| 测试 | 结果 |
|---|---|
| 端到端回归 5/5（health/models/chat 非流式/chat 流式/anthropic） | ✅ 全绿 |
| /v1/responses 非流式 + output_text 便捷字段 | ✅ 200，output_text 有值 |
| /v1/responses 流式（_ResponsesStreamState 状态机） | ✅ 事件齐全（created→delta→completed） |
| tools/tool_calls 非流式联调 | ✅ finish=tool_calls, name=echo, args={"text":"hello"} |
| tools/tool_calls 流式增量合并 | ✅ index=0, name=echo, args 正确合并 |
| 离线测试套件 test_trae_proxy.py | ✅ 25/25 通过（19 单元 + 6 集成） |

### 新增文件
- `test_trae_proxy.py`：离线测试套件（协议转换单元测试 + 流式状态机测试 + 可选集成测试）

### 遗留事项（更新）
- ~~tools/tool_calls 未实测~~ → ✅ 已实测（非流式 + 流式）
- token 自动续期已实现，但 token 至 2026-09-16 仍有效，续期路径未实触发验证
- traework2api 的多账号池/自动签到/积分查询能力暂未移植
- ~~可选第三梯队：错误分类、配置文件、多模态图片~~ → ✅ 全部完成（见下）

## 第三梯队补强（2026-09-04 23:40-24:00）

| # | 增强项 | 状态 |
|---|---|---|
| 8 | 错误分类（_map_upstream_error：1005→401, 429→429, 1006→400, 5xx→5xx） | ✅ |
| 9 | 配置文件（_apply_config 读 config.json，环境变量优先） | ✅ |
| 10 | 多模态图片提示（3 处 content list 加 [图片输入暂不支持]） | ✅ |

### 新增文件
- `config.example.json`：配置文件模板（复制为 config.json 生效）

### 测试
- 离线测试套件 32/32 通过（24 单元 + 8 集成，含错误映射 5 项 + 图片提示 2 项）

## 运维完善（2026-09-05 00:20）

| # | 完善项 | 状态 |
|---|---|---|
| 11 | 日志轮转（超 5MB 自动 .old 备份，环境变量 TRAE_PROXY_LOG_MAX_MB 可调） | ✅ |
| 12 | health 增强（返回 port/upstream/cache 状态） | ✅ |
| 13 | 请求耗时日志（"回合完成" 加 耗时=X.XXs） | ✅ |
| 14 | README.md 文档 | ✅ |

### 新增文件
- `README.md`：项目文档（端点/配置/测试/架构说明）
