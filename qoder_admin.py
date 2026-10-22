# -*- coding: utf-8 -*-
"""
qoder_admin.py — Qoder 双代理共享管理层（国内版 qoder_proxy.py / 国际版 qoder_intl_proxy.py 共用）

把 Cloudflare Worker 网关（qoder-cf-gw）的管理能力移植到 Python 本地代理：
  1. KeyStore      多 API Key 存储：JSON 文件持久化 + 原子写（tmp + os.replace，0600）、
                   create/patch/delete/reset_usage/list/lookup，字段对齐 CF createApiKey：
                   {id, name, key_value, key_hash, created_at, last_used_at,
                    enabled, max_calls, expires_at}
  2. check_admin   管理 Bearer 认证（secrets.compare_digest 恒时比对；密码未配置一律拒绝）
  3. AdminApi      框架无关的管理 API handler：handle(method, path, headers, body_bytes)
                   → (status, headers_dict, body_bytes) 纯函数式入口，
                   路由形态对齐 CF index.js handleAdmin（/api/keys|accounts|stats|models|
                   model-routes + /admin 单页），admin 页面 JS 无需大改
  4. admin_html()  管理后台单页（从 qoder-cf-gw src/admin.js 的 adminHtml() 移植，
                   静态常量 ADMIN_HTML）

给集成者（任务 17 / 18）的接入要点：
  * 零第三方依赖（纯标准库）；本模块不 import 任何代理模块，方向是代理 import 它。
  * AdminApi.handle 不依赖 http.server，BaseHTTPRequestHandler 里薄薄一层转接即可：

        admin = AdminApi(KeyStore(keys_path), ADMIN_PASSWORD, adapters={...})
        # do_GET/do_POST/do_PATCH/do_DELETE 里：
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        status, headers, payload = admin.handle(
            self.command, self.path, self.headers, body)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    headers 参数接受 dict 或 email.message.Message（Authorization 大小写不敏感查找）。
    返回的 headers 只含 Content-Type / Cache-Control，Content-Length 由转接层计算。
  * 数据面鉴权（替换原 _check_api_key 的单 Key 逻辑）：

        record, allowed = key_store.lookup(raw_key)
        if record is None:      # key 不存在 / 已禁用 / 已过期 → 401
            ...
        if not allowed:         # 当日调用超过 max_calls → 429
            ...

    lookup 命中且放行时当日计数 +1 并立即落盘；last_used_at 不逐请求回写（对齐 CF
    touchApiKey 的"免写额度"考量，本地写盘成本也省了）。
  * adapters 全部可选（dict[str, callable]），缺省路由返回 501：
        list_accounts() -> [公开账号视图]
        latest_checkins() -> [签到记录]            （GET /api/accounts 响应里的 checkins，
                                                     缺省 []，异常吞成 []）
        import_accounts(list) -> {imported, updated, skipped, ...}
        patch_account(id, fields) -> dict | None   （None → 404 账号不存在）
        delete_account(id) -> 任意                  （幂等，恒回 {ok: true}）
        test_account(id) -> dict | None             （None → 404；异常 → 500）
        refresh_account(id) -> dict | None
        checkin_account(id) -> dict | None
        stats() -> {today, trend, accounts, models, checkins, ...}
                                                   （AdminApi 会补 setdefault("keys", N)）
        list_models() -> [模型] | {models, updated_at}
        model_routes() -> {mappings, aliases}
        patch_model_routes(body) -> {mappings, aliases} | None
    POST /api/accounts 与 POST /api/accounts/import 等价（body 支持数组 / {accounts:[...]} /
    单对象，形态规整成 list 后交给 import_accounts）。
    /api/model-routes 同时接受 PUT（CF 页面 JS 用）与 PATCH。
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid

__all__ = [
    "KeyStore", "KeyStoreError", "check_admin", "AdminApi",
    "admin_html", "ADMIN_HTML",
]

# 按 Key 字段口径：sk- + 24 随机字节的小写 hex（48 字符），对齐 CF createApiKey。
_KEY_RANDOM_BYTES = 24


# ── 基础工具 ─────────────────────────────────────────────────────────────────

def _now():
    return int(time.time())


def _utc_date():
    """按天计数的日期键（UTC），对齐 CF utcDate()。"""
    return time.strftime("%Y-%m-%d", time.gmtime())


def _sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key_preview(value):
    """对齐 CF keyPreview：<=6 位 → 前3...；否则 前5...末1。"""
    key = str(value or "")
    if len(key) <= 6:
        return key[:3] + "..."
    return key[:5] + "..." + key[-1:]


def _read_json_file(path):
    """容错读 JSON：文件不存在 / 坏 JSON → None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _atomic_write_json(path, payload):
    """原子写 JSON：tmp 直接以 0600 创建（否则 umask 会把密钥文件降成 644），
    再 os.replace 覆盖。参照 qoder_proxy._save_pool_locked。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 没有 POSIX 权限位，跳过


def _get_header(headers, name):
    """从 dict / email.message.Message 取 header（大小写不敏感）。"""
    if not headers:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is not None:
        return str(value)
    low = str(name).lower()
    try:
        items = headers.items()
    except Exception:
        return ""
    for key, val in items:
        if str(key).lower() == low:
            return str(val)
    return ""


# ── Key 字段规范化（对齐 CF store.js）───────────────────────────────────────

def _normalize_expiry(value):
    """对齐 CF normalizeExpiry：空/0 → 0（永不过期）；数字 >1e12 视为毫秒；
    字符串支持数字与 ISO 日期（Date.parse 的近似子集）。"""
    if value is None or value == "" or value is False or value == 0:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
        if n > 10 ** 12:
            n //= 1000
        return n if n > 0 else 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        n = int(float(text))
        if n > 10 ** 12:
            n //= 1000
        return n if n > 0 else 0
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        n = int(dt.timestamp())
        return n if n > 0 else 0
    except ValueError:
        return 0


def _normalize_max_calls(value):
    """对齐 CF normalizeMaxCalls：非正数 / 坏值 → 0（不限）。"""
    if value is None or value == "" or isinstance(value, bool):
        return 0
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


_CUSTOM_KEY_RE = re.compile(r"^[\x21-\x7e]{8,200}$")


def _normalize_custom_key(value):
    """对齐 CF normalizeCustomKey：空 → 随机生成；否则必须 8-200 个可见 ASCII 且无空格
    （避免粘贴混入不可见字符导致客户端无法调用），非法抛 KeyStoreError(400)。"""
    if value is None:
        return ""
    key = str(value).strip()
    if not key:
        return ""
    if not _CUSTOM_KEY_RE.match(key):
        raise KeyStoreError(
            "自定义密钥需为 8-200 个可见 ASCII 字符，且不能包含空格", 400)
    return key


class KeyStoreError(ValueError):
    """KeyStore 操作错误：message + HTTP status（对齐 CF 的 e.status 语义；
    继承 ValueError 以兼容"查重抛 ValueError"的约定）。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ── 多 API Key 存储 ──────────────────────────────────────────────────────────

