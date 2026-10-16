#!/usr/bin/env node
// qoder_sign.mjs — Qoder CN 请求签名 sidecar（qoder_proxy.py 的子进程）
//
// Qoder CN 的 /algo 网关要求 Authorization: Bearer COSY.<...> 签名，签名与请求体
// 编码都在客户端的 WASM 模块里（qoder_auth_wasm）。本脚本直接驱动该 WASM，不启动、
// 不依赖 Qoder 桌面端进程，只需要客户端安装目录里的 worker runtime 文件。
//
// 协议：stdin/stdout 按行 JSON（JSON-RPC 风格）
//   --> {"id":1,"cmd":"ping"}
//   <-- {"id":1,"ok":true,"data":{...}}
//   --> {"id":2,"cmd":"request","auth":{...},"endpoint":"https://...","path":"/algo/...",
//        "method":"GET","body":null,"infer":{"model":"qfmodel","source":"system"}}
//   <-- {"id":2,"ok":true,"data":{"url":"...","headers":{...},"body":"..."|null}}
//
// 单独运行（调试）：
//   node qoder_sign.mjs --selftest

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

// ── 定位客户端 worker runtime ───────────────────────────────────────────────
const BUNDLE_CANDIDATES = [
  process.env.QODER_WORKER_BUNDLE,
  path.join(HERE, "qoder_assets", "qoder-worker-runtime.obf.mjs"),
  // Windows 默认安装位置
  "C:/Programs/Qoder CN/resources/app.asar.unpacked/node_modules/@qoder-ai/qoder-cn-agent-sdk/dist/_worker/qoder-worker-runtime.obf.mjs",
  // 其它常见盘符/自定义安装目录
  ...(process.platform === "win32"
    ? ["C", "D", "E"].flatMap((d) => [
        path.join(`${d}:\\`, "Programs", "Qoder CN", "resources", "app.asar.unpacked",
          "node_modules", "@qoder-ai", "qoder-cn-agent-sdk", "dist", "_worker",
          "qoder-worker-runtime.obf.mjs"),
      ])
    : []),
  // macOS / Linux
  "/Applications/Qoder CN.app/Contents/Resources/app.asar.unpacked/node_modules/@qoder-ai/qoder-cn-agent-sdk/dist/_worker/qoder-worker-runtime.obf.mjs",
].filter(Boolean);

function findBundle() {
  for (const p of BUNDLE_CANDIDATES) {
    try { if (fs.statSync(p).isFile()) return p; } catch {}
  }
  throw new Error("找不到 Qoder worker runtime，请设置环境变量 QODER_WORKER_BUNDLE 指向 " +
    "qoder-worker-runtime.obf.mjs（可从已安装客户端 resources/app.asar.unpacked/... 复制）");
}

// 把 bundle 里的 dn(exports, getters) 注册点插桩，用于按导出名反查内部模块；
// 同时删掉文件尾部的 CLI 入口调用，避免 import 时真的把 CLI 跑起来。
const DN_NEEDLE = "(A,e)=>{for(var t in e)gTA(A,t,{get:e[t],enumerable:!0})}";
const ENTRY_NEEDLES = ["fx(),vsr(", "vsr(async()=>"];

function patchedPath(bundle) {
  const st = fs.statSync(bundle);
  const tag = crypto.createHash("sha1").update(`${bundle}|${st.size}|${st.mtimeMs}`).digest("hex").slice(0, 16);
  const dir = process.env.QODER_PROXY_CACHE
    || path.join(os.homedir(), ".qoder-cn-proxy", "cache");
  fs.mkdirSync(dir, { recursive: true });
  const out = path.join(dir, `qoder-worker-${tag}.patched.mjs`);
  if (fs.existsSync(out)) return out;
  let src = fs.readFileSync(bundle, "utf8");
  if (!src.includes(DN_NEEDLE)) throw new Error("bundle 结构变化：未找到 dn() 注册助手（需要更新 qoder_sign.mjs）");
  src = src.replace(DN_NEEDLE,
    "(A,e)=>{(globalThis.__QMODS??=[]).push({exports:A,getters:e});for(var t in e)gTA(A,t,{get:e[t],enumerable:!0})}");
  let cut = -1;
  for (const n of ENTRY_NEEDLES) {
    const i = src.lastIndexOf(n);
    if (i > src.length - 20000 && (cut < 0 || i < cut)) cut = i;
  }
  if (cut < 0) throw new Error("bundle 结构变化：未定位到 CLI 入口调用");
  const head = src.slice(0, cut);
  if (!/[;}\])]$/.test(head.slice(-64))) {
    throw new Error("bundle 结构变化：CLI 入口切割点落在表达式中间，需要更新 ENTRY_NEEDLES");
  }
  src = head + "\nexport const __qoderSignerStripped = true;\n";
  const tmp = out + ".tmp";
  fs.writeFileSync(tmp, src);
  fs.renameSync(tmp, out);
  return out;
}

// ── 加载 WASM 签名器 ────────────────────────────────────────────────────────
let MODS = null;      // 插桩后记录到的模块导出表
let GLUE = null;      // wasm-bindgen 直接导出（QoderContext / generate_runtime_auth_fields ...）
let HELP = null;      // 高层助手（createContext / headersMapToRecord ...）
let CTX = null;       // 已签名的当前 QoderContext
let ctxKey = "";

function pick(...names) {
  for (const m of MODS || []) if (names.every((n) => n in m.getters)) return m.exports;
  return null;
}

