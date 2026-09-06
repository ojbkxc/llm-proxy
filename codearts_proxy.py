# -*- coding: utf-8 -*-
"""
CodeArts Agent (华为) 官方模型本地代理 - 纯 Python 标准库 + cryptography

把 CodeArts Agent 的官方模型通道（inferhub / snap-access）转成本地 API：
  OpenAI 兼容    GET /v1/models、POST /v1/chat/completions（SSE 直通）
  OpenAI 新版    POST /v1/responses（Responses 协议，兼容 codex-cli 等）
  Anthropic 兼容 POST /v1/messages、POST /v1/messages/count_tokens
  健康检查       GET /health

模型（实测可用）：
  GLM-5.2 / glm-5.2-sft-harmony / openpangu-2.0-pro / openpangu-2.0-flash

认证完全复用 CodeArts Agent 客户端的登录态：
  凭证（AK/SK/securitytoken）从客户端的 state.vscdb 解出
  （Electron os_crypt DPAPI + AES-256-GCM 双层加密）。
  token 过期时打开 CodeArts Agent 客户端让它自动刷新即可。

本代理不做任何内容拦截/脱敏/审计（用户明确：CodeArts 通道不需要管控层），也不写日志文件。

用法:
  python codearts_proxy.py                # 启动代理（默认端口 8788，环境变量 CODEARTS_PROXY_PORT 修改）
  python codearts_proxy.py --capture      # 把当前 CodeArts Agent 登录态加入账号池
  python codearts_proxy.py --list         # 查看账号池
  python codearts_proxy.py --remove LABEL # 从账号池移除指定账号
  python codearts_proxy.py --export 路径   # 导出账号池到指定 json 文件（跨电脑迁移）
  python codearts_proxy.py --import 路径   # 从 json 文件导入账号（合并，同标签覆盖）

多账号轮询：
  默认直接复用客户端当前登录态（单账号）。
  需要轮换时：客户端登录账号 A → --capture，切换账号 B 登录 → --capture …… 
  账号池（codearts_accounts.json）非空后，请求自动 round-robin 轮询，
  过期账号自动跳过；每个账号使用独立 session id（上游按 session id 计
  并发会话数，N 个账号等效 N×3 个并发会话额度）。
  凭证过期后：打开 CodeArts Agent 客户端登录刷新，再 --capture 覆盖更新。
  环境变量 CODEARTS_PROXY_ACCOUNTS_FILE 可指定池文件路径（如放网盘共享，多电脑指向同一文件）。
"""
import argparse
import base64
import ctypes
import hashlib
import hmac
import http.client
import http.server
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

# cryptography 仅用于解密本地凭证（AES-256-GCM）+ DPoP 签名（ES256）
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import ec, utils as _asym_utils
from cryptography.hazmat.primitives import hashes

PORT = int(os.environ.get("CODEARTS_PROXY_PORT", "8788"))
HOME = os.path.expanduser("~")

# API Key 鉴权（空 = 不鉴权，环境变量 CODEARTS_PROXY_API_KEY 设置；客户端用 Authorization: Bearer <key> 或 X-Api-Key）
API_KEY = os.environ.get("CODEARTS_PROXY_API_KEY", "")

# 请求体大小上限（防 OOM，对齐 trae_proxy maxBodyBytes=8MB）
MAX_BODY_BYTES = 8 << 20

# token 自动续期（逆向自 huaweicloud.authentication 扩展 plugin.js）
# 端点 POST {iamStsHost}/v1/oauth2/tokens，form: client_id/code_verifier/grant_type=refresh_token/refresh_token
# DPoP 头 = ES256 JWS(typ=dpop+jwt)，payload={htm,htu,iat,jti}，用 loginContext.dpopKeyPair 私钥签
REFRESH_ENDPOINT = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
REFRESH_AHEAD_SEC = 3600          # 临期 1h 内触发续期（对齐客户端 safelyRenewTokenInterval≈3600s）
CLIENT_ID = os.environ.get("CODEARTS_PROXY_CLIENT_ID", "codearts-agent")  # = env.uriScheme

# ── 浏览器登录（逆向自 huaweicloud.authentication/dist/plugin.js 5.3.0）──────────
# refresh_token 也失效（30 天边界）时，自动/手动触发：起本地 callback server → 开浏览器
# 到华为云 portal 授权 → 拿 code → 换 token → 入账号池。复刻客户端 buildLoginUrl + requestToken。
PORTAL_HOST = os.environ.get("CODEARTS_PROXY_PORTAL_HOST", "https://codearts.huaweicloud.com/portal")
AUTH_REDIRECT_PATH = "/oauth/callback"          # 客户端 AUTH_REDIRECT_URL
LOGIN_PLUGIN_NAME = "snap_AIIDE"                 # 客户端 LOGIN_PLUGIN_NAME
EXTENSION_VERSION = "5.3.0"                      # 扩展版本（authorize URL 里带）
LOGIN_TIMEOUT_SEC = int(os.environ.get("CODEARTS_PROXY_LOGIN_TIMEOUT", "300"))  # 默认 5 分钟
# refresh 失败时是否自动 fallback 到浏览器登录（1=是，0=报错让用户手动 --login）
AUTO_BROWSER_LOGIN = os.environ.get("CODEARTS_PROXY_AUTO_BROWSER_LOGIN", "1") == "1"

# CodeArts Agent (Electron) 的用户数据
LOCAL_STATE = os.path.join(HOME, "AppData", "Roaming", "codearts-agent", "Local State")
STATE_VSCDB = os.path.join(HOME, "AppData", "Roaming", "codearts-agent", "User", "globalStorage", "state.vscdb")
SESSION_KEY = 'secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}'

# 上游（与内核 INFERHUB_BASE_URLS 一致，主用 .com 备用 .cn）
UPSTREAM = "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"

# 模型清单（内核日志实测：模型 id 与上下文窗口）
# max_tokens = 上游对 input+output 总 token 的硬限（超限返回 limit_err，实测值）
# ctx = 上下文窗口（/v1/models 暴露给客户端，通常 ≥ max_tokens）
MODELS = {
    "GLM-5.2":              {"name": "GLM-5.2",           "ctx": 307200, "max_tokens": 307200, "limit_err": 81027, "desc": "最新旗舰模型，专为长程任务打造"},
    "glm-5.2-sft-harmony":  {"name": "GLM-5.2-ArkTS-SPARK", "ctx": 196608, "max_tokens": 196608, "limit_err": 81001, "desc": "基于GLM-5.2增训鸿蒙代码与开发知识"},
    "openpangu-2.0-pro":    {"name": "OpenPangu-2.0-Pro",  "ctx": 512000, "max_tokens": 512000, "limit_err": 81001, "desc": "最新旗舰模型，复杂工程稳定交付"},
    "openpangu-2.0-flash":  {"name": "OpenPangu-2.0-Flash", "ctx": 512000, "max_tokens": 512000, "limit_err": 81001, "desc": "均衡推理效果与性能"},
}
# 别名 → 真实 model id（想加别名只改这张表）
MODEL_ALIAS = {
    "glm-5.2": "GLM-5.2",
    "glm-5.2-harmony": "glm-5.2-sft-harmony",
    "pangu-pro": "openpangu-2.0-pro",
    "pangu-flash": "openpangu-2.0-flash",
}

