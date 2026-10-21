# Qoder Worker Runtime 逆向报告：签到 claim 请求端完整规格

> 目标文件：`D:\Programs\Qoder\resources\app.asar.unpacked\node_modules\@qoder-ai\qoder-agent-sdk\dist\_worker\qoder-worker-runtime.obf.mjs`（33MB，混淆 JS）
> 版本：qoder-worker-runtime 1.1.69（site=global，build=prod，git 806e24f）
> 分析方式：字节偏移 grep + python seek 提取 + `_$d` 解密器复现（base64 + XOR）+ 本地实验验证
> 本报告所有结论均标注了文件内字节偏移，可复查。

---

## 0. 执行摘要（一句话）

**claim 请求没有硬编码的 URL/body——它由服务端在 campaigns 响应里下发的 JS 脚本（`placements[].commands[].js`）在客户端 vm 沙箱中动态执行**；客户端只提供 `apiBase`（`https://openapi.qoder.sh`）+ 一套 Gcr 构造的 `Cosy-*` 头（同 origin 自动注入）+ 裸 accessToken。**machineToken 可在代理侧 100% 复现**（直接调用 `runtime-info.exe 4`，输出确定性 JSON，已实验验证）。

---

## 1. claim POST 发起端（核心结论）

### 1.1 请求规格总表

| 项 | 值 | 来源 |
|---|---|---|
| URL | `${apiBase}<脚本内路径>` = `https://openapi.qoder.sh` + 服务端 js 脚本里写的 path | 沙箱全局 `CLI_CAMPAIGN.apiBase`（J9s，偏移 32782890） |
| Method | 由服务端 js 脚本决定（推测 POST） | 沙箱 `fetch(url, init)` |
| Body | 由服务端 js 脚本决定 | 同上 |
| Authorization | `Bearer <裸 accessToken>`（`security_oauth_token \|\| access_token`，**非 COSY 签名**） | e1s action（偏移 32806247 区域） |
| Accept | `application/json` | Gcr（偏移 32783641） |
| Cosy-ClientType | `5`（env `QODER_CLIENT_TYPE`，work 模式 `6`） | yu()，偏移 12215+24742 |
| Cosy-MachineCode | 18 位 hex，runtime-info.exe 输出 | Gcr / umid |
| Cosy-MachineId | UUID，`~/.qoder/.auth/machine_id` 文件内容 | Pl.getMachineId() |
| Cosy-MachineOS | `x86_64_win32`（arch 映射 aarch64/x86_64） | Bse，偏移 3052680 |
| Cosy-MachineToken | 88 字符，runtime-info.exe 输出 | Gcr / umid |
| Cosy-MachineType | 19 位 hex，runtime-info.exe 输出 | Gcr / umid |
| Cosy-Version | `1.1.69`（编译期常量 NH fallback） | Qse，偏移 3052680 |
| Cosy-MachineHostname | 归一化 `os.hostname()`（可选头） | Cse()，偏移 3045476 |
| User-Agent | **`node`**（undici fetch 默认，无自定义） | 实验验证，见 §4 |
| Content-Type | ⚠️ 若 js 未显式设置，undici 对字符串 body 默认 `text/plain;charset=UTF-8` | 实验验证 |
| 其他 undici 默认头 | `accept-language: *`、`sec-fetch-mode: cors`、`accept-encoding: gzip, deflate`、`connection: keep-alive` | 实验验证 |

### 1.2 完整调用链（含偏移）

