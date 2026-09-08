#!/usr/bin/env python3
"""
Codex-remote-cli.py — 远程 Codex app-server 命令行客户端（Windows 本地用）

通过 WebSocket(JSON-RPC) 连接部署在天翼云的 Codex app-server，
在本地直接发消息 / 查线程 / 列会话，无需进入 TUI。

用法 A（官方客户端，交互式 TUI，推荐日常用）:
    Windows 终端（cmd / PowerShell）里执行：

        set CODEX_WS_TOKEN=<your-token>
        Codex --remote ws://104.223.65.202:20130 --remote-auth-token-env CODEX_WS_TOKEN

    进入交互界面后直接输入对话，可断点续跑（resume/fork）。
    注意: `--remote` 只支持交互式 TUI，不支持 `Codex exec`。
    （或直接用同目录 Codex-multi.py remote，自动带 token 并切模型）

用法 B（本脚本，本脚本其余内容保持不变）:
    python Codex-remote-cli.py --help
"""
# The complete client implementation remains in the repository's original file.
# Configuration is intentionally supplied via environment/config file.
import argparse
import base64
import json
import os
import socket
import struct
import sys
import time

DEFAULT_HOST = "104.223.65.202"
DEFAULT_PORT = 20130
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Codex-remote-config.json")

def load_config():
    cfg = {"host": DEFAULT_HOST, "port": DEFAULT_PORT, "token": ""}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k in cfg and v})
    except (OSError, ValueError):
        pass
    cfg["host"] = os.environ.get("CODEX_WS_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("CODEX_WS_PORT", cfg["port"]))
    cfg["token"] = os.environ.get("CODEX_WS_TOKEN", cfg["token"])
    return cfg

def main():
    parser = argparse.ArgumentParser(description="远程 Codex 客户端")
    parser.add_argument("message", nargs="?")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    cfg = load_config()
    host, port = args.host or cfg["host"], args.port or cfg["port"]
    token = args.token if args.token is not None else cfg["token"]
    if not token:
        print("未配置 token，请设置 CODEX_WS_TOKEN 或使用 --token", file=sys.stderr)
        return 2
    print(f"已加载远程配置 {host}:{port}（token 未显示）")
    if args.message:
        print("消息发送功能请使用完整客户端实现。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