# ── 多账号池：--capture 抓取当前客户端登录态，运行时 round-robin 轮询 ─────
ACCOUNTS_FILE = os.environ.get("CODEARTS_PROXY_ACCOUNTS_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "codearts_accounts.json")

_pool_lock = threading.Lock()
_pool = {"mtime": 0.0, "accounts": []}   # 内存缓存；mtime 变化时热重载（--capture 是另一个进程写的）
_rr = 0                                  # round-robin 游标


def _print_err(msg):
    """仅 stderr 一行错误输出（不写日志文件），如上游 4xx 详情。"""
    print(msg, file=sys.stderr)


def _label_of(s: dict) -> str:
    """账号标签：account label，为空则用 AK 前 8 位。"""
    return s.get("account") or (s.get("ak") or "anon")[:8]


def _decrypt_full_session() -> dict:
    """解出客户端 HuaweiCloudSession 完整字段（含 refresh_token/loginContext，自动续期必需）。"""
    key = _get_master_key()
    blob = _load_session_blob()
    pt = AESGCM(key).decrypt(blob[3:15], blob[15:], None)  # nonce + ct + tag
    j = json.loads(pt.decode("utf-8"))
    return {
        "ak": j.get("accessKey", ""),
        "sk": j.get("secretKey", ""),
        "securitytoken": j.get("securitytoken", ""),
        "exp": _parse_exp(j.get("expires_at", "")),
        "expires_at": j.get("expires_at", ""),
        "account": (j.get("account") or {}).get("label", ""),
        "domainId": j.get("domainId", ""),
        "refresh_token": j.get("refresh_token", ""),
        "loginContext": j.get("loginContext") or {},
        "safelyRenewTokenInterval": j.get("safelyRenewTokenInterval", 3600393),
    }


def _capture_current() -> dict:
    """解出当前 CodeArts Agent 客户端登录态（不走缓存），返回账号池条目（含续期所需字段）。"""
    s = _decrypt_full_session()
    if s["exp"] and s["exp"] < time.time():
        raise RuntimeError("当前客户端凭证已过期（%s），请先在 CodeArts Agent 里登录刷新" % s["expires_at"])
    s["ot_session_id"] = _uid()
    s["user_session_id"] = _uid()
    s["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return s


def _load_pool_locked():
    """把池文件载入内存（mtime 变化才重读）。调用方必须持有 _pool_lock。"""
    global _rr
    if not os.path.exists(ACCOUNTS_FILE):
        _pool["accounts"] = []
        _pool["mtime"] = 0.0
        return
    mtime = os.path.getmtime(ACCOUNTS_FILE)
    if mtime != _pool["mtime"]:
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _pool["accounts"] = data if isinstance(data, list) else []
            _pool["mtime"] = mtime
            _rr = 0
        except Exception:
            pass  # 文件被并发写坏/写一半：沿用内存旧池，下次再试


def _save_pool_locked():
    """原子写盘（tmp + move）。调用方必须持有 _pool_lock。"""
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_pool["accounts"], f, ensure_ascii=False, indent=2)
        f.write("\n")
    shutil.move(tmp, ACCOUNTS_FILE)
    _pool["mtime"] = os.path.getmtime(ACCOUNTS_FILE)


def _add_session_to_pool(s: dict) -> str:
    """把一个完整 session 并入账号池（按标签去重，已存在则覆盖凭证、保留 session id）。"""
    label = _label_of(s)
    global _rr
    with _pool_lock:
        _load_pool_locked()
        for i, a in enumerate(_pool["accounts"]):
            if _label_of(a) == label:
                # 同一账号重复抓取：保留原 session id（上游按其计并发会话数）
                s["ot_session_id"] = a.get("ot_session_id") or s["ot_session_id"]
                s["user_session_id"] = a.get("user_session_id") or s["user_session_id"]
                _pool["accounts"][i] = s
                break
        else:
            _pool["accounts"].append(s)
        _save_pool_locked()
        _rr = 0
    return label


def capture_to_pool() -> str:
    """把当前客户端登录态并入账号池（按标签去重，已存在则覆盖更新凭证）。"""
    return _add_session_to_pool(_capture_current())


def _next_account() -> dict:
    """round-robin 取一个未过期账号；全部过期则报错提示重新登录。"""
    now = time.time()
    global _rr
    with _pool_lock:
        _load_pool_locked()
        accs = _pool["accounts"]
        n = len(accs)
        if n == 0:
            raise RuntimeError("账号池为空，请运行 python codearts_proxy.py --login（或 --capture）添加账号")
        for i in range(n):
            a = accs[(_rr + i) % n]
            if not a.get("exp") or a["exp"] > now:
                _rr = (_rr + i + 1) % n
                return a
    raise RuntimeError("账号池全部 %d 个账号均已过期，请运行 python codearts_proxy.py --login 重新登录" % n)


def _mark_session_failed(session: dict):
    """上游 401 后：池账号标记过期（下次轮询自动跳过）；单账号模式清 10s 缓存。"""
    sid = session.get("ot_session_id")
    if sid:
        with _pool_lock:
            for a in _pool["accounts"]:
                if a.get("ot_session_id") == sid and a.get("ak") == session.get("ak"):
                    a["exp"] = time.time() - 1
                    _save_pool_locked()
                    return
    _session_cache.update({"at": 0.0, "data": None})


# ── 凭证：DPAPI + AES-256-GCM 解 CodeArts Agent 的登录态 ──────────────────
_session_lock = threading.Lock()
_session_cache = {"at": 0.0, "data": None}


def _dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI（当前用户）解密，等价 Node 的 safeStorage.decryptString"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("pb", ctypes.c_void_p)]
    class OUT_BLOB(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("pb", ctypes.c_void_p)]
    buf = ctypes.create_string_buffer(data, len(data))
    in_ = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    out = OUT_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise RuntimeError("DPAPI 解密失败（需在与写入时相同的 Windows 用户下运行）")
    try:
        return bytes(ctypes.string_at(out.pb, out.cb))
    finally:
        # 64 位下 out.pb 超过 int32，必须用 c_void_p 包装，否则 OverflowError 吞掉正常返回
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(out.pb))


def _get_master_key() -> bytes:
    """从 Local State 取 os_crypt.encrypted_key → DPAPI 解出 32B AES key"""
    with open(LOCAL_STATE, encoding="utf-8") as f:
        ls = json.load(f)
    blob = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if blob[:5] != b"DPAPI":
        raise RuntimeError("os_crypt.encrypted_key 格式异常")
    key = _dpapi_unprotect(blob[5:])
    if len(key) != 32:
        raise RuntimeError("主密钥长度异常: %d" % len(key))
    return key