```
[1] 登录后 Bbt 插件启动（偏移 32809704）
    └─ mbt.start() → 每 5 分钟(3e5ms) pollOnce（偏移 32782438 起）

[2] j9s: GET {endpoint}/sash/api/v1/me/campaigns（偏移 32783000）
    ├─ Gcr(accessToken, uid, {consumer:"campaign_list"}) 构造全套 Cosy 头
    └─ po({operation:"campaign-platform-list", endpointType:"openapi",
           method:"GET", injectClientIdentityHeaders:false, ...})

[3] H9s 解析响应（偏移 32780273）：
    campaigns[].placements[].type==="CMD" →
    placements[].commands[] = {name, description, js(≤64KB), campaignId, campaignKey, endAt}
    （"js" 字段 = 服务端下发的可执行脚本！）

[4] 用户输入 /<命令名>（如 /checkin）或补全菜单选中（autoExecute=true 表示选中即提交）
    └─ e1s 的 action（偏移 32806247 区域）：
       ├─ token = userInfo.security_oauth_token || userInfo.access_token   ← 裸 token
       ├─ A = await J9s(token, uid, signal)（偏移 32782890）
       │     ├─ Gcr(token, uid, {consumer:"campaign_script", waitMs:1e4})
       │     └─ return {apiBase:"https://openapi.qoder.sh", origin, headers}
       └─ runtime.execute({js, auth:{accessToken}, campaign:{apiBase,campaignId,campaignKey},
                          qoderRequest:A, ...})

[5] hbt.execute（偏移 32803300）：new Worker(shd, {eval:true}) 真线程 + vm 沙箱
    ├─ 沙箱脚本 shd（偏移 32795373，约 8KB 内联源码，已提取到 _rev/sandbox.js）
    ├─ bridgeBootstrap ohd（偏移 32789498，已提取到 _rev/bootstrap.js）
    └─ 沙箱全局：fetch / console / setTimeout / CLI_AUTH / CLI_CAMPAIGN

[6] 沙箱 executeFetch（sandbox.js 中段）：
    └─ injectQoderHeaders(url, headers)：
       仅当 new URL(url).origin === "https://openapi.qoder.sh" 时，
       把 qoderRequest.headers（Gcr 全套头）逐个注入（脚本已设置的头不覆盖）
    └─ 宿主全局 fetch（undici）实际发送；redirect:manual 手工跟随（≤20 跳）；
       跨 origin 重定向时剥离 authorization/cookie
```

### 1.3 对代理的直接含义

- **claim 的确切 path/body 必须抓 campaigns GET 响应**才能确定（`commands[].js` 字段）。客户端仓库内不存在任何 claim URL 常量（已穷尽搜索，见 §7）。
- 沙箱限制（XEd=2 并发、K9s=30s 总超时、Ahd=2MB 响应上限）意味着脚本就是简单的"一个 fetch 打到底"。
- 回执解析器 Qda（偏移 872700）解析的 `claimed/claimedWithBalance/alreadyClaimed/noCampaign/ineligible/serviceError` 是 **dynamic_commands feature gate 里 claimDisplay 的展示文本结构**（偏移 873500），不是 claim API 的响应 schema——真正响应结构要看 js 脚本的输出文本。

---

## 2. ensureMachineInfo（ib()）与 machineToken 生成链路

### 2.1 体系结构（CCe 类，偏移 18733302）

| 组件 | 说明 |
|---|---|
| 签发者 | `runtime-info.exe`（壳）加载 `sgsdk.dll`（阿里系安全 SDK，7MB 黑盒） |
| 源文件 | `D:\Programs\Qoder\resources\umid\{runtime-info.exe, sgsdk.dll}`（manifest.json 校验 sha256） |
| 部署位置 | 复制到 `~/.qoder/.bin/umid-win32-x64-<16位hash>/`（kai()，偏移 3838545；sha256 双文件校验 + lockfile 原子发布） |
| 调用方式 | `spawn(exe, [env, "--account-stdin"], {stdio:[pipe,pipe,pipe], windowsHide:true})`，stdin 传 `JSON.stringify({account})+"\n"`（Cai/startMachineInfoBridge，偏移 18743200） |
| env 参数 | `QjA(Ui)`：国际版=**4**（SINGAPORE），CN 版=0（ONLINE）；Urn={ONLINE:0,PRE:1,DAILY:2,US_ONLINE:3,SINGAPORE:4,KOREA:5}（偏移 18731880） |
| stdout | JSON：`{machineToken(88字符), machineType(19hex), machineCode(18hex), vmInfo{isVm,brand,percentage,vmTypeCode}, accountOutcome?}` |
| 缓存 | `~/.qoder/umid-cache.json`：`{version:1, environment:4, platform, arch, info:{...}}`；每 **1 小时 ±5 分钟**抖动刷新（tdc=36e5, Nai=3e5，scheduleNextRefresh 偏移 18739229 区域） |
| 禁用 | 环境变量 `QODER_DISABLE_UMID_REPORT`（a5，Dr 前缀=QODER_） |
| 失败 | Gcr 抛 `MachineIdentityUnavailableError`（L7e，偏移 32782372），**campaign 链路无降级**（另一条 Sdc 路径偏移 18756295 会用 machineId 顶替 machineToken，但与签到无关） |

### 2.2 代理侧复现：**可以，且已实验验证** ✅

在本机直接调用（stdin 空，无 account）：

