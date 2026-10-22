# -*- coding: utf-8 -*-
"""
qoder_proxy.py —— Qoder 双区官方模型通道的本地 API 代理（OpenAI / Responses / Anthropic 三协议）

单进程单端口同时服务国内版（cn）与国际版（intl）账号：按账号 realm 路由上游端点、
签到形态、签名器 bundle。把已登录的 Qoder 桌面端官方模型通道转成本地标准 API：
  OpenAI 兼容      GET  /v1/models、POST /v1/chat/completions（流式 + 非流式）
  OpenAI Responses POST /v1/responses（兼容 codex-cli 等）
  Anthropic 兼容   POST /v1/messages、POST /v1/messages/count_tokens
  健康检查         GET  /health
  管理面板         GET  /admin（qoder_admin 共享管理层：多 API Key、账号池、模型映射）
  CORS             OPTIONS 同上路径

双区域配置 REALM_CONFIGS（参照 qoder2api-hub）：
  cn   国内版  gateway=gateway.qoder.com.cn  openapi=openapi.qoder.com.cn  sign_js=qoder_sign.mjs
  intl 国际版  gateway=api3.qoder.sh         openapi=openapi.qoder.sh       sign_js=qoder_intl_sign.mjs
  账号池每个号带 realm 字段（缺省按 domain 推断，再缺省 QODER_PROXY_DEFAULT_REALM=cn）。
  请求路由：LLM 端点按 X-Realm 头选 realm → 选该 realm 的可用账号；缺省取全部（按账号自身 realm 走端点）。
  签到形态按 realm 区分：cn 基础（Cosy-Version 1.1.69、无 MachineHostname）；
    intl 对齐 0.4.8（Cosy-Version 0.4.8 + MachineHostname + 双UA + NYe 头校验）。

上游是 Qoder 推理网关（逆向自客户端 worker runtime），端点按账号 realm 取：
  cn   POST https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation
  intl POST https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation
  两者都要求 Authorization: Bearer COSY.<base64>.<sig> 一整套 Cosy-* 签名头，
  且请求体要按 Encode=1 编码 —— 签名与编码已从客户端的 Rust/WASM 模块
  qoder_auth_wasm_bg.wasm 逐字节还原，内联在本文件的 Cosy 段落（纯 Python，
  所以部署只需要 qoder_proxy.py 这一个文件，不需要 qoder_cosy.py）。
  默认不需要 node；QODER_PROXY_SIGN_BACKEND=node 可切回 sidecar 对照（按 realm 选 bundle）。

零第三方依赖（纯标准库）。只有 --capture 需要 cryptography 解 AES-256-GCM；
用 --import 迁移动凭证时不需要任何依赖。日常代理与签名都不需要 node。

用法:
  python qoder_proxy.py                      # 启动代理（默认弹交互菜单，tty 时）
  python qoder_proxy.py --serve              # 跳过菜单直接启动（单进程单端口 8791 服务双区）
  python qoder_proxy.py --login --realm cn   # 浏览器授权登录国内版账号入池
  python qoder_proxy.py --login --realm intl # 浏览器授权登录国际版账号入池
  python qoder_proxy.py --login --new-device # 授权时不用本机设备 ID（token 签发给一套新设备）
  python qoder_proxy.py --capture --realm cn # 从本机国内版客户端解密登录态入池
  python qoder_proxy.py --list               # 每个号各自的设备身份/机器 ID/realm
  python qoder_proxy.py --models
  python qoder_proxy.py --checkin            # 领今天的每日 Credits（遍历池中所有账号，按各自 realm 签到）
  python qoder_proxy.py --test-chat "hi"
"""
import argparse
import base64
import calendar
import ctypes
import hashlib
import http.client
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from email.utils import parsedate

# ── 常量与路径 ────────────────────────────────────────────────────────────────

import llm_proxy_core as C
import qoder_admin as QA

PORT = int(os.environ.get("QODER_PROXY_PORT", "8791"))
API_KEY = os.environ.get("QODER_PROXY_API_KEY") or ""      # 空 = 不鉴权
MAX_BODY_BYTES = 8 << 20                                    # 请求体上限，防 OOM
MAX_LINE_BYTES = 32 << 20        # 上游 SSE 单行上限；超过说明流坏了，直接断掉
TIMEOUT_SEC = int(os.environ.get("QODER_PROXY_TIMEOUT", "600"))
MODELS_TTL = int(os.environ.get("QODER_PROXY_MODELS_TTL", "3600"))
REFRESH_AHEAD_SEC = int(os.environ.get("QODER_PROXY_REFRESH_AHEAD", "86400"))
FAIL_COOLDOWN_SEC = 300         # 401/403 之后的账号冷却，避免立刻反复重试
RATE_COOLDOWN_SEC = 60          # 429 且上游没给 Retry-After 时的兜底冷却
RETRY_MAX = 2                   # 连接失败 / 502/503/504 的额外重试次数（全局，不按账号累计）
RETRY_BACKOFF_SEC = 0.5         # 退避基数，指数增长
SWEEP_SEC = 30                  # 账号池临期巡检最小间隔

# 每日 Credits 自动签到（活动窗口每天 10:00 后开放，越晚越稳，所以默认 11:30）
CHECKIN_ENABLED = (os.environ.get("QODER_PROXY_CHECKIN", "1").strip().lower()
                   not in ("0", "off", "false", "no"))
CHECKIN_AT = (os.environ.get("QODER_PROXY_CHECKIN_AT") or "10:30").strip()
CHECKIN_RETRY_SEC = int(os.environ.get("QODER_PROXY_CHECKIN_RETRY_SEC", "1800"))
CHECKIN_MAX_ATTEMPTS = int(os.environ.get("QODER_PROXY_CHECKIN_TRIES", "4"))
# 状态单独放一个文件（不含 token）：池文件由续期写，混在一起会把"池 md5 变了=续期了"
# 这条判据弄脏；而且进程重启后要知道"今天签过没"，不能只靠内存。
CHECKIN_STATE_FILE = os.environ.get("QODER_PROXY_CHECKIN_STATE") or os.path.join(
    os.path.expanduser("~"), ".qoder-proxy", "checkin_state.json")

SCENE = os.environ.get("QODER_PROXY_SCENE", "chat")
DEFAULT_MODEL = os.environ.get("QODER_PROXY_MODEL", "auto")
BUSINESS_TYPE = os.environ.get("QODER_PROXY_BUSINESS_TYPE", "agent")
BUSINESS_PRODUCT = os.environ.get("QODER_PROXY_BUSINESS_PRODUCT", "cli")
# v3 会话类型：国际版桌面端/CLI 都是 qodercli（客户端 bundle 里的 HMA）。
SESSION_TYPE = os.environ.get("QODER_PROXY_SESSION_TYPE", "qodercli")

HERE = os.path.dirname(os.path.abspath(__file__))
# 管理层（qoder_admin 集成）：多 Key 存储 / 管理密码 / 模型路由文件。
# 默认都放脚本同目录（与账号池同一个"程序 + 数据文件"部署模型），env 可覆盖。
ADMIN_KEYS_FILE = (os.environ.get("QODER_PROXY_ADMIN_KEYS")
                   or os.path.join(HERE, "qoder_admin_keys.json"))
ADMIN_PASSWORD = os.environ.get("QODER_PROXY_ADMIN_PASSWORD", "")   # 空 = 管理 API 禁用
MODEL_ROUTES_FILE = (os.environ.get("QODER_PROXY_MODEL_ROUTES")
                     or os.path.join(HERE, "qoder_model_routes.json"))
SIGN_JS = os.environ.get("QODER_SIGN_JS") or os.path.join(HERE, "qoder_sign.mjs")  # 占位，REALM_CONFIGS 块后按 DEFAULT_REALM 修正
NODE = os.environ.get("QODER_PROXY_NODE", "node")
# auto（默认）：纯 Python 优先，异常才回落 node；py：只用纯 Python；node：只用 sidecar
SIGN_BACKEND = (os.environ.get("QODER_PROXY_SIGN_BACKEND") or "auto").strip().lower()
# 账号池是"程序 + 账号文件"两件套里的第二个文件，路径按优先级解析：
#   1. QODER_PROXY_ACCOUNTS 显式指定
#   2. 脚本同目录的 qoder_accounts.json —— 默认位置，整目录拷走即可部署
#   3. ~/.qoder-proxy/accounts.json —— 兼容旧位置（迁移前放在这里仍能读）
# 同目录那份里是真 token：仓库根 .gitignore 已挡 *_accounts.json，仍不要提交、不要外传。
# 两个都不存在时返回同目录路径，首次写池时自动创建。
def _default_accounts_file():
    env = os.environ.get("QODER_PROXY_ACCOUNTS")
    if env:
        return env
    local = os.path.join(HERE, "qoder_accounts.json")
    if os.path.exists(local):
        return local
    home = os.path.join(os.path.expanduser("~"), ".qoder-proxy", "accounts.json")
    return home if os.path.exists(home) else local


ACCOUNTS_FILE = _default_accounts_file()

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
USERPROFILE = os.environ.get("USERPROFILE", HOME)

# ── 双区域配置（国内 cn / 国际 intl）──────────────────────────────────────────
# 参照 qoder2api-hub 的 REALM_CONFIGS：单进程单端口同时服务双区账号，按账号 realm
# 路由上游端点、签到形态、签名器 bundle。DEFAULT_REALM 缺省 cn（国内风控宽松）。
REALM_CONFIGS = {
    "cn": {
        "name": "国内版",
        "gateway": "https://gateway.qoder.com.cn",
        "openapi": "https://openapi.qoder.com.cn",
        "website": "https://qoder.com.cn",
        "site": "https://qoder.cn",          # 设备码授权页 base
        "sign_js": "qoder_sign.mjs",         # 签名器 bundle
        "domain": "qoder.com.cn",
        "app_dir": "com.qodercn.app.stable",  # 桌面 App Roaming 数据目录
        "home_dir": ".qoder-cn",              # 官方客户端本地目录
        "has_checkin": True,
        # cn 签到形态：基础（Cosy-Version 1.1.69、无 MachineHostname、UA 用代理默认）
        "checkin_shape": "basic",
    },
    "intl": {
        "name": "国际版",
        "gateway": "https://api3.qoder.sh",
        "openapi": "https://openapi.qoder.sh",
        "website": "https://qoder.com",
        "site": "https://qoder.com",
        "sign_js": "qoder_intl_sign.mjs",
        "domain": "qoder.com",
        "app_dir": "com.qoder.app.stable",
        "home_dir": ".qoder",
        "has_checkin": False,
        # intl 签到形态：对齐 0.4.8（Cosy-Version 0.4.8 + MachineHostname + 双UA + NYe）
        "checkin_shape": "aligned",
    },
}
DEFAULT_REALM = (os.environ.get("QODER_PROXY_DEFAULT_REALM") or "cn").strip().lower()
if DEFAULT_REALM not in REALM_CONFIGS:
    DEFAULT_REALM = "cn"


def detect_realm_from_domain(domain):
    """按域名推断 realm：含 qoder.sh 或等于 qoder.com → intl，否则 cn。"""
    d = (domain or "").strip().lower()
    if "qoder.sh" in d or d == "qoder.com":
        return "intl"
    return "cn"


def get_realm_config(realm):
    """取 realm 配置；未知 realm 回落 cn。"""
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["cn"]


def _realm_of(acct):
    """从账号对象取 realm；缺省按 domain 推断，再缺省 DEFAULT_REALM。"""
    if isinstance(acct, dict):
        r = (acct.get("realm") or "").strip().lower()
        if r in REALM_CONFIGS:
            return r
        d = acct.get("domain") or ""
        if d:
            return detect_realm_from_domain(d)
    return DEFAULT_REALM


def _gw(realm):
    """按 realm 取推理网关 host。env 覆盖优先（测试 mock 赋值 GATEWAY 时走全局）。"""
    return get_realm_config(realm)["gateway"]


def _oa(realm):
    """按 realm 取 openapi host。"""
    return get_realm_config(realm)["openapi"]


def _sign_js_path(realm):
    """按 realm 取签名器 bundle 路径。"""
    return os.path.join(HERE, get_realm_config(realm)["sign_js"])


def _site_url(realm):
    """按 realm 取设备码授权页 base。"""
    return get_realm_config(realm)["site"]


def _user_data_dir(realm):
    return os.path.join(APPDATA, get_realm_config(realm)["app_dir"])


def _qoder_home(realm):
    return os.path.join(USERPROFILE, get_realm_config(realm)["home_dir"])


def _endpoint_cache_path(realm):
    return os.path.join(_qoder_home(realm), ".cache", "endpoint-cache.json")