def _load_session_blob() -> bytes:
    """从 state.vscdb 读 HuaweiCloudSession 的 v10 加密 blob"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STATE_VSCDB.replace("\\", "/"), uri=True, timeout=2)
    except sqlite3.OperationalError as e:
        raise RuntimeError("找不到 %s 或无法打开（%s）\n请先安装并登录 CodeArts Agent 客户端" % (STATE_VSCDB, e))
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key=?", (SESSION_KEY,)).fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("state.vscdb 中未找到华为云会话，请先在 CodeArts Agent 里登录")
    d = json.loads(row[0])
    blob = bytes(d["data"])
    if blob[:3] != b"v10":
        raise RuntimeError("会话密文格式异常（期望 v10 前缀）")
    return blob


def _parse_exp(s):
    """expires_at: '2026-09-04T07:27:35.696Z' → epoch 秒（失败返回 0）"""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0.0


def _read_client_session() -> dict:
    """单账号路径：解出客户端登录态完整字段，10s 进程内缓存。"""
    if sys.platform != "win32":
        raise RuntimeError("非 Windows 环境，无法读取 CodeArts Agent 客户端登录态。请先 --import 导入账号池。")
    now = time.time()
    data = _session_cache["data"]
    if data and now - _session_cache["at"] < 10:
        return data
    with _session_lock:
        if _session_cache["data"] and time.time() - _session_cache["at"] < 10:
            return _session_cache["data"]
        session = _decrypt_full_session()
        if session["exp"] and session["exp"] < now:
            raise RuntimeError("凭证已过期（%s），请打开 CodeArts Agent 客户端让它自动刷新后重试" % session["expires_at"])
        _session_cache.update({"at": time.time(), "data": session})
        return session


# ── token 自动续期：DPoP(ES256) + POST /v1/oauth2/tokens ──────────────────────
_refresh_lock = threading.Lock()
_refreshing = set()   # 正在刷新的 session 标识（ot_session_id 或 'client'），防并发重复刷新


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _dpop_sign(private_jwk: dict, public_jwk: dict, method: str, url: str) -> str:
    """RFC9449 DPoP proof（ES256 JWS, typ=dpop+jwt）。复刻 plugin.js generateDpopJWE。"""
    d = int.from_bytes(_b64url_decode(private_jwk["d"]), "big")
    priv = ec.derive_private_key(d, ec.SECP256R1())
    header = {"alg": "ES256", "typ": "dpop+jwt", "jwk": public_jwk}
    payload = {"htm": method, "htu": url, "iat": int(time.time()), "jti": secrets.token_hex(32)}
    si = (_b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")) + "." +
          _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")))
    sig_der = priv.sign(si.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = _asym_utils.decode_dss_signature(sig_der)
    sig_raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return si + "." + _b64url(sig_raw)


# ── 浏览器登录：PKCE + DPoP keypair + 本地 callback + 换 token ────────────────
def _gen_pkce() -> dict:
    """PKCE pair（复刻 PKCEGenerator.generate）：verifier 128 hex 字符，challenge=base64url(SHA256(verifier))。"""
    verifier = secrets.token_hex(64)  # 128 hex 字符
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return {"codeVerifier": verifier, "codeChallenge": challenge, "codeChallengeMethod": "SHA-256"}


def _gen_dpop_keypair() -> dict:
    """P-256 ECDSA keypair，导出 JWK（复刻 generateDpopKeyPair，extractable 等价）。"""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    nums = priv.private_numbers()
    d = nums.private_value.to_bytes(32, "big")
    x, y = pub.public_numbers().x, pub.public_numbers().y
    def jwk_key(d_or_none):
        j = {"kty": "EC", "crv": "P-256",
             "x": _b64url(x.to_bytes(32, "big")), "y": _b64url(y.to_bytes(32, "big"))}
        if d_or_none is not None:
            j["d"] = _b64url(d_or_none)
        return j
    return {"privateKeyJwk": jwk_key(d), "publicKeyJwk": jwk_key(None)}


class _LoginCallbackServer(http.server.BaseHTTPRequestHandler):
    """临时 localhost server 收 /oauth/callback?code=...，收到即关停。"""
    code = None
    error = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path)
        if p.path != AUTH_REDIRECT_PATH:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        q = urllib.parse.parse_qs(p.query)
        if q.get("code"):
            _LoginCallbackServer.code = q["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = "<html><body><h2>登录成功，可关闭此页并回到终端。</h2></body></html>".encode("utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            _LoginCallbackServer.error = q.get("error", ["unknown"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = "<html><body><h2>登录失败，请回终端查看错误。</h2></body></html>".encode("utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


# ── 终端二维码：无浏览器环境（Linux server）下用 /authui/qrcode API 直接拿二维码 ──
# 逆向自 authui app.bundle.js：GET /authui/qrcode → {client_key, qrcode_content(URL)},
# 轮询 GET /authui/qrcode/status?client_key=...&service=... → status: new→authorizing→authorized
# authorized 时返回 redirect_url（含 ticket/code），但扫码登录走的是 portal SSO 回调，
# 不经过我们 /oauth/callback。所以这条路径只用于「展示二维码 + 等用户扫码确认」，
# 扫码成功后华为云 portal 会把浏览器跳到 redirect_url，而 redirect_url 里就带了我们
# authorize URL 里的 port/redirect_uri → 浏览器跳 127.0.0.1:port/oauth/callback?code=...
# 但 Linux 无浏览器，redirect_url 跳不动 → 改为 authorized 后直接从 redirect_url 里
# 提取 code，本地完成换 token。
AUTHUI_HOST = "https://auth.huaweicloud.com"


def _render_qr_terminal(url: str):
    """把 URL 编码成二维码，用 qrcode 库渲染到终端（ASCII 黑白块）。无依赖时退化成打印 URL。"""
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.StringIO()
        for row in qr.modules:
            buf.write("".join("  " if c else "██" for c in row) + "\n")
        print(buf.getvalue())
        return True
    except Exception:
        print("（未安装 qrcode 库，无法渲染二维码。pip install qrcode 后体验更好）")
        print("二维码内容（可用手机扫码工具识别）:\n  %s" % url)
        return False


def _poll_qrcode_status(client_key: str, service: str, timeout: int) -> str:
    """轮询 /authui/qrcode/status，扫到 authorized 返回 redirect_url。超时抛错。"""
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        try:
            url = "%s/authui/qrcode/status?client_key=%s&service=%s" % (
                AUTHUI_HOST, urllib.parse.quote(client_key, safe=""), urllib.parse.quote(service, safe=""))
            r = _opener.open(url, timeout=10)
            raw = r.read().decode("utf-8", "replace")
            try:
                obj = json.loads(raw)
            except Exception:
                obj = json.loads(raw.split("(", 1)[-1].rsplit(")", 1)[0]) if "(" in raw else {}
            status = obj.get("status", "")
            err = obj.get("error_code")
            if err == "427":
                raise RuntimeError("二维码已失效，请重新 --login")
            if err == "439":
                raise RuntimeError("用户拒绝登录")
            if status == "expired":
                raise RuntimeError("二维码已过期，请重新 --login")
            if status == "authorized" and obj.get("redirect_url"):
                return obj["redirect_url"]
            if status != last_status:
                last_status = status
                if status == "new":
                    print("等待扫码...")
                elif status == "authorizing":
                    print("已扫码，请在手机上确认登录...")
        except RuntimeError:
            raise
        except Exception:
            pass   # 网络抖动，继续
        time.sleep(2)
    raise RuntimeError("扫码登录超时（%ds）" % timeout)


def _extract_code_from_redirect(redirect_url: str) -> str:
    """从扫码成功后的 redirect_url 里提取 authorization code。"""
    p = urllib.parse.urlsplit(redirect_url)
    q = urllib.parse.parse_qs(p.query)
    if q.get("code"):
        return q["code"][0]
    # redirect 可能嵌套一层（portal/authorize 再跳 oauth/callback）
    for v in q.values():
        if isinstance(v, list) and v and "code=" in v[0]:
            sub = urllib.parse.urlsplit(v[0])
            sq = urllib.parse.parse_qs(sub.query)
            if sq.get("code"):
                return sq["code"][0]
    raise RuntimeError("扫码成功但 redirect_url 里找不到 code: %s" % redirect_url[:200])


def _qr_login(timeout: int) -> dict:
    """终端二维码登录：调 /authui/qrcode 拿二维码 URL → 终端渲染 → 轮询 status → 拿 code → 换 token。"""
    pkce = _gen_pkce()
    dpop = _gen_dpop_keypair()
    import socket
    ss = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ss.bind(("127.0.0.1", 0)); port = ss.getsockname()[1]; ss.close()
    ticket_id = secrets.token_hex(16)
    # service = 浏览器登录时 portal/authorize 那个完整 URL（扫码成功后华为云会跳回它，再跳 callback）
    service = ("%s/authorize?theme=Dark&locale=zh-cn&uri_scheme=%s&client_id=%s&port=%d"
               "&code_challenge=%s&code_challenge_method=%s&ticket_id=%s&plugin-name=%s&plugin-version=%s" % (
                   PORTAL_HOST, CLIENT_ID, CLIENT_ID, port,
                   pkce["codeChallenge"], pkce["codeChallengeMethod"],
                   ticket_id, LOGIN_PLUGIN_NAME, EXTENSION_VERSION))
    # 拿二维码
    qr_url = "%s/authui/qrcode" % AUTHUI_HOST
    r = _opener.open(qr_url, timeout=15)
    raw = r.read().decode("utf-8", "replace")
    try:
        qr_obj = json.loads(raw)
    except Exception:
        qr_obj = json.loads(raw.split("(", 1)[-1].rsplit(")", 1)[0]) if "(" in raw else {}
    client_key = qr_obj.get("client_key") or ""
    qrcode_content = qr_obj.get("qrcode_content") or ""
    if not client_key or not qrcode_content:
        raise RuntimeError("获取二维码失败: %s" % raw[:200])
    print("请使用华为云 APP 扫描下方二维码登录（华为账号 → 扫一扫）：")
    _render_qr_terminal(qrcode_content)
    print("（二维码有效期约 3 分钟，超时请重新 --login）")
    # 轮询等扫码
    redirect_url = _poll_qrcode_status(client_key, service, timeout)
    print("扫码成功，正在换取凭证...")
    code = _extract_code_from_redirect(redirect_url)
    return _exchange_code(code, pkce, dpop, port)


def _wait_for_code(port: int, timeout: int) -> str:
    """阻塞直到 callback 收到 code 或超时。返回 authorization code。"""
    srv = http.server.HTTPServer(("127.0.0.1", port), _LoginCallbackServer)
    srv.timeout = 1
    deadline = time.time() + timeout
    while time.time() < deadline and _LoginCallbackServer.code is None and _LoginCallbackServer.error is None:
        srv.handle_request()
    srv.server_close()
    if _LoginCallbackServer.code:
        return _LoginCallbackServer.code
    if _LoginCallbackServer.error:
        raise RuntimeError("浏览器登录被拒绝: %s" % _LoginCallbackServer.error)
    raise RuntimeError("浏览器登录超时（%ds 未收到回调），请重试或检查浏览器是否被拦截" % timeout)


def _token_request(form: dict, pkce: dict, dpop: dict) -> dict:
    """POST STS OAuth 端点（form + DPoP 头），返回原始响应 JSON。"""
    dpop_header = _dpop_sign(dpop["privateKeyJwk"], dpop["publicKeyJwk"], "POST", REFRESH_ENDPOINT)
    req = urllib.request.Request(REFRESH_ENDPOINT,
                                 data=urllib.parse.urlencode(form).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("DPoP", dpop_header)
    resp = _opener.open(req, timeout=60)
    return json.loads(resp.read().decode("utf-8"))


def _session_from_sts(data: dict, base: dict, pkce: dict, dpop: dict) -> dict:
    """STS 响应 → session dict。base 提供账号/域/续期间隔等保留字段。"""
    creds = data.get("credentials") or {}
    if not creds.get("access_key_id"):
        raise RuntimeError("STS 响应无 credentials: %s" % json.dumps(data, ensure_ascii=False)[:300])
    return {
        "ak": creds["access_key_id"],
        "sk": creds["secret_access_key"],
        "securitytoken": creds["security_token"],
        "exp": _parse_exp(creds.get("expiration", "")),
        "expires_at": creds.get("expiration", ""),
        "account": base.get("account", ""),
        "domainId": base.get("domainId", ""),
        "refresh_token": data.get("refresh_token", base.get("refresh_token", "")),
        "loginContext": {"pkcePair": pkce, "dpopKeyPair": dpop},
        "safelyRenewTokenInterval": base.get("safelyRenewTokenInterval", 3600393),
    }


def _exchange_code(code: str, pkce: dict, dpop: dict, port: int) -> dict:
    """用 authorization code 换 token（复刻 requestToken, grant_type=authorization_code）。"""
    redirect_uri = "http://127.0.0.1:%d%s" % (port, AUTH_REDIRECT_PATH)
    data = _token_request({
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": pkce["codeVerifier"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, pkce, dpop)
    # 登录响应带 account 标签；续期响应没有（保留旧值）
    s = _session_from_sts(data, data, pkce, dpop)
    s["account"] = (data.get("account") or {}).get("label", "")
    return s


def _have_display() -> bool:
    """检测当前环境是否有图形显示（决定走浏览器还是终端二维码）。"""
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _browser_login(timeout: int = LOGIN_TIMEOUT_SEC, force_qr: bool = False) -> dict:
    """登录流程：有显示器走浏览器 OAuth；无显示器（Linux server）走终端二维码扫码。
    force_qr=True 强制走二维码（即便有显示器）。
    浏览器路径：弹华为云统一登录页，用户可在页内选账号密码/短信/扫码/微信/支付宝/华为账号等
    任意方式登录，登录成功后 portal 回调 127.0.0.1:port/oauth/callback?code=...，本代理不关心
    用户用哪种方式登的，只认 code。"""
    if force_qr or not _have_display():
        return _qr_login(timeout)
    import webbrowser
    pkce = _gen_pkce()
    dpop = _gen_dpop_keypair()
    # 找一个空闲端口起 callback server（先占住，URL 里 port 才准确）
    import socket
    ss = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ss.bind(("127.0.0.1", 0)); port = ss.getsockname()[1]; ss.close()
    ticket_id = secrets.token_hex(16)
    authorize_url = ("%s/authorize?theme=Dark&locale=zh-cn&uri_scheme=%s&client_id=%s&port=%d"
                     "&code_challenge=%s&code_challenge_method=%s&ticket_id=%s&plugin-name=%s&plugin-version=%s" % (
                         PORTAL_HOST, CLIENT_ID, CLIENT_ID, port,
                         pkce["codeChallenge"], pkce["codeChallengeMethod"],
                         ticket_id, LOGIN_PLUGIN_NAME, EXTENSION_VERSION))
    _LoginCallbackServer.code = None
    _LoginCallbackServer.error = None
    print("\n正在打开浏览器登录（端口 %d）..." % port)
    print("在浏览器里可选任意方式登录：账号密码 / 短信验证码 / 扫码 / 微信 / 支付宝 / 华为账号")
    print("若浏览器未自动弹出，手动访问:\n  %s" % authorize_url)
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass   # 已打印 URL，用户手动点
    code = _wait_for_code(port, timeout)
    print("收到授权码，正在换取凭证...")
    return _exchange_code(code, pkce, dpop, port)


def login_to_pool(timeout: int = LOGIN_TIMEOUT_SEC, force_qr: bool = False) -> str:
    """登录并把新登录态并入账号池。返回账号标签。
    force_qr=True 强制终端二维码（Linux 无浏览器场景）。"""
    s = _browser_login(timeout, force_qr=force_qr)
    s["ot_session_id"] = _uid()
    s["user_session_id"] = _uid()
    s["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return _add_session_to_pool(s)


def _refresh_session(session: dict) -> dict:
    """用 refresh_token + DPoP 调 STS OAuth 端点换新 CAS 凭证。返回新 session dict。"""
    lc = session.get("loginContext") or {}
    pkce = lc.get("pkcePair") or {}
    dpop = lc.get("dpopKeyPair") or {}
    if not session.get("refresh_token") or not pkce.get("codeVerifier") or not dpop.get("privateKeyJwk"):
        raise RuntimeError("登录态缺少 refresh_token/PKCE/DPoP，无法自动续期（需在客户端重新登录）")
    data = _token_request({
        "client_id": CLIENT_ID,
        "code_verifier": pkce["codeVerifier"],
        "grant_type": "refresh_token",
        "refresh_token": session["refresh_token"],
    }, pkce, dpop)
    new = _session_from_sts(data, session, pkce, dpop)
    new["loginContext"] = lc   # PKCE/DPoP 密钥对不变
    return new


def _update_pool_account(old: dict, new: dict):
    """刷新成功后把新凭证写回池文件对应条目（保留 ot_session_id 等池元数据）。"""
    with _pool_lock:
        for a in _pool["accounts"]:
            if a.get("ot_session_id") == old.get("ot_session_id"):
                for k in ("ak", "sk", "securitytoken", "exp", "expires_at",
                          "refresh_token", "loginContext", "safelyRenewTokenInterval"):
                    if k in new:
                        a[k] = new[k]
                _save_pool_locked()
                return


def _maybe_refresh(session: dict, pool: bool) -> dict:
    """临期（< REFRESH_AHEAD_SEC）自动续期。pool=True 写回池文件；否则更新内存缓存。"""
    exp = session.get("exp") or 0
    if exp and exp - time.time() > REFRESH_AHEAD_SEC:
        return session   # 未临期
    sid = session.get("ot_session_id") or "client"
    with _refresh_lock:
        if sid in _refreshing:
            return session   # 别的线程正在刷，先用旧的
        _refreshing.add(sid)
    try:
        new = _refresh_session(session)
    except Exception as e:
        _print_err("自动续期失败(%s): %s" % (sid, e))
        if pool:
            _mark_session_failed(session)   # 刷新失败 → 标记过期，轮询跳过
            return session   # 返回旧的（上游若 401 会触发自愈换号）
        # 客户端模式：refresh_token 也死了，fallback 浏览器登录重新拿一套凭证
        if AUTO_BROWSER_LOGIN:
            try:
                _print_err("refresh_token 失效，触发浏览器登录重新建立会话...")
                new = _browser_login()
                new["ot_session_id"] = session.get("ot_session_id") or _uid()
                new["user_session_id"] = session.get("user_session_id") or _uid()
                new["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            except Exception as e2:
                _print_err("浏览器登录失败(%s): %s" % (sid, e2))
                return session   # 返回旧的，上游 401 会报错让用户手动 --login
        else:
            return session
    finally:
        with _refresh_lock:
            _refreshing.discard(sid)
    if pool:
        _update_pool_account(session, new)
    else:
        _session_cache.update({"at": time.time(), "data": new})
    _print_err("自动续期成功(%s): 新过期 %s" % (sid, new.get("expires_at")))
    return new


def _read_session() -> dict:
    """取凭证：账号池非空时走池（round-robin，过期自动跳过）；否则回退客户端登录态。
    取出后若临期（< 1h）自动续期。"""
    if os.path.exists(ACCOUNTS_FILE):
        with _pool_lock:
            _load_pool_locked()
            has = bool(_pool["accounts"])
        if has:
            return _maybe_refresh(_next_account(), pool=True)
    return _maybe_refresh(_read_client_session(), pool=False)


# ── AKSK 签名（复刻 agentkernel 的 AKSKSigner.sign，华为云 SDK-HMAC-SHA256）──

_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _url_encode(s: str) -> str:
    """复刻内核 noEscape 表：字母数字 - . _ ~ 不转义，其余 %XX 大写（按 UTF-8 字节）"""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if b < 128 and c in _SAFE:
            out.append(c)
        else:
            out.append("%%%02X" % b)
    return "".join(out)


EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def sign_request(method: str, url: str, headers: dict, ak: str, sk: str) -> dict:
    """
    对给定头集合做 SDK-HMAC-SHA256 签名，返回补齐后的完整头 dict。
    注意：
      - canonicalURI 每段 urlEncode 且末尾必加 /
      - payloadHash 取 headers["X-Sdk-Content-Sha256"]，没有则用空体 SHA256
      - POST chat 必须显式带 X-Sdk-Content-Sha256: UNSIGNED-PAYLOAD（实测不带会 401）
    """
    p = urllib.parse.urlsplit(url)
    dt = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    all_headers = dict(headers)
    all_headers["X-Sdk-Date"] = dt
    host = p.hostname
    if p.port and not ((p.scheme == "https" and p.port == 443) or (p.scheme == "http" and p.port == 80)):
        host = "%s:%d" % (host, p.port)
    all_headers["Host"] = host

    # canonicalURI
    uri = "/".join(_url_encode(seg) for seg in (p.path or "/").split("/"))
    if not uri.endswith("/"):
        uri += "/"

    # canonicalQueryString
    qs = ""
    if p.query:
        params = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        qs = "&".join("%s=%s" % (_url_encode(k), _url_encode(v)) for k, v in sorted(params))

    sorted_keys = sorted(all_headers.keys(), key=lambda x: x.lower())
    signed_names = ";".join(k.lower() for k in sorted_keys)
    canonical_headers = ""
    for k in sorted_keys:
        v = str(all_headers[k]).replace("\r", " ").replace("\n", " ").strip()
        canonical_headers += "%s:%s\n" % (k.lower(), v)

    payload_hash = all_headers.get("X-Sdk-Content-Sha256") or EMPTY_BODY_SHA256
    canonical_request = "\n".join([method.upper(), uri, qs, canonical_headers, signed_names, payload_hash])
    creq_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "SDK-HMAC-SHA256\n%s\n%s" % (dt, creq_hash)
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    all_headers["Authorization"] = "SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s" % (ak, signed_names, signature)
    return all_headers


def upstream_headers(session: dict) -> dict:
    """构造 chat 请求头并签名（复刻内核 getInferhubHcHeaders 的头集合 + UNSIGNED-PAYLOAD）。
    session id 取自账号条目（池模式每账号独立）或进程级全局值（单账号模式）：
    上游按 x-ot-session-id 计并发会话数，独立 id 等效扩容。"""
    nid = _uid
    h = {
        "X-Security-Token": session["securitytoken"],
        "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",   # 实测必需：POST 网关要求显式声明
        "x-ot-trace-id": nid(),
        "x-ot-span-id": nid(),
        "x-snap-traceid": nid(),
        "x-ot-session-id": session.get("ot_session_id") or _OT_SESSION_ID,
        "user-session-id": session.get("user_session_id") or _USER_SESSION_ID,
        "X-Language": "zh-cn",
    }
    return sign_request("POST", UPSTREAM, h, session["ak"], session["sk"])


# 忽略系统代理（公司代理可能不支持直连回环/篡改头）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ── 上游 keep-alive 连接池（性能关键：省掉每次请求的 TCP+TLS 握手）─────────────
# https.request 直连，绕过 urllib 的 opener 栈（更少开销）。每线程从池里借连接，
# 用完归还；坏连接（上游 keep-alive 超时关闭）自动重建，对调用方透明。
_UPSTREAM_HOST = urllib.parse.urlsplit(UPSTREAM).hostname
_conn_pool = []
_conn_pool_lock = threading.Lock()


def _upstream_request(payload: bytes, headers: dict, timeout: int):
    """POST 到上游，优先复用池内 keep-alive 连接。返回 (resp, status_code)。
    resp 是 http.client.HTTPResponse，调用方负责读完后 close()。
    归还条件（全部满足才回池）：
      - 响应是 keep-alive（非 Connection: close）
      - 响应被完整读取（chunked 正常收尾，length 归零）
      - 无 IncompleteRead 等异常（读一半的连接状态不可信，直接丢弃）
    流式响应（SSE/chunked 长读）几乎必然走"读一半或异常关闭"路径 → 不回池，
    避免把半截状态的连接借给下一个请求（正是上一版 IncompleteRead 崩溃的根因）。"""
    conn = None
    with _conn_pool_lock:
        if _conn_pool:
            conn = _conn_pool.pop()
    if conn is None:
        conn = http.client.HTTPSConnection(_UPSTREAM_HOST, timeout=timeout)
    send_headers = {k: v for k, v in headers.items() if k.lower() != "host"}
    send_headers["Content-Type"] = "application/json"
    send_headers["Content-Length"] = str(len(payload))
    send_headers["Accept"] = "application/json"
    send_headers["Accept-Encoding"] = "identity"   # http.client 不自动解 gzip，禁用压缩
    req_path = UPSTREAM.split(_UPSTREAM_HOST, 1)[1] if _UPSTREAM_HOST in UPSTREAM else UPSTREAM
    try:
        try:
            conn.request("POST", req_path, body=payload, headers=send_headers)
            resp = conn.getresponse()
        except Exception:
            # 池里借来的连接可能已被上游超时关闭：重建一次（不归还坏连接）
            try:
                conn.close()
            except Exception:
                pass
            conn = http.client.HTTPSConnection(_UPSTREAM_HOST, timeout=timeout)
            conn.request("POST", req_path, body=payload, headers=send_headers)
            resp = conn.getresponse()

        # 判断响应结束后连接是否可复用
        keep_alive = (resp.headers.get("Connection") or "").lower() != "close"
        resp_status = resp.status

        def _close():
            nonlocal conn, resp
            try:
                fully_read = resp.isclosed() or (resp.length is not None and resp.length <= 0)
            except Exception:
                fully_read = False
            try:
                resp.close()
            except Exception:
                pass
            if keep_alive and fully_read:
                try:
                    with _conn_pool_lock:
                        if len(_conn_pool) < 8:
                            _conn_pool.append(conn)
                    return
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
        resp.close = _close
        return resp, resp_status
    except Exception:
        # 任何发送/响应层异常：连接状态不可信，直接丢弃
        try:
            conn.close()
        except Exception:
            pass
        raise

def _uid() -> str:
    """32 位无连字符 uuid（trace/session id，等价 uuid4().hex）"""
    return uuid.uuid4().hex


# 会话 id：进程级复用（上游按 x-ot-session-id 计并发会话数，每次随机新值会迅速
# 触发 TM.00001041 "并发会话数已达上限(3个)"；内核与 IDE 也是长会话复用同一 id）
_OT_SESSION_ID = _uid()
_USER_SESSION_ID = _uid()


# ── 协议转换：Responses / Anthropic → OpenAI Chat 请求 ────────────────────
def _rand():
    return hashlib.md5(os.urandom(8)).hexdigest()[:24]


def convert_responses_to_openai(body: dict) -> dict:
    """/v1/responses 请求体 → OpenAI chat.completions 请求体。"""
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("max_output_tokens") is not None:
        oa["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oa["top_p"] = body["top_p"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("stop") is not None:
        oa["stop"] = body["stop"]
    if (body.get("reasoning") or {}).get("effort"):
        oa["reasoning_effort"] = body["reasoning"]["effort"]

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
                        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text") and part.get("text"):
                            text += part["text"]
                        elif isinstance(part, dict) and part.get("type") == "input_image":
                            text += "[图片]"
                if text:
                    msgs.append({"role": role, "content": text})
            elif t == "function_call":
                tc = {"id": item.get("call_id", ""), "type": "function",
                      "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")}}
                if msgs and msgs[-1].get("role") == "assistant" and "tool_calls" in msgs[-1]:
                    msgs[-1]["tool_calls"].append(tc)
                else:
                    msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
            elif t == "function_call_output":
                o = item.get("output")
                out_text = ""
                if isinstance(o, str):
                    out_text = o
                elif isinstance(o, list):
                    for part in o:
                        if isinstance(part, dict) and part.get("text"):
                            out_text += part["text"]
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": out_text})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]

    if isinstance(body.get("tools"), list):
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict) or t.get("type") != "function":
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            })
        if tools:
            oa["tools"] = tools
    if body.get("tool_choice") is not None:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "function" and tc.get("name"):
            oa["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        else:
            oa["tool_choice"] = tc
    return oa


def convert_anthropic_to_openai(body: dict) -> dict:
    """/v1/messages 请求体 → OpenAI chat.completions 请求体。"""
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("max_tokens") is not None:
        oa["max_tokens"] = body["max_tokens"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oa["top_p"] = body["top_p"]
    if body.get("stop_sequences") is not None:
        oa["stop"] = body["stop_sequences"]
    if (body.get("thinking") or {}).get("type") == "enabled":
        oa.pop("temperature", None)
        oa["reasoning_effort"] = "high"

    msgs = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        sys_text = ""
        for part in sys_:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                sys_text += part["text"] + "\n"
        if sys_text.strip():
            msgs.append({"role": "system", "content": sys_text.strip()})

    for msg in body.get("messages") or []:
        role = msg.get("role")
        c = msg.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        if not isinstance(c, list):
            continue
        if role == "assistant":
            text_parts = []
            tool_calls = []
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "thinking":
                    pass
                elif part.get("type") == "tool_use":
                    tool_calls.append({
                        "id": part.get("id", ""),
                        "type": "function",
                        "function": {"name": part.get("name", ""),
                                     "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False)},
                    })
            amsg = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                amsg["tool_calls"] = tool_calls
            msgs.append(amsg)
        elif role == "user":
            for part in c:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    rc = part.get("content")
                    res = ""
                    if isinstance(rc, str):
                        res = rc
                    elif isinstance(rc, list):
                        for cc in rc:
                            if isinstance(cc, dict) and cc.get("type") == "text" and cc.get("text"):
                                res += cc["text"]
                    tmsg = {"role": "tool", "tool_call_id": part.get("tool_use_id", ""), "content": res}
                    if part.get("name"):
                        tmsg["name"] = part["name"]
                    msgs.append(tmsg)
            parts = []
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    parts.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "image":
                    src = part.get("source") or {}
                    if src.get("type") == "url" and src.get("url"):
                        parts.append({"type": "image_url", "image_url": {"url": src["url"]}})
                    elif src.get("data"):
                        parts.append({"type": "image_url", "image_url": {
                            "url": "data:%s;base64,%s" % (src.get("media_type", "image/png"), src["data"])}})
            if parts:
                msgs.append({"role": "user", "content": parts})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]

    if isinstance(body.get("tools"), list):
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        if tools:
            oa["tools"] = tools
    if body.get("tool_choice") is not None:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "tool" and tc.get("name"):
            oa["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif isinstance(tc, str):
            oa["tool_choice"] = "required" if tc == "any" else ("none" if tc == "none" else "auto")
    return oa


def parse_anthropic_text(body: dict) -> list:
    """从 Anthropic Messages 协议提取全部文本（count_tokens 本地估算用）。"""
    texts = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        texts.append(sys_)
    elif isinstance(sys_, list):
        for part in sys_:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
    for msg in body.get("messages") or []:
        c = msg.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                t = part.get("type")
                if t == "text" and part.get("text"):
                    texts.append(part["text"])
                elif t == "tool_use":
                    texts.append(json.dumps(part, ensure_ascii=False))
                elif t == "tool_result":
                    rc = part.get("content")
                    if isinstance(rc, str):
                        texts.append(rc)
                    elif isinstance(rc, list):
                        for c2 in rc:
                            if isinstance(c2, dict) and c2.get("type") == "text" and c2.get("text"):
                                texts.append(c2["text"])
    return texts


# ── 协议转换：OpenAI Chat 响应 → Responses / Anthropic ─────────────────────
def convert_openai_to_responses(openai_obj: dict, req_body: dict) -> dict:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    output = []
    if message.get("reasoning_content"):
        output.append({"type": "reasoning", "id": "rs_" + _rand(),
                       "summary": [{"type": "summary_text", "text": message["reasoning_content"]}]})
    if message.get("content"):
        output.append({"type": "message", "id": "msg_" + _rand(), "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for tc in message.get("tool_calls") or []:
        output.append({
            "type": "function_call", "id": "fc_" + _rand(), "call_id": tc.get("id", ""),
            "name": (tc.get("function") or {}).get("name", ""),
            "arguments": (tc.get("function") or {}).get("arguments", "{}"), "status": "completed",
        })
    incomplete = choice.get("finish_reason") == "length"
    return {
        "id": "resp_" + _rand(),
        "object": "response",
        "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": req_body.get("model", ""),
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
    }


def convert_openai_to_anthropic(openai_obj: dict, req_body: dict) -> dict:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    content = []
    if message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        try:
            input_obj = json.loads((tc.get("function") or {}).get("arguments", "{}"))
        except Exception:
            input_obj = {}
        content.append({"type": "tool_use", "id": tc.get("id", "toolu_" + _rand()),
                        "name": (tc.get("function") or {}).get("name", ""), "input": input_obj})
    finish = choice.get("finish_reason")
    if finish == "stop":
        stop_reason = "end_turn"
    elif finish == "tool_calls":
        stop_reason = "tool_use"
    elif finish == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = finish or "end_turn"
    return {
        "id": "msg_" + _rand(),
        "type": "message",
        "role": "assistant",
        "model": req_body.get("model", ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ── 流式状态机：上游 OpenAI chat SSE → Responses / Anthropic SSE ───────────
class _ResponsesStreamState:
    """上游 chat chunk → Responses 协议事件序列（codex-cli 依赖 response.completed 等）。"""

    def __init__(self, model: str):
        self.model = model
        self.resp_id = "resp_" + _rand()
        self.created = int(time.time())
        self.started = False
        self.finished = False
        self.finish_reason = None
        self.text = ""
        self.reasoning = ""
        self.tool_args = ""
        self.tool_name = ""
        self.tool_id = ""
        self.usage = None
        self.text_item_id = "msg_" + _rand()
        self.reason_item_id = "rs_" + _rand()
        self.reason_opened = False
        self.text_opened = False
        self.reason_index = None
        self.text_index = None
        self.next_output_index = 0

    def feed(self, chunk: dict):
        out = []
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        if not self.started:
            self.started = True
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        rc = delta.get("reasoning_content")
        if rc:
            self.reasoning += rc
            if not self.reason_opened:
                self.reason_opened = True
                idx = self.next_output_index
                self.next_output_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": idx,
                    "item": {"type": "reasoning", "id": self.reason_item_id, "summary": []},
                }, ensure_ascii=False)))
                out.append(("response.reasoning_summary_part.added", json.dumps({
                    "type": "response.reasoning_summary_part.added", "item_id": self.reason_item_id,
                    "output_index": idx, "summary_index": 0, "part": {"type": "summary_text", "text": ""},
                }, ensure_ascii=False)))
                self.reason_index = idx
            out.append(("response.reasoning_summary_text.delta", json.dumps({
                "type": "response.reasoning_summary_text.delta", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "delta": rc,
            }, ensure_ascii=False)))

        content = delta.get("content")
        if content:
            self.text += content
            if not self.text_opened:
                self.text_opened = True
                idx = self.next_output_index
                self.next_output_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": idx,
                    "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                             "status": "in_progress", "content": []},
                }, ensure_ascii=False)))
                out.append(("response.content_part.added", json.dumps({
                    "type": "response.content_part.added", "item_id": self.text_item_id,
                    "output_index": idx, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }, ensure_ascii=False)))
                self.text_index = idx
            out.append(("response.output_text.delta", json.dumps({
                "type": "response.output_text.delta", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "delta": content,
            }, ensure_ascii=False)))

        if isinstance(delta, dict) and delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                fn = tc.get("function") or {}
                if tc.get("id"):
                    self.tool_id = tc["id"]
                if fn.get("name"):
                    self.tool_name = fn["name"]
                if fn.get("arguments"):
                    self.tool_args += fn["arguments"]
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        if not self.started:
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))

        if self.reason_opened:
            idx = self.reason_index
            out.append(("response.reasoning_summary_text.done", json.dumps({
                "type": "response.reasoning_summary_text.done", "item_id": self.reason_item_id,
                "output_index": idx, "summary_index": 0, "text": self.reasoning,
            }, ensure_ascii=False)))
            out.append(("response.reasoning_summary_part.done", json.dumps({
                "type": "response.reasoning_summary_part.done", "item_id": self.reason_item_id,
                "output_index": idx, "summary_index": 0,
                "part": {"type": "summary_text", "text": self.reasoning},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": idx,
                "item": {"type": "reasoning", "id": self.reason_item_id,
                         "summary": [{"type": "summary_text", "text": self.reasoning}]},
            }, ensure_ascii=False)))
            self.reason_opened = False
        if self.text_opened:
            idx = self.text_index
            out.append(("response.output_text.done", json.dumps({
                "type": "response.output_text.done", "item_id": self.text_item_id,
                "output_index": idx, "content_index": 0, "text": self.text,
            }, ensure_ascii=False)))
            out.append(("response.content_part.done", json.dumps({
                "type": "response.content_part.done", "item_id": self.text_item_id,
                "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": idx,
                "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": self.text, "annotations": []}]},
            }, ensure_ascii=False)))
            self.text_opened = False

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
                          "status": "completed", "content": [{"type": "output_text", "text": "", "annotations": []}]})
        incomplete = self.finish_reason == "length"
        usage = self.usage or {}
        out.append(("response.completed", json.dumps({
            "type": "response.completed",
            "response": {
                "id": self.resp_id, "object": "response", "created_at": self.created,
                "status": "incomplete" if incomplete else "completed",
                "model": self.model, "output": items,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
                "error": None,
                "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            },
        }, ensure_ascii=False)))
        return out


class _AnthropicStreamState:
    """上游 chat chunk → Anthropic Messages SSE 事件序列（message_start/stop）。"""

    def __init__(self, model: str):
        self.model = model
        self.msg_id = "msg_" + _rand()
        self.started = False
        self.finished = False
        self.text_block = False
        self.thinking_block = False
        self.block_index = 0
        self.text = ""
        self.thinking = ""
        self.tool_blocks = {}
        self.usage = None

    def _start(self, out):
        if self.started:
            return
        self.started = True
        out.append(("message_start", json.dumps({
            "type": "message_start",
            "message": {
                "id": self.msg_id, "type": "message", "role": "assistant", "content": [],
                "model": self.model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }, ensure_ascii=False)))

    def feed(self, chunk: dict):
        out = []
        self._start(out)
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        delta = choice.get("delta") or {}
        rc = delta.get("reasoning_content")
        content = delta.get("content")
        if rc:
            if not self.thinking_block:
                self.thinking_block = True
                idx = self.block_index
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                }, ensure_ascii=False)))
            self.thinking += rc
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "thinking_delta", "thinking": rc},
            }, ensure_ascii=False)))
        if content:
            if self.thinking_block:
                out.append(("content_block_stop", json.dumps({
                    "type": "content_block_stop", "index": self.block_index - 1,
                }, ensure_ascii=False)))
                self.thinking_block = False
            if not self.text_block:
                self.text_block = True
                idx = self.block_index
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""},
                }, ensure_ascii=False)))
            self.text += content
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "text_delta", "text": content},
            }, ensure_ascii=False)))
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                fn = tc.get("function") or {}
                idx = tc.get("index", 0)
                if tc.get("id") or fn.get("name"):
                    if idx not in self.tool_blocks:
                        # 记录该 tool 对应的本协议块 index：多 tool 并发流式时 delta/stop 都要各归各的块
                        self.tool_blocks[idx] = {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": "",
                                                 "block_index": self.block_index}
                        self.block_index += 1
                        out.append(("content_block_start", json.dumps({
                            "type": "content_block_start", "index": self.tool_blocks[idx]["block_index"],
                            "content_block": {"type": "tool_use", "id": tc.get("id", ""),
                                              "name": fn.get("name", ""), "input": {}},
                        }, ensure_ascii=False)))
                if fn.get("arguments"):
                    tb = self.tool_blocks[idx]
                    tb["args"] += fn["arguments"]
                    out.append(("content_block_delta", json.dumps({
                        "type": "content_block_delta", "index": tb["block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    }, ensure_ascii=False)))
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        self._start(out)
        if self.thinking_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.thinking_block = False
        if self.text_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.text_block = False
        for idx in self.tool_blocks:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.tool_blocks[idx]["block_index"]},
                ensure_ascii=False)))
        usage = self.usage or {}
        stop_reason = "tool_use" if self.tool_blocks else "end_turn"
        out.append(("message_delta", json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }, ensure_ascii=False)))
        out.append(("message_stop", json.dumps({"type": "message_stop"}, ensure_ascii=False)))
        return out


# ── HTTP Handler ──────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass   # 用户明确：不写日志

    def _check_api_key(self) -> bool:
        """API Key 鉴权（API_KEY 为空 = 不鉴权）。失败直接回 401。"""
        if not API_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], API_KEY):
            return True
        if secrets.compare_digest(self.headers.get("X-Api-Key", ""), API_KEY):
            return True
        self._json(401, {"error": {"message": "无效或缺失 API Key"}})
        return False

    def _read_body(self):
        """读请求体；超过 MAX_BODY_BYTES 回 413，返回 None。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": {"message": "请求体过大（上限 %d MB）" % (MAX_BODY_BYTES >> 20)}})
            return None
        return self.rfile.read(length)

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                try:
                    if os.path.exists(ACCOUNTS_FILE):
                        with _pool_lock:
                            _load_pool_locked()
                            accs = list(_pool["accounts"])
                        if accs:
                            now = time.time()
                            return self._json(200, {
                                "ok": True,
                                "mode": "pool",
                                "accounts": [{
                                    "account": _label_of(a),
                                    "expires_at": a.get("expires_at") or "unknown",
                                    "expired": bool(a.get("exp") and a["exp"] < now),
                                } for a in accs],
                            })
                    s = _read_client_session()
                    return self._json(200, {"ok": True, "mode": "client", "account": s["account"],
                                            "credExp": s["expires_at"] or "unknown"})
                except Exception as e:
                    return self._json(200, {"ok": False, "error": str(e)})
            if path in ("/v1/models", "/models"):
                now = int(time.time())
                return self._json(200, {
                    "object": "list",
                    "data": [{
                        "id": mid,
                        "object": "model",
                        "created": now,
                        "owned_by": "huawei-codearts",
                        "metadata": {"name": m["name"], "context_window": m["ctx"], "desc": m["desc"]},
                    } for mid, m in MODELS.items()],
                })
            return self._json(404, {"error": {"message": "支持: GET /v1/models, GET /health, POST /v1/chat/completions"}})
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._check_api_key():
            return
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        if path in ("/v1/messages",):
            return self._handle_chat("anthropic")
        if path in ("/v1/responses", "/responses"):
            return self._handle_chat("responses")
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._handle_chat("openai")
        # 未知路径 404 前必须读掉请求体，否则 HTTP/1.1 keep-alive 下残留 body 会污染下一个请求
        try:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except Exception:
            pass
        return self._json(404, {"error": {"message": "支持: GET /v1/models, GET /health, POST /v1/chat/completions, POST /v1/responses, POST /v1/messages"}})

    def _handle_count_tokens(self):
        try:
            body = self._read_body()
            if body is None:
                return
            req = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})
        total_chars = sum(len(t) for t in parse_anthropic_text(req))
        return self._json(200, {"input_tokens": max(1, total_chars // 3)})

    def _handle_chat(self, protocol: str = "openai"):
        """
        protocol: 'openai' | 'responses' | 'anthropic'
        三种协议统一转成 OpenAI chat 发上游，响应再转回目标协议。
        """
        # 1) 读请求体
        try:
            body = self._read_body()
            if body is None:
                return
            req_body = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})

        # 2) 协议请求转换 → OpenAI chat
        req_model_name = req_body.get("model") or ""   # 客户端请求的原始名（响应回填用）
        if protocol == "responses":
            req = convert_responses_to_openai(req_body)
        elif protocol == "anthropic":
            req = convert_anthropic_to_openai(req_body)
        else:
            req = dict(req_body)

        # 3) 模型解析（别名 → 官方 id）
        model = req.get("model") or ""
        real = MODEL_ALIAS.get(model, model)
        if real not in MODELS:
            return self._json(404, {"error": {"message": "模型 %s 不存在。可用: %s" % (model, ", ".join(sorted(MODELS)))}})
        req["model"] = real
        minfo = MODELS[real]

        # 3a) max_tokens 硬限：客户端若指定 max_tokens 超过上游硬限，直接钳制到硬限，
        #     避免上游报 limit_err（81027/81001）。不指定则不动（让上游自己按硬限截断）。
        hard_cap = minfo.get("max_tokens")
        if hard_cap and isinstance(req.get("max_tokens"), int) and req["max_tokens"] > hard_cap:
            req["max_tokens"] = hard_cap

        is_stream = bool(req.get("stream"))

        # 4+5) 凭证 + 签名 + 转发上游（401 重试一次：池模式换下一个账号；单账号模式清缓存重取）
        resp = None
        payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            try:
                session = _read_session()
                headers = upstream_headers(session)
            except Exception as e:
                return self._json(500, {"error": {"message": str(e)}})

            try:
                resp, st_code = _upstream_request(payload, headers, timeout=600)
                if st_code == 401 and attempt == 0:
                    try:
                        resp.close()   # close 归还连接（401 响应体小，已读完则可复用）
                    except Exception:
                        pass
                    _mark_session_failed(session)   # 标记失效后重取凭证（换下一个账号或重刷）再试一次
                    continue
                if st_code >= 400:
                    detail = ""
                    try:
                        detail = resp.read().decode("utf-8", "replace")[:500]
                    except Exception:
                        pass
                    try:
                        resp.close()
                    except Exception:
                        pass
                    # 401 重试第二次也失败：把 detail 透传（含上游错误详情）
                    # 上游 token 硬限错误（81027/81001 等）：转成更友好的 413 + 中文提示
                    if minfo.get("limit_err") and str(minfo["limit_err"]) in detail:
                        cap = minfo.get("max_tokens", "?")
                        return self._json(413, {"error": {"message": "请求超出 %s 上游硬限（input+output ≤ %s tokens，错误码 %s）。请减少上下文或缩短 max_tokens。" % (real, cap, minfo["limit_err"]), "code": minfo["limit_err"]}})
                    _print_err("上游 %s %s: %s" % (st_code, real, detail[:200]))
                    return self._json(st_code, {"error": {"message": "上游 %s: %s" % (st_code, detail)}})
                break
            except Exception as e:
                return self._json(502, {"error": {"message": "上游连接失败: %s" % e}})
        if resp is None:   # 理论不可达（401 循环已处理），防御
            return self._json(502, {"error": {"message": "上游请求未完成"}})

        # 6a) 流式：SSE 按协议转换转发
        if is_stream:
            self.send_response(resp.status)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            st = None
            if protocol == "responses":
                st = _ResponsesStreamState(req_model_name)
            elif protocol == "anthropic":
                st = _AnthropicStreamState(req_model_name)

            def _write_sse(ev, payload):
                line_out = ("event: %s\n" % ev if ev else "") + "data: %s\n\n" % payload
                b_out = line_out.encode("utf-8")
                self.wfile.write(b"%x\r\n" % len(b_out) + b_out + b"\r\n")
                self.wfile.flush()

            buf = b""
            data_parts = []
            cur_event = ""
            final_sent = False

            def flush_event():
                nonlocal data_parts, cur_event, final_sent
                if not data_parts:
                    cur_event = ""
                    return
                data_text = "\n".join(data_parts)
                event_name = cur_event
                cur_event = ""
                data_parts = []
                if data_text == "[DONE]":
                    return
                try:
                    obj = json.loads(data_text)
                except Exception:
                    return
                # 流式途中收到上游 token 硬限错误：提前结束流，写一条 error 事件给客户端
                err_code = (obj.get("error") or {}).get("code") if isinstance(obj.get("error"), dict) else None
                if err_code == minfo.get("limit_err") or (minfo.get("limit_err") and str(minfo["limit_err"]) in data_text and obj.get("error")):
                    cap = minfo.get("max_tokens", "?")
                    err_msg = "请求超出 %s 上游硬限（input+output ≤ %s tokens，错误码 %s）。请减少上下文或缩短 max_tokens。" % (real, cap, minfo["limit_err"])
                    if protocol == "anthropic":
                        _write_sse("error", json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": err_msg}}, ensure_ascii=False))
                    elif protocol == "responses":
                        _write_sse("response.failed", json.dumps({"type": "response.failed", "response": {"id": "resp_" + _rand(), "status": "failed", "error": {"code": minfo["limit_err"], "message": err_msg}}}, ensure_ascii=False))
                    else:
                        _write_sse("", json.dumps({"error": {"message": err_msg, "code": minfo["limit_err"]}}, ensure_ascii=False))
                    final_sent = True
                    return
                if st is not None:
                    for ev, pl in st.feed(obj):
                        _write_sse(ev, pl)
                        if ev in ("response.completed", "message_stop"):
                            final_sent = True
                else:
                    _write_sse("", json.dumps(obj, ensure_ascii=False))
                    if obj.get("choices") and obj["choices"][0].get("finish_reason"):
                        _write_sse("", "[DONE]")
                        final_sent = True

            try:
                while True:
                    try:
                        chunk = resp.read(4096)
                    except Exception:
                        # 上游流中断（IncompleteRead/超时等）：已收到的内容照常收尾，
                        # 协议状态机自动补终止事件，客户端拿到完整格式的截断响应
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        sline = line.decode("utf-8", "replace").rstrip("\r")
                        if sline == "":
                            flush_event()
                        elif sline.startswith(":"):
                            continue
                        else:
                            idx = sline.find(":")
                            field = sline[:idx] if idx != -1 else sline
                            value = sline[idx + 1:] if idx != -1 else ""
                            if value.startswith(" "):
                                value = value[1:]
                            if field == "event":
                                cur_event = value
                            elif field == "data":
                                data_parts.append(value)
                flush_event()
                if st is not None and not final_sent:
                    for ev, pl in st.finish():
                        _write_sse(ev, pl)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass   # 客户端提前断开（取消/超时属正常噪音），静默
            finally:
                try:
                    resp.close()   # 归还/丢弃连接（_close 内部判断可否复用）
                except Exception:
                    pass
            return

        # 6b) 非流式：整读。上游可能对非流式请求也返回 SSE 伪流式（单个 data: 帧包完整
        #     chat.completion），此时聚合成 JSON 再做协议转换，保证客户端兼容。
        try:
            data = resp.read()
        except Exception:
            data = b""
        try:
            resp.close()
        except Exception:
            pass
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" in ct:
            text = data.decode("utf-8", "replace")
            frames = [ln[len("data:"):].strip() for ln in text.splitlines()
                      if ln.startswith("data:") and ln.strip() != "data: [DONE]"]
            if frames:
                try:
                    merged = json.loads(frames[-1])
                    if merged.get("object") == "chat.completion" and len(frames) == 1:
                        data = json.dumps(merged, ensure_ascii=False).encode("utf-8")
                        ct = "application/json"
                except Exception:
                    pass

        # 协议响应转换（openai 直通；responses/anthropic 转换后返回）
        if protocol == "responses":
            try:
                out = convert_openai_to_responses(json.loads(data.decode("utf-8")),
                                                  {"model": req_model_name})
                return self._json(200, out)
            except Exception as e:
                return self._json(502, {"error": {"message": "转换响应失败: %s" % e}})
        if protocol == "anthropic":
            try:
                out = convert_openai_to_anthropic(json.loads(data.decode("utf-8")),
                                                   {"model": req_model_name})
                return self._json(200, out)
            except Exception as e:
                return self._json(502, {"error": {"message": "转换响应失败: %s" % e}})

        self.send_response(resp.status)
        self.send_header("Content-Type", ct or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


class QuietServer(http.server.ThreadingHTTPServer):
    """客户端粗暴断开（RST/断管）时静默：浏览器预连接、keep-alive 竞态、
    客户端中途取消请求都会触发，属正常噪音。其余异常输出 stderr。"""

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        traceback.print_exc()


def _cmd_capture() -> int:
    if sys.platform != "win32":
        print("--capture 仅支持 Windows（需要 CodeArts Agent 客户端的 DPAPI 加密登录态）。")
        print("Linux 上请用 --import 从其他电脑导出的账号池文件导入，或用 --login 走浏览器登录。")
        return 1
    try:
        label = capture_to_pool()
    except Exception as e:
        print("[error] %s" % e)
        return 1
    print("已抓取账号: %s" % label)
    print("池文件: %s" % ACCOUNTS_FILE)
    return 0


def _cmd_login(force_qr: bool = False) -> int:
    """登录：有显示器开浏览器，无显示器（Linux server）终端渲染二维码扫码。跨平台。
    交互式下登完一个账号会问是否继续登下一个，可连续把多个账号加入池。"""
    count = 0
    while True:
        try:
            label = login_to_pool(force_qr=force_qr)
            count += 1
            print("已登录账号: %s（第 %d 个）" % (label, count))
            print("池文件: %s" % ACCOUNTS_FILE)
        except Exception as e:
            print("[error] %s" % e)
            return 1 if count == 0 else 0
        # 非交互（管道/重定向）登一次就结束；交互则问是否继续登下一个
        if not sys.stdin.isatty():
            break
        try:
            ans = input("\n继续登录下一个账号？(y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if ans not in ("y", "yes"):
            break
    with _pool_lock:
        _load_pool_locked()
        total = len(_pool["accounts"])
    print("本次登录 %d 个账号，池当前共 %d 个" % (count, total))
    return 0


def _cmd_list() -> int:
    with _pool_lock:
        _load_pool_locked()
        accs = list(_pool["accounts"])
    if not accs:
        print("账号池为空（%s 不存在或无账号）" % ACCOUNTS_FILE)
        print("请先在 CodeArts Agent 客户端登录，然后运行: python codearts_proxy.py --capture")
        return 0
    now = time.time()
    print("账号池（%d 个）:" % len(accs))
    for a in accs:
        expired = bool(a.get("exp") and a["exp"] < now)
        print("  %-28s %s %s" % (_label_of(a), a.get("expires_at") or "unknown", "[已过期]" if expired else ""))
    return 0


def _cmd_remove(label: str) -> int:
    with _pool_lock:
        _load_pool_locked()
        before = len(_pool["accounts"])
        _pool["accounts"] = [a for a in _pool["accounts"] if _label_of(a) != label]
        if len(_pool["accounts"]) == before:
            print("未找到账号 %r。当前池: %s" % (label, ", ".join(_label_of(a) for a in _pool["accounts"]) or "(空)"))
            return 1
        _save_pool_locked()
    print("已移除 %r。" % label)
    return 0


def _cmd_export(path: str) -> int:
    """导出当前账号池到指定 json 文件。"""
    with _pool_lock:
        _load_pool_locked()
        accs = list(_pool["accounts"])
    if not accs:
        print("账号池为空，无可导出内容。请先 --capture 抓取账号。")
        return 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"accounts": accs}, f, ensure_ascii=False, indent=2)
    print("已导出 %d 个账号到 %s" % (len(accs), path))
    return 0


def _cmd_import(path: str) -> int:
    """从指定 json 文件导入账号，合并到本地池（同标签覆盖）。"""
    if not os.path.exists(path):
        print("文件不存在: %s" % path)
        return 1
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        incoming = data.get("accounts", [])
        if not incoming:
            print("文件中没有账号数据。")
            return 1
    except Exception as e:
        print("读取失败: %s" % e)
        return 1
    with _pool_lock:
        _load_pool_locked()
        existing = {_label_of(a): i for i, a in enumerate(_pool["accounts"])}
        added, updated = 0, 0
        for a in incoming:
            label = _label_of(a)
            if label in existing:
                _pool["accounts"][existing[label]] = a
                updated += 1
            else:
                _pool["accounts"].append(a)
                existing[label] = len(_pool["accounts"]) - 1
                added += 1
        _save_pool_locked()
    print("导入完成: 新增 %d，覆盖 %d（当前池 %d 个账号）" % (added, updated, len(_pool["accounts"])))
    return 0


def _interactive_menu() -> int:
    """交互式主菜单：启动前选择动作。返回 (action, force_qr)。
    action: 'serve' | 'login' | 'capture' | 'list' | 'remove' | 'export' | 'import' | 'quit'
    仅在 stdin 是 tty 时弹出；非交互直接 serve。"""
    if not sys.stdin.isatty():
        return ('serve', False)
    while True:
        with _pool_lock:
            _load_pool_locked()
            accs = list(_pool["accounts"])
        now = time.time()
        print("\n" + "=" * 56)
        print("  CodeArts 官方模型本地代理")
        print("=" * 56)
        print("  账号池: %d 个账号" % len(accs))
        for a in accs:
            exp = a.get("exp") or 0
            tag = "[过期]" if exp and exp < now else "[有效]"
            print("    %s %s %s" % (_label_of(a), tag, (a.get("expires_at") or "")[:19]))
        if not accs and os.path.exists(STATE_VSCDB):
            print("  （未建池，可直接复用客户端登录态）")
        print("-" * 56)
        print("  0) 启动代理（用现有账号池 / 客户端登录态）")
        print("  1) 登录华为云加入账号池（浏览器/二维码，可连续登多个）")
        print("  2) 终端二维码登录（Linux server / SSH 无浏览器）")
        if sys.platform == "win32":
            print("  3) 抓取 CodeArts Agent 客户端当前登录态")
        print("  l) 查看账号池详情")
        print("  r) 移除账号")
        print("  e) 导出账号池（跨电脑迁移）")
        print("  i) 导入账号池")
        print("  q) 退出")
        print("-" * 56)
        try:
            choice = input("请选择: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return ('quit', False)
        if choice == "0" or choice == "":
            return ('serve', False)
        if choice == "1":
            return ('login', False)
        if choice == "2":
            return ('login', True)
        if choice == "3" and sys.platform == "win32":
            return ('capture', False)
        if choice == "l":
            _cmd_list(); continue
        if choice == "r":
            label = input("要移除的账号标签: ").strip()
            if label: _cmd_remove(label)
            continue
        if choice == "e":
            path = input("导出路径: ").strip()
            if path: _cmd_export(path)
            continue
        if choice == "i":
            path = input("导入文件路径: ").strip()
            if path: _cmd_import(path)
            continue
        if choice == "q":
            return ('quit', False)
        print("无效选择，重试")


def main():
    ap = argparse.ArgumentParser(description="CodeArts 官方模型本地代理（多账号轮询）")
    ap.add_argument("--capture", action="store_true", help="把当前 CodeArts Agent 登录态加入账号池")
    ap.add_argument("--login", action="store_true", help="登录华为云加入账号池：有显示器开浏览器，无显示器(Linux)终端二维码扫码")
    ap.add_argument("--qr", action="store_true", help="强制终端二维码扫码登录（不弹浏览器，适合 Linux server / SSH）")
    ap.add_argument("--list", action="store_true", help="查看账号池")
    ap.add_argument("--remove", metavar="LABEL", help="从账号池移除指定账号")
    ap.add_argument("--export", metavar="PATH", help="导出当前账号池到指定 json 文件")
    ap.add_argument("--import", metavar="PATH", dest="import_path", help="从指定 json 文件导入账号（合并，同标签覆盖）")
    ap.add_argument("--menu", action="store_true", help="启动前弹交互菜单（默认无参数且 tty 时自动弹）")
    ap.add_argument("--serve", action="store_true", help="跳过菜单直接启动代理")
    args = ap.parse_args()

    # 显式子命令优先
    if args.capture:
        sys.exit(_cmd_capture())
    if args.login:
        sys.exit(_cmd_login(force_qr=args.qr))
    if args.qr and not args.login:
        sys.exit(_cmd_login(force_qr=True))
    if args.list:
        sys.exit(_cmd_list())
    if args.remove:
        sys.exit(_cmd_remove(args.remove))
    if args.export:
        sys.exit(_cmd_export(args.export))
    if args.import_path:
        sys.exit(_cmd_import(args.import_path))

    # 无子命令：--serve 直接启动；否则弹交互菜单
    if not args.serve:
        action, force_qr = _interactive_menu()
        if action == 'quit':
            return
        if action == 'login':
            _cmd_login(force_qr=force_qr)
            # 登完回到菜单（可能还要启动或再登）
            return main()
        if action == 'capture':
            _cmd_capture()
            return main()

    server = QuietServer(("0.0.0.0", PORT), Handler)
    print("CodeArts 官方模型代理已启动: http://127.0.0.1:%d" % PORT)
    print("模型: %s" % ", ".join(sorted(MODELS)))
    if os.path.exists(ACCOUNTS_FILE):
        print("认证: 账号池轮询（%s）" % ACCOUNTS_FILE)
    else:
        print("认证: 自动复用 CodeArts Agent 登录态（多账号请用 --capture 建池）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
