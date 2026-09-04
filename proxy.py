# -*- coding: utf-8 -*-
"""
Workspace (Midea) LLM 本地代理 - 纯 Python 标准库实现，无需 pip install 任何东西

把美的 Workspace 编辑器内部的 LLM 通道转成本地 API：
  OpenAI 兼容    GET /v1/models、POST /v1/chat/completions
  OpenAI 新版    POST /v1/responses（Responses 协议，兼容 codex-cli 等）
  Anthropic 兼容 POST /v1/messages、POST /v1/messages/count_tokens
  健康检查       GET /health

认证完全复用 Workspace 编辑器的登录态（token 过期时打开编辑器即可自动刷新）。

安全合规层（针对外部大模型使用规范）：
  1. 账号拦截：请求头 X-User-Id 以 ex_ 开头（个人账号）→ 403
  2. 敏感信息可逆脱敏：密码/令牌/私钥/密钥/手机号/身份证/车牌/银行卡 → 替换为
     PH_ 占位符后转发，原文写入 audit_redact.jsonl 供审计与响应后还原
  3. 违规词汇拦截：政治/宗教/违法等（SENSITIVE_WORDS 可配置）→ 403
  4. 忽略路径：含 .github 的路径直接跳过检查

用法:  python proxy.py        （默认端口 8787，可用环境变量 WS_PROXY_PORT 修改）
"""
import base64
import hashlib
import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request

# 模型映射：你本地希望用的名字 → Workspace 里的真实 model id
# 想加别名/改名字只改这张表即可，不用动下面的逻辑
MODEL_ALIAS = {
    "deepseek-v4-pro": "deepseek_v4",
    "deepseek-r1":     "deepseek_v4",
    "gpt-5.6-luna":    "gpt-5.6-luna",
    "qwen3.8-max":     "qwen3.8-max",
    "qwen3.7-plus":    "qwen3.7-plus",
    "qwen3.6-plus":    "qwen3.6-plus",
    "glm-5.2":         "aliyun-glm-5.2",
    "hw-glm-5":        "hw-glm-5",
}

PORT = int(os.environ.get("WS_PROXY_PORT", "8787"))
HOME = os.path.expanduser("~")
DB_FILE = os.path.join(HOME, ".local", "share", "workspace-code-prd", "opencode.db")
BASE = "https://workspace-prd.midea.com"
MODELS_TTL = 300  # 模型列表缓存 5 分钟

# 运行日志（同目录，方便排查）：WS_PROXY_LOG 可改路径，空字符串关闭
LOG_FILE = os.environ.get("WS_PROXY_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.log"))

# ── 行缓冲写盘：日志/审计走内存队列 + 后台刷盘线程，避免每请求一次磁盘 syscall ──
# 高并发下 _log/_audit_* 只做 append + notify，磁盘 IO 由单一线程批量完成。
_write_queue = []                 # 待落盘的 (file_kind, line) 列表
_write_queue_lock = threading.Lock()
_write_queue_event = threading.Event()
_writer_started = False
_writer_thread = None


def _kind_path(kind: str) -> str:
    if kind == "log":
        return LOG_FILE
    if kind == "audit":
        return AUDIT_FILE
    return PASS_AUDIT_FILE


def _writer_loop():
    """后台刷盘线程：批量把队列里的行写进对应文件，失败静默（与原行为一致）。"""
    while True:
        _write_queue_event.wait()
        _write_queue_event.clear()
        while True:
            with _write_queue_lock:
                batch = _write_queue[:]
                _write_queue.clear()
            if not batch:
                break
            grouped = {}  # kind -> [lines]
            for kind, line in batch:
                grouped.setdefault(kind, []).append(line)
            for kind, lines in grouped.items():
                path = _kind_path(kind)
                if not path:
                    continue
                try:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                except Exception:
                    pass


def _enqueue(kind: str, line: str):
    """入队一行（线程安全），并唤醒刷盘线程。"""
    with _write_queue_lock:
        _write_queue.append((kind, line))
    _write_queue_event.set()


def start_writer():
    """启动后台刷盘线程（幂等）。main() 与测试脚本调用。"""
    global _writer_started, _writer_thread
    with _write_queue_lock:
        if _writer_started:
            return
        _writer_started = True
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True, name="ws-proxy-writer")
    _writer_thread.start()


def flush_buffers(timeout: float = 5.0) -> int:
    """强制刷盘：把队列中现有行全部写完才返回。返回入队时统计的行数。"""
    with _write_queue_lock:
        n = len(_write_queue)
        if n == 0 and not _write_queue_event.is_set():
            return 0
        _write_queue_event.set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _write_queue_lock:
            if not _write_queue:
                break
        time.sleep(0.01)
    return n


def shutdown_writer():
    """兜底关闭：刷完队列剩余内容（线程为 daemon，进程退出不阻塞）。"""
    try:
        flush_buffers()
    except Exception:
        pass


