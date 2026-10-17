# -*- coding: utf-8 -*-
"""
qoder_proxy.py —— Qoder CN 官方模型通道的本地 API 代理（OpenAI / Responses / Anthropic 三协议）

把已登录的 Qoder CN 桌面端官方模型通道转成本地标准 API，供任意客户端调用：
  OpenAI 兼容      GET  /v1/models、POST /v1/chat/completions（流式 + 非流式）
  OpenAI Responses POST /v1/responses（兼容 codex-cli 等）
  Anthropic 兼容   POST /v1/messages、POST /v1/messages/count_tokens
  健康检查         GET  /health
  CORS             OPTIONS 同上路径

上游是 Qoder CN 的推理网关（逆向自客户端 worker runtime）：
  POST https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation
       ?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1
  GET  https://gateway.qoder.com.cn/algo/api/v2/model/list
  两者都要求 Authorization: Bearer COSY.<base64>.<sig> 一整套 Cosy-* 签名头，
  且请求体要按 Encode=1 编码 —— 签名与编码已从客户端的 Rust/WASM 模块
  qoder_auth_wasm_bg.wasm 逐字节还原，实现在同目录的 qoder_cosy.py（纯 Python）。
  默认不需要 node；QODER_PROXY_SIGN_BACKEND=node 可切回 qoder_sign.mjs sidecar 对照。

脱离桌面端：只读磁盘上的登录态文件，不启动、不连接 Qoder CN 进程；
签名在进程内完成，客户端可以完全没打开。只有 --login 的设备码流程需要 node
（PKCE 授权与轮询逻辑在客户端 bundle 里，暂未用 Python 复刻）。
  %APPDATA%\\com.qodercn.app.stable\\auth.v1.dat   Electron safeStorage（DPAPI + AES-256-GCM）
  ~\\.qoder-cn\\.auth\\machine_id                    机器 ID（签名要用）
  ~\\.qoder-cn\\umid-cache.json                      machine_token/machineType/machineCode
  ~\\.qoder-cn\\.cache\\endpoint-cache.json           网关/openapi host（自动发现）

账号池（对齐 codearts_proxy.py 的用法）：
  --login              浏览器设备码登录换 token 入池（本机可以完全没有 Qoder CN 客户端）
  --capture            从本机客户端解密登录态并入池
  --list / --remove    查看、移除池内账号
  --export / --import  凭证包跨机迁移（目标机不需要 Windows/DPAPI/客户端）
  运行时              token 临期自动续期（POST openapi /api/v1/deviceToken/refresh，无需签名），
                      401/403 自动换号重试，续期链失效的账号标记 refresh_dead 不再反复尝试

零第三方依赖（纯标准库）。只有 --capture 需要 cryptography 解 AES-256-GCM；
用 --import 迁移动凭证时不需要任何依赖。日常代理与签名都不需要 node。

用法:
  python qoder_proxy.py                      # 启动代理（默认弹交互菜单，tty 时）
  python qoder_proxy.py --serve              # 跳过菜单直接启动
  python qoder_proxy.py --login              # 跳浏览器授权，拿不到客户端时用这个
  python qoder_proxy.py --login --new-device # 强制生成新设备 ID（多账号隔离）
  python qoder_proxy.py --capture
  python qoder_proxy.py --models
  python qoder_proxy.py --checkin            # 领今天的每日 Credits（多账号）
  python qoder_proxy.py --test-chat "hi"
"""
import argparse
import base64
import calendar
import ctypes
import http.client
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

import qoder_cosy as _cosy

# ── 常量与路径 ────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("QODER_PROXY_PORT", "8791"))
API_KEY = os.environ.get("QODER_PROXY_API_KEY") or ""      # 空 = 不鉴权
MAX_BODY_BYTES = 8 << 20                                    # 请求体上限，防 OOM
MAX_LINE_BYTES = 32 << 20        # 上游 SSE 单行上限；超过说明流坏了，直接断掉
TIMEOUT_SEC = int(os.environ.get("QODER_PROXY_TIMEOUT", "600"))
MODELS_TTL = int(os.environ.get("QODER_PROXY_MODELS_TTL", "3600"))
REFRESH_AHEAD_SEC = int(os.environ.get("QODER_PROXY_REFRESH_AHEAD", "86400"))
FAIL_COOLDOWN_SEC = 300         # 401/403 之后的账号冷却，避免立刻反复重试
SWEEP_SEC = 30                  # 账号池临期巡检最小间隔

SCENE = os.environ.get("QODER_PROXY_SCENE", "chat")
DEFAULT_MODEL = os.environ.get("QODER_PROXY_MODEL", "auto")
BUSINESS_TYPE = os.environ.get("QODER_PROXY_BUSINESS_TYPE", "agent")
BUSINESS_PRODUCT = os.environ.get("QODER_PROXY_BUSINESS_PRODUCT", "cli")

HERE = os.path.dirname(os.path.abspath(__file__))
SIGN_JS = os.environ.get("QODER_SIGN_JS", os.path.join(HERE, "qoder_sign.mjs"))
NODE = os.environ.get("QODER_PROXY_NODE", "node")
# auto（默认）：纯 Python 优先，异常才回落 node；py：只用纯 Python；node：只用 sidecar
SIGN_BACKEND = (os.environ.get("QODER_PROXY_SIGN_BACKEND") or "auto").strip().lower()
# 账号池默认放在仓库外：文件里是真 token，llm-proxy 是 git 工作区且没有 .gitignore，
# 放在脚本旁边很容易被一次 git add . 提交出去。用 QODER_PROXY_ACCOUNTS 可覆盖。
ACCOUNTS_FILE = os.environ.get("QODER_PROXY_ACCOUNTS") or os.path.join(
    os.path.expanduser("~"), ".qoder-cn-proxy", "accounts.json")

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
USERPROFILE = os.environ.get("USERPROFILE", HOME)
USER_DATA = os.path.join(APPDATA, "com.qodercn.app.stable")
QODER_HOME = os.path.join(USERPROFILE, ".qoder-cn")

AUTH_BLOB = os.path.join(USER_DATA, "auth.v1.dat")
LOCAL_STATE = os.path.join(USER_DATA, "Local State")
MACHINE_ID_FILE = os.path.join(QODER_HOME, ".auth", "machine_id")
UMID_FILE = os.path.join(QODER_HOME, "umid-cache.json")
ENDPOINT_CACHE = os.path.join(QODER_HOME, ".cache", "endpoint-cache.json")

MODEL_LIST_PATH = "/algo/api/v2/model/list"
INFER_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
INFER_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
REFRESH_PATH = "/api/v1/deviceToken/refresh"
USERINFO_PATH = "/api/v1/userinfo"
ORG_TAGS_PATH = "/api/v1/organizations/{org}/tags"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
MACHINE_OS = os.environ.get("QODER_PROXY_MACHINE_OS") or (
    "x86_64_win32" if sys.platform == "win32" else "x86_64_" + sys.platform)

DEFAULT_GATEWAY = "https://gateway.qoder.com.cn"
DEFAULT_OPENAPI = "https://openapi.qoder.com.cn"

USER_AGENT = "qoder-proxy/1.0 (+https://github.com)"


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _endpoint(kind):
    """从客户端 endpoint-cache 自动发现 host；拿不到就用官方默认域名。"""
    cache = _read_json_file(ENDPOINT_CACHE) or {}
    entries = cache.get("entries") or {}
    prod = entries.get("prod") or {}
    if kind == "openapi":
        host = prod.get("openapiEndpoint") or cache.get("openapiEndpoint")
    else:
        host = prod.get("inferEndpoints") or cache.get("inferEndpoints")
        if isinstance(host, list):
            host = host[0] if host else None
    if isinstance(host, str) and host.startswith("http"):
        return host.rstrip("/")
    return DEFAULT_GATEWAY if kind != "openapi" else DEFAULT_OPENAPI


GATEWAY = os.environ.get("QODER_PROXY_GATEWAY") or _endpoint("infer")
OPENAPI = os.environ.get("QODER_PROXY_OPENAPI") or _endpoint("openapi")

# 设备码登录（--login）：站点授权页 base 取自 bundle 里 startDeviceFlow 的 site 参数
# （CN 版是 https://qoder.cn，国际版是 https://qoder.com）；轮询走 openapi host。
# 客户端 bundle 里的懒初始化会给出真 client_id，所以这里不需要硬编码。
SITE = os.environ.get("QODER_PROXY_SITE") or "https://qoder.cn"
LOGIN_TIMEOUT_SEC = int(os.environ.get("QODER_PROXY_LOGIN_TIMEOUT", "330"))

LOG_FILE = os.environ.get("QODER_PROXY_LOG") or ""
LOG_MAX_BYTES = int(os.environ.get("QODER_PROXY_LOG_MAX_MB", "5")) << 20
_log_lock = threading.Lock()

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── 日志 / 脱敏 ───────────────────────────────────────────────────────────────
def _log(msg):
    if not LOG_FILE:
        return
    try:
        with _log_lock:
            try:
                if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".old")
            except Exception:
                pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _print_err(msg):
    sys.stderr.write(str(msg) + "\n")
    sys.stderr.flush()


def _rand():
    return secrets.token_hex(12)


def _merge_tool_name(cur, frag):
    """上游可能把工具名分片下发（"get_" + "time"），也可能整名重发；两种都要拼对。"""
    if not frag:
        return cur
    if not cur:
        return frag
    return cur if frag == cur else cur + frag


def _redact(v, keep=3):
    """凭证脱敏：只保留前缀，用于命令行展示。"""
    if not isinstance(v, str):
        return v
    if len(v) <= keep:
        return "*" * len(v)
    return v[:keep] + "*" * min(24, len(v) - keep)


def _mask_account(a):
    out = dict(a or {})
    for k in list(out):
        if any(t in k.lower() for t in ("token", "secret", "cookie", "password", "key")):
            out[k] = _redact(out[k])
    out.pop("auth", None)
    return out


# ── 客户端登录态解密（Windows safeStorage）────────────────────────────────────
try:                                                  # cryptography 仅 --capture 需要
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:
    AESGCM = None


def _dpapi_unprotect(blob):
    """CryptUnprotectData：解出 Local State 里的 Chromium os_crypt 主密钥。"""
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    inb = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outb = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, 0,
                                      ctypes.byref(outb)):
        raise OSError("CryptUnprotectData failed: %d" % kernel32.GetLastError())
    try:
        return ctypes.string_at(outb.pbData, outb.cbData)
    finally:
        kernel32.LocalFree(outb.pbData)


def _get_master_key():
    """Local State → os_crypt.encrypted_key → 去 "DPAPI" 前缀 → 32B AES-256 key。"""
    ls = _read_json_file(LOCAL_STATE)
    if not ls:
        raise RuntimeError("找不到 %s（Qoder CN 未安装或未登录）" % LOCAL_STATE)
    raw = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if raw[:5] != b"DPAPI":
        raise RuntimeError("Local State 主密钥前缀异常，客户端加密方案可能已变")
    return _dpapi_unprotect(raw[5:])


def _aes_unprotect(blob, key):
    """Electron safeStorage：v10 + 12B nonce + AES-256-GCM(ct)."""
    if AESGCM is None:
        raise RuntimeError("--capture 需要 cryptography：pip install cryptography，"
                           "或在别的机器上 --export 后用 --import 导入凭证包")
    if blob[:3] != b"v10":
        raise RuntimeError("auth.v1.dat 前缀异常（期望 v10），客户端加密方案可能已变")
    return AESGCM(key).decrypt(blob[3:15], blob[15:], None)


