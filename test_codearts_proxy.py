# -*- coding: utf-8 -*-
"""
test_codearts_proxy.py — codearts_proxy.py 运维增强测试套件（不触网）

覆盖：
  1. API Key 鉴权（无 key / 错 key / 对 key Bearer / 对 key X-Api-Key / POST 无 key）
  2. 无鉴权模式（/v1/models 结构、/health 200）
  3. body 限制（超 8MB → 413）
  4. count_tokens（200 + input_tokens>=1）

运行：python test_codearts_proxy.py
约束：纯标准库，不触网（不连真实上游/STS，不读真实 state.vscdb）。
"""
import contextlib
import inspect
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import codearts_proxy

# 真 STS 凭证不许被测试轮换：refresh_token 是一次性的，烧掉只能重新扫码。
# renew 线程是 daemon、睡底 max(10, RENEW_SWEEP_SEC) 秒，teardown 还原全局之后它还可能再转一轮，
# 所以这里在 import 时就把池路径搬进只有本进程看得见的临时目录（显式指定过则尊重指定）。
if not os.environ.get("CODEARTS_PROXY_ACCOUNTS_FILE"):
    codearts_proxy.ACCOUNTS_FILE = os.path.join(
        tempfile.mkdtemp(prefix="cd-sandbox-"), "accounts.json")

# ── 常量 ──────────────────────────────────────────────────────────────────
TEST_KEY = "testkey123"
MAX_BODY = codearts_proxy.MAX_BODY_BYTES  # 8MB

_results = []  # [(name, ok, detail)]


def _record(name, ok, detail=""):
    _results.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print("[%s] %s%s" % (flag, name, (" - " + str(detail)[:300]) if detail else ""))


# ── 起停服务 ──────────────────────────────────────────────────────────────
def _start_server():
    """起本地 QuietServer，返回 (server, port)。"""
    srv = codearts_proxy.QuietServer(("127.0.0.1", 0), codearts_proxy.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def _stop_server(srv):
    srv.shutdown()
    srv.server_close()


# ── HTTP 客户端工具 ───────────────────────────────────────────────────────
def _get(url, headers=None, timeout=10):
    """发 GET，返回 (status_code, body_bytes)。HTTPError 也返回其 code。"""
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, body_bytes, headers=None, timeout=10):
    """发 POST，返回 (status_code, body_bytes)。"""
    req = urllib.request.Request(url, data=body_bytes, method="POST")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_oversize(port, path, body_bytes, headers=None, timeout=30):
    """用 raw socket 发 POST 超大 body：线程发送 body，主线程读响应。

    服务器 _read_body 检查 Content-Length 头即回 413，不读 body。
    用线程发送避免服务器不读 body 时客户端 send 阻塞死锁。
    Connection: close 让服务器回 413 后直接关闭连接，不 keep-alive。
    """
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        lines = [
            "POST %s HTTP/1.1" % path,
            "Host: 127.0.0.1:%d" % port,
            "Content-Length: %d" % len(body_bytes),
            "Connection: close",
        ]
        if headers:
            for k, v in headers.items():
                lines.append("%s: %s" % (k, v))
        lines.append("")
        lines.append("")
        head = "\r\n".join(lines).encode()
        s.sendall(head)

        # 线程发送 body（服务器不读时 send 会阻塞，放线程避免死锁）
        send_err = [None]

        def _send_body():
            try:
                s.sendall(body_bytes)
            except Exception as e:
                send_err[0] = e

        t_send = threading.Thread(target=_send_body, daemon=True)
        t_send.start()

        # 主线程读响应
        resp = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
            except Exception:
                break
        t_send.join(timeout=10)

        code = 0
        if resp:
            status_line = resp.split(b"\r\n", 1)[0].decode("ascii", "replace")
            parts = status_line.split()
            if len(parts) > 1:
                try:
                    code = int(parts[1])
                except ValueError:
                    pass
        return code, resp
    finally:
        try:
            s.close()
        except Exception:
            pass


# ── 1. API Key 鉴权 ──────────────────────────────────────────────────────
def test_auth_no_key_header():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        code, _ = _get("http://127.0.0.1:%d/v1/models" % port)
        _record("auth: GET /v1/models 无 Authorization → 401", code == 401, "got %d" % code)
    finally:
        _stop_server(srv)