class KeyStore:
    """多 API Key 存储。

    主文件（构造传入的 path，格式 {"version":1,"keys":[...]}）存 Key 记录；
    同目录 usage 文件（默认 path + ".usage.json"，格式 {key_id:{date:count}}）存按天计数。
    两份文件都是原子写 + 0600；计数每次变更即落盘（本地量小，重启即恢复）。
    线程安全：所有公开方法持同一把 RLock。
    """

    def __init__(self, path, usage_path=None):
        self._path = os.path.abspath(path)
        self._usage_path = os.path.abspath(usage_path) if usage_path \
            else self._path + ".usage.json"
        self._lock = threading.RLock()
        self._records = []
        self._usage = {}
        with self._lock:
            self._load_locked()

    # ── 持久化 ──

    def _load_locked(self):
        data = _read_json_file(self._path)
        if isinstance(data, dict):
            keys = data.get("keys") or []
        elif isinstance(data, list):
            keys = data
        else:
            keys = []
        self._records = [k for k in keys
                         if isinstance(k, dict) and k.get("id") and k.get("key_hash")]
        for rec in self._records:
            rec.setdefault("name", "default")
            rec.setdefault("key_value", "")
            rec.setdefault("created_at", 0)
            rec.setdefault("last_used_at", 0)
            rec.setdefault("enabled", 1)
            rec.setdefault("max_calls", 0)
            rec.setdefault("expires_at", 0)
        usage = _read_json_file(self._usage_path)
        if isinstance(usage, dict):
            self._usage = {k: dict(v) for k, v in usage.items() if isinstance(v, dict)}
        else:
            self._usage = {}

    def _save_locked(self):
        _atomic_write_json(self._path, {"version": 1, "keys": self._records})

    def _usage_save_locked(self):
        _atomic_write_json(self._usage_path, self._usage)

    # ── 内部查找 ──

    def _by_id_locked(self, kid):
        for rec in self._records:
            if rec.get("id") == kid:
                return rec
        return None

    def _by_hash_locked(self, key_hash):
        for rec in self._records:
            if rec.get("key_hash") == key_hash:
                return rec
        return None

    def _used_today_locked(self, kid, today=None):
        return self._usage.get(kid, {}).get(today or _utc_date(), 0)

    # ── 公开视图（对齐 CF keyRow）──

    def _row_locked(self, record):
        today = _utc_date()
        used = self._used_today_locked(record["id"], today)
        now = _now()
        max_calls = int(record.get("max_calls") or 0)
        expires_at = int(record.get("expires_at") or 0)
        if expires_at > 0:
            remaining_days = 0 if expires_at <= now else int(
                (expires_at - now + 86399) // 86400)
        else:
            remaining_days = None
        return {
            "id": record["id"],
            "name": record["name"],
            "key_value": record.get("key_value") or "",
            "key": record.get("key_value") or "",
            "key_preview": _key_preview(record.get("key_value")),
            "created_at": int(record.get("created_at") or 0),
            "last_used_at": int(record.get("last_used_at") or 0),
            "max_calls": max_calls,
            "expires_at": expires_at,
            "enabled": 1 if record.get("enabled") else 0,
            "used_calls": used,
            "remaining_calls": max(0, max_calls - used) if max_calls > 0 else None,
            "remaining_days": remaining_days,
        }

    # ── CRUD ──

    def create(self, name="", opts=None):
        """创建 Key（对齐 CF createApiKey）。opts: {key, max_calls, expires_at}。
        返回公开视图；key_value 重复抛 KeyStoreError("该密钥值已被使用", 409)。"""
        opts = opts if isinstance(opts, dict) else {}
        custom = _normalize_custom_key(opts.get("key"))
        raw = custom or ("sk-" + secrets.token_hex(_KEY_RANDOM_BYTES))
        now = _now()
        key_hash = _sha256_hex(raw)
        record = {
            "id": str(uuid.uuid4()),
            "name": str(name or "default")[:80],
            "key_value": raw,
            "key_hash": key_hash,
            "created_at": now,
            "last_used_at": 0,
            "enabled": 1,
            "max_calls": _normalize_max_calls(opts.get("max_calls")),
            "expires_at": _normalize_expiry(opts.get("expires_at")),
        }
        with self._lock:
            if self._by_hash_locked(key_hash) is not None:
                raise KeyStoreError("该密钥值已被使用", 409)
            self._records.append(record)
            self._save_locked()
            return self._row_locked(record)

    def patch(self, kid, fields=None):
        """更新 Key（对齐 CF updateApiKey）。支持 name / enabled / expires_at /
        max_calls / remaining_calls（填"还能用几次"，换算总量=已用+余量；0=不限）/
        key_value 或 key（轮换热密钥，查重 409）。
        返回更新后的公开视图；id 不存在返回 None。"""
        fields = fields if isinstance(fields, dict) else {}
        with self._lock:
            record = self._by_id_locked(kid)
            if record is None:
                return None
            if "name" in fields:
                record["name"] = str(fields.get("name") or "default")[:80]
            if "expires_at" in fields:
                record["expires_at"] = _normalize_expiry(fields.get("expires_at"))
            if "enabled" in fields:
                record["enabled"] = 1 if fields.get("enabled") else 0
            if "remaining_calls" in fields:
                rem = _normalize_max_calls(fields.get("remaining_calls"))
                used = self._used_today_locked(kid)
                record["max_calls"] = (used + rem) if rem > 0 else 0
            elif "max_calls" in fields:
                record["max_calls"] = _normalize_max_calls(fields.get("max_calls"))
            if "key_value" in fields:
                rotated = _normalize_custom_key(fields.get("key_value"))
            else:
                rotated = _normalize_custom_key(fields.get("key"))
            if rotated:
                new_hash = _sha256_hex(rotated)
                other = self._by_hash_locked(new_hash)
                if other is not None and other.get("id") != kid:
                    raise KeyStoreError("该密钥值已被使用", 409)
                record["key_value"] = rotated
                record["key_hash"] = new_hash
            self._save_locked()
            return self._row_locked(record)

    def delete(self, kid):
        """删除 Key 及其计数。返回是否存在（True=删了，False=本来就没有）。"""
        with self._lock:
            if self._by_id_locked(kid) is None:
                return False
            self._records = [r for r in self._records if r.get("id") != kid]
            self._usage.pop(kid, None)
            self._save_locked()
            self._usage_save_locked()
            return True

    def reset_usage(self, kid):
        """重置已用次数（清空该 Key 的按天计数，对齐 CF DO /reset）。
        返回 {"ok": True, "used": 0}；id 不存在返回 None。"""
        with self._lock:
            if self._by_id_locked(kid) is None:
                return None
            self._usage.pop(kid, None)
            self._usage_save_locked()
            return {"ok": True, "used": 0}

    def list(self):
        """全部 Key 的公开视图（含当日调用数 used_calls）。"""
        with self._lock:
            return [self._row_locked(r) for r in self._records]

    def lookup(self, raw_key):
        """数据面鉴权入口：按明文查 Key。

        返回 (record_view, allowed)：
          record_view 为 None   → key 不存在 / 已禁用 / 已过期（401 语义）
          record_view 非 None 且 allowed=False → 当日调用超过 max_calls（429 语义，
          此时不计数）；allowed=True 时当日计数 +1 并立即落盘。
        """
        if not raw_key or not isinstance(raw_key, str):
            return (None, False)
        key_hash = _sha256_hex(raw_key)
        with self._lock:
            record = self._by_hash_locked(key_hash)
            if record is None:
                return (None, False)
            if not record.get("enabled"):
                return (None, False)
            expires_at = int(record.get("expires_at") or 0)
            if expires_at > 0 and expires_at <= _now():
                return (None, False)
            max_calls = int(record.get("max_calls") or 0)
            today = _utc_date()
            used = self._used_today_locked(record["id"], today)
            allowed = (max_calls <= 0) or (used < max_calls)
            if allowed:
                used += 1
                self._usage.setdefault(record["id"], {})[today] = used
                self._usage_save_locked()
            view = {
                "id": record["id"],
                "name": record["name"],
                "enabled": 1,
                "last_used_at": int(record.get("last_used_at") or 0),
                "max_calls": max_calls,
                "expires_at": expires_at,
                "key_value": record.get("key_value") or "",
                "used_calls": used,
            }
            return (view, allowed)


# ── admin 认证 ───────────────────────────────────────────────────────────────

def check_admin(auth_header, password):
    """管理 Bearer 认证：Authorization: Bearer <ADMIN_PASSWORD>。
    密码未配置（空）→ 一律 False（管理 API 全部 403，页面仍可看登录框）；
    比对用 secrets.compare_digest（恒时），防时序侧信道。"""
    if not password:
        return False
    if not auth_header:
        return False
    text = str(auth_header)
    if not text.startswith("Bearer "):
        return False
    supplied = text[7:]
    return secrets.compare_digest(
        supplied.encode("utf-8"), str(password).encode("utf-8"))


# ── 管理 API handler（框架无关）─────────────────────────────────────────────

_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8",
                 "Cache-Control": "no-store"}