# 模块级默认值（= DEFAULT_REALM 的路径），测试可直接赋值 mock（如 Q.GATEWAY = mock_base）。
# 按 realm 路由时用上面的 _gw/_oa/_sign_js_path 等函数，不要直接读这些全局常量。
USER_DATA = _user_data_dir(DEFAULT_REALM)
QODER_HOME = _qoder_home(DEFAULT_REALM)

AUTH_BLOB = os.path.join(USER_DATA, "auth.v1.dat")
LOCAL_STATE = os.path.join(USER_DATA, "Local State")
MACHINE_ID_FILE = os.path.join(QODER_HOME, ".auth", "machine_id")
UMID_FILE = os.path.join(QODER_HOME, "umid-cache.json")
ENDPOINT_CACHE = _endpoint_cache_path(DEFAULT_REALM)

# REALM_CONFIGS 块已定义，按 DEFAULT_REALM 修正 SIGN_JS（env 未显式指定时）
if not os.environ.get("QODER_SIGN_JS"):
    SIGN_JS = _sign_js_path(DEFAULT_REALM)

MODEL_LIST_PATH = "/algo/api/v2/model/list"
INFER_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
INFER_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
REFRESH_PATH = "/api/v1/deviceToken/refresh"
USERINFO_PATH = "/api/v1/userinfo"
ORG_TAGS_PATH = "/api/v1/organizations/{org}/tags"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
USAGE_PATH = "/sash/api/v2/me/usage"
MACHINE_OS = os.environ.get("QODER_PROXY_MACHINE_OS") or (
    "x86_64_win32" if sys.platform == "win32" else "x86_64_" + sys.platform)


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _endpoint(kind, realm=None):
    """从客户端 endpoint-cache 自动发现 host；拿不到就用 REALM_CONFIGS 默认域名。
    realm 缺省用 DEFAULT_REALM。cache 路径按 realm 分键（cn/intl 各一套缓存）。"""
    r = realm or DEFAULT_REALM
    cfg = get_realm_config(r)
    cache = _read_json_file(_endpoint_cache_path(r)) or {}
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
    return cfg["gateway"] if kind != "openapi" else cfg["openapi"]


GATEWAY = os.environ.get("QODER_PROXY_GATEWAY") or _endpoint("infer")
OPENAPI = os.environ.get("QODER_PROXY_OPENAPI") or _endpoint("openapi")

USER_AGENT = "qoder-proxy/1.0 (+https://github.com)"

# ── 签到链路请求形态常量（对齐 Qoder 桌面客户端 App 0.4.8 的逆向规格，
#    reverse_asar_findings.md §1/§2/§9）：签到相关请求模拟桌面客户端，
#    其余请求（登录、续期、对话）继续用上面的代理自身 USER_AGENT。──────
# 客户端 App 版本号：intl 签到 Cosy-Version 用它（app.getVersion()），与对话链路
# 的 CLI 版本号 COSY_VERSION("1.1.69") 语义不同，别混用。
CLIENT_APP_VERSION = "0.4.8"
# cn 签到用基础形态：Cosy-Version 沿用 CLI 版本号 1.1.69（国内版风控宽松，实战验证）。
CN_CHECKIN_VERSION = "1.1.69"
# 客户端两种请求两种 UA：campaigns 列表 GET 由主进程 nativeHttpService 发，
# UA 字面量就是 "Qoder"；claim POST 由 campaign H5 iframe 发，走 Electron
# session 默认 UA（qoder/<版本> 段随 CLIENT_APP_VERSION 变化）。
CAMPAIGN_LIST_UA = "Qoder"
CLAIM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) qoder/%s Chrome/143.0.0.0 Electron/43.1.1 "
            "Safari/537.36" % CLIENT_APP_VERSION)



# 设备码登录（--login）：站点授权页 base 取自 bundle 里 startDeviceFlow 的 site 参数
# （cn 版 https://qoder.cn / 国际版 https://qoder.com，按 realm 取）；轮询走 openapi host。
# 客户端 bundle 里的懒初始化会给出真 client_id，所以这里不需要硬编码。
SITE = os.environ.get("QODER_PROXY_SITE") or _site_url(DEFAULT_REALM)
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


new_id = _rand = C.new_id
merge_tool_name = _merge_tool_name = C.merge_tool_name


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
        raise RuntimeError("找不到 %s（Qoder 国际版未安装或未登录）" % LOCAL_STATE)
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
        for name in ("machine-id", "auth.machine-id"):
            try:
                with open(os.path.join(USER_DATA, name), encoding="utf-8") as f:
                    mid = f.read().strip()
            except Exception:
                mid = ""
            if mid:
                break
    umid = _read_json_file(UMID_FILE) or {}
    info = umid.get("info") if isinstance(umid.get("info"), dict) else umid
    return {
        "machineId": mid,
        "machineToken": (info or {}).get("machineToken") or "",
        "machineType": str((info or {}).get("machineType") or ""),
        "machineCode": (info or {}).get("machineCode") or "",
    }


# 设备身份模式：isolate（默认，每个账号一套）/ host（沿用本机客户端那一套）
DEVICE_MODE = (os.environ.get("QODER_PROXY_DEVICE") or "isolate").strip().lower()


def _new_device_identity():
    """一套新的设备指纹，形状对齐本机 umid 缓存：machineId 是 uuid，
    machineType/machineCode 是 18 位小写 hex。machineToken 故意留空——那是本机
    客户端的令牌，伪造只会让续期更容易被拒；请求头会退回用 machineId 填。"""
    return {
        "machineId": str(uuid.uuid4()),
        "machineToken": "",
        "machineType": secrets.token_hex(9),
        "machineCode": secrets.token_hex(9),
    }


def _isolate_pool_devices(accs):
    """池里每个账号各用一套设备身份：machineId 缺失或与池内他人重复时，给它重发一套。
    排在前面、已经独占某个 id 的账号不动，所以只有真正撞车的才会被改，重跑也不会来回改。

    被换掉的账号把签发时那台机器的身份留在 loginMachineId/loginMachineToken 里：
    实测签名链路（/algo）和 openapi 只读接口都不校验 Cosy-MachineId 与 token 的对应
    关系，但 deviceToken/refresh 是设备绑定的，续期只能用签发时那一套。
    返回被改动的账号标签。"""
    if DEVICE_MODE == "host":
        return []
    seen = set()
    touched = []
    for a in accs:
        mid = a.get("machineId") or ""
        if mid and mid not in seen:
            seen.add(mid)
            continue
        if mid:
            a["loginMachineId"] = mid
            a["loginMachineToken"] = a.get("machineToken") or ""
        a.update(_new_device_identity())
        seen.add(a["machineId"])
        touched.append(_label_of(a))
    return touched


def _refresh_device_fields(acct):
    """续期请求里的设备字段：优先用签发 token 时那台机器的身份，不是池里给它发的独立指纹。"""
    fields = {}
    mid = acct.get("loginMachineId") or acct.get("machineId") or ""
    if mid:
        fields["machine_id"] = mid
    token = acct.get("loginMachineToken") or acct.get("machineToken") or ""
    if token:
        fields["machine_token"] = token
    return fields


def _device_counts(accs):
    """machineId -> 池内有几个号在用它。"""
    counts = {}
    for a in accs:
        mid = a.get("machineId") or ""
        if mid:
            counts[mid] = counts.get(mid, 0) + 1
    return counts


def _device_state(acct, counts=None):
    """展示用：这个号的设备身份是不是它自己的一套。"""
    mid = acct.get("machineId") or ""
    if not mid:
        return "无"
    if DEVICE_MODE == "host":
        return "本机共用"
    if counts is None:
        with _pool_lock:
            counts = _device_counts(_load_pool_locked())
    return "共用" if counts.get(mid, 0) > 1 else "独立"


def _device_summary(rows):
    """面板上的设备身份小结（rows 来自 _account_table）。"""
    if DEVICE_MODE == "host":
        return "全部共用本机指纹（QODER_PROXY_DEVICE=host）"
    total = len(rows)
    iso = sum(1 for r in rows if r.get("device") == "独立")
    out = "%d/%d 各一套" % (iso, total)
    if iso < total:
        out += " 仍在共用: " + ", ".join(r["account"] for r in rows
                                        if r.get("device") != "独立")
    return out


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
def _openapi_json(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None):
    """打一次 openapi 请求，返回 (status, json_dict)。这几个端点只认 Bearer，不签名。
    ua：User-Agent 覆盖（签到链路要区分客户端两种 UA，见 _checkin_account）；
    缺省用全局 USER_AGENT（登录、续期、userinfo 等代理自身请求）。
    realm：按区域取 openapi host（cn/intl 各自端点）；缺省用全局 OPENAPI
    （测试 mock 直接赋值 Q.OPENAPI 时走全局，向后兼容）。"""
    base = _oa(realm) if realm else OPENAPI
    url = base + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", ua or USER_AGENT)
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
    realm = _realm_of(acct)
    st, ui = _openapi_json("GET", USERINFO_PATH, token=tok, realm=realm)
    if st == 200 and isinstance(ui, dict):
        acct["uid"] = ui.get("id") or acct.get("uid") or ""
        acct["organizationId"] = ui.get("orgId") or ""
        acct["displayName"] = (ui.get("displayName") or ui.get("name")
                               or ui.get("nickName") or acct.get("displayName") or "")
        org = acct["organizationId"]
        if org:
            st2, tags = _openapi_json("GET", ORG_TAGS_PATH.format(org=org), token=tok,
                                      realm=realm)
            if st2 == 200 and isinstance(tags, dict):
                acct["organizationTags"] = tags.get("tags") or tags.get("data") or []
    return acct


# ── 每日 Credits 签到（单账号）──────────────────────────────────────────────
# 客户端头值校验（electronMain.js NYe）：值 trim 后非空且全为可打印 ASCII
# （[\x20-\x7e]）才发送，否则整头丢弃。
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]+$")
_HOSTNAME_MAX = 96      # 客户端 DR0/Lf0：Cosy-MachineHostname 上限 96 字符


def _sanitize_machine_hostname(raw):
    """Cosy-MachineHostname 取值，对齐客户端 DR0（reverse_asar_findings.md §4）：
    trim 后为可打印 ASCII 且 ≤96 字符原值直通（Windows 常规场景）；空返回 ""
    （头不发送）；非 ASCII / 超长走兜底——可打印部分折叠成 "-" 拼 sha256 前
    8 hex（简化实现，不做客户端的 domainToASCII/punycode 分支）。"""
    x = str(raw or "").strip()
    if not x:
        return ""
    if len(x) <= _HOSTNAME_MAX and _PRINTABLE_ASCII_RE.match(x):
        return x
    hash8 = hashlib.sha256(x.encode("utf-8")).hexdigest()[:8]
    cleaned = re.sub(r"[^\x21-\x7e]+", "-", x)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    limit = _HOSTNAME_MAX - len(hash8) - 1
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip("- ")
    return (cleaned + "-" + hash8) if cleaned else "unknown-" + hash8


def _machine_hostname(acct):
    """账号字段 machineHostname 优先，缺省用本机 hostname（客户端 os.hostname()）。"""
    raw = acct.get("machineHostname") or ""
    if not raw:
        try:
            raw = socket.gethostname()
        except Exception:
            raw = ""
    return _sanitize_machine_hostname(raw)


def _cosy_device_headers(acct):
    """签到链路的桌面端设备头，按账号 realm 区分签到形态：
    cn（基础形态）：Cosy-Version 用 CLI 版本号 1.1.69、无 MachineHostname、无 NYe 校验
        —— 国内版风控宽松，对齐国内版实战验证行为。
    intl（对齐形态）：Cosy-Version 用客户端 App 版本号 0.4.8、恒发 MachineHostname、
        所有头值按 NYe 规则校验（trim 后非空且全为可打印 ASCII 才发）
        —— 对齐 Qoder 桌面客户端 0.4.8 逆向规格（reverse_asar_findings.md §1.3/§4）。
    Cosy-ClientType 必须是 10（桌面端）：实测 5 时 /me/campaigns 恒返回
    showCampaign=false、campaigns=[]，签到一分领不到；10 才有活动列表。
    推理链路仍用 5（见 CosyContext._base_headers），只有签到走桌面端身份。"""
    realm = _realm_of(acct)
    h = {
        "Cosy-Version": acct.get("cosyVersion") or (
            CLIENT_APP_VERSION if realm == "intl" else CN_CHECKIN_VERSION),
        "Cosy-ClientType": "10",
        "Cosy-MachineOS": MACHINE_OS,
        "Cosy-MachineId": acct.get("machineId") or "",
        "Cosy-MachineToken": acct.get("machineToken") or acct.get("machineId") or "",
        "Cosy-MachineType": str(acct.get("machineType") or "5"),
        "Cosy-MachineCode": acct.get("machineCode") or "",
    }
    if realm == "intl":
        hostname = _machine_hostname(acct)
        if hostname:
            h["Cosy-MachineHostname"] = hostname
        out = {}
        for k, v in h.items():
            v = str(v).strip()
            if v and _PRINTABLE_ASCII_RE.match(v):
                out[k] = v
        return out
    return h