def test_auth_wrong_bearer():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        code, _ = _get("http://127.0.0.1:%d/v1/models" % port,
                       {"Authorization": "Bearer wrongkey"})
        _record("auth: GET /v1/models 错误 Bearer → 401", code == 401, "got %d" % code)
    finally:
        _stop_server(srv)


def test_auth_correct_bearer():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        code, _ = _get("http://127.0.0.1:%d/v1/models" % port,
                       {"Authorization": "Bearer " + TEST_KEY})
        _record("auth: GET /v1/models 正确 Bearer → 200", code == 200, "got %d" % code)
    finally:
        _stop_server(srv)


def test_auth_correct_x_api_key():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        code, _ = _get("http://127.0.0.1:%d/v1/models" % port,
                       {"X-Api-Key": TEST_KEY})
        _record("auth: GET /v1/models 正确 X-Api-Key → 200", code == 200, "got %d" % code)
    finally:
        _stop_server(srv)


def test_auth_post_no_key():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        body = json.dumps({"model": "GLM-5.2",
                            "messages": [{"role": "user", "content": "hi"}]}).encode()
        code, _ = _post("http://127.0.0.1:%d/v1/messages/count_tokens" % port, body)
        _record("auth: POST /v1/messages/count_tokens 无 key → 401",
                code == 401, "got %d" % code)
    finally:
        _stop_server(srv)


# ── 2. 无鉴权模式 ────────────────────────────────────────────────────────
def test_no_auth_models():
    codearts_proxy.API_KEY = ""
    srv, port = _start_server()
    try:
        code, body = _get("http://127.0.0.1:%d/v1/models" % port)
        ok = code == 200
        detail = "got %d" % code
        if ok:
            obj = json.loads(body.decode())
            if obj.get("object") != "list":
                ok = False
                detail += "; object=%r" % obj.get("object")
            data = obj.get("data") or []
            ids = [m.get("id") for m in data]
            if "GLM-5.2" not in ids:
                ok = False
                detail += "; GLM-5.2 not in %s" % ids
        _record("no-auth: GET /v1/models → 200 + {object:list, data 含 GLM-5.2}",
                ok, detail)
    finally:
        _stop_server(srv)


def test_no_auth_health():
    codearts_proxy.API_KEY = ""
    srv, port = _start_server()
    try:
        code, body = _get("http://127.0.0.1:%d/health" % port)
        # ok 可能 True 或 False（取决于有无池/session），不强制；只看 200
        _record("no-auth: GET /health → 200", code == 200, "got %d" % code)
    finally:
        _stop_server(srv)


# ── 3. body 限制 ──────────────────────────────────────────────────────────
def test_body_oversize():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        # 构造超 8MB body：content 填充到 >8MB
        big = "x" * (MAX_BODY + 4096)
        body = json.dumps({"model": "GLM-5.2",
                            "messages": [{"role": "user", "content": big}]}).encode()
        assert len(body) > MAX_BODY, "body 应超 8MB，实际 %d" % len(body)
        # 鉴权先于 body 检查，必须带正确 key
        code, _ = _post_oversize(port, "/v1/chat/completions", body,
                                 {"Authorization": "Bearer " + TEST_KEY})
        _record("body: POST /v1/chat/completions 超 8MB → 413",
                code == 413, "got %d (body %d bytes)" % (code, len(body)))
    finally:
        _stop_server(srv)


# ── 4. count_tokens ──────────────────────────────────────────────────────
def test_count_tokens():
    codearts_proxy.API_KEY = TEST_KEY
    srv, port = _start_server()
    try:
        body = json.dumps({"model": "GLM-5.2",
                            "messages": [{"role": "user", "content": "hello world"}]}).encode()
        code, resp = _post("http://127.0.0.1:%d/v1/messages/count_tokens" % port, body,
                           {"Authorization": "Bearer " + TEST_KEY})
        ok = code == 200
        detail = "got %d" % code
        if ok:
            obj = json.loads(resp.decode())
            n = obj.get("input_tokens")
            if not isinstance(n, int) or n < 1:
                ok = False
                detail += "; input_tokens=%r" % n
        _record("count_tokens: POST → 200 + {input_tokens: N>=1}", ok, detail)
    finally:
        _stop_server(srv)


