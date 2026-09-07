#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote-multi.py — 本地连服务器，跑多模型协作（同一个工作会话）

SSH 到服务器，在服务器同一个工作目录下驱动多模型团队
（分析 → 写码 → 审查 → 迭代 → 汇总），多模型直连网关，输出实时回显到本地。

一个任务 = 一个工作会话：任务上下文 + 产出的文件都落在服务器同一目录，
多个真实模型（glm-5.3 / deepseek-v4-pro / kimi-k2.7-code / flash）在其中协作。

说明：codex 原生 spawn_agent 多模型（同一个 codex 会话派生子代理）在
      CLI/app-server 环境拿不到（0.153.4 不注入该工具，已实测）。本脚本
      改用服务器上的 multi-model.py 引擎：多模型直连网关、并行协作、
      文件落同一目录，效果等同"一个会话里多个模型一起干活"。

用法:
    python remote-multi.py                  # 数字交互菜单
    python remote-multi.py team "任务"       # 真团队流水线(落地文件+审查迭代)
    python remote-multi.py orchestrate "任务" # 指挥官拆解→并行→汇总
    python remote-multi.py parallel "问题"    # 多模型同问对比
    python remote-multi.py ask glm "问题"     # 单模型问答
    python remote-multi.py list               # 列出服务器网关可用模型

配置(环境变量可覆盖，默认指向已验证的天翼云服务器):
    REMOTE_HOST=104.223.65.202   REMOTE_PORT=10122
    REMOTE_USER=root             REMOTE_PASS=...
    REMOTE_MULTI=/opt/codex/multi-model.py
    REMOTE_WORKDIR=/opt/codex
"""
import os
import shlex
import sys

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

try:
    import paramiko
except ImportError:
    print("缺少 paramiko，请先安装： pip install paramiko", file=sys.stderr)
    sys.exit(1)

HOST = os.environ.get("REMOTE_HOST", "104.223.65.202")
PORT = int(os.environ.get("REMOTE_PORT", "10122"))
USER = os.environ.get("REMOTE_USER", "root")
PASS = os.environ.get("REMOTE_PASS", "mzyxc8520#")
MULTI = os.environ.get("REMOTE_MULTI", "/opt/codex/multi-model.py")
WORKDIR = os.environ.get("REMOTE_WORKDIR", "/opt/codex")

ALIASES = {
    "astra": "gpt-6-astra", "sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna",
    "sol-fast": "gpt-5.6-sol-fast", "luna-fast": "gpt-5.6-luna-fast",
    "glm": "glm-5.3", "deep": "deepseek-v4-pro-0813", "kimi": "kimi-k2.7-code",
    "dfast": "deepseek-v4-flash-0731", "gfast": "glm-5.3-flash",
}


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=20)
    return c


def run_remote(cmd, timeout=1800):
    """在服务器执行命令，实时回显 stdout 到本地。返回 (exit_code, stdout 行列表)。"""
    c = connect()
    print(f"[SSH] {USER}@{HOST}:{PORT}")
    print(f"[远程] {cmd}\n")
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    lines = []
    while True:
        line = stdout.readline()
        if not line:
            break
        s = line.rstrip("\n")
        lines.append(s)
        print(s)
    for line in stderr:
        print(line.rstrip("\n"), file=sys.stderr)
    code = stdout.channel.recv_exit_status()
    c.close()
    return code, lines


def q(s):
    return shlex.quote(s)


def do_team(task):
    # --workdir 是全局参数，必须在 team 之前
    return run_remote(f"python3 -u {q(MULTI)} --workdir {q(WORKDIR)} team {q(task)}")


def do_orchestrate(task):
    return run_remote(f"python3 -u {q(MULTI)} orchestrate {q(task)}")


def do_parallel(prompt):
    return run_remote(f"python3 -u {q(MULTI)} parallel {q(prompt)}")


def do_ask(model, prompt):
    model = ALIASES.get(model, model)
    return run_remote(f"python3 -u {q(MULTI)} ask {q(model)} {q(prompt)}")


def do_list():
    return run_remote(f"python3 -u {q(MULTI)} list", timeout=60)


def menu():
    while True:
        print("\n" + "=" * 56)
        print("  本地连服务器 · 多模型协作 (remote-multi)")
        print("=" * 56)
        print(f"  服务器: {USER}@{HOST}:{PORT}  工作目录: {WORKDIR}")
        print("-" * 56)
        print("  1. 真团队流水线 (team, 落地文件+审查迭代) ★推荐")
        print("  2. 指挥官拆解→并行→汇总 (orchestrate)")
        print("  3. 多模型同问对比 (parallel)")
        print("  4. 单模型问答 (ask)")
        print("  5. 列出可用模型")
        print("  6. 退出")
        print("=" * 56)
        try:
            s = input("  请选择 [1-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        try:
            if s == "1":
                do_team(input("  任务: ").strip())
            elif s == "2":
                do_orchestrate(input("  任务: ").strip())
            elif s == "3":
                do_parallel(input("  问题: ").strip())
            elif s == "4":
                m = input("  模型(glm/deep/kimi/dfast/gfast/astra/sol/luna): ").strip()
                do_ask(m, input("  问题: ").strip())
            elif s == "5":
                do_list()
            elif s == "6":
                print("再见")
                return
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="本地连服务器，跑多模型协作（同一工作会话）")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("team", help="真团队流水线(落地文件+审查迭代)")
    p.add_argument("task")

    p = sub.add_parser("orchestrate", help="指挥官拆解→并行→汇总")
    p.add_argument("task")

    p = sub.add_parser("parallel", help="多模型同问对比")
    p.add_argument("prompt")

    p = sub.add_parser("ask", help="单模型问答")
    p.add_argument("model")
    p.add_argument("prompt")

    sub.add_parser("list", help="列出可用模型")

    args = ap.parse_args()

    if not args.action:
        menu()
        return

    if args.action == "team":
        sys.exit(do_team(args.task)[0])
    elif args.action == "orchestrate":
        sys.exit(do_orchestrate(args.task)[0])
    elif args.action == "parallel":
        sys.exit(do_parallel(args.prompt)[0])
    elif args.action == "ask":
        sys.exit(do_ask(args.model, args.prompt)[0])
    elif args.action == "list":
        sys.exit(do_list()[0])


if __name__ == "__main__":
    main()