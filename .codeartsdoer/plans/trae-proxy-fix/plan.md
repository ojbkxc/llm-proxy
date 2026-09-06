# TRAE 代理 create_agent_task 400 修复计划

## 背景与卡点

`trae_proxy.py` 目标：把 TRAE SOLO CN（字节）客户端官方模型通道转为本地 OpenAI/Responses/Anthropic 兼容 API。

- ✅ 认证逆向已完成（storage.json 信封解密，token 至 2026-09-13，/health 实测 200）
- ✅ 三协议转换层代码已完成
- ✅ 上游通道已确认：Cloud Agent 编排 `create_agent_task` → `workflow/start` →（tool_call 时）`interrupt`
- ❌ 卡点：`create_agent_task` 请求体为日志还原的最小化占位模板，上游 400：
  `json: cannot unmarshal string into Go struct field CreateAgentTaskRequest.user_input of type ideagent.UserInput`
  即 **user_input 需为 ideagent.UserInput 结构体对象**，当前发的是字符串。

## 探测策略决策树

工具：`trae_probe.py`（`python trae_probe.py <case号>` 跑单个 case；不带参数默认跑 21-26）。
每次探测间隔 ≥2 秒，防风控。每个 case 记录：HTTP 状态码、报错信息（400 的 Go 反序列化错误会精确指出下一个不匹配的字段）、响应头 500 字节。

### 第一轮：user_input 结构族（最高优先级，直接针对报错）
| 顺序 | case | 假设 | 预期 |
|---|---|---|---|
| 1 | 8 | `user_input={id,type,data:{content}}` + model_name/config_name 用 `__dev` 后缀 | 报错字段变化（信息增量）或通过 |
| 2 | 2 | `user_input={type,data:{content}}`（无 id） | 同上 |
| 3 | 7 | `user_input={id,content:[{type,data:{content}}]}` | 同上 |
| 4 | 5 | case 2 结构 + 删掉 query 字段（query 与 user_input 可能二选一） | 同上 |

**判读规则**：400 报错中字段名变化（如从 user_input 变为 workflow 相关字段）= 上一字段已修好，继续按报错逐字段修正；报错消失但 4xx/5xx 其他错误 = 结构对了但缺业务字段，看 message 内容补。

### 第二轮：scene_location / 模式族
- case 21（scene_location=1）、22（=0）、23（删除）、24（=1 + is_solo_mode=false）、26（session_type=chat）、25（chat_mode=0）
- 默认批（不带参数跑 `python trae_probe.py`）就是 21-26，可一次跑完

### 第三轮：workspace / 版本族
- case 27/28（真实工作区 C:\\GitHub / 正斜杠）、17-20（版本 0.1.62 + 不同 version_code）

### 第四轮：专项端点
- `python trae_probe.py 90`：llm_utils_chat（已知 user_input 须字符串、minimal 结构能过绑定卡在 function 字段缺失 → 重点尝试 function 字段枚举：`chat`/`completion`/`custom_model` 等，并观察报错是否变为 function 值不合法——那是**接近成功**的信号）
- `python trae_probe.py 91`：generate_summary 预注册后再 create（模拟客户端真实时序）
- `python trae_probe.py 92`：llm_raw_chat v1/v2 裸聊天端点（若通则完全绕开 agent 编排，是最优解）
- `python trae_probe.py 93`：get_model_list / batch_get_detail_param（拿服务端权威模型标识，顺带验证请求头/凭证完全可用）

### 兜底（全部 400 时，按成本从低到高）
1. **查 TRAE 客户端日志**：`%APPDATA%/TRAE SOLO CN/logs/`（及 globalStorage 同级目录）找 create_agent_task 真实请求报文或字段线索（客户端日志常含 request dump）
2. **跳过 create 直接 workflow/start**：用假 task_id 调 workflow/start，观察报错是否为 task 不存在（说明端点活跃，或存在免 task 的模式）
3. **llm_utils_chat 替代通道**：作为纯聊天 fallback，需猜对 function 字段
4. **mitm 抓包（人工步骤，需用户配合）**：配置系统代理抓 TRAE 客户端一次真实对话，把完整 88KB 报文交给编码工程师替换模板。若走到此步，停下来向用户说明操作方法

## 修复方案（探测成功后）

1. 用可行 case 的 body 结构改写 `trae_proxy.py: build_create_body`（保持函数签名 `sess, session_id, query_text, cfg` 不变；成功结构写成常量模板，去掉"占位模板"警告注释）
2. `build_workflow_body` 同步核对：task_id 字段名、是否也要 user_input 对象、模型名字段
3. 核对 `_extract_task_id`：确认能从新响应结构（JSON 或 SSE）提取 task_id；若 create 响应是 SSE 流，补齐逐事件读取逻辑
4. 按真实响应修正 `agent_turn_chunks` 的 SSE 事件映射（thought/reasoning/tool_call/turn_completion/token_usage 事件名以实测为准）
5. 回归：跑一次端到端（见验收标准）

## 验收标准

- `python trae_proxy.py` 启动无报错，`GET /health` 200
- `POST /v1/chat/completions`（stream=false，model=默认，messages 只含 "hello"）→ 200 且 choices[0].message.content 非空
- `POST /v1/chat/completions`（stream=true）→ SSE 正常流出 chunk 并以 finish_reason 结束
- `POST /v1/messages`、`POST /v1/responses` 各实测一次 200
- 日志 `trae_proxy.log` 无未处理异常

## 硬约束（红线）

- 所有探测/测试内容只用 `"hello"` 等无害占位内容（AGENTS.md 红线），禁止任何真实 PII/敏感词
- 禁止 git commit / push
- 不碰 `proxy.py` / `test_proxy.py` / 审计文件（那是另一条 Workspace 代理线）
- 探测请求间隔 ≥2 秒，防上游风控
- 保持纯 Python 标准库 + cryptography 依赖风格，不引入新第三方包