def _machine_identity():
    """签名要用 machine_id；续期要用 umid 里的 machine_token/machineType/machineCode。"""
    mid = ""
    try:
        with open(MACHINE_ID_FILE, encoding="utf-8") as f:
            mid = f.read().strip()
    except Exception:
        pass
    if not mid:
        app_mid = os.path.join(USER_DATA, "machine-id")
        try:
            with open(app_mid, encoding="utf-8") as f:
                mid = f.read().strip()
        except Exception:
            mid = ""
    umid = _read_json_file(UMID_FILE) or {}
    info = umid.get("info") if isinstance(umid.get("info"), dict) else umid
    return {
        "machineId": mid,
        "machineToken": (info or {}).get("machineToken") or "",
        "machineType": str((info or {}).get("machineType") or ""),
        "machineCode": (info or {}).get("machineCode") or "",
    }


_EXP_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
                      r"(Z|[+-]\d{2}:?\d{2})?")


def _parse_exp(s):
    """ISO8601 字符串 / unix 时间戳 → unix 秒；解析不了返回 None。"""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    if isinstance(s, str) and s.isdigit():
        return float(s)
    if not isinstance(s, str):
        return None
    m = _EXP_ISO.search(s)
    if not m:
        return None
    y, mo, d, hh, mm, ss, tz = m.groups()
    base = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    if not tz or tz == "Z":
        return float(base)
    sign = 1 if tz[0] == "+" else -1
    tz = tz[1:].replace(":", "")
    return float(base - sign * (int(tz[:2]) * 3600 + int(tz[2:4] or 0) * 60))


# ── openapi 明文 JSON（无需 Cosy 签名）───────────────────────────────────────
def _openapi_json(method, path, body=None, token="", timeout=20, headers=None):
    """打一次 openapi 请求，返回 (status, json_dict)。这几个端点只认 Bearer，不签名。"""
    url = OPENAPI + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        if v not in (None, ""):
            req.add_header(str(k), str(v))
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        return 0, {"message": "网络错误: %s" % e}
    try:
        return status, json.loads(raw) if raw.strip() else {}
    except Exception:
        return status, {"message": raw[:500]}


def _enrich_account(acct):
    """补 uid / 组织信息（签名要用）；只存 id 和显示名，不存邮箱等 PII。"""
    tok = acct.get("token") or ""
    st, ui = _openapi_json("GET", USERINFO_PATH, token=tok)
    if st == 200 and isinstance(ui, dict):
        acct["uid"] = ui.get("id") or acct.get("uid") or ""
        acct["organizationId"] = ui.get("orgId") or ""
        acct["displayName"] = (ui.get("displayName") or ui.get("name")
                               or ui.get("nickName") or acct.get("displayName") or "")
        org = acct["organizationId"]
        if org:
            st2, tags = _openapi_json("GET", ORG_TAGS_PATH.format(org=org), token=tok)
            if st2 == 200 and isinstance(tags, dict):
                acct["organizationTags"] = tags.get("tags") or tags.get("data") or []
    return acct


# ── 每日 Credits 签到（单账号）──────────────────────────────────────────────
def _cosy_device_headers(acct):
    return {
        "Cosy-Version": acct.get("cosyVersion") or "1.1.69",
        "Cosy-ClientType": "5",
        "Cosy-MachineOS": MACHINE_OS,
        "Cosy-MachineId": acct.get("machineId") or "",
        "Cosy-MachineToken": acct.get("machineToken") or acct.get("machineId") or "",
        "Cosy-MachineType": str(acct.get("machineType") or "5"),
        "Cosy-MachineCode": acct.get("machineCode") or "",
    }


def _campaign_id(campaign):
    for k in ("id", "campaignId", "campaign_id"):
        v = campaign.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _checkin_account(acct):
    """只给传入的这一个账号领今天的 campaign；没有可领项就是成功 no-op。"""
    token = acct.get("token") or ""
    headers = _cosy_device_headers(acct)
    st, obj = _openapi_json("GET", CAMPAIGNS_PATH, token=token, headers=headers)
    rep = {"status": st, "campaigns": 0, "claimable": False, "claimed": []}
    if st != 200 or not isinstance(obj, dict):
        rep["error"] = ((obj or {}).get("message") if isinstance(obj, dict) else "")
        rep["error"] = rep["error"] or "HTTP %s" % st
        return False, rep
    cps = [c for c in (obj.get("campaigns") or []) if isinstance(c, dict)]
    rep["campaigns"] = len(cps)
    rep["claimable"] = bool(obj.get("claimable"))
    if not rep["claimable"]:
        rep["message"] = "当前没有可领取的活动（可能已领、未到窗口或活动已结束）"
        return True, rep
    target = next((c for c in cps if _campaign_id(c)), None)
    if target is None:
        rep["message"] = "claimable=true 但响应里没有 campaign id"
        return False, rep
    cid = _campaign_id(target)
    path = "%s/%s/claim" % (CAMPAIGNS_PATH, urllib.parse.quote(cid, safe=""))
    cst, cobj = _openapi_json("POST", path, body={}, token=token, headers=headers)
    item = {"id": cid, "status": cst}
    if isinstance(cobj, dict):
        for k in ("message", "code", "success"):
            if k in cobj:
                item[k] = cobj[k]
    rep["claimed"].append(item)
    st2, obj2 = _openapi_json("GET", CAMPAIGNS_PATH, token=token, headers=headers)
    rep["after"] = {"status": st2,
                    "claimable": bool(obj2.get("claimable")) if isinstance(obj2, dict) else None}
    ok = cst in (200, 201, 204)
    if not ok:
        rep["error"] = item.get("message") or "claim HTTP %s" % cst
    return ok, rep


# ── 账号池 ────────────────────────────────────────────────────────────────────
_pool = {"accounts": [], "mtime": 0.0}
_pool_lock = threading.RLock()
_rr = 0
_last_sweep = [0.0]


def _label_of(a):
    uid_part = "uid-" + (a.get("uid") or "")[:8]
    name = a.get("displayName") or ""
    base = a.get("account") or uid_part
    if name and base == uid_part:
        return "%s (%s)" % (name, uid_part)
    return base


def _load_pool_locked():
    """文件被外部改动（手工编辑/另一进程 capture）时热加载。"""
    try:
        mtime = os.path.getmtime(ACCOUNTS_FILE)
    except Exception:
        _pool["accounts"] = []
        _pool["mtime"] = 0.0
        return _pool["accounts"]
    if mtime != _pool["mtime"]:
        data = _read_json_file(ACCOUNTS_FILE) or {}
        accs = data.get("accounts") if isinstance(data, dict) else data
        _pool["accounts"] = [a for a in (accs or []) if isinstance(a, dict) and a.get("token")]
        _pool["mtime"] = mtime
    return _pool["accounts"]


def _save_pool_locked():
    payload = {"version": 1, "accounts": _pool["accounts"]}
    tmp = ACCOUNTS_FILE + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(ACCOUNTS_FILE)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)
    try:
        _pool["mtime"] = os.path.getmtime(ACCOUNTS_FILE)
    except Exception:
        pass


def _add_session_to_pool(acct):
    """入池；同标签视为同一账号，覆盖旧 token（续期后的写回也走这里）。"""
    with _pool_lock:
        accs = _load_pool_locked()
        label = _label_of(acct)
        for i, old in enumerate(accs):
            if _label_of(old) == label:
                merged = dict(old)
                merged.update(acct)
                merged["account"] = label
                merged.setdefault("session_seed", old.get("session_seed") or label)
                accs[i] = merged
                break
        else:
            acct = dict(acct)
            acct["account"] = label
            acct.setdefault("session_seed", label)
            accs.append(acct)
        _save_pool_locked()
        return label


def _update_account(label, patch):
    with _pool_lock:
        accs = _load_pool_locked()
        hit = False
        for i, a in enumerate(accs):
            if _label_of(a) == label:
                merged = dict(a)
                merged.update(patch)
                accs[i] = merged
                _pool["accounts"][i] = merged
                hit = True
                break
        if hit:
            _save_pool_locked()
        return hit


def _mark_account_failed(acct, status):
    """401/403：把该账号临时判死，换下一个号；冷却期内不再拿它发请求，也不反复续期。"""
    now = time.time()
    _update_account(_label_of(acct), {"exp": now - 1, "failed_at": now, "failed_status": status})