def _resp_json(status, obj):
    return (status, dict(_JSON_HEADERS),
            json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _resp_html(html_text):
    return (200, {"Content-Type": "text/html; charset=utf-8",
                  "Cache-Control": "no-store"}, html_text.encode("utf-8"))


def _parse_json(body_bytes):
    """请求体 JSON 解析：空 body / 坏 JSON / 非 UTF-8 → None（调用方回 400）。"""
    if not body_bytes:
        return None
    try:
        return json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _err_not_impl(capability):
    return _resp_json(501, {"error": {"message":
                                      "此代理未提供该能力（%s adapter 未注入）" % capability}})


class AdminApi:
    """管理 API handler。构造：AdminApi(key_store, admin_password, adapters)。

    adapters 见模块 docstring；全部可选，缺省路由返回 501。
    handle(method, path, headers, body_bytes) -> (status, headers_dict, body_bytes)，
    纯函数式入口，不依赖 http.server（转接层示例见模块 docstring）。
    """

    def __init__(self, key_store, admin_password, adapters=None):
        self.key_store = key_store
        self.admin_password = str(admin_password) if admin_password else ""
        self.adapters = dict(adapters or {})

    # ── adapter 调用 ──

    def _adapter(self, name):
        fn = self.adapters.get(name)
        return fn if callable(fn) else None

    @staticmethod
    def _call(fn, *args):
        return fn(*args)

    # ── 入口 ──

    def handle(self, method, path, headers, body_bytes):
        """处理一条管理请求。KeyStoreError → 对应 status；其余异常 → 500。
        adapter 抛出的异常若带 int 型 .status 属性（如代理侧的 ProxyError(502, ...)），
        则沿用该 status（集成方表达"上游失败→502"这类语义），否则兜底 500。"""
        try:
            return self._route(method, path, headers, body_bytes)
        except KeyStoreError as e:
            return _resp_json(e.status, {"error": {"message": str(e)}})
        except Exception as e:  # adapter 内部错误兜底，别让管理面板 502 挂死
            status = getattr(e, "status", None)
            if not isinstance(status, int) or not (400 <= status <= 599):
                status = 500
            return _resp_json(status, {"error": {"message": "内部错误: %s" % e}})

    def _route(self, method, path, headers, body_bytes):
        method = str(method or "GET").upper()
        base = str(path or "/").split("?", 1)[0].rstrip("/") or "/"

        if base == "/admin":
            return _resp_html(admin_html())

        if base == "/api/auth/login" and method == "POST":
            return self._login(body_bytes)

        if not self.admin_password:
            return _resp_json(403, {"error": {
                "message": "管理密码未配置，管理接口已禁用"}})
        if not check_admin(_get_header(headers, "Authorization"),
                           self.admin_password):
            return _resp_json(401, {"error": {
                "message": "未授权：请用 Bearer <ADMIN_PASSWORD>"}})

        parts = [p for p in base.split("/") if p]
        if len(parts) < 2 or parts[0] != "api":
            return _resp_json(404, {"error": {"message": "Not found"}})
        section = parts[1]

        if section == "keys":
            return self._keys(method, parts, body_bytes)
        if section == "accounts":
            return self._accounts(method, parts, body_bytes)
        if section == "stats":
            return self._stats(method, parts)
        if section == "models":
            return self._models(method, parts)
        if section == "model-routes":
            return self._model_routes(method, parts, body_bytes)
        return _resp_json(404, {"error": {"message": "Not found"}})

    # ── 登录（页面 JS 直接 fetch，无需 Bearer）──

    def _login(self, body_bytes):
        body = _parse_json(body_bytes)
        if not isinstance(body, dict):
            return _resp_json(400, {"error": {"message": "请求体无效"}})
        if not self.admin_password:
            return _resp_json(403, {"error": {
                "message": "管理密码未配置，管理接口已禁用"}})
        pw = str(body.get("password") or "")
        if pw and secrets.compare_digest(
                pw.encode("utf-8"), self.admin_password.encode("utf-8")):
            return _resp_json(200, {"ok": True})
        return _resp_json(401, {"error": {"message": "密码错误"}})

    # ── /api/keys（本地实现，不走 adapters）──

    def _keys(self, method, parts, body_bytes):
        ks = self.key_store
        if len(parts) == 2:
            if method == "GET":
                return _resp_json(200, {"keys": ks.list()})
            if method == "POST":
                body = _parse_json(body_bytes)
                if not isinstance(body, dict):
                    return _resp_json(400, {"error": {"message": "请求体无效"}})
                return _resp_json(200, ks.create(body.get("name"), body))
            return _resp_json(404, {"error": {"message": "Not found"}})
        kid = parts[2] if len(parts) >= 3 else ""
        if not kid:
            return _resp_json(404, {"error": {"message": "Not found"}})
        if len(parts) == 3 and method == "PATCH":
            body = _parse_json(body_bytes)
            if not isinstance(body, dict):
                return _resp_json(400, {"error": {"message": "请求体无效"}})
            row = ks.patch(kid, body)
            if row is None:
                return _resp_json(404, {"error": {"message": "Key 不存在"}})
            return _resp_json(200, row)
        if len(parts) == 4 and parts[3] == "reset" and method == "POST":
            out = ks.reset_usage(kid)
            if out is None:
                return _resp_json(404, {"error": {"message": "Key 不存在"}})
            return _resp_json(200, out)
        if len(parts) == 3 and method == "DELETE":
            if not ks.delete(kid):
                return _resp_json(404, {"error": {"message": "Key 不存在"}})
            return _resp_json(200, {"ok": True})
        return _resp_json(404, {"error": {"message": "Not found"}})

    # ── /api/accounts（走 adapters）──

    def _accounts(self, method, parts, body_bytes):
        if len(parts) == 2:
            if method == "GET":
                fn = self._adapter("list_accounts")
                if fn is None:
                    return _err_not_impl("list_accounts")
                rows = self._call(fn)
                if not isinstance(rows, list):
                    rows = []
                checkins = []
                ck_fn = self._adapter("latest_checkins")
                if ck_fn is not None:
                    try:  # 签到记录是附属数据，失败不拖垮账号列表
                        got = self._call(ck_fn)
                        if isinstance(got, list):
                            checkins = got
                    except Exception:
                        checkins = []
                return _resp_json(200, {"accounts": rows, "checkins": checkins})
            if method == "POST":  # 与 /api/accounts/import 等价
                return self._import(body_bytes)
            return _resp_json(404, {"error": {"message": "Not found"}})
        if len(parts) == 3 and parts[2] == "import" and method == "POST":
            return self._import(body_bytes)
        aid = parts[2] if len(parts) >= 3 else ""
        if not aid:
            return _resp_json(404, {"error": {"message": "Not found"}})
        if len(parts) == 3 and method == "PATCH":
            body = _parse_json(body_bytes)
            if not isinstance(body, dict):
                return _resp_json(400, {"error": {"message": "请求体无效"}})
            fn = self._adapter("patch_account")
            if fn is None:
                return _err_not_impl("patch_account")
            out = self._call(fn, aid, body)
            if out is None:
                return _resp_json(404, {"error": {"message": "账号不存在"}})
            return _resp_json(200, out)
        if len(parts) == 3 and method == "DELETE":
            fn = self._adapter("delete_account")
            if fn is None:
                return _err_not_impl("delete_account")
            self._call(fn, aid)  # 幂等删除，对齐 CF
            return _resp_json(200, {"ok": True})
        if len(parts) == 4 and method == "POST" \
                and parts[3] in ("test", "refresh", "checkin"):
            fn = self._adapter(parts[3] + "_account")
            if fn is None:
                return _err_not_impl(parts[3] + "_account")
            out = self._call(fn, aid)
            if out is None:
                return _resp_json(404, {"error": {"message": "账号不存在"}})
            return _resp_json(200, out)
        return _resp_json(404, {"error": {"message": "Not found"}})

    def _import(self, body_bytes):
        body = _parse_json(body_bytes)
        if body is None:
            return _resp_json(400, {"error": {"message": "请求体无效"}})
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict) and isinstance(body.get("accounts"), list):
            items = body["accounts"]
        elif isinstance(body, dict):
            items = [body]
        else:
            items = []
        fn = self._adapter("import_accounts")
        if fn is None:
            return _err_not_impl("import_accounts")
        out = self._call(fn, items)
        if not isinstance(out, dict):
            out = {"ok": True}
        return _resp_json(200, out)

    # ── /api/stats、/api/models、/api/model-routes ──

    def _stats(self, method, parts):
        if method != "GET" or len(parts) != 2:
            return _resp_json(404, {"error": {"message": "Not found"}})
        fn = self._adapter("stats")
        if fn is None:
            return _err_not_impl("stats")
        out = self._call(fn)
        if not isinstance(out, dict):
            out = {}
        out.setdefault("keys", len(self.key_store.list()))
        return _resp_json(200, out)

    def _models(self, method, parts):
        if method != "GET" or len(parts) != 2:
            return _resp_json(404, {"error": {"message": "Not found"}})
        fn = self._adapter("list_models")
        if fn is None:
            return _err_not_impl("list_models")
        rows = self._call(fn)
        if isinstance(rows, dict):
            out = rows
            out.setdefault("models", [])
        else:
            out = {"models": rows if isinstance(rows, list) else []}
        out.setdefault("updated_at", None)
        return _resp_json(200, out)

    def _model_routes(self, method, parts, body_bytes):
        if len(parts) != 2:
            return _resp_json(404, {"error": {"message": "Not found"}})
        if method == "GET":
            fn = self._adapter("model_routes")
            if fn is None:
                return _err_not_impl("model_routes")
            routes = self._call(fn)
            mappings = routes.get("mappings") if isinstance(routes, dict) else None
            aliases = routes.get("aliases") if isinstance(routes, dict) else None
            return _resp_json(200, {
                "mappings": mappings if isinstance(mappings, dict) else {},
                "aliases": aliases if isinstance(aliases, dict) else {},
            })
        if method in ("PUT", "PATCH"):  # PUT 对齐 CF 页面 JS，PATCH 对齐任务规格
            body = _parse_json(body_bytes)
            if not isinstance(body, dict):
                return _resp_json(400, {"error": {"message": "请求体无效"}})
            fn = self._adapter("patch_model_routes")
            if fn is None:
                return _err_not_impl("patch_model_routes")
            result = self._call(fn, body)
            out = {"ok": True}
            if isinstance(result, dict):
                out.update(result)
            out.setdefault("mappings", body.get("mappings") or {})
            out.setdefault("aliases", body.get("aliases") or {})
            return _resp_json(200, out)
        return _resp_json(404, {"error": {"message": "Not found"}})


# ── adminHtml 单页 ───────────────────────────────────────────────────────────
# ADMIN_HTML 从 qoder-cf-gw src/admin.js 的 adminHtml() 模板字符串移植（模板无 ${}
# 插值，为完全静态内容）。移植时做的针对性调整见文件末尾常量前的注释。

def admin_html():
    """管理后台单页 HTML（静态内容，与 ADMIN_HTML 常量一致）。"""
    return ADMIN_HTML

