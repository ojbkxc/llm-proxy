#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workspace (Midea) 认证管理脚本（独立于 proxy.py，不修改 proxy.py / test_proxy.py）。

用途：
  python ws_auth.py status    只读展示当前登录用户、accessToken / refreshToken 有效期，
                              以及两处存储（opencode.db 与 VSCode 存储）是否一致。
  python ws_auth.py refresh   用 refreshToken 调刷新端点，成功后把新 session 双写：
                              - 写回 VSCode 存储（含新 refreshToken，保证下次还能刷）
                              - 写回 opencode.db（proxy.py 读它，无需改动即可继续工作）

原理（逆向自 ws.exe 主服务 MideaAuthenticationService）：
  - 完整 session（含 refreshToken）存在 VSCode 存储：
      %APPDATA%\\Workspace\\User\\globalStorage\\state.vscdb 的 ItemTable，key="midea.authentication.session"
    密文 = "v10" + AES-256-GCM(nonce=12, ct+tag)。AES 密钥来自
      %APPDATA%\\Workspace\\Local State 的 os_crypt.encrypted_key（DPAPI 解开）。
  - 刷新端点：GET https://workspace-prd.midea.com/api/login-server/v1/auth/refresh-token
    请求头 Refresh-Token: <refreshToken>，响应 body 含新 access_token / refresh_token / user / *_info。
  - proxy.py 读 opencode.db 的 workspace_session 表（XOR 加密），只有 {accessToken, id, label}。

依赖：cryptography（AES-GCM，本机已装）。主代理 proxy.py 保持零第三方依赖。
"""

import os
import sys
import json
import time
import base64
import sqlite3
import ctypes
import ctypes.wintypes
import hashlib
import secrets
import urllib.request
import urllib.error

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── 路径常量 ──────────────────────────────────────────────────────────────
HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
WS_APP_DIR = os.path.join(APPDATA, "Workspace")
VSCDB = os.path.join(WS_APP_DIR, "User", "globalStorage", "state.vscdb")
LOCAL_STATE = os.path.join(WS_APP_DIR, "Local State")
OPENCODE_DB = os.path.join(HOME, ".local", "share", "workspace-code-prd", "opencode.db")

SESSION_KEY = "midea.authentication.session"
REFRESH_URL = "https://workspace-prd.midea.com/api/login-server/v1/auth/refresh-token"

# ── DPAPI ─────────────────────────────────────────────────────────────────
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_unprotect(blob: bytes) -> bytes:
    """用当前用户凭据解 DPAPI 密文（CryptUnprotectData）。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_blob = _DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise OSError(ctypes.WinError())
    data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    kernel32.LocalFree(out_blob.pbData)
    return data


# ── VSCode 存储读写（AES-256-GCM）────────────────────────────────────────
def _get_aes_key() -> bytes:
    with open(LOCAL_STATE, "r", encoding="utf-8") as f:
        ls = json.load(f)
    enc_b64 = ls.get("os_crypt", {}).get("encrypted_key")
    if not enc_b64:
        raise RuntimeError("Local State 缺少 os_crypt.encrypted_key")
    raw = base64.b64decode(enc_b64)
    if raw[:5] != b"DPAPI":
        raise RuntimeError("encrypted_key 前缀异常（非 DPAPI）")
    return _dpapi_unprotect(raw[5:])


def _vscdb_get(key: str) -> bytes:
    con = sqlite3.connect("file:%s?mode=ro" % VSCDB.replace("\\", "/"), uri=True, timeout=2)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    buf = json.loads(row[0])
    return bytes(buf["data"])


def _vscdb_set(key: str, value: bytes):
    payload = json.dumps({"type": "Buffer", "data": list(value)})
    con = sqlite3.connect(VSCDB, timeout=2)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO ItemTable(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, payload),
        )
        con.commit()
    finally:
        con.close()


def _aes_decrypt(blob: bytes, aes_key: bytes) -> bytes:
    if blob[:3] != b"v10":
        raise RuntimeError("session 密文前缀异常（非 v10）")
    cipher = blob[3:]
    nonce, ct_tag = cipher[:12], cipher[12:]
    return AESGCM(aes_key).decrypt(nonce, ct_tag, None)


