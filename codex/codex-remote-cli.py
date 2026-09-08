#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex-remote-cli.py — 远程 Codex app-server 命令行客户端（零依赖，纯标准库）

通过 WebSocket + JSON-RPC 连接远程 Codex app-server（协议由 `codex app-server`
暴露，见 .proto/ 下生成的 schema），在本地直接管理远程会话 / 发消息 / 切模型，
无需进入 TUI。

协议要点（与 `codex app-server generate-json-schema` 输出一致）:
    - 握手:  客户端发 initialize 请求 -> 服务端回 initialize 响应 -> 客户端发
              `{"method":"initialized"}` 通知（无 id）。
    - 请求:   {"id": <int>, "method": "...", "params": {...}}
    - 响应:   {"id": <int>, "result": {...}} 或 {"id": <int>, "error": {...}}
    - 通知:   {"method": "...", "params": {...}}（服务端主动推，如流式增量）
    - 认证:   WebSocket Upgrade 头带 `Authorization: Bearer <token>`

用法:
    python codex-remote-cli.py list                         # 列出远程会话
    python codex-remote-cli.py info                         # 远程 server 信息
    python codex-remote-cli.py model                        # 列出可用模型
    python codex-remote-cli.py model set gpt-6-astra        # 切模型（写 config）
    python codex-remote-cli.py start --cwd /opt/Codex "任务描述"
    python codex-remote-cli.py send --thread <id> "追加消息"
    python codex-remote-cli.py steer --thread <id> --turn <id> "中途改方向"
    python codex-remote-cli.py interrupt --thread <id> --turn <id>
    python codex-remote-cli.py read --thread <id>
    python codex-remote-cli.py items --thread <id> [--turn <id>]

★ 中途介入不打断 vs 打断：
    steer      在 turn 进行中注入新输入，turn 保持 inProgress 继续跑，不会中断。
    interrupt  显式打断当前 turn，状态变为 interrupted。
    想「改方向但别停」用 steer；想「叫停」才用 interrupt。