# ── 凭证生命周期：自动续期的失败分类 / 全池巡检 / 后台线程 ─────────────────
def _account(sid, exp_ahead, **kw):
    """假账号：只放续期路径真正会读的字段。"""
    a = {"label": "acct-" + sid, "ak": "ak-" + sid, "sk": "sk-" + sid, "securitytoken": "st-" + sid,
         "exp": time.time() + exp_ahead, "expires_at": "2099-01-01T00:00:00Z",
         "refresh_token": "rt-" + sid, "ot_session_id": sid,
         "loginContext": {"pkcePair": {"codeVerifier": "v"},
                          "dpopKeyPair": {"privateKeyJwk": {}, "publicKeyJwk": {}}}}
    a.update(kw)
    return a


@contextlib.contextmanager
def _sandbox(accounts, raises=None):
    """临时池 + 假 STS：不碰真池文件、不触网。退出时把模块状态原样还回去。"""
    A = codearts_proxy
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(accounts, f)
    saved = {"file": A.ACCOUNTS_FILE, "mtime": A._pool["mtime"],
             "accounts": list(A._pool["accounts"]), "retry_at": dict(A._retry_at),
             "refreshing": set(A._refreshing), "refresh": A._refresh_session,
             "print_err": A._print_err}
    calls = []

    def fake_refresh(session):
        calls.append(session.get("ot_session_id"))
        if raises is not None:
            raise raises
        sid = session["ot_session_id"]
        return {"ak": "new-" + sid, "sk": "sk", "securitytoken": "st",
                "exp": time.time() + 7200, "expires_at": "2099-01-01T02:00:00Z",
                "refresh_token": "rotated-" + sid, "ot_session_id": sid}

    A._print_err = lambda *a, **k: None
    A._refresh_session = fake_refresh
    A.ACCOUNTS_FILE = path
    A._pool["mtime"] = 0.0          # 逼 _load_pool_locked 从临时文件重读，别拿真池当测试对象
    with A._pool_lock:
        A._load_pool_locked()
    try:
        yield {"pool": lambda: A._pool["accounts"], "calls": calls, "path": path}
    finally:
        A.ACCOUNTS_FILE = saved["file"]
        A._pool["accounts"] = saved["accounts"]
        A._pool["mtime"] = saved["mtime"]
        A._retry_at.clear()
        A._retry_at.update(saved["retry_at"])
        A._refreshing.clear()
        A._refreshing.update(saved["refreshing"])
        A._refresh_session = saved["refresh"]
        A._print_err = saved["print_err"]
        try:
            os.remove(path)
        except OSError:
            pass


def test_refresh_only_kills_real_dead_chains():
    """超时/TLS/5xx 都是抖动：一次抖动不该烧掉一条 30 天的 refresh 链。"""
    cases = [
        (RuntimeError('STS 400: {"error_code":"STS5.1806","error_msg":"the refresh token has been used"}'), True),
        (RuntimeError("STS 400: kid not found"), True),
        (RuntimeError("refresh code has expired"), True),
        (RuntimeError("STS 503: upstream busy"), False),
        (urllib.error.URLError("timed out"), False),
        (ssl.SSLError("EOF occurred in violation of protocol"), False),
    ]
    bad = []
    for err, fatal in cases:
        with _sandbox([_account("x", 120)], raises=err) as sb:
            codearts_proxy._maybe_refresh(sb["pool"]()[0])
            marked = bool(sb["pool"]()[0].get("refresh_dead"))
            if marked != fatal:
                bad.append("%r → refresh_dead=%s（期望 %s）" % (err, marked, fatal))
    _record("refresh_only_kills_real_dead_chains", not bad, bad)


def test_transient_failure_leaves_pool_file_untouched():
    """判死要写盘、抖动不写盘：池文件 md5/mtime 是"续期过没"的判据，不能被抖动污染。"""
    with _sandbox([_account("y", 120)], raises=urllib.error.URLError("timed out")) as sb:
        before = os.stat(sb["path"]).st_mtime_ns
        codearts_proxy._maybe_refresh(sb["pool"]()[0])
        ok = (os.stat(sb["path"]).st_mtime_ns == before
              and not sb["pool"]()[0].get("refresh_dead")
              and "y" in codearts_proxy._retry_at)
        _record("transient_failure_leaves_pool_file_untouched", ok,
                "retry_at=%s dead=%s" % (codearts_proxy._retry_at.get("y"),
                                        sb["pool"]()[0].get("refresh_dead")))