def _aes_encrypt(plain: bytes, aes_key: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    ct_tag = AESGCM(aes_key).encrypt(nonce, plain, None)
    return b"v10" + nonce + ct_tag


def read_vscode_session() -> dict:
    """读并解密完整 session（含 refreshToken）。不存在则返回 None。"""
    blob = _vscdb_get(SESSION_KEY)
    if not blob:
        return None
    aes_key = _get_aes_key()
    return json.loads(_aes_decrypt(blob, aes_key).decode("utf-8"))


def write_vscode_session(session: dict):
    aes_key = _get_aes_key()
    blob = _aes_encrypt(json.dumps(session, ensure_ascii=False).encode("utf-8"), aes_key)
    _vscdb_set(SESSION_KEY, blob)


# ── opencode.db 读写（XOR，与 proxy.py 一致）────────────────────────────
def _xor(key: str, raw: bytes) -> bytes:
    return bytes(b ^ ord(key[i % len(key)]) for i, b in enumerate(raw))


def read_opencode_session() -> dict:
    """读 opencode.db 的 workspace_session，返回 {accessToken, id, label} 或 None。"""
    if not os.path.exists(OPENCODE_DB):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % OPENCODE_DB.replace("\\", "/"), uri=True, timeout=2)
    try:
        rows = con.execute(
            "SELECT id, key, encrypted, timestamp FROM workspace_session WHERE id='default'"
        ).fetchall()
    finally:
        con.close()
    for _rid, key, enc_hex, _ts in rows:
        try:
            return json.loads(_xor(key, bytes.fromhex(enc_hex)).decode("utf-8"))
        except Exception:
            continue
    return None


def write_opencode_session(access_token: str, label: str, uid: str, refresh_token: str = None):
    """把 {accessToken, id, label, refreshToken?} XOR 加密写回 workspace_session（id='default'）。"""
    if not os.path.exists(OPENCODE_DB):
        raise RuntimeError("找不到 %s，请先安装并登录 Workspace 编辑器" % OPENCODE_DB)
    con = sqlite3.connect(OPENCODE_DB, timeout=5)
    try:
        con.execute("BEGIN IMMEDIATE")
        old = con.execute(
            "SELECT time_created FROM workspace_session WHERE id='default'"
        ).fetchone()
        time_created = old[0] if old else int(time.time() * 1000)
        key = secrets.token_urlsafe(48)
        payload = {"accessToken": access_token, "id": uid, "label": label}
        if refresh_token:
            payload["refreshToken"] = refresh_token
        payload = json.dumps(payload, ensure_ascii=False)
        enc = _xor(key, payload.encode("utf-8")).hex()
        now_ms = int(time.time() * 1000)
        now_s = int(time.time())
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


# ── JWT 解析 ─────────────────────────────────────────────────────────────
def _decode_jwt(token: str) -> dict:
    payload_b64 = token.split(".")[1].replace("-", "+").replace("_", "/")
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.b64decode(payload_b64).decode("utf-8"))


# ── 刷新 ─────────────────────────────────────────────────────────────────
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_refresh(refresh_token: str) -> dict:
    """GET refresh-token 端点，返回响应 body。"""
    req = urllib.request.Request(REFRESH_URL, method="GET")
    req.add_header("Refresh-Token", refresh_token)
    with _opener.open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("success"):
        raise RuntimeError("刷新失败：success=false（refreshToken 可能已失效）")
    return data.get("body", {})


# ── 命令 ─────────────────────────────────────────────────────────────────
def _fmt_expiry(ms):
    if not ms:
        return "未知"
    dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))
    left = (ms / 1000 - time.time()) / 3600
    return "%s（剩余 %.1f 小时）" % (dt, left)


