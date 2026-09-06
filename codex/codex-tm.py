#!/usr/bin/env python3
"""
codex-tm.py — 服务器端 Codex 持续终端场景管理器（tmux）

在 /opt/codex 下用 tmux 创建常驻会话，在会话内启动 `codex` 交互 TUI，
断 SSH 也不掉线；一个场景 = 一个模型 = 一个 tmux 会话，多场景可并行。

依赖: 仅 Python 标准库 + 系统 tmux + codex CLI（走 CF 网关 cfapi.1232333.xyz）。

用法:
    python3 /opt/codex/codex-tm.py          # 无参数 = 数字交互菜单
    python3 /opt/codex/codex-tm.py start [场景]     # 创建/复用会话并启动 codex
    python3 /opt/codex/codex-tm.py start-all        # 全部模型场景一次拉起(并行)
    python3 /opt/codex/codex-tm.py attach [场景]    # 附加到会话（退出: Ctrl+b d）
    python3 /opt/codex/codex-tm.py list              # 列出会话与 codex 运行状态
    python3 /opt/codex/codex-tm.py stop  [场景]      # 杀会话（默认全部）
    python3 /opt/codex/codex-tm.py exec  "提示词" [场景]  # 非交互跑一条 codex exec

内置模型场景（一模型一会话）:
    glm   = glm-5.3                (1.25M 上下文, 最强, 重活/总指挥)
    deep  = deepseek-v4-pro-0813    (1M, 深度推理)
    kimi  = kimi-k2.7-code          (262K, 写码专精)
    dfast = deepseek-v4-flash-0731  (最快)
    gfast = glm-5.3-flash           (快速)
    codex = 默认场景(服务器 config.toml 的模型)

GPT 假名（网关 cf-ai-gw 提供, 可在 -m / 场景配置里直接用, 与真模型等效）:
    gpt-6-astra   → glm-5.3                (指挥官位)
    gpt-5.6-sol   → deepseek-v4-pro-0813   (深度分析位)
    gpt-5.6-luna  → kimi-k2.7-code         (执行杂活位)
    gpt-5.6-sol-fast  → deepseek-v4-flash-0731
    gpt-5.6-luna-fast → glm-5.3-flash
    假名管理: dashboard cfapi.1232333.xyz → Settings → GPT 假名

配置: 环境变量 CODEX_MODEL / CODEX_SANDBOX / CODEX_APPROVAL 可覆盖默认场景；
      /opt/codex/codex-scenarios.conf 可追加自定义场景（格式: 场景名|model|sandbox|approval）。

注意: Codex CLI 0.153.4 不向 TUI/exec 注入 spawn_agent 子代理工具，
      three-tier 等编排 skill 无法在 CLI 环境运行（已实测），
      多模型并行请用本脚本的多场景方式。
"""
import argparse
import json
import os
import subprocess
import sys
import time

TMUX = "tmux"
WORKDIR = "/opt/codex"
DEFAULT_SCENE = "codex"
SCENARIOS_CONF = os.path.join(WORKDIR, "codex-scenarios.conf")
CONF_SEP = "|"

# 默认场景参数（可用环境变量覆盖）
DEFAULTS = {
    "model": os.environ.get("CODEX_MODEL", "kimi-k2.7-code"),
    "sandbox": os.environ.get("CODEX_SANDBOX", "workspace-write"),
    "approval": os.environ.get("CODEX_APPROVAL", "never"),
}

# 内置模型场景: 一模型一个 tmux 会话, 混合并行用
# 附加文件 codex-scenarios.conf 可增删（格式: 场景名|model|sandbox|approval）
# model 列可填真实模型名, 也可填 GPT 假名（如 gpt-6-astra, 网关层转发）
BUILTIN_SCENES = {
    "glm":   {"model": "glm-5.3",                "sandbox": "workspace-write", "approval": "never"},
    "deep":  {"model": "deepseek-v4-pro-0813",   "sandbox": "workspace-write", "approval": "never"},
    "kimi":  {"model": "kimi-k2.7-code",         "sandbox": "workspace-write", "approval": "never"},
    "dfast": {"model": "deepseek-v4-flash-0731", "sandbox": "workspace-write", "approval": "never"},
    "gfast": {"model": "glm-5.3-flash",          "sandbox": "workspace-write", "approval": "never"},
    # GPT 假名场景（与上面真名等效, 供喜欢 GPT 风格名字时用）
    "astra": {"model": "gpt-6-astra",            "sandbox": "workspace-write", "approval": "never"},
    "sol":   {"model": "gpt-5.6-sol",            "sandbox": "workspace-write", "approval": "never"},
    "luna":  {"model": "gpt-5.6-luna",          "sandbox": "workspace-write", "approval": "never"},
}


