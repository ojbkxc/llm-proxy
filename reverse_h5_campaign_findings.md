# Qoder 签到 H5 链路深挖报告（activity-iframe.js 逆向 + reward 端点实测）

> 2026-10-20 深夜 · 团队任务 15 产出 · 结论：H5 层无当日翻盘机制；BLOCKED grant 终态不可逆（客户端代码层面最终确认）；reward 端点给出完整 grant 详情与当天时间线，判定因子最可能是 machineId+IP 组合锚定

## 一、H5 页面真实架构（与此前推测的关键差异）

签到页面 = `https://openapi.qoder.sh/growth-page/activity-iframe`（campaigns 响应的 `campaignUrl` 字段下发，所有账号同一 URL）。页面是固定 SPA：

- HTML 壳：`<title>Qoder Campaign</title>`，配置 `window.QoderConfig = { env: 'qoder-global' }`
- 主 JS：`https://g.alicdn.com/qbase/qoder/0.0.730/growth-page/activity-iframe/activity-iframe.js`（274KB，prettier 格式化后 17322 行）
- 带 GTM 埋点（GTM-PQZDSC7G / G-EELJT0HWX0 / AW-17523085727）

**推翻此前推测**：claim 请求并非"服务端 campaigns 响应里 `placements[].commands[].js` 下发的脚本在 vm 沙箱执行"——那是 CLI worker runtime（ClientType=5）的机制。桌面端 H5 是**固定 SPA**，运行时通过 **postMessage 桥**让主进程代发请求（protocol=`qoder.campaign`, version=1，requestId + 10s 超时）。

## 二、H5 只有三个 API 操作（无解锁/撤销/重试端点）

`$t(clientType, path, init)` 的 Electron 分支（L16238-16279），全部桥接给主进程 `campaign:apiRequest`：

| 操作 | 端点 | 方法 | body |
|---|---|---|---|
| listCampaigns | `/sash/api/v1/me/campaigns` | GET | - |
| **getCampaignReward**（新发现） | `/sash/api/v1/me/campaigns/{id}/reward` | GET | - |
| claimCampaign | `/sash/api/v1/me/campaigns/{id}/claim` | POST | 无 body（keepalive） |

**结论：客户端代码层面无任何"解锁/撤销 grant/重试领取"端点。** claim 幂等，BLOCKED grant 重放永远 BLOCKED。当日翻盘机制不存在。

## 三、reward GET 实测（当晚从未打过的新端点，2026-10-20 深夜）

对当日活动 `act-20261020-910`（campaignId=01a15721-d469-7a24-abd5-a81b73dc68f9）：

| 账号 | status | failureCode | claimedAt(UTC) | grantedAt | addOnTotal |
|---|---|---|---|---|---|
| 165github | CLAIMED | - | 03:08:11 | 03:08:11 | 100 |
| Grace Fields | CLAIMED | - | 06:47:46 | 06:47:46 | 100 |
| ojbkxc1 | **BLOCKED** | SAME_PERSON_ALREADY_CLAIMED | **07:07:02.590** | 无 | None |
| qqwd email2 | **BLOCKED** | SAME_PERSON_ALREADY_CLAIMED | **07:07:02.912** | 无 | None |
| ojbk xc3 | **BLOCKED** | SAME_PERSON_ALREADY_CLAIMED | **07:07:03.256** | 无 | None |

benefit 规格：CREDITS 100，ALL_MODELS，RELATIVE_DAYS 30 天。

## 四、当天时间线重建与判定因子分析

三号 BLOCKED grant 在 07:07:02-03（北京时间 15:07）**同一秒级批量创建**——白天 cron/批量脚本用共享身份跑的。此时线上 4 号仍共用共享 machineId 02ab95d6（17:12 才 PATCH 独立身份）。

