#!/usr/bin/env python3
"""
trae_login.py — TRAE SOLO 登录：生成链接 → 浏览器登录 → 粘贴回调 → 换 token → 落盘。

用法：
  python trae_login.py              # 交互式登录
  python trae_login.py --export     # 从现有 storage.json 导出（无需重新登录）

产出：auths/trae-{uid}.json（明文，兼容 traework2api 格式）
"""
import json, os, sys, time, secrets, urllib.parse, urllib.request, urllib.error

CLIENT_ID = "en1oxy7wnw8j9n"
APP_VERSION = "0.1.43"
API_HOST = "https://api.trae.com.cn"
AUTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auths")

# 信封解密常量（同 trae_proxy.py）
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


def _decrypt_auth_info(encoded: str) -> dict:
    import base64, hashlib
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    envelope = base64.b64decode(encoded)
    if len(envelope) <= 38 or envelope[:6] != ENVELOPE_HEADER:
        raise RuntimeError("无效信封")
    random_key = envelope[6:38]
    secret = bytes(l ^ r for l, r in zip(LEFT_SECRET, RIGHT_SECRET))
    derived = hashlib.sha512(hashlib.sha512(random_key).digest() + secret).digest()
    key, iv = derived[:16], derived[16:32]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(envelope[38:]) + dec.finalize()
    plain = padded[:-padded[-1]]
    return json.loads(plain[64:].decode("utf-8"))


def _decode_jwt_exp(token: str):
    import base64
    try:
        p64 = token.split(".")[1].replace("-", "+").replace("_", "/")
        p64 += "=" * (-len(p64) % 4)
        return int(json.loads(base64.b64decode(p64)).get("exp") or 0) or None
    except Exception:
        return None