def _log(msg: str):
    """写一行运行日志（入队异步落盘，失败静默）。"""
    if not LOG_FILE:
        return
    _enqueue("log", "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), msg))

# 审计日志：脱敏原文存储在这里，用于审计追查 + 响应后还原占位符
AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_redact.jsonl")

# 放行请求审计：未拦截的请求记录单独存一个文件，方便事后分析（谁、何时、什么协议/模型、token 数）
PASS_AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_pass.jsonl")


def _audit_pass(entry: dict):
    """记录一条放行请求（不写敏感原文，只有元信息）。失败静默。"""
    _enqueue("pass", json.dumps(entry, ensure_ascii=False))

# 忽略系统代理环境变量（本机代理可能不支持 CONNECT 隧道，导致 Errno 22）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── 会话：从 opencode.db 解出 accessToken，临期/过期用 refreshToken 自动续期 ──
_session_lock = threading.Lock()
_session_cache = {"at": 0, "data": None}

# accessToken 剩余有效期低于该秒数时视为「临期」，触发刷新（提前续期，避免过期后才补救）
REFRESH_AHEAD_SEC = 3600
# refresh-token 端点（与 ws.exe MideaAuthenticationService.performRefresh 一致）
REFRESH_URL = BASE + "/api/login-server/v1/auth/refresh-token"


def _decrypt_session_row(key: str, enc_hex: str) -> dict:
    """复刻 ws.exe 的 XOR 解密：hex 密文逐字节 XOR key 循环"""
    raw = bytes.fromhex(enc_hex)
    out = bytes(b ^ ord(key[i % len(key)]) for i, b in enumerate(raw))
    return json.loads(out.decode("utf-8"))


def _encrypt_session_row(key: str, payload: dict) -> str:
    """XOR 加密 session payload，返回 hex 密文（与 _decrypt_session_row 互逆）。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return bytes(b ^ ord(key[i % len(key)]) for i, b in enumerate(raw)).hex()


def _decode_jwt(token: str) -> dict:
    """解出 JWT payload（失败抛异常，由调用方决定是否跳过该行）。"""
    payload_b64 = token.split(".")[1].replace("-", "+").replace("_", "/")
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.b64decode(payload_b64).decode("utf-8"))


def _load_session_from_db() -> dict:
    """只读打开 opencode.db，解出 workspace_session 原始数据（含 refreshToken，若有）。"""
    if not os.path.exists(DB_FILE):
        raise RuntimeError("找不到 %s，请先安装并登录 Workspace 编辑器" % DB_FILE)
    # immutable=1 只读打开，避免和正在运行的 ws.exe 抢锁
    con = sqlite3.connect("file:%s?mode=ro" % DB_FILE.replace("\\", "/"), uri=True, timeout=2)
    try:
        rows = con.execute(
            "SELECT id, key, encrypted, timestamp FROM workspace_session WHERE id='default'"
        ).fetchall()
    finally:
        con.close()
    for _rid, key, enc, _ts in rows:
        try:
            return _decrypt_session_row(key, enc)
        except Exception:
            continue
    raise RuntimeError("db 中未找到有效 session，请先启动 Workspace 编辑器并登录")


def _write_session_row(payload: dict):
    """把 payload XOR 加密写回 workspace_session（id='default'）。"""
    if not os.path.exists(DB_FILE):
        raise RuntimeError("找不到 %s，请先安装并登录 Workspace 编辑器" % DB_FILE)
    con = sqlite3.connect(DB_FILE, timeout=5)
    try:
        con.execute("BEGIN IMMEDIATE")
        old = con.execute(
            "SELECT time_created FROM workspace_session WHERE id='default'"
        ).fetchone()
        time_created = old[0] if old else int(time.time() * 1000)
        key = hashlib.sha256(os.urandom(32)).hexdigest()  # 随机 XOR key，每次写回都换新
        enc = _encrypt_session_row(key, payload)
        now_s = int(time.time())
        now_ms = int(time.time() * 1000)
        con.execute(
            "INSERT INTO workspace_session(id, key, encrypted, timestamp, time_created, time_updated) "
            "VALUES('default', ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET key=excluded.key, encrypted=excluded.encrypted, "
            "timestamp=excluded.timestamp, time_updated=excluded.time_updated",
            (key, enc, now_s, time_created, now_ms),
        )
        con.commit()
    finally:
        con.close()


def _refresh_token_via_refresh(refresh_token: str) -> dict:
    """用 refreshToken 调刷新端点，返回 {accessToken, refreshToken, exp}。"""
    # 走 _http_json 而非 _opener：mock 测试模式下能记录请求并注入预设响应
    with _http_json(REFRESH_URL, {"Refresh-Token": refresh_token}, method="GET", timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("success"):
        raise RuntimeError("刷新失败：success=false")
    body = data.get("body") or {}
    # ws.exe 同样剥掉 Bearer 前缀再存
    access_token = (body.get("access_token") or "").replace("Bearer", "").replace("bearer", "").strip()
    refresh_token_new = body.get("refresh_token") or refresh_token
    if not access_token:
        raise RuntimeError("刷新响应缺少 access_token")
    exp = int(_decode_jwt(access_token).get("exp", 0))
    return {"accessToken": access_token, "refreshToken": refresh_token_new, "exp": exp}


def _refresh_session_locked(db_data: dict) -> dict:
    """（持有 _session_lock 时调用）用 db 里的 refreshToken 续期，并写回 db。"""
    rt = db_data.get("refreshToken")
    if not rt:
        raise RuntimeError("token 已过期且缺少 refreshToken，请打开 Workspace 编辑器重新登录")
    try:
        new = _refresh_token_via_refresh(rt)
    except Exception as e:
        # SSO 会话到期、网络异常等都归为刷新失败：提示重新登录
        _log("[session] 自动刷新失败: %s" % e)
        raise RuntimeError("登录态已失效，请打开 Workspace 编辑器重新登录后再试") from e
    # 写回 db（含新 refreshToken，保证下次还能续期；对 ws.exe 透明）
    label = db_data.get("label", "")
    uid = db_data.get("id", "")
    username = db_data.get("username", "") or _decode_jwt(new["accessToken"]).get("preferred_username", "")
    if not username:
        username = uid
    payload = {"accessToken": new["accessToken"], "id": uid, "label": label, "refreshToken": new["refreshToken"]}
    _write_session_row(payload)
    session = {
        "accessToken": new["accessToken"],
        "label": label,
        "id": uid,
        "username": username,
        "exp": new["exp"],
        "refreshToken": new["refreshToken"],
    }
    _log("[session] accessToken 已自动续期，新有效期至 %s" %
         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(session["exp"])) + " (UTC)")
    return session


def _read_session() -> dict:
    now = time.time()
    # 快路径：无锁读缓存（dict 读取原子），命中且未临期即返回，高并发下不再抢 _session_lock
    data = _session_cache["data"]
    if data and now - _session_cache["at"] < 10 and data["exp"] - now > REFRESH_AHEAD_SEC:
        return data
    with _session_lock:
        data = _session_cache["data"]
        if data and now - _session_cache["at"] < 10 and data["exp"] - now > REFRESH_AHEAD_SEC:
            return data
        # 缓存过期/临期：重读 db 拿最新（可能含别人刚写的 refreshToken）
        db_data = _load_session_from_db()
        token = db_data.get("accessToken") or ""
        try:
            jwt = _decode_jwt(token)
        except Exception:
            jwt = {}
        exp = int(jwt.get("exp", 0))
        username = jwt.get("preferred_username", "") or db_data.get("id", "")
        if exp - now <= REFRESH_AHEAD_SEC:
            # 临期或已过期：优先用 refreshToken 自动续期
            db_data["username"] = username
            session = _refresh_session_locked(db_data)
        else:
            session = {
                "accessToken": token,
                "label": db_data.get("label", ""),
                "id": db_data.get("id", ""),
                "username": username,
                "exp": exp,
                "refreshToken": db_data.get("refreshToken", ""),
            }
        _session_cache.update({"at": now, "data": session})
        return session


# 测试开关：若设置了 WS_PROXY_MOCK，则所有上游 HTTP 调用都走本地 mock（不触网）。
# 用于 CI / 本地验证（协议转换、脱敏、伪装还原），避免把测试请求打到真实后端。
MOCK_SERVER = None  # 测试脚本注入的本地 mock HTTP server（带 .status/.headers/.read() 属性）


def _http_json_mock(url: str, headers: dict, body: bytes = None, method: str = None, timeout: int = 120):
    if MOCK_SERVER is None:
        raise RuntimeError("WS_PROXY_MOCK 已开启，但未注入 MOCK_SERVER（测试脚本需先设置）")
    s = MOCK_SERVER
    if hasattr(s, "records"):
        try:
            s.records.append((url, dict(headers), body))
        except Exception:
            pass
    # 弹出下一个预设响应，包装成带 read/with 的响应对象返回（与真实 HTTPResponse 一致）
    resp = s.next_response()
    # 与真实 urllib 行为对齐：4xx/5xx 必须抛 HTTPError（否则上游 400 自愈重试无法触发）
    status = getattr(resp, "status", None) or getattr(resp, "code", 200)
    if isinstance(resp, urllib.error.HTTPError):
        raise resp
    if status and status >= 400:
        data = b""
        try:
            data = resp.read()
        except Exception:
            pass
        raise urllib.error.HTTPError(url, status, "Mock HTTP %s" % status,
                                     getattr(resp, "headers", None) or {}, io.BytesIO(data))
    return resp


def _http_json(url: str, headers: dict, body: bytes = None, method: str = None, timeout: int = 120):
    if os.environ.get("WS_PROXY_MOCK"):
        return _http_json_mock(url, headers, body, method, timeout)
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    for k, v in headers.items():
        req.add_header(k, v)
    return _opener.open(req, timeout=timeout)


# ── 模型列表 ──────────────────────────────────────────────────────────────
_models_lock = threading.Lock()
_models_cache = {"at": 0.0, "map": {}}
_models_inflight = []  # single-flight：TTL 过期瞬间只放一个线程去打上游，其余等待复用


def fetch_models(session: dict) -> dict:
    now = time.time()
    # 快路径：无锁读缓存（GIL 下 dict 读取原子），热缓存直接返回
    m = _models_cache["map"]
    if m and now - _models_cache["at"] < MODELS_TTL:
        return m
    # single-flight：抢不到 inflight 名额的线程等待现有请求完成后复用其结果
    acquired = False
    while True:
        with _models_lock:
            m = _models_cache["map"]
            if m and time.time() - _models_cache["at"] < MODELS_TTL:
                return m
            if not _models_inflight:
                _models_inflight.append(True)
                acquired = True
                break
        time.sleep(0.05)
    if not acquired:
        return fetch_models(session)  # 上面循环已等到缓存填充，这里兜底再查一次
    try:
        H = {"Authorization": "Bearer " + session["accessToken"]}
        with _http_json(BASE + "/api/cn-control/ide/list-organizations", H) as r:
            orgs = json.loads(r.read().decode("utf-8"))
        org_id = (orgs.get("organizations") or [{}])[0].get("id")
        if not org_id:
            raise RuntimeError("list-organizations 返回异常: %s" % json.dumps(orgs)[:200])
        with _http_json(BASE + "/api/cn-control/ide/list-assistants?organizationId=" + org_id, H) as r:
            assistants = json.loads(r.read().decode("utf-8"))
        if isinstance(assistants, dict):
            assistants = assistants.get("assistants") or []
        out = {}
        for a in assistants:
            for mdl in (a.get("configResult") or {}).get("config", {}).get("models") or []:
                base = mdl.get("onPremProxyUrl") or mdl.get("apiBase")
                if not base:
                    continue
                out[mdl["model"]] = {
                    "id": mdl["model"],
                    "name": mdl.get("name", mdl["model"]),
                    "baseUrl": base.rstrip("/"),
                    "capabilities": [c.lower() for c in mdl.get("capabilities") or []],
                }
        if not out:
            raise RuntimeError("模型列表为空，检查 token 权限")
        with _models_lock:
            _models_cache.update({"at": time.time(), "map": out})
        return out
    finally:
        with _models_lock:
            if _models_inflight:
                _models_inflight.pop()


def get_models(session: dict, force: bool = False) -> dict:
    if force:
        with _models_lock:
            _models_cache.update({"at": 0.0, "map": {}})
    return fetch_models(session)


# ── 上游请求头（复刻 ws.exe 的 fetch wrapper）─────────────────────────────
def upstream_headers(session: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + session["accessToken"],
        "TEAM": "workspace-dev",
        "SCENE": "workspace-local",
    }


def inject_user(body_bytes: bytes, session: dict, real_model: str) -> bytes:
    """把工号注入请求体 user 字段，并把 model 字段改写为上游真实 id（与 ws.exe 行为一致）"""
    try:
        b = json.loads(body_bytes.decode("utf-8"))
        if session.get("id") and b.get("user") is None:
            b["user"] = session["id"]
        if real_model:
            b["model"] = real_model
        return json.dumps(b, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body_bytes


def _fmt_ts(epoch_sec: float) -> str:
    """Windows 上 time.localtime 超出范围会抛 OSError，这里用 utcfromtypestamp 兜底"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch_sec))
    except (OSError, OverflowError, ValueError):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch_sec)) + " (UTC)"


