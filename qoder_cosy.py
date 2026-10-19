# -*- coding: utf-8 -*-
"""Qoder CN 的 Cosy 签名与请求体编码（纯 Python 标准库实现）。

客户端把签名写在 qoder_auth_wasm_bg.wasm 里；这里是逐字节还原的等价实现：
  - 请求体 Encode=1：自定义字母表 base64 + 分块重排
  - generate_runtime_auth_fields：AES-128-CBC(info) + RSA-1024 PKCS#1 v1.5(key)
  - COSY 头：part0(base64 JSON) + "." + md5(part0\\nkey\\ndate\\nbody\\npath)
所有常量取自客户端 wasm 的只读数据段（见 .reverse/qoder/）。
"""
import base64
import hashlib
import json
import os
import time
import uuid

COSY_VERSION = "1.1.69"
GATEWAY = "https://gateway.qoder.com.cn"
INFER_PATH = "/api/v2/service/pro/sse/agent_chat_generation"
INFER_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
MODEL_LIST_PATH = "/api/v2/model/list"

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
        path = INFER_PATH
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
        base = (endpoint or GATEWAY).rstrip("/")
        return {"url": base + "/algo" + path + "?" + INFER_QUERY,
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
        url = (endpoint or GATEWAY).rstrip("/") + "/algo" + rel
        if query:
            url += "?" + query
        out = {"url": url, "headers": headers}
        if body is not None:
            out["body"] = encoded
        return out