# ADMIN_HTML：adminHtml() 的移植结果（原始字符串，浏览器侧 JS 里的 \' 转义
# 原样保留——renderAccounts 内联回调的 onclick 属性拼接需要它）。
# 移植调整：删 Google Fonts 外链（国内不可达且阻塞渲染）；标题 Qoder CF Gateway →
# Qoder Admin；KV / Durable Object / Cron 等 CF 特有文案改为本地口径。
# fetch 端点与 /api/keys|accounts|stats|models|model-routes 及 /api/auth/login
# 完全一致，页面 JS 逻辑零改动。
ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qoder Admin</title>
<script>
try{document.documentElement.setAttribute("data-theme",localStorage.getItem("qoder_theme")||"dark")}catch(e){}
</script>
<style>
:root{
  --bg:#0b0f19;--card-bg:rgba(30,41,59,.45);--line:rgba(255,255,255,.08);
  --text:#f8fafc;--dim:#94a3b8;
  --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 50%,#ec4899 100%);
  --accent:#a855f7;--input-bg:rgba(15,23,42,.6);--input-border:rgba(255,255,255,.1);
  --btn-bg:rgba(255,255,255,.06);--btn-hover:rgba(255,255,255,.12);
  --shadow:0 8px 32px 0 rgba(0,0,0,.3);--blur:20px;
  --sidebar-bg:rgba(15,23,42,.6);--sidebar-w:260px;--table-head:rgba(0,0,0,.2);
  --orb-1:rgba(99,102,241,.12);--orb-2:rgba(236,72,153,.08);
  --code-bg:rgba(0,0,0,.25);--code-color:#e9d5ff;--code-border:rgba(255,255,255,.04)
}
:root[data-theme="light"]{
  --bg:#f1f5f9;--card-bg:rgba(255,255,255,.7);--line:rgba(0,0,0,.06);
  --text:#0f172a;--dim:#64748b;
  --grad:linear-gradient(135deg,#4f46e5 0%,#9333ea 50%,#db2777 100%);
  --accent:#9333ea;--input-bg:rgba(241,245,249,.8);--input-border:rgba(0,0,0,.08);
  --btn-bg:rgba(0,0,0,.04);--btn-hover:rgba(0,0,0,.08);
  --shadow:0 8px 32px 0 rgba(31,38,135,.07);
  --sidebar-bg:rgba(255,255,255,.6);--table-head:rgba(0,0,0,.03);
  --orb-1:rgba(99,102,241,.06);--orb-2:rgba(236,72,153,.04);
  --code-bg:rgba(79,70,229,.07);--code-color:#4f46e5;--code-border:rgba(79,70,229,.15)
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(148,163,184,.25);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(148,163,184,.4)}
.hidden{display:none!important}.muted{color:var(--dim);font-size:12px}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.bg-orbs{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.orb{position:absolute;border-radius:50%;filter:blur(100px)}
.orb-1{top:-10%;left:-10%;width:50vw;height:50vw;background:var(--orb-1)}
.orb-2{bottom:-10%;right:-10%;width:60vw;height:60vw;background:var(--orb-2)}

/* Login */
.login-wrap{position:relative;z-index:1;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.login-card{width:100%;max-width:400px;padding:40px 36px;border-radius:22px;background:var(--card-bg);border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);text-align:center}
.login-logo{width:58px;height:58px;border-radius:17px;margin:0 auto 20px;background:var(--grad);display:flex;align-items:center;justify-content:center;font-family:'Outfit',sans-serif;font-size:27px;font-weight:700;color:#fff;box-shadow:0 10px 28px rgba(168,85,247,.35)}
.login-card h1{font-family:'Outfit',sans-serif;font-size:24px;font-weight:700;margin:0 0 8px;letter-spacing:-.02em}
.login-card p{margin:0;color:var(--dim);font-size:13px;line-height:1.6}
.login-card .login-fields{margin-top:24px;display:flex;flex-direction:column;gap:12px}
.login-card input{text-align:center;letter-spacing:.08em}
.login-card button{width:100%;padding:12px}

/* Shell */
.mobile-header{display:none}
.app-container{display:flex;min-height:100vh;position:relative;z-index:1}
aside{position:fixed;top:0;bottom:0;left:0;width:var(--sidebar-w);z-index:100;display:flex;flex-direction:column;padding:28px 18px;background:var(--sidebar-bg);border-right:1px solid var(--line);backdrop-filter:blur(var(--blur));-webkit-backdrop-filter:blur(var(--blur));transition:transform .3s cubic-bezier(.16,1,.36,1)}
.logo-area{display:flex;align-items:center;gap:12px;padding:0 8px;margin-bottom:34px}
.logo-icon{width:40px;height:40px;flex-shrink:0;border-radius:12px;background:var(--grad);display:flex;align-items:center;justify-content:center;font-family:'Outfit',sans-serif;font-size:20px;font-weight:700;color:#fff;box-shadow:0 6px 18px rgba(99,102,241,.3)}
.logo-text{font-family:'Outfit',sans-serif;font-size:18px;font-weight:700;letter-spacing:-.5px;background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}
.nav-menu{display:flex;flex-direction:column;gap:8px;flex:1}
.nav-item{display:flex;align-items:center;gap:12px;width:100%;padding:12px 16px;border:0;border-radius:10px;background:transparent;box-shadow:none;color:var(--dim);font-size:14px;font-weight:500;text-align:left;cursor:pointer;transition:all .25s cubic-bezier(.16,1,.36,1)}
.nav-item svg{width:18px;height:18px;flex-shrink:0}
.nav-item:hover{color:var(--text);background:var(--btn-bg);transform:translateX(4px);box-shadow:none}
.nav-item.active{color:#fff;background:var(--grad);box-shadow:0 0 18px rgba(168,85,247,.35),0 0 40px rgba(99,102,241,.15),inset 0 1px 0 rgba(255,255,255,.1)}
.nav-item.active:hover{transform:none}
.aside-footer{display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--line);padding-top:18px}
.aside-footer .ghost{width:100%;font-size:13px}
.aside-footer .ghost svg{width:16px;height:16px;flex-shrink:0}
main{flex:1;min-width:0;margin-left:var(--sidebar-w);padding:38px 40px 56px}
main>header{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:28px}
main>header h1{font-family:'Outfit',sans-serif;font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0}
main>header p{margin:5px 0 0;color:var(--dim);font-size:13px}
.status-pill{display:inline-flex;align-items:center;gap:7px;padding:6px 14px;border-radius:999px;font-size:12px;font-weight:600;color:#34d399;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.2);white-space:nowrap}
.status-pill::before{content:'';width:7px;height:7px;border-radius:50%;background:#10b981;box-shadow:0 0 8px rgba(16,185,129,.8)}

/* Cards */
.card{background:var(--card-bg);border:1px solid var(--line);border-radius:18px;padding:26px;margin-bottom:22px;box-shadow:var(--shadow);backdrop-filter:blur(var(--blur));-webkit-backdrop-filter:blur(var(--blur))}
.card-head{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.card-title{font-family:'Outfit',sans-serif;font-size:18px;font-weight:600;margin:0}
.hint{margin:8px 0 0;color:var(--dim);font-size:12.5px;line-height:1.6}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.grow{flex:1}

/* Stats */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px}
.stat-card{position:relative;overflow:hidden;padding:22px 24px;border-radius:18px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.015));box-shadow:var(--shadow);transition:transform .35s cubic-bezier(.16,1,.36,1),box-shadow .35s,border-color .35s}
:root[data-theme="light"] .stat-card{background:linear-gradient(180deg,rgba(255,255,255,.9),rgba(255,255,255,.6))}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--grad);opacity:0;transition:opacity .35s}
.stat-card:hover{transform:translateY(-4px);border-color:rgba(168,85,247,.35);box-shadow:0 16px 40px rgba(168,85,247,.12)}
.stat-card:hover::before{opacity:1}
.stat-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.stat-title{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.stat-icon{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;background:rgba(168,85,247,.12);color:var(--accent)}
.stat-icon svg{width:18px;height:18px}
.stat-value{font-family:'Outfit',sans-serif;font-size:34px;font-weight:700;letter-spacing:-.02em;line-height:1;margin-top:14px}
.stat-desc{font-size:12px;color:var(--dim);margin-top:7px}

/* Endpoint cards */
.endpoint-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:16px}
.endpoint-card{display:block;width:100%;text-align:left;padding:17px 19px;border-radius:16px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.015));box-shadow:none;color:inherit;cursor:pointer;transition:transform .25s cubic-bezier(.16,1,.36,1),border-color .25s,box-shadow .25s}
:root[data-theme="light"] .endpoint-card{background:linear-gradient(180deg,rgba(255,255,255,.9),rgba(248,250,252,.7))}
.endpoint-card:hover{transform:translateY(-2px);border-color:rgba(168,85,247,.28);box-shadow:0 12px 30px rgba(168,85,247,.08)}
.endpoint-badge{display:inline-flex;align-items:center;padding:4px 10px;border-radius:999px;background:rgba(168,85,247,.12);color:var(--accent);font-size:12px;font-weight:600}
.endpoint-url{display:block;margin-top:12px;font-family:ui-monospace,Consolas,monospace;font-size:13px;line-height:1.55;word-break:break-all;color:var(--text);text-decoration:underline;text-decoration-color:rgba(168,85,247,.5);text-underline-offset:3px}
.endpoint-card:hover .endpoint-url{color:var(--accent)}
.endpoint-hint{margin-top:8px;font-size:12px;color:var(--dim)}

/* Forms */
input,textarea,select{font:inherit;background:var(--input-bg);border:1px solid var(--input-border);color:var(--text);border-radius:10px;padding:11px 14px;width:100%;outline:none;transition:border-color .25s,box-shadow .25s;backdrop-filter:blur(10px)}
input:focus,textarea:focus,select:focus{border-color:rgba(168,85,247,.55);box-shadow:0 0 0 3px rgba(168,85,247,.12)}
input::placeholder,textarea::placeholder{color:var(--dim);opacity:.7}
textarea{min-height:130px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.65;resize:vertical}
input[type=file]{padding:8px 10px;font-size:13px;color:var(--dim)}
input[type=file]::file-selector-button{font:inherit;font-size:12.5px;font-weight:600;margin-right:10px;padding:6px 12px;border:0;border-radius:8px;background:var(--btn-bg);color:var(--text);cursor:pointer;transition:background .2s}
input[type=file]::file-selector-button:hover{background:var(--btn-hover)}