def sh(cmd):
    """运行命令并返回 (code, stdout)。"""
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def sh_tmux(args):
    """封装 tmux 调用，忽略失败返回值。"""
    return sh([TMUX] + args)


def load_scenarios():
    """内置 5 模型场景 + codex-scenarios.conf 追加覆盖（场景名|model|sandbox|approval）。"""
    scenes = {k: dict(v) for k, v in BUILTIN_SCENES.items()}
    scenes[DEFAULT_SCENE] = dict(DEFAULTS)  # 默认 codex 场景（kimi-k2.7-code）
    if os.path.exists(SCENARIOS_CONF):
        with open(SCENARIOS_CONF, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(CONF_SEP)
                name = parts[0].strip()
                if not name:
                    continue
                model = (parts[1] if len(parts) > 1 else "").strip() or DEFAULTS["model"]
                sandbox = (parts[2] if len(parts) > 2 else "").strip() or DEFAULTS["sandbox"]
                approval = (parts[3] if len(parts) > 3 else "").strip() or DEFAULTS["approval"]
                scenes[name] = {"model": model, "sandbox": sandbox, "approval": approval}
    return scenes


def session_exists(name):
    code, _ = sh_tmux(["has-session", "-t", name])
    return code == 0


def is_codex_running(name):
    code, out = sh_tmux(["list-panes", "-t", name, "-F", "#{pane_current_command}"])
    return code == 0 and "codex" in out


def start(scene_name):
    scenes = load_scenarios()
    if scene_name not in scenes:
        print(f"[错误] 场景不存在: {scene_name} (可用: {', '.join(scenes)})", file=sys.stderr)
        sys.exit(1)
    s = scenes[scene_name]

    if not session_exists(scene_name):
        # 创建会话，不 attach（-d），工作目录设为 /opt/github
        sh_tmux(["new-session", "-d", "-s", scene_name, "-c", WORKDIR])
        print(f"[创建] tmux 会话 {scene_name}")
    else:
        print(f"[复用] tmux 会话 {scene_name}")

    if is_codex_running(scene_name):
        print(f"[跳过] codex 已在会话 {scene_name} 中运行")
        return

    # 在会话中启动 codex 交互 TUI（幂等：codex 已在跑则跳过）
    # 显式 export CUSTOM_API_KEY + 覆盖模型/沙箱/审批，然后 exec codex
    env = "export CUSTOM_API_KEY=$CUSTOM_API_KEY; "
    if os.environ.get("CUSTOM_API_KEY"):
        env = "export CUSTOM_API_KEY=%s; " % os.environ["CUSTOM_API_KEY"]
    run = (
        f"export CUSTOM_API_KEY={os.environ.get('CUSTOM_API_KEY', '')}; "
        f"cd {WORKDIR} && codex -c model=\"{s['model']}\" "
        f"-c sandbox_mode=\"{s['sandbox']}\" -c approval_policy=\"{s['approval']}\""
    )
    sh_tmux(["send-keys", "-t", scene_name, run, "Enter"])
    print(f"[启动] {run}")
    print(f"[提示] 场景 {scene_name}: model={s['model']} sandbox={s['sandbox']} approval={s['approval']}")


def attach(scene_name):
    if not session_exists(scene_name):
        print(f"[错误] 会话不存在: {scene_name} (先 start)", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("TMUX"):
        sh_tmux(["switch-client", "-t", scene_name])
    else:
        sh_tmux(["attach", "-t", scene_name])


def stop(scene_name=None):
    scenes = load_scenarios()
    targets = [scene_name] if scene_name else list(scenes)
    for name in targets:
        if session_exists(name):
            sh_tmux(["kill-session", "-t", name])
            print(f"[停止] {name}")
        else:
            print(f"[跳过] 会话不存在: {name}")


def list_sessions():
    scenes = load_scenarios()
    print("Codex 场景列表:")
    for name in scenes:
        status = "未启动"
        if session_exists(name):
            status = "运行中" if is_codex_running(name) else "会话在/无codex"
        s = scenes[name]
        print(f"  {name:<12} {status:<14} model={s['model']} sandbox={s['sandbox']} approval={s['approval']}")


def exec_msg(prompt, scene_name):
    """非交互: 在 tmux 会话外直接跑 codex exec。"""
    scenes = load_scenarios()
    s = scenes.get(scene_name, scenes[DEFAULT_SCENE])
    cmd = [
        "codex", "exec",
        "-c", f"model=\"{s['model']}\"",
        "-c", f"sandbox_mode=\"{s['sandbox']}\"",
        "-c", f"approval_policy=\"{s['approval']}\"",
        prompt,
    ]
    print(f"[exec] {' '.join(cmd)}")
    p = subprocess.run(cmd, capture_output=False)
    return p.returncode


def pick_scene(prompt_txt):
    """让用户从场景里选一个，返回场景名。"""
    scenes = load_scenarios()
    names = list(scenes)
    print("可选场景:")
    for i, n in enumerate(names, 1):
        s = scenes[n]
        print(f"  {i}. {n}  (model={s['model']}, sandbox={s['sandbox']}, approval={s['approval']})")
    s = input(f"{prompt_txt} [1-{len(names)}] 默认 1: ").strip()
    if not s.isdigit() or not (1 <= int(s) <= len(names)):
        return names[0]
    return names[int(s) - 1]


def start_all():
    """一次拉起全部内置模型场景（多模型并行，各占一个 tmux 会话）。"""
    for name in BUILTIN_SCENES:
        print()
        start(name)


def menu():
    """无参数启动时：数字交互菜单。"""
    choices = [
        ("start",   "启动 Codex 持续终端（选模型场景）"),
        ("startall", "全部模型场景一起启动 (glm/deep/kimi/dfast/gfast 并行)"),
        ("attach",  "附加到会话"),
        ("list",    "列出会话与状态"),
        ("stop",    "停止会话"),
        ("exec",    "非交互跑一条 codex exec"),
        ("quit",    "退出"),
    ]
    while True:
        print("\n" + "=" * 50)
        print("  Codex 持续终端管理器 (codex-tm)")
        print("=" * 50)
        for i, (_, label) in enumerate(choices, 1):
            print(f"  {i}. {label}")
        print("=" * 50)
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
            if key == "start":
                start(pick_scene("启动哪个场景"))
            elif key == "startall":
                start_all()
            elif key == "attach":
                attach(pick_scene("附加到哪个场景"))
            elif key == "list":
                list_sessions()
            elif key == "stop":
                stop(pick_scene("停止哪个场景"))
            elif key == "exec":
                prompt = input("  输入提示词: ").strip()
                if prompt:
                    exec_msg(prompt, pick_scene("用哪个场景"))
                else:
                    print("  提示词为空，取消")
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def main():
    ap = argparse.ArgumentParser(description="Codex 持续终端场景管理器 (tmux)")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("start", help="创建/复用会话并启动 codex")
    p.add_argument("scene", nargs="?", default=DEFAULT_SCENE)

    p = sub.add_parser("attach", help="附加到会话")
    p.add_argument("scene", nargs="?", default=DEFAULT_SCENE)

    p = sub.add_parser("stop", help="停止会话(默认全部)")
    p.add_argument("scene", nargs="?", default=None)

    p = sub.add_parser("list", help="列出场景与状态")
    p.add_argument("--json", action="store_true", help="JSON 输出")

    p = sub.add_parser("start-all", help="一次拉起全部模型场景(并行)")
    p.add_argument("--no-attach", action="store_true", help="只启动不附加")

    p = sub.add_parser("exec", help="非交互跑一条 codex exec")
    p.add_argument("prompt")
    p.add_argument("scene", nargs="?", default=DEFAULT_SCENE)

    args = ap.parse_args()

    # 无参数 → 交互菜单
    if not args.action:
        menu()
        return

    if args.action == "start":
        start(args.scene)
    elif args.action == "attach":
        attach(args.scene)
    elif args.action == "stop":
        stop(args.scene)
    elif args.action == "list":
        list_sessions()
    elif args.action == "start-all":
        start_all()
    elif args.action == "exec":
        sys.exit(exec_msg(args.prompt, args.scene))


if __name__ == "__main__":
    main()