凭据优先级: --token > 环境变量 CODEX_WS_TOKEN > codex-remote-config.json 的 token。
连接地址优先级: --host/--port > CODEX_WS_HOST/CODEX_WS_PORT > config 文件。
"""
import argparse
import base64
import hashlib
import json
import os
import select
import socket
import struct
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "codex-remote-config.json")
DEFAULT_HOST = "104.223.65.202"
DEFAULT_PORT = 20130
CLIENT_NAME = "codex-remote-cli"
CLIENT_VERSION = "0.1.0"

# 服务端主动推送、客户端需关注的流式/状态通知
STREAM_METHODS = {
    "item/agentMessage/delta",
    "item/started",
    "item/completed",
    "turn/started",
    "turn/completed",
    "thread/status/changed",
    "item/reasoning/textDelta",
    "item/reasoning/summaryTextDelta",
    "error",
    "warning",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _b64(s: str) -> str:
    """WebSocket Sec-WebSocket-Accept / Key 用的 base64。"""
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def load_config() -> dict:
    cfg = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "token": ""}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k in cfg and v})
    except (OSError, ValueError):
        pass
    if os.environ.get("CODEX_WS_HOST"):
        cfg["host"] = os.environ["CODEX_WS_HOST"]
    if os.environ.get("CODEX_WS_PORT"):
        try:
            cfg["port"] = int(os.environ["CODEX_WS_PORT"])
        except ValueError:
            pass
    if os.environ.get("CODEX_WS_TOKEN"):
        cfg["token"] = os.environ["CODEX_WS_TOKEN"]
    return cfg


class WSProtocolError(Exception):
    pass


class WSClose(Exception):
    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket closed: {code} {reason}")


# --------------------------------------------------------------------------- #
# WebSocket 帧层（RFC 6455，仅客户端所需部分）
# --------------------------------------------------------------------------- #
class WebSocket:
    """同步、单连接、掩码客户端的极简 RFC6455 实现。"""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._closed = False
        self._frag_opcode = None
        self._frag_buf = bytearray()
        self._recv_buf = b""

    # -- 帧编解码 --------------------------------------------------------- #
    @staticmethod
    def _encode_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
        b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
        mask_bit = 0x80  # 客户端帧必须掩码
        n = len(payload)
        header = bytearray([b0])
        if n < 126:
            header.append(mask_bit | n)
        elif n < (1 << 16):
            header.append(mask_bit | 126)
            header += struct.pack(">H", n)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", n)
        mask_key = os.urandom(4)
        header += mask_key
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return bytes(header) + masked

    @staticmethod
    def _decode_frame(buf: bytes) -> tuple:
        """解析一帧，返回 (fin, opcode, payload, 消费字节数)。数据不完整返回 None。"""
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        off = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack(">H", buf[2:4])[0]
            off = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack(">Q", buf[2:10])[0]
            off = 10
        mask_key = b""
        if masked:
            if len(buf) < off + 4:
                return None
            mask_key = buf[off:off + 4]
            off += 4
        if len(buf) < off + n:
            return None
        payload = buf[off:off + n]
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload, off + n

    # -- 发送 ------------------------------------------------------------- #
    def send_text(self, text: str):
        self._sock.sendall(self._encode_frame(0x1, text.encode("utf-8")))

    def send_ping(self, data: bytes = b""):
        self._sock.sendall(self._encode_frame(0x9, data))

    def send_close(self, code: int = 1000, reason: str = ""):
        try:
            body = struct.pack(">H", code) + reason.encode("utf-8")
            self._sock.sendall(self._encode_frame(0x8, body))
        except OSError:
            pass

    # -- 接收 ------------------------------------------------------------- #
    def _read_exact(self, n: int) -> bytes:
        while len(self._recv_buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WSClose(1006, "unexpected EOF")
            self._recv_buf += chunk
        out, self._recv_buf = self._recv_buf[:n], self._recv_buf[n:]
        return out

    def recv_frame(self) -> tuple:
        """返回 (opcode, payload)。文本/二进制被完整重组；控制帧即时处理。"""
        while True:
            # 先用已有缓冲尝试解析，不足再收
            while True:
                r = self._decode_frame(self._recv_buf)
                if r is None:
                    break
                fin, opcode, payload, used = r
                self._recv_buf = self._recv_buf[used:]
                if opcode == 0x8:  # close
                    code = 1000
                    reason = ""
                    if len(payload) >= 2:
                        code = struct.unpack(">H", payload[:2])[0]
                        reason = payload[2:].decode("utf-8", "replace")
                    raise WSClose(code, reason)
                if opcode == 0x9:  # ping -> pong
                    self._sock.sendall(self._encode_frame(0xA, payload))
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode == 0x0:  # continuation
                    if self._frag_opcode is None:
                        raise WSProtocolError("unexpected continuation frame")
                    self._frag_buf += payload
                    if fin:
                        op, self._frag_opcode, self._frag_buf = \
                            self._frag_opcode, None, bytearray()
                        return op, bytes(self._frag_buf)
                    continue
                if not fin:  # 起始分片
                    self._frag_opcode = opcode
                    self._frag_buf = bytearray(payload)
                    continue
                return opcode, payload
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WSClose(1006, "unexpected EOF")
            self._recv_buf += chunk

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.send_close()
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass


def handshake(host: str, port: int, token: str = "", timeout: float = 15.0) -> WebSocket:
    """建立 TCP + WebSocket Upgrade 握手，返回已连接的 WebSocket。"""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(None)  # 交给上层 select 控制超时
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = [
        f"GET / HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token:
        req.append(f"Authorization: Bearer {token}")
    req.append("")
    req.append("")
    sock.sendall(("\r\n".join(req)).encode("ascii"))

    # 读响应头
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSProtocolError("握手时连接被关闭")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0] if lines else ""
    if " 101" not in status:
        raise WSProtocolError(f"握手失败: {status}")
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    accept = headers.get("sec-websocket-accept", "")
    expect = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii"))
        .digest()).decode("ascii")
    if accept != expect:
        raise WSProtocolError("Sec-WebSocket-Accept 校验失败（可能被中间设备篡改）")

    ws = WebSocket(sock)
    ws._recv_buf = rest  # 握手后可能已有帧数据
    return ws


# --------------------------------------------------------------------------- #
# JSON-RPC 客户端
# --------------------------------------------------------------------------- #
class CodexRemote:
    def __init__(self, host: str, port: int, token: str = ""):
        self.host = host
        self.port = port
        self.token = token
        self.ws = None
        self._next_id = 1
        self._lock = threading.Lock()
        self.server_info = {}

    # -- 连接 ------------------------------------------------------------- #
    def connect(self):
        self.ws = handshake(self.host, self.port, self.token)
        init = self._request("initialize", {
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "capabilities": {},
        })
        self.server_info = {
            "codexHome": init.get("codexHome"),
            "platformFamily": init.get("platformFamily"),
            "platformOs": init.get("platformOs"),
            "userAgent": init.get("userAgent"),
        }
        self.ws.send_text(json.dumps({"method": "initialized"}))
        return self.server_info

    def close(self):
        if self.ws:
            self.ws.close()
            self.ws = None

    # -- 请求原语 --------------------------------------------------------- #
    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self.ws.send_text(json.dumps({
                "id": req_id, "method": method, "params": params or {},
            }, ensure_ascii=False))
        while True:
            opcode, payload = self.ws.recv_frame()
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("id") != req_id:
                self._dispatch_notification(msg)  # 夹带的推送不丢
                continue
            if "error" in msg:
                err = msg["error"]
                raise WSProtocolError(
                    f"{method} 失败: {err.get('message','')} (code {err.get('code')})")
            return msg.get("result", {})

    def _dispatch_notification(self, msg: dict):
        method = msg.get("method", "")
        params = msg.get("params", {}) if isinstance(msg.get("params"), dict) else {}
        if method in STREAM_METHODS:
            self._on_stream(method, params)

    # -- 流式回调（可被子类/CLI 覆盖） ------------------------------------ #
    def _on_stream(self, method: str, params: dict):
        # 默认静默；CLI 会覆盖以打印增量
        pass

    # -- 高层方法 --------------------------------------------------------- #
    def list_threads(self, limit: int = 50, **filters) -> list:
        p = {"limit": limit}
        p.update(filters)
        return self._request("thread/list", p).get("data", [])

    def thread_read(self, thread_id: str, include_turns: bool = False) -> dict:
        return self._request("thread/read", {
            "threadId": thread_id, "includeTurns": include_turns,
        }).get("thread", {})

    def thread_turns(self, thread_id: str, limit: int = 20) -> list:
        return self._request("thread/turns/list", {
            "threadId": thread_id, "limit": limit,
        }).get("data", [])

    def thread_items(self, thread_id: str, turn_id: str = None, limit: int = 100) -> list:
        p = {"threadId": thread_id, "limit": limit}
        if turn_id:
            p["turnId"] = turn_id
        return self._request("thread/items/list", p).get("data", [])

    def thread_start(self, cwd: str, model: str = None,
                     base_instructions: str = None, ephemeral: bool = False) -> dict:
        p = {"cwd": cwd}
        if model:
            p["model"] = model
        if base_instructions:
            p["baseInstructions"] = base_instructions
        if ephemeral:
            p["ephemeral"] = True
        r = self._request("thread/start", p)
        return r  # 含 thread + model 等

    def turn_start(self, thread_id: str, text: str, model: str = None) -> dict:
        p = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if model:
            p["model"] = model
        return self._request("turn/start", p).get("turn", {})

    def turn_steer(self, thread_id: str, expected_turn_id: str, text: str) -> dict:
        return self._request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": [{"type": "text", "text": text}],
        })

    def turn_interrupt(self, thread_id: str, turn_id: str) -> dict:
        return self._request("turn/interrupt", {
            "threadId": thread_id, "turnId": turn_id,
        })

    def model_list(self, include_hidden: bool = True) -> list:
        return self._request("model/list",
                             {"includeHidden": include_hidden}).get("data", [])

    def model_set(self, model: str) -> dict:
        return self._request("config/value/write", {
            "keyPath": "model", "value": model, "mergeStrategy": "replace",
        })

    def config_read(self) -> dict:
        return self._request("config/read", {"includeLayers": True})

    # -- 组合：发起 turn 并等待完成（流式打印由 _on_stream 处理） ---------- #
    def run_turn(self, thread_id: str, text: str, model: str = None,
                 timeout: float = None) -> dict:
        """发 turn/start 并阻塞到 turn/completed 或 error。返回完整 turn。"""
        turn = self.turn_start(thread_id, text, model)
        turn_id = turn.get("id")
        deadline = (time.time() + timeout) if timeout else None
        while True:
            if deadline and time.time() > deadline:
                raise TimeoutError(f"等待 turn {turn_id} 完成超时")
            remaining = (deadline - time.time()) if deadline else None
            try:
                opcode, payload = self.ws.recv_frame()
            except WSClose:
                raise
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            method = msg.get("method", "")
            params = msg.get("params", {}) if isinstance(msg.get("params"), dict) else {}
            if "id" in msg:
                continue  # 响应帧，非本流程关注
            self._dispatch_notification(msg)
            if method == "turn/completed":
                t = params.get("turn", {})
                if t.get("id") == turn_id:
                    return t
            elif method == "error":
                if params.get("turnId") == turn_id:
                    err = params.get("error", {})
                    raise WSProtocolError(
                        f"turn 出错: {json.dumps(err, ensure_ascii=False)[:300]}")
            elif method == "thread/status/changed":
                st = params.get("status", {})
                if st.get("type") in ("idle", "systemError") and \
                        params.get("threadId") == thread_id:
                    # 兜底：如果 turn 结束通知丢失，读到 idle 也返回
                    return {"id": turn_id, "status": "idle"}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt_thread(t: dict) -> str:
    name = t.get("name") or t.get("preview") or "(未命名)"
    if len(name) > 48:
        name = name[:48] + "…"
    st = t.get("status", {})
    status = st.get("type", "?") if isinstance(st, dict) else "?"
    return f"{t.get('id','')[:12]:12s}  {t.get('model','') or '-':18s}  " \
           f"{status:10s}  {t.get('cwd','') or '-':24s}  {name}"


def _text_of_item(item: dict) -> str:
    if item.get("type") == "agentMessage":
        return item.get("text", "")
    return ""


class StreamPrinter:
    """把流式通知转成终端输出。"""

    def __init__(self, quiet: bool = False, json_out: bool = False):
        self.quiet = quiet
        self.json_out = json_out
        self._cur_item = None
        self._cur_turn = None

    def on_stream(self, method: str, params: dict):
        if self.json_out:
            sys.stdout.write(json.dumps({"method": method, "params": params},
                                        ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return
        if self.quiet:
            return
        if method == "turn/started":
            t = params.get("turn", {})
            self._cur_turn = t.get("id")
            print(f"\n── turn {t.get('id','')[:12]} 开始 ──", flush=True)
        elif method == "item/agentMessage/delta":
            sys.stdout.write(params.get("delta", ""))
            sys.stdout.flush()
        elif method == "item/reasoning/textDelta":
            sys.stdout.write(params.get("text", ""))
            sys.stdout.flush()
        elif method == "turn/completed":
            print("\n── turn 完成 ──", flush=True)
        elif method == "error":
            err = params.get("error", {})
            print(f"\n[错误] {json.dumps(err, ensure_ascii=False)[:300]}",
                  file=sys.stderr, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="远程 Codex app-server 命令行客户端（WebSocket JSON-RPC）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="turn 等待超时秒数（默认 300）")
    ap.add_argument("--json", action="store_true",
                    help="以原始 JSON 输出流式事件（机器可读）")
    ap.add_argument("--quiet", action="store_true", help="不打印流式增量")

    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="列出远程会话")
    p_list.add_argument("--limit", type=int, default=50)

    sub.add_parser("info", help="远程 server 信息（initialize 结果）")

    p_start = sub.add_parser("start", help="新建会话，可选立即发首条消息")
    p_start.add_argument("--cwd", required=True, help="远程工作目录（绝对路径）")
    p_start.add_argument("--model", default=None)
    p_start.add_argument("--ephemeral", action="store_true")
    p_start.add_argument("message", nargs="?")

    p_send = sub.add_parser("send", help="向空闲会话追加一条消息")
    p_send.add_argument("--thread", required=True)
    p_send.add_argument("--model", default=None)
    p_send.add_argument("message")

    p_steer = sub.add_parser(
        "steer", help="中途介入：给进行中的 turn 注入新指令，不打断")
    p_steer.add_argument("--thread", required=True)
    p_steer.add_argument("--turn", required=True, dest="turn_id",
                         help="当前活跃 turn 的 id（不匹配会被服务端拒绝）")
    p_steer.add_argument("message")

    p_int = sub.add_parser(
        "interrupt", help="打断进行中的 turn（状态变为 interrupted）")
    p_int.add_argument("--thread", required=True)
    p_int.add_argument("--turn", required=True, dest="turn_id")

    p_read = sub.add_parser("read", help="读会话元数据")
    p_read.add_argument("--thread", required=True)
    p_read.add_argument("--turns", action="store_true", help="一并列出最近 turns")

    p_items = sub.add_parser("items", help="列出会话/某 turn 的 items")
    p_items.add_argument("--thread", required=True)
    p_items.add_argument("--turn", dest="turn_id", default=None)

    p_model = sub.add_parser("model", help="列出可用模型")
    p_model.add_argument("action", nargs="?", choices=["list", "set"], default="list")
    p_model.add_argument("value", nargs="?")

    args = ap.parse_args(argv)

    cfg = load_config()
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    token = args.token if args.token is not None else cfg["token"]

    if not args.cmd:
        ap.print_help()
        return 0

    # 建立连接
    client = CodexRemote(host, port, token)
    try:
        client.connect()
    except (OSError, WSProtocolError) as e:
        print(f"[连接失败] {e}", file=sys.stderr)
        return 2

    printer = StreamPrinter(quiet=args.quiet, json_out=args.json)
    client._on_stream = printer.on_stream

    try:
        if args.cmd == "info":
            print(json.dumps(client.server_info, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "list":
            threads = client.list_threads(limit=args.limit)
            print(f"共 {len(threads)} 个会话:")
            for t in threads:
                print("  " + _fmt_thread(t))
            return 0

        if args.cmd == "model":
            if args.action == "set":
                if not args.value:
                    print("请提供模型名: model set <name>", file=sys.stderr)
                    return 2
                r = client.model_set(args.value)
                print(f"[OK] 模型已写为 {args.value}")
                return 0
            models = client.model_list()
            print("可用模型:")
            for m in models:
                if isinstance(m, dict):
                    print("  " + json.dumps(m, ensure_ascii=False))
                else:
                    print(f"  {m}")
            return 0

        if args.cmd == "start":
            r = client.thread_start(args.cwd, model=args.model,
                                    ephemeral=args.ephemeral)
            thread = r.get("thread", {})
            tid = thread.get("id", "")
            print(f"[OK] 会话已创建: {tid}")
            print(f"     模型: {r.get('model','')}  目录: {thread.get('cwd','')}")
            if args.message:
                client.run_turn(tid, args.message, model=args.model,
                                timeout=args.timeout)
            return 0

        if args.cmd == "send":
            client.run_turn(args.thread, args.message, model=args.model,
                            timeout=args.timeout)
            return 0

        if args.cmd == "steer":
            client.turn_steer(args.thread, args.turn_id, args.message)
            print("[OK] 已发送转向指令", flush=True)
            return 0

        if args.cmd == "interrupt":
            client.turn_interrupt(args.thread, args.turn_id)
            print("[OK] 已发送打断指令")
            return 0

        if args.cmd == "read":
            thread = client.thread_read(args.thread, include_turns=args.turns)
            print(_fmt_thread(thread))
            if args.turns:
                for t in client.thread_turns(args.thread):
                    st = t.get("status", "")
                    print(f"  - turn {t.get('id','')[:12]} status={st}")
            return 0

        if args.cmd == "items":
            items = client.thread_items(args.thread, args.turn_id)
            for it in items:
                ty = it.get("type", "?")
                txt = _text_of_item(it)
                if ty == "agentMessage":
                    print(f"[{it.get('id','')[:8]}] {txt}")
                elif ty == "userMessage":
                    content = it.get("content", [])
                    parts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    print(f"[用户] {' '.join(parts)}")
                else:
                    print(f"[{ty}] {json.dumps(it, ensure_ascii=False)[:200]}")
            return 0

        print(f"未知命令: {args.cmd}", file=sys.stderr)
        return 2
    except WSClose as e:
        print(f"[连接关闭] code={e.code} {e.reason}", file=sys.stderr)
        return 1
    except WSProtocolError as e:
        print(f"[协议错误] {e}", file=sys.stderr)
        return 1
    except TimeoutError as e:
        print(f"[超时] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[中断]", file=sys.stderr)
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())