/* Buttons */
button{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 18px;border:0;border-radius:10px;background:var(--grad);color:#fff;font-size:14px;font-weight:600;cursor:pointer;transition:all .3s cubic-bezier(.16,1,.36,1);box-shadow:0 4px 14px rgba(168,85,247,.25)}
button:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(168,85,247,.4);filter:brightness(1.05)}
button:active{transform:translateY(0)}
button.ghost{background:var(--btn-bg);color:var(--text);border:1px solid var(--line);box-shadow:none;font-weight:500}
button.ghost:hover{background:var(--btn-hover);box-shadow:none;transform:translateY(-1px);filter:none}
button.danger{background:rgba(239,68,68,.13);color:#fca5a5;border:1px solid rgba(239,68,68,.25);box-shadow:none;font-weight:500}
:root[data-theme="light"] button.danger{color:#dc2626}
button.danger:hover{background:rgba(239,68,68,.22);transform:translateY(-1px);box-shadow:none;filter:none}
button:disabled{opacity:.5;cursor:not-allowed;transform:none!important;box-shadow:none!important;filter:none!important}
td .row{flex-wrap:nowrap}
td .row button{padding:6px 11px;font-size:12.5px;border-radius:8px;box-shadow:none}
td .row button:hover{transform:none}

/* Badges */
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;background:rgba(148,163,184,.12);color:var(--dim);border:1px solid transparent;white-space:nowrap}
.badge.ok{background:rgba(16,185,129,.1);color:#34d399;border-color:rgba(16,185,129,.2)}
.badge.bad{background:rgba(239,68,68,.1);color:#f87171;border-color:rgba(239,68,68,.2)}
.badge.warn{background:rgba(245,158,11,.1);color:#fbbf24;border-color:rgba(245,158,11,.2)}
:root[data-theme="light"] .badge.ok{color:#059669}
:root[data-theme="light"] .badge.bad{color:#dc2626}
:root[data-theme="light"] .badge.warn{color:#d97706}

/* Tables */
.table-wrap{overflow:auto;margin:0 -6px;padding:0 6px}
table{width:100%;border-collapse:collapse;font-size:14px;text-align:left}
th{background:var(--table-head);color:var(--dim);font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:13px 16px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:15px 16px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr{transition:background .2s}
tbody tr:hover{background:rgba(168,85,247,.045)}
tbody tr:last-child td{border-bottom:0}
code,.key-value{font-family:ui-monospace,Consolas,monospace;background:var(--code-bg);border:1px solid var(--code-border);color:var(--code-color);border-radius:8px;padding:8px 12px;font-size:12.5px;word-break:break-all;display:block;cursor:pointer;transition:border-color .2s}
code:hover,.key-value:hover{border-color:rgba(168,85,247,.35)}

/* Route target chips */
.target-cloud{display:flex;flex-wrap:wrap;gap:8px}
.target-cloud .target-chip{display:inline-block;width:auto;padding:6px 10px;font-size:12px}

/* Modal */
.modal-wrap{position:fixed;inset:0;z-index:900;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(3,7,18,.62);backdrop-filter:blur(6px)}
:root[data-theme="light"] .modal-wrap{background:rgba(15,23,42,.35)}
.modal{width:100%;max-width:520px;padding:26px 24px;border-radius:18px;background:var(--card-bg);border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(var(--blur));-webkit-backdrop-filter:blur(var(--blur));max-height:88vh;overflow:auto}
.modal-title{font-family:'Outfit',sans-serif;font-size:19px;font-weight:600;margin:0 0 18px}
.field{display:block;margin-bottom:16px}
.field>span{display:block;margin-bottom:7px;font-size:12.5px;font-weight:600;color:var(--dim)}
.check{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--text);cursor:pointer}
.check input{width:auto}
.modal-actions{margin-top:8px;justify-content:flex-end}
.modal .row .muted{font-size:12px}

/* Toast */
#toast{position:fixed;top:24px;right:24px;z-index:9999;max-width:min(420px,calc(100vw - 32px));padding:14px 20px;border-radius:12px;background:rgba(30,41,59,.94);border:1px solid rgba(168,85,247,.35);box-shadow:var(--shadow);backdrop-filter:blur(18px);color:var(--text);font-size:14px;font-weight:500;opacity:0;transform:translateY(-14px);transition:all .35s cubic-bezier(.16,1,.36,1);pointer-events:none}
:root[data-theme="light"] #toast{background:rgba(255,255,255,.96)}
#toast.show{opacity:1;transform:translateY(0)}
#toast.error{border-color:rgba(239,68,68,.55);background:rgba(127,29,29,.92)}
:root[data-theme="light"] #toast.error{background:rgba(254,226,226,.97)}
.sidebar-overlay{display:none}

@media (max-width:960px){
  .mobile-header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 16px;position:sticky;top:0;z-index:120;background:var(--sidebar-bg);border-bottom:1px solid var(--line);backdrop-filter:blur(var(--blur))}
  .mobile-header .logo-area{margin:0}
  .mobile-header .logo-icon{width:34px;height:34px;border-radius:10px;font-size:16px}
  .mobile-header .logo-text{font-size:16px}
  .menu-btn{padding:8px 14px;font-size:13px}
  aside{transform:translateX(-100%)}
  aside.open{transform:translateX(0);box-shadow:0 0 60px rgba(0,0,0,.5)}
  main{margin-left:0;padding:24px 18px 48px}
  main>header{flex-direction:column;align-items:flex-start;gap:12px}
  .sidebar-overlay{display:block;position:fixed;inset:0;z-index:90;background:rgba(3,7,18,.55);opacity:0;pointer-events:none;transition:opacity .3s}
  .sidebar-overlay.show{opacity:1;pointer-events:auto}
  .card{padding:20px;border-radius:16px}
  .login-card{padding:32px 24px}
}
</style>
</head>
<body>
<div class="bg-orbs"><div class="orb orb-1"></div><div class="orb orb-2"></div></div>

<div id="login" class="login-wrap hidden">
  <div class="login-card">
    <div class="login-logo">Q</div>
    <h1>Qoder Admin</h1>
    <p>输入管理密码以进入控制台</p>
    <div class="login-fields">
      <input id="pw" type="password" placeholder="管理密码" autofocus onkeydown="if(event.key==='Enter')login()">
      <button onclick="login()">登录</button>
    </div>
  </div>
</div>

<div id="app" class="hidden">
  <div class="mobile-header">
    <div class="logo-area"><div class="logo-icon">Q</div><span class="logo-text">Qoder Gateway</span></div>
    <button class="ghost menu-btn" onclick="toggleSidebar()">菜单</button>
  </div>
  <div class="app-container">
    <aside id="sidebar">
      <div class="logo-area"><div class="logo-icon">Q</div><span class="logo-text">Qoder Gateway</span></div>
      <nav class="nav-menu">
        <button class="nav-item active" data-tab="overview" onclick="tab('overview')">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
          概览
        </button>
        <button class="nav-item" data-tab="accounts" onclick="tab('accounts')">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
          账号池
        </button>
        <button class="nav-item" data-tab="keys" onclick="tab('keys')">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
          API Key
        </button>
        <button class="nav-item" data-tab="models" onclick="tab('models')">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
          模型
        </button>
        <button class="nav-item" data-tab="routes" onclick="tab('routes')">
          <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 13H4m0 0l4 4m-4-4l4-4"/></svg>
          模型映射
        </button>
      </nav>
      <div class="aside-footer">
        <button class="ghost" onclick="toggleTheme()" id="themeBtn">
          <svg class="theme-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>
          <span>切换主题</span>
        </button>
        <button class="ghost" onclick="logout()">退出登录</button>
      </div>
    </aside>
    <div class="sidebar-overlay" id="sidebarOverlay" onclick="closeSidebar()"></div>
    <main>
      <header>
        <div><h1 id="viewTitle">概览</h1><p id="viewSubtitle">账号池、调用量与每日签到</p></div>
        <span class="status-pill">系统正常运行</span>
      </header>

      <section id="tab-overview">
        <div class="card"><div class="card-head"><h3 class="card-title">今日</h3></div><div id="stats" class="grid"></div></div>
        <div class="card">
          <div class="card-head"><h3 class="card-title">接入信息</h3></div>
          <div class="endpoint-grid">
            <button class="endpoint-card" onclick="copyText(this.querySelector('.endpoint-url').textContent)">
              <span class="endpoint-badge">OpenAI</span>
              <span class="endpoint-url" data-path="/v1/chat/completions"></span>
              <span class="endpoint-hint">Chat Completions，支持流式与非流式</span>
            </button>
            <button class="endpoint-card" onclick="copyText(this.querySelector('.endpoint-url').textContent)">
              <span class="endpoint-badge">Anthropic</span>
              <span class="endpoint-url" data-path="/v1/messages"></span>
              <span class="endpoint-hint">Messages API，支持流式与非流式</span>
            </button>
            <button class="endpoint-card" onclick="copyText(this.querySelector('.endpoint-url').textContent)">
              <span class="endpoint-badge">Responses</span>
              <span class="endpoint-url" data-path="/v1/responses"></span>
              <span class="endpoint-hint">OpenAI Responses，支持流式与非流式</span>
            </button>
          </div>
          <p class="hint">鉴权：<code style="display:inline;padding:2px 7px;cursor:default">Authorization: Bearer &lt;API Key&gt;</code> 或 <code style="display:inline;padding:2px 7px;cursor:default">x-api-key</code></p>
        </div>
        <div class="card"><div class="card-head"><h3 class="card-title">今日签到</h3></div><div id="checkins" class="table-wrap"></div></div>
      </section>

      <section id="tab-accounts" class="hidden">
        <div class="card">
          <div class="card-head"><h3 class="card-title">导入账号</h3></div>
          <p class="hint" style="margin-top:0">支持 Python 版 qoder_intl_accounts.json 原样粘贴，或单个账号对象 / 数组。token 不会回显。每号自己的 machineId/machineToken 会一起带进来（Python 侧已默认每个账号一套设备身份）。</p>
          <textarea id="importText" placeholder='{"accounts":[{"token":"...","refreshToken":"...","uid":"...","machineId":"..."}]}'></textarea>
          <div class="row" style="margin-top:12px"><input id="importFile" type="file" accept=".json,application/json" style="max-width:330px" onchange="readFile()"><button onclick="importAccounts()">导入</button></div>
        </div>
        <div class="card">
          <div class="card-head"><h3 class="card-title grow">账号列表</h3><button class="ghost" onclick="loadAccounts()">刷新</button></div>
          <div class="table-wrap"><table><thead><tr><th>账号</th><th>状态</th><th>有效期</th><th>设备身份</th><th>今日签到</th><th>操作</th></tr></thead><tbody id="accounts"></tbody></table></div>
          <div id="deviceWarn"></div>
        </div>
      </section>

      <section id="tab-keys" class="hidden">
        <div id="no-key-warning" class="card hidden" style="border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.08)">
          <p class="hint" style="margin:0;color:#fbbf24">当前没有可用的 API Key：网关对所有请求放开。请创建一把 Key 后再对外提供。</p>
        </div>
        <div class="card">
          <div class="card-head"><h3 class="card-title grow">API 密钥</h3><button class="ghost" onclick="loadKeys()">刷新</button><button onclick="openAddKeyModal()">生成新密钥</button></div>
          <p class="hint" style="margin-top:0">密钥明文保存在本地 JSON 文件，可随时在列表中复制。次数上限由本地按天计数器校验，并发不超卖；改动立即生效。</p>
          <div class="table-wrap"><table><thead><tr><th>名称</th><th>API Key</th><th>有效期</th><th>剩余次数</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="keys"></tbody></table></div>
        </div>
      </section>

      <div id="key-modal" class="modal-wrap hidden">
        <div class="modal">
          <h3 id="key-modal-title" class="modal-title">生成新 API 密钥</h3>
          <div id="key-modal-form">
            <label class="field"><span>名称</span><input id="key-name" placeholder="例如：本地 Claude Code"></label>
            <label id="key-val-field" class="field"><span>API 密钥值（留空则随机生成 sk- 开头）</span><input id="key-val" autocomplete="off" spellcheck="false" placeholder="留空自动生成"></label>
            <div class="field">
              <span>有效期</span>
              <div class="row">
                <label class="check"><input id="key-forever" type="checkbox" checked onchange="onKeyForeverChange()"><span>永久</span></label>
                <input id="key-days" type="number" min="0.01" step="0.01" placeholder="天数" disabled oninput="onKeyDaysChange()" style="max-width:130px"><span class="muted">天</span>
              </div>
              <p id="key-exp-hint" class="hint"></p>
            </div>
            <label class="field"><span id="key-calls-label">最大调用次数（留空=不限）</span><input id="key-calls" type="number" min="0" step="1" placeholder="不限"></label>
            <p id="key-modal-error" class="hint" style="color:#f87171"></p>
            <div class="row modal-actions">
              <button class="ghost" onclick="closeKeyModal()">取消</button>
              <button id="key-save-btn" onclick="saveKey()">生成密钥</button>
            </div>
          </div>
          <div id="generated-key-holder" class="hidden"><div class="key-value" id="generated-key-val" onclick="copyKeyText(this.textContent)"></div></div>
        </div>
      </div>

      <section id="tab-models" class="hidden">
        <div class="card">
          <div class="card-head"><h3 class="card-title grow">上游模型清单</h3><span id="modelsUpdated" class="muted"></span><button class="ghost" onclick="loadModels(true)">强制刷新</button></div>
          <p class="hint" style="margin-top:0">清单持久化在本地，强制刷新会合并所有账号的可见模型；刷新页面不会丢失。</p>
          <div id="models" class="table-wrap" style="margin-top:14px"></div>
        </div>
      </section>

      <section id="tab-routes" class="hidden">
        <div class="card">
          <div class="card-head"><h3 class="card-title grow">模型映射</h3><button class="ghost" onclick="addRouteRow('map')">新增一行</button><button onclick="saveRoutes()">保存</button></div>
          <p class="hint" style="margin-top:0">请求里出现的模型名 → 上游模型 key。映射优先于假名，也优先于上游原名匹配；用于把外部客户端写死的模型名指向 Qoder 上游模型。</p>
          <div class="table-wrap"><table><thead><tr><th>请求模型名</th><th>目标上游模型</th><th></th></tr></thead><tbody id="route-map"></tbody></table></div>
        </div>
        <div class="card">
          <div class="card-head"><h3 class="card-title grow">假名（别名）</h3><button class="ghost" onclick="addRouteRow('alias')">新增一行</button><button onclick="saveRoutes()">保存</button></div>
          <p class="hint" style="margin-top:0">给上游模型起对外名字（假名不能与上游模型重名，重名请改用上面的模型映射）。</p>
          <div class="table-wrap"><table><thead><tr><th>假名</th><th>目标上游模型</th><th></th></tr></thead><tbody id="route-alias"></tbody></table></div>
        </div>
        <div class="card">
          <div class="card-head"><h3 class="card-title">可选目标模型</h3><button class="ghost" onclick="loadModels(true)">强制刷新清单</button></div>
          <p class="hint" style="margin-top:0">保存时后端会校验目标必须存在于清单；清单为空请先强制刷新。点击可复制模型 key。</p>
          <div id="route-targets" class="target-cloud"></div>
        </div>
      </section>
    </main>
  </div>
</div>
<div id="toast"></div>
<script>
var KEY_STORE = "qoder_cf_admin_pw";
var CURRENT_TAB = "overview";
var CHECKINS = {};
var DEVICES = {};
var ACCOUNTS = [];
var KEYS = [];
var MODELS = [];
var ROUTES = { mappings: {}, aliases: {} };
var TAB_META = {
  overview: {title:"概览", subtitle:"账号池、调用量与每日签到"},
  accounts: {title:"账号池", subtitle:"导入、续期与签到管理"},
  keys: {title:"API Key", subtitle:"调用上限与到期控制"},
  models: {title:"模型", subtitle:"上游模型清单与上下文能力"},
  routes: {title:"模型映射", subtitle:"请求模型名 → 上游模型，映射与假名"}
};
var ICONS = {
  accounts: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>',
  requests: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12h4l3-8 4 16 3-8h4"/></svg>',
  tokens: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>',
  models: '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>'
};

function el(id){return document.getElementById(id)}
function esc(v){return String(v==null?"":v).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function toast(msg,isErr){var t=el("toast");t.textContent=msg;t.classList.toggle("error",!!isErr);t.classList.add("show");clearTimeout(toast._t);toast._t=setTimeout(function(){t.classList.remove("show")},3000)}
function copyText(text,okMsg,failMsg){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(function(){toast(okMsg||"已复制")}).catch(function(){_copyFallback(text,okMsg,failMsg)})}else{_copyFallback(text,okMsg,failMsg)}}
function _copyFallback(text,okMsg,failMsg){try{var ta=document.createElement("textarea");ta.value=text;ta.style.position="fixed";ta.style.left="-9999px";document.body.appendChild(ta);ta.focus();ta.select();var ok=document.execCommand("copy");document.body.removeChild(ta);if(ok)toast(okMsg||"已复制");else toast(failMsg||"复制失败",true)}catch(e){toast(failMsg||"复制失败",true)}}
function copyKeyText(val){copyText(val,"API Key 复制成功！","复制失败，请手动复制")}
function keyMask(v){var s=String(v||"");if(!s)return "";return s.length<=6?s.slice(0,3)+"...":s.slice(0,5)+"..."+s.slice(-1)}
function fmtTime(v){if(!v)return "-";if(typeof v==="number"){return new Date(v*1000).toLocaleString()}var d=new Date(v);return isNaN(d.getTime())?String(v):d.toLocaleString()}
function fmtNum(v){return Number(v||0).toLocaleString()}
function fmtExp(a){
  if(!a.exp) return '<span class="badge bad">未知</span>';
  var left = Math.floor((a.exp - Date.now()/1000)/86400);
  if(left < 0) return '<span class="badge bad">已过期</span>';
  if(left < 3) return '<span class="badge bad">剩 '+left+' 天</span>';
  return '<span class="badge ok">剩 '+left+' 天</span>';
}
function auth(){return {"Authorization":"Bearer "+(sessionStorage.getItem(KEY_STORE)||"")}}
function api(path,opts){
  opts=opts||{};
  opts.headers=Object.assign({},auth(),opts.headers||{});
  if(opts.body && typeof opts.body!=="string"){opts.headers["Content-Type"]="application/json";opts.body=JSON.stringify(opts.body)}
  return fetch(path,opts).then(function(r){return r.json().catch(function(){return {}}).then(function(j){if(!r.ok)throw new Error(j.error&&j.error.message||j.message||("HTTP "+r.status));return j})})
}
function login(){
  var pw=el("pw").value.trim();if(!pw)return;
  fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:pw})}).then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error&&j.error.message||"登录失败");return j})}).then(function(){
    sessionStorage.setItem(KEY_STORE,pw);showApp();toast("已登录")
  }).catch(function(e){toast(e.message)})
}
function logout(){sessionStorage.removeItem(KEY_STORE);location.reload()}
function applyTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  try{localStorage.setItem("qoder_theme",t)}catch(e){}
  var b=el("themeBtn");
  if(b&&b.querySelector("span"))b.querySelector("span").textContent=t==="light"?"切换夜间":"切换日间";
}
function toggleTheme(){applyTheme(document.documentElement.getAttribute("data-theme")==="light"?"dark":"light")}
function toggleSidebar(){var s=el("sidebar"),o=el("sidebarOverlay");s.classList.toggle("open");o.classList.toggle("show")}
function closeSidebar(){var s=el("sidebar"),o=el("sidebarOverlay");if(s)s.classList.remove("open");if(o)o.classList.remove("show")}
function fillEndpoints(){
  if(typeof location==="undefined")return;
  document.querySelectorAll("[data-path]").forEach(function(n){n.textContent=location.origin+n.getAttribute("data-path")})
}
function showApp(){
  el("login").classList.add("hidden");el("app").classList.remove("hidden");
  applyTheme(document.documentElement.getAttribute("data-theme")||"dark");
  fillEndpoints();loadOverview();loadAccounts();loadKeys();
}
function tab(name){
  CURRENT_TAB=name;
  ["overview","accounts","keys","models","routes"].forEach(function(n){el("tab-"+n).classList.toggle("hidden",n!==name)});
  document.querySelectorAll(".nav-item").forEach(function(b){b.classList.toggle("active",b.getAttribute("data-tab")===name)});
  var meta=TAB_META[name]||{};
  if(el("viewTitle"))el("viewTitle").textContent=meta.title||"";
  if(el("viewSubtitle"))el("viewSubtitle").textContent=meta.subtitle||"";
  closeSidebar();
  if(name==="overview")loadOverview();
  if(name==="accounts")loadAccounts();
  if(name==="keys")loadKeys();
  if(name==="models")loadModels(false);
  if(name==="routes"){loadModels(false);loadRoutes()}
}