# ── 客户端指纹伪装：把第三方工具名字替换成 Workspace 风格占位符 ────────────
# 目的：上游（litellm 网关 / vLLM）可能检测 UA / 客户端标识，伪装成
#       Workspace 官方客户端可降低被识别为"外部工具调用"的概率。
# 伪装格式：`Workspace_<hash>`（仍是 Workspace 前缀，防检测效果一致）；
#           每个原词生成唯一占位符并记入脱敏缓存，响应侧精确还原原文。
# 只匹配"独立词/单词边界"，避免误伤正文（如 normal、ocean 等普通单词）。
_CLIENT_SPOOF = {
    "Codex":          "Workspace",
    "Codex CLI":      "Workspace",
    "Claude Code":    "Workspace",
    "Claude":         "Workspace",
    "Anthropic":      "Workspace",
    "Copilot":        "Workspace",
    "Cline":          "Workspace",
    "Roo":            "Workspace",
    "Trae":           "Workspace",
    "TraeCode":       "Workspace",
    "CodeArts Agent": "Workspace",
    "CodeArtsAgent":  "Workspace",
    "CodeArts":       "Workspace",
    "OpenCode":       "Workspace",
    "Cursor":         "Workspace",
    "DeepSeek":       "Workspace",
    "Windsurf":       "Workspace",
    "OpenClaw":       "Workspace",
    "Clawdbot":       "Workspace",
    "ZCode":          "Workspace",
    "Z Code":         "Workspace",
}
# 预编译：整词边界匹配（不区分大小写），先长词后短词避免子串截胡。
# 用显式 ASCII 边界 (?<![A-Za-z0-9_]) / (?![A-Za-z0-9_]) 而非 \b：
# Python3 中 \b 对中文不构成词边界，客户端名后直接连中文（如 "Trae客户端"）会漏伪装。
_SPOOF_PATTERNS = sorted(
    ((re.compile(r"(?i)(?<![A-Za-z0-9_])" + re.escape(k) + r"(?![A-Za-z0-9_])"), k) for k in _CLIENT_SPOOF),
    key=lambda t: len(t[1]),
    reverse=True,
)
# 原词 → Workspace_<hash> 占位符的稳定映射（sha1 前 6 位，与 _CLIENT_SPOOF 一一对应）
_SPOOF_PH = {
    orig: "Workspace_" + hashlib.sha1(orig.encode("utf-8")).hexdigest()[:6].upper()
    for orig in _CLIENT_SPOOF
}
# 还原正则：Workspace_ 后跟 6 位十六进制哈希。
# 尾部同样用显式 ASCII 边界（非 (?![A-Za-z0-9_])），否则占位符后连中文（如
# "Workspace_060D15客户端"）时 \b 失效导致不还原。
_SPOOF_PH_RE = re.compile(r"Workspace_[A-F0-9]{6}(?![A-Za-z0-9_])")


def _spoof_text(text: str, user: str = "") -> str:
    """把文本里的客户端名替换为 Workspace_<hash> 占位符（可逆）。"""
    for pat, orig in _SPOOF_PATTERNS:
        def _repl(m, _orig=orig, _user=user):
            ph = _SPOOF_PH[_orig]
            if _lookup(ph) is None:
                _remember(ph, _orig, "client_spoof", _user)
            return ph
        text = pat.sub(_repl, text)
    return text


def _spoof_walk(obj, user: str = ""):
    """递归遍历，把字符串值里的客户端名替换为 Workspace 占位符。"""
    if isinstance(obj, str):
        return _spoof_text(obj, user)
    if isinstance(obj, list):
        return [_spoof_walk(v, user) for v in obj]
    if isinstance(obj, dict):
        return {k: _spoof_walk(v, user) for k, v in obj.items()}
    return obj


def _unspoof_text(text: str) -> str:
    """把响应文本里 Workspace_<hash> 占位符还原为原客户端名。"""
    def _repl(m):
        entry = _lookup(m.group(0))
        return entry["original"] if entry else m.group(0)
    return _SPOOF_PH_RE.sub(_repl, text)


def _unspoof_walk(obj):
    """递归遍历，把响应文本里伪装占位符还原为原客户端名。"""
    if isinstance(obj, str):
        return _unrestore_text(obj)
    if isinstance(obj, list):
        return [_unspoof_walk(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _unspoof_walk(v) for k, v in obj.items()}
    return obj


def spoof_upstream_headers(headers: dict) -> dict:
    """改写转发上游的 HTTP 头：去掉可疑客户端 UA，补上 Workspace 官方 UA。"""
    h = dict(headers)
    if "User-Agent" in h:
        del h["User-Agent"]
    h["User-Agent"] = "Workspace"  # 上游统一看到 Workspace 客户端
    return h


# ── 安全合规层 ─────────────────────────────────────────────────────────────
# 可逆脱敏（请求侧替换为占位符 → 响应侧还原）：
#   密码/API Key/令牌/私钥/连接串等“值可以被 LLM 引用但原文不能外泄”的键值对
REDACT_PATTERNS = [
    # password/pwd/passwd/密码（支持 = : 中文冒号 后跟非空白值）
    (re.compile(r"(?i)(password|passwd|pwd|密码)\s*[=:：]\s*([^\s,，;；\"']+)"), "password"),
    # api key / secret / token / access_token / 密钥 / 秘钥
    (re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|密钥|秘钥)\s*[=:：]\s*([^\s,，;；\"']+)"), "token"),
    # 私钥 PEM 块
    (re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]*-----END [A-Z ]*PRIVATE KEY-----"), "private_key"),
    # 数据库连接串 mongodb:// 或 mysql:// 等（含认证信息）
    (re.compile(r"(?i)(mongodb|mysql|postgres(?:ql)?|redis)://[^\s\"']+"), "dsn"),
    # JWT（eyJ 开头的三段式）
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "jwt"),
    # 一般化的 “key = 长值” 长串（≥16 位字母数字混排，可能为密钥）
    (re.compile(r"(?i)(key|token|secret|credential)\s*[=:：]\s*([A-Za-z0-9_-]{16,})"), "secret"),
]

