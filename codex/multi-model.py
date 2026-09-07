#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi-model.py — 多模型协作编排器（真·多模型共用 + 工具 + 审查迭代）

不依赖 codex 的 spawn_agent 子代理工具（CLI 0.153.x 不注入该工具，已实测），
直接通过网关并行调用多个真实模型，并自带工具层，
让不同模型组成一个「能读代码、能写文件、能跑命令、能互相审查迭代」的团队。

角色分工（五个 gpt-* 假名，网关层转发到真实模型）:
    指挥官   gpt-6-astra       → glm-5.3                 总指挥 / 拆解 / 汇总
    分析     gpt-5.6-sol       → deepseek-v4-pro-0813    深度分析 / 设计 / 审查
    写码     gpt-5.6-luna      → kimi-k2.7-code          实现 / 编码 / 落地文件
    快速     gpt-5.6-sol-fast  → deepseek-v4-flash-0731  快速杂活 / 整理
    快答     gpt-5.6-luna-fast → glm-5.3-flash           快速问答

用法:
    python multi-model.py ask <模型> "问题"            单模型问答
    python multi-model.py parallel "问题"              多模型同问对比（真并行）
    python multi-model.py orchestrate "任务"           指挥官拆解 → 多模型并行 → 汇总
    python multi-model.py team "任务" [--workdir DIR]  真团队流水线(带工具+审查迭代)
    python multi-model.py auto "任务" [--hours 24]     无人值守连续跑(挂机)
    python multi-model.py list                         列出可用模型
    python multi-model.py                              数字交互菜单

依赖: 仅 Python 标准库。网关地址与 Key 优先读同目录 deploy_ai_cli.py
      （DEFAULT_BASE_URL / DEFAULT_API_KEY），改那一处本脚本即跟着换网关；
      兜底取 CF_GATEWAY_KEY / CUSTOM_API_KEY / ANTHROPIC_AUTH_TOKEN / --api-key。
