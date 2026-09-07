#!/usr/bin/env python3
"""
codex-remote-cli.py — 远程 Codex app-server 命令行客户端（Windows 本地用）

通过 WebSocket(JSON-RPC) 连接部署在天翼云的 Codex app-server，
在本地直接发消息 / 查线程 / 列会话，无需进入 TUI。
线程存在服务器上，断线/关机不丢，可 resume/fork 续跑。

===========================================================
用法 A（官方客户端，交互式 TUI，推荐日常用）:
    Windows 终端（cmd / PowerShell）里执行：

        set CODEX_WS_TOKEN=3926a359a64235af4d488962f0de529e63bb6e398cc3562689f84ed44843f669
        codex --remote ws://104.223.65.202:20130 --remote-auth-token-env CODEX_WS_TOKEN

    进入交互界面后直接输入对话，可断点续跑（resume/fork）。
    注意: `--remote` 只支持交互式 TUI，不支持 `codex exec`。
    （或直接用同目录 codex-multi.py remote，自动带 token 并切模型）

===========================================================
用法 B（本脚本，命令行脚本化，适合自动化/CI）:

    python codex-remote-cli.py                     # 无参数 = 数字交互菜单
    python codex-remote-cli.py "你好，请只回复：收到"     # 发一条消息并等待回复
    python codex-remote-cli.py --thread <id> "继续"       # 在指定线程继续对话
    python codex-remote-cli.py --list                    # 列出最近线程
    python codex-remote-cli.py --config                  # 打印当前连接配置
    python codex-remote-cli.py --healthz                 # 探活

服务器会话的模型由服务器 ~/.codex/config.toml 决定（当前 kimi-k2.7-code）。
多模型需求: 服务器 /opt/codex/codex-tm.py 一个场景一个模型(glm/deep/kimi/dfast/gfast)，
GPT 假名(gpt-6-astra→glm-5.3 等)在网关层转发, dashboard cfapi.1232333.xyz → Settings 管理。

配置来源（优先级从高到低）:
    1. 命令行参数 --host/--port/--token
    2. 环境变量 CODEX_WS_HOST / CODEX_WS_PORT / CODEX_WS_TOKEN
    3. 脚本同目录 codex-remote-config.json
    4. 内置默认值（104.223.65.202:20130）

依赖: 仅 Python 标准库（socket + json + struct），无需 pip 安装。
"""

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time

# ---------------- 配置 ----------------

DEFAULT_HOST = "104.223.65.202"
DEFAULT_PORT = 20130
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex-remote-config.json")