# 违规词汇（政治/宗教/违法等），命中即 403。可在此增删。
SENSITIVE_WORDS = [
    "法轮功", "六四", "天安门事件", "反政府", "颠覆国家",
]

# 个人账号特征：命中任一即判定为个人账号 → 403 禁止调用外部大模型。
# 美的工号里 ex_ 开头的是外包/个人账号（如 ex_****）。
# 如需扩展其他特征（如 tmp_ / guest_ / 外部邮箱后缀），在下面追加正则即可。
# 注意：此处的"特征规则"可扩展，但"是否启用拦截"是固定逻辑，不可关闭。
PERSONAL_ACCOUNT_PATTERNS = [
    re.compile(r"^ex_[A-Za-z0-9_.\-]+$", re.IGNORECASE),
]


def _is_personal_account(user: str) -> bool:
    """按个人账号特征判定是否为个人账号。"""
    if not user:
        return False
    u = user.strip()
    return any(p.match(u) for p in PERSONAL_ACCOUNT_PATTERNS)

# 可逆 PII：手机号 / 身份证号 / 车牌号 / 银行卡号。
# 用命名分组交替 + 前后数字边界，保证"一段独立数字串只被命中一次"，
# 多个同类值（如一条消息里 2 个手机号）会各生成唯一占位符，响应时全部还原。
# 注意顺序：交替匹配按"整段独立数字串"命中，边界 (?<!\d)/(?!\d) 防止身份证/银行卡
# 里截出手机号之类的子串二次替换成残片。
_PII_GROUP_ORDER = ["手机号", "身份证号", "车牌号", "银行卡号"]
_PII_PATTERN = re.compile(
    r"(?<!\d)(?P<身份证号>\d{17}[\dXx])(?!\d)"
    r"|(?<!\d)(?P<银行卡号>6[0-9]{11,17}|4[0-9]{12,18}|5[1-5][0-9]{14,17})(?!\d)"
    r"|(?<!\d)(?P<手机号>1[3-9]\d{9})(?!\d)"
    r"|(?P<车牌号>[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,6})"
)

# 邮箱 PII：任何带个人特征的邮箱都要脱敏（如 ex_****@partner.midea.com）。
# 邮箱整体替换为占位符，响应还原。匹配标准邮箱格式，避免误伤普通词。
_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# 忽略的路径片段：含这些片段的路径请求直接放行，不做任何检查
IGNORE_PATH_PARTS = (".github",)


# 通用辅助：就地递归处理所有字符串值
def _redact_walk(obj, user, hits):
    """就地递归：对每个字符串做可逆脱敏，返回处理后的对象（同一对象，原地修改）。"""
    if isinstance(obj, dict):
        for k in obj:
            obj[k] = _redact_walk(obj[k], user, hits)
        return obj
    if isinstance(obj, list):
        for i in range(len(obj)):
            obj[i] = _redact_walk(obj[i], user, hits)
        return obj
    if isinstance(obj, str):
        newtext, hs = _replace_redact(obj, user)
        hits.extend(hs)
        return newtext
    return obj


def _restore_walk(obj):
    """就地递归：对每个字符串还原占位符。"""
    if isinstance(obj, dict):
        for k in obj:
            obj[k] = _restore_walk(obj[k])
        return obj
    if isinstance(obj, list):
        for i in range(len(obj)):
            obj[i] = _restore_walk(obj[i])
        return obj
    if isinstance(obj, str):
        return restore_text(obj)
    return obj


def _block_walk(obj, hits):
    """就地递归：对每个字符串做拦截检查，收集命中类别。"""
    if isinstance(obj, dict):
        for k in obj:
            _block_walk(obj[k], hits)
        return obj
    if isinstance(obj, list):
        for item in obj:
            _block_walk(item, hits)
        return obj
    if isinstance(obj, str):
        hits.extend(_check_block(obj))
    return obj


# 审计存储（行缓冲入队 + 后台批量落盘，避免每条审计一次磁盘 syscall）
def _audit_record(entry: dict):
    try:
        _enqueue("audit", json.dumps(entry, ensure_ascii=False))
    except Exception:
        # 审计失败不影响主流程
        pass


def _ph(seed: str) -> str:
    """生成占位符：PH_ 开头 + 哈希，保证同一原文哈希稳定、可还原。"""
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10].upper()
    return "PH_%s" % h


# 脱敏映射：placeholder → 原文（进程内缓存 + 每次新值也回写审计文件）
_redact_cache = {}
_redact_cache_lock = threading.Lock()


