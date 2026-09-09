# AIGX UI 审美迁移方案

## 视觉原则

从全局玻璃拟态转向“克制的工作台”：中性背景、清晰表面层级、低强度边框、有限的强调色。玻璃效果只用于顶部导航、浮层和关键状态，不用于所有容器。所有页面共享 CSS token，优先保证信息层次、可读性和响应式。

## 34 页面迁移优先级

### P0：核心用户体验（1–10）

1. ChatCenter（新建）：聊天主界面、空状态、流式消息、输入区。
2. Login：登录、OAuth、错误和加载状态。
3. Register：注册、密码规则、验证反馈。
4. Settings：拆分后的设置壳与导航。
5. Settings/General：语言、默认模型、区域设置。
6. Settings/Interface：主题、密度、消息显示。
7. Settings/Account：资料、API 账户关联。
8. Settings/Security：2FA、会话管理、撤销。
9. Playground：Chat 与 Completions 模式入口。
10. Sidebar：会话列表、搜索、置顶、折叠和移动端抽屉。

### P0：管理闭环（11–18）

11. Dashboard：成本、用量、延迟、错误率总览。
12. Channels：渠道列表、状态、连通性测试。
13. ChannelDetail：凭据、能力、模型、健康历史。
14. Models：模型同步、缺失模型、价格入口。
15. Pricing：模型价格、比例、缓存价格。
16. Users：用户列表、状态、额度和权限。
17. UserDetail：用户资料、用量、会话和安全操作。
18. Logs：请求检索、错误分类、request id 详情。

### P1：效率与体验（19–27）

19. Keys：创建、轮换、撤销与最近使用。
20. Presets：系统提示、参数和模型预设。
21. Notify：通知中心和失败提示。
22. Usage：按租户/模型/渠道的用量分析。
23. Costs：成本模拟与缓存节省。
24. Health：渠道健康、熔断和恢复时间线。
25. Playground/Completions：参数面板、原始请求与响应。
26. Playground/Images：图像生成参数与结果历史。
27. OAuth/OIDC：提供商选择、回调失败和绑定状态。

### P2：扩展工作区（28–34）

28. Knowledge：知识库列表、导入和索引状态。
29. KnowledgeDetail：文档、权限、检索测试、来源。
30. Prompts：提示词模板和变量。
31. Tools：工具列表、权限和调用日志。
32. Skills：技能组合与启停。
33. ConversationFolders：文件夹、标签和批量整理。
34. Share/Overview：分享链接、权限、会话概览流。

## 每页迁移清单

- 使用 token 而非硬编码颜色、阴影、间距和圆角。
- 覆盖 loading、empty、error、success、disabled、focus 六类状态。
- 桌面、平板、移动三个断点检查；核心操作触摸目标至少 44px。
- 检查键盘顺序、焦点可见性、ARIA 名称和文字对比度。
- 页面迁移后删除重复样式，并保留旧路由的兼容跳转。

## 完成定义

页面通过视觉回归、可访问性检查和核心流程手工验收；P0 页面不出现横向滚动、未处理错误或无法恢复的加载状态。
