# -*- coding: utf-8 -*-
"""
test_qoder_admin.py — qoder_admin.py 离线测试套件（不触网、纯假数据）

覆盖：
  1. KeyStore：创建（随机 sk- 前缀 / 自定义 key）、字段对齐 CF createApiKey、
     查重 409 语义、非法自定义 key 400、patch（name/enabled/expires_at/max_calls/
     remaining_calls 口径/热密钥轮换）、delete、reset_usage、list 公开视图、
     lookup（命中 / 禁用 / 过期 / 限额 429 / 未知 / 计数累加）、按天计数跨天归零、
     原子写落盘可重载（重开 KeyStore 计数恢复、无 .tmp 残留、坏 JSON 容错）
  2. check_admin：正确 / 错误 / 空密码 / 非 Bearer / 无 header
  3. AdminApi：/admin 单页（含 <title>、CF 特有字样已清理）、auth/login、
     未认证 401、密码未配置 403、keys CRUD 全路由（坏 JSON 400、未知 id 404、
     查重 409）、adapters 缺省 501、注入假 adapters 后各路由正常、
     model-routes PUT/PATCH 双形态、stats 补 keys 计数、adapter 异常 500

运行：python test_qoder_admin.py（输出 N/N 通过，退出码 0/1）
约束：纯标准库、全部假数据（acct-1 / m1 / testkey…）、不触网；
      存储路径用 tempfile 临时目录，测试完清理。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoder_admin as QA

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PW = "test-admin-pw-123"
AUTH = {"Authorization": "Bearer " + PW}

_results = []


def _record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" - " + str(detail)[:300]) if detail else ""))


def _check(name, cond, detail=""):
    _record(name, bool(cond), detail)


def _tmpdir():
    return tempfile.mkdtemp(prefix="qa-admin-")


def _mk_store(folder):
    return QA.KeyStore(os.path.join(folder, "api_keys.json"))


def _call(api, method, path, body=None, auth=True, headers=None, raw_body=None):
    """直调 AdminApi.handle（不经过 HTTP 层，框架无关入口的意义所在）。"""
    h = dict(headers or {})
    if auth and "Authorization" not in h and "authorization" not in h:
        h.update(AUTH)
    if raw_body is not None:
        bb = raw_body
    elif body is not None:
        bb = json.dumps(body).encode("utf-8")
    else:
        bb = b""
    return api.handle(method, path, h, bb)


def _jresp(resp):
    status, headers, payload = resp
    return status, headers, json.loads(payload.decode("utf-8"))


# ── KeyStore ─────────────────────────────────────────────────────────────────

def _test_keystore_basic():
    d = _tmpdir()
    try:
        ks = _mk_store(d)
        row = ks.create()
        _check("ks.create 默认名称", row["name"] == "default", row["name"])
        _check("ks.create 随机 key sk- 前缀",
               row["key_value"].startswith("sk-") and len(row["key_value"]) == 51,
               row["key_value"][:12])
        # 公开视图对齐 CF keyRow：不回显 key_hash，从落盘记录验证
        with open(os.path.join(d, "api_keys.json"), encoding="utf-8") as f:
            stored = json.load(f)["keys"][0]
        _check("ks.create 落盘 key_hash=sha256(key_value)",
               stored["key_hash"] == QA._sha256_hex(stored["key_value"])
               and stored["key_value"] == row["key_value"])
        _check("ks.create 公开视图不含 key_hash", "key_hash" not in row)
        _check("ks.create 字段对齐 CF",
               row["enabled"] == 1 and row["max_calls"] == 0 and row["expires_at"] == 0
               and row["last_used_at"] == 0 and row["created_at"] > 0
               and len(row["id"]) == 36)
        _check("ks.create 公开视图 key/key_preview",
               row["key"] == row["key_value"] and row["key_preview"].endswith(
                   row["key_value"][-1]))

        row2 = ks.create("本地测试", {"key": "my-custom-key-123"})
        _check("ks.create 自定义 key 原样保留",
               row2["key_value"] == "my-custom-key-123")
        _check("ks.create name 截断 80",
               ks.create("x" * 100, {})["name"] == "x" * 80)

        for bad in ("abc", "abcd efgh", "密钥测试值正好八个字", "ab\tcd\tef"):
            try:
                ks.create("bad", {"key": bad})
                _check("ks.create 非法自定义 key 拒绝 %r" % bad, False)
            except QA.KeyStoreError as e:
                _check("ks.create 非法自定义 key 400 %r" % bad, e.status == 400)
        try:
            ks.create("dup", {"key": "my-custom-key-123"})
            _check("ks.create 查重 409", False)
        except ValueError as e:
            _check("ks.create 查重 409 且抛 ValueError",
                   str(e) == "该密钥值已被使用"
                   and isinstance(e, QA.KeyStoreError) and e.status == 409)

        _check("ks.create 规范化 max_calls",
               ks.create("n", {"max_calls": "5"})["max_calls"] == 5
               and ks.create("n", {"max_calls": -3})["max_calls"] == 0
               and ks.create("n", {"max_calls": "abc"})["max_calls"] == 0)
        _check("ks.create 规范化 expires_at 毫秒→秒",
               ks.create("n", {"expires_at": 10 ** 13})["expires_at"] == 10 ** 10)
        _check("ks.create 规范化 expires_at ISO 字符串",
               ks.create("n", {"expires_at": "2030-01-02T00:00:00Z"})["expires_at"]
               == 1893542400)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_keystore_patch():
    d = _tmpdir()
    try:
        ks = _mk_store(d)
        row = ks.create("orig", {"max_calls": 10})
        kid = row["id"]
        out = ks.patch(kid, {"name": "renamed", "enabled": 0,
                             "expires_at": 4102444800})
        _check("ks.patch 更新 name/enabled/expires_at",
               out["name"] == "renamed" and out["enabled"] == 0
               and out["expires_at"] == 4102444800)
        _check("ks.patch 后 list 生效",
               ks.list()[0]["name"] == "renamed")
        _check("ks.patch 未知 id → None", ks.patch("no-such-id", {"name": "x"}) is None)

        out = ks.patch(kid, {"enabled": 1, "max_calls": 7})
        _check("ks.patch max_calls", out["max_calls"] == 7)

        raw = row["key_value"]
        ks.lookup(raw); ks.lookup(raw)  # used = 2
        out = ks.patch(kid, {"remaining_calls": 5})
        _check("ks.patch remaining_calls 口径=已用+余量",
               out["max_calls"] == 7 and out["used_calls"] == 2, out["max_calls"])
        out = ks.patch(kid, {"remaining_calls": 0})
        _check("ks.patch remaining_calls 0=不限", out["max_calls"] == 0)

        rotated = ks.patch(kid, {"key_value": "rotated-key-9999"})
        _check("ks.patch 热密钥轮换", rotated["key_value"] == "rotated-key-9999")
        _check("ks.patch 轮换后新明文命中、旧明文失效",
               ks.lookup("rotated-key-9999")[0] is not None
               and ks.lookup(raw)[0] is None)
        other = ks.create("other", {"key": "other-custom-key"})
        try:
            ks.patch(kid, {"key_value": "other-custom-key"})
            _check("ks.patch 轮换撞已有 key 409", False)
        except QA.KeyStoreError as e:
            _check("ks.patch 轮换撞已有 key 409", e.status == 409)
        _check("ks.patch 轮换撞已有 key 不改原值",
               other["key_value"] == "other-custom-key")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_keystore_delete_reset():
    d = _tmpdir()
    try:
        ks = _mk_store(d)
        row = ks.create("gone", {})
        _check("ks.delete 存在 → True", ks.delete(row["id"]) is True)
        _check("ks.delete 后 list 为空", ks.list() == [])
        _check("ks.delete 后 lookup 不命中", ks.lookup(row["key_value"])[0] is None)
        _check("ks.delete 不存在 → False", ks.delete("no-such-id") is False)

        row = ks.create("quota", {"max_calls": 100})
        ks.lookup(row["key_value"])
        ks.lookup(row["key_value"])
        _check("ks.reset_usage 前计数累加", ks.list()[0]["used_calls"] == 2)
        out = ks.reset_usage(row["id"])
        _check("ks.reset_usage 返回", out == {"ok": True, "used": 0})
        _check("ks.reset_usage 清零", ks.list()[0]["used_calls"] == 0)
        rec, allowed = ks.lookup(row["key_value"])
        _check("ks.reset_usage 后重新计数", allowed and rec["used_calls"] == 1)
        _check("ks.reset_usage 未知 id → None", ks.reset_usage("no-such-id") is None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_keystore_lookup():
    d = _tmpdir()
    try:
        ks = _mk_store(d)
        _check("ks.lookup 未知 key → (None, False)",
               ks.lookup("sk-nothing") == (None, False))
        _check("ks.lookup 空 key → (None, False)", ks.lookup("") == (None, False))
        _check("ks.lookup None → (None, False)", ks.lookup(None) == (None, False))

        row = ks.create("hit", {"max_calls": 0})
        raw = row["key_value"]
        rec, allowed = ks.lookup(raw)
        _check("ks.lookup 命中放行", allowed and rec is not None)
        _check("ks.lookup 视图字段",
               rec["id"] == row["id"] and rec["name"] == "hit"
               and rec["max_calls"] == 0 and rec["used_calls"] == 1
               and rec["key_value"] == raw and rec["enabled"] == 1)
        rec2, _ = ks.lookup(raw)
        _check("ks.lookup 计数累加", rec2["used_calls"] == 2)
        _check("ks.lookup 后 list 同步", ks.list()[0]["used_calls"] == 2)

        ks.patch(row["id"], {"enabled": 0})
        _check("ks.lookup 禁用 key → (None, False)",
               ks.lookup(raw) == (None, False))
        ks.patch(row["id"], {"enabled": 1})

        past = ks.create("past", {"expires_at": 1})
        _check("ks.lookup 过期 key → (None, False)",
               ks.lookup(past["key_value"]) == (None, False))
        future = ks.create("future", {"expires_at": 4102444800})
        _check("ks.lookup 未过期 key 命中",
               ks.lookup(future["key_value"])[1] is True)

        lim = ks.create("lim", {"max_calls": 2})
        lraw = lim["key_value"]
        seq = [ks.lookup(lraw)[1] for _ in range(4)]
        _check("ks.lookup 限额序列 True/True/False/False",
               seq == [True, True, False, False], seq)
        rec, allowed = ks.lookup(lraw)
        _check("ks.lookup 超限不继续计数（429 语义）",
               not allowed and rec["used_calls"] == 2)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_keystore_daily():
    d = _tmpdir()
    try:
        ks = _mk_store(d)
        row = ks.create("daily", {"max_calls": 100})
        raw = row["key_value"]
        orig = QA._utc_date
        try:
            QA._utc_date = lambda: "2026-10-01"
            ks.lookup(raw); ks.lookup(raw)
            _check("ks.按天计数 当日=2", ks.list()[0]["used_calls"] == 2)
            QA._utc_date = lambda: "2026-10-02"
            rec, allowed = ks.lookup(raw)
            _check("ks.跨天计数归零重新计", allowed and rec["used_calls"] == 1)
            _check("ks.跨天后 list 用新日期", ks.list()[0]["used_calls"] == 1)
            QA._utc_date = lambda: "2026-10-01"
            _check("ks.历史日期计数保留", ks.list()[0]["used_calls"] == 2)
        finally:
            QA._utc_date = orig
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_keystore_persist():
    d = _tmpdir()
    try:
        path = os.path.join(d, "api_keys.json")
        ks = QA.KeyStore(path)
        a = ks.create("keep-a", {})
        b = ks.create("keep-b", {})
        ks.lookup(a["key_value"]); ks.lookup(a["key_value"])
        ks.lookup(b["key_value"])
        _check("ks.持久化 usage 文件生成",
               os.path.exists(path + ".usage.json"))
        _check("ks.原子写无 .tmp 残留",
               not any(f.endswith(".tmp") for f in os.listdir(d)),
               sorted(os.listdir(d)))
        if os.name == "posix":
            _check("ks.主文件权限 0600",
                   (os.stat(path).st_mode & 0o777) == 0o600,
                   oct(os.stat(path).st_mode))
        else:
            _record("ks.主文件权限 0600（POSIX 才校验）", True, "skip on windows")

        ks2 = QA.KeyStore(path)
        rows = {r["name"]: r for r in ks2.list()}
        _check("ks.重开恢复 Key 记录", set(rows) == {"keep-a", "keep-b"})
        _check("ks.重开恢复当日计数",
               rows["keep-a"]["used_calls"] == 2 and rows["keep-b"]["used_calls"] == 1)
        rec, allowed = ks2.lookup(a["key_value"])
        _check("ks.重开后计数继续", allowed and rec["used_calls"] == 3)

        ks2.delete(b["id"])
        ks3 = QA.KeyStore(path)
        _check("ks.重开删除已落盘", [r["name"] for r in ks3.list()] == ["keep-a"])

        bad = os.path.join(d, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        ks4 = QA.KeyStore(bad)
        _check("ks.坏 JSON 容错为空存储", ks4.list() == [])
        row = ks4.create("after-bad", {})
        _check("ks.坏 JSON 后可继续创建", ks4.list()[0]["name"] == "after-bad")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── check_admin ──────────────────────────────────────────────────────────────

def _test_check_admin():
    _check("auth.正确密码", QA.check_admin("Bearer " + PW, PW) is True)
    _check("auth.错误密码", QA.check_admin("Bearer wrong", PW) is False)
    _check("auth.空密码一律拒绝", QA.check_admin("Bearer x", "") is False)
    _check("auth.密码空时无 header 拒绝", QA.check_admin(None, "") is False)
    _check("auth.非 Bearer 前缀", QA.check_admin("Basic " + PW, PW) is False)
    _check("auth.无 header", QA.check_admin(None, PW) is False)
    _check("auth.小写 bearer 不认", QA.check_admin("bearer " + PW, PW) is False)
    _check("auth.空 token", QA.check_admin("Bearer ", PW) is False)


# ── AdminApi：/admin 页面 + 认证 ─────────────────────────────────────────────

def _test_admin_page():
    d = _tmpdir()
    try:
        api = QA.AdminApi(_mk_store(d), PW, None)
        st, hd, body = api.handle("GET", "/admin", {}, b"")
        text = body.decode("utf-8")
        _check("page./admin 200 HTML",
               st == 200 and hd["Content-Type"].startswith("text/html"))
        _check("page.含 <title>", "<title>Qoder Admin</title>" in text)
        _check("page.no-store", hd.get("Cache-Control") == "no-store")
        st2, _, _ = api.handle("GET", "/admin/", {}, b"")
        _check("page./admin/ 尾斜杠同 200", st2 == 200)
        st3, _, _ = api.handle("GET", "/admin", {"Authorization": "Bearer wrong"}, b"")
        _check("page.未认证也可看登录框", st3 == 200)
        _check("page.保留管理端点 JS",
               "/api/keys" in text and "/api/auth/login" in text
               and "/api/model-routes" in text and "/api/accounts" in text)
        _check("page.深色主题单页主体保留",
               'data-theme="dark"' in text and "--grad:" in text)
        for token in ("Qoder CF Gateway", "Durable Object", "Cron ",
                      "fonts.googleapis.com", "持久化到 KV"):
            _check("page.CF 特有字样已清理: %r" % token, token not in text)
        _check("page.admin_html()==ADMIN_HTML",
                QA.admin_html() is QA.ADMIN_HTML)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_admin_auth():
    d = _tmpdir()
    try:
        api = QA.AdminApi(_mk_store(d), PW, None)
        st, _, body = _jresp(_call(api, "GET", "/api/keys", auth=False))
        _check("api.未认证 401", st == 401, body)
        st, _, body = _jresp(_call(api, "GET", "/api/keys",
                                   headers={"Authorization": "Bearer wrong"}))
        _check("api.错误 Bearer 401", st == 401)
        st, _, _ = _jresp(_call(api, "GET", "/api/keys"))
        _check("api.正确 Bearer 200", st == 200)
        st, _, _ = _jresp(_call(api, "GET", "/api/keys",
                                headers={"authorization": "Bearer " + PW}))
        _check("api.header 大小写不敏感", st == 200)

        st, _, body = _jresp(_call(api, "POST", "/api/auth/login",
                                   body={"password": PW}, auth=False))
        _check("api.login 正确密码", st == 200 and body.get("ok") is True)
        st, _, body = _jresp(_call(api, "POST", "/api/auth/login",
                                   body={"password": "nope"}, auth=False))
        _check("api.login 错误密码 401", st == 401)
        st, _, _ = _jresp(_call(api, "POST", "/api/auth/login",
                                raw_body=b"{bad json", auth=False))
        _check("api.login 坏 JSON 400", st == 400)
        st, _, _ = _jresp(_call(api, "POST", "/api/auth/login",
                                raw_body=b"", auth=False))
        _check("api.login 空 body 400", st == 400)

        closed = QA.AdminApi(_mk_store(d), "", None)
        st, _, body = _jresp(_call(closed, "GET", "/api/keys",
                                   headers={"Authorization": "Bearer anything"}))
        _check("api.密码未配置管理 API 403", st == 403)
        st, _, _ = _jresp(_call(closed, "GET", "/api/keys", auth=False))
        _check("api.密码未配置无 header 也 403", st == 403)
        st, _, _ = _jresp(_call(closed, "POST", "/api/auth/login",
                                body={"password": "anything"}, auth=False))
        _check("api.密码未配置 login 403", st == 403)
        st, _, _ = closed.handle("GET", "/admin", {}, b"")
        _check("api.密码未配置页面仍可看", st == 200)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── AdminApi：/api/keys CRUD ─────────────────────────────────────────────────

def _test_admin_keys_api():
    d = _tmpdir()
    try:
        api = QA.AdminApi(_mk_store(d), PW, None)
        st, _, body = _jresp(_call(api, "GET", "/api/keys"))
        _check("keys.GET 空列表", st == 200 and body == {"keys": []})

        st, _, row = _jresp(_call(api, "POST", "/api/keys",
                                  body={"name": "k1", "max_calls": 3,
                                        "expires_at": 0}))
        _check("keys.POST 创建", st == 200 and row["name"] == "k1"
               and row["max_calls"] == 3 and row["key_value"].startswith("sk-"))
        kid = row["id"]

        st, _, err = _jresp(_call(api, "POST", "/api/keys", raw_body=b"{oops"))
        _check("keys.POST 坏 JSON 400", st == 400 and "error" in err)
        st, _, err = _jresp(_call(api, "POST", "/api/keys", raw_body=b""))
        _check("keys.POST 空 body 400", st == 400)
        st, _, err = _jresp(_call(api, "POST", "/api/keys",
                                  body={"name": "bad", "key": "short"}))
        _check("keys.POST 非法自定义 key 400", st == 400)
        st, _, err = _jresp(_call(api, "POST", "/api/keys",
                                  body={"name": "dup", "key": row["key_value"]}))
        _check("keys.POST 查重 409",
               st == 409 and err["error"]["message"] == "该密钥值已被使用")

        st, _, body = _jresp(_call(api, "GET", "/api/keys"))
        _check("keys.GET 一条", st == 200 and len(body["keys"]) == 1
               and body["keys"][0]["id"] == kid)

        st, _, row2 = _jresp(_call(api, "PATCH", "/api/keys/" + kid,
                                   body={"enabled": 0, "name": "k1-off"}))
        _check("keys.PATCH 更新", st == 200 and row2["enabled"] == 0
               and row2["name"] == "k1-off")
        st, _, err = _jresp(_call(api, "PATCH", "/api/keys/no-such-id",
                                  body={"enabled": 1}))
        _check("keys.PATCH 未知 id 404", st == 404)
        st, _, _ = _jresp(_call(api, "PATCH", "/api/keys/" + kid, raw_body=b"[1]"))
        _check("keys.PATCH 坏 JSON 400", st == 400)

        ks_view, _ = api.key_store.lookup(
            api.key_store.list()[0]["key_value"])
        _ = ks_view  # 计一次，供 reset 验证
        st, _, out = _jresp(_call(api, "POST", "/api/keys/%s/reset" % kid))
        _check("keys.reset 200", st == 200 and out.get("ok") is True
               and out.get("used") == 0)
        st, _, _ = _jresp(_call(api, "POST", "/api/keys/no-such-id/reset"))
        _check("keys.reset 未知 id 404", st == 404)

        st, _, out = _jresp(_call(api, "DELETE", "/api/keys/" + kid))
        _check("keys.DELETE 200", st == 200 and out == {"ok": True})
        st, _, body = _jresp(_call(api, "GET", "/api/keys"))
        _check("keys.DELETE 后列表为空", body["keys"] == [])
        st, _, _ = _jresp(_call(api, "DELETE", "/api/keys/" + kid))
        _check("keys.DELETE 未知 id 404", st == 404)

        st, _, _ = _jresp(_call(api, "GET", "/api/keys/%s/deep" % kid))
        _check("keys.未知子路径 404", st == 404)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── AdminApi：adapters 缺省 501 ──────────────────────────────────────────────

def _test_admin_no_adapters():
    d = _tmpdir()
    try:
        api = QA.AdminApi(_mk_store(d), PW, None)
        cases = [
            ("GET", "/api/accounts"),
            ("POST", "/api/accounts/import", {"accounts": []}),
            ("PATCH", "/api/accounts/a1", {"label": "x"}),
            ("DELETE", "/api/accounts/a1"),
            ("POST", "/api/accounts/a1/test"),
            ("POST", "/api/accounts/a1/refresh"),
            ("POST", "/api/accounts/a1/checkin"),
            ("GET", "/api/stats"),
            ("GET", "/api/models"),
            ("GET", "/api/model-routes"),
            ("PUT", "/api/model-routes", {"mappings": {}, "aliases": {}}),
        ]
        for case in cases:
            method, path = case[0], case[1]
            body = case[2] if len(case) > 2 else None
            st, _, err = _jresp(_call(api, method, path, body=body))
            _check("501.%s %s" % (method, path), st == 501 and "error" in err)
        st, _, _ = _jresp(_call(api, "GET", "/api/nope"))
        _check("404.未知 /api 路径", st == 404)
        st, _, _ = _jresp(_call(api, "GET", "/not-mine"))
        _check("404.非 /api 非 /admin 路径", st == 404)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── AdminApi：注入假 adapters 后全路由 ────────────────────────────────────────

class _FakeAdapters:
    """假 adapters：记录调用参数，返回可控假数据（全部编造，不触网）。"""

    def __init__(self, fail_checkins=False):
        self.calls = []
        self.fail_checkins = fail_checkins
        self.db = {"a1": {"id": "a1", "label": "acct-1", "uid": "uid-fake-1"}}
        self.routes = {"mappings": {"gpt-4o": "qoder-max"}, "aliases": {"fast": "qoder-flash"}}

    def build(self):
        return {
            "list_accounts": self.list_accounts,
            "latest_checkins": self.latest_checkins,
            "import_accounts": self.import_accounts,
            "patch_account": self.patch_account,
            "delete_account": self.delete_account,
            "test_account": self.test_account,
            "refresh_account": self.refresh_account,
            "checkin_account": self.checkin_account,
            "stats": self.stats,
            "list_models": self.list_models,
            "model_routes": self.model_routes,
            "patch_model_routes": self.patch_model_routes,
        }

    def list_accounts(self):
        self.calls.append(("list_accounts",))
        return [dict(self.db["a1"])]

    def latest_checkins(self):
        self.calls.append(("latest_checkins",))
        if self.fail_checkins:
            raise RuntimeError("checkin source down")
        return [{"account_id": "a1", "secured": 1, "amount": 100}]

    def import_accounts(self, items):
        self.calls.append(("import_accounts", items))
        return {"imported": len(items), "updated": 0, "skipped": 0,
                "items": [{"label": "acct-1", "action": "imported"}]}

    def patch_account(self, aid, fields):
        self.calls.append(("patch_account", aid, fields))
        if aid not in self.db:
            return None
        out = dict(self.db[aid])
        out.update(fields)
        return out

    def delete_account(self, aid):
        self.calls.append(("delete_account", aid))
        self.db.pop(aid, None)

    def test_account(self, aid):
        self.calls.append(("test_account", aid))
        return {"ok": True, "models": 3} if aid in self.db else None

    def refresh_account(self, aid):
        self.calls.append(("refresh_account", aid))
        return {"ok": True, "expires_at": "2030-01-01", "refresh_dead": False}

    def checkin_account(self, aid):
        self.calls.append(("checkin_account", aid))
        return {"ok": True, "rep": {"secured": True, "amount": 100}}

    def stats(self):
        self.calls.append(("stats",))
        return {"today": {"requests": 5, "total_tokens": 100}, "trend": [],
                "accounts": 1, "models": 2, "checkins": []}

    def list_models(self):
        self.calls.append(("list_models",))
        return [{"id": "qoder-max", "name": "Qoder Max",
                 "context_length": 128000, "max_output_tokens": 8192,
                 "reasoning": True, "vision": False}]

    def model_routes(self):
        self.calls.append(("model_routes",))
        return dict(self.routes)

    def patch_model_routes(self, body):
        self.calls.append(("patch_model_routes", body))
        self.routes = {"mappings": dict(body.get("mappings") or {}),
                       "aliases": dict(body.get("aliases") or {})}
        return dict(self.routes)


def _test_admin_with_adapters():
    d = _tmpdir()
    try:
        fake = _FakeAdapters()
        ks = _mk_store(d)
        ks.create("k-a", {})
        ks.create("k-b", {})
        api = QA.AdminApi(ks, PW, fake.build())

        st, _, body = _jresp(_call(api, "GET", "/api/accounts"))
        _check("acc.GET 账号+签到",
               st == 200 and body["accounts"][0]["label"] == "acct-1"
               and body["checkins"][0]["account_id"] == "a1")

        st, _, body = _jresp(_call(api, "POST", "/api/accounts/import",
                                   body={"accounts": [{"token": "fake-token"}]}))
        _check("acc.import {accounts:[...]} 规整成 list",
               st == 200 and body["imported"] == 1
               and fake.calls[-1] == ("import_accounts", [{"token": "fake-token"}]))
        st, _, body = _jresp(_call(api, "POST", "/api/accounts/import",
                                   body=[{"token": "t1"}, {"token": "t2"}]))
        _check("acc.import 数组 body", st == 200 and body["imported"] == 2)
        st, _, body = _jresp(_call(api, "POST", "/api/accounts",
                                   body={"token": "single"}))
        _check("acc.POST = import 单对象",
               st == 200 and fake.calls[-1][1] == [{"token": "single"}])
        st, _, _ = _jresp(_call(api, "POST", "/api/accounts/import", raw_body=b"[["))
        _check("acc.import 坏 JSON 400", st == 400)

        st, _, body = _jresp(_call(api, "PATCH", "/api/accounts/a1",
                                   body={"label": "acct-renamed"}))
        _check("acc.PATCH 透传", st == 200 and body["label"] == "acct-renamed"
               and fake.calls[-1] == ("patch_account", "a1", {"label": "acct-renamed"}))
        st, _, err = _jresp(_call(api, "PATCH", "/api/accounts/zz",
                                  body={"label": "x"}))
        _check("acc.PATCH 未知 404", st == 404 and "账号不存在" in err["error"]["message"])

        st, _, body = _jresp(_call(api, "DELETE", "/api/accounts/a1"))
        _check("acc.DELETE 幂等 {ok:true}", st == 200 and body == {"ok": True}
               and ("delete_account", "a1") in fake.calls)
        # 恢复假库，供后面的 test/refresh/checkin 用例继续命中 a1
        fake.db["a1"] = {"id": "a1", "label": "acct-1", "uid": "uid-fake-1"}

        st, _, body = _jresp(_call(api, "POST", "/api/accounts/a1/test"))
        _check("acc.test 透传（空 body 也可）",
               st == 200 and body["ok"] is True and body["models"] == 3)
        st, _, body = _jresp(_call(api, "POST", "/api/accounts/a1/refresh"))
        _check("acc.refresh 透传", st == 200 and body["refresh_dead"] is False)
        st, _, body = _jresp(_call(api, "POST", "/api/accounts/a1/checkin"))
        _check("acc.checkin 透传", st == 200 and body["rep"]["secured"] is True)
        st, _, _ = _jresp(_call(api, "POST", "/api/accounts/zz/test"))
        _check("acc.test 未知 404", st == 404)

        st, _, body = _jresp(_call(api, "GET", "/api/stats"))
        _check("stats.透传+补 keys 数",
               st == 200 and body["today"]["requests"] == 5 and body["keys"] == 2,
               body.get("keys"))
        strict = QA.AdminApi(ks, PW, {"stats": lambda: {"keys": 99}})
        st, _, body = _jresp(_call(strict, "GET", "/api/stats"))
        _check("stats.adapter 给了 keys 就不覆盖", body["keys"] == 99)

        st, _, body = _jresp(_call(api, "GET", "/api/models"))
        _check("models.列表包装", st == 200
               and body["models"][0]["id"] == "qoder-max"
               and "updated_at" in body)
        dict_api = QA.AdminApi(ks, PW, {"list_models": lambda: {
            "models": [{"id": "m1"}], "updated_at": 1700000000}})
        st, _, body = _jresp(_call(dict_api, "GET", "/api/models"))
        _check("models.dict 形态透传",
               body["models"] == [{"id": "m1"}] and body["updated_at"] == 1700000000)

        st, _, body = _jresp(_call(api, "GET", "/api/model-routes"))
        _check("routes.GET", st == 200
               and body["mappings"] == {"gpt-4o": "qoder-max"}
               and body["aliases"] == {"fast": "qoder-flash"})
        st, _, body = _jresp(_call(api, "PUT", "/api/model-routes",
                                   body={"mappings": {"gpt": "qoder-max"},
                                         "aliases": {}}))
        _check("routes.PUT（CF 页面形态）",
               st == 200 and body["ok"] is True
               and body["mappings"] == {"gpt": "qoder-max"})
        st, _, body = _jresp(_call(api, "PATCH", "/api/model-routes",
                                   body={"mappings": {}, "aliases": {"f2": "qoder-flash"}}))
        _check("routes.PATCH（规格形态）",
               st == 200 and body["ok"] is True
               and body["aliases"] == {"f2": "qoder-flash"}
               and ("patch_model_routes",
                    {"mappings": {}, "aliases": {"f2": "qoder-flash"}}) in fake.calls)
        st, _, _ = _jresp(_call(api, "PUT", "/api/model-routes", raw_body=b"xx"))
        _check("routes.坏 JSON 400", st == 400)

        boom = QA.AdminApi(ks, PW, {"list_accounts": lambda: (_ for _ in ()).throw(
            RuntimeError("adapter boom"))})
        st, _, err = _jresp(_call(boom, "GET", "/api/accounts"))
        _check("acc.adapter 异常兜底 500",
               st == 500 and "adapter boom" in err["error"]["message"])

        resilient = _FakeAdapters(fail_checkins=True)
        api2 = QA.AdminApi(ks, PW, resilient.build())
        st, _, body = _jresp(_call(api2, "GET", "/api/accounts"))
        _check("acc.latest_checkins 异常吞成 []",
               st == 200 and body["checkins"] == [] and body["accounts"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    tests = [
        _test_keystore_basic,
        _test_keystore_patch,
        _test_keystore_delete_reset,
        _test_keystore_lookup,
        _test_keystore_daily,
        _test_keystore_persist,
        _test_check_admin,
        _test_admin_page,
        _test_admin_auth,
        _test_admin_keys_api,
        _test_admin_no_adapters,
        _test_admin_with_adapters,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:  # 测试自身崩了也要记下来并继续
            _record("suite.%s 异常" % fn.__name__, False, repr(e))

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print()
    print("=" * 64)
    print("qoder_admin 测试：%d/%d 通过" % (passed, total))
    if passed != total:
        print("失败项：")
        for name, ok, detail in _results:
            if not ok:
                print("  [FAIL] %s - %s" % (name, detail))
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())