def cmd_status():
    print("── opencode.db（proxy.py 实际读取）──")
    oc = read_opencode_session()
    if not oc:
        print("  （无有效记录）")
    else:
        token = oc.get("accessToken") or ""
        try:
            jwt = _decode_jwt(token)
            exp = jwt.get("exp", 0) * 1000
            user = jwt.get("preferred_username", "")
        except Exception:
            exp, user = None, ""
        print("  用户      : %s (id=%s)" % (oc.get("label", ""), oc.get("id", "")))
        print("  accessToken 长度 : %d" % len(token))
        print("  accessToken 有效期 : %s" % _fmt_expiry(exp))

    print("── VSCode 存储（含 refreshToken）──")
    try:
        vs = read_vscode_session()
    except Exception as e:
        print("  读取失败：%s" % e)
        vs = None
    if not vs:
        print("  （无有效 session）")
    else:
        acct = vs.get("midea.authentication.session.account", {})
        at = vs.get("midea.authentication.session.accessToken", "")
        rt = vs.get("midea.authentication.session.refreshToken", "")
        at_exp = vs.get("midea.authentication.session.accessTokenExpiryTime")
        rt_exp = vs.get("midea.authentication.session.refreshTokenExpiryTime")
        print("  用户      : %s (id=%s)" % (acct.get("label", ""), acct.get("id", "")))
        print("  accessToken 长度 : %d，refreshToken 长度 : %d" % (len(at), len(rt)))
        print("  accessToken 有效期 : %s" % _fmt_expiry(at_exp))
        print("  refreshToken 有效期 : %s" % _fmt_expiry(rt_exp))

    # 一致性
    if oc and vs:
        same = (oc.get("accessToken") == vs.get("midea.authentication.session.accessToken"))
        print("── 一致性 ──")
        print("  两处 accessToken 一致 : %s" % ("是" if same else "否（opencode 侧可能未同步）"))


def cmd_refresh():
    vs = read_vscode_session()
    if not vs:
        raise SystemExit("VSCode 存储无 session，请先登录 Workspace 编辑器")
    rt = vs.get("midea.authentication.session.refreshToken")
    if not rt:
        raise SystemExit("session 缺少 refreshToken")

    print("刷新前 refreshToken 有效期 : %s" % _fmt_expiry(vs.get("midea.authentication.session.refreshTokenExpiryTime")))
    print("正在调用刷新端点 ...")
    body = call_refresh(rt)

    access_token = (body.get("access_token") or "").replace("Bearer", "").replace("bearer", "").strip()
    refresh_token = body.get("refresh_token") or ""
    user = body.get("user") or {}
    uid = user.get("uid", vs.get("midea.authentication.session.account", {}).get("id", ""))
    cn = user.get("cn", vs.get("midea.authentication.session.account", {}).get("label", ""))
    at_exp = (body.get("access_token_info") or {}).get("expire_time", 0) * 1000
    rt_exp = (body.get("refresh_token_info") or {}).get("expire_time", 0) * 1000

    if not access_token:
        raise RuntimeError("刷新响应缺少 access_token")

    # 更新 VSCode session（含新 refreshToken）
    new_vs = dict(vs)
    new_vs["midea.authentication.session.accessToken"] = access_token
    new_vs["midea.authentication.session.refreshToken"] = refresh_token
    new_vs["midea.authentication.session.account"] = {"id": uid, "label": cn}
    new_vs["midea.authentication.session.lastUpdateTime"] = int(time.time() * 1000)
    new_vs["midea.authentication.session.accessTokenExpiryTime"] = at_exp
    new_vs["midea.authentication.session.refreshTokenExpiryTime"] = rt_exp
    new_vs["midea.authentication.session.id"] = str(secrets.token_hex(16))

    write_vscode_session(new_vs)
    print("已写回 VSCode 存储（含新 refreshToken）")

    # 写回 opencode.db（proxy.py 读取）
    write_opencode_session(access_token, cn, uid, refresh_token)
    print("已写回 opencode.db（proxy.py 可继续工作，含 refreshToken 供自动续期）")

    print("── 刷新完成 ──")
    print("  用户      : %s (id=%s)" % (cn, uid))
    print("  accessToken 有效期 : %s" % _fmt_expiry(at_exp))
    print("  refreshToken 有效期 : %s" % _fmt_expiry(rt_exp))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("status", "refresh"):
        print(__doc__)
        raise SystemExit(1)
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    else:
        cmd_refresh()


if __name__ == "__main__":
    main()