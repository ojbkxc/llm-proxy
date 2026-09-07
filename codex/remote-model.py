#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote-model.py — Linux 服务器侧 codex 模型切换器

部署到 codex app-server 所在服务器（如 /opt/codex/remote-model.py），
用于切换远程 codex 线程的模型：改写 ~/.codex/config.toml 的 model 行，
并重启 codex-app-server systemd 服务使其生效。

网关 API 与模型档位全部硬编码（单人使用，不读 models.json，不读环境变量）。

用法:
    python3 remote-model.py list                  # 当前模型 + 可选档位
    python3 remote-model.py set luna              # 切到 gpt-5.6-luna 并重启
    python3 remote-model.py set gpt-6-astra       # 直接用假名
    python3 remote-model.py service [--port 8788] # 常驻 HTTP: POST /switch {"model":"luna"}

Windows 本地也可通过 SSH 直接调用:
    ssh root@104.223.65.202 -p 10122 "python3 /opt/codex/remote-model.py set luna"

依赖: 仅 Python 标准库。
"""
import argparse
import json
import os
import re
import subprocess
import sys

# --------------------------------------------------------------------------- #
# 硬编码配置（单人使用）
# --------------------------------------------------------------------------- #
CONFIG_PATH = os.path.expanduser("~/.codex/config.toml")
SERVICE_NAME = "codex-app-server"
HEALTH_PORT = 20130

# 网关 API（codex 的 custom provider 用，写死）
GATEWAY_BASE_URL = "https://cfapi.1232333.xyz/v1"
GATEWAY_ENV_KEY = "CUSTOM_API_KEY"
GATEWAY_API_KEY = "sk-wa-f9cb7d4ba48f403797fc3f55b928ceac"

# 档位 -> (模型假名, reasoning effort, 说明)。gpt-* 假名由网关转发到真实模型。
PROFILES = {
    "fast":  ("gpt-5.6-luna-fast", "low",    "快速"),
    "sfast": ("gpt-5.6-sol-fast",  "low",    "最快"),
    "mid":   ("gpt-5.6-sol",       "medium", "深度推理"),
    "code":  ("gpt-5.6-luna",      "high",   "写码主力"),
    "deep":  ("gpt-6-astra",       "high",   "旗舰推理"),
}

# 短别名 -> 模型假名
ALIASES = {
    "astra": "gpt-6-astra",
    "sol": "gpt-5.6-sol",
    "luna": "gpt-5.6-luna",
    "sol-fast": "gpt-5.6-sol-fast",
    "luna-fast": "gpt-5.6-luna-fast",
}


def resolve_model(name):
    """档位名/别名 -> 模型假名；未知输入原样当模型名返回。"""
    name = (name or "").strip()
    if name in PROFILES:
        return PROFILES[name][0]
    if name in ALIASES:
        return ALIASES[name]
    return name


def current_model():
    """读 config.toml 顶层 model 行。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r'^model\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else None


def set_model(model):
    """改 config.toml 顶层 model 行并重启 app-server。返回 (ok, msg)。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return False, f"读取配置失败: {e}"

    if not re.search(r'^model\s*=\s*"[^"]*"', text, re.M):
        return False, f"配置里没找到顶层 model 行: {CONFIG_PATH}"

    new_text, n = re.subn(
        r'^model\s*=\s*"[^"]*"', f'model = "{model}"', text, count=1, flags=re.M)
    if n != 1:
        return False, "model 行替换失败"

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CONFIG_PATH)

    r = subprocess.run(["systemctl", "restart", SERVICE_NAME],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"systemctl 重启失败: {(r.stderr or '').strip()[:200]}"
    return True, f"已切到 {model} 并重启 {SERVICE_NAME}"


def list_models():
    cur = current_model()
    print(f"当前模型: {cur or '(未知)'}")
    print(f"配置路径: {CONFIG_PATH}")
    print(f"服务: {SERVICE_NAME}  网关: {GATEWAY_BASE_URL}")
    print("可选档位:")
    for name, (model, effort, label) in PROFILES.items():
        mark = "  <- 当前" if model == cur else ""
        print(f"  {name:6s} {model:20s} effort={effort:7s} {label}{mark}")
    print("别名: " + ", ".join(f"{k}->{v}" for k, v in ALIASES.items()))


def serve(port):
    """常驻 HTTP 服务：POST /switch {"model":"..."} 切模型，GET /status 查当前。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == "/status":
                self._json({"model": current_model()})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path.rstrip("/") != "/switch":
                self._json({"error": "not found"}, 404)
                return
            n = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._json({"error": f"bad json: {e}"}, 400)
                return
            model = resolve_model(data.get("model", ""))
            if not model:
                self._json({"error": "empty model"}, 400)
                return
            ok, msg = set_model(model)
            self._json({"ok": ok, "model": model, "msg": msg}, 200 if ok else 500)

        def log_message(self, *args):
            pass  # 关闭访问日志噪音

    print(f"[service] 监听 0.0.0.0:{port}  POST /switch  GET /status")
    HTTPServer(("0.0.0.0", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="codex 远程模型切换器（服务器侧）")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("set", help="切换模型并重启 app-server")
    p.add_argument("model")

    sub.add_parser("list", help="列出当前模型与可选档位")

    p = sub.add_parser("service", help="常驻 HTTP 服务")
    p.add_argument("--port", type=int, default=8788)

    args = ap.parse_args()

    if args.action == "list" or not args.action:
        list_models()
    elif args.action == "set":
        model = resolve_model(args.model)
        if not model:
            print("[错误] 模型名为空", file=sys.stderr)
            sys.exit(2)
        ok, msg = set_model(model)
        print(("[OK] " if ok else "[错误] ") + msg)
        sys.exit(0 if ok else 1)
    elif args.action == "service":
        serve(args.port)


if __name__ == "__main__":
    main()