def _campaign_id(campaign):
    for k in ("id", "campaignId", "campaign_id"):
        v = campaign.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _is_credits_benefit(campaign):
    """这一项是不是"每天 100 Credits"那种真奖励（用于判断今天的份到没到位）。"""
    return (campaign.get("actionType") == "CLAIM_BENEFIT"
            and ((campaign.get("benefit") or {}).get("kind") or "").upper() == "CREDITS")


def _claimable_campaigns(cps):
    """真正要领的那几项：actionType=CLAIM_BENEFIT 且 claimStatus=CLAIMABLE。
    顶层 claimable 只是"有没有份"的总开关，不区分是哪个活动；已领过的项
    claimStatus 会变成 CLAIMED，所以按项筛才不会把 VIEW_DETAILS 当成奖励去 POST。"""
    return [c for c in cps
            if _campaign_id(c) and c.get("actionType") == "CLAIM_BENEFIT"
            and c.get("claimStatus") == "CLAIMABLE"]


def _credits_secured(cps):
    """今天的 Credits 是否已经在手上（不管是这一轮领的还是客户端/上一轮领的）。"""
    return any(_is_credits_benefit(c) and c.get("claimStatus") == "CLAIMED" for c in cps)


CLAIM_RECEIPT_KEYS = ("grantId", "claimedAt", "grantedAt", "replayed")


def _add_on_total(acct):
    """额度桶里的 addOnQuota.total——发放回执只证明服务端说给了，这个才证明真到手。
    实测每成功领一次它 +100（客户端日志里 100→200→300）。读不到就 None，不影响签到结论。"""
    realm = _realm_of(acct)
    try:
        st, obj = _openapi_json("GET", USAGE_PATH, token=acct.get("token") or "",
                                headers=_cosy_device_headers(acct), realm=realm)
    except Exception:
        return None
    if st != 200 or not isinstance(obj, dict):
        return None
    q = obj.get("qoderUsage") if isinstance(obj.get("qoderUsage"), dict) else obj
    total = (q.get("addOnQuota") or {}).get("total")
    return total if isinstance(total, int) else None


def _checkin_account(acct):
    """给这一个账号把列表里所有可领的活动（CLAIM_BENEFIT + CLAIMABLE）逐个领一遍；
    没有可领项就是成功 no-op。

    行为对齐国内版 qoder_proxy.py（服务器实战验证：所有账号连续多天全部正常
    领取）：签到一律用账号池身份——每号一套独立 machineId，machineToken 为空
    就用 machineId 顶替（见 _cosy_device_headers），配合 _isolate_pool_devices
    保证每号独立。此前盲领失败的根因是账号共用了本机客户端共享身份（撞设备
    判重连坐），不是"列表没条目"，所以不盲领、不派生身份：只领自己列表里
    CLAIMABLE 的条目（_claimable_campaigns），已领过的 claimStatus=CLAIMED
    自然落进 no-op。claim 幂等（同账号重复 POST 返回 replayed=true 不二次发放），
    整条链路可以随便重试。"""
    token = acct.get("token") or ""
    realm = _realm_of(acct)
    headers = _cosy_device_headers(acct)   # 账号池身份：签到认池里这号的设备字段
    # 签到 UA 按 realm 区分：intl 对齐桌面客户端两种 UA（reverse_asar_findings.md §2.2）
    # ——列表 GET 是主进程 nativeHttpService 发的（UA="Qoder"）；claim POST 由 campaign
    # H5 iframe 发，走 Electron session 默认 UA（CLAIM_UA）。cn 用代理默认 UA（风控宽松）。
    list_ua = CAMPAIGN_LIST_UA if realm == "intl" else None
    claim_ua = CLAIM_UA if realm == "intl" else None
    st, obj = _openapi_json("GET", CAMPAIGNS_PATH, token=token, headers=headers,
                            ua=list_ua, realm=realm)
    rep = {"status": st, "campaigns": 0, "claimable": False, "claimed": [], "secured": False}
    if st != 200 or not isinstance(obj, dict):
        rep["error"] = ((obj or {}).get("message") if isinstance(obj, dict) else "")
        rep["error"] = rep["error"] or "HTTP %s" % st
        return False, rep
    cps = [c for c in (obj.get("campaigns") or []) if isinstance(c, dict)]
    rep["campaigns"] = len(cps)
    rep["claimable"] = bool(obj.get("claimable"))
    rep["secured"] = _credits_secured(cps)
    targets = _claimable_campaigns(cps)
    if not targets:
        rep["message"] = ("今天的 Credits 已在手上（之前领过）" if rep["secured"]
                          else "当前没有可领取的活动（未到窗口或活动已结束）")
        return True, rep
    for c in targets:
        cid = _campaign_id(c)
        benefit = c.get("benefit") or {}
        path = "%s/%s/claim" % (CAMPAIGNS_PATH, urllib.parse.quote(cid, safe=""))
        cst, cobj = _openapi_json("POST", path, body={}, token=token, headers=headers,
                                  ua=claim_ua, realm=realm)
        item = {"id": cid, "status": cst,
                "key": c.get("campaignKey"),
                "kind": benefit.get("kind"), "amount": benefit.get("amount")}
        if isinstance(cobj, dict):
            for k in CLAIM_RECEIPT_KEYS:
                if k in cobj:
                    item[k] = cobj[k]
            # 国际版回执体里带 status:"CLAIMED"/"BLOCKED"，不能拿它覆盖 HTTP 状态码
            if cobj.get("status") is not None:
                item["claimStatus"] = cobj["status"]
            for k in ("message", "code", "success", "failureCode"):
                if k in cobj:
                    item[k] = cobj[k]
            rb = cobj.get("benefit")
            if isinstance(rb, dict):
                item["kind"] = rb.get("kind") or item["kind"]
                item["amount"] = rb.get("amount") if rb.get("amount") is not None else item["amount"]
        rep["claimed"].append(item)
    # 领取后再看一眼列表（展示用）。secured 只升不降：回执已认定到账的，不能让
    # 复查结果（列表可能滞后）把 True 冲掉。
    st2, obj2 = _openapi_json("GET", CAMPAIGNS_PATH, token=token, headers=headers,
                              ua=list_ua, realm=realm)
    cps2 = [c for c in ((obj2 or {}).get("campaigns") or []) if isinstance(c, dict)] \
        if st2 == 200 and isinstance(obj2, dict) else []
    rep["after"] = {"status": st2,
                    "claimable": bool(obj2.get("claimable")) if isinstance(obj2, dict) else None}
    rep["secured"] = rep["secured"] or _credits_secured(cps2)
    if rep["claimed"]:
        rep["addOnTotal"] = _add_on_total(acct)
    # 回执逐条判定（兼容国内版"2xx 即成功"形态与国际版带业务状态的回执）：
    #   2xx 且（无 claimStatus / CLAIMED）→ 发放到账；
    #   2xx 且 BLOCKED + failureCode=SAME_PERSON_ALREADY_CLAIMED → 上游拒发，
    #     该号未到账但不报错（判重每天每设备一份，当天重试只重放，别让 cron 误报）；
    #   其它（非 2xx、或 BLOCKED 其它原因）→ 失败。

    def _item_ok(i):
        if i["status"] not in (200, 201, 204):
            return False
        rs = i.get("claimStatus")
        return rs in (None, "CLAIMED") or (rs == "BLOCKED"
                                           and i.get("failureCode") == "SAME_PERSON_ALREADY_CLAIMED")

    bad = next((i for i in rep["claimed"] if not _item_ok(i)), None)
    ok = bad is None
    if ok:
        granted = [i for i in rep["claimed"] if i.get("claimStatus") in (None, "CLAIMED")]
        if granted:
            rep["secured"] = True
            has_fresh = any(not i.get("replayed") and i.get("grantedAt") for i in granted)
            rep["message"] = ("今日 100 Credits 已到账（本轮已领取）" if has_fresh
                              else "今日 100 Credits 已到账（本账号此前已领，回执 replayed）")
        else:
            rep["message"] = ("上游拒发：该设备身份今日已被领过（SAME_PERSON_ALREADY_CLAIMED），"
                              "未到账；检查账号池 machineId 是否与他人或本机客户端共用"
                              "（_isolate_pool_devices 保证每号独立）")
    else:
        detail = bad.get("message") or bad.get("failureCode") or ""
        if bad["status"] not in (200, 201, 204):
            rep["error"] = "claim HTTP %s%s" % (bad["status"],
                                                (": " + detail) if detail else "")
        else:
            rep["error"] = detail or ("claim 回执 status=%s" % (bad.get("claimStatus") or "?"))
    return ok, rep



# ── 账号池 ────────────────────────────────────────────────────────────────────
_pool = {"accounts": [], "mtime": 0.0}
_pool_lock = threading.RLock()
_rr = 0
_last_sweep = [0.0]
_sweep_lock = threading.Lock()      # "判间隔 + 记下这次扫了" 必须同锁：ThreadingHTTPServer 一请求一线程，
#                                    分开写会让两个线程都以为自己该扫，同一批号被并发刷两遍
_refreshing = set()                 # 正在续期的账号标签（refresh_token 是一次性轮换，双刷必有一败）
_refreshing_lock = threading.Lock()


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
        # 给每个号补 realm（缺省按 domain 推断，再缺省 DEFAULT_REALM）
        for a in _pool["accounts"]:
            if not a.get("realm"):
                a["realm"] = _realm_of(a)
        _pool["mtime"] = mtime
        # 默认每个号一套设备身份；发出来的必须落盘，否则每次重启都换一套
        if _isolate_pool_devices(_pool["accounts"]):
            _save_pool_locked()
    return _pool["accounts"]


def _chmod_secret(path):
    """凭证文件收成 0600。Windows 没有 POSIX 权限位，失败就跳过。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _save_pool_locked():
    """写盘（tmp + replace）。池里是真 token：tmp 直接以 0600 创建，
    否则默认 umask 会让每次自动续期都把文件从 600 降成 644——公网机上等于任何本机用户可读。"""
    payload = {"version": 1, "accounts": _pool["accounts"]}
    tmp = ACCOUNTS_FILE + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(ACCOUNTS_FILE)), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)
    _chmod_secret(ACCOUNTS_FILE)
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
        _isolate_pool_devices(accs)
        _save_pool_locked()
        return label


def _pooled_account(label, fallback=None):
    """按标签读池里那条真正落盘的记录（入池时设备身份可能被重发过，展示要用落盘后的值）。"""
    with _pool_lock:
        for a in _load_pool_locked():
            if _label_of(a) == label:
                return a
    return fallback or {}


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
        _log("提示：本机没有 machine_id（%s），入池时会给这个号发一套独立设备身份"
             % MACHINE_ID_FILE)
    acct = _enrich_account(acct)
    if not acct.get("uid"):
        acct["uid"] = user.get("id") or ""
    acct["account"] = "uid-" + (acct.get("uid") or "")[:8]
    acct["session_seed"] = acct["account"]
    return acct


# ── 设备码登录（--login：浏览器授权换 token，不需要装客户端）──────────────────
class LoginError(Exception):
    pass


def _device_login_argv(machine, site=None, client_id=None, realm=None):
    """组 sidecar 的一次性 --login 命令行。

    没有客户端登录态时 machine 参数直接省略（sidecar 自己生成 uuid），这样
    全新机器 / 纯 Linux 容器也能 --login。
    realm：按区域选签名器 bundle（cn→qoder_sign.mjs / intl→qoder_intl_sign.mjs）、
    openapi host、授权页 base；缺省用全局 SIGN_JS/OPENAPI/SITE（测试 mock 兼容）。
    """
    sign_js = _sign_js_path(realm) if realm else SIGN_JS
    openapi = _oa(realm) if realm else OPENAPI
    site_base = site or (_site_url(realm) if realm else SITE)
    argv = [NODE, sign_js, "--login", "--site", site_base, "--openapi", openapi]
    if machine.get("machineId"):
        argv += ["--machine-id", machine["machineId"]]
    if machine.get("machineToken"):
        argv += ["--machine-token", machine["machineToken"]]
    if client_id:
        argv += ["--client-id", client_id]
    return argv


def _account_from_device_token(dev, machine=None, realm=None):
    """设备码轮询回来的 JSON → 池内账号 dict。

    上游给的是 snake_case，客户端自己归一化成 camelCase，两种都要吃下；
    只存显示名和 id，不存邮箱等 PII。
    realm：登录时指定的区域（cn/intl），写入账号字段供按 realm 路由；缺省 DEFAULT_REALM。
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
    r = (realm or dev.get("realm") or DEFAULT_REALM).strip().lower()
    if r not in REALM_CONFIGS:
        r = DEFAULT_REALM
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
        "realm": r,
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