def export_from_storage():
    """从现有 storage.json 导出 trae-{uid}.json。"""
    storage_path = os.path.join(os.environ.get("APPDATA", ""), "TRAE SOLO CN",
                                "User", "globalStorage", "storage.json")
    if not os.path.exists(storage_path):
        print("找不到 storage.json：%s" % storage_path)
        sys.exit(1)
    storage = json.load(open(storage_path, "r", encoding="utf-8"))
    encoded = storage.get("iCubeAuthInfo://icube.cloudide")
    if not encoded:
        print("storage.json 无 iCubeAuthInfo（未登录？）")
        sys.exit(1)
    info = _decrypt_auth_info(encoded)
    token = info.get("token", "")
    refresh = info.get("refreshToken", "")
    uid = str(info.get("userId", ""))
    exp = _decode_jwt_exp(token)
    if not token or not uid:
        print("解密失败或字段缺失")
        sys.exit(1)
    _save_auth(uid, token, refresh, exp, info.get("host", ""), storage)
    print("导出成功: auths/trae-%s.json" % uid)
    print("  token 有效期: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "unknown")


def _save_auth(uid, token, refresh, exp, host, storage):
    os.makedirs(AUTH_DIR, exist_ok=True)
    machine_id = storage.get("telemetry.machineId", "")
    device_id = ""
    for k in storage:
        if k.startswith("iCubeAuthInfo://icube-dc:"):
            device_id = k[len("iCubeAuthInfo://icube-dc:"):]
            break
    auth = {
        "account": {"uid": uid, "enterpriseId": "", "nickname": ""},
        "auth": {
            "accessToken": token, "refreshToken": refresh,
            "expiresAt": exp or 0, "domain": "trae.cn",
            "apiHost": API_HOST, "machineId": machine_id, "deviceId": device_id,
        },
    }
    path = os.path.join(AUTH_DIR, "trae-%s.json" % uid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2, ensure_ascii=False)


def interactive_login():
    """生成登录链接 → 用户粘贴回调 → 换 token → 落盘。"""
    machine_id = secrets.token_hex(16)
    device_id = secrets.token_hex(16)
    trace_id = secrets.token_hex(8)

    params = {
        "login_version": "1", "auth_from": "solo", "login_channel": "native_ide",
        "plugin_version": "2.3.62834", "auth_type": "local", "client_id": CLIENT_ID,
        "redirect": "0", "login_trace_id": trace_id,
        "auth_callback_url": "http://127.0.0.1:18080/authorize",
        "machine_id": machine_id, "device_id": device_id,
        "x_device_id": device_id, "x_machine_id": machine_id,
        "x_device_brand": "PC", "x_device_type": "PC",
        "x_os_version": "1.0", "x_app_version": APP_VERSION, "x_app_type": "stable",
    }
    url = "https://www.trae.cn/authorization?" + urllib.parse.urlencode(params)

    print("=" * 60)
    print("  TRAE SOLO 登录")
    print("=" * 60)
    print()
    print("步骤：")
    print("  1. 在浏览器打开下面链接，用手机号/验证码登录")
    print("  2. 登录成功后浏览器会跳到打不开的 127.0.0.1 地址")
    print("  3. 复制浏览器地址栏的完整链接，粘贴到下面")
    print()
    print("登录链接：")
    print()
    print("  " + url)
    print()
    callback = input("粘贴回调链接: ").strip()
    if not callback:
        print("未输入，已取消")
        sys.exit(1)

    # 解析回调
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback).query)
    refresh_token = (qs.get("refreshToken") or [""])[0]

    def parse_json_param(raw):
        if not raw:
            return {}
        for val in (raw, urllib.parse.unquote(raw)):
            try:
                obj = json.loads(val)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
        return {}

    user_info = parse_json_param((qs.get("userInfo") or [""])[0])
    user_jwt = parse_json_param((qs.get("userJwt") or [""])[0])
    uid = str(user_info.get("UserID") or "")
    nickname = str(user_info.get("ScreenName") or "")

    if not refresh_token:
        refresh_token = user_jwt.get("RefreshToken", "")
    if not refresh_token:
        print("回调链接缺少 refreshToken")
        sys.exit(1)

    # ExchangeToken
    body = json.dumps({"ClientID": CLIENT_ID, "RefreshToken": refresh_token,
                       "ClientSecret": "-", "UserID": ""}).encode()
    req = urllib.request.Request(API_HOST + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Trae/" + APP_VERSION)
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read().decode()).get("Result", {})
    token = result.get("Token", "")
    if not token:
        print("ExchangeToken 失败")
        sys.exit(1)
    new_refresh = result.get("RefreshToken", refresh_token)
    expires_at = int(result.get("TokenExpireAt") or 0)
    if expires_at > 1e12:
        expires_at //= 1000

    # GetUserInfo
    try:
        req2 = urllib.request.Request(API_HOST + "/cloudide/api/v3/trae/GetUserInfo",
                                      data=json.dumps({"ReqSource": "IDE", "IDEVersion": APP_VERSION}).encode(),
                                      method="POST")
        req2.add_header("Content-Type", "application/json")
        req2.add_header("x-cloudide-token", token)
        ui = json.loads(urllib.request.urlopen(req2, timeout=15).read().decode()).get("Result", {})
        if ui.get("UserID"):
            uid = str(ui["UserID"])
            nickname = str(ui.get("ScreenName", nickname))
    except Exception:
        pass

    if not uid:
        print("未能获取 uid")
        sys.exit(1)

    # 落盘
    storage = {"telemetry.machineId": machine_id}
    _save_auth(uid, token, new_refresh, expires_at, "", storage)

    print()
    print("=" * 60)
    print("  登录完成！")
    print("  UID:      %s" % uid)
    print("  Nickname: %s" % nickname)
    print("  Token:    %s..." % token[:20])
    print("  有效期:   %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at)) if expires_at else "unknown"))
    print("  文件:     auths/trae-%s.json" % uid)
    print("=" * 60)


if __name__ == "__main__":
    if "--export" in sys.argv:
        export_from_storage()
    else:
        interactive_login()