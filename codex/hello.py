#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hello.py — 零依赖问候/冒烟测试入口

只依赖 Python 标准库，用于快速验证：
    1. Python 环境可用、中文 UTF-8 输出不乱码（Windows 下无 UnicodeEncodeError）
    2. 本地 8787 代理是否在线（可选，--health）

用法:
    python hello.py            # 打印“你好”
    python hello.py --health   # 打印“你好”，并探测本地代理 /health 状态
    python hello.py --help     # 帮助

退出码:
    0  正常（--health 时表示代理在线）
    1  --health 时代理离线/不可达
    2  参数错误

环境变量: WS_PROXY_PORT 可改代理端口（默认 8787，与 proxy.py/allin.py 一致）。
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.request

# 与项目其他入口（allin.py / multi-model.py）一致的 Windows UTF-8 约定，
# 避免中文在 GBK 控制台下触发 UnicodeEncodeError。
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PORT = int(os.environ.get("WS_PROXY_PORT", "8787"))
HEALTH_URL = "http://127.0.0.1:%d/health" % PORT

USAGE = """\
用法:
    python hello.py            打印“你好”（冒烟测试 Python 环境与 UTF-8 输出）
    python hello.py --health   同时探测本地代理健康状态（默认端口 8787）

环境变量:
    WS_PROXY_PORT              代理端口，默认 8787
"""


def port_open(timeout=1.0):
    """只测端口是否有人监听，不触发 /health 的认证流程。"""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
            return True
    except OSError:
        return False


def check_health(timeout=5.0):
    """探测本地代理 /health。

    返回 (online, detail)：
      online=True  代理在运行（含端口开但 /health 暂未应答的情况）
      online=False 端口无人监听，代理离线

    注意：/health 会触发 proxy 的会话读取，token 临期时可能自动开浏览器
    SSO 而长时间不应答，所以这里用短超时 + 端口预检区分“离线”与“在线但
    认证未就绪”，避免把等待登录误报成离线。
    """
    if not port_open():
        return False, "端口 %d 无监听" % PORT
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            user = "?"
            try:
                info = json.loads(body)
                if isinstance(info, dict):
                    user = info.get("user", "?")
            except Exception:
                pass
            return True, "HTTP %s，用户 %s" % (getattr(r, "status", 200), user)
    except urllib.error.HTTPError as e:
        # 端口在、返回非 200：代理在跑，多半是认证还没就绪
        return True, "HTTP %s（代理在运行，认证可能未就绪）" % e.code
    except Exception as e:
        # 端口开但 /health 迟迟不应答：通常正在等浏览器 SSO 登录
        return True, "/health 暂未应答（%s），可能正在等待浏览器认证" % e


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    if "-h" in args or "--help" in args:
        print(USAGE, end="")
        return 0

    unknown = [a for a in args if a != "--health"]
    if unknown:
        print("[hello] 未识别的参数: %s（支持: --health，-h/--help）" % " ".join(unknown),
              file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

    # 主输出：问候 + UTF-8 冒烟
    print("你好")

    if "--health" in args:
        online, detail = check_health()
        if online:
            print("[hello] 代理在线 %s (%s)" % (HEALTH_URL, detail))
            return 0
        print("[hello] 代理离线 %s (%s)" % (HEALTH_URL, detail))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