def _cmd_login(_args=None, popen=None, open_url=None, new_device=False, realm=None):
    """设备码登录：打印/打开授权链接 → sidecar 轮询 → token 入池。
    new_device=True 时强制生成新设备 ID（不用本机的），用于多账号隔离。
    realm：按区域选签名器 bundle 和授权页（cn/intl）；缺省 DEFAULT_REALM。"""
    realm = (realm or getattr(_args, "realm", None) or DEFAULT_REALM).strip().lower()
    if realm not in REALM_CONFIGS:
        realm = DEFAULT_REALM
    site = getattr(_args, "site", None) or _site_url(realm)
    client_id = getattr(_args, "client_id", None)
    machine = {} if new_device else _machine_identity()
    spawn = popen or (lambda argv, **kw: subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, cwd=HERE))
    opener = open_url or webbrowser.open
    try:
        proc = spawn(_device_login_argv(machine, site=site, client_id=client_id, realm=realm))
    except Exception as e:
        _print_err("启动签名器失败: %s" % e)
        _print_err("--login 需要 node 和 %s"
                   "（可用 QODER_PROXY_NODE / QODER_SIGN_JS 指定路径）"
                   % get_realm_config(realm)["sign_js"])
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
    acct = _account_from_device_token(dev, machine, realm=realm)
    if not acct:
        _print_err("授权结果里没有 token，请重新 --login")
        return 1
    try:
        acct = _enrich_account(acct)       # 补 uid/组织标签（签名要用）；拿不到不影响登录
    except Exception as e:
        _log("补全账号信息失败: %s" % e)
        _print_err("提示：没取到组织信息，如果上游报 403 请重新 --login")
    label = _add_session_to_pool(acct)
    row = _pooled_account(label, acct)
    print("已入池: %s" % label)
    print("  token      %s" % _redact(row.get("token") or acct.get("token")))
    print("  有效期至   %s" % (row.get("expiresAt") or acct.get("expiresAt") or "unknown"))
    print("  可续期     %s" % ("是" if (row.get("refreshToken") or acct.get("refreshToken"))
                               else "否"))
    print("  设备身份   %s（机器 ID %s）" % (_device_state(row),
                                          _redact(row.get("machineId"), 8) or "-"))
    print("  凭证文件   %s" % ACCOUNTS_FILE)
    if not _refresh_device_fields(row).get("machine_token"):
        _print_err("提示：这个号没有 machine_token，自动续期可能被拒；"
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


def _acct_cooling(a, now):
    """429 限流后的临时冷却期，期内不参与轮询，也不去做续期。"""
    until = a.get("cool_until")
    return bool(until) and now < until


def _next_account(realm=None):
    """轮询取一个可用账号；全不可用时退回第一个（让上游告诉我们到底行不行）。
    realm：按区域过滤账号（cn/intl 各自轮询）；缺省取全部。

    指针自增必须在 _pool_lock 里：代理是 ThreadingHTTPServer，一个请求一个线程，
    锁外自增会让两个线程算出同一个 _rr、拿到同一个号，配额分布就歪了。
    """
    global _rr
    with _pool_lock:
        accs = list(_load_pool_locked())
        if realm:
            accs = [a for a in accs if _realm_of(a) == realm]
        if not accs:
            return None
        now = time.time()
        healthy = [a for a in accs
                   if not _pool_expired(a, now)
                   and not (a.get("failed_at") and now - a["failed_at"] < FAIL_COOLDOWN_SEC)
                   and not _acct_cooling(a, now)]
        cand = healthy or accs
        _rr = (_rr + 1) % len(cand)
        return cand[_rr]


# ── token 续期 ────────────────────────────────────────────────────────────────
def _refresh_account_once(acct, force=False):
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
    body.update(_refresh_device_fields(acct))
    st, js = _openapi_json("POST", REFRESH_PATH, body, realm=_realm_of(acct))
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
    with _sweep_lock:
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
        if _acct_cooling(a, now):
            continue
        if a.get("exp") and a["exp"] - now < REFRESH_AHEAD_SEC:
            _refresh_account(a)


def _refresh_account(acct, force=False):
    """按账号标签单飞的续期入口：`_read_session` 选中路上的号和 sweep 巡到的同一个号
    可能同时想刷，而 refresh_token 用一次就作废——败方会被判成"链已死"。同一标签只放一个进去。"""
    label = _label_of(acct)
    with _refreshing_lock:
        if label in _refreshing:
            return acct
        _refreshing.add(label)
    try:
        return _refresh_account_once(acct, force)
    finally:
        with _refreshing_lock:
            _refreshing.discard(label)


def _read_session(realm=None):
    """取一个可发请求的账号：轮询 + 临期自动续期 + 过期账号按需续一次。
    realm：按区域选账号（cn/intl 各自轮询）；缺省取全部。"""
    _sweep_due()
    acct = _next_account(realm)
    if acct is None:
        return None
    now = time.time()
    if acct.get("exp") and acct["exp"] - now < REFRESH_AHEAD_SEC:
        acct = _refresh_account(acct)
    return acct


# ── Cosy 签名与请求体编码（纯 Python，逐字节还原自客户端 WASM）───────────────
# 原为同目录的 qoder_cosy.py，为了让整个代理单文件即可部署而内联到此处。
# 常量取自客户端 qoder_auth_wasm_bg.wasm 的只读数据段（见 .reverse/qoder/）。
# 注意：这里的 GATEWAY / INFER_PATH / INFER_QUERY 是 "/algo" 之前的相对形态，与文件
# 顶部那组同名常量取值不同，所以带 _COSY_ 前缀，别改回去。
COSY_VERSION = "1.1.69"
_COSY_GATEWAY = "https://api3.qoder.sh"
_COSY_INFER_PATH = "/api/v2/service/pro/sse/agent_chat_generation"
_COSY_INFER_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"

# wasm 只读数据段偏移 0x103618 的 1024 位公钥模数（e=65537）
RSA_N = int(
    "c0f22307e5cd362e296bb04470f6de8fbf935ce24e8fcf511a0e2701329769c4"
    "a76e499bb938036a52af1eaf818cf79a2600620e3ce87e371d2ca6d85803606a"
    "1b3fa5e874643c9ed2db7e85673ef7227fca56e2e7c08f0927609bb896a9f24b"
    "e1782099a66016a5bfdc3f1ff756bfc9e88d7b5dc5be30bf45a0223a00ebcecf", 16)
RSA_E = 65537
RSA_LEN = 128
AES_KEY_LEN = 16

BODY_ALPHABET = b"_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
BODY_PAD = 0x24  # '$'


# ── 请求体编码（Encode=1）────────────────────────────────────────────────────
def _b64_custom(data):
    n = len(data)
    out = bytearray()
    full = n - n % 3
    tbl = BODY_ALPHABET
    for i in range(0, full, 3):
        c0, c1, c2 = data[i], data[i + 1], data[i + 2]
        out.append(tbl[c0 >> 2])
        out.append(tbl[((c0 & 3) << 4) | (c1 >> 4)])
        out.append(tbl[((c1 & 15) << 2) | (c2 >> 6)])
        out.append(tbl[c2 & 63])
    rem = n - full
    if rem == 1:
        c0 = data[full]
        out += bytes((tbl[c0 >> 2], tbl[(c0 & 3) << 4], BODY_PAD, BODY_PAD))
    elif rem == 2:
        c0, c1 = data[full], data[full + 1]
        out += bytes((tbl[c0 >> 2], tbl[((c0 & 3) << 4) | (c1 >> 4)],
                      tbl[(c1 & 15) << 2], BODY_PAD))
    return bytes(out)


def _reorder(raw):
    # wasm f182: 把编码结果按 1/3 切成三段，尾段提到最前
    n = len(raw)
    if n == 0:
        return raw
    a = n // 3
    b = n - a
    return raw[b:] + raw[a:b] + raw[:a]


def encode_body(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _reorder(_b64_custom(data))


# ── AES-128-CBC（仅加密；PKCS#7）────────────────────────────────────────────
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mix(a):
    t = a[0] ^ a[1] ^ a[2] ^ a[3]
    u = a[0]
    return (
        a[0] ^ t ^ _xtime(a[0] ^ a[1]),
        a[1] ^ t ^ _xtime(a[1] ^ a[2]),
        a[2] ^ t ^ _xtime(a[2] ^ a[3]),
        a[3] ^ t ^ _xtime(a[3] ^ u),
    )


class _AES128:
    def __init__(self, key):
        if len(key) != AES_KEY_LEN:
            raise ValueError("AES-128 key 必须 16 字节")
        w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
        for i in range(4, 44):
            t = list(w[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[x] for x in t]
                t[0] ^= _RCON[i // 4 - 1]
            w.append([w[i - 4][j] ^ t[j] for j in range(4)])
        self.rk = [sum(w[i:i + 4], []) for i in range(0, 44, 4)]

    def encrypt_block(self, block):
        s = [block[i] ^ self.rk[0][i] for i in range(16)]
        for rnd in range(1, 11):
            s = [_SBOX[x] for x in s]
            # ShiftRows（列主序）
            s = [
                s[0], s[5], s[10], s[15],
                s[4], s[9], s[14], s[3],
                s[8], s[13], s[2], s[7],
                s[12], s[1], s[6], s[11],
            ]
            if rnd != 10:
                for c in range(4):
                    s[4 * c:4 * c + 4] = _mix(s[4 * c:4 * c + 4])
            k = self.rk[rnd]
            s = [s[i] ^ k[i] for i in range(16)]
        return bytes(s)


def aes128_cbc_encrypt(key, iv, data):
    if len(iv) != 16:
        raise ValueError("IV 必须 16 字节")
    pad = 16 - len(data) % 16
    data = data + bytes([pad]) * pad
    aes = _AES128(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = aes.encrypt_block(block)
        out += prev
    return bytes(out)


# ── RSA PKCS#1 v1.5（只做公钥加密）──────────────────────────────────────────
def _rsa_encrypt(msg, rng=os.urandom):
    ps_len = RSA_LEN - len(msg) - 3
    if ps_len < 8:
        raise ValueError("RSA 明文过长")
    ps = bytearray()
    while len(ps) < ps_len:
        ps.extend(b for b in rng(ps_len * 2) if b != 0)
    em = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + msg
    c = pow(int.from_bytes(em, "big"), RSA_E, RSA_N)
    return c.to_bytes(RSA_LEN, "big")


def generate_runtime_auth_fields(account_json, rng=os.urandom):
    """入参与客户端一致：{uid, security_oauth_token, organization_id,
    organization_tags, data_policy_agreed} 的 JSON 字符串。"""
    if isinstance(account_json, dict):
        account_json = json.dumps(account_json, ensure_ascii=False, separators=(",", ":"))
    raw = account_json.encode("utf-8")
    uid_hex = uuid.UUID(bytes=bytes(rng(16)), version=4).hex
    m = uid_hex[:16].encode("ascii")
    key = base64.b64encode(_rsa_encrypt(m, rng)).decode("ascii")
    info = base64.b64encode(aes128_cbc_encrypt(m, m, raw)).decode("ascii")
    return {"encrypt_user_info": info, "key": key}


# ── COSY 头 ──────────────────────────────────────────────────────────────────
def _b64(s):
    if isinstance(s, str):
        s = s.encode("utf-8")
    return base64.b64encode(s).decode("ascii")


def _norm_path(path):
    p = path or "/"
    q = ""
    if "?" in p:
        p, q = p.split("?", 1)
    if p.startswith("/algo/"):
        p = p[len("/algo"):]
    elif p == "/algo":
        p = "/"
    return p, q


class CosyContext:
    """对应客户端的 QoderContext：机器标识 + 账号加密字段。"""

    def __init__(self, uid, machine_id, cosy_version, fields,
                 data_policy="disagree", rng=os.urandom, clock=time.time):
        self.uid = uid or ""
        self.machine_id = machine_id or ""
        self.cosy_version = cosy_version or COSY_VERSION
        self.info = fields["encrypt_user_info"]
        self.key = fields["key"]
        self.data_policy = data_policy
        self.rng = rng
        self.clock = clock

    @classmethod
    def from_account(cls, auth, rng=os.urandom, clock=time.time):
        """auth: {token,uid,machineId,cosyVersion,organizationId,organizationTags,dataPolicyAgreed}"""
        account_json = json.dumps({
            "uid": auth.get("uid") or "",
            "security_oauth_token": auth.get("token") or "",
            "organization_id": auth.get("organizationId") or "",
            "organization_tags": auth.get("organizationTags") or [],
            "data_policy_agreed": bool(auth.get("dataPolicyAgreed")),
        }, ensure_ascii=False, separators=(",", ":"))
        fields = generate_runtime_auth_fields(account_json, rng=rng)
        policy = "agree" if auth.get("dataPolicyAgreed") else "disagree"
        return cls(auth.get("uid") or "", auth.get("machineId") or "",
                   auth.get("cosyVersion") or COSY_VERSION, fields,
                   data_policy=policy, rng=rng, clock=clock)

    def _sign(self, path, body_line):
        date = str(int(self.clock()))
        rid = str(uuid.UUID(bytes=bytes(self.rng(16)), version=4))
        part0 = json.dumps({
            "version": "v1",
            "requestId": rid,
            "info": self.info,
            "cosyVersion": self.cosy_version,
            "ideVersion": "",
        }, ensure_ascii=False, separators=(",", ":"))
        p0 = _b64(part0)
        pre = "\n".join((p0, self.key, date, body_line, path))
        sig = hashlib.md5(pre.encode("utf-8")).hexdigest()
        return p0, sig, date

    def _base_headers(self):
        return {
            "Cosy-User": self.uid,
            "Cosy-Key": self.key,
            "Cosy-MachineId": self.machine_id,
            "Cosy-MachineToken": self.machine_id,
            "Cosy-MachineType": "5",
            "Cosy-ClientType": "5",
            "Cosy-Business-Product": "cli",
            "Cosy-Business-Type": "agent",
            "Cosy-Scene": "assistant",
            "Cosy-Version": self.cosy_version,
            "Login-Version": "v2",
            "Cosy-Data-Policy": self.data_policy,
            "Content-Type": "application/json",
        }

    def prepare_infer_request(self, endpoint, body, model, source):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encoded = encode_body(body).decode("ascii")
        path = _COSY_INFER_PATH
        p0, sig, date = self._sign(path, encoded)
        headers = self._base_headers()
        headers.update({
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Model-Key": model,
            "X-Model-Source": source,
            "Cosy-Date": date,
            "Authorization": "Bearer COSY.%s.%s" % (p0, sig),
        })
        base = (endpoint or _COSY_GATEWAY).rstrip("/")
        return {"url": base + "/algo" + path + "?" + _COSY_INFER_QUERY,
                "headers": headers, "body": encoded}

    def prepare_request(self, endpoint, path, method="GET", auth_type="auth",
                        body=None, extra_headers=None):
        rel, query = _norm_path(path)
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encoded = encode_body(body).decode("ascii") if body is not None else ""
        p0, sig, date = self._sign(rel, encoded)
        headers = self._base_headers()
        headers.update({"Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Cosy-ClientIp": self.machine_id})
        headers["Cosy-Date"] = date
        headers["Authorization"] = "Bearer COSY.%s.%s" % (p0, sig)
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        url = (endpoint or _COSY_GATEWAY).rstrip("/") + "/algo" + rel
        if query:
            url += "?" + query
        out = {"url": url, "headers": headers}
        if body is not None:
            out["body"] = encoded
        return out


# ── 签名 sidecar（qoder_intl_sign.mjs）────────────────────────────────────────────
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
            raise SignerError("找不到 node（%s）。Qoder 国际版的签名在客户端 WASM 里，"
                              "需要 Node 运行 qoder_intl_sign.mjs" % node)
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
    """纯 Python 签名器：本文件 Cosy 段落复刻的 WASM 算法，不需要 node。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self.last_info = None

    @staticmethod
    def _key(auth):
        return (auth.get("uid") or "", auth.get("token") or "",
                auth.get("machineId") or "",
                auth.get("cosyVersion") or COSY_VERSION,
                auth.get("organizationId") or "",
                tuple(auth.get("organizationTags") or []),
                bool(auth.get("dataPolicyAgreed")))

    def _ctx(self, auth):
        key = self._key(auth)
        with self._lock:
            ctx = self._cache.get(key)
            if ctx is None:
                ctx = CosyContext.from_account(auth)
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
                          "cosyVersion": COSY_VERSION, "wasmReady": True}
        return self.last_info

    def info(self):
        return self.peek()

    @staticmethod
    def describe():
        return "纯 Python（内联 Cosy 实现，不需要 node）"


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
            return "qoder_intl_sign.mjs（node 子进程）"
        if self.mode == "py":
            return self._native.describe()
        return "纯 Python 优先（内联 Cosy 实现；node 仅回落）"


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
# 上游模型缺 context_config 且 max_input_tokens=0 时的上下文兜底（对齐 catalog 默认 200K）
DEFAULT_CTX_FALLBACK = 200000


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
            if ctx == 0:
                ctx = DEFAULT_CTX_FALLBACK
            out.append({
                "key": m["key"],
                "source": m.get("source") or "system",
                "name": m.get("display_name") or m["key"],
                "reasoning": bool(m.get("is_reasoning")),
                "vision": bool(m.get("is_vl")),
                "default": bool(m.get("is_default")),
                "enabled": bool(m.get("enable", True)),
                "ctx": ctx,
                "max_output_tokens": int(m.get("max_output_tokens") or 0),
                "price_factor": m.get("price_factor"),
                "format": m.get("format") or "openai",
                "scene": scene,
                "thinking_config": m.get("thinking_config")
                if isinstance(m.get("thinking_config"), dict) else None,
            })
    return out


def _fetch_models(force=False):
    """签名拉 /algo/api/v2/model/list，缓存 MODELS_TTL 秒。
    双区合并：分别从 cn/intl 各拉一次，按 key 去重，每个模型带 realm 字段。
    单个 realm 失败（无账号 / HTTP 非 200 / 解析异常）不中断，只有全部失败才抛。"""
    now = time.time()
    if not force and _models["list"] and now - _models["at"] < MODELS_TTL:
        return _models
    all_models = []
    seen_keys = set()
    key_realms = {}
    for realm in ("cn", "intl"):
        acct = _read_session(realm)
        if acct is None:
            _log("模型清单 %s 跳过：无可用账号" % realm)
            continue
        params = {"endpoint": _gw(realm), "path": MODEL_LIST_PATH, "method": "GET"}
        try:
            status, resp = _signed_send(acct, params)
            try:
                raw = resp.read().decode("utf-8", "replace")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        except Exception as e:
            _log("模型清单 %s 拉取失败: %s" % (realm, e))
            continue
        if status != 200:
            _log("模型清单 %s HTTP %s: %s" % (realm, status, raw[:200]))
            continue
        try:
            lst = _normalize_models(json.loads(raw))
        except Exception as e:
            _log("模型清单 %s 解析失败: %s" % (realm, e))
            continue
        for m in lst:
            m["realm"] = realm
            key_realms.setdefault(m["key"], set()).add(realm)
            if m["key"] not in seen_keys:
                seen_keys.add(m["key"])
                all_models.append(m)
        _log("模型清单 %s: %d 个" % (realm, len(lst)))
    if not all_models:
        raise RuntimeError(
            "模型清单为空：cn/intl 均无可用账号或上游不可达，先运行 python qoder_proxy.py --capture")
    for m in all_models:
        m["realms"] = sorted(key_realms.get(m["key"], {m["realm"]}))
    _models["at"] = now
    _models["list"] = all_models
    _models["by_id"] = {m["key"]: m for m in all_models}
    alias = {}
    for m in all_models:
        alias[m["key"].lower()] = m
        alias[_slug(m["name"])] = m
    _models["alias"] = alias
    _log("模型清单合并 %d 个（cn+intl）" % len(all_models))
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
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "qoder-intl-proxy:" + seed))


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


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return ""


def _build_infer_payload(oa, model, acct):
    """国际版网关的 v3 请求体（对齐客户端 bundle 里的 y4e 构造）。

    和 CN 版不同，国际版必须带 chat_task / chat_context / agent_id / version，
    否则会被 agent_router 以 "flow nodes found" 拒绝（HTTP 200 信封里的 400）。
    """
    mid = str(uuid.uuid4())
    session_id = _session_id(acct)
    messages = oa.get("messages") or []
    think_effort, think_params = _thinking_upstream(oa, model)
    is_reasoning = bool(model.get("reasoning")) and think_effort != "none"

    user_text = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_text = _content_text(m.get("content"))
            break
    system_text = "\n\n".join(
        _content_text(m.get("content")) for m in messages
        if isinstance(m, dict) and m.get("role") == "system")

    params = {}
    for key in ("temperature", "top_p", "tool_choice"):
        if oa.get(key) is not None:
            params[key] = oa[key]
    max_tokens = oa.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = int(model.get("max_output_tokens") or 0) or 32000
    params["max_tokens"] = max_tokens
    base = oa.get("parameters")
    if isinstance(base, dict):
        params.update(base)
    if think_params:
        params.update(think_params)

    return {
        "request_id": mid,
        "request_set_id": mid,
        "chat_record_id": mid,
        "session_id": session_id,
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {
            "text": user_text,
            "features": [],
            "extra": {
                "context": [],
                "modelConfig": {"key": model["key"], "is_reasoning": is_reasoning},
                "originalContent": user_text,
            },
            "chatPrompt": "",
            "imageUrls": None,
        },
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "agent_id": "agent_common",
        "task_id": "common",
        "session_type": SESSION_TYPE,
        "aliyun_user_type": "",
        "model_config": {
            "key": model["key"],
            "display_name": model.get("name") or model["key"],
            "model": "",
            "format": model.get("format") or "openai",
            "is_vl": bool(model.get("vision")),
            "is_reasoning": is_reasoning,
            "api_key": "",
            "url": "",
            "source": model.get("source") or "system",
            "max_input_tokens": int(model.get("ctx") or 0) or 200000,
        },
        "system": system_text,
        "messages": messages,
        "tools": oa.get("tools") or [],
        "parameters": params,
        "business": {"type": BUSINESS_TYPE, "product": BUSINESS_PRODUCT},
    }


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


# ── 协议层：与 codearts_proxy.py 共用 llm_proxy_core，不再各写一份 ──────────────
# 保留原有函数名，是为了让既有调用点和 test_qoder_intl_proxy.py 的用例继续按老名字打。
# aggregate_sse 取"恒返回 dict"那个入口：_complete() 直接 merged["choices"][0]，不能给它 None。
aggregate_sse = C.aggregate_sse
convert_anthropic_to_openai = C.anthropic_to_openai
convert_openai_to_anthropic = C.openai_to_anthropic
convert_openai_to_responses = C.openai_to_responses
convert_responses_to_openai = C.responses_to_openai
error_of = _error_of = C.error_of
finish_to_anthropic = _finish_to_anthropic = C.finish_to_anthropic
merge_tool_name = _merge_tool_name = C.merge_tool_name
parse_anthropic_text = C.anthropic_text
tools_to_openai = _tools_to_openai = C.tools_to_openai
tool_choice_to_openai = _tool_choice_to_openai = C.tool_choice_to_openai
AnthropicStreamState = _AnthropicStreamState = C.AnthropicStreamState
ResponsesStreamState = _ResponsesStreamState = C.ResponsesStreamState


# ── HTTP Handler ─────────────────────────────────────────────────────────────
class ProxyError(Exception):
    """给客户端的错误（带 HTTP 状态码）。"""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def _map_upstream_error(status, text=""):
    s = str(text or "").lower()
    # 上游把业务限制塞进 SSE 首帧（HTTP 200 + statusCodeValue 403），内层还是 JSON：
    # 110=Billing daily count exceeded（该号当日请求数用尽），10605/isQueued=模型排队
    # 限流（带 retryAfterSeconds），112/pricingUrl=额度用尽。都不是鉴权问题，
    # 映成 401 会误导人去重新 --capture。
    if any(k in s for k in ("daily count exceeded", "isqueued", "10605",
                            '"code":"112"', "pricingurl")):
        return 429
    if status in (401, 403):
        return 401
    if status == 429:
        return 429
    if any(k in s for k in ("sensitive", "content_security", "违规", "audit")):
        return 400
    if status in (400, 404, 409, 422):
        return 400
    return 502


def _retry_after_sec(resp, default):
    """上游 Retry-After：兼容"秒数"和 HTTP-date 两种写法，读不出来退回 default。"""
    try:
        raw = str((resp.headers.get("Retry-After") or "")).strip()
    except Exception:
        return default
    if not raw:
        return default
    if raw.isdigit():
        return max(1, int(raw))
    try:
        tv = parsedate(raw)
        if tv:
            return max(1, int(calendar.timegm(tv) - time.time()))
    except Exception:
        pass
    return default


def _backoff_sleep(n):
    time.sleep(min(RETRY_BACKOFF_SEC * (2 ** (n - 1)), 4.0))


# 上游 500 不在重试之列：500 可能已经把这一轮跑完并扣了额度，重放就是二次消费。
RETRY_STATUS = (502, 503, 504)


def _open_upstream(oa, model, realm=None):
    """签名 + 发上游。

      401/403   → 标记账号失效，换号
      429       → 按 Retry-After 冷却该号，换号
      连接失败 / 502/503/504 → 指数退避后重试（全局最多 RETRY_MAX 次）
      其余 4xx  → 原样映射后抛给客户端，不重试
      SignerError → 直接 502：本地签名坏了，换号或重试都没意义

    重试只发生在拿到响应头之前，流一旦开始就不在这条路上，不会向客户端重复输出。
    realm：按区域选账号（cn/intl 各自轮询）；缺省取全部（按账号自身 realm 路由端点）。
    """
    with _pool_lock:
        n_acc = len(_load_pool_locked())
    last = None
    switches = max(2, min(n_acc, 4))
    retried = 0
    while switches > 0:
        acct = _read_session(realm)
        if acct is None:
            raise ProxyError(401, "账号池为空：先运行 python qoder_proxy.py --capture")
        params = {"endpoint": _gw(_realm_of(acct)), "method": "POST", "path": INFER_PATH,
                  "infer": {"model": model["key"], "source": model.get("source") or "system"},
                  "body": _build_infer_payload(oa, model, acct)}
        try:
            status, resp = _signed_send(acct, params)
        except SignerError as e:
            raise ProxyError(502, "签名/连接失败: %s" % e)
        except Exception as e:
            last = ProxyError(502, "签名/连接失败: %s" % e)
            if retried >= RETRY_MAX:
                raise last
            retried += 1
            _log("上游连接失败(%s)，第 %d 次退避重试" % (e, retried))
            _backoff_sleep(retried)
            continue
        if status in (401, 403):
            detail = _drain(resp)
            _mark_account_failed(acct, status)
            _log("账号 %s 上游 %s，换号: %s" % (_label_of(acct), status, detail[:120]))
            last = ProxyError(401, "上游鉴权失败(%s)，请重新 --capture：%s"
                              % (status, detail[:200]))
            switches -= 1
            continue
        if status == 429:
            detail = _drain(resp)
            delay = _retry_after_sec(resp, RATE_COOLDOWN_SEC)
            _update_account(_label_of(acct), {"cool_until": time.time() + delay,
                                              "failed_status": 429})
            _log("账号 %s 被限流(429)，冷却 %ds: %s" % (_label_of(acct), delay, detail[:120]))
            last = ProxyError(429, "上游限流(429)，账号 %s 冷却 %ds：%s"
                              % (_label_of(acct), delay, detail[:200]))
            switches -= 1
            continue
        if status in RETRY_STATUS and retried < RETRY_MAX:
            detail = _drain(resp)
            retried += 1
            last = ProxyError(_map_upstream_error(status, detail),
                              "上游 %s: %s" % (status, detail[:300]))
            _log("上游 %s，第 %d 次退避重试" % (status, retried))
            _backoff_sleep(retried)
            continue
        if status >= 400:
            detail = _drain(resp)
            raise ProxyError(_map_upstream_error(status, detail),
                             "上游 %s: %s" % (status, detail[:300]))
        return acct, resp
    raise last or ProxyError(502, "上游请求失败")


# ── 管理层：qoder_admin 集成（多 Key 鉴权 / 管理 API / 模型映射）──────────────
# qoder_admin.py 是双代理共享的管理层（纯标准库、框架无关）；本段把它的
# KeyStore / AdminApi 接进国际版代理的现有函数。约定：
#   * 账号在管理 API 里的 id = 账号标签（_label_of，即池内 account 字段）
#   * KeyStore 空 且配了 QODER_PROXY_API_KEY 时走单 Key 兼容；都没配不鉴权
#   * adapters 全部是本模块函数，上游交互复用签到/续期/签名链路

_key_store = None
_key_store_lock = threading.Lock()


def _get_key_store():
    """KeyStore 单例（懒加载；测试可整只替换 _key_store 注入）。"""
    global _key_store
    if _key_store is None:
        with _key_store_lock:
            if _key_store is None:
                _key_store = QA.KeyStore(ADMIN_KEYS_FILE)
    return _key_store


def _check_key_auth(candidates):
    """数据面鉴权判定（Handler._check_api_key 的纯逻辑部分，便于离线单测）。

    优先级：KeyStore 多 Key → 单 Key env 兼容 → 不鉴权。
    candidates 是按优先级排的候选明文（Authorization Bearer 值、X-Api-Key 值）。
    返回 (ok, status, message, err_type)：ok=True 时后三个为占位值。
    """
    try:
        has_keys = bool(_get_key_store().list())
    except Exception:
        has_keys = False
    if has_keys:
        for raw in candidates:
            if not raw:
                continue
            record, allowed = _get_key_store().lookup(raw)
            if record is not None:
                if allowed:
                    return True, 0, "", ""
                return False, 429, "当日调用次数已用尽（该 Key 的 max_calls）", \
                    "rate_limit_exceeded"
        return False, 401, "missing or invalid API key", "invalid_api_key"
    if not API_KEY:
        return True, 0, "", ""
    for raw in candidates:
        if raw and secrets.compare_digest(raw, API_KEY):
            return True, 0, "", ""
    return False, 401, "missing or invalid API key", "invalid_api_key"


# 今日请求计数（LLM 数据面）：stats 面板用；UTC 日切，进程内存，重启归零。
# total_tokens 没有计量来源，恒 0 —— 不虚构。
_req_stats = {"date": "", "requests": 0}
_req_stats_lock = threading.Lock()
# 兼容旧测试（国内版用 _usage_stats/{date:{requests,total_tokens}}，intl 版用 _req_stats；
# 这里提供空 dict + lock 别名供测试 clear，实际统计走 _req_stats/_bump_req_stats）
_usage_stats = {}
_usage_stats_lock = _req_stats_lock


def _bump_req_stats():
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _req_stats_lock:
        if _req_stats["date"] != today:
            _req_stats["date"] = today
            _req_stats["requests"] = 0
        _req_stats["requests"] += 1


def _req_stats_snapshot():
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _req_stats_lock:
        if _req_stats["date"] != today:
            return {"requests": 0, "total_tokens": 0}
        return {"requests": _req_stats["requests"], "total_tokens": 0}


# ── 模型映射（mappings 链式改写 + aliases 假名）──────────────────────────────
_model_routes = {"mappings": {}, "aliases": {}, "mtime": 0.0}
_model_routes_lock = threading.RLock()


def _norm_route_map(obj):
    """路由表规范化：非 dict → {}；键值转 str、去空白、丢空键/空值。"""
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        ks, vs = str(k).strip(), str(v).strip()
        if ks and vs:
            out[ks] = vs
    return out


def _load_model_routes_locked():
    """路由文件热加载（mtime 变了重读；手工编辑与管理 API 写盘都走得通）。"""
    try:
        mtime = os.path.getmtime(MODEL_ROUTES_FILE)
    except Exception:
        _model_routes.update(mappings={}, aliases={}, mtime=0.0)
        return _model_routes
    if mtime != _model_routes["mtime"]:
        data = _read_json_file(MODEL_ROUTES_FILE) or {}
        mappings = data.get("mappings") if isinstance(data, dict) else None
        aliases = data.get("aliases") if isinstance(data, dict) else None
        _model_routes["mappings"] = _norm_route_map(mappings)
        _model_routes["aliases"] = _norm_route_map(aliases)
        _model_routes["mtime"] = mtime
    return _model_routes


def _save_model_routes_locked():
    """原子写路由文件（tmp + replace + 0600，参照 _save_pool_locked）。"""
    tmp = MODEL_ROUTES_FILE + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(MODEL_ROUTES_FILE)), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mappings": _model_routes["mappings"],
                   "aliases": _model_routes["aliases"]},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, MODEL_ROUTES_FILE)
    try:
        _model_routes["mtime"] = os.path.getmtime(MODEL_ROUTES_FILE)
    except Exception:
        pass


def _route_model_name(raw):
    """模型名路由：aliases 先（一次替换）、mappings 后（链式跟进，防环 8 跳）。
    未命中原样透传；纯本地查表，不依赖模型清单、不触网。"""
    name = str(raw or "").strip()
    if not name:
        return raw
    with _model_routes_lock:
        routes = _load_model_routes_locked()
        routed = routes["aliases"].get(name, name)
        seen = set()
        for _ in range(8):        # 链深上限：到顶即停，环不会死循环
            nxt = routes["mappings"].get(routed)
            if nxt is None or routed in seen:
                break
            seen.add(routed)
            routed = nxt
    return routed


def _model_route_entries(upstream_list, created):
    """/v1/models 的附加条目：aliases 假名与 mappings 源名（客户端可请求的
    额外名字），元数据取"路由后最终目标"的模型（假名常指向中间名，再经
    mappings 链落到真实上游模型）；最终目标不在上游清单的悬空映射不虚构条目。"""
    with _model_routes_lock:
        routes = _load_model_routes_locked()
        alias_map = dict(routes["aliases"])
        mapping_map = dict(routes["mappings"])
    base_by_key = {m["key"].lower(): m for m in upstream_list}
    entries, seen = [], set()
    for name, kind in ([(a, "alias") for a in alias_map]
                       + [(m, "mapping") for m in mapping_map]):
        low = str(name).lower()
        if low in seen or low in base_by_key:
            continue
        base = base_by_key.get(str(_route_model_name(name)).lower())
        if base is None:
            continue          # 悬空映射：最终目标不在上游清单，不虚构条目
        entries.append({
            "id": name, "object": "model", "created": created,
            "owned_by": "qoder-" + base.get("realm", "cn"), "name": base["name"],
            "context_window": base["ctx"], "reasoning": base["reasoning"],
            "vision": base["vision"], "realm": base.get("realm", "cn"),
            "metadata": {"display_name": base["name"], "source": kind,
                         "target": base["key"], "realm": base.get("realm", "cn")}})
        seen.add(low)
    return entries


# ── AdminApi adapters（把现有函数接进 qoder_admin.AdminApi）─────────────────

def _admin_account_row(a, counts=None, now=None):
    """账号公开视图（admin 页面 renderAccounts 需要的字段；不回显 token）。"""
    label = _label_of(a)
    exp = a.get("exp")
    now = time.time() if now is None else now
    return {
        "id": label,
        "label": label,
        "uid": a.get("uid") or "",
        "realm": a.get("realm") or _realm_of(a),
        "expires_at": a.get("expiresAt") or "",
        "exp": exp,
        "left_days": round((exp - now) / 86400, 1) if exp else None,
        "has_refresh": bool(a.get("refreshToken")),
        "refresh_dead": bool(a.get("refresh_dead")),
        "failed_at": a.get("failed_at") or 0,
        "failed_status": a.get("failed_status"),
        "machine_id": _redact(a.get("machineId"), 6),
        "device": _device_state(a, counts if counts is not None else {}),
    }


def _admin_list_accounts():
    with _pool_lock:
        accs = list(_load_pool_locked())
    counts = _device_counts(accs)
    now = time.time()
    return [_admin_account_row(a, counts, now) for a in accs]


def _admin_latest_checkins():
    """签到摘要（读签到状态文件）。状态文件按天存全池汇总（不分号），按此汇报，
    不虚构分号明细；过期日期不展示。"""
    st = _read_checkin_state()
    today = time.strftime("%Y-%m-%d")
    if not isinstance(st, dict) or st.get("date") != today:
        return []
    return [{
        "account_id": "*",
        "label": "全池汇总",
        "secured": bool(st.get("done")),
        "status": "ok" if st.get("last_all_ok") else "failed",
        "amount": 100 * int(st.get("secured_accounts") or 0),
        "add_on_total": None,
        "message": "已到位 %s/%s（尝试 %s 次，done=%s）" % (
            st.get("secured_accounts"), st.get("pool_accounts"),
            st.get("attempts"), st.get("done")),
        "updated_at": st.get("last_at"),
    }]


def _admin_import_accounts(items):
    """导入账号（token 必填）；同标签覆盖旧记录（_add_session_to_pool 语义）。"""
    imported = updated = skipped = 0
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get("token") or "").strip():
            skipped += 1
            out.append({"error": "missing token"})
            continue
        acct = dict(it)
        label = _label_of(acct)
        with _pool_lock:
            existed = any(_label_of(a) == label for a in _load_pool_locked())
        final = _add_session_to_pool(acct)
        if existed:
            updated += 1
            action = "updated"
        else:
            imported += 1
            action = "imported"
        out.append({"id": final, "label": final, "action": action})
    return {"imported": imported, "updated": updated, "skipped": skipped,
            "items": out}


_ADMIN_ACCOUNT_READONLY = ("account", "session_seed")


def _admin_patch_account(aid, fields):
    """按标签改账号字段；account/session_seed 由入池逻辑管理，不接受外部改。"""
    if not isinstance(fields, dict):
        return None
    patch = {k: v for k, v in fields.items()
             if k not in _ADMIN_ACCOUNT_READONLY}
    with _pool_lock:
        accs = _load_pool_locked()
        hit = next((a for a in accs if _label_of(a) == aid), None)
    if hit is None:
        return None
    if patch:
        _update_account(aid, patch)
    merged = _pooled_account(aid)
    if not merged:
        return None
    return _admin_account_row(merged)


def _admin_delete_account(aid):
    """幂等删除（对齐 _cmd_remove 的池写回，但不打印）。"""
    with _pool_lock:
        accs = _load_pool_locked()
        keep = [a for a in accs if _label_of(a) != aid]
        if len(keep) != len(accs):
            _pool["accounts"] = keep
            _save_pool_locked()


def _admin_test_account(aid):
    """轻量上游探测：用该号签名拉一次模型清单。失败抛 KeyStoreError(status=502)，
    AdminApi.handle 会带 status 回给客户端。"""
    acct = _pooled_account(aid)
    if not acct.get("token"):
        return None
    params = {"endpoint": _gw(_realm_of(acct)), "path": MODEL_LIST_PATH, "method": "GET"}
    try:
        status, resp = _signed_send(acct, params)
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception as e:
        raise QA.KeyStoreError("账号测试失败: %s" % e, 502)
    if status != 200:
        raise QA.KeyStoreError("账号测试失败: 上游 HTTP %s" % status, 502)
    try:
        models = _normalize_models(json.loads(raw))
    except Exception as e:
        raise QA.KeyStoreError("账号测试失败: 模型清单解析失败 %s" % e, 502)
    return {"ok": True, "models": len(models)}


def _admin_refresh_account(aid):
    """手动续期（force=True：按"续期"按钮语义直接刷，不看临期阈值）。"""
    acct = _pooled_account(aid)
    if not acct.get("token"):
        return None
    if not acct.get("refreshToken"):
        return {"ok": False, "expires_at": acct.get("expiresAt") or "",
                "refresh_dead": bool(acct.get("refresh_dead")),
                "error": "该账号没有 refreshToken，无法续期"}
    merged = _refresh_account(acct, force=True)
    out = {
        "ok": not merged.get("refresh_dead"),
        "expires_at": merged.get("expiresAt") or "",
        "refresh_dead": bool(merged.get("refresh_dead")),
    }
    if merged.get("refresh_error"):
        out["error"] = merged["refresh_error"]
    return out


def _admin_checkin_account(aid):
    """单号签到：回执 rep 含 secured/message（页面 toast 直接展示）。"""
    acct = _pooled_account(aid)
    if not acct.get("token"):
        return None
    ok, rep = _checkin_account(acct)
    return {"ok": bool(ok), "rep": rep}


def _admin_stats():
    with _pool_lock:
        n_accounts = len(_load_pool_locked())
    return {
        "today": _req_stats_snapshot(),
        "trend": [],                       # 本地代理没有历史聚合，不虚构
        "accounts": n_accounts,
        "models": len(_models["list"]),
        "checkins": _admin_latest_checkins(),
        "gateway": GATEWAY,
        "port": PORT,
    }


def _admin_list_models():
    try:
        cached = _fetch_models()
    except Exception as e:
        raise QA.KeyStoreError("模型清单不可用: %s" % e, 502)
    rows = [{"id": m["key"], "name": m["name"], "context_length": m["ctx"],
             "max_output_tokens": m["max_output_tokens"],
             "reasoning": m["reasoning"], "vision": m["vision"],
             "realm": m.get("realm", "cn"),
             "realms": m.get("realms", [m.get("realm", "cn")])}
            for m in cached["list"]]
    # updated_at 用毫秒（对齐页面 fmtTime(Math.floor(v/1000)) 的口径）
    return {"models": rows, "updated_at": int(_models["at"] * 1000)}


def _admin_model_routes():
    with _model_routes_lock:
        routes = _load_model_routes_locked()
        return {"mappings": dict(routes["mappings"]),
                "aliases": dict(routes["aliases"])}


def _admin_patch_model_routes(body):
    if not isinstance(body, dict):
        return None
    mappings = _norm_route_map(body.get("mappings"))
    aliases = _norm_route_map(body.get("aliases"))
    with _model_routes_lock:
        _model_routes["mappings"] = mappings
        _model_routes["aliases"] = aliases
        _save_model_routes_locked()
    return {"mappings": mappings, "aliases": aliases}


def _admin_adapters():
    return {
        "list_accounts": _admin_list_accounts,
        "latest_checkins": _admin_latest_checkins,
        "import_accounts": _admin_import_accounts,
        "patch_account": _admin_patch_account,
        "delete_account": _admin_delete_account,
        "test_account": _admin_test_account,
        "refresh_account": _admin_refresh_account,
        "checkin_account": _admin_checkin_account,
        "stats": _admin_stats,
        "list_models": _admin_list_models,
        "model_routes": _admin_model_routes,
        "patch_model_routes": _admin_patch_model_routes,
    }


def _admin_api():
    """AdminApi 工厂（无状态：KeyStore 单例 + 当前 ADMIN_PASSWORD + adapters；
    每次请求现取，测试里改全局即可生效）。"""
    return QA.AdminApi(_get_key_store(), ADMIN_PASSWORD, _admin_adapters())


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
        """鉴权：多 Key（qoder_admin.KeyStore）优先；KeyStore 空且配了
        QODER_PROXY_API_KEY 时走单 Key 兼容；都没配不鉴权。
        失败时已回复，返回 False。"""
        authz = self.headers.get("Authorization") or ""
        bearer = authz[7:].strip() if authz.startswith("Bearer ") else ""
        xkey = (self.headers.get("X-Api-Key") or "").strip()
        ok, status, message, err_type = _check_key_auth([bearer, xkey])
        if ok:
            return True
        self._err(status, message, err_type)
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
    def _maybe_admin(self):
        """admin 路由（/admin 页面 + /api/* 管理 API）接管请求；其余返回 False。
        admin 有自己的 Bearer 管理密码鉴权，不走数据面 _check_api_key。"""
        path = self.path.split("?", 1)[0]
        if path.startswith("/api") or path.rstrip("/") == "/admin":
            self._handle_admin()
            return True
        return False

    def _handle_admin(self):
        """qoder_admin.AdminApi 薄转接：headers 直接传 email.message.Message
        （qoder_admin 大小写不敏感取 Authorization）；path 过一道 unquote ——
        账号标签带空格/中文时浏览器会 percent-encode，不解码就命中不了标签。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            _drain_body(self, length)
            self.close_connection = True
            return self._err(413, "请求体过大（%d > %d 字节）" % (length, MAX_BODY_BYTES))
        body = self.rfile.read(length) if length else b""
        try:
            status, headers, payload = _admin_api().handle(
                self.command, urllib.parse.unquote(self.path), self.headers, body)
        except Exception as e:
            _log("管理接口异常 %s %s: %r" % (self.command, self.path, e))
            return self._err(500, "管理接口内部错误: %s" % e)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PATCH, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self._maybe_admin():
            return
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
        if self._maybe_admin():
            return
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

    def do_PATCH(self):
        if self._maybe_admin():
            return
        return self._err(404, "PATCH 仅用于 /api/* 管理接口")

    def do_PUT(self):
        if self._maybe_admin():
            return
        return self._err(404, "PUT 仅用于 /api/* 管理接口")

    def do_DELETE(self):
        if self._maybe_admin():
            return
        return self._err(404, "DELETE 仅用于 /api/* 管理接口")

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
                   "cooling_sec": max(0, int(a.get("cool_until", 0) - now)),
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
        data = [{"id": m["key"], "object": "model", "created": now,
                 "owned_by": "qoder-" + m.get("realm", "cn"),
                 "name": m["name"], "context_window": m["ctx"], "reasoning": m["reasoning"],
                 "vision": m["vision"], "realm": m.get("realm", "cn"),
                 "metadata": {"display_name": m["name"], "source": m["source"],
                              "format": m["format"], "price_factor": m["price_factor"],
                              "enabled": m["enabled"], "scene": m["scene"],
                              "realm": m.get("realm", "cn"),
                              "thinking_config": m["thinking_config"]}}
                for m in cached["list"]]
        data.extend(_model_route_entries(cached["list"], now))
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
        _bump_req_stats()
        if protocol == "responses":
            oa = convert_responses_to_openai(body)
            if not body.get("input") and not body.get("messages"):
                return self._bad("缺少 input")
        else:
            oa = convert_anthropic_to_openai(body) if protocol == "anthropic" else dict(body)
            if not oa.get("messages"):
                return self._bad("缺少 messages")
        req_model = body.get("model") or DEFAULT_MODEL
        routed_model = _route_model_name(req_model)
        if not _models["list"]:
            try:
                _fetch_models()
            except SignerError as e:
                return self._err(502, "签名器异常: %s" % e)
            except Exception as e:
                _log("模型清单不可用，模型名原样透传: %s" % e)
        try:
            model = _resolve_model(routed_model)
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
            # 请求级 realm 路由：X-Realm 头显式指定 cn/intl；缺省取全部（按账号自身 realm 走端点）
            realm = (self.headers.get("X-Realm") or "").strip().lower()
            if realm not in REALM_CONFIGS:
                realm = None
            _, resp = _open_upstream(oa, model, realm=realm)
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
    counts = _device_counts(accs)
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
            "device": _device_state(a, counts),
        })
    return rows


def _cmd_capture(_args=None):
    try:
        acct = _capture_current()
    except Exception as e:
        _print_err("提取登录态失败: %s" % e)
        return 1
    label = _add_session_to_pool(acct)
    row = _pooled_account(label, acct)
    print("已入池: %s" % label)
    print("  token      %s" % _redact(acct.get("token")))
    print("  有效期至   %s" % (acct.get("expiresAt") or "unknown"))
    print("  可续期     %s" % ("是" if acct.get("refreshToken") else "否"))
    print("  组织       %s" % (acct.get("organizationId") or "-"))
    print("  设备身份   %s（机器 ID %s）" % (_device_state(row),
                                          _redact(row.get("machineId"), 8) or "-"))
    print("  凭证文件   %s" % ACCOUNTS_FILE)
    return 0


def _cmd_list(_args=None):
    rows = _account_table()
    if not rows:
        print("账号池为空: python qoder_intl_proxy.py --capture")
        return 1
    for r in rows:
        print("%-16s token=%-28s 到期=%s(剩 %s 天) 续期=%s 设备=%s(%s)%s"
              % (r["account"], r["token"], r["expires_at"], r["left_days"],
                 "有" if r["has_refresh"] else "无",
                 r["device"], r["machine_id"],
                 " [续期链已失效]" if r["refresh_dead"] else ""))
    print("共 %d 个账号，设备身份 %s，文件 %s"
          % (len(rows), _device_summary(rows), ACCOUNTS_FILE))
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
    print("目标机不需要 Qoder 国际版客户端，但签名器仍需要 Node；"
          "客户端的 worker runtime 文件可用 QODER_INTL_WORKER_BUNDLE 指定")
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
        print("  %-16s 到期=%s 续期=%s 设备=%s(%s)%s"
              % (r["account"], r["expires_at"],
                 "有" if r["has_refresh"] else "无",
                 r["device"], r["machine_id"],
                 " [续期链已失效]" if r["refresh_dead"] else ""))
    print("设备身份 %s（每号一套是默认，QODER_PROXY_DEVICE=host 才共用本机）"
          % _device_summary(rows))
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
    st = _read_checkin_state()
    print("签到     %s 计划=%s 今天=%s attempts=%s 已到位=%s/%s 上次=%s"
          % ("开" if CHECKIN_ENABLED else "关（QODER_PROXY_CHECKIN=0）", CHECKIN_AT,
             st.get("date"), st.get("attempts"), st.get("secured_accounts"),
             st.get("pool_accounts"), st.get("last_at")))
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
    secured = 0
    counts = _device_counts(accs)
    for acct in accs:
        ok, rep = _checkin_account(acct)
        print("账号     %s" % _label_of(acct))
        print("设备     %s（机器 ID %s）" % (_device_state(acct, counts),
                                          _redact(acct.get("machineId"), 8) or "-"))
        print("活动     HTTP %s campaigns=%d claimable=%s 今天的份=%s"
              % (rep["status"], rep["campaigns"], rep["claimable"],
                 "已到位" if rep.get("secured") else "没到位"))
        for x in rep["claimed"]:
            print("领取     %s %s %s → HTTP %s%s%s grant=%s 发放=%s%s"
                  % (x.get("key") or x["id"], x.get("kind") or "",
                     ("%s" % x["amount"]) if x.get("amount") is not None else "",
                     x.get("status"),
                     (" " + str(x["claimStatus"])) if x.get("claimStatus") else "",
                     (" " + str(x["message"])) if x.get("message") else "",
                     str(x.get("grantId") or "-")[:8], x.get("grantedAt") or x.get("claimedAt") or "-",
                     "（重复请求，服务端 replayed，不再二次发放）" if x.get("replayed") else ""))
        if rep.get("after"):
            print("领取后   HTTP %s claimable=%s"
                  % (rep["after"]["status"], rep["after"]["claimable"]))
        if "addOnTotal" in rep:
            print("额度     addOnQuota.total=%s（领到后，每领一次 +100）" % rep["addOnTotal"])
        if rep.get("message"):
            print("结果     %s" % rep["message"])
        if rep.get("secured"):
            secured += 1
        if not ok:
            _print_err("签到失败: %s" % (rep.get("error") or rep.get("message")))
            all_ok = False
        if len(accs) > 1:
            print("-" * 40)
    print("合计     今天的 Credits 已到位 %d/%d 个号" % (secured, len(accs)))
    if len(accs) > 1 and secured < len(accs):
        _print_err("提示：部分号被上游以 SAME_PERSON_ALREADY_CLAIMED 拒发（每天每设备一份，"
                    "当天重试只重放）；签到已用账号池独立身份，若仍撞车检查该号"
                    " machineId 是否与他人共用")
    return 0 if (all_ok and secured == len(accs)) else 1


def _checkin_round(reason):
    """给池里每个号各跑一次当日 Credits 签到（对齐国内版：账号池身份，只领
    各号列表里 CLAIM_BENEFIT+CLAIMABLE 的条目，不探测、不盲领）。
    返回 (今天的份已到位的号数, 池内号数, 是否全部调用成功)。
    "已到位"= 这个号今天的 CREDITS 活动是 CLAIMED（这一轮领的、或客户端/上一轮领的都算），
    只有全部到位才算今天不用再来；单号异常只记日志并继续下一个。
    SAME_PERSON_ALREADY_CLAIMED 的号 ok=True 但不"已到位"（上游拒发的判定每天
    每设备一份，当天重试只是幂等重放，不多领也不误报失败）。"""
    with _pool_lock:
        accs = list(_load_pool_locked())
    total = len(accs)
    if not accs:
        _log("签到跳过(%s): 账号池为空" % reason)
        return 0, 0, True
    secured, newgot, all_ok = 0, 0, True
    for acct in accs:
        label = _label_of(acct)
        try:
            ok, rep = _checkin_account(acct)
        except Exception as e:
            _log("签到异常 %s: %r" % (label, e))
            all_ok = False
            continue
        claimed = [c for c in (rep.get("claimed") or []) if c.get("status") in (200, 201, 204)]
        # BLOCKED（含 SAME_PERSON）也回 HTTP 200 且带 benefit 字段，
        # 必须按业务状态过滤，否则"无份"会被算成"本轮新领"。
        fresh = [c for c in claimed if not c.get("replayed")
                 and c.get("claimStatus") == "CLAIMED"]
        credits = sum(int(c.get("amount") or 0)
                      for c in fresh if (c.get("kind") or "").upper() == "CREDITS")
        if fresh:
            newgot += 1
            _log("签到成功 %s campaign=%s credits=%d 发放时间=%s 额度总额=%s"
                 % (label, ",".join(str(c["id"])[:8] for c in fresh), credits,
                    fresh[0].get("grantedAt") or fresh[0].get("claimedAt") or "-",
                    rep.get("addOnTotal")))
        if rep.get("secured"):
            secured += 1
        elif ok:
            _log("签到无份 %s: %s" % (label, rep.get("message") or ""))
        if not ok:
            all_ok = False
            _log("签到失败 %s HTTP %s: %s"
                 % (label, rep.get("status"), rep.get("error") or ""))
    _log("签到小结(%s): 已到位 %d/%d，本轮新领 %d" % (reason, secured, total, newgot))
    return secured, total, all_ok


def _read_checkin_state():
    try:
        with open(CHECKIN_STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _write_checkin_state(st):
    d = os.path.dirname(CHECKIN_STATE_FILE)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    tmp = CHECKIN_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CHECKIN_STATE_FILE)
    except OSError as e:
        _print_err("签到状态写盘失败: %s" % e)


def _today_at(hhmm, base=None):
    """今天 hhmm（本地时区）的 epoch；hhmm 非法则退回 CHECKIN_AT 默认时间。"""
    try:
        hh, mm = hhmm.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
    except Exception:
        try:    # 非法 → 退回 CHECKIN_AT（默认 10:30）
            dh, dm = CHECKIN_AT.split(":")
            hh, mm = int(dh), int(dm)
        except Exception:
            hh, mm = 10, 30
    lt = time.localtime(base if base is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))


def _checkin_plan(now, st, today, due):
    """纯函数：今天该不该跑一轮、下次几点醒来。返回 ("run"|"wait", 秒)。
    抽出来只为能单测那四个边界（未到点 / 到点没成 / 已完成 / 次数用完），
    不然要验证就得真的睡满一天。"""
    if now < due:
        return "wait", due - now                      # 未到今天的点
    if st.get("date") != today:
        st = {"date": today, "attempts": 0, "done": False}
    if st.get("done") or st.get("attempts", 0) >= CHECKIN_MAX_ATTEMPTS:
        return "wait", due + 86400 - now              # 今天收工（或放弃），等明天
    return "run", 0


def _checkin_loop():
    """睡到点 → 跑一轮 → 每个号的今天的份都到位就记 done，否则按 RETRY_SEC 再试，次数用完等明天。
    重启安全：今天的 attempts/done 存状态文件，宕机/重拉都会补跑或跳过，不重复不遗漏。"""
    while True:
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        st = _read_checkin_state()
        action, wait = _checkin_plan(now, st, today, _today_at(CHECKIN_AT, now))
        if action == "run":
            secured, total, all_ok = _checkin_round("定时")
            done = secured >= total
            st = {"date": today,
                  "attempts": st.get("attempts", 0) + 1,
                  "done": done,                       # 全部号今天的 Credits 都在手上才算今天完
                  "secured_accounts": secured,
                  "pool_accounts": total,
                  "last_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                  "last_all_ok": all_ok}
            _write_checkin_state(st)
            _log("签到轮次 done=%s attempts=%d 已到位=%d/%d 全 OK=%s"
                 % (done, st["attempts"], secured, total, all_ok))
            wait = 60 if done else CHECKIN_RETRY_SEC
        time.sleep(max(30.0, min(wait, 3600.0)))   # 分片睡：改点/排障不用等一整天


def _start_checkin_thread():
    if not CHECKIN_ENABLED:
        return None
    t = threading.Thread(target=_checkin_loop, name="checkin", daemon=True)
    t.start()
    return t


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
        print("  Qoder 国际版官方模型本地代理")
        print("=" * 56)
        print("  账号池: %d 个账号" % len(accs))
        counts = _device_counts(accs)
        if DEVICE_MODE == "host":
            print("  设备身份: 全部共用本机指纹（QODER_PROXY_DEVICE=host）")
        else:
            print("  设备身份: %d/%d 各一套"
                  % (sum(1 for a in accs if _device_state(a, counts) == "独立"), len(accs)))
        for a in accs:
            exp = a.get("exp") or 0
            tag = "[过期]" if exp and exp < now else "[有效]"
            print("    %s %s %s [%s 机器ID %s]"
                  % (_label_of(a), tag, (a.get("expiresAt") or "")[:19],
                     _device_state(a, counts), _redact(a.get("machineId"), 8) or "-"))
        if len(accs) > 1 and DEVICE_MODE != "host":
            print("  注: 各一套设备身份做指纹隔离；能否每号都领到每日 Credits 取决于上游判重（桌面端换号可领，代理连发曾被拒，待验证）")
        print("-" * 56)
        if sys.platform == "win32":
            print("  0 启动代理")
            print("  1 浏览器设备码登录（加入账号池，可连续登多个）")
            print("  2 浏览器设备码登录（token 直接签发给一套全新设备）")
            print("  3 抓取本机 Qoder 国际版客户端登录态")
            print("  4 领取今天的每日 Credits（多账号）")
            print("  5 查看账号池详情")
            print("  6 移除账号")
            print("  7 导出账号池（跨电脑迁移）")
            print("  8 导入账号池")
            print("  9 退出")
        else:
            print("  0 启动代理")
            print("  1 浏览器设备码登录（加入账号池，可连续登多个）")
            print("  2 浏览器设备码登录（token 直接签发给一套全新设备）")
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
    print("Qoder 国际版代理: http://127.0.0.1:%d  (%s)" % (PORT, ACCOUNTS_FILE))
    print("  OpenAI      POST /v1/chat/completions   GET /v1/models")
    print("  Responses   POST /v1/responses")
    print("  Anthropic   POST /v1/messages、/v1/messages/count_tokens")
    print("  健康        GET /health")
    print("  上游        %s%s" % (GATEWAY, INFER_PATH))
    print("  签名        %s" % _signer.describe())
    if ADMIN_PASSWORD:
        print("  管理后台    http://127.0.0.1:%d/admin（QODER_PROXY_ADMIN_PASSWORD 已配置）"
              % PORT)
    else:
        print("  管理后台    /admin 页面可看，但管理 API 已禁用"
              "（未配置 QODER_PROXY_ADMIN_PASSWORD）")
    try:
        n_keys = len(_get_key_store().list())
    except Exception:
        n_keys = 0
    if n_keys:
        print("  API Key     多 Key 已启用（%d 个，管理: /admin；%s）"
              % (n_keys, ADMIN_KEYS_FILE))
    elif API_KEY:
        print("  API Key     已启用（QODER_PROXY_API_KEY；/admin 可切多 Key）")
    else:
        print("  API Key     未设置 —— 任何本机进程都能用这个代理，建议设 QODER_PROXY_API_KEY"
              " 或在 /admin 建多 Key")
    with _pool_lock:
        n = len(_load_pool_locked())
    if not n:
        print("  警告        账号池为空，先运行: python qoder_intl_proxy.py --login 或 --capture")
    else:
        print("  设备        %s" % _device_summary(_account_table()))
    _log("启动 port=%d accounts=%d" % (PORT, n))
    if CHECKIN_ENABLED:
        print("  签到        每日 %s 自动领 Credits（今天没领成会重试 %d 次，间隔 %ds）"
              % (CHECKIN_AT, CHECKIN_MAX_ATTEMPTS, CHECKIN_RETRY_SEC))
        _start_checkin_thread()
        _log("签到线程启动 at=%s tries=%d retry=%ds state=%s 现状=%s"
             % (CHECKIN_AT, CHECKIN_MAX_ATTEMPTS, CHECKIN_RETRY_SEC, CHECKIN_STATE_FILE,
                json.dumps(_read_checkin_state(), ensure_ascii=False)))
    else:
        print("  签到        已关闭（QODER_PROXY_CHECKIN=0）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def main():
    global PORT, LOG_FILE, API_KEY
    p = argparse.ArgumentParser(
        description="Qoder 双区官方模型本地代理（OpenAI / Responses / Anthropic 三协议，单进程单端口同时服务 cn/intl）")
    p.add_argument("--capture", action="store_true", help="从本机 Qoder 客户端提取登录态入池（按 --realm 选区）")
    p.add_argument("--login", action="store_true",
                   help="浏览器设备码登录换 token 入池（按 --realm 选区，本机可无客户端）")
    p.add_argument("--new-device", action="store_true",
                   help="--login 时不用本机设备 ID（token 签发给一套新设备）")
    p.add_argument("--realm", choices=("cn", "intl"), default=None,
                   help="指定区域：cn=国内版 / intl=国际版（默认 QODER_PROXY_DEFAULT_REALM 或 cn）")
    p.add_argument("--site", help="设备码授权页 base（默认按 realm：cn=https://qoder.cn / intl=https://qoder.com）")
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