def load_config():
    cfg = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "token": ""}
    # 内置默认 token 提示（不硬编码敏感值）
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg.update({k: v for k, v in data.items() if k in cfg and v})
        except Exception as e:
            print(f"警告: 读取 {CONFIG_FILE} 失败: {e}", file=sys.stderr)
    # 环境变量覆盖
    cfg["host"] = os.environ.get("CODEX_WS_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("CODEX_WS_PORT", cfg["port"]))
    cfg["token"] = os.environ.get("CODEX_WS_TOKEN", cfg["token"])
    return cfg


# ---------------- WebSocket 客户端 ----------------

class WSClient:
    def __init__(self, host, port, token, timeout=15):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.settimeout(timeout)
        self.buf = b""
        self.msg_id = 0
        self._open(host, port, token)

    def _open(self, host, port, token):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n\r\n"
        )
        self.s.send(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("WS 握手失败: 连接被关闭")
            resp += chunk
        head = resp.split(b"\r\n\r\n")[0].decode("utf-8", "replace")
        if "101" not in head:
            raise ConnectionError(f"WS 握手失败: {head.splitlines()[0]}")

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        frame = bytearray()
        frame.append(0x88 if opcode == 8 else 0x81)
        n = len(payload)
        if n < 126:
            frame.append(0x80 | n)
        elif n < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", n)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", n)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(bytes(frame))

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_frame(self, timeout=20):
        self.s.settimeout(timeout)
        try:
            hdr = self._recv_exact(2)
            opcode = hdr[0] & 0x0F
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            masked = hdr[1] & 0x80
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            return opcode, payload
        except socket.timeout:
            return None, b""

    def call(self, method, params=None):
        self.msg_id += 1
        msg = {"id": self.msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send_frame(1, json.dumps(msg).encode())
        return self.msg_id

    def notify(self, method, params=None):
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        self._send_frame(1, json.dumps(msg).encode())

    def close(self):
        try:
            self._send_frame(8, b"")
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# ---------------- 核心操作 ----------------

def initialize(ws):
    """JSON-RPC initialize 握手，返回 server 信息。"""
    rid = ws.call("initialize", {"protocolVersion": 1, "clientInfo": {"name": "codex-remote-cli", "version": "1.0"}})
    for _ in range(5):
        op, payload = ws.recv_frame(timeout=10)
        if op is None:
            continue
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            continue
        if msg.get("id") == rid:
            if "result" in msg:
                return msg["result"]
            if "error" in msg:
                raise RuntimeError(f"initialize 失败: {msg['error'].get('message')}")
    raise TimeoutError("initialize 超时")


def create_thread(ws, cwd="/root", approval="never", sandbox="workspace-write", ephemeral=True):
    """创建线程，返回 threadId。"""
    rid = ws.call("thread/start", {
        "cwd": cwd,
        "approvalPolicy": approval,
        "sandbox": sandbox,
        "ephemeral": ephemeral,
    })
    for _ in range(10):
        op, payload = ws.recv_frame(timeout=12)
        if op is None:
            continue
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            continue
        if msg.get("id") == rid:
            if "result" in msg:
                return msg["result"]["thread"]["id"]
            if "error" in msg:
                raise RuntimeError(f"thread/start 失败: {msg['error'].get('message')}")
    raise TimeoutError("thread/start 超时")


def send_turn(ws, thread_id, text, timeout=180):
    """发送 turn/start 并发消息，返回收到的全部文本。"""
    rid = ws.call("turn/start", {
        "threadId": thread_id,
        "input": [{"type": "text", "text": text}],
    })
    texts = []
    deadline = time.time() + timeout
    # 等 turn/start 响应（确认已受理）
    while time.time() < deadline:
        op, payload = ws.recv_frame(timeout=min(30, deadline - time.time() + 1))
        if op is None:
            continue
        if op == 8:
            break
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == rid:
            if "error" in msg:
                raise RuntimeError(f"turn/start 失败: {msg['error'].get('message')}")
            break  # 受理成功，继续收流
    # 流式收回复
    while time.time() < deadline:
        op, payload = ws.recv_frame(timeout=min(35, deadline - time.time() + 1))
        if op is None:
            continue
        if op == 8:
            break
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        m = msg.get("method", "")
        if m in ("item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            p = msg.get("params", {})
            txt = p.get("text") or p.get("delta") or p.get("content") or ""
            if txt:
                texts.append(txt)
        elif m == "turn/completed":
            break
    return "".join(texts).strip()


def list_threads(ws, limit=10):
    """列出最近线程。"""
    rid = ws.call("thread/list", {"limit": limit})
    threads = []
    for _ in range(10):
        op, payload = ws.recv_frame(timeout=12)
        if op is None:
            continue
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            continue
        if msg.get("id") == rid:
            if "result" in msg:
                data = msg["result"]
                items = data.get("threads", data if isinstance(data, list) else [])
                for t in items:
                    threads.append({
                        "id": t.get("id"),
                        "preview": (t.get("preview") or "")[:80],
                        "cwd": t.get("cwd"),
                        "model": t.get("model"),
                        "status": (t.get("status") or {}).get("type"),
                        "updatedAt": t.get("updatedAt"),
                    })
            elif "error" in msg:
                raise RuntimeError(f"thread/list 失败: {msg['error'].get('message')}")
            break
    return threads


# ---------------- CLI ----------------

def do_send(ws, message, thread_id=None):
    """发消息（新线程或续接），返回线程 id 和回复。"""
    if not thread_id:
        thread_id = create_thread(ws)
        print(f"[新线程] {thread_id}")
    print(f"[发送] {message}")
    reply = send_turn(ws, thread_id, message)
    print(f"\n[回复] {reply}")
    return thread_id


def do_list(ws):
    threads = list_threads(ws)
    if not threads:
        print("(无线程)")
    for t in threads:
        print(f"{t['id']}  {t.get('updatedAt','')}  [{t.get('status','')}] {t.get('preview','')}")


def connect(cfg):
    """建立连接并完成 initialize 握手，返回 WSClient。"""
    ws = WSClient(cfg["host"], cfg["port"], cfg["token"])
    initialize(ws)
    return ws


def menu(cfg):
    """无参数启动时：数字交互菜单。"""
    choices = [
        ("send",  "发消息（新线程）"),
        ("cont",  "在已有线程继续对话"),
        ("list",  "列出最近线程"),
        ("thread", "输入线程 ID 后发消息"),
        ("health", "探活（HTTP /healthz）"),
        ("config", "打印连接配置"),
        ("quit",  "退出"),
    ]
    while True:
        print("\n" + "=" * 48)
        print("  远程 Codex 客户端 (codex-remote-cli)")
        print("=" * 48)
        for i, (_, label) in enumerate(choices, 1):
            print(f"  {i}. {label}")
        print("=" * 48)
        try:
            s = input("  请选择 [1-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if not s.isdigit() or not (1 <= int(s) <= len(choices)):
            print("  无效输入，请重新输入")
            continue
        key = choices[int(s) - 1][0]
        if key == "quit":
            print("再见")
            return
        try:
            if key == "send":
                msg = input("  输入消息: ").strip()
                if msg:
                    with connect(cfg) as ws:
                        do_send(ws, msg)
                else:
                    print("  消息为空，取消")
            elif key == "cont":
                msg = input("  输入消息: ").strip()
                tid = input("  线程 ID (回车看最近线程): ").strip()
                if not tid:
                    with connect(cfg) as ws:
                        do_list(ws)
                    print("  请用选项 3 列出的线程 ID 重试")
                elif msg:
                    with connect(cfg) as ws:
                        do_send(ws, msg, tid)
                else:
                    print("  消息为空，取消")
            elif key == "list":
                with connect(cfg) as ws:
                    do_list(ws)
            elif key == "thread":
                tid = input("  线程 ID: ").strip()
                msg = input("  输入消息: ").strip()
                if tid and msg:
                    with connect(cfg) as ws:
                        do_send(ws, msg, tid)
                else:
                    print("  线程 ID 或消息为空，取消")
            elif key == "health":
                import urllib.request
                url = f"http://{cfg['host']}:{cfg['port']}/healthz"
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        print(f"healthz: HTTP {r.status}")
                except Exception as e:
                    print(f"healthz 失败: {e}")
            elif key == "config":
                print(json.dumps({
                    "host": cfg["host"],
                    "port": cfg["port"],
                    "token": cfg["token"][:8] + "...",
                    "服务器模型": "kimi-k2.7-code (服务器 config.toml)",
                    "GPT 假名": "gpt-6-astra→glm-5.3 / gpt-5.6-sol→deepseek-pro / gpt-5.6-luna→kimi-k2.7-code",
                    "服务器多场景": "/opt/codex/codex-tm.py",
                }, ensure_ascii=False, indent=2))
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def main():
    ap = argparse.ArgumentParser(description="远程 Codex app-server 命令行客户端")
    ap.add_argument("message", nargs="?", help="要发送的消息")
    ap.add_argument("--host", default=None, help="app-server 地址")
    ap.add_argument("--port", type=int, default=None, help="app-server 端口")
    ap.add_argument("--token", default=None, help="capability token")
    ap.add_argument("--thread", default=None, help="已有线程 ID，继续对话")
    ap.add_argument("--list", action="store_true", help="列出最近线程")
    ap.add_argument("--healthz", action="store_true", help="探活（HTTP /healthz）")
    ap.add_argument("--config", action="store_true", help="打印连接配置")
    args = ap.parse_args()

    cfg = load_config()
    if args.host: cfg["host"] = args.host
    if args.port: cfg["port"] = args.port
    if args.token: cfg["token"] = args.token

    if not cfg["token"]:
        # 尝试从默认位置读
        for p in [r"C:\Users\Administrator\.codex\ws-cap-token.txt"]:
            if os.path.exists(p):
                cfg["token"] = open(p, encoding="utf-8").read().strip()
                break
    if not cfg["token"]:
        print("错误: 未提供 token（--token / CODEX_WS_TOKEN / codex-remote-config.json / ws-cap-token.txt）", file=sys.stderr)
        sys.exit(2)

    # 无任何动作参数 → 交互菜单
    if not args.message and not args.list and not args.healthz and not args.config:
        menu(cfg)
        return

    if args.config:
        print(json.dumps({"host": cfg["host"], "port": cfg["port"], "token": cfg["token"][:8] + "..."}, ensure_ascii=False, indent=2))
        return

    if args.healthz:
        import urllib.request
        url = f"http://{cfg['host']}:{cfg['port']}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                print(f"healthz: HTTP {r.status}")
        except Exception as e:
            print(f"healthz 失败: {e}", file=sys.stderr)
            sys.exit(1)
        return

    ws = None
    try:
        ws = connect(cfg)
        print(f"[已连接] 远程 Codex app-server @ {cfg['host']}:{cfg['port']}")

        if args.list:
            do_list(ws)
            return

        if not args.message:
            ap.print_help()
            return

        do_send(ws, args.message, args.thread)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if ws:
            ws.close()


if __name__ == "__main__":
    main()