function statCard(title,value,desc,icon){
  return '<div class="stat-card"><div class="stat-top"><span class="stat-title">'+title+'</span><span class="stat-icon">'+icon+'</span></div><div class="stat-value">'+esc(value)+'</div><div class="stat-desc">'+desc+'</div></div>'
}
function loadOverview(){
  api("/api/stats").then(function(s){
    var t=s.today||{};
    el("stats").innerHTML=
      statCard("账号",s.accounts,"可用账号数",ICONS.accounts)+
      statCard("今日请求",fmtNum(t.requests||0),"UTC 今日累计",ICONS.requests)+
      statCard("今日 Tokens",fmtNum(t.total_tokens||0),"输入与输出合计",ICONS.tokens)+
      statCard("模型缓存",fmtNum(s.models),"上游可见模型",ICONS.models);
    CHECKINS={};(s.checkins||[]).forEach(function(c){CHECKINS[c.account_id]=c});
    var rows=(s.checkins||[]);
    el("checkins").innerHTML=rows.length?('<table><thead><tr><th>账号</th><th>状态</th><th>Credits</th><th>额度桶</th><th>说明</th><th>时间</th></tr></thead><tbody>'+rows.map(function(c){
      return '<tr><td>'+esc(c.label||c.account_id)+'</td><td>'+(c.secured?'<span class="badge ok">已到账</span>':'<span class="badge bad">'+(c.status==="ok"?"未到账":"失败")+'</span>')+'</td><td>'+esc(c.amount||0)+'</td><td>'+esc(c.add_on_total==null?"-":c.add_on_total)+'</td><td class="muted">'+esc(c.message||"-")+'</td><td class="muted">'+esc(fmtTime(c.updated_at))+'</td></tr>'
    }).join("")+'</tbody></table>'):'<p class="muted">今天还没有签到记录。签到由代理内嵌调度自动执行。若出现 SAME_PERSON_ALREADY_CLAIMED：上游判定同一身份今日已领而拒发（本机桌面端换号可领、代理连发被拒，触发条件待查；判定按账号+活动缓存，当天重试只重放）。</p>';
    renderAccounts();
  }).catch(function(e){toast(e.message)})
}

