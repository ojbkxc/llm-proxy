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
import json
import socket
import sys
import threading
import urllib.error
import urllib.request

import codearts_proxy

# ── 常量 ──────────────────────────────────────────────────────────────────
TEST_KEY = "testkey123"
MAX_BODY = codearts_proxy.MAX_BODY_BYTES  # 8MB

_results = []  # [(name, ok, detail)]


def _record(name, ok, detail=""):
    _results.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print("[%s] %s%s" % (flag, name, (" - " + detail) if detail else ""))


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