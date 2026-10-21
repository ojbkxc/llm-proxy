# Qoder 国际版桌面客户端 签到（claim）请求规格逆向报告

- 逆向对象：`D:\Programs\Qoder`（Qoder App 0.4.8 stable，build f8c98a8，Electron 43.1.1 / Chromium 143，win32-x64）
- 分析范围：`resources\app.asar`（17MB 主进程 `out/main/electronMain.js` + 15.5MB 渲染 bundle `out/renderer/assets/DIR8HOYO.js` + preload）、`resources\extensions\`、`resources\bundled-resources\`、`resources\umid\`（runtime-info.exe + sgsdk.dll）
- 分析方法：自写 asar 解析器逐文件字节搜 → 官方 `@electron/asar` 提取 → acorn AST + 字符串数组解码器（obfuscator.io 形态，还原 32 万+ 处字符串调用）→ 语义锚点定位
- 结论可信度：主进程代码为还原后的原始逻辑摘录（非推测）；仅 UA 字符串与 claim body 为推断（依据已注明）

---

## 0. 一句话结论（TL;DR）

桌面签到 claim 请求**不是桌面 JS 代码直接发的**：主进程先 `GET /sash/api/v1/me/campaigns` 拿到活动地址，然后用 iframe 加载 qoder.sh 域上的 **campaign H5 页面**，H5 页面里的 JS 发 `POST /sash/api/v1/me/campaigns/{campaignId}/claim`，Electron 主进程用 `webRequest.onBeforeSendHeaders` 拦截器**统一注入** `Authorization` + 全套 `Cosy-*` 头（含 `Cosy-MachineHostname`）。机器指纹三件套（machineToken/Code/Type）由 `umid/runtime-info.exe`（内部调 sgsdk.dll，阿里 SecurityGuard/UMID）生成。

**我们代理与客户端真实形态的 4 个关键差异**：

| 项 | 代理当前值 | 桌面端真实值 | 说明 |
|---|---|---|---|
| Cosy-MachineHostname | ❌ 缺失 | `os.hostname()` 原值 | 桌面必发（几乎恒有值） |
| Cosy-Version | 1.1.69 | **0.4.8**（app.getVersion()） | CLI 用 CLI 版本号，桌面用 App 版本号，语义不同 |
| User-Agent | qoder-intl-proxy/1.0 | claim：Electron 默认 UA；native GET：`Qoder` | 见 §1/§2 |
| Cosy-MachineId | 自造/账号缓存 | 客户端 `auth.machine-id` 文件的 UUID | 建议直接读客户端文件复用 |

---

## 1. 桌面签到 claim 请求完整规格（H5 页面发出 + 主进程拦截器注入）

### 1.1 URL / method

```
POST https://openapi.qoder.sh/sash/api/v1/me/campaigns/{campaignId}/claim
```

- `campaignId`：由 campaign H5 页面自己从活动上下文获得（桌面代码中无 claim 端点字符串，见 §7）。主进程硬编码的常量 campaignId 是 `client_launch_26`（`EYe`，仅用于 limited-number 查询，见 §2.3）。
- 桌面代码不含 claim 的 query/body 证据；无任何额外签名头（拦截器只注入 Authorization + Cosy-*，见 §1.3）。

### 1.2 发出路径与拦截条件（electronMain.js `KYe` + `Vpx`）

claim 由 campaign H5（iframe，`sandbox="allow-forms allow-same-origin allow-scripts"`、`referrerPolicy="no-referrer"`）的 JS 发出。主进程在 DI 容器装配时调用 `Vpx()`：

```js
// electronMain.js（还原后，函数名保留混淆名）
function Vpx(t = {}) {
  const f = ["https://*/*"];
  t.allowInsecureLoopback === true && f.push("http://127.0.0.1/*", "http://localhost/*");
  return {
    clientIdentity: {
      clientVersion: app.getVersion(),   // "0.4.8"
      machineOs: qMx(),                  // "x86_64_win32"
      machineHostname: DR0()             // sanitize(os.hostname())
    },
    installBeforeSendHeaders: r => {
      session.defaultSession.webRequest.onBeforeSendHeaders({urls: f}, (details, cb) => { ... });
    }
  };
}
```

拦截放行条件（`KYe`，全部满足才注入头）：

```js
resourceType === "xhr"
&& 请求 URL 与 frameUrl 均不含 userinfo
&& new URL(请求URL).origin ∈ surface.allowedRequestOrigins   // = {campaignOrigin, apiBaseUrl origin}
&& new URL(frameUrl).origin === surface.campaignOrigin        // 必须从活动页面发出
&& method ∈ AYe                                                // GET/POST/PUT/PATCH/DELETE/OPTIONS
```

### 1.3 请求头（`MYe` —— 真实的头重写器，还原后）

```js
function MYe(origHeaders, token, clientType, clientIdentity, machineIdentity) {
  const out = {};
  // 1) 透传页面原有头，但剥掉(小写比较) "authorization" 和一切 "cosy-" 开头的头
  for (const [k, v] of Object.entries(origHeaders)) {
    const lk = k.toLowerCase();
    if (lk === "authorization" || lk.startsWith("cosy-")) continue;
    out[k] = v;
  }
  // 2) 注入（值经 NYe 校验：trim 后非空且 /^[\\x20-\\x7e]+$/，否则整头丢弃）
  out["Authorization"]  = "Bearer " + token;
  out["Cosy-ClientType"] = String(clientType);          // "10"
  Fl(out, "Cosy-Version",        clientIdentity.clientVersion);  // "0.4.8"
  Fl(out, "Cosy-MachineOS",      clientIdentity.machineOs);      // "x86_64_win32"
  Fl(out, "Cosy-MachineHostname", clientIdentity.machineHostname); // os.hostname() 见 §4
  machineIdentity && Fl(out, "Cosy-MachineId",    machineIdentity.machineId);     // UUID 见 §3.3
  machineIdentity && Fl(out, "Cosy-MachineToken", machineIdentity.machineToken);  // runtime-info.exe 见 §3
  machineIdentity && Fl(out, "Cosy-MachineCode",  machineIdentity.machineCode);
  machineIdentity && Fl(out, "Cosy-MachineType",  machineIdentity.machineType);
  return out;
}
```

claim 请求的逐头规格（H5 发出、拦截器重写后到达服务端的形态）：

| Header | 值 | 来源 |
|---|---|---|
| Authorization | `Bearer <accessToken>` | `authService.getAccessToken()`（登录态 token，运行时刷新） |
| Cosy-ClientType | `10` | `Hs.clientType`，`Hs = Object.freeze({clientType:10, businessProduct:"app", sessionType:"app"})` |
| Cosy-Version | `0.4.8`（随客户端版本变化） | `app.getVersion()`（asar package.json / build-manifest.json 的 productVersion） |
| Cosy-MachineOS | `x86_64_win32` | `qMx()`：`(arch==="x64"?"x86_64":arch==="arm64"?"aarch64":arch) + "_" + platform` |
| Cosy-MachineHostname | 本机主机名（如 `DESKTOP-XXXXXXX`） | `DR0(os.hostname())`，见 §4 |
| Cosy-MachineId | UUID（如 `xxxxxxxx-xxxx-...`） | `%APPDATA%\com.qoder.app.stable\auth.machine-id` 文件，见 §3.3 |
| Cosy-MachineToken | runtime-info.exe 输出的 machineToken | sgsdk.dll getSecurityToken，见 §3 |
| Cosy-MachineCode | runtime-info.exe 输出的 machineCode | 同上 |
| Cosy-MachineType | runtime-info.exe 输出的 machineType（含 VM 检测语义） | 同上 |
| User-Agent | Electron 默认 session UA（未 setUserAgent 自定义）：`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) qoder/0.4.8 Chrome/143.0.0.0 Electron/43.1.1 Safari/537.36` | Electron 43 默认模板（name=qoder, version=0.4.8）。**注**：这是按 Electron 默认 UA 规则的推断，Chrome 143 为本构建 Chromium 版本 |
| Accept / Content-Type / 其它 | H5 页面 JS 自带值原样透传 | 页面行为，桌面代码不含 |

- **machineIdentity 可为 null**（runtime-info.exe 失败时 `catch(()=>null)`），此时 Cosy-MachineId/Token/Code/Type 四头**全部不发送**——即服务端必须容忍老机器/异常场景下缺这四个头，但 MachineHostname/OS/Version/ClientType 恒发。
- **无额外签名头**：claim 路径没有任何 x-signature / 请求签名机制（与 `/algo` 网关的 COSY 签名不同，sash openapi 只用 Bearer token + Cosy 设备头）。
- **body**：桌面代码不含 claim body 定义（H5 行为）。结合现有代理实测（无 body 也能命中端点，错误码是 SAME_PERSON_ALREADY_CLAIMED 而非参数错误），claim 大概率无 body 或空 JSON。

### 1.4 与代理现状对照（判重问题）

代理 `_cosy_device_headers`（qoder_intl_proxy.py L496）与桌面真实形态逐头对比：

| Header | 代理 | 桌面 | 差异影响 |
|---|---|---|---|
| Cosy-Version | `1.1.69` | `0.4.8` | 版本语义不同（CLI vs App）；服务端可能按版本路由风控 |
| Cosy-MachineHostname | ❌ 无 | 有 | 已知客户端 campaign 请求带此头；服务端可用它参与设备指纹 |
| Cosy-MachineId | acct.machineId | auth.machine-id UUID | 建议代理直接读 `%APPDATA%\com.qoder.app.stable\auth.machine-id` 与客户端同值 |
| Cosy-MachineToken | fallback=machineId | runtime-info.exe 输出 | 建议代理 spawn 客户端 runtime-info.exe（sidecar，同 qoder_intl_sign.mjs 先例），见 §3.4 |
| Cosy-MachineType | `"5"`（写死） | runtime-info.exe 输出 | 同上，值语义未实测确认 |
| Cosy-MachineCode | acct.machineCode | runtime-info.exe 输出 | 同上 |
| User-Agent | `qoder-intl-proxy/1.0` | Electron UA / `Qoder` | 建议改为桌面 UA |

> SAME_PERSON_ALREADY_CLAIMED 大概率不是"请求形态不像桌面"导致的：该错误语义是**同账号（同 person）当日已领过**。若用户桌面客户端当天已自动弹窗/手动领过，代理再 claim 必然判重。请求形态对齐的价值在于排除"设备指纹不匹配被当作第二个设备/异常流量"的风控放大。建议先在同一台装了客户端的机器上：客户端不点领取 → 代理以完全对齐的头去 claim → 观察是否成功，以此二分定位。

---

## 2. campaigns GET 列表请求完整规格（主进程 native 发出）

### 2.1 基本形态

```
GET https://openapi.qoder.sh/sash/api/v1/me/campaigns
```

- URL 构造：`new URL("/sash/api/v1/me/campaigns", discoveryBaseUrl)`，`discoveryBaseUrl = apiBaseUrl = rTe()`：

```js
function rTe() {
  return process.env.QODER_OPENAPI_BASE_URL || Ev()["openApiBaseUrl"]; // 配置文件字段
  // 国际版默认 https://openapi.qoder.sh（electronMain.js 内常量，另有 test-openapi.qoder.sh / openapi.qoder.com.cn）
}
```

- method：`GET`；无 body；无 query（`forceRefresh` 只是客户端缓存控制，不上 URL）
- 超时 `AbortSignal.timeout(15000)`（`SYe=15e3`）；状态缓存 300s（`XYe=300*1e3`）；401/403 → `invalidateAccessToken`
- 请求器：`nativeHttpService`（Electron `net` 封装，`tyx.request`，仅允许 https 或 127.0.0.1/localhost/[::1] 的 http）

### 2.2 请求头（`createRequestHeaders`，还原后）

```js
async createRequestHeaders(accountIdentity) {
  return this.nativeCampaignRequestService.createAuthorizedHeaders(
    { Accept: "application/json", "User-Agent": "Qoder" },   // ← 注意 UA 字面量就是 "Qoder"
    { token, clientType: Hs.clientType /*10*/,
      machineIdentity: await this.resolveMachineIdentity() }
  );  // → 即 §1.3 的 MYe()
}
```

| Header | 值 |
|---|---|
| Accept | `application/json` |
| User-Agent | **`Qoder`**（字面量，主进程 native 请求专用） |
| Authorization | `Bearer <accessToken>` |
| Cosy-ClientType | `10` |
| Cosy-Version | `0.4.8` |
| Cosy-MachineOS | `x86_64_win32` |
| Cosy-MachineHostname | `os.hostname()`（§4） |
| Cosy-MachineId / Token / Code / Type | 同 §1.3（machineIdentity 失败时缺失） |

> 注意：**同一个客户端，两种请求两种 UA**——列表 GET（native）UA=`Qoder`；claim（H5 iframe）UA=Electron 浏览器 UA。代理若模拟桌面，列表用 `Qoder`、claim 用 Electron UA 最保真。

### 2.3 同一服务的另外两个请求（补充）

- `GET {apiBaseUrl}/sash/api/v1/me/campaigns/client_launch_26/limited-number`（限量编号查询，`requestLimitedNumber`；失败重试 1 次延迟 750ms）
- 响应解析 `OYe`：`{showCampaign: bool, claimable: bool, campaignUrl: string}`；`campaignUrl` 必须通过 `cax()` 信任校验：

```js
// cax(): https + hostname(小写) ∈ {"test.qoder.ai"} 或 等于/以 .qoder.sh/.qoder.com/.qoder.com.cn 结尾
// allowInsecureLoopback=true 时额外允许 http://127.0.0.1|localhost|[::1]
```

---

## 3. machineToken / machineCode / machineType 生成方式

### 3.1 链路（electronMain.js，全部还原）

```
GYe(nativeRiskIdentityMainService, 缓存 + 3600s±300s 定时刷新)
  └─ wMx({runtimeDirectory, platform, environment})           // DI: Wpx(umidRuntimeDirectory, regionCode)
       executablePath = path.join(umidRuntimeDirectory, "runtime-info.exe")   // win32
       environment    = regionCode === "global" ? 3 : 0
  └─ LMx()  // spawn 原生进程
       argv  = [String(environment), "--account-stdin"]        // win32/darwin；linux 只传 environment
       stdin = JSON.stringify({account}) + "\n"                // account = 登录账号 id 字符串
       stdio = [pipe, pipe, pipe], windowsHide: true
       超时 25s，stdout 上限 1MB
  └─ zMx(stdout)  // JSON.parse 后提取：
       { machineToken: qf0(f.machineToken),
         machineType:  qf0(f.machineType),
         machineCode:  qf0(f.machineCode) }
       // qf0: trim 后非空、UTF-8 字节数 ≤ 4096、可打印 ASCII(/^[\\x21-\\x7e](?:[\\x20-\\x7e]*[\\x21-\\x7e])?$/) 才接受