function loadAccounts(){
  api("/api/accounts").then(function(r){ACCOUNTS=r.accounts||[];(r.checkins||[]).forEach(function(c){CHECKINS[c.account_id]=c});renderAccounts()}).catch(function(e){toast(e.message)})
}
function renderAccounts(){
  if(!el("accounts"))return;
  if(!ACCOUNTS.length){el("accounts").innerHTML='<tr><td colspan="6" class="muted">账号池为空</td></tr>';return}
  DEVICES={};ACCOUNTS.forEach(function(a){if(a.machine_id)DEVICES[a.machine_id]=(DEVICES[a.machine_id]||0)+1});
  var dupes=Object.keys(DEVICES).filter(function(k){return DEVICES[k]>1});
  if(el("deviceWarn"))el("deviceWarn").innerHTML=dupes.length
    ?'<div class="hint">有 '+dupes.length+' 套设备身份被 '+ACCOUNTS.length+' 个号中的多个共用：同一台机器导进来的号会自动各发一套，共用只会出现在你显式沿用了本机指纹时。</div>'
    :'';
  el("accounts").innerHTML=ACCOUNTS.map(function(a){
    var ck=CHECKINS[a.id];
    var mid=a.machine_id||"";
    var device=mid?('<span class="mono">'+esc(mid.slice(0,8))+'</span> '+(DEVICES[mid]>1
      ?'<span class="badge bad">共用</span>':'<span class="badge ok">独立</span>'))
      :'<span class="badge">未设置</span>';
    var status=a.refresh_dead?'<span class="badge bad">续期链失效</span>':(a.failed_at&&(Date.now()/1000-a.failed_at)<300?'<span class="badge bad">冷却中 '+esc(a.failed_status||"")+'</span>':'<span class="badge ok">可用</span>');
    var checkin=ck?(ck.secured?'<span class="badge ok">已领 '+esc(ck.amount||100)+'</span>':'<span class="badge bad">'+esc(ck.message||ck.status||"未到账")+'</span>'):'<span class="badge">未执行</span>';
    var realmTag=a.realm==='intl'?'<span class="badge" style="background:#6366f1;color:#fff">国际</span>':'<span class="badge" style="background:#10b981;color:#fff">国内</span>';
    return '<tr><td><b>'+esc(a.label)+'</b> '+realmTag+'<div class="muted mono">'+esc(a.uid||"-")+'</div></td>'+
      '<td>'+status+'<div class="muted">'+esc(a.has_refresh?"可续期":"无 refresh")+'</div></td>'+
      '<td>'+fmtExp(a)+'<div class="muted">'+esc(a.expires_at||"-")+'</div></td>'+
      '<td>'+device+'</td>'+
      '<td>'+checkin+'</td>'+
      '<td><div class="row"><button class="ghost" onclick="testAccount(\''+a.id+'\')">测试</button>'+
      '<button class="ghost" onclick="refreshAccount(\''+a.id+'\')">续期</button>'+
      '<button class="ghost" onclick="checkinAccount(\''+a.id+'\')">签到</button>'+
      '<button class="danger" onclick="deleteAccount(\''+a.id+'\',\''+esc(a.label).replace(/'/g,"")+'\')">删除</button></div></td></tr>'
  }).join("")
}
function readFile(){var f=el("importFile").files[0];if(!f)return;var r=new FileReader();r.onload=function(){el("importText").value=r.result};r.readAsText(f)}
function importAccounts(){
  var text=el("importText").value.trim();if(!text)return toast("没有内容");
  var data;try{data=JSON.parse(text)}catch(e){return toast("JSON 解析失败: "+e.message)}
  api("/api/accounts/import",{method:"POST",body:data}).then(function(r){toast("导入 "+r.imported+"，更新 "+r.updated+"，跳过 "+r.skipped);el("importText").value="";loadAccounts()}).catch(function(e){toast(e.message)})
}
function testAccount(id){api("/api/accounts/"+id+"/test",{method:"POST"}).then(function(r){toast("测试通过，模型 "+r.models+" 个")}).catch(function(e){toast("测试失败: "+e.message)})}
function refreshAccount(id){api("/api/accounts/"+id+"/refresh",{method:"POST"}).then(function(r){toast("续期成功");loadAccounts()}).catch(function(e){toast("续期失败: "+e.message)})}
function checkinAccount(id){api("/api/accounts/"+id+"/checkin",{method:"POST"}).then(function(r){var rep=r.rep||{};toast(rep.secured?"今日 100 Credits 已到账":(rep.message||"签到已执行，但今日额度未到账"));loadOverview()}).catch(function(e){toast("签到失败: "+e.message)})}
function deleteAccount(id,label){if(!confirm("删除账号 "+(label||"")+" ?"))return;api("/api/accounts/"+id,{method:"DELETE"}).then(function(){toast("已删除");loadAccounts()}).catch(function(e){toast(e.message)})}

function fmtKeyExpiry(ts){
  if(!ts)return '<span class="badge">永久</span>';
  var left=Math.floor((ts-Date.now()/1000)/86400);
  var to=new Date(ts*1000).toLocaleDateString();
  if(left<0)return '<span class="badge bad">已到期（至 '+to+'）</span>';
  return '<span class="badge '+(left<3?'bad':'ok')+'">'+left+' 天（至 '+to+'）</span>'
}
function fmtKeyQuota(k){
  if(!k.max_calls)return '<span class="mono">'+fmtNum(k.used_calls||0)+' / 不限</span>';
  var rem=k.remaining_calls==null?Math.max(0,k.max_calls-(k.used_calls||0)):k.remaining_calls;
  return '<span class="badge '+(rem<=0?'bad':(rem/k.max_calls<=0.2?'warn':'ok'))+'">'+fmtNum(rem)+' / '+fmtNum(k.max_calls)+'</span>'
}
function attrJson(v){return esc(JSON.stringify(v==null?"":String(v)))}
function loadKeys(){api("/api/keys").then(function(r){
  KEYS=r.keys||[];
  var warn=el("no-key-warning");if(warn)warn.classList.toggle("hidden",KEYS.length>0);
  el("keys").innerHTML=KEYS.map(function(k){
    // 显示对齐 cf-ai-gw：列表只展示截断预览（前5...末1），完整明文经复制按钮获取
    var raw=k.key_value||k.key||"";
    var shown=esc(raw?keyMask(raw):(k.key_preview||"(旧密钥，无明文)"));
    var copy=raw?'<button class="ghost" style="padding:4px 8px;font-size:11px;border-radius:6px" onclick="copyKeyText('+attrJson(raw)+')">复制</button>':'';
    return '<tr><td><b>'+esc(k.name)+'</b></td>'+
      '<td><div class="row" style="gap:8px"><code id="key-val-'+esc(k.id)+'">'+shown+'</code>'+copy+'</div></td>'+
      '<td>'+fmtKeyExpiry(k.expires_at)+'</td>'+
      '<td>'+fmtKeyQuota(k)+'</td>'+
      '<td class="muted">'+esc(fmtTime(k.created_at))+'</td>'+
      '<td><div class="row"><button class="ghost" onclick="openEditKeyModal('+attrJson(k.id)+')">编辑</button>'+
      '<button class="ghost" onclick="resetKey('+attrJson(k.id)+')">重置用量</button>'+
      '<button class="danger" onclick="deleteKey('+attrJson(k.id)+')">删除</button></div></td></tr>'
  }).join("")||'<tr><td colspan="6" class="muted">暂无 API 密钥</td></tr>'
}).catch(function(e){toast(e.message)})}
var KEY_EDITING_ID=null,KEY_EXPIRES_ORIGINAL=0,KEY_EXPIRES_DIRTY=false;
function closeKeyModal(){el("key-modal").classList.add("hidden")}
function onKeyForeverChange(){
  var forever=el("key-forever").checked,d=el("key-days");
  d.disabled=forever;if(forever){d.value="";el("key-exp-hint").textContent=""}else onKeyDaysChange();
  KEY_EXPIRES_DIRTY=true;
}
function keyExpFromDays(days){
  if(!(days>0))return 0;
  return Math.floor((Date.now()+days*86400000)/1000);
}
function onKeyDaysChange(){
  KEY_EXPIRES_DIRTY=true;
  var days=Number(el("key-days").value)||0,ts=keyExpFromDays(days);
  el("key-exp-hint").textContent=ts?("生效后至 "+new Date(ts*1000).toLocaleString()+"（"+days+" 天）"):"请填写大于 0 的天数，或勾选「永久」";
}
function resetKeyForm(){
  ["key-name","key-val","key-days","key-calls"].forEach(function(i){el(i).value=""});
  el("key-modal-error").textContent="";
  el("key-forever").checked=true;el("key-days").disabled=true;el("key-exp-hint").textContent="";
}
function openAddKeyModal(){
  KEY_EDITING_ID=null;KEY_EXPIRES_ORIGINAL=0;KEY_EXPIRES_DIRTY=false;
  resetKeyForm();el("key-val-field").classList.remove("hidden");
  el("key-calls-label").textContent="最大调用次数（留空=不限）";
  el("key-modal-title").textContent="生成新 API 密钥";el("key-save-btn").textContent="生成密钥";
  el("key-modal").classList.remove("hidden");el("key-name").focus();
}
function openEditKeyModal(id){
  var k=null;KEYS.forEach(function(x){if(x.id===id)k=x});
  if(!k)return toast("Key 不存在，请刷新");
  KEY_EDITING_ID=id;KEY_EXPIRES_DIRTY=false;KEY_EXPIRES_ORIGINAL=k.expires_at||0;
  resetKeyForm();
  el("key-name").value=k.name||"";
  el("key-val-field").classList.add("hidden");
  el("key-calls-label").textContent="剩余次数（留空或 0 = 不限；当前已用 "+(k.used_calls||0)+" 次）";
  if(k.remaining_calls!=null)el("key-calls").value=k.remaining_calls;
  if(k.expires_at){
    el("key-forever").checked=false;el("key-days").disabled=false;
    var days=Math.round(((k.expires_at-Date.now()/1000)/86400)*100)/100;
    el("key-days").value=days>0?days:0;
    el("key-exp-hint").textContent="当前有效期至 "+new Date(k.expires_at*1000).toLocaleString()+"；不动则保持不变";
  }
  el("key-modal-title").textContent="编辑 API 密钥";el("key-save-btn").textContent="保存修改";
  el("key-modal").classList.remove("hidden");
}
function saveKey(){
  var name=el("key-name").value.trim();
  var forever=el("key-forever").checked,days=Number(el("key-days").value)||0;
  var calls=Math.floor(Number(el("key-calls").value)||0);
  var err=function(m){el("key-modal-error").textContent=m};
  err("");
  if(!name)return err("请填写名称");
  var expTs=0;
  if(!forever){
    if(!(days>0))return err("请填写大于 0 的天数，或勾选「永久」");
    expTs=keyExpFromDays(days);
  }
  if(KEY_EDITING_ID){
    var body={name:name};
    if(KEY_EXPIRES_DIRTY)body.expires_at=expTs;
    if(calls>0)body.remaining_calls=calls;else if(!forever||KEY_EXPIRES_DIRTY)body.remaining_calls=0;
    if(!forever&&KEY_EXPIRES_DIRTY)body.expires_at=expTs;
    api("/api/keys/"+KEY_EDITING_ID,{method:"PATCH",body:body}).then(function(){
      closeKeyModal();toast("已保存");loadKeys()
    }).catch(function(e){err(e.message)})
    return;
  }
  var created={name:name,expires_at:expTs,max_calls:calls};
  var kv=el("key-val").value.trim();if(kv)created.key=kv;
  api("/api/keys",{method:"POST",body:created}).then(function(r){
    closeKeyModal();loadKeys();
    el("generated-key-val").textContent=r.key||r.key_value||"";
    toast("已生成，可在列表中随时复制")
  }).catch(function(e){err(e.message)})
}
function resetKey(id){if(!confirm("重置这把 Key 的已用次数？"))return;api("/api/keys/"+id+"/reset",{method:"POST"}).then(function(){toast("已重置");loadKeys()}).catch(function(e){toast(e.message)})}
function deleteKey(id){if(!confirm("删除这个 API Key ?"))return;api("/api/keys/"+id,{method:"DELETE"}).then(function(){toast("已删除");loadKeys()}).catch(function(e){toast(e.message)})}

function loadModels(force){
  el("models").innerHTML='<p class="muted">加载中…</p>';
  api("/api/models"+(force?"?refresh=1":"")).then(function(r){
    MODELS=r.models||[];
    if(el("modelsUpdated"))el("modelsUpdated").textContent=r.updated_at?("最后更新："+fmtTime(Math.floor(r.updated_at/1000))):"";
    el("models").innerHTML='<table><thead><tr><th>模型</th><th>上下文</th><th>最大输出</th><th>能力</th></tr></thead><tbody>'+MODELS.map(function(m){
      var rt=(m.realms&&m.realms.length>1)?'':(m.realm==='intl'?'<span class="badge" style="background:#6366f1;color:#fff">国际</span>':'<span class="badge" style="background:#10b981;color:#fff">国内</span>');
      return '<tr><td class="mono">'+esc(m.id)+' '+rt+'<div class="muted">'+esc(m.name||"")+'</div></td><td>'+esc(m.context_length||"-")+'</td><td>'+esc(m.max_output_tokens||"-")+'</td><td>'+esc((m.reasoning?"reasoning ":"")+(m.vision?"vision":"")||"-")+'</td></tr>'
    }).join("")+'</tbody></table>';
    renderRouteTargets();refreshRouteSelects();
  }).catch(function(e){el("models").innerHTML='<p class="badge bad">'+esc(e.message)+'</p>'})
}

function routeSelectHtml(kind,source,target){
  var opts=MODELS.map(function(m){
    return '<option value="'+esc(m.id)+'"'+(String(m.id).toLowerCase()===String(target||"").toLowerCase()?' selected':'')+'>'+esc(m.id)+(m.route?'（→ '+esc(m.target)+'）':'')+'</option>'
  }).join("");
  var inputId="route-"+kind+"-src-"+(source==null?"new":encodeURIComponent(source));
  return {source:source,target:target,select:'<select data-route-select="'+kind+'">'+(opts||'<option value="">（清单为空）</option>')+'</select>'}
}
function renderRoutes(map,alias){
  [["map",map],["alias",alias]].forEach(function(pair){
    var kind=pair[0],rows=pair[1]||[];
    var tbody=el("route-"+kind);
    if(!tbody)return;
    tbody.innerHTML=rows.length?rows.map(function(item){
      var cfg=routeSelectHtml(kind,item.source,item.target);
      return '<tr><td><input class="route-source" data-route-kind="'+kind+'" value="'+esc(item.source)+'" placeholder="'+(kind==="map"?"请求模型名":"假名")+'"></td>'+
        '<td>'+cfg.select+'</td>'+
        '<td><button class="danger" onclick="removeRouteRow(this)">删除</button></td></tr>'
    }).join(""):'<tr><td colspan="3" class="muted">暂无配置</td></tr>';
  });
}
function collectRoutes(){
  var out={map:{},alias:{}};
  ["map","alias"].forEach(function(kind){
    var tbody=el("route-"+kind);if(!tbody)return;
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"),function(tr){
      var srcEl=tr.querySelector(".route-source"),sel=tr.querySelector("select[data-route-select]");
      if(!srcEl||!sel)return;
      var s=srcEl.value.trim();if(!s)return;
      out[kind][s]=sel.value;
    });
  });
  return out;
}
function addRouteRow(kind){
  var rows=rowsFromTable(kind);rows.push({source:"",target:MODELS.length?MODELS[0].id:""});
  renderRoutes(kind==="map"?rows:rowsFromTable("map"),kind==="alias"?rows:rowsFromTable("alias"));
}
function rowsFromTable(kind){
  var tbody=el("route-"+kind);if(!tbody)return [];
  var out=[];
  Array.prototype.forEach.call(tbody.querySelectorAll("tr"),function(tr){
    var srcEl=tr.querySelector(".route-source"),sel=tr.querySelector("select[data-route-select]");
    if(!srcEl||!sel)return;
    if(!srcEl.value.trim()&&!sel.value)return;
    out.push({source:srcEl.value.trim(),target:sel.value});
  });
  return out;
}
function removeRouteRow(btn){var tr=btn.closest("tr");if(tr)tr.parentNode.removeChild(tr)}
function refreshRouteSelects(){
  var map=rowsFromTable("map"),alias=rowsFromTable("alias");
  if(map.length||alias.length)renderRoutes(map,alias);
}
function renderRouteTargets(){
  var box=el("route-targets");if(!box)return;
  box.innerHTML=MODELS.length?MODELS.map(function(m){
    return '<code class="target-chip" onclick="copyText('+attrJson(m.id)+')">'+esc(m.id)+'</code>'
  }).join(""):'<p class="muted">清单为空，请先强制刷新</p>';
}
function loadRoutes(){
  el("route-map").innerHTML='<tr><td colspan="3" class="muted">加载中…</td></tr>';
  el("route-alias").innerHTML='<tr><td colspan="3" class="muted">加载中…</td></tr>';
  api("/api/model-routes").then(function(r){
    ROUTES={mappings:r.mappings||{},aliases:r.aliases||{}};
    renderRoutes(toRows(ROUTES.mappings),toRows(ROUTES.aliases));
  }).catch(function(e){toast(e.message)})
}
function toRows(obj){return Object.keys(obj||{}).map(function(k){return {source:k,target:obj[k]}})}
function saveRoutes(){
  var collected=collectRoutes();
  api("/api/model-routes",{method:"PUT",body:{mappings:collected.map,aliases:collected.alias}}).then(function(r){
    ROUTES={mappings:r.mappings||{},aliases:r.aliases||{}};
    renderRoutes(toRows(ROUTES.mappings),toRows(ROUTES.aliases));
    toast("已保存映射与假名")
  }).catch(function(e){toast(e.message)})
}

applyTheme(document.documentElement.getAttribute("data-theme")||"dark");
if(sessionStorage.getItem(KEY_STORE)){showApp()}else{el("login").classList.remove("hidden")}
</script>
</body>
</html>'''