def _remember(placeholder: str, original: str, category: str, user: str):
    entry = {"placeholder": placeholder, "original": original, "category": category,
             "user": user, "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}
    with _redact_cache_lock:
        _redact_cache[placeholder] = entry
    _audit_record(entry)


def _lookup(placeholder: str):
    with _redact_cache_lock:
        return _redact_cache.get(placeholder)


def load_audit_cache():
    """启动时加载已有审计文件，使重启后仍能还原历史占位符。"""
    if not os.path.exists(AUDIT_FILE):
        return
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("placeholder"):
                    with _redact_cache_lock:
                        _redact_cache.setdefault(entry["placeholder"], entry)
    except Exception:
        pass


def _replace_redact(text: str, user: str) -> tuple[str, list]:
    """对单条字符串做可逆脱敏：把匹配的敏感值替换为占位符。返回 (新文本, 命中的 category 列表)。"""
    hits = []
    for pattern, category in REDACT_PATTERNS:
        text, hs = _replace_redact_one(text, pattern, category, user)
        hits.extend(hs)
    # PII（手机号/身份证/车牌/银行卡）与邮箱整体走组合正则，避免同段值被连环替换
    if _PII_PATTERN.search(text) or _EMAIL_PATTERN.search(text):
        text, hs = _replace_pii(text, user)
        hits.extend(hs)
    return text, hits


def _replace_pii(text: str, user: str) -> tuple[str, list]:
    """把文本里的手机号/身份证/车牌/银行卡/邮箱替换成 PH_ 占位符。

    PII 与邮箱各用一条正则 finditer 扫过：每段独立值只命中一次，
    各自生成唯一占位符；同一条字符串里多个同类值（如 2 个手机号）会全部替换。
    """
    hits = []
    out = []
    last = 0
    for m in _PII_PATTERN.finditer(text):
        out.append(text[last:m.start()])
        category = next((g for g in _PII_GROUP_ORDER if m.group(g)), "敏感信息")
        full = m.group(0)
        ph = _ph(full)
        if _lookup(ph) is None:
            _remember(ph, full, category, user)
        hits.append(category)
        out.append(ph)
        last = m.end()
    text = "".join(out) + text[last:]
    # 邮箱脱敏
    if _EMAIL_PATTERN.search(text):
        out2 = []
        last2 = 0
        for m in _EMAIL_PATTERN.finditer(text):
            out2.append(text[last2:m.start()])
            full = m.group(0)
            ph = _ph(full)
            if _lookup(ph) is None:
                _remember(ph, full, "邮箱", user)
            hits.append("邮箱")
            out2.append(ph)
            last2 = m.end()
        text = "".join(out2) + text[last2:]
    return text, hits


def _replace_redact_one(text: str, pattern, category: str, user: str) -> tuple[str, list]:
    """单条正则的脱敏：非重叠遍历匹配，原地构造新字符串。返回 (新文本, 命中类别列表)。"""
    hits = []
    # 键值对类：保留键名与分隔符，只替换值 → 便于 LLM 理解并引用占位符
    kv = category in ("password", "token", "secret") and pattern.groups >= 2

    out = []
    last = 0
    for m in pattern.finditer(text):
        out.append(text[last:m.start()])
        if kv:
            key, val = m.group(1), m.group(2)
            ph = _ph(val)
            if _lookup(ph) is None:
                _remember(ph, val, category, user)
            hits.append(category)
            out.append("%s=%s" % (key, ph))
        else:
            full = m.group(0)
            ph = _ph(full)
            if _lookup(ph) is None:
                _remember(ph, full, category, user)
            hits.append(category)
            out.append(ph)
        last = m.end()
    out.append(text[last:])
    return "".join(out), hits


def _check_block(text: str) -> list:
    """对单条字符串做不可逆拦截检查。返回命中的类别列表（空=放行）。"""
    hits = []
    for w in SENSITIVE_WORDS:
        if w in text:
            hits.append("敏感词:" + w)
    return hits


def redact_body(body: dict, user: str) -> tuple[dict, list]:
    """对整个请求体递归脱敏。返回 (新 body, 命中类别列表)。命中可逆脱敏则放行。"""
    hits = []
    # 注意：json.loads 生成的 dict 是新的，_redact_walk 就地修改它，不污染原对象
    body = _redact_walk(body, user, hits)
    return body, hits


def block_check_body(body: dict) -> list:
    """对整个请求体递归做不可逆拦截检查。返回命中类别列表（空=放行）。"""
    hits = []
    _block_walk(body, hits)
    return hits


def restore_body(body: dict) -> dict:
    """对整个响应体递归还原占位符。返回新 body。"""
    return _restore_walk(body)


def restore_text(text: str) -> str:
    """还原纯文本中的占位符。"""
    def _repl(m):
        entry = _lookup(m.group(0))
        return entry["original"] if entry else m.group(0)
    return re.sub(r"PH_[A-F0-9]{10}", _repl, text)


# 流式还原辅助：占位符可能被模型按 token 拆开（如 "PH_1CC0E18D"、"DC"）。
# 输出增量时，先把尾部可能是"未完成占位符"的片段暂存，等下一段拼完整再还原。
# 这样流式 delta 也能正确还原手机号/邮箱/客户端名，而不是只在 done/completed 事件里还原。
_PARTIAL_PH_RE = re.compile(
    r"(?:PH_[A-F0-9]{0,9}"
    r"|W(?:o(?:r(?:k(?:s(?:p(?:a(?:c(?:e(?:_[A-F0-9]{0,6})?)?)?)?)?)?)?)?)?)?$"
)
# 尾部孕育探测：串尾是否出现了占位符前缀的一部分（如 "P" "Wo" "Workspa"）。
# 命中才需要走 _PARTIAL_PH_RE 缓冲逻辑；普通中文文本几乎不命中。
# 前缀链必须与 _PARTIAL_PH_RE 的可选链完全对齐，缺 "Workspac" 会导致占位符被
# 切成 "Workspac" + "e_XXXXXX" 两块时，前半段被快路径直接放行、无法跨 chunk 还原。
_PH_SEED_TAIL_RE = re.compile(
    r"(?:P|W|Wo|Wor|Work|Works|Worksp|Workspa|Workspac|Workspace|Workspace_|PH|PH_)$"
)


def _unrestore_text(text: str) -> str:
    """同时还原 PH_ 敏感值占位符 + Workspace_ 客户端伪装占位符。

    快路径：绝大多数流式 delta 是不含占位符的普通文本，用一次 substring
    探测（O(len) 且无锁）跳过两次 re.sub + 两次缓存锁查询。
    """
    if "PH_" not in text and "Workspace_" not in text:
        return text
    return restore_text(_unspoof_text(text))


def _stream_restore(delta: str, pend: str) -> tuple[str, str]:
    """流式还原增量文本。返回 (可安全输出的已还原文本, 需留给下一段的尾部缓冲)。

    快路径：pend 与 delta 都不含占位符前缀且 delta 尾部也不可能孕育占位符时，
    直接拼串返回，避免正则 search。
    """
    if pend:
        combined = pend + delta
        pend = ""
    else:
        combined = delta
    # 快路径：整段无 "PH_"/"Workspace_"，且尾部不可能孕育占位符前缀 → 无需正则
    if "PH_" not in combined and "Workspace_" not in combined and not _PH_SEED_TAIL_RE.search(combined):
        return combined, ""
    m = _PARTIAL_PH_RE.search(combined)
    if m:
        safe, hold = combined[:m.start()], combined[m.start():]
    else:
        safe, hold = combined, ""
    return _unrestore_text(safe), hold


def audit_block(entry: dict):
    """记录一次拦截事件（供合规审计）。"""
    _audit_record({"action": "block", **entry})


def should_ignore_path(path: str) -> bool:
    """命中 IGNORE_PATH_PARTS 的路径跳过所有检查（如 .github 目录）。"""
    p = path.replace("\\", "/").lower()
    return any(part.lower() in p for part in IGNORE_PATH_PARTS)


# ── Anthropic 消息文本提取（供 count_tokens 本地估算使用）───────────────────
def parse_anthropic(body: dict) -> list:
    """从 Anthropic Messages 协议提取消息文本。"""
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
                elif t == "thinking" and part.get("thinking"):
                    texts.append(part["thinking"])
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


# ── 请求合规入口 ────────────────────────────────────────────────────────────
def compliance_check(body: dict, user: str) -> tuple[bool, str, dict]:
    """
    对整个请求体做三层检查 + 可逆脱敏。
    返回 (是否放行, 拦截原因/提示, 脱敏后的 body)。
    放行时 body 已脱敏；拦截时 body 原样返回（不应被使用）。
    """
    # 1) 不可逆拦截检查（PII / 违规词）
    blocked = block_check_body(body)
    if blocked:
        return False, "禁止向外部大模型传敏感信息，已拦截：%s" % ",".join(sorted(set(blocked))), body

    # 2) 可逆脱敏（密码/密钥/令牌等）→ 放行，但值已被占位符替换
    new_body, _ = redact_body(body, user)
    return True, "", new_body


# ── 上游调用 ────────────────────────────────────────────────────────────────
# 上游 400 报文里 "supports at most N completion tokens" → 提取模型 max_tokens 上限
_MAX_TOKENS_LIMIT_RE = re.compile(r"supports at most (\d+) completion tokens")


# ── 流式 SSE 状态机：OpenAI chat delta → Responses / Anthropic 事件 ─────────
class _ResponsesStreamState:
    """把上游 OpenAI chat 流式 chunk 转成 Responses 协议 SSE 事件序列。

    事件序列对齐官方 Responses 流式协议（codex-cli / openai SDK 依赖）：
      response.created
        → (response.output_item.added → response.content_part.added)*
        → (response.output_text.delta / response.reasoning_summary_text.delta)*
        → (response.output_text.done → response.content_part.done → response.output_item.done)*
        → response.completed
    """

    def __init__(self, model: str):
        self.model = model
        self.resp_id = "resp_" + _rand()
        self.created = int(time.time())
        self.started = False
        self.finished = False
        self.finish_reason = None
        # 累积内容
        self.text = ""
        self.reasoning = ""
        self.tool_args = ""
        self.tool_name = ""
        self.tool_id = ""
        self.usage = None
        # 流式增量 item 状态（边 feed 边发增量事件，finish 时发 done/completed）
        self.text_item_id = "msg_" + _rand()
        self.reason_item_id = "rs_" + _rand()
        self.reason_opened = False
        self.text_opened = False
        self.reason_index = None   # reason item 打开时的 output_index
        self.text_index = None     # text item 打开时的 output_index
        self.next_output_index = 0
        # 流式占位符还原缓冲：跨 chunk 的未完成占位符尾部先暂存
        self._pend = ""

    def feed(self, chunk: dict):
        """返回 [(event, payload_json_str), ...]"""
        out = []
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        if not self.started:
            self.started = True
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {
                    "id": self.resp_id, "object": "response", "created_at": self.created,
                    "status": "in_progress", "model": self.model, "output": [],
                },
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

        # 还原占位符（客户端名 + PH_ 敏感值，如自我认知/回显手机号场景）。
        # 占位符可能被上游按 token 拆开，跨 chunk 缓冲拼完整后再还原。
        for i in range(len(out)):
            if out[i][0] in ("response.reasoning_summary_text.delta", "response.output_text.delta"):
                try:
                    payload = json.loads(out[i][1])
                    d_text, self._pend = _stream_restore(payload.get("delta", ""), self._pend)
                    payload["delta"] = d_text
                    out[i] = (out[i][0], json.dumps(payload, ensure_ascii=False))
                except Exception:
                    pass

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

    def _close_reasoning(self, out):
        if not self.reason_opened:
            return
        idx = self.reason_index
        out.append(("response.reasoning_summary_text.done", json.dumps({
            "type": "response.reasoning_summary_text.done", "item_id": self.reason_item_id,
            "output_index": idx, "summary_index": 0,
            "text": _unrestore_text(self.reasoning),
        }, ensure_ascii=False)))
        out.append(("response.reasoning_summary_part.done", json.dumps({
            "type": "response.reasoning_summary_part.done", "item_id": self.reason_item_id,
            "output_index": idx, "summary_index": 0,
            "part": {"type": "summary_text", "text": _unrestore_text(self.reasoning)},
        }, ensure_ascii=False)))
        out.append(("response.output_item.done", json.dumps({
            "type": "response.output_item.done", "output_index": idx,
            "item": {"type": "reasoning", "id": self.reason_item_id,
                     "summary": [{"type": "summary_text", "text": _unrestore_text(self.reasoning)}]},
        }, ensure_ascii=False)))
        self.reason_opened = False

    def _close_text(self, out):
        if not self.text_opened:
            return
        idx = self.text_index
        out.append(("response.output_text.done", json.dumps({
            "type": "response.output_text.done", "item_id": self.text_item_id,
            "output_index": idx, "content_index": 0,
            "text": _unrestore_text(self.text),
        }, ensure_ascii=False)))
        out.append(("response.content_part.done", json.dumps({
            "type": "response.content_part.done", "item_id": self.text_item_id,
            "output_index": idx, "content_index": 0,
            "part": {"type": "output_text", "text": _unrestore_text(self.text), "annotations": []},
        }, ensure_ascii=False)))
        out.append(("response.output_item.done", json.dumps({
            "type": "response.output_item.done", "output_index": idx,
            "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                     "status": "completed",
                     "content": [{"type": "output_text", "text": _unrestore_text(self.text), "annotations": []}]},
        }, ensure_ascii=False)))
        self.text_opened = False

    def finish(self):
        """上游流结束：输出完整的 Responses 事件序列（codex-cli 依赖）。"""
        if self.finished:
            return []
        self.finished = True
        out = []
        # 先补发未输出的 message_start（若上游只发了 usage）
        if not self.started:
            self.started = True
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))

        # 关闭所有未闭合 item
        self._close_reasoning(out)
        self._close_text(out)

        items = []
        if self.reasoning:
            items.append({"type": "reasoning", "id": self.reason_item_id,
                          "summary": [{"type": "summary_text", "text": _unrestore_text(self.reasoning)}]})
        if self.text:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant", "status": "completed",
                          "content": [{"type": "output_text", "text": _unrestore_text(self.text), "annotations": []}]})
        if self.tool_name:
            items.append({"type": "function_call", "id": "fc_" + _rand(), "call_id": self.tool_id,
                          "name": self.tool_name, "arguments": self.tool_args, "status": "completed"})
        if not items:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant", "status": "completed",
                          "content": [{"type": "output_text", "text": "", "annotations": []}]})
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
                    "input_tokens_details": {"cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
                    "output_tokens": usage.get("completion_tokens", 0),
                    "output_tokens_details": {"reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)},
                    "total_tokens": usage.get("total_tokens", 0),
                },
                "error": None,
                "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            },
        }, ensure_ascii=False)))
        return out