def test_backoff_suppresses_immediate_retry():
    """退避窗口内不再重复打 STS（否则每个请求都撞一次同样的抖动）。"""
    with _sandbox([_account("z", 120)], raises=urllib.error.URLError("reset by peer")) as sb:
        acct = sb["pool"]()[0]
        codearts_proxy._maybe_refresh(acct)
        n1 = len(sb["calls"])
        codearts_proxy._maybe_refresh(sb["pool"]()[0])
        n2 = len(sb["calls"])
        codearts_proxy._retry_at.pop("z", None)
        codearts_proxy._maybe_refresh(sb["pool"]()[0])
        n3 = len(sb["calls"])
        _record("backoff_suppresses_immediate_retry", n1 == 1 and n2 == 1 and n3 == 2,
                "calls=%d/%d/%d" % (n1, n2, n3))


def test_renew_sweep_covers_whole_pool():
    """巡检要把"这次没被选中"的临期号也刷到：只刷临期的，跳过健康/已死/无链。"""
    accs = [_account("due1", 120), _account("due2", 5000), _account("healthy", 20000),
            _account("dead", 120, refresh_dead=True), _account("nolink", 120, refresh_token=None)]
    with _sandbox(accs) as sb:
        touched = codearts_proxy._sweep_due()
        got = sorted(sb["calls"])
        _record("renew_sweep_covers_whole_pool", got == ["due1", "due2"] and touched == 2,
                "calls=%s touched=%s" % (got, touched))


def test_renew_thread_switch():
    """后台线程：CODEARTS_PROXY_RENEW=0 不起；起的那条叫 renew、daemon、并且真的会巡检。"""
    A = codearts_proxy
    old_en, old_sec, old_sweep = A.RENEW_ENABLED, A.RENEW_SWEEP_SEC, A._sweep_due
    hits = []
    A._sweep_due = lambda: hits.append(1)
    A.RENEW_SWEEP_SEC = 3600      # 别让测试进程退出前再跑第二轮
    try:
        A.RENEW_ENABLED = False
        off_ok = A._start_renew_thread() is None
        A.RENEW_ENABLED = True
        t = A._start_renew_thread()
        shape_ok = t is not None and t.daemon and t.name == "renew"
        time.sleep(0.5)
        _record("renew_thread_switch", off_ok and shape_ok and hits and len(hits) >= 1,
                "off=%s shape=%s 轮次=%d" % (off_ok, shape_ok, len(hits)))
    finally:
        A.RENEW_ENABLED, A.RENEW_SWEEP_SEC, A._sweep_due = old_en, old_sec, old_sweep


def test_pool_write_keeps_file_secret():
    """自动续期写回的池必须还是 0600：真 token 不能因为默认 umask 变成 644。"""
    src = inspect.getsource(codearts_proxy._save_pool_locked)
    uses_0600 = "0o600" in src and "os.open" in src
    if os.name == "posix":
        with _sandbox([_account("m", 120)]) as sb:
            codearts_proxy._maybe_refresh(sb["pool"]()[0])   # 成功 → 写盘
            mode = oct(os.stat(sb["path"]).st_mode & 0o777)
        _record("pool_write_keeps_file_secret", mode == "0o600", "mode=%s" % mode)
    else:
        _record("pool_write_keeps_file_secret", uses_0600,
                "Windows 没有 POSIX 权限位，改为源码核对 0o600=%s" % uses_0600)


# ── 主入口 ────────────────────────────────────────────────────────────────
def main():
    tests = [
        test_auth_no_key_header,
        test_auth_wrong_bearer,
        test_auth_correct_bearer,
        test_auth_correct_x_api_key,
        test_auth_post_no_key,
        test_no_auth_models,
        test_no_auth_health,
        test_body_oversize,
        test_count_tokens,
        test_refresh_only_kills_real_dead_chains,
        test_transient_failure_leaves_pool_file_untouched,
        test_backoff_suppresses_immediate_retry,
        test_renew_sweep_covers_whole_pool,
        test_renew_thread_switch,
        test_pool_write_keeps_file_secret,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            _record(fn.__name__, False, "异常: %r" % e)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n==== 总结: %d/%d 通过 ====" % (passed, total))
    if passed != total:
        print("失败项:")
        for name, ok, detail in _results:
            if not ok:
                print("  - %s: %s" % (name, detail))
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()