# -*- coding: utf-8 -*-
"""
TRAE SOLO CN 官方模型本地代理 - 纯 Python 标准库 + cryptography

把 TRAE SOLO CN（字节）的官方模型通道转成本地 OpenAI 兼容 API：
  OpenAI 兼容    GET /v1/models、POST /v1/chat/completions
  OpenAI 新版    POST /v1/responses（Responses 协议，兼容 codex-cli 等）
  Anthropic 兼容 POST /v1/messages、POST /v1/messages/count_tokens
  健康检查       GET /health

上游（SOLO 免费聊天通道，不走 create_agent_task 编排）：
  POST /api/agent/v3/llm_utils_chat   请求体近 OpenAI 格式，SSE 事件为
      metadata / output{response, reasoning_content, tool_calls}
      / token_usage / done / error
  本代理把 SSE 事件翻译为 OpenAI chat chunk / Responses / Anthropic 三协议。

认证完全复用 TRAE SOLO CN 客户端登录态（算法移植自 trae-mate trae_auth.rs）：
  %APPDATA%/TRAE SOLO CN/User/globalStorage/storage.json
    → iCubeAuthInfo://icube.cloudide（base64 信封）
    → 信封 = HEADER(6B) + randomKey(32B) + AES-128-CBC 密文
    → secret = LEFT_SECRET ⊕ RIGHT_SECRET（64B 常量）
    → key/iv = SHA512( SHA512(randomKey) ++ secret )[0:16]/[16:32]
    → 明文 = SHA512(payload) + payload(JSON: token/userId/host/refreshToken...)
  token 临期自动续期（ExchangeToken），续期后写回 storage.json，无需手动刷新。

模型表自动同步（上游新增/下线模型，本代理自动跟进）：
  数据源 POST /api/ide/v1/get_detail_param（config_info_list 含 display/ctx/
  multimodal/capability/is_invisible_to_user 元数据），三条同步触发路径：
    1. 启动时后台线程立即拉一次；
    2. 之后每 models_ttl 秒定期拉（config.json "models_ttl"，默认 3600）；
    3. /v1/models 请求发现缓存过期时现场拉（失败按 models_fail_cooldown 负缓存冷却）。
  每次成功拉取：覆盖内存模型表（上游下线的模型即从 /v1/models 移除）
  → 原子写入 trae_models.json（tmp + os.replace，写坏不伤旧文件，重启免拉取）。
  /v1/models 只暴露"可见 + 有实质能力 + 白名单放行"的模型；隐形 subagent /
  custom_model 占位 / summary 内部模型仍在内部表，改白名单即可放开。
  白名单（目前只开放六个新一代模型，与 Trae CN 客户端模型目录一致）：
    glm-5.3 / glm-5.3-flashx / mimo-v2.6-pro / mimo-v2.6-flash /
    deepseek-v4.1-flash / minimax-m3.1-flash
  其余模型暂不开放（不进列表、聊天 404）；config.json "model_whitelist" 数组
  或环境变量 TRAE_PROXY_MODEL_WHITELIST（逗号分隔）即时调整，设为空 = 全部开放。
  公共名映射（通用规则，未写死任何模型名）：上游 config_name 带 -solo 后缀时
  对外自动去后缀（glm-5.3-solo ⇄ glm-5.3 双向映射），以此类推任何 -solo 模型；
  上游同时存在 glm-5.3 与 glm-5.3-solo 时原名持有者优先，-solo 版保留全名直连。

已知限制：
  1. 工具调用仅透传：tool_calls 翻译返回给客户端执行（云端工具不在本机跑）。
  2. 服务端排队（request_wait_in_queue）时首 token 延迟可达分钟级（Free 账号）。
  3. 每条消息是独立"回合"，对话历史被拼接进单条 query（无服务端多轮状态）。
  4. 图片输入不支持（转成"[图片输入暂不支持]"占位文本）。

用法:  python trae_proxy.py    （默认端口 8790，环境变量 TRAE_PROXY_PORT 修改）
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = int(os.environ.get("TRAE_PROXY_PORT", "8790"))
HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))

# TRAE SOLO CN 客户端用户数据
STORAGE_JSON = os.path.join(APPDATA, "TRAE SOLO CN", "User", "globalStorage", "storage.json")

# 上游网关（alaudalog 实测）
GATEWAY = "https://trae-api-cn.mchost.guru"

# —— SOLO 免费聊天通道（solo_work_remote，2026-10-28 本机实测打通）——————
# 旧通道 solo_work_lite 只有旧一代模型（glm-5.2/kimi-k2.6 等，无 glm-5.3）；
# remote 通道与 Trae CN 客户端模型目录一致，含新一代模型
# （glm-5.3/glm-5.3-flashx/mimo-v2.6/deepseek-v4.1/minimax-m3.1 等）。
# 不走 create_agent_task 编排，直接用 llm_utils_chat：请求体近 OpenAI 格式，
# SSE 事件为 metadata / output{response, reasoning_content, tool_calls}
# / token_usage / done / error。
EP_LLM_UTILS = GATEWAY + "/api/agent/v3/llm_utils_chat"
EP_GET_DETAIL_PARAM = GATEWAY + "/api/ide/v1/get_detail_param"
SOLO_FUNCTION = "solo_work_remote"
SOLO_IDE_VERSION = "0.1.43"
SOLO_IDE_VERSION_CODE = "20260716"
SOLO_DEVICE_BRAND = "83DG"

# 模型表自动同步（get_detail_param 拉取，详见文件头说明）：
#   成功 → 覆盖内存表 + 原子写 trae_models.json；失败 → 负缓存冷却后重试
# TRAE_PROXY_MODELS_FILE / config.json "models_file" 可改缓存文件路径
TRAE_MODELS_FILE = os.environ.get("TRAE_PROXY_MODELS_FILE") or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "trae_models.json")
SOLO_MODELS_TTL = 3600            # 定期同步间隔 / 正缓存 TTL（秒）
SOLO_MODELS_FAIL_COOLDOWN = 300   # 拉取失败后的重试冷却（秒）
_models_lock = threading.Lock()   # 保护 MODELS / MODEL_ALIAS / 公共名映射 / _models_state
_models_state = {"ts": 0, "fail_ts": 0}  # 最近一次成功 / 失败同步的 epoch
_public_to_upstream = {}   # 公共名(小写) → 上游 config_name（-solo 后缀自动去除）
_upstream_to_public = {}   # 上游 config_name → 公共展示名

# token 自动续期（traework2api client.go refreshLocked 移植）
OAUTH_HOST = "https://api.trae.com.cn"
EP_EXCHANGE = OAUTH_HOST + "/cloudide/api/v3/trae/oauth/ExchangeToken"
SOLO_CLIENT_ID = "en1oxy7wnw8j9n"
REFRESH_AHEAD_SEC = 3600  # token 临期 1h 内触发续期

# API Key 鉴权（空 = 不鉴权，环境变量 TRAE_PROXY_API_KEY 设置）
API_KEY = os.environ.get("TRAE_PROXY_API_KEY", "")

# 请求体大小上限（防 OOM，对齐 traework2api handler.go maxBodyBytes=8MB）
MAX_BODY_BYTES = 8 << 20

# ── 凭证信封常量（trae-mate trae_auth.rs 逐字节移植）────────────────────────
ENVELOPE_HEADER = bytes([116, 99, 5, 16, 0, 0])
LEFT_SECRET = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227, 57, 130,
    155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61,
    238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109,
    139, 209, 37,
])
RIGHT_SECRET = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127, 169, 25,
    181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176, 200,
    235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33,
    12, 125,
])

# ── 模型清单（2026-10-28 solo_work_remote 实拉快照；仅作离线兜底种子，───────────
# 运行时以自动同步的 trae_models.json / 上游拉取结果为准，上游下线即随之移除；
# 对外开放范围由 MODEL_WHITELIST 白名单控制，默认只开放六个）
MODELS = {
    "glm-5.3":             {"model_name": "glm-5.3__dev",             "display": "GLM-5.3",           "ctx": 200000, "multimodal": False, "capability": "reasoning_model"},
    "glm-5.3-flashx":      {"model_name": "glm-5.3-flashx__dev",      "display": "GLM-5.3-FlashX",    "ctx": 200000, "multimodal": False, "capability": "reasoning_model"},
    "mimo-v2.6-pro":       {"model_name": "mimo-v2.6-pro__dev",       "display": "MiMo-V2.6-Pro",     "ctx": 200000, "multimodal": True,  "capability": "reasoning_model"},
    "mimo-v2.6-flash":     {"model_name": "mimo-v2.6-flash__dev",     "display": "MiMo-V2.6-Flash",   "ctx": 200000, "multimodal": True,  "capability": "reasoning_model"},
    "deepseek-v4.1-flash": {"model_name": "deepseek-v4.1-flash__dev", "display": "DeepSeek-V4.1-Flash", "ctx": 200000, "multimodal": True, "capability": "reasoning_model"},
    "minimax-m3.1-flash":  {"model_name": "minimax-m3.1-flash__dev",  "display": "MiniMax-M3.1-Flash", "ctx": 200000, "multimodal": False, "capability": "reasoning_model"},
}
# 手写短别名 → 官方 config_name（自动同步后重建别名表时保留这部分）
_EXTRA_MODEL_ALIAS = {}
# 小写别名 → 官方 config_name
MODEL_ALIAS = {m.lower(): m for m in MODELS}
MODEL_ALIAS.update(_EXTRA_MODEL_ALIAS)
DEFAULT_MODEL = "glm-5.3"  # solo_work_remote 通道实测可用（与 Trae CN 客户端当前主力一致）

# ── 模型白名单：目前只开放这六个模型（2026-10-28）─────────────────────────────
# 其余模型不进 /v1/models，聊天直接 404（暂不开放、非永久屏蔽，改白名单即时生效）。
# 调整方式：config.json "model_whitelist": ["glm-5.3", ...]（设为 [] = 全部开放）
#   或环境变量 TRAE_PROXY_MODEL_WHITELIST="glm-5.3,mimo-v2.6-pro"（逗号分隔，空 = 全部开放）
DEFAULT_MODEL_WHITELIST = [
    "glm-5.3", "glm-5.3-flashx", "mimo-v2.6-pro",
    "mimo-v2.6-flash", "deepseek-v4.1-flash", "minimax-m3.1-flash",
]
MODEL_WHITELIST = [s.strip().lower() for s in DEFAULT_MODEL_WHITELIST]

# ── 配置文件（config.json 覆盖常量；环境变量优先）────────────────────────────
def _apply_config():
    """读 config.json 覆盖常量。优先级：环境变量 > config.json > 代码默认值。"""
    global PORT, API_KEY, MAX_BODY_BYTES, SOLO_MODELS_TTL, SOLO_MODELS_FAIL_COOLDOWN, REFRESH_AHEAD_SEC, DEFAULT_MODEL, TRAE_MODELS_FILE, MODEL_WHITELIST
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.loads(f.read())
    except Exception:
        return
    if "port" in cfg and not os.environ.get("TRAE_PROXY_PORT"):
        PORT = int(cfg["port"])
    if "api_key" in cfg and not os.environ.get("TRAE_PROXY_API_KEY"):
        API_KEY = str(cfg["api_key"])
    if "max_body_mb" in cfg and not os.environ.get("TRAE_PROXY_MAX_BODY_MB"):
        MAX_BODY_BYTES = int(cfg["max_body_mb"]) << 20
    if "models_ttl" in cfg and not os.environ.get("TRAE_PROXY_MODELS_TTL"):
        SOLO_MODELS_TTL = int(cfg["models_ttl"])
    if "models_fail_cooldown" in cfg and not os.environ.get("TRAE_PROXY_MODELS_FAIL_COOLDOWN"):
        SOLO_MODELS_FAIL_COOLDOWN = int(cfg["models_fail_cooldown"])
    if "models_file" in cfg and not os.environ.get("TRAE_PROXY_MODELS_FILE"):
        TRAE_MODELS_FILE = os.path.abspath(str(cfg["models_file"]))
    if "refresh_ahead_sec" in cfg and not os.environ.get("TRAE_PROXY_REFRESH_AHEAD_SEC"):
        REFRESH_AHEAD_SEC = int(cfg["refresh_ahead_sec"])
    if "default_model" in cfg and not os.environ.get("TRAE_PROXY_DEFAULT_MODEL"):
        DEFAULT_MODEL = str(cfg["default_model"])
    wl_env = os.environ.get("TRAE_PROXY_MODEL_WHITELIST")
    if wl_env is not None:
        # 环境变量：逗号分隔；空字符串 = 不限制（全部开放）
        MODEL_WHITELIST = [s.strip().lower() for s in wl_env.split(",") if s.strip()]
    elif isinstance(cfg.get("model_whitelist"), list):
        # config.json：数组；空数组 = 不限制（全部开放）
        MODEL_WHITELIST = [str(s).strip().lower() for s in cfg["model_whitelist"] if str(s).strip()]

_apply_config()

# 日志（同目录；TRAE_PROXY_LOG 空字符串关闭）
LOG_FILE = os.environ.get("TRAE_PROXY_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trae_proxy.log"))
LOG_MAX_BYTES = int(os.environ.get("TRAE_PROXY_LOG_MAX_MB", "5")) << 20
_log_lock = threading.Lock()

# 直连（不走系统代理；mchost.guru CN 网关可直连，同 trae-mate 结论）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(msg):
    if not LOG_FILE:
        return
    try:
        with _log_lock:
            try:
                if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    old = LOG_FILE + ".old"
                    if os.path.exists(old):
                        os.remove(old)
                    os.rename(LOG_FILE, old)
            except Exception:
                pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), msg))
    except Exception:
        pass


def _rand():
    return secrets.token_hex(12)


# ── 凭证：storage.json 信封解密（trae-mate trae_auth.rs 移植）────────────────
_session_lock = threading.Lock()
_session_cache = {"at": 0.0, "data": None}


def _decrypt_auth_info(encoded: str) -> dict:
    """解密 iCubeAuthInfo base64 信封，返回 payload JSON。"""
    envelope = base64.b64decode(encoded)
    if len(envelope) <= 38 or envelope[:6] != ENVELOPE_HEADER:
        raise RuntimeError("Invalid TRAE credential envelope")
    random_key = envelope[6:38]
    secret = bytes(l ^ r for l, r in zip(LEFT_SECRET, RIGHT_SECRET))
    derived = hashlib.sha512(hashlib.sha512(random_key).digest() + secret).digest()
    key, iv = derived[:16], derived[16:32]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(envelope[38:]) + dec.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16:
        raise RuntimeError("AES decrypt failed (bad padding)")
    plain = padded[:-pad]
    if len(plain) < 64:
        raise RuntimeError("decrypted payload too short")
    digest, payload = plain[:64], plain[64:]
    if hashlib.sha512(payload).digest() != digest:
        raise RuntimeError("TRAE credential integrity check failed")
    return json.loads(payload.decode("utf-8"))


def _decode_jwt_exp(token: str):
    """解析 JWT exp（不校验签名），返回 epoch 秒或 None。"""
    try:
        p64 = token.split(".")[1].replace("-", "+").replace("_", "/")
        p64 += "=" * (-len(p64) % 4)
        payload = json.loads(base64.b64decode(p64).decode("utf-8"))
        return int(payload.get("exp") or 0) or None
    except Exception:
        return None


def _encrypt_auth_info(payload: dict) -> str:
    """_decrypt_auth_info 的逆运算：payload → base64 信封（续期后写回 storage.json 用）。"""
    plain = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha512(plain).digest()
    full = digest + plain
    pad = 16 - len(full) % 16
    full += bytes([pad]) * pad
    random_key = secrets.token_bytes(32)
    secret = bytes(l ^ r for l, r in zip(LEFT_SECRET, RIGHT_SECRET))
    derived = hashlib.sha512(hashlib.sha512(random_key).digest() + secret).digest()
    key, iv = derived[:16], derived[16:32]
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = enc.update(full) + enc.finalize()
    return base64.b64encode(ENVELOPE_HEADER + random_key + ciphertext).decode("ascii")


_refresh_lock = threading.Lock()
_refreshing = False   # 是否有线程正在续期（防并发重复刷新 + 并发写同一 tmp 文件）


def _refresh_token(sess: dict) -> dict:
    """用 refreshToken 调 ExchangeToken 续期，更新 sess 并写回 storage.json。

    移植自 traework2api client.go refreshLocked。失败时不改 sess（旧 token 可重试）。
    并发去重：临期时多个请求同时进入，只让一个真正调 ExchangeToken，其余直接返回旧 sess。
    """
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return sess   # 别的线程正在刷，先用旧的
        _refreshing = True
    try:
        rt = sess.get("refresh_token") or ""
        if not rt:
            raise RuntimeError("无 refreshToken，请打开 TRAE SOLO CN 客户端重新登录")
        body = json.dumps({"ClientID": SOLO_CLIENT_ID, "RefreshToken": rt,
                           "ClientSecret": "-", "UserID": ""}).encode("utf-8")
        req = urllib.request.Request(EP_EXCHANGE, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "Trae/" + SOLO_IDE_VERSION)
        resp = _opener.open(req, timeout=30)
        obj = json.loads(resp.read().decode("utf-8", "replace"))
        result = obj.get("Result") or {}
        new_token = result.get("Token") or ""
        if not new_token:
            raise RuntimeError("ExchangeToken 未返回 Token，refreshToken 可能已失效，请重新登录")
        new_rt = result.get("RefreshToken") or rt
        expires_at = result.get("TokenExpireAt") or 0
        if expires_at > 1e12:
            expires_at = expires_at // 1000  # 毫秒 → 秒
        # 写回 storage.json（重新加密信封）；用唯一 tmp 名避免并发写同一文件
        try:
            with open(STORAGE_JSON, "r", encoding="utf-8") as f:
                storage = json.load(f)
            encoded = storage.get("iCubeAuthInfo://icube.cloudide")
            if encoded:
                info = _decrypt_auth_info(encoded)
                info["token"] = new_token
                info["refreshToken"] = new_rt
                storage["iCubeAuthInfo://icube.cloudide"] = _encrypt_auth_info(info)
                tmp = "%s.tmp.%d" % (STORAGE_JSON, os.getpid())
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(storage, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STORAGE_JSON)
                _log("token 续期已写回 storage.json，新 exp=%s" % expires_at)
        except Exception as e:
            _log("token 续期写回 storage.json 失败（内存已更新）: %r" % e)
        new_sess = dict(sess)
        new_sess["token"] = new_token
        new_sess["refresh_token"] = new_rt
        new_sess["exp"] = expires_at or _decode_jwt_exp(new_token)
        with _session_lock:
            _session_cache["at"] = time.time()
            _session_cache["data"] = new_sess
        return new_sess
    finally:
        with _refresh_lock:
            _refreshing = False


def _read_session() -> dict:
    """读 TRAE 登录态：{token, user_id, device_id, machine_id, exp, refresh_token}。缓存 60s。

    token 临期（REFRESH_AHEAD_SEC 内）时自动调 _refresh_token 续期。
    """
    now = time.time()
    with _session_lock:
        if _session_cache["data"] and now - _session_cache["at"] < 60:
            return _session_cache["data"]
    if not os.path.exists(STORAGE_JSON):
        raise RuntimeError("找不到 %s，请先安装并登录 TRAE SOLO CN" % STORAGE_JSON)
    with open(STORAGE_JSON, "r", encoding="utf-8") as f:
        storage = json.load(f)
    encoded = storage.get("iCubeAuthInfo://icube.cloudide")
    if not encoded:
        raise RuntimeError("storage.json 无 iCubeAuthInfo（未登录？）")
    info = _decrypt_auth_info(encoded)
    token = info.get("token") or ""
    if not token:
        raise RuntimeError("TRAE 登录 token 为空")
    # device_id 取 iCubeAuthInfo://icube-dc:<id> 键后缀（alaudalog 实测 user_unique_id）
    device_id = ""
    for k in storage:
        if k.startswith("iCubeAuthInfo://icube-dc:"):
            device_id = k[len("iCubeAuthInfo://icube-dc:"):]
            break
    sess = {
        "token": token,
        "user_id": info.get("userId") or "",
        "device_id": device_id,
        "machine_id": storage.get("telemetry.machineId") or "",
        "host": info.get("host") or GATEWAY,
        "exp": _decode_jwt_exp(token),
        "refresh_token": info.get("refreshToken") or "",
    }
    with _session_lock:
        _session_cache["at"] = now
        _session_cache["data"] = sess
    # token 临期自动续期（traework2api RefreshTokenIfNeeded 移植）
    exp = sess.get("exp")
    if exp and exp - time.time() < REFRESH_AHEAD_SEC:
        _log("token 临期（exp=%s，%ds 后过期），触发自动续期" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)), int(exp - time.time())))
        try:
            sess = _refresh_token(sess)
        except Exception as e:
            _log("自动续期失败（继续用旧 token）: %r" % e)
    return sess


class UpstreamError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _map_upstream_error(code, msg: str) -> int:
    """上游内部错误码/消息 → 客户端友好的 HTTP 状态码。"""
    s = (str(msg) or "").lower()
    c = str(code) if code is not None else ""
    # 认证/会话失效 → 401
    if c in ("1005", "1001", "1002", "401") or any(k in s for k in ("auth", "session", "token", "login", "unauthorized")):
        return 401
    # 限流 → 429
    if c == "429" or any(k in s for k in ("rate", "limit", "quota", "too many")):
        return 429
    # 内容安全 → 400
    if c in ("1006", "1003", "400") or any(k in s for k in ("content_security", "security", "sensitive", "违规")):
        return 400
    # 上游明确返回的 5xx
    if c in ("500", "502", "503"):
        return int(c)
    return 502


def _iter_sse(resp):
    """解析上游 SSE：产出 (event_name, data_text)。"""
    buf = b""
    event = ""
    data_parts = []
    while True:
        try:
            chunk = resp.read(4096)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            s = line.decode("utf-8", "replace").rstrip("\r")
            if s == "":
                if data_parts:
                    yield event, "\n".join(data_parts)
                event, data_parts = "", []
            elif s.startswith(":"):
                continue
            elif s.startswith("event:"):
                event = s[6:].strip()
            elif s.startswith("data:"):
                data_parts.append(s[5:].strip())
    if data_parts:
        yield event, "\n".join(data_parts)


# ── SOLO llm_utils_chat 通道（主力，2026-09-04 实测打通）─────────────────────
def solo_headers(sess: dict, stream: bool = True) -> dict:
    """SOLO 通道请求头（对齐 traework2api SOLOHeaders，实测必需）。"""
    at = sess["token"]
    h = {
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
        "user-agent": "Trae/" + SOLO_IDE_VERSION,
        "authorization": "Cloud-IDE-JWT " + at,
        "x-cloudide-token": at,
        "x-ide-token": at,
        "x-uid": sess["user_id"],
        "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "x-app-version": "default",
        "x-ide-version": SOLO_IDE_VERSION,
        "x-ide-version-code": SOLO_IDE_VERSION_CODE,
        "x-app-version-code": SOLO_IDE_VERSION_CODE,
        "x-ide-version-type": "stable",
        "x-device-type": "windows",
        "x-os-version": "Windows 11 Pro",
        "x-device-brand": SOLO_DEVICE_BRAND,
        "request-traffic-type": "prod",
    }
    if sess.get("machine_id"):
        h["x-machine-id"] = sess["machine_id"]
    if sess.get("device_id"):
        h["x-device-id"] = sess["device_id"]
    return h


def _solo_messages(msgs) -> list:
    """OpenAI messages → SOLO 格式：content 字符串 → [{type:text,text:...}]，
    assistant tool_calls 的 function → function_call（上游字段名）。"""
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        role = m.get("role")
        content = m.get("content")
        if content is None:
            m.pop("content", None)
        elif isinstance(content, str):
            m["content"] = [{"type": "text", "text": content}]
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            kept = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tc = dict(tc)
                fn = tc.get("function")
                if isinstance(fn, dict):
                    if not str(fn.get("name") or "").strip():
                        continue  # 上游要求 function_call.name 必填
                    tc["function_call"] = fn
                    tc.pop("function", None)
                kept.append(tc)
            if kept:
                m["tool_calls"] = kept
            else:
                m.pop("tool_calls", None)
        out.append(m)
    return out


def _solo_body(oa: dict, cfg: dict) -> dict:
    """OpenAI 请求对象 → llm_utils_chat 请求体。"""
    body = {
        "messages": _solo_messages(oa.get("messages")),
        "function": SOLO_FUNCTION,
        "stream": True,  # 上游强制流式，非流式由本地聚合
        "config_name": cfg["name"],
        "model": cfg["name"],
    }
    # tools：上游要求 function.parameters 是 JSON 字符串（非对象）
    tools = oa.get("tools")
    if isinstance(tools, list) and tools:
        out_tools = []
        for t in tools:
            if not isinstance(t, dict) or not isinstance(t.get("function"), dict):
                continue
            t = dict(t)
            fn = dict(t["function"])
            params = fn.get("parameters")
            if isinstance(params, dict):
                fn["parameters"] = json.dumps(params, ensure_ascii=False)
            t["function"] = fn
            out_tools.append(t)
        if out_tools:
            body["tools"] = out_tools
    # tool_choice 归一化
    tc = oa.get("tool_choice")
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            body.pop("tools", None)
        else:
            body["tool_choice"] = tc
    elif isinstance(tc, dict):
        typ = str(tc.get("type") or "").lower()
        if typ == "none":
            body.pop("tools", None)
        elif typ in ("auto", "required"):
            body["tool_choice"] = typ
        elif typ == "function":
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = str(fn.get("name") or "").strip()
            body["tool_choice"] = name or "auto"
    return body


def solo_turn_chunks(sess: dict, cfg: dict, oa: dict):
    """llm_utils_chat 回合：产出内部 OpenAI chat.completion.chunk 字典流。"""
    body = _solo_body(oa, cfg)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(EP_LLM_UTILS, data=payload,
                                 headers=solo_headers(sess, stream=True), method="POST")
    try:
        resp = _opener.open(req, timeout=600)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        _log("llm_utils_chat %s: %s" % (e.code, detail[:200]))
        raise UpstreamError("llm_utils_chat %s: %s" % (e.code, detail), status=e.code)
    except Exception as e:
        raise UpstreamError("llm_utils_chat 连接失败: %s" % e)

    chat_id = "chatcmpl-" + _rand()
    created = int(time.time())
    finish_reason = None
    usage = None

    def chunk(delta, fr=None):
        return {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                "model": cfg.get("public") or cfg["name"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}

    yield chunk({"role": "assistant", "content": ""})
    tc_index = 0

    for event, data_text in _iter_sse(resp):
        if data_text == "[DONE]":
            break
        try:
            data = json.loads(data_text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        if event == "output":
            reasoning = data.get("reasoning_content")
            response = data.get("response")
            if isinstance(reasoning, str) and reasoning:
                yield chunk({"reasoning_content": reasoning})
            if isinstance(response, str) and response:
                yield chunk({"content": response})
            raw_tc = data.get("tool_calls")
            if raw_tc:
                calls = raw_tc if isinstance(raw_tc, list) else [raw_tc]
                deltas = []
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    # SOLO 上游用 function_call 字段名（OpenAI 是 function）
                    fc = call.get("function_call") or call.get("function")
                    if not isinstance(fc, dict):
                        continue
                    name = str(fc.get("name") or "").strip()
                    # 对齐 traework2api：忽略 SOLO 专属 partial_arguments/namespace，只认 arguments
                    args = fc.get("arguments") if isinstance(fc.get("arguments"), str) else ""
                    has_id = isinstance(call.get("id"), str) and call["id"]
                    if not name and not args and not has_id:
                        continue
                    # 上游 index 优先（增量分片按 index 合并），无 index 才本地递增兜底
                    idx = call.get("index")
                    idx = int(idx) if isinstance(idx, (int, float)) else tc_index
                    tc_index = max(tc_index, idx + 1)
                    delta = {"index": idx, "type": "function", "function": {}}
                    if has_id:
                        delta["id"] = call["id"]
                    if name:
                        delta["function"]["name"] = name
                    if args:
                        delta["function"]["arguments"] = args
                    deltas.append(delta)
                if deltas:
                    yield chunk({"tool_calls": deltas})
                    finish_reason = "tool_calls"
            continue

        if event == "token_usage":
            usage = {
                "prompt_tokens": data.get("prompt_tokens") or 0,
                "completion_tokens": data.get("completion_tokens") or 0,
                "total_tokens": data.get("total_tokens")
                                or (data.get("prompt_tokens") or 0) + (data.get("completion_tokens") or 0),
            }
            for extra in ("reasoning_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if data.get(extra) is not None:
                    usage[extra] = data[extra]
            continue

        if event == "done":
            fr = data.get("finish_reason")
            if isinstance(fr, str) and fr and not (finish_reason == "tool_calls" and fr == "stop"):
                # 已判定 tool_calls 时不被上游的兜底 stop 覆盖（OpenAI 语义）
                finish_reason = fr
            if finish_reason is None:
                finish_reason = "stop"
            break

        if event == "error":
            code = data.get("code")
            msg = data.get("message") or data_text[:200]
            extra = str(data.get("extra") or "")
            # 1005 + plan：模型需要付费计划（权益不足），提示换免费模型而非误导性的认证失败
            if str(code) == "1005" and "plan" in extra:
                raise UpstreamError("模型 %s 需要付费计划（error 1005, extra=%s）；请换用免费模型，如 glm-5.2" % (
                    cfg["name"], extra), status=403)
            raise UpstreamError("上游事件错误 error(code=%s): %s" % (code, msg), status=_map_upstream_error(code, msg))

    if finish_reason is None:
        finish_reason = "stop"
    final = chunk({}, finish_reason)
    if usage:
        final["usage"] = usage
    yield final


# ── 模型表自动同步（get_detail_param → 内存表 + trae_models.json）────────────
def _parse_solo_models(obj: dict) -> dict:
    """get_detail_param 响应 → {config_name: {model_name, display, ctx, multimodal,
    capability, invisible}}。config_name 去重（首次出现优先：主通道条目在前，
    带 __dev/ctx 元数据）；config_switch=false 的直接丢弃。"""
    out = {}
    for ci in obj.get("config_info_list") or []:
        if not isinstance(ci, dict):
            continue
        name = ci.get("config_name")
        if not isinstance(name, str) or not name or name in out:
            continue
        if not ci.get("config_switch", True):
            continue
        dc = ci.get("display_config") or {}
        mdl = ci.get("model_detail_list") or []
        model_name = name
        if isinstance(mdl, list) and mdl and isinstance(mdl[0], dict):
            model_name = str(mdl[0].get("model_name") or "") or name
        try:
            ctx = int((ci.get("context_window_tokens") or {}).get("dev") or 0)
        except Exception:
            ctx = 0
        out[name] = {
            "model_name": model_name,
            "display": str(dc.get("display_name") or ""),
            "ctx": ctx,
            "multimodal": bool(dc.get("multimodal")),
            "capability": str(dc.get("model_capability") or ""),
            "invisible": bool(ci.get("is_invisible_to_user")),
        }
    return out


def _is_model_open(pub_name: str) -> bool:
    """白名单开关：未配置（空列表）= 全部开放；配置后只开放白名单内的公共名
    （比较不区分大小写；白名单外模型暂不开放，改配置即时生效，无需重启）。"""
    if not MODEL_WHITELIST:
        return True
    return str(pub_name).lower() in MODEL_WHITELIST


def _is_model_exposed(name: str, table: dict, u2p: dict = None) -> bool:
    """是否进 /v1/models 列表：可见 + 有实质能力 + 白名单放行。
    隐形 subagent、custom_model 占位、summary 内部模型仍在内部表，改白名单即可放开。"""
    m = table.get(name) or {}
    if m.get("invisible"):
        return False
    if not (m.get("display") or m.get("capability")):
        return False
    pub = (u2p or {}).get(name, name)
    return _is_model_open(pub)


def _build_public_maps(table: dict):
    """生成 (公共名→上游名, 上游名→公共名)。公共名 = 上游 config_name 自动去掉
    -solo 后缀（通用规则，未写死任何模型名），如 glm-5.3-solo → glm-5.3。
    冲突策略：上游同时有 glm-5.3 与 glm-5.3-solo 时，glm-5.3 归原名持有者，
    -solo 版保留全名展示（仍可点名直连）。"""
    p2u, u2p = {}, {}
    for name in table:
        if not name.endswith("-solo"):
            p2u[name.lower()] = name
            u2p[name] = name
    for name in table:
        if name.endswith("-solo"):
            short = name[:-5]
            if short and short.lower() not in p2u:
                p2u[short.lower()] = name
                u2p[name] = short
            else:
                u2p[name] = name
    return p2u, u2p


def _merge_models(fetched: dict):
    """上游模型表整体覆盖内存表（上游下线的模型随之移除），重建别名表与公共名映射。"""
    global MODEL_ALIAS, _public_to_upstream, _upstream_to_public
    with _models_lock:
        MODELS.clear()
        MODELS.update(fetched)
        MODEL_ALIAS = {m.lower(): m for m in MODELS}
        MODEL_ALIAS.update(_EXTRA_MODEL_ALIAS)
        _public_to_upstream, _upstream_to_public = _build_public_maps(MODELS)


def _resolve_model(model: str) -> str:
    """请求模型名 → 上游 config_name（大小写不敏感）：
    1) 别名表（含手写短别名）→ 2) 公共名映射（-solo 自动去后缀）→ 3) 原名直通。
    例：上游只有 glm-5.3-solo 时，请求 glm-5.3 / GLM-5.3 均自动映射到它。"""
    low = str(model).lower()
    cand = MODEL_ALIAS.get(low)
    if cand and cand in MODELS:
        return cand
    up = _public_to_upstream.get(low)
    if up:
        return up
    return cand or model


def _save_models_cache(fetched: dict):
    """原子写 trae_models.json（tmp + os.replace，写坏不伤旧文件、不半写）。"""
    obj = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
           "models": fetched}
    tmp = "%s.tmp.%d" % (TRAE_MODELS_FILE, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TRAE_MODELS_FILE)


def _load_models_cache():
    """启动时读 trae_models.json 覆盖静态种子表（结构异常则忽略、保留种子，幂等）。"""
    if not os.path.exists(TRAE_MODELS_FILE):
        return
    try:
        with open(TRAE_MODELS_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        fetched = obj.get("models") or {}
        if not isinstance(fetched, dict) or not fetched:
            return
        clean = {}
        for k, v in fetched.items():
            if isinstance(k, str) and k and isinstance(v, dict):
                try:
                    clean[k] = {"model_name": str(v.get("model_name") or k),
                                "display": str(v.get("display") or ""),
                                "ctx": int(v.get("ctx") or 0),
                                "multimodal": bool(v.get("multimodal")),
                                "capability": str(v.get("capability") or ""),
                                "invisible": bool(v.get("invisible"))}
                except Exception:
                    continue
        if clean:
            _merge_models(clean)
            _log("模型表缓存已加载: %d 个（拉取于 %s）" % (len(clean), obj.get("fetched_at") or "?"))
    except Exception as e:
        _log("模型表缓存读取失败（用静态种子）: %r" % e)


def fetch_solo_models(sess: dict) -> dict:
    """get_detail_param 拉取 solo_work_lite 模型表（完整元数据，未解析过滤）。"""
    body = {"function": SOLO_FUNCTION, "config_names": None, "need_prompt": False,
            "current_config_info": None, "poly_prompt": True, "mode_type": None,
            "agent_type": None}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(EP_GET_DETAIL_PARAM, data=payload,
                                 headers=solo_headers(sess, stream=False), method="POST")
    resp = _opener.open(req, timeout=30)
    obj = json.loads(resp.read().decode("utf-8", "replace"))
    return _parse_solo_models(obj)


def _sync_models_once(trigger: str) -> int:
    """拉上游模型表 → 覆盖内存表 → 原子写缓存。返回模型数；失败抛异常且不动表。"""
    fetched = fetch_solo_models(_read_session())
    if not fetched:
        raise RuntimeError("上游 config_info_list 为空")
    _merge_models(fetched)
    now = int(time.time())
    try:
        _save_models_cache(fetched)
    except Exception as e:
        _log("模型表缓存写入失败（内存已更新）: %r" % e)
    with _models_lock:
        _models_state["ts"] = now
        _models_state["fail_ts"] = 0
    with _models_lock:
        snap = dict(MODELS)
        u2p = dict(_upstream_to_public)
    exposed = sum(1 for n in snap if _is_model_exposed(n, snap, u2p))
    _log("模型表已同步(%s): 共 %d 个，开放 %d 个" % (trigger, len(fetched), exposed))
    return len(fetched)


def _models_refresh_loop():
    """后台守护线程：启动立即同步一次，此后每 SOLO_MODELS_TTL 秒同步；
    失败按 SOLO_MODELS_FAIL_COOLDOWN 冷却重试（凭证缺失等场景不轰炸上游）。"""
    while True:
        interval = SOLO_MODELS_TTL
        try:
            _sync_models_once("后台定期")
        except Exception as e:
            with _models_lock:
                _models_state["fail_ts"] = int(time.time())
            _log("模型表同步失败: %r" % e)
            interval = SOLO_MODELS_FAIL_COOLDOWN
        time.sleep(max(30, interval))


# ── chunk 流聚合（非流式响应）───────────────────────────────────────────────
def collect_chunks(chunks, model_name):
    text_parts = []
    reasoning_parts = []
    tool_calls = {}
    usage = None
    finish = None
    for c in chunks:
        usage = c.get("usage") or usage
        choice = (c.get("choices") or [{}])[0]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        d = choice.get("delta") or {}
        if d.get("content"):
            text_parts.append(d["content"])
        if d.get("reasoning_content"):
            reasoning_parts.append(d["reasoning_content"])
        for tc in d.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [{
            "id": slot["id"] or ("call_" + _rand()),
            "type": "function",
            "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
        } for _, slot in sorted(tool_calls.items())]
    return {
        "id": "chatcmpl-" + _rand(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ── 协议转换：Responses / Anthropic → OpenAI chat ───────────────────────────
def convert_responses_to_openai(body: dict) -> dict:
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("max_output_tokens") is not None:
        oa["max_tokens"] = body["max_output_tokens"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oa["top_p"] = body["top_p"]

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
                        elif isinstance(part, dict) and part.get("type") in ("input_image", "image", "image_url"):
                            text += "[图片输入暂不支持]"
                if text:
                    msgs.append({"role": role, "content": text})
            elif t == "function_call":
                tc = {"id": item.get("call_id", ""), "type": "function",
                      "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")}}
                if msgs and msgs[-1].get("role") == "assistant":
                    msgs[-1].setdefault("tool_calls", []).append(tc)
                else:
                    msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
            elif t == "function_call_output":
                o = item.get("output")
                out_text = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": out_text})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    return oa


def convert_anthropic_to_openai(body: dict) -> dict:
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]

    msgs = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        sys_text = "\n".join(p.get("text", "") for p in sys_
                             if isinstance(p, dict) and p.get("type") == "text")
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
                elif part.get("type") == "tool_use":
                    tool_calls.append({
                        "id": part.get("id", ""), "type": "function",
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
                    res = rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False)
                    msgs.append({"role": "tool", "tool_call_id": part.get("tool_use_id", ""), "content": res})
            texts = [p.get("text", "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            if any(t.strip() for t in texts):
                msgs.append({"role": "user", "content": "\n".join(texts)})
            if any(isinstance(p, dict) and p.get("type") == "image" for p in c):
                msgs.append({"role": "user", "content": "[图片输入暂不支持]"})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    return oa


def parse_anthropic_text(body: dict) -> list:
    texts = []
    sys_ = body.get("system")
    if isinstance(sys_, str):
        texts.append(sys_)
    elif isinstance(sys_, list):
        for p in sys_:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(p.get("text", ""))
    for msg in body.get("messages") or []:
        c = msg.get("content")
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


def convert_openai_to_responses(openai_obj: dict, req_model: str) -> dict:
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
                       "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for tc in message.get("tool_calls") or []:
        output.append({"type": "function_call", "id": "fc_" + _rand(), "call_id": tc.get("id", ""),
                       "name": (tc.get("function") or {}).get("name", ""),
                       "arguments": (tc.get("function") or {}).get("arguments", "{}"),
                       "status": "completed"})
    incomplete = choice.get("finish_reason") == "length"
    _ot = "".join(
        seg.get("text", "")
        for item in output if item.get("type") == "message"
        for seg in (item.get("content") or []) if seg.get("type") == "output_text"
    )
    return {
        "id": "resp_" + _rand(), "object": "response", "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": req_model, "output": output, "output_text": _ot,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
    }


def convert_openai_to_anthropic(openai_obj: dict, req_model: str) -> dict:
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
    stop_reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(finish, finish or "end_turn")
    return {
        "id": "msg_" + _rand(), "type": "message", "role": "assistant",
        "model": req_model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
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
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))

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
    """内部 chat chunk → Anthropic Messages SSE 事件序列。"""

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
        self.tool_block = None  # {index, id, name, args}
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

    def _close_thinking(self, out):
        if self.thinking_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.thinking_block = False

    def _close_text(self, out):
        if self.text_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.text_block = False

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
                self._close_text(out)
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
        if content:
            if not self.text_block:
                self._close_thinking(out)
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
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if self.tool_block is None:
                self._close_thinking(out)
                self._close_text(out)
                self.block_index += 1
                self.tool_block = {"index": self.block_index - 1,
                                   "id": tc.get("id", ""), "name": fn.get("name", ""), "args": ""}
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.tool_block["index"],
                    "content_block": {"type": "tool_use", "id": self.tool_block["id"],
                                      "name": self.tool_block["name"], "input": {}},
                }, ensure_ascii=False)))
            if fn.get("arguments"):
                self.tool_block["args"] += fn["arguments"]
                out.append(("content_block_delta", json.dumps({
                    "type": "content_block_delta", "index": self.tool_block["index"],
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                }, ensure_ascii=False)))
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        self._start(out)
        self._close_thinking(out)
        self._close_text(out)
        if self.tool_block:
            try:
                parsed = json.loads(self.tool_block["args"] or "{}")
            except Exception:
                parsed = {}
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.tool_block["index"]}, ensure_ascii=False)))
        usage = self.usage or {}
        stop_reason = "tool_use" if self.tool_block else "end_turn"
        out.append(("message_delta", json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)},
        }, ensure_ascii=False)))
        out.append(("message_stop", json.dumps({"type": "message_stop"}, ensure_ascii=False)))
        return out


# ── HTTP Handler ────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        _log("%s - %s" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _check_api_key(self):
        """API Key 校验（TRAE_PROXY_API_KEY 设置时启用）。已响应错误返回 True。"""
        if not API_KEY:
            return False
        authz = self.headers.get("Authorization") or ""
        if not authz.startswith("Bearer ") or not secrets.compare_digest(authz[7:], API_KEY):
            self._json(401, {"error": {"message": "missing or invalid API key", "type": "invalid_api_key"}})
            return True
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if self._check_api_key():
            return
        try:
            if path == "/health":
                try:
                    s = _read_session()
                    exp = s.get("exp")
                    exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "unknown"
                    now = int(time.time())
                    with _models_lock:
                        snap = dict(MODELS)
                        u2p = dict(_upstream_to_public)
                        ts = _models_state["ts"]
                    exposed = sorted(u2p.get(n, n) for n in snap if _is_model_exposed(n, snap, u2p))
                    cache_age = now - ts if ts else None
                    return self._json(200, {"ok": True, "user": s["user_id"],
                                            "tokenExp": exp_str,
                                            "port": PORT, "upstream": EP_LLM_UTILS,
                                            "models": exposed,
                                            "cache": {"count": len(snap), "exposed": len(exposed),
                                                      "age": cache_age, "ttl": SOLO_MODELS_TTL}})
                except Exception as e:
                    return self._json(200, {"ok": False, "error": str(e)})
            if path in ("/v1/models", "/models"):
                now = int(time.time())
                # 缓存过期则现场同步一次（失败记负缓存冷却，避免每个请求都轰炸上游）
                if now - _models_state["ts"] >= SOLO_MODELS_TTL \
                        and now - _models_state["fail_ts"] >= SOLO_MODELS_FAIL_COOLDOWN:
                    try:
                        _sync_models_once("请求触发")
                    except Exception as e:
                        with _models_lock:
                            _models_state["fail_ts"] = now
                        _log("模型表同步失败(请求触发): %r" % e)
                with _models_lock:
                    snap = dict(MODELS)
                    u2p = dict(_upstream_to_public)
                data = []
                for n in sorted(snap):
                    if not _is_model_exposed(n, snap, u2p):
                        continue
                    m = snap[n]
                    pub = u2p.get(n, n)   # 公共名 = 自动去 -solo 后缀
                    data.append({
                        "id": pub, "object": "model", "created": now,
                        "owned_by": "trae-solo-cn",
                        "metadata": {"display_name": m.get("display") or pub,
                                     "model_name": m.get("model_name", n),
                                     "config_name": n,
                                     "context_window": m.get("ctx", 0),
                                     "capability": m.get("capability", ""),
                                     "multimodal": bool(m.get("multimodal"))},
                    })
                return self._json(200, {"object": "list", "data": data})
            return self._json(404, {"error": {"message": "支持: GET /v1/models, GET /health, POST /v1/chat/completions, /v1/responses, /v1/messages"}})
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if self._check_api_key():
            return
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        if path == "/v1/messages":
            return self._handle_chat("anthropic")
        if path in ("/v1/responses", "/responses"):
            return self._handle_chat("responses")
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._handle_chat("openai")
        return self._json(404, {"error": {"message": "支持: POST /v1/chat/completions, /v1/responses, /v1/messages, /v1/messages/count_tokens"}})

    def _handle_count_tokens(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._json(413, {"error": {"message": f"请求体过大（{length} > {MAX_BODY_BYTES} 字节）"}})
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})
        total = sum(len(t) for t in parse_anthropic_text(req))
        return self._json(200, {"input_tokens": max(1, total // 3)})

    def _handle_chat(self, protocol: str):
        _t0 = time.time()
        # 1) 读请求体
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._json(413, {"error": {"message": f"请求体过大（{length} > {MAX_BODY_BYTES} 字节）"}})
            req_body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})

        # 2) 协议请求转换（统一取 messages 数组）
        req_model_name = req_body.get("model") or ""
        if protocol == "responses":
            oa = convert_responses_to_openai(req_body)
        elif protocol == "anthropic":
            oa = convert_anthropic_to_openai(req_body)
        else:
            oa = dict(req_body)

        # 3) 模型解析（别名/公共名映射/大小写不敏感 → 上游 config_name）+ 白名单闸门
        model = oa.get("model") or DEFAULT_MODEL
        if not oa.get("model") and MODEL_WHITELIST and not _is_model_open(model):
            model = MODEL_WHITELIST[0]   # 未指定模型且默认模型不在白名单 → 取白名单首项
        real = _resolve_model(model)
        pub_name = _upstream_to_public.get(real, real)
        if not _is_model_open(pub_name):
            return self._json(404, {"error": {"message": "模型 %s 暂未开放，当前开放: %s（config.json 的 model_whitelist 可调整）" % (
                model, ", ".join(MODEL_WHITELIST))}})
        if real in MODELS:
            cfg = {"name": real, "public": pub_name, **MODELS[real]}
        else:
            cfg = {"name": real, "model_name": real, "display": real, "ctx": 0, "public": real}
        is_stream = bool(oa.get("stream"))

        # 4) 凭证
        try:
            sess = _read_session()
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

        # 5) 上游 SOLO 聊天回合（llm_utils_chat 通道）
        # P0 真流式：流式时保留生成器边收边写，非流式才落内存
        if is_stream:
            chunks = solo_turn_chunks(sess, cfg, oa)
        else:
            try:
                chunks = list(solo_turn_chunks(sess, cfg, oa))
            except UpstreamError as e:
                return self._json(e.status, {"error": {"message": str(e)}})
            except Exception as e:
                return self._json(502, {"error": {"message": "上游异常: %s" % e}})
            _log("回合完成 model=%s stream=False chunks=%d 耗时=%.2fs" % (real, len(chunks), time.time() - _t0))

        # 6a) 流式：协议状态机输出 SSE
        if is_stream:
            self.send_response(200)
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

            def _write_error(err):
                try:
                    if protocol == "anthropic":
                        _write_sse("error", json.dumps({"type": "error", "error": {
                            "type": "api_error", "message": str(err)}}, ensure_ascii=False))
                    elif protocol == "responses":
                        _write_sse("response.failed", json.dumps({"type": "response.failed",
                            "response": {"id": "resp_" + _rand(), "object": "response",
                            "created_at": int(time.time()), "status": "failed",
                            "model": req_model_name, "output": [],
                            "error": {"code": "upstream_error", "message": str(err)}}}, ensure_ascii=False))
                    else:
                        _write_sse("", json.dumps({"error": {"message": str(err),
                            "type": "api_error", "code": "upstream_error"}}, ensure_ascii=False))
                except Exception:
                    pass

            n = 0
            try:
                for c in chunks:
                    n += 1
                    if st is not None:
                        for ev, pl in st.feed(c):
                            _write_sse(ev, pl)
                    else:
                        _write_sse("", json.dumps(c, ensure_ascii=False))
                if st is not None:
                    for ev, pl in st.finish():
                        _write_sse(ev, pl)
                else:
                    _write_sse("", "[DONE]")
                self.wfile.write(b"0\r\n\r\n")
                _log("回合完成 model=%s stream=True chunks=%d 耗时=%.2fs" % (real, n, time.time() - _t0))
            except UpstreamError as e:
                _log("流内上游错误 model=%s: %s" % (real, e))
                _write_error(e)
                try:
                    if st is None:
                        _write_sse("", "[DONE]")
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass
            except (BrokenPipeError, ConnectionResetError):
                _log("客户端提前断开 stream model=%s" % real)
            except Exception as e:
                _log("流式异常 model=%s: %r" % (real, e))
                _write_error(e)
                try:
                    if st is None:
                        _write_sse("", "[DONE]")
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass
            return

        # 6b) 非流式：聚合 → 协议转换（model 字段回显公共名）
        merged = collect_chunks(chunks, cfg.get("public") or real)
        if protocol == "responses":
            return self._json(200, convert_openai_to_responses(merged, req_model_name))
        if protocol == "anthropic":
            return self._json(200, convert_openai_to_anthropic(merged, req_model_name))
        return self._json(200, merged)


def main():
    _load_models_cache()  # 先用上次同步结果（离线也有完整表），后台再刷新
    threading.Thread(target=_models_refresh_loop, name="models-sync", daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        sess = _read_session()
        exp = sess.get("exp")
        exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "unknown"
        print("TRAE 官方模型代理已启动: http://127.0.0.1:%d" % PORT)
        print("用户: %s  token 有效期: %s" % (sess["user_id"], exp_str))
    except Exception as e:
        print("TRAE 官方模型代理已启动: http://127.0.0.1:%d" % PORT)
        print("警告: 凭证暂不可用（%s）；启动 TRAE SOLO CN 登录后自动恢复" % e)
    with _models_lock:
        snap = dict(MODELS)
        u2p = dict(_upstream_to_public)
    print("模型: 内部 %d 个，开放 %d 个: %s" % (
        len(snap), sum(1 for n in snap if _is_model_exposed(n, snap, u2p)),
        ", ".join(sorted(u2p.get(n, n) for n in snap if _is_model_exposed(n, snap, u2p)))))
    print("上游: llm_utils_chat 通道（solo_work_remote，每 %ds 自动同步）%s" % (SOLO_MODELS_TTL, GATEWAY))
    print("认证: 自动复用 TRAE SOLO CN 登录态（token 临期自动续期）")
    _log("启动 port=%d models=%d" % (PORT, len(snap)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