class _AnthropicStreamState:
    """把上游 OpenAI chat 流式 chunk 转成 Anthropic Messages SSE 事件序列。"""

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
        # 流式占位符还原缓冲：跨 chunk 的未完成占位符尾部先暂存
        self._pend = ""

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
            # 上游思维链也可能引用占位符，跨 chunk 缓冲拼完整后还原
            d_rc, self._pend = _stream_restore(rc, self._pend)
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "thinking_delta", "thinking": d_rc},
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
            # 占位符可能被上游按 token 拆开，跨 chunk 缓冲拼完整后再还原
            d_content, self._pend = _stream_restore(content, self._pend)
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "text_delta", "text": d_content},
            }, ensure_ascii=False)))
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                fn = tc.get("function") or {}
                idx = tc.get("index", 0)
                if tc.get("id") or fn.get("name"):
                    if idx not in self.tool_blocks:
                        self.tool_blocks[idx] = {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": ""}
                        self.block_index += 1
                        out.append(("content_block_start", json.dumps({
                            "type": "content_block_start", "index": self.block_index - 1,
                            "content_block": {"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": {}},
                        }, ensure_ascii=False)))
                if fn.get("arguments"):
                    self.tool_blocks[idx]["args"] += fn["arguments"]
                    out.append(("content_block_delta", json.dumps({
                        "type": "content_block_delta", "index": self.block_index - 1,
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
            out.append(("content_block_stop", json.dumps({"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.thinking_block = False
        if self.text_block:
            out.append(("content_block_stop", json.dumps({"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.text_block = False
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


# ── HTTP 服务 ─────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 请求访问日志写入 proxy.log（不再往 stderr）
        _log("[req] %s %s" % (self.command, self.path))
        # 原始 format 也可保留到日志：fmt % args
        try:
            _log("[req] detail " + (fmt % args))
        except Exception:
            pass

    def _json(self, code: int, obj: dict, extra_headers: dict = None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端在响应写出前主动断开（超时/取消），属正常现象，不再冒泡成 traceback
            _log("[conn] 客户端在响应写出前断开 (%s %s)" % (self.command, self.path))
        except Exception as e:
            # 其余写盘异常（如 ssl/编码问题）不抛给上层，避免被当成 502 重新响应
            _log("[conn] 响应写出异常: %r" % (e,))

    def _block(self, message: str, user: str, protocol: str, model: str = ""):
        audit_block({"user": user, "protocol": protocol, "model": model,
                     "reason": message, "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
        return self._json(403, {
            "error": {
                "type": "compliance_blocked",
                "message": message,
                "code": "compliance_blocked",
            }
        })

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            session = _read_session()
            if path in ("/health",):
                return self._json(200, {
                    "ok": True,
                    "user": session["username"],
                    "tokenExp": _fmt_ts(session["exp"]),
                })
            if path in ("/v1/models", "/models"):
                models = get_models(session, force=True)
                now = int(time.time())
                return self._json(200, {
                    "object": "list",
                    "data": [{
                        "id": m["id"], "object": "model", "created": now,
                        "owned_by": "midea-workspace",
                        "metadata": {"name": m["name"], "baseUrl": m["baseUrl"],
                                     "capabilities": m["capabilities"]},
                    } for m in models.values()],
                })
            return self._json(404, {"error": {"message": "支持: GET /v1/models, POST /v1/chat/completions, POST /v1/responses, POST /v1/messages, GET /health"}})
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

    # ---- 统一 POST 入口：根据路径分发到三种协议 ----
    def do_POST(self):
        path = self.path.split("?")[0]
        base = path
        # 兼容带项目名的路径（如 /repo/.github/v1/... 或 /{project}/.github/v1/...）
        # 若路径包含 .github 且有 API 后缀，则按 API 路由处理，但 _common_llm 内会跳过合规检查
        if "/.github/" in path:
            idx = path.find("/.github/")
            suffix = path[idx:]  # 例如 "/.github/v1/chat/completions"
            if "/v1/chat/completions" in suffix or "/chat/completions" in suffix:
                return self._handle_openai_chat()
            if "/v1/responses" in suffix or "/responses" in suffix:
                return self._handle_responses()
            if "/v1/messages/count_tokens" in suffix:
                return self._handle_count_tokens()
            if "/v1/messages" in suffix or "/messages" in suffix:
                return self._handle_anthropic()
            return self._json(404, {"error": {"message": "不支持该 .github 路径"}})

        # Anthropic token 计数（不转发上游，本地估算）
        if path in ("/v1/messages/count_tokens",):
            self._handle_count_tokens()
            return

        if path in ("/v1/chat/completions", "/chat/completions"):
            self._handle_openai_chat()
            return
        if path in ("/v1/responses", "/responses"):
            self._handle_responses()
            return
        if path in ("/v1/messages", "/messages"):
            self._handle_anthropic()
            return
        return self._json(404, {"error": {"message": "仅支持 /v1/chat/completions, /v1/responses, /v1/messages, /v1/messages/count_tokens"}})

    # ---- Anthropic count_tokens ----
    def _handle_count_tokens(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            req_body = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON"}})
        # 本地估算 token（字符数/3 粗略）
        texts = parse_anthropic(req_body)
        total_chars = sum(len(t) for t in texts)
        estimated = max(1, total_chars // 3)
        return self._json(200, {"input_tokens": estimated})

    # ---- 通用：读取 body + 解析协议 + 合规检查 + 转发 + 还原 ----
    def _common_llm(self, protocol: str, out_fn):
        """
        protocol: 'openai' | 'responses' | 'anthropic'
        out_fn(upstream_bytes: bytes, req_body: dict) -> dict 响应转换器
        """
        # 1) 读 body
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            req_body = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})

        # 2) 忽略路径（.github 等）→ 跳过账号拦截与内容检查，直接转发
        ignore = should_ignore_path(self.path)

        # 2.5) 账号拦截（ex_ 开头 = 个人账号），忽略路径除外。
        #      仅当客户端显式声明 X-User-Id 时才判定身份：ex_ 开头 → 拦截。
        #      本地直连（不带头）视为可信本机用户，放行账号检查（内容层检查仍生效）。
        #      此拦截为强制逻辑，不可配置关闭。
        declared_user = self.headers.get("X-User-Id") or ""
        session = None
        try:
            session = _read_session()
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})
        if declared_user:
            user = declared_user
        else:
            user = session.get("username", "") or session.get("id", "") or ""
        if not ignore and declared_user and _is_personal_account(user):
            return self._block("个人账号禁止调用外部大模型，请使用公司账号", user, protocol, req_body.get("model", ""))

        # 3) 合规检查 + 脱敏
        if not ignore:
            ok, reason, redacted_body = compliance_check(req_body, user)
            if not ok:
                return self._block(reason, user, protocol, req_body.get("model", ""))
            req_body = redacted_body

        # 3.5) 客户端指纹伪装：把 Codex/Claude/CodeArts/Trae 等名字替换为 Workspace 占位符
        #      （响应回来时在 _common_llm 出口统一还原，见步骤 6/7）
        req_body = _spoof_walk(req_body, user)

        # 4) 模型解析
        model_id = req_body.get("model") or ""
        model_id = MODEL_ALIAS.get(model_id, model_id)  # 别名 → 真实 id
        try:
            models = get_models(session)
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})
        m = models.get(model_id)
        if not m:
            ids = ", ".join(sorted(models))
            return self._json(404, {"error": {"message": "模型 %s 不存在。可用: %s" % (model_id, ids)}})

        url = m["baseUrl"] + "/chat/completions"

        # 4.5) 构造上游 OpenAI chat 请求体（三种协议统一转成 chat 格式）
        up_body = req_body
        if protocol == "responses":
            up_body = convert_responses_to_openai(req_body)
        elif protocol == "anthropic":
            up_body = convert_anthropic_to_openai(req_body)
        # 流式统一：上游只发 OpenAI SSE，格式转换在回程做
        is_stream = bool(up_body.get("stream"))

        payload = inject_user(json.dumps(up_body, ensure_ascii=False).encode("utf-8"), session, m["id"])

        # 5) 转发上游（上游统一走 OpenAI chat 接口，SSE 透传也基于 chat 格式）
        #    上游 400 且报文含 "at most N completion tokens" 时，按 N 截断 max_tokens 重试一次
        def _forward(pay):
            return _http_json(url, spoof_upstream_headers(upstream_headers(session)), body=pay, timeout=600)

        try:
            upstream = _forward(payload)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            # 自愈：max_tokens 超过模型上限（如 "supports at most 128000 completion tokens"）
            retry_payload = None
            if e.code == 400:
                lim = _MAX_TOKENS_LIMIT_RE.search(detail)
                if lim and up_body.get("max_tokens"):
                    capped = min(int(lim.group(1)), up_body["max_tokens"])
                    if capped < up_body["max_tokens"]:
                        up_body["max_tokens"] = capped
                        retry_payload = inject_user(json.dumps(up_body, ensure_ascii=False).encode("utf-8"), session, m["id"])
            if retry_payload is not None:
                _audit_pass({"action": "upstream_retry", "user": user, "protocol": protocol,
                             "model": req_body.get("model", ""), "status": e.code,
                             "max_tokens": capped,
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
                try:
                    upstream = _forward(retry_payload)
                except urllib.error.HTTPError as e2:
                    try:
                        detail = e2.read().decode("utf-8", "replace")[:500]
                    except Exception:
                        detail = ""
                    _audit_pass({"action": "upstream_error", "user": user, "protocol": protocol,
                                 "model": req_body.get("model", ""), "status": e2.code,
                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
                    return self._json(e2.code, {"error": {"message": "上游 %s: %s" % (e2.code, detail)}})
                except Exception as e2:
                    _audit_pass({"action": "upstream_error", "user": user, "protocol": protocol,
                                 "model": req_body.get("model", ""), "status": 500,
                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
                    return self._json(500, {"error": {"message": str(e2)}})
            else:
                _audit_pass({"action": "upstream_error", "user": user, "protocol": protocol,
                             "model": req_body.get("model", ""), "status": e.code,
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
                return self._json(e.code, {"error": {"message": "上游 %s: %s" % (e.code, detail)}})
        except Exception as e:
            _audit_pass({"action": "upstream_error", "user": user, "protocol": protocol,
                         "model": req_body.get("model", ""), "status": 500,
                         "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
            return self._json(500, {"error": {"message": str(e)}})

        if is_stream:
            # 放行审计：记录未拦截请求（流式不等待完成，先记一条）
            _audit_pass({"action": "pass", "user": user, "protocol": protocol,
                         "model": req_body.get("model", ""), "stream": True,
                         "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
            # 流式：上游始终是 OpenAI chat SSE。按客户端协议转换：
            #   openai    → 透传（边还原占位符）
            #   responses → Responses SSE 事件流（codex-cli 依赖 response.completed 等）
            #   anthropic → Anthropic Messages SSE 事件流（message_start/stop）
            self.send_response(upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() in ("transfer-encoding", "content-length", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            # 协议流状态：把 OpenAI chat 的 delta 增量转成目标协议 SSE 事件
            if protocol == "responses":
                st = _ResponsesStreamState(req_body.get("model", ""))
            elif protocol == "anthropic":
                st = _AnthropicStreamState(req_body.get("model", ""))
            else:
                st = None

            def _write_sse(ev, payload):
                line_out = ("event: %s\n" % ev if ev else "") + "data: %s\n\n" % payload
                b_out = line_out.encode("utf-8")
                self.wfile.write(b"%x\r\n" % len(b_out) + b_out + b"\r\n")
                self.wfile.flush()

            buf = b""
            data_parts = []
            cur_event = ""
            final_sent = False
            pend = {"content": "", "reasoning_content": ""}  # openai chat 透传的跨 chunk 还原缓冲

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
                if protocol == "responses":
                    evs = st.feed(obj)
                    for ev, payload in evs:
                        _write_sse(ev, payload)
                        if ev == "response.completed":
                            final_sent = True
                elif protocol == "anthropic":
                    evs = st.feed(obj)
                    for ev, payload in evs:
                        _write_sse(ev, payload)
                        if ev == "message_stop":
                            final_sent = True
                else:
                    # openai chat 透传：还原占位符 + 还原客户端名。
                    # 占位符可能被上游按 token 拆开，用 _stream_restore 跨 chunk 缓冲
                    # （与 responses/anthropic 一致），否则拆成两块的占位符无法还原。
                    obj = restore_body(obj)
                    obj = _unspoof_walk(obj)
                    for ch in (obj.get("choices") or []):
                        d = ch.get("delta") or {}
                        for key in ("content", "reasoning_content"):
                            if d.get(key):
                                restored, pend[key] = _stream_restore(d[key], pend[key])
                                d[key] = restored
                    _write_sse("", json.dumps(obj, ensure_ascii=False))
                    if obj.get("choices") and obj["choices"][0].get("finish_reason"):
                        _write_sse("", "[DONE]")
                        final_sent = True

            try:
                while True:
                    chunk = upstream.read(4096)
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
                # 上游流结束时补齐协议收尾事件
                if protocol == "responses" and not final_sent:
                    for ev, payload in st.finish():
                        _write_sse(ev, payload)
                elif protocol == "anthropic" and not final_sent:
                    for ev, payload in st.finish():
                        _write_sse(ev, payload)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                _log("[conn] 流式传输中客户端断开 (%s %s)" % (self.command, self.path))
                upstream.close()
            return

        # 7) 非流式：读完整响应，转换协议格式，再还原
        try:
            raw = upstream.read()
        except Exception as e:
            return self._json(502, {"error": {"message": "读取上游失败: %s" % e}})
        try:
            resp_obj = json.loads(raw.decode("utf-8"))
        except Exception:
            # 上游返回非 JSON（如空/HTML）→ 直接透传
            self.send_response(upstream.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)
            return
        try:
            resp_obj = restore_body(resp_obj)
            resp_obj = _unspoof_walk(resp_obj)   # 还原客户端名（Codex/Claude 等 → 原名）
            out = out_fn(resp_obj, req_body)
            # 放行审计：记录未拦截请求（含上游返回的 token 统计）
            usage = resp_obj.get("usage") or {}
            _audit_pass({"action": "pass", "user": user, "protocol": protocol,
                         "model": req_body.get("model", ""), "stream": False,
                         "prompt_tokens": usage.get("prompt_tokens"),
                         "completion_tokens": usage.get("completion_tokens"),
                         "total_tokens": usage.get("total_tokens"),
                         "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
            return self._json(200, out)
        except Exception as e:
            return self._json(502, {"error": {"message": "转换响应失败: %s" % e}})

    # ---- OpenAI Chat ----
    def _handle_openai_chat(self):
        def passthrough(upstream_obj, req_body):
            return upstream_obj  # OpenAI chat 响应原样返回
        return self._common_llm("openai", passthrough)

    # ---- OpenAI Responses ----
    def _handle_responses(self):
        def to_responses(upstream_obj, req_body):
            return convert_openai_to_responses(upstream_obj, req_body)
        return self._common_llm("responses", to_responses)

    # ---- Anthropic Messages ----
    def _handle_anthropic(self):
        def to_anthropic(upstream_obj, req_body):
            return convert_openai_to_anthropic(upstream_obj, req_body)
        return self._common_llm("anthropic", to_anthropic)


# ── Responses 请求 → OpenAI Chat 请求格式转换 ───────────────────────────────
def convert_responses_to_openai(body: dict) -> dict:
    """把 /v1/responses 请求体转成 OpenAI chat.completions 请求体。"""
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
    # 上游 litellm 要求 parallel_tool_calls 仅在显式声明 tools 时合法（否则 400）。
    # codex 不带 tools 也会发 parallel_tool_calls=false，此处无 tools 时丢弃该字段。
    if body.get("parallel_tool_calls") is not None and body.get("tools"):
        oa["parallel_tool_calls"] = body["parallel_tool_calls"]
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
                role = item.get("role") == "assistant" and "assistant" or "user"
                c = item.get("content")
                text = ""
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and (part.get("type") in ("input_text", "output_text", "text")) and part.get("text"):
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
    if msgs:
        oa["messages"] = msgs
    else:
        oa["messages"] = [{"role": "user", "content": ""}]

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


# ── Anthropic Messages 请求 → OpenAI Chat 请求格式转换 ──────────────────────
def convert_anthropic_to_openai(body: dict) -> dict:
    """把 /v1/messages 请求体转成 OpenAI chat.completions 请求体。"""
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
        # temperature 与 reasoning_effort 互斥（上游 gpt-5.6-luna 400：cannot specify both）
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
                    pass  # 思考内容不注入用户可见消息
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
            # tool_result 先拆出来
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result":
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
        else:
            text_parts = []
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    text_parts.append(part["text"])
            if text_parts:
                msgs.append({"role": role, "content": "".join(text_parts)})
    if msgs:
        oa["messages"] = msgs
    else:
        oa["messages"] = [{"role": "user", "content": ""}]

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


# ── OpenAI Chat 响应 → Responses 格式转换 ───────────────────────────────────
def convert_openai_to_responses(openai_obj: dict, req_body: dict) -> dict:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    output = []
    if message.get("reasoning_content"):
        output.append({
            "type": "reasoning",
            "id": "rs_" + _rand(),
            "summary": [{"type": "summary_text", "text": message["reasoning_content"]}],
        })
    if message.get("content"):
        output.append({
            "type": "message",
            "id": "msg_" + _rand(),
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": message["content"], "annotations": []}],
        })
    for tc in message.get("tool_calls") or []:
        output.append({
            "type": "function_call",
            "id": "fc_" + _rand(),
            "call_id": tc.get("id", ""),
            "name": (tc.get("function") or {}).get("name", ""),
            "arguments": (tc.get("function") or {}).get("arguments", "{}"),
            "status": "completed",
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
            "input_tokens_details": {"cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
            "output_tokens": usage.get("completion_tokens", 0),
            "output_tokens_details": {"reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)},
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "instructions": req_body.get("instructions"),
        "parallel_tool_calls": req_body.get("parallel_tool_calls"),
        "temperature": req_body.get("temperature"),
        "top_p": req_body.get("top_p"),
        "tool_choice": req_body.get("tool_choice"),
        "tools": req_body.get("tools"),
    }


# ── OpenAI Chat 响应 → Anthropic Messages 格式转换 ─────────────────────────
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
        content.append({
            "type": "tool_use",
            "id": tc.get("id", "toolu_" + _rand()),
            "name": (tc.get("function") or {}).get("name", ""),
            "input": input_obj,
        })
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


def _rand() -> str:
    """短随机 id 片段。"""
    return hashlib.md5(os.urandom(8)).hexdigest()[:24]


# ── 入口 ────────────────────────────────────────────────────────────────────
class QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """与 ThreadingHTTPServer 一致，但对客户端主动断开（ConnectionReset/BrokenPipe）
    不打印 traceback——这只说明客户端超时/取消，不是服务端错误。"""

    def handle_error(self, request, client_address):
        # 客户端主动断开 / 写盘被打断：不是服务端逻辑错误，只记一行日志，不打印 traceback。
        # 注意 process_request_thread 里 request 形参收到的是异常对象、连接对象在 client_address 形参里。
        conn = request if isinstance(request, socket.socket) else getattr(request, "socket", None)
        exc = request
        if isinstance(conn, socket.socket):
            try:
                code = conn.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            except Exception:
                code = None
            if code is not None and code != 0:
                _log("[conn] 客户端断开 (SO_ERROR=%r) %r" % (code, client_address))
                return
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)) or isinstance(conn, (BrokenPipeError, ConnectionResetError)):
            _log("[conn] 客户端断开 %r" % (client_address,))
            return
        http.server.ThreadingHTTPServer.handle_error(self, request, client_address)


def main():
    load_audit_cache()
    start_writer()
    if not os.path.exists(DB_FILE):
        print("[ws-proxy] 错误: 找不到 %s" % DB_FILE)
        print("[ws-proxy] 请先安装并登录 Workspace 编辑器，再运行本脚本")
        sys.exit(1)
    try:
        s = _read_session()
        print("[ws-proxy] 用户: %s, token 至 %s" % (
            s["username"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s["exp"])) + " (UTC)",
        ))
    except Exception as e:
        print("[ws-proxy] 警告: " + str(e))
    _log("=== ws-proxy 启动 ===")
    server = QuietThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("[ws-proxy] http://127.0.0.1:%d/v1  (OpenAI / Responses / Anthropic 兼容, Ctrl+C 退出)" % PORT)
    print("[ws-proxy] 拦截/脱敏审计: %s" % AUDIT_FILE)
    print("[ws-proxy] 放行请求审计: %s" % PASS_AUDIT_FILE)
    if LOG_FILE:
        print("[ws-proxy] 运行日志: %s" % LOG_FILE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("=== ws-proxy 退出 ===")
        shutdown_writer()
        print("\n[ws-proxy] 已退出")


if __name__ == "__main__":
    main()