```

- `account` 的来源：authService 登录态订阅 → `nativeRiskIdentityService.setActiveAccount(account.id)`（`ZYe`：字符串 trim、≤512 字节）
- 实际文件：`D:\Programs\Qoder\resources\umid\runtime-info.exe`（Rust 编写，480KB）+ `sgsdk.dll`（7.3MB）
- runtime-info.exe 侧写（PE 字符串）：加载同目录 `sgsdk.dll`，`LoadLibrary`/`dlsym` 取 `initUMID` / `getSecurityToken` / `freeSecurityToken` / `setActiveAccount`；另有 **VM 检测**（注册表探测 Amazon EC2 / Azure / Alibaba ECS / Tencent CVM / VMware + BIOS 信息）输出 `isVm/brand/percentage/vmTypeCode/vmInfo` 字段——**machineType 很可能就是 VM 检测类型码**（代理写死的 `"5"` 语义未在桌面代码中出现，需实测真实值）
- sgsdk.dll 导出：`initUMID, getSecurityToken, freeSecurityToken, setActiveAccount, zeroUMID, sgInitUMIDBySgCookie, sgscert_*`——阿里 SecurityGuard（无线保镖）系设备指纹 SDK，**算法黑盒，无法纯代理侧复现**

### 3.2 machineId（Cosy-MachineId，与上面三个不同源！）

```js
// machineIdentityMainService（jJ 类，electronMain.js）
identityPath = path.join(userDataDir, "auth.machine-id")
// userDataDir = %APPDATA%\com.qoder.app.stable（product.json: dataDirectoryName）