def _capture_current():
    """解密本机客户端登录态 → 池内账号 dict（不落任何明文密钥到日志）。"""
    key = _get_master_key()
    with open(AUTH_BLOB, "rb") as f:
        blob = f.read()
    data = json.loads(_aes_unprotect(blob, key).decode("utf-8"))
    user = data.get("user") or {}
    acct = {
        "token": data.get("token") or "",
        "refreshToken": data.get("refreshToken") or "",
        "expiresAt": data.get("expiresAt") or "",
        "refreshTokenExpiresAt": data.get("refreshTokenExpiresAt") or "",
        "uid": user.get("id") or "",
        "displayName": (user.get("displayName") or user.get("name")
                        or user.get("nickName") or ""),
        "exp": _parse_exp(data.get("expiresAt")),
        "organizationId": "",
        "organizationTags": [],
        "dataPolicyAgreed": bool(user.get("dataPolicyAgreed")),
        "cosyVersion": os.environ.get("QODER_COSY_VERSION", "1.1.69"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not acct["token"]:
        raise RuntimeError("auth.v1.dat 里没有 token，客户端可能未登录")
    acct.update(_machine_identity())
    if not acct.get("machineId"):
        raise RuntimeError("找不到 machine_id（%s），签名需要它" % MACHINE_ID_FILE)
    acct = _enrich_account(acct)
    if not acct.get("uid"):
        acct["uid"] = user.get("id") or ""
    acct["account"] = "uid-" + (acct.get("uid") or "")[:8]
    acct["session_seed"] = acct["account"]
    return acct


# ── 设备码登录（--login：浏览器授权换 token，不需要装客户端）──────────────────
class LoginError(Exception):
    pass


def _device_login_argv(machine, site=None, client_id=None):
    """组 sidecar 的一次性 --login 命令行。

    没有客户端登录态时 machine 参数直接省略（sidecar 自己生成 uuid），这样
    全新机器 / 纯 Linux 容器也能 --login。
    """
    argv = [NODE, SIGN_JS, "--login", "--site", site or SITE, "--openapi", OPENAPI]
    if machine.get("machineId"):
        argv += ["--machine-id", machine["machineId"]]
    if machine.get("machineToken"):
        argv += ["--machine-token", machine["machineToken"]]
    if client_id:
        argv += ["--client-id", client_id]
    return argv


def _account_from_device_token(dev, machine=None):
    """设备码轮询回来的 JSON → 池内账号 dict。

    上游给的是 snake_case，客户端自己归一化成 camelCase，两种都要吃下；
    只存显示名和 id，不存邮箱等 PII。
    """
    if not isinstance(dev, dict):
        return None
    token = dev.get("token") or dev.get("device_token") or ""
    if not token:
        return None
    profile = dev.get("profile") if isinstance(dev.get("profile"), dict) else {}
    expires = dev.get("expires_at") or dev.get("expiresAt") or ""
    exp = _parse_exp(expires)
    if exp is None:
        exp = _parse_exp(dev.get("expireTimeS"))
    if exp is None and dev.get("expires_in") is not None:
        try:
            exp = time.time() + float(dev["expires_in"])
        except Exception:
            exp = None
    acct = {
        "token": token,
        "refreshToken": (dev.get("refresh_token") or dev.get("device_refresh_token")
                         or dev.get("refreshToken") or ""),
        "expiresAt": expires,
        "refreshTokenExpiresAt": (dev.get("refresh_token_expires_at")
                                  or dev.get("refreshTokenExpiresAt")
                                  or dev.get("refreshTokenExpireTimeS") or ""),
        "uid": dev.get("user_id") or dev.get("uid") or profile.get("uid") or "",
        "displayName": (profile.get("displayName") or profile.get("name")
                        or profile.get("nickName") or ""),
        "exp": exp,
        "organizationId": "",
        "organizationTags": [],
        "dataPolicyAgreed": False,
        "cosyVersion": os.environ.get("QODER_COSY_VERSION", "1.1.69"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "login": "device",
    }
    for k in ("machineId", "machineToken", "machineType", "machineCode"):
        acct[k] = (machine or {}).get(k) or ""
    if not acct["machineId"] and dev.get("machineIdUsed"):
        acct["machineId"] = dev["machineIdUsed"]
    return acct


def _consume_login_lines(lines, on_auth_url=None, deadline=None):
    """转 sidecar --login 的 JSON-lines：auth_url 一到就回调（要用户去点），拿到 token 返回。"""
    for raw in lines:
        if deadline and time.time() > deadline:
            raise LoginError("等待授权超时")
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("ok") is False or obj.get("event") == "error":
            raise LoginError(str(obj.get("error") or "签名器登录失败"))
        if obj.get("event") == "auth_url":
            url = obj.get("url") or ""
            if url and on_auth_url:
                on_auth_url(url)
        elif obj.get("event") == "token":
            return obj.get("data") or {}
    raise LoginError("签名器没有返回 token（未授权、超时或 sidecar 已退出）")


def _cmd_login(_args=None, popen=None, open_url=None, new_device=False):
    """设备码登录：打印/打开授权链接 → sidecar 轮询 → token 入池。
    new_device=True 时强制生成新设备 ID（不用本机的），用于多账号隔离。"""
    site = getattr(_args, "site", None) or SITE
    client_id = getattr(_args, "client_id", None)
    machine = {} if new_device else _machine_identity()
    spawn = popen or (lambda argv, **kw: subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, cwd=HERE))
    opener = open_url or webbrowser.open
    try:
        proc = spawn(_device_login_argv(machine, site=site, client_id=client_id))
    except Exception as e:
        _print_err("启动签名器失败: %s" % e)
        _print_err("--login 需要 node 和 qoder_sign.mjs"
                   "（可用 QODER_PROXY_NODE / QODER_SIGN_JS 指定路径）")
        return 1

    def _show(url):
        print("请在浏览器里完成授权（选择账号并确认，5 分钟内有效）:")
        print("  %s" % url)
        try:
            opened = bool(opener(url))
        except Exception as e:
            _log("打开浏览器失败: %s" % e)
            opened = False
        print("  %s" % ("已尝试打开浏览器，等待授权…" if opened
                        else "浏览器没有自动打开，请手动复制上面的链接"))
        sys.stdout.flush()

    try:
        dev = _consume_login_lines(proc.stdout, on_auth_url=_show,
                                   deadline=time.time() + LOGIN_TIMEOUT_SEC)
    except LoginError as e:
        _print_err("登录失败: %s" % e)
        return 1
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    acct = _account_from_device_token(dev, machine)
    if not acct:
        _print_err("授权结果里没有 token，请重新 --login")
        return 1
    try:
        acct = _enrich_account(acct)       # 补 uid/组织标签（签名要用）；拿不到不影响登录
    except Exception as e:
        _log("补全账号信息失败: %s" % e)
        _print_err("提示：没取到组织信息，如果上游报 403 请重新 --login")
    label = _add_session_to_pool(acct)
    print("已入池: %s" % label)
    print("  token      %s" % _redact(acct["token"]))
    print("  有效期至   %s" % (acct.get("expiresAt") or "unknown"))
    print("  可续期     %s" % ("是" if acct.get("refreshToken") else "否"))
    print("  机器 ID    %s" % (acct.get("machineId") or "-"))
    print("  凭证文件   %s" % ACCOUNTS_FILE)
    if not acct.get("machineToken"):
        _print_err("提示：本机没有客户端的 machine_token，自动续期可能被拒；"
                   "临期重新 --login 即可")
    return 0


def _pool_expired(a, now):
    exp = a.get("exp")
    if not exp:
        return False
    if exp >= now:
        return False
    failed_at = a.get("failed_at")
    return bool(failed_at) and (now - failed_at) < FAIL_COOLDOWN_SEC


def _next_account():
    """轮询取一个可用账号；全不可用时退回第一个（让上游告诉我们到底行不行）。"""
    global _rr
    with _pool_lock:
        accs = list(_load_pool_locked())
    if not accs:
        return None
    now = time.time()
    healthy = [a for a in accs
               if not _pool_expired(a, now)
               and not (a.get("failed_at") and now - a["failed_at"] < FAIL_COOLDOWN_SEC)]
    cand = healthy or accs
    _rr = (_rr + 1) % len(cand)
    return cand[_rr]


# ── token 续期 ────────────────────────────────────────────────────────────────
def _refresh_account(acct, force=False):
    """POST /api/v1/deviceToken/refresh（无签名）。成功写回池；401/403 判定续期链已死。"""
    rt = acct.get("refreshToken")
    if not rt:
        return acct
    now = time.time()
    if not force and acct.get("refresh_dead"):
        return acct
    if not force and acct.get("exp") and acct["exp"] - now > REFRESH_AHEAD_SEC:
        return acct
    body = {"refresh_token": rt}
    if acct.get("machineId"):
        body["machine_id"] = acct["machineId"]
    if acct.get("machineToken"):
        body["machine_token"] = acct["machineToken"]
    st, js = _openapi_json("POST", REFRESH_PATH, body)
    if st == 200 and isinstance(js, dict) and (js.get("device_token") or js.get("token")):
        patch = {
            "token": js.get("device_token") or js.get("token"),
            "refreshToken": js.get("refresh_token") or acct.get("refreshToken"),
            "expiresAt": js.get("expires_at") or acct.get("expiresAt"),
            "refreshTokenExpiresAt": js.get("refresh_token_expires_at")
            or acct.get("refreshTokenExpiresAt"),
            "refresh_dead": False,
            "failed_at": 0,
        }
        patch["exp"] = _parse_exp(patch["expiresAt"]) or (now + 30 * 86400)
        _update_account(_label_of(acct), patch)
        merged = dict(acct)
        merged.update(patch)
        _log("续期成功 %s exp=%s" % (_label_of(acct), patch["expiresAt"]))
        return merged
    if st in (400, 401, 403):
        _update_account(_label_of(acct), {"refresh_dead": True,
                                          "refresh_error": "%s %s" % (st, js)[:200]})
        merged = dict(acct)
        merged["refresh_dead"] = True
        _log("续期被拒 %s -> %s，标记 refresh_dead" % (_label_of(acct), st))
        return merged
    _log("续期失败 %s -> %s %s" % (_label_of(acct), st, str(js)[:120]))
    return acct


def _sweep_due():
    """临期账号集中续期（每 SWEEP_SEC 秒最多一次），健康账号只做时间比较不花开销。"""
    now = time.time()
    if now - _last_sweep[0] < SWEEP_SEC:
        return
    _last_sweep[0] = now
    with _pool_lock:
        accs = list(_load_pool_locked())
    for a in accs:
        if a.get("refresh_dead") or not a.get("refreshToken"):
            continue
        if a.get("failed_at") and now - a["failed_at"] < FAIL_COOLDOWN_SEC:
            continue
        if a.get("exp") and a["exp"] - now < REFRESH_AHEAD_SEC:
            _refresh_account(a)


def _read_session():
    """取一个可发请求的账号：轮询 + 临期自动续期 + 过期账号按需续一次。"""
    _sweep_due()
    acct = _next_account()
    if acct is None:
        return None
    now = time.time()
    if acct.get("exp") and acct["exp"] - now < REFRESH_AHEAD_SEC:
        acct = _refresh_account(acct)
    return acct


# ── 签名 sidecar（qoder_sign.mjs）────────────────────────────────────────────
class SignerError(Exception):
    pass


class _Signer:
    """长驻 node 子进程，JSON-lines RPC：{id,cmd,auth,endpoint,...} → {id,ok,data}。"""

    def __init__(self):
        self._proc = None
        self._lock = threading.RLock()
        self._lines = queue.Queue()
        self._seq = 0
        self.last_info = None
        self._pump = None

    # 进程管理
    def _spawn(self):
        if self._proc and self._proc.poll() is None:
            return
        if not os.path.exists(SIGN_JS):
            raise SignerError("找不到签名器 %s" % SIGN_JS)
        node = NODE
        if not shutil.which(node):
            raise SignerError("找不到 node（%s）。Qoder CN 的签名在客户端 WASM 里，"
                              "需要 Node 运行 qoder_sign.mjs" % node)
        self._proc = subprocess.Popen(
            [node, SIGN_JS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, encoding="utf-8", cwd=HERE)
        self._pump = threading.Thread(target=self._read_loop, daemon=True)
        self._pump.start()

    def _read_loop(self):
        try:
            for line in self._proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        self._lines.put(None)   # EOF 哨兵

    def _stop(self):
        p = self._proc
        self._proc = None
        if not p:
            return
        try:
            if p.poll() is None:
                p.stdin.write(json.dumps({"id": 0, "cmd": "exit"}) + "\n")
                p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # RPC
    def _rpc_nolock(self, cmd, params, timeout):
        self._spawn()
        self._seq += 1
        rid = self._seq
        msg = {"id": rid, "cmd": cmd}
        msg.update(params or {})
        try:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            self._stop()
            raise SignerError("sidecar 写入失败: %s" % e)
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self._stop()
                raise SignerError("sidecar 超时（%ss）" % timeout)
            try:
                line = self._lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if line is None:
                self._stop()
                raise SignerError("sidecar 已退出")
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("event") == "ready":
                continue
            if obj.get("id") == rid:
                if not obj.get("ok"):
                    raise SignerError(obj.get("error") or "sidecar 返回失败")
                return obj.get("data") or {}

    def rpc(self, cmd, params, timeout=60):
        with self._lock:
            try:
                return self._rpc_nolock(cmd, params, timeout)
            except SignerError:
                self._stop()          # 可能是连接被上游/客户端更新打断：重启再试一次
                return self._rpc_nolock(cmd, params, timeout)

    def info(self):
        try:
            self.last_info = self.rpc("ping", {}, 60)
        except SignerError as e:
            self.last_info = {"started": False, "error": str(e)}
        return self.last_info

    def peek(self):
        """不启动子进程就能拿到的状态（/health 用它保证秒回）。"""
        return self.last_info or {"started": False, "error": "签名器尚未启动"}

    def request(self, acct, params, timeout=None):
        return self.rpc("request", {"auth": _auth_of(acct), **params},
                        timeout or TIMEOUT_SEC)


def _auth_of(acct):
    return {
        "token": acct.get("token"),
        "uid": acct.get("uid"),
        "machineId": acct.get("machineId"),
        "cosyVersion": acct.get("cosyVersion") or "1.1.69",
        "organizationId": acct.get("organizationId") or "",
        "organizationTags": acct.get("organizationTags") or [],
        "dataPolicyAgreed": bool(acct.get("dataPolicyAgreed")),
    }


class _NativeSigner:
    """纯 Python 签名器：qoder_cosy.py 复刻的 WASM 算法，不需要 node。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self.last_info = None

    @staticmethod
    def _key(auth):
        return (auth.get("uid") or "", auth.get("token") or "",
                auth.get("machineId") or "",
                auth.get("cosyVersion") or _cosy.COSY_VERSION,
                auth.get("organizationId") or "",
                tuple(auth.get("organizationTags") or []),
                bool(auth.get("dataPolicyAgreed")))

    def _ctx(self, auth):
        key = self._key(auth)
        with self._lock:
            ctx = self._cache.get(key)
            if ctx is None:
                ctx = _cosy.CosyContext.from_account(auth)
                self._cache = {key: ctx}   # 只留当前账号，池子轮换时不涨内存
            return ctx

    @staticmethod
    def _build(ctx, params):
        body = params.get("body")
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        endpoint = params.get("endpoint") or GATEWAY
        if params.get("infer"):
            if body is None:
                raise SignerError("infer 请求需要 body")
            r = ctx.prepare_infer_request(endpoint, body,
                                          params["infer"].get("model"),
                                          params["infer"].get("source") or "system")
        else:
            extra = params.get("extraHeaders")
            if isinstance(extra, str):
                extra = json.loads(extra)
            r = ctx.prepare_request(endpoint, params.get("path") or "/",
                                    (params.get("method") or "GET").upper(),
                                    params.get("authType") or "auth", body, extra)
        out = {"url": r["url"], "headers": r["headers"]}
        if "body" in r:
            out["body"] = r["body"]
        return out

    def request(self, acct, params, timeout=None):
        return self._build(self._ctx(_auth_of(acct)), params)

    def peek(self):
        self.last_info = {"started": True, "backend": "python", "node": None,
                          "cosyVersion": _cosy.COSY_VERSION, "wasmReady": True}
        return self.last_info

    def info(self):
        return self.peek()

    @staticmethod
    def describe():
        return "纯 Python（qoder_cosy.py，不需要 node）"


class _AutoSigner:
    """auto：纯 Python 优先，签名异常再回落 node sidecar。"""

    def __init__(self, native=None, node=None, mode=None):
        self.mode = mode or SIGN_BACKEND
        self._native = native if native is not None else _NativeSigner()
        self._node = node if node is not None else _Signer()
        self.last_info = None

    def request(self, acct, params, timeout=None):
        if self.mode == "py":
            return self._native.request(acct, params, timeout=timeout)
        if self.mode == "node":
            return self._node.request(acct, params, timeout=timeout)
        try:
            return self._native.request(acct, params, timeout=timeout)
        except Exception as py_err:
            try:
                out = self._node.request(acct, params, timeout=timeout)
            except Exception as node_err:
                raise SignerError("纯 Python 签名失败(%s)；node 回落也失败(%s)"
                                  % (py_err, node_err))
            self.last_info = {"started": True, "backend": "node",
                              "fallback_from": str(py_err)}
            return out

    def peek(self):
        if self.mode == "node":
            return self._node.peek()
        info = dict(self._native.peek())
        if self.mode == "auto":
            info["fallback"] = "node"
            if self.last_info and self.last_info.get("backend") == "node":
                info.update(self.last_info)
        return info

    def info(self):
        if self.mode == "node":
            return self._node.info()
        return self.peek()

    def describe(self):
        if self.mode == "node":
            return "qoder_sign.mjs（node 子进程）"
        if self.mode == "py":
            return self._native.describe()
        return "纯 Python 优先（qoder_cosy.py；node 仅回落）"


def _make_signer(mode=None):
    mode = (mode or SIGN_BACKEND)
    if mode == "py":
        return _NativeSigner()
    if mode == "node":
        return _Signer()
    return _AutoSigner()


_signer = _make_signer()


# ── 上游 HTTP（按 host 维护 keep-alive 连接池）────────────────────────────────
_conn_pools = {}
_conn_pool_lock = threading.Lock()


def _split_url(url):
    u = urllib.parse.urlsplit(url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return u.scheme or "https", u.netloc, path


def _borrow_conn(scheme, host, timeout):
    with _conn_pool_lock:
        pool = _conn_pools.get((scheme, host)) or []
        if pool:
            return pool.pop()
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, timeout=timeout)


def _send(scheme, host, method, path, headers, body, timeout):
    """发一次上游请求。resp.close() 时才决定连接回不回池：
    只有响应完整读完且不是 Connection: close 才复用，流式中途断开一律丢弃。"""
    conn = _borrow_conn(scheme, host, timeout)
    send_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
    send_headers.setdefault("Accept-Encoding", "identity")
    if body is not None:
        send_headers.setdefault("Content-Length", str(len(body)))

    def _new():
        try:
            conn.close()
        except Exception:
            pass
        return _borrow_conn(scheme, host, timeout)

    try:
        try:
            conn.request(method, path, body=body, headers=send_headers)
            resp = conn.getresponse()
        except Exception:
            conn = _new()
            conn.request(method, path, body=body, headers=send_headers)
            resp = conn.getresponse()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise

    keep_alive = (resp.headers.get("Connection") or "").lower() != "close"
    real_close = resp.close

    def _close():
        try:
            fully = resp.isclosed() or (resp.length is not None and resp.length <= 0)
        except Exception:
            fully = False
        try:
            real_close()
        except Exception:
            pass
        if keep_alive and fully:
            with _conn_pool_lock:
                bucket = _conn_pools.setdefault((scheme, host), [])
                if len(bucket) < 4:
                    bucket.append(conn)
            return
        try:
            conn.close()
        except Exception:
            pass

    resp.close = _close
    return resp


def _signed_send(acct, params, timeout=None):
    """签名 → 发送。返回 (status, resp)；签名失败抛 SignerError。"""
    signed = _signer.request(acct, params, timeout=timeout)
    scheme, host, path = _split_url(signed["url"])
    body = signed.get("body")
    data = body.encode("utf-8") if isinstance(body, str) else body
    resp = _send(scheme, host, params.get("method") or ("POST" if data else "GET"),
                 path, signed.get("headers") or {}, data, timeout or TIMEOUT_SEC)
    return resp.status, resp


# ── 模型清单 ──────────────────────────────────────────────────────────────────
_models = {"at": 0.0, "list": [], "by_id": {}, "alias": {}}


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def _max_ctx(cfg):
    best = 0
    if isinstance(cfg, dict):
        for v in cfg.values():
            if isinstance(v, dict) and isinstance(v.get("token_count"), (int, float)):
                best = max(best, int(v["token_count"]))
    return best


def _normalize_models(raw):
    out = []
    for scene in (SCENE, "developer", "assistant"):
        lst = (raw or {}).get(scene)
        if not isinstance(lst, list):
            continue
        for m in lst:
            if not isinstance(m, dict) or not m.get("key"):
                continue
            if any(x["key"] == m["key"] for x in out):
                continue
            ctx = max(int(m.get("max_input_tokens") or 0), _max_ctx(m.get("context_config")))
            out.append({
                "key": m["key"],
                "source": m.get("source") or "system",
                "name": m.get("display_name") or m["key"],
                "reasoning": bool(m.get("is_reasoning")),
                "vision": bool(m.get("is_vl")),
                "default": bool(m.get("is_default")),
                "enabled": bool(m.get("enable", True)),
                "ctx": ctx,
                "price_factor": m.get("price_factor"),
                "format": m.get("format") or "openai",
                "scene": scene,
                "thinking_config": m.get("thinking_config")
                if isinstance(m.get("thinking_config"), dict) else None,
            })
    return out


def _fetch_models(force=False):
    """签名拉 /algo/api/v2/model/list，缓存 MODELS_TTL 秒。"""
    now = time.time()
    if not force and _models["list"] and now - _models["at"] < MODELS_TTL:
        return _models
    acct = _read_session()
    if acct is None:
        raise RuntimeError("账号池为空：先运行 python qoder_proxy.py --capture")
    params = {"endpoint": GATEWAY, "path": MODEL_LIST_PATH, "method": "GET"}
    _log("拉取模型清单")
    status, resp = _signed_send(acct, params)
    try:
        raw = resp.read().decode("utf-8", "replace")
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if status != 200:
        raise RuntimeError("模型清单 HTTP %s: %s" % (status, raw[:200]))
    lst = _normalize_models(json.loads(raw))
    _models["at"] = now
    _models["list"] = lst
    _models["by_id"] = {m["key"]: m for m in lst}
    alias = {}
    for m in lst:
        alias[m["key"].lower()] = m
        alias[_slug(m["name"])] = m
    _models["alias"] = alias
    _log("模型清单 %d 个" % len(lst))
    return _models


def _resolve_model(name):
    """模型名/别名 → 上游 model_config；清单不可用时原样透传，未知时返回 None。"""
    raw = str(name or "").strip()
    low = raw.lower()
    if not low or low in ("default", "auto"):
        lst = _models["list"]
        if lst:
            return next((m for m in lst if m["default"]), lst[0])
        return {"key": DEFAULT_MODEL, "source": "system", "name": DEFAULT_MODEL}
    if _models["alias"]:
        return _models["by_id"].get(raw) or _models["alias"].get(low) \
            or _models["alias"].get(_slug(raw))
    return {"key": raw, "source": "system", "name": raw}


# ── 上游聊天请求体 ───────────────────────────────────────────────────────────
def _session_id(acct):
    """同一账号稳定复用同一 session_id（上游按它做上下文与计费聚合）。"""
    seed = "%s:%s" % (_label_of(acct), acct.get("session_seed") or "")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "qoder-proxy:" + seed))


# 思考深度：与客户端一致的档位表与预算换算（bundle: Ujc 表 / s_() 分段）。
EFFORT_BUDGETS = {"none": 0, "low": 1024, "medium": 8192, "high": 24576,
                  "xhigh": 49152, "max": 65536}
_EFFORT_ALIASES = {"disabled": "none", "off": "none", "minimal": "low"}


def _effort_of_budget(n):
    """预算 token → 档位（客户端 s_() 的分段）。"""
    if n <= 0:
        return "none"
    if n <= 1024:
        return "low"
    if n <= 8192:
        return "medium"
    if n <= 24576:
        return "high"
    if n <= 49152:
        return "xhigh"
    return "max"


def _norm_effort(v):
    """档位字符串/数值 → 规范档位；无法识别的值返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _effort_of_budget(v)
    s = str(v).strip().lower()
    s = _EFFORT_ALIASES.get(s, s)
    return s if s in EFFORT_BUDGETS else None


def _thinking_inputs(body):
    """三协议入参 → (effort, budget, enable, preserve)，未提及项为 None。

    覆盖 OpenAI 直发（reasoning_effort / thinking_budget / enable_thinking）、
    Responses（reasoning.effort）、Anthropic 及兼容层（thinking.type 与 budget）。
    """
    effort = _norm_effort(body.get("reasoning_effort"))
    budget = body.get("reasoning_budget_tokens")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool):
        budget = body.get("thinking_budget")
    budget = budget if isinstance(budget, (int, float)) and not isinstance(budget, bool) else None
    enable = body.get("enable_thinking") if isinstance(body.get("enable_thinking"), bool) else None
    preserve = body.get("preserve_thinking") \
        if isinstance(body.get("preserve_thinking"), bool) else None
    r = body.get("reasoning")
    if isinstance(r, dict):
        if effort is None:
            effort = _norm_effort(r.get("effort"))
        if enable is None and isinstance(r.get("enabled"), bool):
            enable = r["enabled"]
        rb = r.get("budgetTokens", r.get("budget_tokens"))
        if budget is None and isinstance(rb, (int, float)) and not isinstance(rb, bool):
            budget = rb
    t = body.get("thinking")
    if isinstance(t, dict):
        typ = str(t.get("type") or "").strip().lower()
        if enable is None and typ in ("enabled", "disabled", "off"):
            enable = typ == "enabled"
        tb = t.get("budget_tokens", t.get("budgetTokens"))
        if budget is None and isinstance(tb, (int, float)) and not isinstance(tb, bool):
            budget = tb
    if effort is None and budget is not None:
        effort = _effort_of_budget(budget)
    if effort is None and enable is False:
        effort = "none"
    return effort, budget, enable, preserve


def _allowed_efforts(model):
    """模型声明的档位：thinking_config.enabled.efforts + disabled → none。

    返回 None 表示清单没给信息（不校验，原样透传）。
    """
    tc = (model or {}).get("thinking_config")
    if not isinstance(tc, dict):
        return None
    allowed = []
    en = tc.get("enabled")
    effs = en.get("efforts") if isinstance(en, dict) else None
    if isinstance(effs, dict):
        for k in effs:
            e = _norm_effort(k)
            if e and e != "none" and e not in allowed:
                allowed.append(e)
    d = tc.get("disabled")
    if "disabled" in tc and d is not None and d is not False and "none" not in allowed:
        allowed.append("none")
    allowed.sort(key=lambda e: EFFORT_BUDGETS[e])
    return allowed


def _clamp_effort(effort, allowed):
    """按预算距离取最接近的声明档位（同距取小）；无可用档位返回 None。"""
    cands = [a for a in allowed if a != "none"] if effort != "none" else list(allowed)
    if not cands:
        return None
    return min(cands, key=lambda a: (abs(EFFORT_BUDGETS[a] - EFFORT_BUDGETS[effort]),
                                     EFFORT_BUDGETS[a]))


def _thinking_upstream(oa, model):
    """思考深度入参 → 上游 (reasoningEffort, parameters 增量)。

    档位不在模型声明的集合内时按预算距离就近调整（与客户端档位表一致）；
    预算只在显式给出正数且与最终档位同一来源时上送，关思考时清掉。
    """
    effort, budget, enable, preserve = _thinking_inputs(oa)
    if effort is None and budget is None and enable is None and preserve is None:
        return None, None
    allowed = _allowed_efforts(model)
    if effort is not None and allowed is not None and effort not in allowed:
        clamped = _clamp_effort(effort, allowed)
        _log("思考档位 %s 不在模型 %s 声明的 %s 内，调整为 %s"
             % (effort, model.get("key"), allowed, clamped))
        if clamped is None:
            return None, None
        effort, budget = clamped, None
    params = {}
    if effort is not None:
        params["reasoning_effort"] = effort
        if effort == "none":
            enable, budget = False, None
        else:
            enable = True
    if enable is not None:
        params["enable_thinking"] = enable
    if budget is not None and budget > 0:
        params["reasoning_budget_tokens"] = int(budget)
    if preserve is not None:
        params["preserve_thinking"] = preserve
    return effort, params


def _build_infer_payload(oa, model, acct):
    mid = str(uuid.uuid4())
    payload = {
        "session_id": _session_id(acct),
        "request_id": mid,
        "request_set_id": mid,
        "source_session_id": _session_id(acct),
        "scene": SCENE,
        "messages": oa.get("messages") or [],
        "model_config": {"key": model["key"], "source": model.get("source") or "system"},
        "business": {"type": BUSINESS_TYPE, "product": BUSINESS_PRODUCT},
        "stream": True,
    }
    for k in ("tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop",
              "presence_penalty", "frequency_penalty", "seed", "response_format"):
        if oa.get(k) is not None:
            payload[k] = oa[k]
    think_effort, think_params = _thinking_upstream(oa, model)
    if think_effort:
        payload["reasoningEffort"] = think_effort
    if think_params:
        base = oa.get("parameters")
        merged = dict(base) if isinstance(base, dict) else {}
        merged.update(think_params)
        payload["parameters"] = merged
    if payload["model_config"].get("key") in (None, "auto", ""):
        payload["model_config"]["key"] = DEFAULT_MODEL
    return payload


def _unwrap_frame(data_text):
    """上游信封帧 data:{"headers":..,"body":"<chunk JSON>","statusCodeValue":200} → 内层。

    同时兼容没有信封的标准 OpenAI SSE（直接原样返回）。
    """
    if data_text is None:
        return None
    s = data_text.strip()
    if s == "[DONE]":
        return "[DONE]"
    try:
        obj = json.loads(s)
    except Exception:
        return s
    if not isinstance(obj, dict):
        return s
    if "body" in obj and ("statusCodeValue" in obj or "headers" in obj):
        code = obj.get("statusCodeValue")
        if isinstance(code, int) and code >= 400:
            return json.dumps({"error": {"message": str(obj.get("body") or "")[:400],
                                         "type": "upstream_error", "code": code}},
                              ensure_ascii=False)
        inner = obj.get("body")
        if isinstance(inner, str):
            return inner.strip() or None
        return json.dumps(inner, ensure_ascii=False)
    return s


def _iter_upstream(resp):
    """把上游 SSE 逐帧解成内层 JSON 字符串；[DONE] 结束。

    必须自己缓冲跨 read() 边界的半行：上游一帧是一条几百~几千字节的 JSON，
    按固定块读再 split("\\n") 会把半截行当成 data: 前缀、丢掉后半段（表现为回复掉字、
    usage 变 0）；而且按块 decode 会把多字节字符切成乱码。所以缓冲按 **字节** 留存，
    只在遇到 b"\\n" 时解一行。
    """
    buf = b""
    data_parts = []
    while True:
        try:
            chunk = resp.read(65536)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                if len(buf) > MAX_LINE_BYTES:
                    return
                break
            line, buf = buf[:nl], buf[nl + 1:]
            line = line.decode("utf-8", "replace").rstrip("\r")
            if line.startswith("data:"):
                data_parts.append(line[5:].strip())
            elif line.strip() == "":
                if not data_parts:
                    continue
                inner = _unwrap_frame("\n".join(data_parts))
                data_parts = []
                if inner == "[DONE]":
                    return
                if inner:
                    yield inner
    tail = buf.decode("utf-8", "replace").strip()
    if tail.startswith("data:"):
        data_parts.append(tail[5:].strip())
    if data_parts:
        inner = _unwrap_frame("\n".join(data_parts))
        if inner and inner != "[DONE]":
            yield inner


def aggregate_sse(frames, model_name):
    """内层 chunk 序列 → 非流式 chat.completion（上游只吐 SSE，stream=false 也得自己拼）。"""
    chunks = []
    for f in frames:
        if isinstance(f, (str, bytes)):
            try:
                f = json.loads(f)
            except Exception:
                continue
        if isinstance(f, dict):
            chunks.append(f)
    text, reasoning, usage, finish = [], [], None, None
    tools = {}
    cid, created = "chatcmpl-" + _rand(), int(time.time())
    model = model_name
    for c in chunks:
        cid = c.get("id") or cid
        created = c.get("created") or created
        model = c.get("model") or model
        if c.get("usage"):
            usage = c["usage"]
        for ch in c.get("choices") or []:
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            d = ch.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                slot = tools.setdefault(tc.get("index") or 0,
                                        {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["name"] = _merge_tool_name(slot["name"], fn.get("name"))
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
    message = {"role": "assistant", "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tools:
        message["tool_calls"] = [{
            "id": s["id"] or ("call_" + _rand()), "type": "function",
            "function": {"name": s["name"], "arguments": s["args"] or "{}"},
        } for _, s in sorted(tools.items())]
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _error_of(obj):
    """chunk/响应里的错误 → dict；没有错误返回 None。"""
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        if isinstance(err, str):
            return {"message": err}
        return err or {}
    return None


# ── 协议转换：外部请求 → 内部 OpenAI messages ────────────────────────────────
def _tools_to_openai(tools):
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("function"):
            out.append({"type": "function", "function": t["function"]})
        elif t.get("name"):
            out.append({"type": "function", "function": {
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or t.get("input_schema") or {"type": "object"},
            }})
    return out


def _tool_choice_to_openai(choice, protocol):
    if choice is None:
        return None
    if isinstance(choice, str):
        if protocol == "anthropic":
            return {"any": "required", "auto": "auto", "none": "none"}.get(choice, "auto")
        return choice
    if not isinstance(choice, dict):
        return None
    if choice.get("type") == "function" and choice.get("function"):
        return choice
    if choice.get("type") == "function" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    if choice.get("type") == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return choice


def convert_responses_to_openai(body):
    """Responses API 请求 → OpenAI Chat Completions 请求。"""
    oa = {}
    for src, dst in (("model", "model"), ("max_output_tokens", "max_tokens"),
                     ("stream", "stream"), ("temperature", "temperature"),
                     ("top_p", "top_p"), ("top_logprobs", "top_logprobs"),
                     ("stop", "stop"), ("seed", "seed")):
        if body.get(src) is not None:
            oa[dst] = body[src]
    if oa.get("stream") is None:
        oa["stream"] = False
    msgs = []
    if isinstance(body.get("instructions"), str) and body["instructions"]:
        msgs.append({"role": "system", "content": body["instructions"]})
    inp = body.get("input")
    if isinstance(inp, str) and inp:
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "message":
                role = "assistant" if item.get("role") == "assistant" else "user"
                c = item.get("content")
                text = ""
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    for part in c:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in ("input_text", "output_text", "text"):
                            text += part.get("text") or ""
                        elif part.get("type") in ("input_image", "image", "image_url"):
                            text += "[图片输入暂不支持]"
                if text:
                    msgs.append({"role": role, "content": text})
            elif t == "function_call":
                tc = {"id": item.get("call_id", ""), "type": "function",
                      "function": {"name": item.get("name", ""),
                                   "arguments": item.get("arguments") or "{}"}}
                if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls") is None:
                    msgs[-1]["tool_calls"] = [tc]
                else:
                    msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
            elif t == "function_call_output":
                o = item.get("output")
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                             "content": o if isinstance(o, str)
                             else json.dumps(o, ensure_ascii=False)})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    tools = _tools_to_openai(body.get("tools"))
    if tools:
        oa["tools"] = tools
    tc = _tool_choice_to_openai(body.get("tool_choice"), "responses")
    if tc is not None:
        oa["tool_choice"] = tc
    if isinstance(body.get("reasoning"), dict):
        oa["reasoning"] = body["reasoning"]
    return oa


def convert_anthropic_to_openai(body):
    """Anthropic Messages 请求 → OpenAI Chat Completions 请求。"""
    oa = {}
    for src, dst in (("model", "model"), ("max_tokens", "max_tokens"), ("stream", "stream"),
                     ("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")):
        if body.get(src) is not None:
            oa[dst] = body[src]
    msgs = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        text = "\n".join(p.get("text", "") for p in sys_
                         if isinstance(p, dict) and p.get("type") == "text")
        if text.strip():
            msgs.append({"role": "system", "content": text.strip()})
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role, c = msg.get("role"), msg.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        if not isinstance(c, list):
            continue
        if role == "assistant":
            texts, calls = [], []
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text") or "")
                elif part.get("type") == "tool_use":
                    calls.append({"id": part.get("id", ""), "type": "function",
                                  "function": {"name": part.get("name", ""),
                                               "arguments": json.dumps(part.get("input") or {},
                                                                       ensure_ascii=False)}})
            am = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                am["tool_calls"] = calls
            msgs.append(am)
        else:
            for part in c:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    rc = part.get("content")
                    msgs.append({"role": "tool", "tool_call_id": part.get("tool_use_id", ""),
                                 "content": rc if isinstance(rc, str)
                                 else json.dumps(rc, ensure_ascii=False)})
            texts = [p.get("text", "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            if any(t.strip() for t in texts):
                msgs.append({"role": "user", "content": "\n".join(texts)})
            if any(isinstance(p, dict) and p.get("type") == "image" for p in c):
                msgs.append({"role": "user", "content": "[图片输入暂不支持]"})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    tools = _tools_to_openai(body.get("tools"))
    if tools:
        oa["tools"] = tools
    tc = _tool_choice_to_openai(body.get("tool_choice"), "anthropic")
    if tc is not None:
        oa["tool_choice"] = tc
    if isinstance(body.get("thinking"), dict):
        oa["thinking"] = body["thinking"]
    return oa


def parse_anthropic_text(body):
    """Anthropic 请求里的全部文本（count_tokens 估算用）。"""
    texts = []
    sys_ = body.get("system")
    if isinstance(sys_, str):
        texts.append(sys_)
    elif isinstance(sys_, list):
        for p in sys_:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(p.get("text", ""))
    for msg in body.get("messages") or []:
        c = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        texts.append(p.get("text", ""))
                    elif p.get("type") in ("tool_use", "tool_result"):
                        texts.append(json.dumps(p, ensure_ascii=False))
    return texts


def _finish_to_anthropic(finish):
    return {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(
        finish, finish or "end_turn")


def convert_openai_to_responses(openai_obj, req_model):
    """OpenAI chat.completion → Responses API 响应。"""
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    output = []
    if message.get("reasoning_content"):
        output.append({"type": "reasoning", "id": "rs_" + _rand(),
                       "summary": [{"type": "summary_text", "text": message["reasoning_content"]}]})
    if message.get("content"):
        output.append({"type": "message", "id": "msg_" + _rand(), "role": "assistant",
                       "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"],
                                    "annotations": []}]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({"type": "function_call", "id": "fc_" + _rand(),
                       "call_id": tc.get("id", ""), "name": fn.get("name", ""),
                       "arguments": fn.get("arguments") or "{}", "status": "completed"})
    incomplete = choice.get("finish_reason") == "length"
    text = "".join(seg.get("text", "") for item in output if item.get("type") == "message"
                   for seg in (item.get("content") or []) if seg.get("type") == "output_text")
    return {
        "id": "resp_" + _rand(), "object": "response", "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": req_model, "output": output, "output_text": text,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0),
                  "output_tokens_details": {"reasoning_tokens": (
                      (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))}},
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
    }


def convert_openai_to_anthropic(openai_obj, req_model):
    """OpenAI chat.completion → Anthropic Messages 响应。"""
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    content = []
    if message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id") or ("toolu_" + _rand()),
                        "name": fn.get("name", ""), "input": inp})
    return {
        "id": "msg_" + _rand(), "type": "message", "role": "assistant",
        "model": req_model, "content": content or [{"type": "text", "text": ""}],
        "stop_reason": _finish_to_anthropic(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# ── 流式状态机：内部 chat chunk → Responses / Anthropic SSE ─────────────────
class _ResponsesStreamState:
    """内部 chat chunk → Responses 协议事件序列。"""

    def __init__(self, model: str):
        self.model = model
        self.resp_id = "resp_" + _rand()
        self.created = int(time.time())
        self.started = False
        self.finished = False
        self.finish_reason = None
        self.text = ""
        self.reasoning = ""
        self.tool_name = ""
        self.tool_id = ""
        self.tool_args = ""
        self.usage = None
        self.text_item_id = "msg_" + _rand()
        self.reason_item_id = "rs_" + _rand()
        self.reason_opened = False
        self.text_opened = False
        self.reason_index = None
        self.text_index = None
        self.next_index = 0

    def _head_events(self):
        """response.created + response.in_progress（OpenAI 官方序列的开头两帧）。"""
        resp = {"id": self.resp_id, "object": "response", "created_at": self.created,
                "status": "in_progress", "model": self.model, "output": []}
        return [("response.created", json.dumps({"type": "response.created", "response": resp},
                                                ensure_ascii=False)),
                ("response.in_progress", json.dumps({"type": "response.in_progress", "response": resp},
                                                    ensure_ascii=False))]

    def feed(self, chunk: dict):
        out = []
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}

        if not self.started:
            self.started = True
            out.extend(self._head_events())

        rc = delta.get("reasoning_content")
        if rc:
            self.reasoning += rc
            if not self.reason_opened:
                self.reason_opened = True
                self.reason_index = self.next_index
                self.next_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": self.reason_index,
                    "item": {"type": "reasoning", "id": self.reason_item_id, "summary": []},
                }, ensure_ascii=False)))
                out.append(("response.reasoning_summary_part.added", json.dumps({
                    "type": "response.reasoning_summary_part.added", "item_id": self.reason_item_id,
                    "output_index": self.reason_index, "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                }, ensure_ascii=False)))
            out.append(("response.reasoning_summary_text.delta", json.dumps({
                "type": "response.reasoning_summary_text.delta", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "delta": rc,
            }, ensure_ascii=False)))

        content = delta.get("content")
        if content:
            self.text += content
            if not self.text_opened:
                self.text_opened = True
                self.text_index = self.next_index
                self.next_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": self.text_index,
                    "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                             "status": "in_progress", "content": []},
                }, ensure_ascii=False)))
                out.append(("response.content_part.added", json.dumps({
                    "type": "response.content_part.added", "item_id": self.text_item_id,
                    "output_index": self.text_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }, ensure_ascii=False)))
            out.append(("response.output_text.delta", json.dumps({
                "type": "response.output_text.delta", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "delta": content,
            }, ensure_ascii=False)))

        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if tc.get("id"):
                self.tool_id = tc["id"]
            self.tool_name = _merge_tool_name(self.tool_name, fn.get("name"))
            if fn.get("arguments"):
                self.tool_args += fn["arguments"]
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        if not self.started:
            out.extend(self._head_events())
        if self.reason_opened:
            out.append(("response.reasoning_summary_text.done", json.dumps({
                "type": "response.reasoning_summary_text.done", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "text": self.reasoning,
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": self.reason_index,
                "item": {"type": "reasoning", "id": self.reason_item_id,
                         "summary": [{"type": "summary_text", "text": self.reasoning}]},
            }, ensure_ascii=False)))
        if self.text_opened:
            out.append(("response.output_text.done", json.dumps({
                "type": "response.output_text.done", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "text": self.text,
            }, ensure_ascii=False)))
            out.append(("response.content_part.done", json.dumps({
                "type": "response.content_part.done", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0,
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": self.text_index,
                "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": self.text, "annotations": []}]},
            }, ensure_ascii=False)))
        items = []
        if self.reasoning:
            items.append({"type": "reasoning", "id": self.reason_item_id,
                          "summary": [{"type": "summary_text", "text": self.reasoning}]})
        if self.text:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": self.text, "annotations": []}]})
        if self.tool_name:
            items.append({"type": "function_call", "id": "fc_" + _rand(), "call_id": self.tool_id,
                          "name": self.tool_name, "arguments": self.tool_args, "status": "completed"})
        if not items:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": "", "annotations": []}]})
        usage = self.usage or {}
        out.append(("response.completed", json.dumps({
            "type": "response.completed",
            "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                         "status": "completed" if self.finish_reason != "length" else "incomplete",
                         "model": self.model, "output": items,
                         "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                   "output_tokens": usage.get("completion_tokens", 0),
                                   "total_tokens": usage.get("total_tokens", 0)},
                         "error": None,
                         "incomplete_details": {"reason": "max_output_tokens"}
                         if self.finish_reason == "length" else None},
        }, ensure_ascii=False)))
        return out