**判定因子假说检验**（结合当天全量事实）：
- 纯 machineId 说 ❌：Grace Fields（02ab95d6 + 本机 IP）在 165github（02ab95d6 + CF IP）之后 3.6 小时仍成功
- 纯 IP 说 ❌：国内版 qoder_proxy.py 单服务器 IP 多号连日全成功
- **machineId+IP 组合锚定说 ✓（最兼容）**：165github（02ab95d6, CF IP）占用组合 → 三号（02ab95d6, CF IP）同组合被拒；Grace（02ab95d6, 本机 IP）组合不同放行；国内版（每号独立 machineId，同 IP）组合各不相同 → 全成功
- 账号级真人集群说（uid 同批注册特征）：仍不能排除，10-21 结果区分——4/4 成功=组合锚定说；仅 1 个成功=纯 IP 或真人集群说

**10-21 预测（组合锚定说）**：线上 4 号现有独立 machineId + 同 CF 出口 IP，组合互不相同 → 4/4 应全部 secured。

## 五、H5 状态机（wt/yt/kt 映射，L15872-15956）

**reward 响应 → UI 状态**：CLAIMED+code→claimed；CLAIMED 无 code→confirming（轮询）；REVOKED→revoked；PENDING→confirming；否则按 failureCode：RISK_BLOCKED→unavailable、REDEMPTION_CODE_OUT_OF_STOCK→outOfStock、ACHIEVEMENT_NOT_COMPLETED→locked、CAMPAIGN_NOT_ACTIVE→inactive、RISK_DEPENDENCY_UNAVAILABLE→serviceUnavailable、default→error。

**claim BLOCKED 响应（我方三号实测形态）**：`status:BLOCKED` 不在 wt 枚举 → failureCode=SAME_PERSON_ALREADY_CLAIMED 无映射 → **default "error"**——客户端 UI 显示通用错误，无申诉入口。

**错误码**：GRANT_RESULT_UNKNOWN/504/0→confirming（**异步发券语义，H5 会轮询 refresh 重查**）；CAMPAIGN_NOT_FOUND→notFound；401→unauthenticated；403→forbidden。

## 六、深链入口（L16793-16817）

iframe URL 带 `?campaign_id=<uuid>&view=reward` 时 H5 **绕过列表过滤**直接构造 campaign（St()：默认 actionType=claimBenefit/benefitKind=REDEMPTION_CODE）并 query reward；GRANT_NOT_FOUND → refresh 列表重判（locked/outOfStock/inactive 会重映射）。**深链也不产生新 claim 通道**——只是查 reward。

## 七、VIEW_DETAILS 实测（claim 通道健全性证明）

对 `act-20260901-493`（VIEW_DETAILS, 长期活动）POST claim：
- ojbkxc1 / qqwd email2：**CLAIMED, replayed:false，全新 grantId（01a15978-*）**——claim 端点对三号完全正常！
- VIEW_DETAILS claim 不发钱（kind=null），addOnTotal 不变

**定性：账号 claim 能力未被封，锁死的是"当日活动"那一条 grant。**

## 八、对代理实现的落地启示

1. reward GET 应纳入代理回执链：claim 后打 reward GET 可拿到 failureCode/grantedAt/expiresAt 的权威终态（比 claim 响应更全），`confirming`（PENDING/UNKNOWN）场景应轮询
2. `campaigns` 响应还有 `campaignUrl`/`showCampaign`/`claimable` 顶层字段可利用（showCampaign=true 但列表无当日条目 = grant 已存在被过滤的信号）
3. H5 claim POST 无 body、无 nonce、无签名——代理现有形态已完全对齐，无遗漏参数

## 附：本晚实验与产物

- `dump_placements.py`：拉完整 campaigns 顶层结构（发现 campaignUrl/showCampaign）
- `activity_iframe.html/.js/.pretty.js`：H5 页面与主 JS（格式化 17322 行）
- `get_reward.py`：reward GET 决定性实验（本报告 §三 数据源）
- `claim_viewdetails.py`：VIEW_DETAILS claim 通道健全性实验（§七 数据源）