async resolveMachineId() {
  const saved = await this.readIdentity();      // 读文件 trim，UUID 正则校验
  if (saved) return saved;
  const id = randomUUID();                       // node:crypto
  await this.writeIdentity(id);                  // 临时文件 + rename 原子写
  return id;
}
// UUID 校验正则: /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
```

**machineId 与硬件无关**，是首次启动生成的随机 UUID 持久化文件。campaign 请求里的 machineIdentity 组装：

```js
async resolveMachineIdentity() {
  const [machineId, risk] = await Promise.all([
    this.machineIdentityService.getMachineId(),          // auth.machine-id
    this.nativeRiskIdentityService.getMachineIdentity()  // runtime-info.exe（失败→null，四头全缺）
  ]);
  return { machineId, ...risk };
}
```

### 3.3 代理侧复现方案（可行）

1. **machineId**：直接读 `%APPDATA%\com.qoder.app.stable\auth.machine-id`（与客户端共享同一设备身份）。
2. **machineToken/Code/Type**：sidecar 方式 spawn 客户端自带 exe（先例：qoder_intl_sign.mjs 驱动 worker runtime）：

```python
# 伪代码
p = subprocess.Popen([r"D:\Programs\Qoder\resources\umid\runtime-info.exe", "3", "--account-stdin"],
                     stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
out, _ = p.communicate(json.dumps({"account": uid}).encode() + b"\n", timeout=25)
ident = json.loads(out)   # {machineToken, machineType, machineCode, isVm, ...}
```

   - environment 参数：国际版传 `"3"`（global）；注意该参数是**字符串形态**（`String(environment)`）
   - 结果缓存 1 小时（客户端行为），无需每次请求都跑

---

## 4. Cosy-MachineHostname 取值逻辑（DR0，完整还原）

```js
// eS0 = node:os 的 hostname()；Wsx = node:url 的 domainToASCII
function DR0(t = os.hostname()) {
  const x = t.trim();
  if (!x) return;                                  // 空 → 头不发送
  if ($b0.test(x)) return Lf0(x);                  // 可打印 ASCII 且无首尾空格 → 长度限制器
  const r = domainToASCII(x);                      // punycode/IDNA（处理中文等非 ASCII 主机名）
  if (r && $b0.test(r)) return Lf0(r);
  const hash8 = sha256(x, "utf8").hex.slice(0, 8);
  const cleaned = x.replace(/[^\x21-\x7e]+/gu, "-").replace(/-{2,}/gu, "-").replace(/^-+|-+$/gu, "");
  return Lf0(cleaned ? cleaned + "-" + hash8 : "unknown-" + hash8);
}
function Lf0(t) {                                  // 上限 96 字符
  if (t.length <= 96) return t;
  const h = sha256(t, "utf8").hex.slice(0, 8);
  const head = t.slice(0, 96 - 8 - 1).replace(/[-\s]+$/u, "");
  return head ? head + "-" + h : "unknown-" + h;
}
// $b0 = /^[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?$/u
```

**Windows 常规场景：`Cosy-MachineHostname = os.hostname() 原值`**（如 `DESKTOP-ABC1234`，≤96 字符直通）。Python 侧等价实现：`socket.gethostname()`（注意 trim）。仅当主机名含非 ASCII（如中文计算机名）才走 domainToASCII / sha256 兜底路径。

---

## 5. 客户端 UI 触发签到的代码路径

```
主进程 (out/main/electronMain.js)
  X30 campaignMainService（DI token "campaignMainService"）
    ├─ authService.stateObs 订阅: signed_in → nativeRiskIdentityService.setActiveAccount(account.id)
    ├─ getStatus(forceRefresh) → GET /sash/api/v1/me/campaigns → 缓存 {showCampaign, campaignUrl, claimable}
    ├─ openSurface(webContentsId, ctx)
    │    └─ nativeCampaignRequestService.registerSurface({
    │         webContentsId, campaignUrl, apiBaseUrl,
    │         clientType: Hs.clientType /*10*/,
    │         getAccessToken, getMachineIdentity })
    │         // 同时 Vpx().installBeforeSendHeaders 已装 session.webRequest.onBeforeSendHeaders
    └─ telemetry: ENTRY_VISIBLE / USAGE_ENTRY_CLICK / BENEFIT_ENTRY_SHOW / BENEFIT_ENTRY_CLICK
         / SURFACE_OPENED / SURFACE_CLOSED / SLOT_CLOSE(slot="quest.sidebar.campaign") / SURFACE_LOAD_FINISHED

preload (out/preload/index.cjs) —— IPC bridge 方法名
  getCampaignStatus / getCampaignLimitedNumber / reportUsageEntryClick
  / reportCampaignBenefitEntryShow / reportCampaignEntryVisible / reportCampaignEntryClick
  / openCampaignSurface / closeCampaignSurface / reportCampaignSurfaceLoadResult

渲染进程 (out/renderer/assets/DIR8HOYO.js)
  ├─ campaign 状态管理器: requestRefresh/updateAttention（claimable→入口红点）；source:"automatic" 自动弹
  ├─ Usage 面板入口（i18n）: usage.rewards="Rewards"、usage.openCampaign="Open benefits"、
  │    usage.campaign="Benefits"（中文："奖励"/"打开奖励"/"福利"）
  └─ campaign surface 容器组件:
       openCampaignSurface({campaignUrl, source, reopened, language, theme})
       → <iframe src={campaignUrl} sandbox="allow-forms allow-same-origin allow-scripts"
                 referrerPolicy="no-referrer" allow="camera 'none'; ...">
       → postMessage 协议(H5↔桌面): campaign:ready / campaign:requestClose
         / campaign:stateChanged / campaign:openExternal
       → iframe 内 H5 JS 发 claim（§1），成功后 campaign:stateChanged 通知桌面刷新列表
```

即：用户点 Usage 面板的 "Open benefits"（或客户端 `source:"automatic"` 自动弹出）→ iframe 加载 `https://<qoder 域>/...` 的 H5 → H5 内点"领取"发 claim → 主进程拦截器注入设备头 → H5 postMessage 通知刷新。

---

## 6. claim 端点额外参数排查结论

| 排查项 | 结果 |
|---|---|
| query 参数 | 桌面代码无任何 claim URL 构造（H5 行为）；无证据 |
| body | 无桌面侧证据（H5 行为）；实测空 body 可达端点 |
| 签名头（x-signature 等） | **无**。campaign/claim 路径只有 Bearer + Cosy-* 设备头，无 COSY 签名（那是 /algo 网关的机制） |
| 幂等头 | 主进程通用 openapi 请求器 `cDx` 允许 `x-trace-id/x-request-id/if-match/idempotency-key`，但**均为调用方显式传入才发**；campaign 服务未传 → claim 无这些头 |
| CSRF/Referer | iframe `referrerPolicy="no-referrer"` → claim 无 Referer 头 |
| Origin | 浏览器 xhr 自动带 `Origin: <campaignUrl origin>`（qoder 域，非 openapi 域，跨域 CORS 场景）——H5 行为，代理可不发或按需模拟 |

---

## 7. 搜过但未找到的东西（避免重复劳动）

- **app.asar 全部 5168 个文件**（含 unpacked）字节搜：`me/campaigns`+`/claim` 端点字符串 **0 处**（claim 全部命中是 `claimable`/`claimedAt`/`claimedAppPluginIdentities`/聊天字段 `claim:`）；`CLAIM_BENEFIT` 0 处；`SAME_PERSON` 0 处；`campaigns` 复数仅 `/sash/api/v1/me/campaigns` 一个端点。→ **桌面代码确实不构造 claim 请求**（H5 页面行为，页面 JS 不在客户端安装目录内）。
- `sgsdk`/`umid`/`runtime-info` 在 electronMain.js 中无明文（全部藏在混淆字符串数组里，本报告已通过解码器还原定位）；`runtime-info.json`（@10305356）是 ripgrep 资产清单，与 umid 无关。
- **extensions 9 个内置扩展**（qoder.security/canvas/knowledge.center/computer-control/office/sites/index-settings/find-extensions/desktop-pet-skill）：无签到/campaign/claim/Cosy 实现（claim/hostname 命中均为文档词汇或无关 API；qoder.knowledge.center 的 `/sash/` 为其它接口）。
- **bundled-resources**（catalog.v1.json + archives 两个 zip）：全部关键词 **0 命中**。
- `setUserAgent` 调用：无（webview UA 为 Electron 默认）。electronMain.js 中唯一的硬编码 UA `Mozilla/5.0 (Macintosh; ...) Chrome/131...` 属于 LinkPreview（链接预览）服务，与签到无关。
- worker runtime `qoder-worker-runtime.obf.mjs`（33MB）：按分工未分析（同事负责）。
- `Cosy-MachineType` 的具体取值语义（VM 检测码？）：桌面代码不含，需实测 runtime-info.exe 输出确认。
- claim body 的确切形态（空/JSON）：H5 侧资源未随客户端分发，无法静态确认。

---

## 附录 A：关键文件与偏移速查（electronMain.js，node utf8 字符坐标）

| 内容 | 位置（约） |
|---|---|
| `$Ye` headers 脱敏序列化器（`<present>`/`<missing>`/`<redacted>`，日志/IPC 防泄漏用，非请求构造器） | 14201408 |
| `MYe` 真实头重写器 + `Fl`/`NYe` 值校验 | 14184029 起 |
| `KYe` 拦截放行条件 + `AYe` method 白名单（GET/POST/PUT/PATCH/DELETE/OPTIONS） | 14181700 |
| `Vpx` clientIdentity（getVersion/qMx/DR0）+ onBeforeSendHeaders 安装器 | 9978100 |
| `qMx`（MachineOS=`x86_64_win32`） | 9896973 |
| `DR0`（MachineHostname= sanitize(os.hostname())） | 9897166 |
| `wMx`/`LMx`（runtime-info.exe spawn 协议）+ `zMx`/`qf0`（输出解析校验） | 9896600/9896900/9899916 |
| 常量 `Hg0=96, Qb0=8, oMx=4096, pg0=1MB, vMx=25s` + `$b0` 正则 | 9896300 |
| `Hs = {clientType:10, businessProduct:"app", sessionType:"app"}` + `LJ0` 基础头模板（UA:"Qoder"） | 8895940 |
| `rTe`（apiBaseUrl = QODER_OPENAPI_BASE_URL ‖ openApiBaseUrl 配置） | 14203067 |
| `kYe="/sash/api/v1/me/campaigns"`, `EYe="client_launch_26"`, `IYe=["qoder.sh","qoder.com","qoder.com.cn"]`, `UYe={"test.qoder.ai"}` | 14187380 起 |
| X30 campaignMainService（getStatus/openSurface/createRequestHeaders/resolveMachineIdentity） | 14188700–14196000 |
| GYe nativeRiskIdentityMainService（缓存/3600s±300s 刷新/setActiveAccount） | 14185500 |
| jJ machineIdentityMainService（auth.machine-id randomUUID 持久化） | 8942500 |
| `cax` campaignUrl 域名信任校验 | 14200601 |

## 附录 B：复现工具（本次分析产物，位于 D:\GitHub\qoder_asar_extract\）

- `asar_scan.py` / `asar_extract.py`：自写 asar 解析器（注意：自写版 base offset 有 3 字节 padding 偏差，**正式提取请用 `npx @electron/asar extract-file`**，产物在 `official_extract/`）
- `decode_electronMain.mjs`：electronMain.js 字符串数组解码器（OU 数组 + u0 + shuffle IIFE + b 包装，`node decode_electronMain.mjs "a,b"` 可查任意调用）
- `deobf2.mjs`：acorn AST 作用域感知批量还原（`replacements.json` 32 万条）；`render_ctx.mjs`：区间渲染还原代码
- `ctx_tool.py`：明文 bundle 关键词上下文查看器

## 附录 C：建议的代理对齐 checklist

1. 列表 GET 用 `User-Agent: Qoder`，claim POST 用 Electron UA（`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) qoder/0.4.8 Chrome/143.0.0.0 Electron/43.1.1 Safari/537.36`）
2. `Cosy-Version` 改为客户端 App 版本（当前 0.4.8，随客户端升级同步）
3. 补 `Cosy-MachineHostname: socket.gethostname().strip()`（>96 字符按 §4 的 Lf0 截断+sha256 前 8 hex）
4. `Cosy-MachineId` 直接读 `%APPDATA%\com.qoder.app.stable\auth.machine-id`
5. `Cosy-MachineToken/Code/Type` sidecar spawn `runtime-info.exe 3 --account-stdin`（stdin `{"account": "<uid>"}`，缓存 1h）
6. claim 不带 Referer；可带 `Origin: https://<campaignUrl 域>`；无签名头
7. 判重排查：先在同一台机器验证"客户端未领 → 代理对齐后 claim"是否成功，以区分"形态被风控"与"当日名额已被客户端消耗"