class _AnthropicStreamState:
    """内部 chat chunk → Anthropic Messages SSE 事件序列。

    tool_use 的 name/arguments 在上游是分片到达的（如 "get_" + "time"），
    content_block_start 又必须带完整 name，所以工具块先缓冲、finish() 时整块吐出。
    """

    def __init__(self, model: str):
        self.model = model
        self.msg_id = "msg_" + _rand()
        self.started = False
        self.finished = False
        self.thinking_block = False
        self.text_block = False
        self.block_index = 0
        self.text = ""
        self.thinking = ""
        self.tool_blocks = {}      # 上游 index → {index, id, name, args}
        self.usage = None

    def _start(self, out):
        if self.started:
            return
        self.started = True
        out.append(("message_start", json.dumps({
            "type": "message_start",
            "message": {"id": self.msg_id, "type": "message", "role": "assistant", "content": [],
                        "model": self.model, "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        }, ensure_ascii=False)))

    def _stop_block(self, out):
        if self.thinking_block or self.text_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1},
                ensure_ascii=False)))
            self.thinking_block = False
            self.text_block = False

    def feed(self, chunk: dict):
        out = []
        self._start(out)
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        delta = choice.get("delta") or {}
        rc = delta.get("reasoning_content")
        if rc:
            if not self.thinking_block:
                self._stop_block(out)
                self.thinking_block = True
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.block_index - 1,
                    "content_block": {"type": "thinking", "thinking": ""},
                }, ensure_ascii=False)))
            self.thinking += rc
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "thinking_delta", "thinking": rc},
            }, ensure_ascii=False)))
        content = delta.get("content")
        if content:
            if not self.text_block:
                self._stop_block(out)
                self.text_block = True
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.block_index - 1,
                    "content_block": {"type": "text", "text": ""},
                }, ensure_ascii=False)))
            self.text += content
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "text_delta", "text": content},
            }, ensure_ascii=False)))
        for i, tc in enumerate(delta.get("tool_calls") or []):
            fn = tc.get("function") or {}
            key = tc.get("index", i)
            blk = self.tool_blocks.get(key)
            if blk is None:
                blk = {"index": len(self.tool_blocks), "id": tc.get("id") or "",
                       "name": fn.get("name") or "", "args": ""}
                self.tool_blocks[key] = blk
            elif tc.get("id"):
                blk["id"] = tc["id"]
            blk["name"] = _merge_tool_name(blk["name"], fn.get("name"))
            if fn.get("arguments"):
                blk["args"] += fn["arguments"]
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        self._start(out)
        self._stop_block(out)
        for blk in sorted(self.tool_blocks.values()):
            index = self.block_index + 1
            self.block_index = index
            out.append(("content_block_start", json.dumps({
                "type": "content_block_start", "index": index - 1,
                "content_block": {"type": "tool_use",
                                  "id": blk["id"] or ("toolu_" + _rand()),
                                  "name": blk["name"], "input": {}},
            }, ensure_ascii=False)))
            if blk["args"]:
                out.append(("content_block_delta", json.dumps({
                    "type": "content_block_delta", "index": index - 1,
                    "delta": {"type": "input_json_delta", "partial_json": blk["args"]},
                }, ensure_ascii=False)))
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": index - 1}, ensure_ascii=False)))
        usage = self.usage or {}
        stop_reason = "tool_use" if self.tool_blocks else "end_turn"
        out.append(("message_delta", json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)},
        }, ensure_ascii=False)))
        out.append(("message_stop", json.dumps({"type": "message_stop"}, ensure_ascii=False)))
        return out


