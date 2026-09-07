#!/usr/bin/env python3
"""
multi-model.py — 多模型协作编排器（真·多模型共用）

不依赖 codex 的 spawn_agent 子代理工具（CLI 0.153.x 不注入该工具，已实测），
直接通过 CF 网关 cfapi.1232333.xyz 并行调用多个真实模型：

    指挥官   glm-5.3                总指挥 / 拆解 / 汇总
    分析     deepseek-v4-pro-0813   深度分析 / 审查 / 复杂推理
    写码     kimi-k2.7-code         实现 / 编码
    快速     deepseek-v4-flash-0731 快速杂活 / 整理
    快答     glm-5.3-flash          快速问答

网关 GPT 假名与真模型等效，也可直接使用：
    gpt-6-astra → glm-5.3 / gpt-5.6-sol → deepseek-v4-pro / gpt-5.6-luna → kimi-k2.7-code

用法:
    python multi-model.py ask <模型> "问题"            单模型问答
    python multi-model.py parallel "问题"              多模型同问对比（真并行）
    python multi-model.py orchestrate "任务"           指挥官拆解 → 多模型并行执行 → 汇总
    python multi-model.py list                         列出可用模型
    python multi-model.py                              数字交互菜单

依赖: 仅 Python 标准库。API Key 取 CF_GATEWAY_KEY / CUSTOM_API_KEY / --api-key。
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows 控制台默认 GBK，遇到生僻字会抛 UnicodeEncodeError；统一重配为 UTF-8
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE_URL = "https://cfapi.1232333.xyz/v1"
UA = "codex-cli"

# 角色 → 真实模型映射（五个不同真实模型，实现真正的多模型共用）
ROLES = {
    "指挥官": {"model": "glm-5.3", "desc": "总指挥/拆解/汇总", "temp": 0.4},
    "分析":   {"model": "deepseek-v4-pro-0813", "desc": "深度分析/审查/推理", "temp": 0.3},
    "写码":   {"model": "kimi-k2.7-code", "desc": "实现/编码", "temp": 0.3},
    "快速":   {"model": "deepseek-v4-flash-0731", "desc": "快速杂活/整理", "temp": 0.5},
    "快答":   {"model": "glm-5.3-flash", "desc": "快速问答", "temp": 0.5},
}

# 单模型快捷名（ask / parallel 用）
ALIASES = {
    "glm": "glm-5.3", "deep": "deepseek-v4-pro-0813", "kimi": "kimi-k2.7-code",
    "dfast": "deepseek-v4-flash-0731", "gfast": "glm-5.3-flash",
    "astra": "gpt-6-astra", "sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna",
    "sol-fast": "gpt-5.6-sol-fast", "luna-fast": "gpt-5.6-luna-fast",
}


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def api_key():
    for k in ("CF_GATEWAY_KEY", "CUSTOM_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v
    return ""


def call(model, messages, temperature=0.4, max_tokens=4000, timeout=600):
    """调 /v1/chat/completions，返回 (content, reasoning_content)。"""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        BASE_URL + "/chat/completions", data=body, method="POST",
        headers={
            "Authorization": "Bearer " + api_key(),
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())
    data = json.loads(resp.read().decode())
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    return content, reasoning


def call_json(model, system, user, temperature=0.3, timeout=600):
    """要求模型返回纯 JSON 并解析。"""
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    content, _ = call(model, msgs, temperature=temperature, timeout=timeout)
    # 剥离可能的 markdown 代码块
    s = content.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    return json.loads(s)


def ask(model, prompt, system=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    content, reasoning = call(model, msgs)
    return content, reasoning


def parallel(prompt, models=None):
    """同题并行问多个模型，返回 {model: content}。"""
    models = models or [r["model"] for r in ROLES.values()]
    results = {}
    with ThreadPoolExecutor(max_workers=len(models)) as ex:
        futs = {ex.submit(ask, m, prompt): m for m in models}
        for f in as_completed(futs):
            m = futs[f]
            try:
                c, _ = f.result()
                results[m] = c
            except Exception as e:
                results[m] = f"[错误] {e}"
    return results


def orchestrate(task):
    """指挥官拆解任务 → 按角色并行分发给多模型 → 指挥官汇总。"""
    print(f"\n[1/3] 指挥官 glm-5.3 拆解任务...")
    plan_prompt = (
        "你是多模型团队的总指挥。把下面的任务拆成 3~5 个可并行、互不依赖的子任务，"
        "每个子任务指定一个角色（分析/写码/快速/快答，任选其一）。"
        "只输出 JSON 数组，不要任何解释。格式：\n"
        '[{"role":"分析","task":"..."},{"role":"写码","task":"..."}]\n\n'
        f"任务：{task}"
    )
    try:
        plan = call_json("glm-5.3", "你是总指挥，只输出 JSON。", plan_prompt)
    except Exception as e:
        print(f"[错误] 拆解失败: {e}")
        return
    subs = [s for s in plan if s.get("task") and s.get("role") in ROLES]
    if not subs:
        print("[错误] 拆解结果为空或格式不对")
        return
    print(f"  拆解为 {len(subs)} 个子任务:")
    for i, s in enumerate(subs, 1):
        print(f"    {i}. [{s['role']}] {s['task'][:60]}")

    print(f"\n[2/3] 并行分发给 {len({s['role'] for s in subs})} 个角色执行...")
    outputs = {}

    def run_sub(item):
        role, sub_task = item["role"], item["task"]
        model = ROLES[role]["model"]
        temp = ROLES[role]["temp"]
        sys_msg = f"你是团队里的「{role}」角色（模型 {model}）。只完成分配给你的子任务，输出简洁。"
        msgs = [{"role": "system", "content": sys_msg},
                {"role": "user", "content": sub_task}]
        t0 = time.time()
        try:
            c, _ = call(model, msgs, temperature=temp)
            return role, model, sub_task, c, round(time.time() - t0, 1), None
        except Exception as e:
            return role, model, sub_task, "", 0, str(e)

    with ThreadPoolExecutor(max_workers=len(subs)) as ex:
        futs = [ex.submit(run_sub, s) for s in subs]
        for f in as_completed(futs):
            role, model, sub_task, content, dt, err = f.result()
            outputs[(role, sub_task)] = (model, content, dt, err)
            tag = "✓" if not err else "✗"
            print(f"  {tag} [{role}] {model} ({dt}s) {sub_task[:40]}"
                  + (f"  错误: {err}" if err else ""))

    print(f"\n[3/3] 指挥官 glm-5.3 汇总...")
    report = []
    for s in subs:
        model, content, dt, err = outputs[(s["role"], s["task"])]
        body = content if (not err and content) else ("[错误] " + err)
        report.append(f"【{s['role']} / {model}】{s['task']}\n{body[:1200]}")
    summary_prompt = (
        "你是总指挥。以下是各子任务执行结果，请汇总成一份连贯的最终交付。\n"
        "要求：直接输出最终交付内容（中文），不要输出或复述你的思考过程，不要用英文思考，"
        "不要重复原始材料，简洁扼要。结构：结论 / 关键点 / 各模型分工。\n\n"
        + "\n\n".join(report)
    )
    try:
        final, _ = ask("glm-5.3", summary_prompt, system="你是总指挥。直接输出最终交付内容，不要输出你的思考过程。")
        # 若模型仍把思考当正文，则回退输出原始结果，保证一定交付
        if not final or final.startswith("The user") or "thinking" in final[:60].lower():
            final = "\n\n".join(report)
    except Exception as e:
        final = f"[汇总失败] {e}\n\n子任务原始结果：\n\n" + "\n\n".join(report)
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60)


def list_models():
    req = urllib.request.Request(
        BASE_URL + "/models",
        headers={"Authorization": "Bearer " + api_key(), "User-Agent": UA},
    )
    resp = urllib.request.urlopen(req, timeout=30, context=_ssl_ctx())
    data = json.loads(resp.read().decode())
    print(f"网关可用模型（{len(data['data'])} 个）:")
    for m in data["data"]:
        print("  -", m["id"])


def menu():
    while True:
        print("\n" + "=" * 56)
        print("  多模型协作编排器 (multi-model)")
        print("=" * 56)
        print("  1. 单模型问答 (ask)")
        print("  2. 多模型同问对比 (parallel, 真并行)")
        print("  3. 指挥官拆解→多模型并行→汇总 (orchestrate)")
        print("  4. 列出可用模型")
        print("  5. 退出")
        print("=" * 56)
        try:
            s = input("  请选择 [1-5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        try:
            if s == "1":
                m = input("  模型(glm/deep/kimi/dfast/gfast/astra/sol/luna): ").strip()
                m = ALIASES.get(m, m)
                p = input("  问题: ").strip()
                c, _ = ask(m, p)
                print("\n" + c)
            elif s == "2":
                p = input("  问题: ").strip()
                rs = parallel(p)
                for m, c in rs.items():
                    print(f"\n--- {m} ---\n{c}")
            elif s == "3":
                p = input("  任务: ").strip()
                orchestrate(p)
            elif s == "4":
                list_models()
            elif s == "5":
                print("再见")
                return
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def main():
    ap = argparse.ArgumentParser(description="多模型协作编排器")
    ap.add_argument("--api-key", default=None, help="覆盖 API Key")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("ask", help="单模型问答")
    p.add_argument("model")
    p.add_argument("prompt")

    p = sub.add_parser("parallel", help="多模型同问对比")
    p.add_argument("prompt")
    p.add_argument("--models", nargs="*", default=None, help="指定模型列表")

    p = sub.add_parser("orchestrate", help="指挥官拆解→并行→汇总")
    p.add_argument("task")

    sub.add_parser("list", help="列出可用模型")

    args = ap.parse_args()

    if args.api_key:
        os.environ["CF_GATEWAY_KEY"] = args.api_key

    if not args.action:
        menu()
        return

    if args.action == "ask":
        m = ALIASES.get(args.model, args.model)
        c, _ = ask(m, args.prompt)
        print(c)
    elif args.action == "parallel":
        models = args.models or None
        if models:
            models = [ALIASES.get(m, m) for m in models]
        rs = parallel(args.prompt, models)
        for m, c in rs.items():
            print(f"\n--- {m} ---\n{c}")
    elif args.action == "orchestrate":
        orchestrate(args.task)
    elif args.action == "list":
        list_models()


if __name__ == "__main__":
    main()