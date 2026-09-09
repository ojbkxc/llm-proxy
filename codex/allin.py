#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
allin.py — 一键入口：同目录调用 proxy.py + 自动认证 + 多模型共用

不需要 pip 装任何东西，只依赖 Python 标准库 + 同目录两个文件：
    proxy.py        本地 8787 代理（认证复用 Workspace 登录态，token 挂了自动开浏览器）
    multi-model.py  多模型协作引擎（gpt-* 假名，网关层转发，直连代理）

allin.py 负责把流程串起来：
    1. 检查 8787 有没有代理在跑，没有就后台拉起 proxy.py
    2. 探活 /health 等认证：token 过期时 proxy 自己打开浏览器 SSO，
       你在浏览器点登录，它拿到新 token 后自动继续
    3. 认证就绪后进入多模型共用

用法:
    python allin.py                      # 起代理+等认证 → 多模型数字菜单
    python allin.py --auto "任务" --hours 24   # 起代理+等认证 → 无人值守连跑
    python allin.py team "任务"          # 起代理+等认证 → 直接跑一次 team
    python allin.py ask luna "问题"      # 起代理+等认证 → 单模型问答
    python allin.py list                 # 起代理+等认证 → 列模型
    （其余子命令直接透传给 multi-model.py）

    MCP 服务器（codex 会话内多模型协作）:
    python mcp_server.py --self-check    # 自检：确认 8 个 MCP 工具就绪
    # 部署时自动注册到 ~/.codex/config.toml，codex 会话内即可调用
    # multi_team_start / multi_task_status / multi_task_result 等工具
    # 跳过注册：python deploy_ai_cli.py --skip-mcp

环境变量: WS_PROXY_PORT 可改代理端口（默认 8787，与 proxy.py 一致）。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_PY = os.path.join(HERE, "proxy.py")
MULTI_PY = os.path.join(HERE, "multi-model.py")
PORT = int(os.environ.get("WS_PROXY_PORT", "8787"))
HEALTH_URL = "http://127.0.0.1:%d/health" % PORT


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def ensure_proxy():
    """检查代理端口；没跑就后台拉起同目录的 proxy.py。"""
    if port_open():
        print("[allin] 本地代理已在运行 %s" % HEALTH_URL)
        return True
    if not os.path.exists(PROXY_PY):
        print("[allin] 错误: 找不到 %s" % PROXY_PY, file=sys.stderr)
        return False
    print("[allin] 代理未运行，后台启动 proxy.py (端口 %d)...")
    try:
        kwargs = {"cwd": HERE}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([sys.executable, PROXY_PY], **kwargs)
    except Exception as e:
        print("[allin] 启动 proxy.py 失败: %s" % e, file=sys.stderr)
        return False
    return True


def wait_proxy_ready(timeout=600):
    """等代理就绪并认证通过。token 失效时 proxy 内部会自动开浏览器，这里提示用户。

    说明：/health 会触发 proxy 的会话读取 —— token 临期自动 refresh，
    refresh 也失效则 _browser_login_locked() 打开浏览器 SSO（最长等 180s）。
    所以单个 health 请求可能阻塞较久，整体 timeout 给足。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open():
            break
        time.sleep(0.5)
    else:
        print("[allin] 超时: 代理端口 %d 未打开" % PORT, file=sys.stderr)
        return False

    print("[allin] 正在探活认证...")
    print("[allin] 若 token 已过期，会自动打开浏览器 —— 请在浏览器完成 SSO 登录")
    warned = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=200) as r:
                if r.status == 200:
                    try:
                        info = json.loads(r.read().decode("utf-8", "replace"))
                        user = info.get("user", "?")
                    except Exception:
                        user = "?"
                    print("[allin] 认证 OK，用户 %s" % user)
                    return True
        except urllib.error.HTTPError as e:
            if not warned:
                print("[allin] 认证尚未完成 (HTTP %s)，等待中..." % e.code)
                warned = True
        except Exception as e:
            if not warned:
                print("[allin] 探活异常 (%s)，继续等待..." % e)
                warned = True
        time.sleep(1)
    print("[allin] 认证等待超时 (%ds)，请确认浏览器已完成登录后重试" % timeout, file=sys.stderr)
    return False


def main():
    args = sys.argv[1:]

    # --auto 是 allin 自己的开关，其余透传给 multi-model.py
    auto = False
    rest = []
    for a in args:
        if a == "--auto":
            auto = True
        else:
            rest.append(a)

    if not ensure_proxy():
        sys.exit(1)
    if not wait_proxy_ready():
        sys.exit(2)

    # --auto: 转成 multi-model.py auto 子命令
    if auto:
        rest = ["auto"] + rest
    if not rest:
        # 无参数 → 数字菜单
        pass

    print("[allin] 认证就绪，进入多模型协作...\n")
    sys.exit(subprocess.call([sys.executable, MULTI_PY] + rest))


if __name__ == "__main__":
    main()