# ── HTTP Handler ─────────────────────────────────────────────────────────────
class ProxyError(Exception):
    """给客户端的错误（带 HTTP 状态码）。"""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def _map_upstream_error(status, text=""):
    s = str(text or "").lower()
    if status in (401, 403):
        return 401
    if status == 429:
        return 429
    if any(k in s for k in ("sensitive", "content_security", "违规", "audit")):
        return 400
    if status in (400, 404, 409, 422):
        return 400
    return 502


def _open_upstream(oa, model):
    """签名 + 发上游。401/403 换号重试；签名器异常直接 502（换号也没用）。"""
    with _pool_lock:
        n_acc = len(_load_pool_locked())
    last = None
    for _ in range(max(2, min(n_acc, 4))):
        acct = _read_session()
        if acct is None:
            raise ProxyError(401, "账号池为空：先运行 python qoder_proxy.py --capture")
        params = {"endpoint": GATEWAY, "method": "POST", "path": INFER_PATH,
                  "infer": {"model": model["key"], "source": model.get("source") or "system"},
                  "body": _build_infer_payload(oa, model, acct)}
        try:
            status, resp = _signed_send(acct, params)
        except Exception as e:
            raise ProxyError(502, "签名/连接失败: %s" % e)
        if status in (401, 403):
            detail = _drain(resp)
            _mark_account_failed(acct, status)
            _log("账号 %s 上游 %s，换号: %s" % (_label_of(acct), status, detail[:120]))
            last = ProxyError(401, "上游鉴权失败(%s)，请重新 --capture：%s"
                              % (status, detail[:200]))
            continue
        if status >= 400:
            detail = _drain(resp)
            raise ProxyError(_map_upstream_error(status, detail),
                             "上游 %s: %s" % (status, detail[:300]))
        return acct, resp
    raise last or ProxyError(502, "上游请求失败")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "qoder-proxy/1.0"

    def log_message(self, fmt, *args):
        _log("%s - %s" % (self.address_string(), fmt % args))

    # 基础工具
    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, message, err_type="api_error"):
        return self._json(code, {"error": {"message": str(message)[:600], "type": err_type}})

    def _check_api_key(self):
        """没设 QODER_PROXY_API_KEY 时不鉴权；失败时已回复，返回 False。"""
        if not API_KEY:
            return True
        authz = self.headers.get("Authorization") or ""
        xkey = self.headers.get("X-Api-Key") or ""
        ok = (authz.startswith("Bearer ") and secrets.compare_digest(authz[7:], API_KEY)) \
            or (xkey and secrets.compare_digest(xkey, API_KEY))
        if ok:
            return True
        self._err(401, "missing or invalid API key", "invalid_api_key")
        return False

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            _drain_body(self, length)
            self.close_connection = True
            return self._err(413, "请求体过大（%d > %d 字节）" % (length, MAX_BODY_BYTES))
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return self._bad("请求体为空")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._bad("无效 JSON 请求体")
        if not isinstance(obj, dict):
            return self._bad("请求体必须是 JSON 对象")
        return obj

    def _bad(self, message):
        self._err(400, message, "invalid_request_error")
        return None

    # 路由
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._check_api_key():
            return
        try:
            if path == "/health":
                return self._handle_health()
            if path in ("/v1/models", "/models"):
                return self._handle_models()
            return self._err(404, "支持: GET /health, GET /v1/models, "
                                  "POST /v1/chat/completions, /v1/responses, /v1/messages")
        except Exception as e:
            return self._err(500, "内部错误: %s" % e)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._check_api_key():
            return
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._handle_chat("openai")
        if path in ("/v1/responses", "/responses"):
            return self._handle_chat("responses")
        if path == "/v1/messages":
            return self._handle_chat("anthropic")
        return self._err(404, "支持: POST /v1/chat/completions, /v1/responses, "
                              "/v1/messages, /v1/messages/count_tokens")

    # /health 只读本地状态：不启动 node、不打上游，保证秒回
    def _handle_health(self):
        now = time.time()
        with _pool_lock:
            accs = list(_load_pool_locked())
        out = {"ok": bool(accs), "gateway": GATEWAY, "openapi": OPENAPI,
               "scene": SCENE, "port": PORT, "account_count": len(accs),
               "accounts": [{
                   "account": _label_of(a),
                   "expires_at": a.get("expiresAt") or "unknown",
                   "expired": bool(a.get("exp") and a["exp"] < now),
                   "renews_in_sec": int(a["exp"] - now) if a.get("exp") else None,
                   "has_refresh": bool(a.get("refreshToken")),
                   "refresh_dead": bool(a.get("refresh_dead")),
                   "failed_status": a.get("failed_status"),
               } for a in accs]}
        if not accs:
            out["error"] = "账号池为空，请先 --capture 或 --import"
        si = _signer.peek()
        out["signer"] = {"started": bool(si.get("started") or si.get("wasmReady")),
                         "backend": si.get("backend"), "node": si.get("node"),
                         "cosyVersion": si.get("cosyVersion"),
                         "bundle": bool(si.get("bundle")), "error": si.get("error")}
        out["models"] = len(_models["list"])
        return self._json(200, out)

    def _handle_models(self):
        try:
            cached = _fetch_models()
        except ProxyError as e:
            return self._err(e.status, e.message)
        except Exception as e:
            return self._err(502, "模型清单获取失败: %s" % e)
        now = int(_models["at"])
        data = [{"id": m["key"], "object": "model", "created": now, "owned_by": "qoder-cn",
                 "name": m["name"], "context_window": m["ctx"], "reasoning": m["reasoning"],
                 "vision": m["vision"],
                 "metadata": {"display_name": m["name"], "source": m["source"],
                              "format": m["format"], "price_factor": m["price_factor"],
                              "enabled": m["enabled"], "scene": m["scene"],
                              "thinking_config": m["thinking_config"]}}
                for m in cached["list"]]
        return self._json(200, {"object": "list", "data": data})

    def _handle_count_tokens(self):
        body = self._read_json_body()
        if body is None:
            return
        total = sum(len(t) for t in parse_anthropic_text(body))
        return self._json(200, {"input_tokens": max(1, total // 3)})

    # 聊天主流程
    def _handle_chat(self, protocol):
        t0 = time.time()
        body = self._read_json_body()
        if body is None:
            return
        if protocol == "responses":
            oa = convert_responses_to_openai(body)
            if not body.get("input") and not body.get("messages"):
                return self._bad("缺少 input")
        else:
            oa = convert_anthropic_to_openai(body) if protocol == "anthropic" else dict(body)
            if not oa.get("messages"):
                return self._bad("缺少 messages")
        req_model = body.get("model") or DEFAULT_MODEL
        if not _models["list"]:
            try:
                _fetch_models()
            except SignerError as e:
                return self._err(502, "签名器异常: %s" % e)
            except Exception as e:
                _log("模型清单不可用，模型名原样透传: %s" % e)
        try:
            model = _resolve_model(req_model)
        except Exception as e:
            return self._err(502, "模型清单不可用: %s" % e)
        if model is None:
            return self._json(404, {"error": {
                "message": "未知模型 %r，可用: %s" % (req_model, ", ".join(
                    sorted(_models["by_id"]))[:400]),
                "type": "invalid_request_error", "code": "model_not_found"}})
        oa["model"] = model["key"]
        is_stream = bool(oa.get("stream"))
        try:
            _, resp = _open_upstream(oa, model)
        except ProxyError as e:
            return self._err(e.status, e.message)
        if is_stream:
            self._stream(protocol, req_model, model, resp, t0)
        else:
            self._complete(protocol, req_model, model, resp, t0)


    def _complete(self, protocol, req_model, model, resp, t0):
        frames = list(_iter_upstream(resp))
        try:
            resp.close()
        except Exception:
            pass
        objs = []
        for f in frames:
            try:
                o = json.loads(f)
            except Exception:
                continue
            err = _error_of(o)
            if err:
                return self._err(_map_upstream_error(err.get("code"), err.get("message")),
                                 err.get("message") or "上游返回错误")
            objs.append(o)
        merged = aggregate_sse(objs, req_model)
        _log("回合完成 model=%s stream=False frames=%d 耗时=%.2fs"
             % (model["key"], len(frames), time.time() - t0))
        if protocol == "responses":
            return self._json(200, convert_openai_to_responses(merged, req_model))
        if protocol == "anthropic":
            return self._json(200, convert_openai_to_anthropic(merged, req_model))
        return self._json(200, merged)

    def _stream(self, protocol, req_model, model, resp, t0):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        st = (_ResponsesStreamState(req_model) if protocol == "responses"
              else _AnthropicStreamState(req_model) if protocol == "anthropic" else None)

        def write_sse(event, payload):
            text = ("event: %s\n" % event if event else "") + "data: %s\n\n" % payload
            b = text.encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
            self.wfile.flush()

        def write_error(message):
            if protocol == "anthropic":
                write_sse("error", json.dumps({"type": "error", "error": {
                    "type": "api_error", "message": message}}, ensure_ascii=False))
            elif protocol == "responses":
                write_sse("response.failed", json.dumps({
                    "type": "response.failed",
                    "response": {"id": "resp_" + _rand(), "object": "response",
                                 "created_at": int(time.time()), "status": "failed",
                                 "model": req_model, "output": [],
                                 "error": {"code": "upstream_error", "message": message}}},
                    ensure_ascii=False))
            else:
                write_sse("", json.dumps({"error": {"message": message, "type": "api_error",
                                                    "code": "upstream_error"}},
                                         ensure_ascii=False))

        n = 0
        try:
            for inner in _iter_upstream(resp):
                try:
                    obj = json.loads(inner)
                except Exception:
                    continue
                n += 1
                err = _error_of(obj)
                if err:
                    write_error(err.get("message") or "上游返回错误")
                    break
                if st is None:
                    obj.setdefault("object", "chat.completion.chunk")
                    obj.setdefault("model", req_model)
                    write_sse("", json.dumps(obj, ensure_ascii=False))
                else:
                    for ev, payload in st.feed(obj):
                        write_sse(ev, payload)
            if st is None:
                write_sse("", "[DONE]")
            else:
                for ev, payload in st.finish():
                    write_sse(ev, payload)
        except (BrokenPipeError, ConnectionResetError):
            _log("客户端提前断开 stream model=%s" % model["key"])
        except Exception as e:
            _log("流式异常 model=%s: %r" % (model["key"], e))
            try:
                write_error(str(e))
                if st is None:
                    write_sse("", "[DONE]")
            except Exception:
                pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
            try:
                self.wfile.write(b"0\r\n\r\n")
            except Exception:
                pass
        _log("回合完成 model=%s stream=True frames=%d 耗时=%.2fs"
             % (model["key"], n, time.time() - t0))


def _drain_body(handler, length, cap=None):
    """把超限的请求体读干净再回错误：不读就直接断连，客户端只会看到连接被中止。"""
    remaining = min(length, cap or (MAX_BODY_BYTES + (1 << 20)))
    while remaining > 0:
        try:
            data = handler.rfile.read(min(65536, remaining))
        except Exception:
            break
        if not data:
            break
        remaining -= len(data)


def _drain(resp, limit=300):
    try:
        text = resp.read(limit).decode("utf-8", "replace")
    except Exception:
        text = ""
    try:
        resp.close()
    except Exception:
        pass
    return text.strip()


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        _log("处理 %s 异常: %r" % (client_address, exc))


# ── 命令行 ────────────────────────────────────────────────────────────────────
def _account_table():
    with _pool_lock:
        accs = list(_load_pool_locked())
    rows = []
    now = time.time()
    for a in accs:
        exp = a.get("exp")
        rows.append({
            "account": _label_of(a),
            "token": _redact(a.get("token")),
            "expires_at": a.get("expiresAt") or "unknown",
            "left_days": round((exp - now) / 86400, 1) if exp else None,
            "has_refresh": bool(a.get("refreshToken")),
            "refresh_dead": bool(a.get("refresh_dead")),
            "machine_id": _redact(a.get("machineId"), 6),
        })
    return rows


def _cmd_capture(_args=None):
    try:
        acct = _capture_current()
    except Exception as e:
        _print_err("提取登录态失败: %s" % e)
        return 1
    label = _add_session_to_pool(acct)
    print("已入池: %s" % label)
    print("  token      %s" % _redact(acct.get("token")))
    print("  有效期至   %s" % (acct.get("expiresAt") or "unknown"))
    print("  可续期     %s" % ("是" if acct.get("refreshToken") else "否"))
    print("  组织       %s" % (acct.get("organizationId") or "-"))
    print("  凭证文件   %s" % ACCOUNTS_FILE)
    return 0


def _cmd_list(_args=None):
    rows = _account_table()
    if not rows:
        print("账号池为空: python qoder_proxy.py --capture")
        return 1
    for r in rows:
        print("%-16s token=%-28s 到期=%s(剩 %s 天) 续期=%s%s"
              % (r["account"], r["token"], r["expires_at"], r["left_days"],
                 "有" if r["has_refresh"] else "无",
                 " [续期链已失效]" if r["refresh_dead"] else ""))
    print("共 %d 个账号，文件 %s" % (len(rows), ACCOUNTS_FILE))
    return 0


def _cmd_remove(label):
    with _pool_lock:
        accs = _load_pool_locked()
        keep = [a for a in accs if _label_of(a) != label]
        if len(keep) == len(accs):
            _print_err("池里没有账号 %s" % label)
            return 1
        _pool["accounts"] = keep
        _save_pool_locked()
    print("已移除 %s（剩 %d 个）" % (label, len(keep)))
    return 0


def _cmd_export(path):
    with _pool_lock:
        accs = list(_load_pool_locked())
    if not accs:
        _print_err("账号池为空，先 --capture")
        return 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "accounts": accs}, f, ensure_ascii=False, indent=2)
    print("已导出 %d 个账号到 %s（含明文 token，请妥善保管）" % (len(accs), path))
    return 0


def _cmd_import(path):
    data = _read_json_file(path)
    if not data:
        _print_err("读不到凭证包: %s" % path)
        return 1
    accs = data.get("accounts") if isinstance(data, dict) else data
    if not accs:
        _print_err("凭证包里没有账号")
        return 1
    added = []
    for a in accs:
        if isinstance(a, dict) and a.get("token"):
            added.append(_add_session_to_pool(a))
    if not added:
        _print_err("凭证包格式不对：每个账号至少要有 token")
        return 1
    print("已导入 %d 个账号: %s" % (len(added), ", ".join(added)))
    print("目标机不需要 Qoder CN 客户端，但签名器仍需要 Node；"
          "客户端的 worker runtime 文件可用 QODER_WORKER_BUNDLE 指定")
    return 0


def _cmd_models(args=None):
    force = bool(getattr(args, "force", False))
    try:
        cached = _fetch_models(force=force)
    except Exception as e:
        _print_err("拉取模型清单失败: %s" % e)
        return 1
    print("%-22s %-14s %8s %6s %6s %6s %s" % ("key", "名称", "上下文", "推理", "视觉",
                                              "价格系数", "状态"))
    for m in cached["list"]:
        print("%-22s %-14s %8s %6s %6s %6s %s"
              % (m["key"], m["name"][:14], m["ctx"], "Y" if m["reasoning"] else "",
                 "Y" if m["vision"] else "", m["price_factor"],
                 "" if m["enabled"] else "禁用"))
    return 0


def _cmd_check(_args=None):
    rows = _account_table()
    print("网关     %s" % GATEWAY)
    print("openapi  %s" % OPENAPI)
    print("账号池   %s（%d 个）" % (ACCOUNTS_FILE, len(rows)))
    for r in rows:
        print("  %-16s 到期=%s 续期=%s%s" % (r["account"], r["expires_at"],
                                            "有" if r["has_refresh"] else "无",
                                            " [续期链已失效]" if r["refresh_dead"] else ""))
    info = _signer.info()
    if info.get("backend") == "node" or info.get("node"):
        print("签名器   node=%s cosy=%s wasm=%s bundle=%s%s"
              % (info.get("node"), info.get("cosyVersion"), info.get("wasmReady"),
                 bool(info.get("bundle")),
                 "" if not info.get("error") else " 错误: %s" % info.get("error")))
    else:
        backend = "纯 Python" if not info.get("fallback") else "纯 Python 优先"
        print("签名器   %s cosy=%s%s"
              % (backend, info.get("cosyVersion"),
                 "" if not info.get("error") else " 错误: %s" % info.get("error")))
    print("登录     站点=%s 设备码登录=%s"
          % (SITE, "可用" if info.get("deviceInit") else "需 node 侧支持"))
    if not isinstance(info.get("error"), str) and info.get("wasmReady"):
        try:
            cached = _fetch_models()
            print("模型     %d 个（清单可用）" % len(cached["list"]))
        except Exception as e:
            print("模型     清单拉取失败: %s" % e)
    return 0


def _cmd_checkin(_args=None):
    with _pool_lock:
        accs = list(_load_pool_locked())
    if not accs:
        _print_err("账号池为空：先运行 --capture / --login / --import")
        return 1
    all_ok = True
    for acct in accs:
        ok, rep = _checkin_account(acct)
        print("账号     %s" % _label_of(acct))
        print("活动     HTTP %s campaigns=%d claimable=%s"
              % (rep["status"], rep["campaigns"], rep["claimable"]))
        for x in rep["claimed"]:
            print("领取     %s → HTTP %s%s"
                  % (x["id"], x.get("status"),
                     (" " + str(x["message"])) if x.get("message") else ""))
        if rep.get("after"):
            print("领取后   HTTP %s claimable=%s"
                  % (rep["after"]["status"], rep["after"]["claimable"]))
        if rep.get("message"):
            print("结果     %s" % rep["message"])
        if not ok:
            _print_err("签到失败: %s" % (rep.get("error") or rep.get("message")))
            all_ok = False
        if len(accs) > 1:
            print("-" * 40)
    return 0 if all_ok else 1


def _cmd_test_chat(text="hi", model_name=None):
    model = _resolve_model(model_name or DEFAULT_MODEL)
    if model is None:
        _print_err("未知模型 %s" % model_name)
        return 1
    oa = {"model": model["key"], "messages": [{"role": "user", "content": text}],
          "stream": True}
    try:
        _, resp = _open_upstream(oa, model)
    except ProxyError as e:
        _print_err("请求失败 %s: %s" % (e.status, e.message))
        return 1
    frames = list(_iter_upstream(resp))
    try:
        resp.close()
    except Exception:
        pass
    objs = []
    for f in frames:
        try:
            o = json.loads(f)
        except Exception:
            continue
        err = _error_of(o)
        if err:
            _print_err("上游错误: %s" % err.get("message"))
            return 1
        objs.append(o)
    merged = aggregate_sse(objs, model["key"])
    msg = merged["choices"][0]["message"]
    print("模型     %s (%s)" % (model["key"], model["name"]))
    print("finish   %s" % merged["choices"][0]["finish_reason"])
    print("usage    %s" % merged["usage"])
    print("回复     %s" % (msg.get("content") or "")[:300])
    if msg.get("reasoning_content"):
        print("思考     %s" % (msg["reasoning_content"] or "")[:120])
    return 0


def _interactive_menu():
    """交互式主菜单：启动前选择动作。返回 action 字符串。
    仅在 stdin 是 tty 时弹出；非交互直接返回 'serve'。"""
    if not sys.stdin.isatty():
        return 'serve'
    while True:
        with _pool_lock:
            accs = list(_load_pool_locked())
        now = time.time()
        print("\n" + "=" * 56)
        print("  Qoder CN 官方模型本地代理")
        print("=" * 56)
        print("  账号池: %d 个账号" % len(accs))
        for a in accs:
            exp = a.get("exp") or 0
            tag = "[过期]" if exp and exp < now else "[有效]"
            print("    %s %s %s" % (_label_of(a), tag, (a.get("expiresAt") or "")[:19]))
        print("-" * 56)
        if sys.platform == "win32":
            print("  0 启动代理")
            print("  1 浏览器设备码登录（加入账号池，可连续登多个）")
            print("  2 浏览器设备码登录（新设备 ID，最大隔离）")
            print("  3 抓取本机 Qoder CN 客户端登录态")
            print("  4 领取今天的每日 Credits（多账号）")
            print("  5 查看账号池详情")
            print("  6 移除账号")
            print("  7 导出账号池（跨电脑迁移）")
            print("  8 导入账号池")
            print("  9 退出")
        else:
            print("  0 启动代理")
            print("  1 浏览器设备码登录（加入账号池，可连续登多个）")
            print("  2 浏览器设备码登录（新设备 ID，最大隔离）")
            print("  3 领取今天的每日 Credits（多账号）")
            print("  4 查看账号池详情")
            print("  5 移除账号")
            print("  6 导出账号池（跨电脑迁移）")
            print("  7 导入账号池")
            print("  8 退出")
        print("-" * 56)
        try:
            choice = input("请选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 'quit'
        if choice == "0" or choice == "":
            return 'serve'
        if choice == "1":
            return 'login'
        if choice == "2":
            return 'login_new'
        if sys.platform == "win32":
            if choice == "3":
                return 'capture'
            if choice == "4":
                _cmd_checkin()
                continue
            if choice == "5":
                _cmd_list()
                continue
            if choice == "6":
                label = input("要移除的账号标签: ").strip()
                if label:
                    _cmd_remove(label)
                continue
            if choice == "7":
                path = input("导出路径: ").strip()
                if path:
                    _cmd_export(path)
                continue
            if choice == "8":
                path = input("导入文件路径: ").strip()
                if path:
                    _cmd_import(path)
                continue
            if choice == "9":
                return 'quit'
        else:
            if choice == "3":
                _cmd_checkin()
                continue
            if choice == "4":
                _cmd_list()
                continue
            if choice == "5":
                label = input("要移除的账号标签: ").strip()
                if label:
                    _cmd_remove(label)
                continue
            if choice == "6":
                path = input("导出路径: ").strip()
                if path:
                    _cmd_export(path)
                continue
            if choice == "7":
                path = input("导入文件路径: ").strip()
                if path:
                    _cmd_import(path)
                continue
            if choice == "8":
                return 'quit'


def _serve():
    try:
        server = QuietServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        _print_err("端口 %d 起不来: %s（可用 --port 换端口）" % (PORT, e))
        return 1
    print("Qoder CN 代理: http://127.0.0.1:%d  (%s)" % (PORT, ACCOUNTS_FILE))
    print("  OpenAI      POST /v1/chat/completions   GET /v1/models")
    print("  Responses   POST /v1/responses")
    print("  Anthropic   POST /v1/messages、/v1/messages/count_tokens")
    print("  健康        GET /health")
    print("  上游        %s%s" % (GATEWAY, INFER_PATH))
    print("  签名        %s" % _signer.describe())
    if API_KEY:
        print("  API Key     已启用（QODER_PROXY_API_KEY）")
    else:
        print("  API Key     未设置 —— 任何本机进程都能用这个代理，建议设 QODER_PROXY_API_KEY")
    with _pool_lock:
        n = len(_load_pool_locked())
    if not n:
        print("  警告        账号池为空，先运行: python qoder_proxy.py --login 或 --capture")
    _log("启动 port=%d accounts=%d" % (PORT, n))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def main():
    global PORT, LOG_FILE, API_KEY
    p = argparse.ArgumentParser(
        description="Qoder CN 官方模型本地代理（OpenAI / Responses / Anthropic 三协议）")
    p.add_argument("--capture", action="store_true", help="从本机 Qoder CN 客户端提取登录态入池")
    p.add_argument("--login", action="store_true",
                   help="浏览器设备码登录换 token 入池（不需要装 Qoder CN 客户端）")
    p.add_argument("--new-device", action="store_true",
                   help="配合 --login：强制生成新设备 ID（不用本机的），用于多账号隔离")
    p.add_argument("--site", help="设备码授权页 base（默认 https://qoder.cn，等价 QODER_PROXY_SITE）")
    p.add_argument("--client-id", help="覆盖设备码登录的 client_id（默认取自客户端 bundle）")
    p.add_argument("--list", action="store_true", help="列出池内账号（脱敏）")
    p.add_argument("--remove", metavar="ACCOUNT", help="移除池内账号")
    p.add_argument("--export", metavar="FILE", help="导出凭证包（跨机迁移）")
    p.add_argument("--import", dest="import_", metavar="FILE", help="导入凭证包")
    p.add_argument("--models", action="store_true", help="打印上游模型清单")
    p.add_argument("--check", action="store_true", help="检查账号池/签名器/连通性")
    p.add_argument("--checkin", action="store_true",
                   help="领取今天的每日 Credits（多账号，10:00 后窗口内）")
    p.add_argument("--test-chat", metavar="TEXT", nargs="?", const="hi",
                   help="走完整链路发一条测试消息（默认 hi）")
    p.add_argument("--port", type=int, help="覆盖监听端口")
    p.add_argument("--api-key", help="覆盖 API Key（等价于 QODER_PROXY_API_KEY）")
    p.add_argument("--log", help="日志文件路径（默认关闭）")
    p.add_argument("--menu", action="store_true", help="启动前弹交互菜单（默认 tty 时自动弹）")
    p.add_argument("--serve", action="store_true", help="跳过菜单直接启动代理")
    args = p.parse_args()
    if args.port:
        PORT = args.port
    if args.api_key:
        API_KEY = args.api_key
    if args.log:
        LOG_FILE = args.log
    for name, fn in (("capture", _cmd_capture),
                     ("list", _cmd_list), ("check", _cmd_check),
                     ("checkin", _cmd_checkin), ("models", _cmd_models)):
        if getattr(args, name):
            return fn(args)
    if args.login:
        return _cmd_login(args, new_device=getattr(args, 'new_device', False))
    if args.remove:
        return _cmd_remove(args.remove)
    if args.export:
        return _cmd_export(args.export)
    if args.import_:
        return _cmd_import(args.import_)
    if args.test_chat is not None:
        return _cmd_test_chat(args.test_chat)
    # 无子命令：--serve 直接启动；否则弹交互菜单
    if not args.serve:
        action = _interactive_menu()
        if action == 'quit':
            return 0
        if action == 'login':
            _cmd_login(args)
            return main()
        if action == 'login_new':
            _cmd_login(args, new_device=True)
            return main()
        if action == 'capture':
            _cmd_capture(args)
            return main()
    return _serve()


if __name__ == "__main__":
    sys.exit(main())
