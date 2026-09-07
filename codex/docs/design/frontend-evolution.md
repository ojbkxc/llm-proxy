# AIGX 前端演进路线

> 参照 open-webui 的交互范式，优先把 AIGX 从“调试 Playground”演进为“可日常使用的 AI 工作台”。

| 范式 | AIGX 当前状态 | 引入决策 | 后端依赖 |
|---|---|---|---|
| ChatGPT 风格主界面 | 暂无面向普通用户的聊天中心，Playground 偏调试 | P0 采用，建设 ChatCenter、消息流、模型选择、历史会话 | 复用 `/v1/chat/completions`；会话持久化需新增 API |
| 输入工程 | Playground 有基础输入 | P0/P1 增加 slash command、`@model`、附件入口；语音输入延后 | 模型列表 API 已有；附件/语音需上传与转写 API |
| 消息渲染 | 基础渲染 | P1 完善 Markdown、代码块复制、思考过程折叠；Artifacts 与引用延后 | 基础能力无依赖；引用依赖 RAG/来源协议 |
| 对话治理 | 暂无会话列表、搜索、分享 | P0 做持久化、搜索、置顶；P2 做文件夹、标签、分享链接、Overview Flow | 会话、搜索、分享权限 API |
| 设置 IA | 单页设置约 672 行 | P0 拆分 General / Interface / Account / Security / Notifications | 无硬依赖 |
| Playground 三模式 | 主要是 Chat，参数调整有限 | P1 增加 Completions；Image 模式待图像 API 后进入 | Completions 无新增依赖；Image 依赖图像 API |
| Workspace 知识系统 | 暂无 | P1 先做模型预设；P2 再做 Models / Knowledge / Prompts / Tools / Skills | 预设 API；Knowledge 依赖 RAG、向量库与权限 |
| 主题系统 | Glass morphism，缺少切换 | P0 增加 system/light/dark 和主题色变量；玻璃效果改为局部强调 | 无硬依赖 |
| 组件质感 | 约 18 个组件 | P0/P1 统一 Drawer、Dropdown、Dialog、Tooltip 等交互状态 | 无硬依赖 |

## P0 页面与交互基线

- 首屏在桌面和移动端均能进入新建会话。
- 发送、停止、重新生成、复制、编辑、继续生成均有明确状态。
- 流式响应中显示连接状态、耗时和错误恢复入口。
- 键盘导航、焦点环、ARIA 标签、触摸目标不低于 44px。
- 明暗主题切换不改变业务状态；颜色通过 CSS variables 管理。

## 组件迁移策略

1. 先定义设计 token：背景、表面、边框、文字、强调色、危险色、间距、圆角、阴影。
2. 再统一基础组件的 loading/empty/error/disabled/focus 状态。
3. 最后迁移页面，禁止页面继续直接写散落的颜色和阴影值。
4. 保留现有管理端功能路径，使用渐进式路由和 feature flag 发布 ChatCenter。