function exported(name) {
  for (const m of MODS || []) if (name in m.getters) { try { return m.exports[name]; } catch { return undefined; } }
  return undefined;
}

async function init() {
  if (MODS) return;
  const bundle = findBundle();
  const mod = await import(pathToFileURL(patchedPath(bundle)).href);
  if (!globalThis.__QMODS?.length) throw new Error("插桩失败：__QMODS 为空");
  MODS = globalThis.__QMODS;
  GLUE = pick("QoderContext", "generate_runtime_auth_fields");
  HELP = pick("prepareWasmAuthenticatedRequest", "createContext");
  if (!GLUE || !HELP) throw new Error("未找到 Qoder WASM 签名模块（客户端版本可能已变化）");
  const boot = await HELP.initWasm();
  GLUE = boot && boot.QoderContext ? boot : GLUE;
  MODS.__version = exported("COSY_VERSION") || exported("CLI_VERSION") || "1.1.69";
}

// 登录态来自 auth.v1.dat（由 qoder_proxy.py 解密后传入）：
//   {token, user:{id}, machineId, cosyVersion}
function ensureContext(auth) {
  if (!auth || !auth.token || !auth.uid) throw new Error("auth 需要 token 与 uid");
  const machineId = auth.machineId || "unknown";
  const cosyVersion = auth.cosyVersion || MODS.__version || "1.1.69";
  const fields = JSON.parse(String(GLUE.generate_runtime_auth_fields(JSON.stringify({
    uid: auth.uid,
    security_oauth_token: auth.token,
    organization_id: auth.organizationId || "",
    organization_tags: auth.organizationTags || [],
    data_policy_agreed: !!auth.dataPolicyAgreed,
  }))));
  const userInfoJson = JSON.stringify({ uid: auth.uid, ...fields });
  const key = `${machineId}|${cosyVersion}|${userInfoJson}`;
  if (key !== ctxKey) {
    try { CTX?.free?.(); } catch {}
    CTX = HELP.createContext(machineId, cosyVersion, userInfoJson);
    ctxKey = key;
  }
  return CTX;
}

function headerRecord(map) {
  try { return HELP.headersMapToRecord(map); } catch {
    const out = {};
    try { map?.forEach?.((v, k) => { out[k] = v; }); } catch {}
    return out;
  }
}

// cmd:"request" → 产出可直接发送的 url/headers/body
function buildRequest(p) {
  const ctx = ensureContext(p.auth || {});
  const body = p.body == null ? undefined : (typeof p.body === "string" ? p.body : JSON.stringify(p.body));
  let r;
  if (p.infer) {
    // 聊天：prepareInferRequest(endpoint, bodyJson, modelKey, modelSource)
    if (body === undefined) throw new Error("infer 请求需要 body");
    r = ctx.prepareInferRequest(p.endpoint, body, p.infer.model, p.infer.source);
  } else {
    r = ctx.prepareRequest(p.endpoint, p.path, (p.method || "GET").toUpperCase(),
      p.authType || "auth", body, p.extraHeaders ? JSON.stringify(p.extraHeaders) : undefined);
  }
  const out = { url: r.url, headers: headerRecord(r.headers) };
  if (r.body !== undefined && r.body !== null) out.body = String(r.body);
  return out;
}

function info() {
  let bundle = null, err = null;
  try { bundle = findBundle(); } catch (e) { err = String(e.message || e); }
  return {
    bundle, error: err,
    cosyVersion: MODS?.__version || null,
    wasmReady: !!MODS,
    node: process.version,
  };
}

// ── JSON-lines RPC ──────────────────────────────────────────────────────────
const HANDLERS = {
  ping: async () => info(),
  request: async (p) => buildRequest(p),
};

function reply(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function handleLine(line) {
  let msg;
  try { msg = JSON.parse(line); } catch { return reply({ ok: false, error: "bad json" }); }
  const { id, cmd, ...params } = msg;
  if (cmd === "exit") { reply({ id, ok: true }); process.exit(0); }
  const fn = HANDLERS[cmd];
  if (!fn) return reply({ id, ok: false, error: `unknown cmd: ${cmd}` });
  try {
    await init();
    reply({ id, ok: true, data: await fn(params) });
  } catch (e) {
    reply({ id, ok: false, error: String(e?.message || e), detail: String(e?.stack || "").split("\n").slice(0, 3).join(" | ") });
  }
}

async function selftest() {
  await init();
  console.log("info:", JSON.stringify(info()));
  const authFile = path.join(HERE, "qoder_proxy_accounts.json");
  if (!fs.existsSync(authFile)) { console.log("no pool file; 只验证签名器加载"); return; }
  const pool = JSON.parse(fs.readFileSync(authFile, "utf8"));
  const acct = (pool.accounts || [])[0];
  if (!acct) { console.log("pool 为空"); return; }
  const req = buildRequest({
    auth: acct.auth || acct,
    endpoint: "https://gateway.qoder.com.cn",
    path: "/algo/api/v2/model/list",
    method: "GET",
  });
  const res = await fetch(req.url, { headers: req.headers });
  console.log("model/list HTTP", res.status, (await res.text()).slice(0, 120));
}

if (process.argv.includes("--selftest")) {
  await selftest();
} else {
  let buf = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) handleLine(line);
    }
  });
  process.stdin.on("end", () => process.exit(0));
  reply({ ok: true, event: "ready", data: { note: "awaiting init; info on first ping" } });
}