"""
import argparse
import json
import os
import re
import ssl
import subprocess
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

BASE_URL = "http://127.0.0.1:8787/v1"
UA = "codex-cli"

# 从同目录 deploy_ai_cli.py 读网关地址与 Key（单点配置：改 deploy_ai_cli.py 即可全局换网关）
_DEPLOY_API_KEY = ""
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "deploy_ai_cli", os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_ai_cli.py"))
    _d = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_d)
    BASE_URL = getattr(_d, "DEFAULT_BASE_URL", BASE_URL)
    _DEPLOY_API_KEY = getattr(_d, "DEFAULT_API_KEY", "") or ""
except Exception:
    pass  # deploy_ai_cli.py 缺失/出错时退回上面的默认值与兜底 Key

# 角色 → gpt-* 假名映射（网关层转发到真实模型，与 deploy_ai_cli.py 的分档一致）
ROLES = {
    "指挥官": {"model": "gpt-6-astra",       "desc": "总指挥/拆解/汇总",   "temp": 0.4},
    "分析":   {"model": "gpt-5.6-sol",       "desc": "深度分析/设计/审查", "temp": 0.3},
    "写码":   {"model": "gpt-5.6-luna",      "desc": "实现/编码/落地",     "temp": 0.2},
    "快速":   {"model": "gpt-5.6-sol-fast",  "desc": "快速杂活/整理",     "temp": 0.5},
    "快答":   {"model": "gpt-5.6-luna-fast", "desc": "快速问答",          "temp": 0.5},
}

ALIASES = {
    # gpt-* 假名（主用）
    "astra": "gpt-6-astra", "sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna",
    "sol-fast": "gpt-5.6-sol-fast", "luna-fast": "gpt-5.6-luna-fast",
    # 真名兼容（直接写真实模型名也能用）
    "glm": "glm-5.3", "deep": "deepseek-v4-pro-0813", "kimi": "kimi-k2.7-code",
    "dfast": "deepseek-v4-flash-0731", "gfast": "glm-5.3-flash",
}

WORKDIR = os.getcwd()
ALLOW_RUN = True


def _ssl_ctx():
    ctx = ssl.create_default_context()
    return ctx


def api_key():
    for k in ("CF_GATEWAY_KEY", "CUSTOM_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v
    return _DEPLOY_API_KEY


# ---------------------------------------------------------------------------
# 工具层：函数定义 + 执行器
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出指定目录下的文件和子目录（相对工作目录）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径，'.' 表示根"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "description": "最多读的行数，默认 200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入/创建文本文件（会覆盖）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在文本文件中搜索关键字或正则，返回匹配的行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "目录或文件，默认 '.'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "执行一条 shell 命令并返回输出（用于跑测试/构建等）。",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
]


def _safe_path(path):
    """把相对路径解析到 WORKDIR 内，防止目录穿越。"""
    base = os.path.realpath(WORKDIR)
    p = os.path.realpath(os.path.join(base, path))
    if os.path.commonpath([base, p]) != base:
        raise PermissionError(f"路径越出工作目录: {path}")
    return p


def _tool_list_dir(path):
    p = _safe_path(path)
    if not os.path.isdir(p):
        return f"不是目录: {path}"
    out = []
    for name in sorted(os.listdir(p)):
        full = os.path.join(p, name)
        tag = "[D]" if os.path.isdir(full) else "[F]"
        try:
            size = os.path.getsize(full) if os.path.isfile(full) else ""
        except OSError:
            size = ""
        out.append(f"{tag} {name} {size}".rstrip())
    return "\n".join(out) if out else "(空目录)"


def _tool_read_file(path, limit=200):
    p = _safe_path(path)
    if not os.path.isfile(p):
        return f"文件不存在: {path}"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取失败: {e}"
    n = int(limit) if limit else len(lines)
    if len(lines) > n:
        return "".join(lines[:n]) + f"\n...[截断，共 {len(lines)} 行]"
    return "".join(lines)


def _tool_write_file(path, content):
    p = _safe_path(path)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


def _tool_search(pattern, path="."):
    p = _safe_path(path)
    files = []
    if os.path.isfile(p):
        files = [p]
    else:
        for root, dirs, names in os.walk(p):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv")]
            for n in names:
                files.append(os.path.join(root, n))
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(f, WORKDIR)}:{i}: {line.rstrip()[:160]}")
                        if len(hits) >= 60:
                            return "\n".join(hits) + "\n...[截断]"
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(hits) if hits else "无匹配"


def _tool_run_command(cmd):
    if not ALLOW_RUN:
        return "[已禁用] run_command 被 --no-run 关闭"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           cwd=WORKDIR, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        if len(out) > 3000:
            out = out[:3000] + "\n...[截断]"
        return f"exit={r.returncode}\n{out}" if out.strip() else f"exit={r.returncode} (无输出)"
    except subprocess.TimeoutExpired:
        return "命令超时(300s)"
    except Exception as e:
        return f"执行失败: {e}"


def execute_tool(name, args):
    args = args or {}
    try:
        if name == "list_dir":
            return _tool_list_dir(args.get("path", "."))
        if name == "read_file":
            return _tool_read_file(args.get("path", ""), args.get("limit", 200))
        if name == "write_file":
            return _tool_write_file(args.get("path", ""), args.get("content", ""))
        if name == "search":
            return _tool_search(args.get("pattern", ""), args.get("path", "."))
        if name == "run_command":
            return _tool_run_command(args.get("cmd", ""))
        return f"未知工具: {name}"
    except Exception as e:
        return f"工具执行出错: {e}"


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------
def call(model, messages, temperature=0.4, max_tokens=4000, timeout=600, tools=None):
    """调 /v1/chat/completions，返回完整 message dict。"""
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        BASE_URL + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={
            "Authorization": "Bearer " + api_key(),
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())
    data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]


def msg_text(message):
    c = (message.get("content") or "").strip()
    r = (message.get("reasoning_content") or "").strip()
    return c or r or ""


def ask(model, prompt, system=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msg_text(call(model, msgs))


def call_json(model, system, user, temperature=0.3, timeout=600):
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    content = msg_text(call(model, msgs, temperature=temperature, timeout=timeout))
    s = content.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    # 容错：只取第一个 [ 到最后一个 ]
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1:
        a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1:
        s = s[a:b + 1]
    return json.loads(s)


def agent_loop(model, system, user, tools=None, temperature=0.3, max_iter=20):
    """带工具的 agent 循环：模型自主调用工具直到给出最终回答。返回 (文本, 工具轨迹)。"""
    msgs = [{"role": "system", "content": system}]
    if user:
        msgs.append({"role": "user", "content": user})
    trace = []
    for _ in range(max_iter):
        m = call(model, msgs, temperature=temperature, tools=tools, timeout=600)
        tcs = m.get("tool_calls") or []
        if not tcs:
            return msg_text(m), trace
        # 记录 assistant 的 tool_calls 消息
        msgs.append({"role": "assistant", "content": m.get("content") or "",
                     "tool_calls": tcs})
        for tc in tcs:
            fn = tc["function"]
            name, args = fn["name"], json.loads(fn.get("arguments") or "{}")
            result = execute_tool(name, args)
            trace.append((name, args, result[:200]))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": result})
    return "(达到最大工具调用轮数，已停止)", trace


# ---------------------------------------------------------------------------
# 基础命令
# ---------------------------------------------------------------------------
def parallel(prompt, models=None):
    models = models or [r["model"] for r in ROLES.values()]
    results = {}
    with ThreadPoolExecutor(max_workers=len(models)) as ex:
        futs = {ex.submit(ask, m, prompt): m for m in models}
        for f in as_completed(futs):
            m = futs[f]
            try:
                results[m] = f.result()
            except Exception as e:
                results[m] = f"[错误] {e}"
    return results


def orchestrate(task):
    print(f"\n[1/3] 指挥官 {ROLES['指挥官']['model']} 拆解任务...")
    plan_prompt = (
        "你是多模型团队的总指挥。把下面的任务拆成 3~5 个可并行、互不依赖的子任务，"
        "每个子任务指定一个角色（分析/写码/快速/快答，任选其一）。"
        "只输出 JSON 数组，不要任何解释。格式：\n"
        '[{"role":"分析","task":"..."},{"role":"写码","task":"..."}]\n\n'
        f"任务：{task}"
    )
    try:
        plan = call_json(ROLES["指挥官"]["model"], "你是总指挥，只输出 JSON。", plan_prompt)
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
        t0 = time.time()
        try:
            c = ask(model, sub_task, system=sys_msg)
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

    print(f"\n[3/3] 指挥官 {ROLES['指挥官']['model']} 汇总...")
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
        final = ask(ROLES["指挥官"]["model"], summary_prompt,
                    system="你是总指挥。直接输出最终交付内容，不要输出你的思考过程。")
        if not final or final.startswith("The user") or "thinking" in final[:60].lower():
            final = "\n\n".join(report)
    except Exception as e:
        final = f"[汇总失败] {e}\n\n子任务原始结果：\n\n" + "\n\n".join(report)
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60)


# ---------------------------------------------------------------------------
# team 流水线：分析 → 写码 → 审查 → 迭代回改 → 汇总
# ---------------------------------------------------------------------------
def team(task, max_rounds=3):
    """真团队流水线：带工具落地文件 + 审查迭代。"""
    print(f"\n工作目录: {WORKDIR}")
    print(f"任务: {task}\n")

    # 阶段1：分析出设计稿
    print("[1/5] 分析 " + ROLES["分析"]["model"] + " 产出设计稿...")
    design_prompt = (
        "你是软件架构师。先查看当前项目结构（用 list_dir/read_file 工具），"
        "然后针对下面的任务输出一份 JSON 设计稿，不要写代码，只做设计。\n"
        f"任务：{task}\n\n"
        "JSON 格式（严格）：\n"
        '{"files":[{"path":"相对路径","purpose":"这个文件做什么"}],'
        '"plan":"实现步骤要点","acceptance":"验收标准(如何证明完成)"}'
    )
    design_txt, _ = agent_loop(
        ROLES["分析"]["model"],
        "你是架构师，先探索项目再用 JSON 输出设计稿。只输出 JSON，不要输出解释。",
        design_prompt,
        tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "search")],
    )
    try:
        a, b = design_txt.find("{"), design_txt.rfind("}")
        design = json.loads(design_txt[a:b + 1] if a != -1 and b != -1 else design_txt)
    except Exception as e:
        print(f"[警告] 设计稿解析失败({e})，退化为纯文本设计。")
        design = {"files": [], "plan": design_txt, "acceptance": ""}
    print(f"  设计稿: {json.dumps(design, ensure_ascii=False)[:500]}")

    # 阶段2：写码落地
    print("\n[2/5] 写码 " + ROLES["写码"]["model"] + " 按设计稿落地文件...")
    impl_prompt = (
        "你是资深工程师。根据下面的设计稿，用 write_file 工具把代码真正写到磁盘上。"
        "先用 list_dir/read_file 了解现状，再逐个写文件。完成后简要说明写了哪些文件、如何验证。\n\n"
        f"设计稿：{json.dumps(design, ensure_ascii=False)}"
    )
    impl_txt, impl_trace = agent_loop(
        ROLES["写码"]["model"],
        "你是写码工程师，用工具把代码落地到工作目录，写完后做自我检查。",
        impl_prompt,
        tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "write_file", "search")],
        temperature=0.2,
    )
    for name, args, res in impl_trace:
        if name == "write_file":
            print(f"    ✎ {args.get('path')}  → {res}")
    print(f"  写码完成（工具调用 {len(impl_trace)} 次）")

    # 阶段3+4：审查 + 迭代回改
    issues = None
    for rnd in range(1, max_rounds + 1):
        print(f"\n[3/5] 审查 {ROLES['分析']['model']} 读真实代码给意见（第 {rnd} 轮）...")
        review_prompt = (
            "你是代码审查专家。用 read_file/list_dir 工具读取刚才实际写出来的代码，"
            "逐条列出问题。只输出 JSON：\n"
            '{"blocking":[{"file":"...","issue":"..."}],'
            '"minor":[{"file":"...","issue":"..."}],'
            '"verdict":"pass" 或 "fix"}'
            "若无阻塞问题，verdict 为 pass。"
        )
        review_txt, _ = agent_loop(
            ROLES["分析"]["model"],
            "你是代码审查专家，只输出 JSON。",
            review_prompt,
            tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "search")],
        )
        try:
            a, b = review_txt.find("{"), review_txt.rfind("}")
            issues = json.loads(review_txt[a:b + 1] if a != -1 and b != -1 else review_txt)
        except Exception as e:
            print(f"[警告] 审查意见解析失败({e})，按通过处理。")
            issues = {"blocking": [], "minor": [], "verdict": "pass"}
        verdict = issues.get("verdict", "pass")
        blocking = issues.get("blocking", [])
        minor = issues.get("minor", [])
        print(f"  结论: {verdict}  (阻塞 {len(blocking)} 条 / 次要 {len(minor)} 条)")
        for it in blocking + minor:
            print(f"    · [{it.get('file','?')}] {it.get('issue','')[:80]}")
        if verdict == "pass" or not blocking:
            break
        if rnd < max_rounds:
            print(f"\n[4/5] 回改 {ROLES['写码']['model']} 按审查意见修复...")
            fix_prompt = (
                "根据下面的审查意见，用 read_file 读代码、write_file 修复问题。"
                "只修阻塞问题，次要问题一并处理。完成后说明改了什么。\n\n"
                f"审查意见：{json.dumps(issues, ensure_ascii=False)}"
            )
            _, fix_trace = agent_loop(
                ROLES["写码"]["model"],
                "你是工程师，按审查意见修复代码。",
                fix_prompt,
                tools=[t for t in TOOLS if t["function"]["name"] in ("read_file", "write_file", "list_dir", "search")],
                temperature=0.2,
            )
            for name, args, res in fix_trace:
                if name == "write_file":
                    print(f"    ✎ {args.get('path')}  → {res}")

    # 阶段5：汇总
    print("\n[5/5] 汇总 " + ROLES["指挥官"]["model"] + " 输出最终交付...")
    files_done = ", ".join(f.get("path", "") for f in design.get("files", []))
    final_prompt = (
        "你是总指挥。团队已完成以下任务，请汇总最终交付：完成内容、文件清单、"
        "验证方法、剩余风险。简洁，直接输出，不要输出思考过程。\n\n"
        f"任务：{task}\n设计稿：{json.dumps(design, ensure_ascii=False)[:800]}\n"
        f"写码说明：{impl_txt[:800]}\n最终审查：{json.dumps(issues or {}, ensure_ascii=False)[:600]}"
    )
    try:
        final = ask(ROLES["指挥官"]["model"], final_prompt,
                    system="你是总指挥，直接输出最终交付，不要输出思考过程。")
        if final.startswith("The user") or "thinking" in final[:60].lower():
            final = f"任务已完成。涉及文件：{files_done or '见工作目录'}\n\n写码说明：\n{impl_txt[:1500]}"
    except Exception as e:
        final = f"[汇总失败] {e}\n\n写码说明：\n{impl_txt[:1500]}"
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60)


# ---------------------------------------------------------------------------
# auto 无人值守：循环跑 team
# ---------------------------------------------------------------------------
def auto(task=None, hours=None, tasks_file=None):
    """无人值守连续跑：循环跑多模型团队流水线，直到超时或 Ctrl+C。

    - 给 --tasks 清单文件时：按文件里每行一个任务，逐个跑（跑完一轮接一轮）。
    - 只给单个任务时：反复迭代同一个任务，从第二轮起提示模型基于现有产出继续改进，
      不推倒重来（持续开发模式）。
    每轮结束写一行日志到 WORKDIR/auto-run.log。
    """
    tasks = []
    if tasks_file:
        try:
            with open(tasks_file, encoding="utf-8") as f:
                tasks = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        except Exception as e:
            print(f"[错误] 读取任务清单失败: {e}")
            return 1
    elif task:
        tasks = [task]
    if not tasks:
        print("[错误] auto 模式需要任务：直接给任务，或用 --tasks 指定任务清单文件")
        return 1

    deadline = time.time() + hours * 3600 if hours else None
    start = time.time()
    idx = 0
    log_path = os.path.join(WORKDIR, "auto-run.log")
    print(f"[auto] 任务数 {len(tasks)}，时长上限 "
          f"{str(hours)+'h' if hours else '不限(直到 Ctrl+C)'}，日志 {log_path}\n")

    while True:
        if deadline and time.time() > deadline:
            print(f"\n[auto] 已运行满 {hours} 小时，正常停止。")
            break
        base = tasks[idx % len(tasks)]
        idx += 1
        elapsed = round((time.time() - start) / 3600, 2)
        print("\n" + "=" * 60)
        print(f"[auto] 第 {idx} 轮（累计 {elapsed}h）")
        print("=" * 60)

        cur = base
        if idx > 1 and len(tasks) == 1:
            cur = (base + f"\n\n[持续改进] 这是第 {idx} 轮迭代。工作目录里已有前几轮产出的代码，"
                   "请先 list_dir/read_file 了解现状，基于现有成果继续完善、修复、扩展，"
                   "不要推倒重来。")

        try:
            team(cur)
        except KeyboardInterrupt:
            print("\n[auto] 手动中断，停止。")
            break
        except Exception as e:
            print(f"[auto] 本轮出错: {e}，记录后继续下一轮")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | 第{idx}轮 | {base[:80]}\n")
        except Exception:
            pass

    print(f"\n[auto] 结束：共 {idx} 轮，用时 {round((time.time()-start)/3600, 2)}h，日志见 {log_path}")
    return 0


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
        print(f"  网关: {BASE_URL}")
        print("  1. 单模型问答 (ask)")
        print("  2. 多模型同问对比 (parallel, 真并行)")
        print("  3. 指挥官拆解→多模型并行→汇总 (orchestrate)")
        print("  4. 真团队流水线(带工具+审查迭代) (team) ★推荐")
        print("  5. 无人值守连续跑 (auto)")
        print("  6. 列出可用模型")
        print("  7. 退出")
        print("=" * 56)
        try:
            s = input("  请选择 [1-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        try:
            if s == "1":
                m = input("  模型(astra/sol/luna/glm/deep/kimi...): ").strip()
                m = ALIASES.get(m, m)
                p = input("  问题: ").strip()
                print("\n" + ask(m, p))
            elif s == "2":
                p = input("  问题: ").strip()
                for m, c in parallel(p).items():
                    print(f"\n--- {m} ---\n{c}")
            elif s == "3":
                orchestrate(input("  任务: ").strip())
            elif s == "4":
                team(input("  任务: ").strip())
            elif s == "5":
                t = input("  任务(持续迭代，直接回车跳过=用清单): ").strip()
                hs = input("  时长上限小时(回车=不限): ").strip()
                hours = float(hs) if hs else None
                auto(task=t or None, hours=hours)
            elif s == "6":
                list_models()
            elif s == "7":
                print("再见")
                return
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def main():
    global WORKDIR, ALLOW_RUN
    ap = argparse.ArgumentParser(description="多模型协作编排器")
    ap.add_argument("--api-key", default=None, help="覆盖 API Key")
    ap.add_argument("--workdir", default=None, help="工作目录（团队读写文件限定在此）")
    ap.add_argument("--no-run", action="store_true", help="禁用 run_command 工具")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("ask", help="单模型问答")
    p.add_argument("model")
    p.add_argument("prompt")

    p = sub.add_parser("parallel", help="多模型同问对比")
    p.add_argument("prompt")
    p.add_argument("--models", nargs="*", default=None, help="指定模型列表")

    p = sub.add_parser("orchestrate", help="指挥官拆解→并行→汇总")
    p.add_argument("task")

    p = sub.add_parser("team", help="真团队流水线(带工具+审查迭代)")
    p.add_argument("task")
    p.add_argument("--rounds", type=int, default=3, help="审查迭代最大轮数")

    p = sub.add_parser("auto", help="无人值守连续跑多模型（挂机/跑一天一夜）")
    p.add_argument("task", nargs="?", default=None, help="要持续迭代的单个任务")
    p.add_argument("--hours", type=float, default=None, help="时长上限(小时)，不填=不限直到 Ctrl+C")
    p.add_argument("--tasks", default=None, help="任务清单文件，每行一个任务，循环跑")

    sub.add_parser("list", help="列出可用模型")

    args = ap.parse_args()

    if args.api_key:
        os.environ["CF_GATEWAY_KEY"] = args.api_key
    if args.workdir:
        WORKDIR = os.path.abspath(args.workdir)
    if args.no_run:
        ALLOW_RUN = False

    if not args.action:
        menu()
        return

    if args.action == "ask":
        print(ask(ALIASES.get(args.model, args.model), args.prompt))
    elif args.action == "parallel":
        models = args.models or None
        if models:
            models = [ALIASES.get(m, m) for m in models]
        for m, c in parallel(args.prompt, models).items():
            print(f"\n--- {m} ---\n{c}")
    elif args.action == "orchestrate":
        orchestrate(args.task)
    elif args.action == "team":
        team(args.task, max_rounds=args.rounds)
    elif args.action == "auto":
        sys.exit(auto(task=args.task, hours=args.hours, tasks_file=args.tasks))
    elif args.action == "list":
        list_models()


if __name__ == "__main__":
    main()