```
$ echo "" | runtime-info.exe 4
{"machineToken":"P1gAmbkTgKCsy1DsXZ-6ZBr9ZvaSZi3ha95HlYy50h-cFaqHeMutORiwoUr68pJba5ewbwWqtkOLNBPvKBGlIRtP",
 "machineType":"68b9c6ac910ec8aaff","machineCode":"67b526290077689a48",
 "vmInfo":{"isVm":false,"brand":"None","percentage":0,"vmTypeCode":91}}
```

实验矩阵（关键证据）：

| 实验 | 结果 | 结论 |
|---|---|---|
| 重复调用 `4` | 输出逐字节一致 | **token 确定性**，可缓存复用 |
| `4` + `--account-stdin {"account":"test-account-123"}` | token 不变，多出 `"accountOutcome":"success"` | account 只触发服务端账号绑定上报，**不影响 token** |
| 调用 `0`（CN env） | token/machineCode/machineType 全变 | **token 按 env 命名空间**，国际版必须传 `4` |
| 与 `~/.qoder/umid-cache.json` 对比 | 完全一致 | 客户端缓存即此输出 |

**代理实现方案**：直接 spawn 客户端部署好的 `~/.qoder/.bin/umid-win32-x64-*/runtime-info.exe 4`（或从 `D:\Programs\Qoder\resources\umid\` 复制一份），解析 stdout JSON，缓存 1 小时。无需运行 Qoder 客户端。

---

## 3. Cse()（Cosy-MachineHostname 取值）

定义于偏移 3045476，模块导出名为 `readMachineHostnameHeaderValue`：

```js
import { hostname as kic } from "node:os";
cnn = kic();            // 原始 hostname
a6A = Ori(cnn);         // 归一化
function Cse(){ ...warn once if normalized!==original...; return a6A || void 0 }
```

`Ori()`（偏移 3044460）归一化规则（Python 可完整复现）：

1. `trim()`；空 → 返回 `""`（此时不发该头）
2. 若 header-safe（`/^[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?$/`，即可打印 ASCII）→ 直接用（经 snn 截断）
3. 否则尝试 `domainToASCII()`（punycode）；仍不 safe 则把非 ASCII 序列替换为 `-`、压缩连续 `-`、去首尾 `-`，拼 `-<sha256前8位>`
4. `snn()`：长度 > 96 → 截到 87 并去尾部空白 + `-` + sha256 前 8 位

普通 Windows 主机名（纯 ASCII）就是 `os.hostname()` 原样，≤96 字节。**取值 = `socket.gethostname()` / `platform.node()`**。

---

## 4. 客户端 User-Agent

| 请求路径 | UA | 依据 |
|---|---|---|
| campaigns GET（j9s→po） | **无自定义 UA**（undici 默认 `node`） | po/Zri（偏移 3047037）不设 UA；全局 Agent（偏移 19034176，gsn）无 userAgent 选项 |
| **claim POST（campaign script fetch）** | **`node`** | 沙箱 executeFetch 用宿主全局 fetch，无 UA 设置 |
| 其他 openapi JSON 请求（openApiJsonRequest 等 4 处） | `qoder/<version>`（如 `qoder/1.1.69`） | 偏移 18951934/18963922/18966191/19522622：`"User-Agent": \`qoder/${await Fr()}\`` |

**本机实验**（Node 24.15.0 全局 fetch → 本地 http server 抓包）：

```
{"user-agent":"node","accept-language":"*","sec-fetch-mode":"cors",
 "accept-encoding":"gzip, deflate","connection":"keep-alive",
 "content-type":"text/plain;charset=UTF-8"}   ← 字符串 body 未设 CT 时的默认值
```

**结论**：代理当前发 `qoder-intl-proxy/1.0` 是显著指纹。campaigns/claim 链路应伪装为 `node`（或不发 UA + 保留 undici 默认头组合）。若被风控对照，还需补 `accept-language: *`、`sec-fetch-mode: cors` 这类 undici 特征头。

---

## 5. 签到触发时机

worker runtime 内**没有定时自动 claim**。完整时序：

| 时机 | 行为 | 依据 |
|---|---|---|
| 插件初始化 | `Bbt.initialize`：仅 `mode==="interactive"` 且 `QODER_CLI_OPERATIONAL_ACTIVITY_SOURCE!=="campaign-platform"` 之外值时激活（默认激活） | 偏移 32809704 |
| 登录/启动 | `mbt.start()`：订阅 authManager 登录态变化（变化即 abort+refresh）+ 立即 pollOnce | 偏移 32782438 |
| 周期 | **每 5 分钟**（pollIntervalMs=3e5）轮询 campaigns 列表；UMID 失败可重试时 nextPollDelay=max(30s, retryAfterMs)，identityRetryCount<3 | mbt.drainRefreshes |
| token 刷新 | umid machineToken 每 1h±5min 重取；campaign 响应命令到期 prune（expirationTimer） | CCe.scheduleNextRefresh |
| **claim 执行** | 仅用户触发：CLI 输入 `/checkin`（或补全菜单选中，`autoExecute` 只是"补全选中即提交"的 UI 行为，uDA 偏移 28851097） | e1s.action |
| 失败兜底 | 内置 `/claim` 回退命令（t1s，偏移 32806247 后）：仅列出可用活动文案，不发请求 | 偏移 32806700 |

> IDE 的签到按钮/自动触发（若有）在 Electron 层（app.asar / renderer），不在本文件（见 §7）。

---

## 6. Pl.getMachineId()（Cosy-MachineId，与 umid 是两套独立体系）

| 项 | 值 |
|---|---|
| 存储文件 | `~/.qoder/.auth/machine_id`（本机实测：`02ab95d6-3242-426a-a1a2-403f09090448`，UUID 格式） |
| 类 | `Pl`（auth-storage，偏移 3822846）；原子写入：临时文件+硬链接（rai，偏移 3817771） |
| 生成（tai/wuc，偏移 3816443） | darwin：硬件 serial；win/linux：`sha256("qoder-machine-id:v1" + ":" + platform + ":" + systemUuid小写)` 再 UUID 化（盐 `fuc="qoder-machine-id:v1"`，偏移 3821741）；全部失败 → 随机 UUID |
| systemUuid 来源 | systeminformation 库（= Windows 机器 UUID，注册表/BIOS 层） |
| 校验 | 内存单例 Hz 变化检测（MACHINE_ID_CHANGED），`.auth` 下存在 user 凭据文件却无 machine_id 时抛 MACHINE_ID_MISSING |
| 与 umid 关系 | **完全独立**：machineId 是本地稳定 UUID（auth-storage 体系）；machineToken/Code/Type 是 sgsdk 签发（umid 体系）。仅 Sdc 降级路径会把 machineId 当 machineToken 用 |
| 代理复现 | 可行：读 `~/.qoder/.auth/machine_id`，或按上述 sha256 派生规则自算 |

另一参考（OTEL 资源检测器版本，偏移 1728083）：win32 用 `REG.exe QUERY HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Cryptography /v MachineGuid`——这是 SDK 内另一处 machineId 实现（遥测用），与 Pl 的派生算法不同，注意区分。

---

## 7. 搜过但未找到（负结果清单）

| 目标 | 结果 |
|---|---|
| claim 的硬编码 URL/路径常量 | **不存在**。全文件 `.operation("...")` 名录（42 个）无 `campaigns.claim`；`claimCampaign`/`campaigns.claim`/`benefit`/`grant`/`canClaim` 零命中（`CanClaim` 仅 3 处，全在 dynamic_commands 解析器内） |
| `_$d` 加密字符串里的 claim URL | 15762 个加密字符串全部解密（`_rev/decrypted_strings.txt`），无 claim 路径。加密串主要是错误消息/第三方库文本 |
| dynamic_commands `interaction` 模板（domain/path/method/body/headers/auth）的执行器 | 解析器存在（xxr/yda，偏移 873500），但 worker runtime 内无消费方——疑似 IDE Electron 层功能，或预留未启用 |
| campaigns 响应 `commands[].js` 的内容 | 服务端下发，客户端文件内无样例。**需实际抓一次 GET /sash/api/v1/me/campaigns 响应** |
| IDE（Electron 主进程/renderer）签到 UI 与自动触发 | 不在本文件（在 app.asar 内，未分析） |
| campaign script 沙箱内的网络 allowlist | 无 allowlist：任何 URL 都可 fetch，但只有同 origin（openapi.qoder.sh）注入 Cosy 头，跨 origin 自动剥 authorization/cookie |
| 自定义 User-Agent 常量（qoder-cli UA 等） | campaign 链路无；仅 openApiJsonRequest 4 处用 `qoder/${version}` |

---

## 8. 代理实现建议（供 qoder_proxy.py 参照）

1. **头构造完全照抄 Gcr**（§1.1 表格），Authorization 用裸 `access_token`/`security_oauth_token`。
2. **machineToken/machineCode/machineType**：spawn `runtime-info.exe 4`（从 `~/.qoder/.bin/umid-win32-x64-*/` 或 `D:\Programs\Qoder\resources\umid\` 取），解析 stdout，缓存 1h。零参数即可，无需 account。
3. **Cosy-MachineId**：读 `~/.qoder/.auth/machine_id`。
4. **UA 改为 `node`**，并补 `accept-language: *`、`sec-fetch-mode: cors`（若走标准 requests 库无法逐字伪装 undici，至少 UA + Accept 要对）。
5. **claim URL**：先抓 campaigns GET 响应拿 `js` 脚本（用上述头构造直接 GET `https://openapi.qoder.sh/sash/api/v1/me/campaigns`），脚本里就有确切 path/method/body——这是唯一缺口。
6. 若 claim 脚本 POST 时未设 Content-Type，真实请求是 `text/plain;charset=UTF-8`（undici 字符串 body 默认），别想当然发 `application/json`。

---

## 附录 A：关键偏移索引（文件内字节位置）

| 偏移 | 内容 |
|---|---|
| 0–290 | `_$d` 解密器：base64 + XOR key `"8UP80Dst3B8b"` |
| 872700–876400 | dynamic_commands 解析（Qda 回执字段、xxr interaction 模板、requiresCanClaim） |
| 18731880 | QjA/ib/CCe 类（umid 服务） |
| 18733302 | ensureMachineInfo |
| 18739229–18747700 | fetchMachineInfo/startMachineInfoBridge（spawn exe 细节） |
| 18756295 | Sdc（umid 降级路径，非签到链路） |
| 29309 | wr() 配置目录（~/.qoder） |
| 3044460 / 3045476 | Ori() / Cse()（hostname 归一化） |
| 3047037 | po()（qoderServerRequest，Cosy 头注入） |
| 3054817 | JLe()/wse（endpoint 表：openapi.qoder.sh） |
| 32780273 | H9s（campaigns 响应解析，js 字段提取） |
| 32782438 | mbt（campaigns 轮询控制器，5 分钟） |
| 32782890 | J9s（campaign_script 的 {apiBase, origin, headers}） |
| 32783641 | Gcr（Cosy 头构造器） |
| 32789498 | bridgeBootstrap（沙箱 API：fetch/CLI_AUTH/CLI_CAMPAIGN） |
| 32795373 | 沙箱 worker 脚本 shd（executeFetch/injectQoderHeaders） |
| 32806247 | e1s（campaign 动态命令 action，J9s 调用 + 沙箱 execute） |
| 32809704 | Bbt 插件（pollIntervalMs=3e5，getSlashCommands） |
| 33181644 | Yvt（API 客户端 campaigns.list，withMachineIdentity） |
| 3816443 / 3817771 | tai/rai（machineId 生成/原子写） |
| 3821741 | fuc 盐 `"qoder-machine-id:v1"` |
| 3822846 / 3829675 | Pl 类 / getMachineIdInTransaction |

## 附录 B：环境/域名常量速查

| 常量 | 值（国际版 global） |
|---|---|
| apiBase（openapi） | `https://openapi.qoder.sh` |
| inference | `https://api2.qoder.sh` |
| center | `https://center.qoder.sh` |
| 环境变量前缀 Dr() | `QODER_`（CN 版 `QODERCN_`） |
| QODER_ENV / QODERCLI_SITE | 环境选择（prod/daily/test；cn/global） |
| QODER_CLIENT_TYPE | `5`（默认）/`6`（work 集成模式） |
| QODER_DISABLE_UMID_REPORT | 禁用 umid |
| QODER_CLI_OPERATIONAL_ACTIVITY_SOURCE | campaign 轮询开关（默认 campaign-platform） |
| umid env 参数 | 国际版传 `4`（SINGAPORE），CN 传 `0` |

## 附录 C：产出物

- `_rev/decrypted_strings.txt`：全量 15762 条解密字符串（offset\t明文）
- `_rev/sandbox.js`：沙箱 worker 脚本全文（约 8KB）
- `_rev/bootstrap.py` 同目录 `bootstrap.js`：bridgeBootstrap 全文
- `_rev/extract.py` / `multi.py` / `decrypt_all.py`：复现工具

---
*分析日期：2026-10-20。仅只读分析，未修改任何客户端文件；runtime-info.exe 实验在本机副本上以只读方式调用。*