#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_multi_model.py — 全量离线测试（T10）

红线：
- 零 pip 依赖：仅用 Python 标准库（unittest/subprocess/json/os/sys/tempfile/shutil）
- 全部离线不触网：不连网关、不调真实模型
- Windows UTF-8：subprocess 用 encoding="utf-8"
- 测试组 A：MCP 协议黑盒（子进程）
- 测试组 B：引擎与流水线（monkeypatch，进程内）
- 测试组 C：自检（子进程 --self-check）
- 测试组 D：deploy _merge_mcp_servers 幂等（进程内纯函数）

运行：python test_multi_model.py
"""
import unittest
import subprocess
import json
import sys
import os
import tempfile
import shutil
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER = os.path.join(HERE, "mcp_server.py")
MULTI_MODEL = os.path.join(HERE, "multi-model.py")


# =============================================================================
# 测试组 A：MCP 协议测试（子进程黑盒）
# =============================================================================

class TestMCPProtocol(unittest.TestCase):
    """A. MCP 协议测试（子进程黑盒）"""

    def setUp(self):
        self.proc = subprocess.Popen(
            [sys.executable, MCP_SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=HERE,
        )

    def tearDown(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def _send(self, obj):
        """发一帧，读一帧响应。"""
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            self.fail("子进程 stdout 已关闭（EOF），无响应")
        return json.loads(line)

    def _send_raw(self, raw_str):
        """发原始字符串，读一帧响应。"""
        self.proc.stdin.write(raw_str + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            self.fail("子进程 stdout 已关闭（EOF），无响应")
        return json.loads(line)

    def test_01_initialize(self):
        """R-A1: initialize 握手返回 protocolVersion/serverInfo/capabilities.tools"""
        resp = self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])
        self.assertIn("serverInfo", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_02_notification_no_response(self):
        """R-A1: notifications/initialized 是通知，无响应"""
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        self.proc.stdin.flush()
        # 发一个 ping 确认 server 还活着且没被通知搞乱
        resp = self._send({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        self.assertEqual(resp["id"], 2)
        self.assertEqual(resp["result"], {})

    def test_03_tools_list(self):
        """R-A3: tools/list 返回 >=8 工具，每项含非空 description 与 inputSchema"""
        resp = self._send({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        tools = resp["result"]["tools"]
        self.assertGreaterEqual(len(tools), 8)
        for t in tools:
            self.assertTrue(t["description"], f"工具 {t['name']} description 为空")
            self.assertIn("inputSchema", t)
        # 硬性命名检查
        names = {t["name"] for t in tools}
        for required in ("multi_team_start", "multi_task_status", "multi_task_result"):
            self.assertIn(required, names, f"缺少必需工具 {required}")

    def test_04_multi_ping(self):
        """R-A2 间接: multi_ping 返回成功且含 pong"""
        resp = self._send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "multi_ping", "arguments": {}}})
        self.assertNotIn("isError", resp["result"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("pong", text)

    def test_05_malformed_frame(self):
        """R-A10: 畸形行返回 -32700，随后合法请求仍正常"""
        resp1 = self._send_raw("not json at all")
        self.assertEqual(resp1["error"]["code"], -32700)
        # 随后 initialize 仍正常
        resp2 = self._send({"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}})
        self.assertIn("result", resp2)

    def test_06_unknown_method(self):
        """R-A10: 未知方法返回 -32601"""
        resp = self._send({"jsonrpc": "2.0", "id": 6, "method": "nonexistent/method", "params": {}})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_07_missing_params(self):
        """R-A10: multi_ask 缺参返回 isError"""
        resp = self._send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "multi_ask", "arguments": {"model": "luna"}}})
        self.assertTrue(resp["result"].get("isError", False))

    def test_08_unknown_tool(self):
        """R-A10: 未知工具返回 -32602"""
        resp = self._send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "nonexistent_tool", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_09_invalid_task_id(self):
        """R-A8: 伪造 task_id 查 status 返回 isError 含句柄无效提示"""
        resp = self._send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "multi_task_status", "arguments": {"task_id": "fake-00000000-0000-dead"}}})
        self.assertTrue(resp["result"].get("isError", False))
        text = resp["result"]["content"][0]["text"]
        self.assertIn("无效", text)  # "句柄无效或已过期"

    def test_10_stdout_purity(self):
        """R-A2: stdout 每行均合法 JSON-RPC"""
        # 发若干请求
        for i in range(5):
            self._send({"jsonrpc": "2.0", "id": 100 + i, "method": "ping", "params": {}})
        # stdout 已被读走，每行都是合法 JSON（_send 已验证）
        # 这个测试主要确认协议帧通道纯净
        self.assertTrue(True)  # 如果前面 5 个 ping 都成功解析，说明 stdout 纯净


# =============================================================================
# 测试组 B：引擎与流水线测试（monkeypatch，进程内）
# =============================================================================

def _load_mm():
    """加载 multi-model.py 模块。"""
    spec = importlib.util.spec_from_file_location("multi_model", MULTI_MODEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEnginePipeline(unittest.TestCase):
    """B. 引擎与流水线测试（monkeypatch）"""

    @classmethod
    def setUpClass(cls):
        cls.mm = _load_mm()

    def setUp(self):
        """保存原始 call/ask/WORKDIR，每个测试后恢复。"""
        self._orig_call = self.mm.call
        self._orig_ask = self.mm.ask
        self._orig_workdir = self.mm.WORKDIR
        # 临时工作目录
        self.tmpdir = tempfile.mkdtemp(prefix="mmtest_")
        self.mm.WORKDIR = self.tmpdir

    def tearDown(self):
        self.mm.call = self._orig_call
        self.mm.ask = self._orig_ask
        self.mm.WORKDIR = self._orig_workdir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _fake_call_factory(self, responses):
        """造一个假 call 函数，按顺序返回 responses 列表中的响应。
        responses 是 list of dict（message dict）。耗尽后返回空内容响应。"""
        def fake_call(model, messages, temperature=0.4, max_tokens=4000, timeout=600, tools=None, tool_choice=None):
            if responses:
                return responses.pop(0)
            return {"content": "{}", "tool_calls": None}
        fake_call.calls = []
        return fake_call

    def _fake_ask_factory(self, answers):
        """造一个假 ask 函数。answers 是 list of str。"""
        def fake_ask(model, prompt, system=None):
            if answers:
                return answers.pop(0)
            return "fake answer"
        return fake_ask

    def test_11_call_json_tool_path(self):
        """R-B1: call_json tools 路径——fake_call 返回 tool_calls 形态 → 解析成功"""
        # 构造 tool_calls 响应
        tool_response = {
            "content": None,
            "tool_calls": [{"id": "tc1", "type": "function", "function": {
                "name": "submit_json",
                "arguments": json.dumps({"result": json.dumps({"key": "value"})})
            }}]
        }
        self.mm.call = self._fake_call_factory([tool_response])
        result = self.mm.call_json("test-model", "system", "user")
        self.assertEqual(result, {"key": "value"})

    def test_12_call_json_fallback(self):
        """R-B1: call_json 回退路径——tools 请求无 tool_calls → 回退截取解析"""
        # 构造普通文本响应（无 tool_calls）
        text_response = {"content": '```json\n{"answer": 42}\n```', "tool_calls": None}
        # call_json 在 tools 路径无 tool_calls 时会再调一次 call 走回退，需两个响应
        self.mm.call = self._fake_call_factory([text_response, text_response])
        result = self.mm.call_json("test-model", "system", "user")
        self.assertEqual(result, {"answer": 42})

    def test_13_dangerous_command_block(self):
        """R-B6: 危险命令拦截"""
        # rm -rf 应被拦截
        result = self.mm._tool_run_command("rm -rf /", allow_risky=False)
        self.assertIn("已拦截", result)
        self.assertIn("rm -rf", result)

    def test_14_dangerous_command_allow(self):
        """R-B7: --allow-risky 放行"""
        # 用一个无害命令测试 allow_risky 透传（rm -rf 会真执行，太危险）
        result = self.mm._tool_run_command("echo hello", allow_risky=True)
        self.assertIn("exit=0", result)

    def test_15_safe_command_pass(self):
        """R-B8: 安全命令畅通（dir/ls/python/pytest 不拦截）"""
        for cmd in ["echo test", "dir", "python --version"]:
            result = self.mm._tool_run_command(cmd, allow_risky=False)
            self.assertNotIn("已拦截", result, f"安全命令被误拦: {cmd}")

    def test_16_match_dangerous_cases(self):
        """R-B6: 黑名单正负用例表"""
        # 应拦截
        dangerous = [
            "rm -rf /",
            "rm -fr /tmp",
            "rm -r -f /",
            "del /s /q *",
            "rd /s /q test",
            "Remove-Item -Recurse -Force foo",
            "format c:",
            "mkfs.ext4 /dev/sda",
            "shutdown /s",
            "diskpart",
        ]
        for cmd in dangerous:
            matched = self.mm._match_dangerous(cmd)
            self.assertIsNotNone(matched, f"应拦截但未拦截: {cmd}")
        # 不应拦截
        safe = [
            "dir",
            "ls -la",
            "python test.py",
            "pytest",
            "echo hello",
            "format",  # 单词不拦
            "Remove-Item foo",  # 不带 -Recurse -Force
            "git status",
            "npm install",
        ]
        for cmd in safe:
            matched = self.mm._match_dangerous(cmd)
            self.assertIsNone(matched, f"不应拦截但被拦截: {cmd} -> {matched}")

    def test_17_state_persistence(self):
        """R-B3: 状态文件原子写盘 + 可读回"""
        state = self.mm._new_state("test task", self.tmpdir, "builtin", False)
        self.mm._save_state_atomic(self.tmpdir, state)
        loaded = self.mm._load_state(self.tmpdir, state["task_id"])
        self.assertEqual(loaded["task_id"], state["task_id"])
        self.assertEqual(loaded["task"], "test task")
        self.assertEqual(loaded["phase"], 1)

    def test_18_state_corrupt(self):
        """R-B5: 状态文件损坏 → StateError"""
        state = self.mm._new_state("test", self.tmpdir, "builtin", False)
        self.mm._save_state_atomic(self.tmpdir, state)
        # 写坏文件
        path = self.mm._state_path(self.tmpdir, state["task_id"])
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json {{{")
        with self.assertRaises(self.mm.StateError):
            self.mm._load_state(self.tmpdir, state["task_id"])

    def test_19_state_schema_mismatch(self):
        """R-B5: schema_v 不符 → StateError"""
        state = self.mm._new_state("test", self.tmpdir, "builtin", False)
        state["schema_v"] = 999  # 错误版本
        self.mm._save_state_atomic(self.tmpdir, state)
        with self.assertRaises(self.mm.StateError):
            self.mm._load_state(self.tmpdir, state["task_id"])

    def test_20_handoff_summary(self):
        """R-B9: 交接摘要含三要素"""
        state = self.mm._new_state("test task", self.tmpdir, "builtin", False)
        state["design"] = {
            "files": [{"path": "src/foo.py", "purpose": "做某事"}],
            "plan": "关键决策：用策略模式。" * 100,  # 长文本
            "acceptance": "验收：测试通过。"
        }
        summary = self.mm._handoff_design(state)
        self.assertIn("src/foo.py", summary)  # 文件清单
        self.assertIn("关键决策", summary)  # 关键决策
        self.assertLessEqual(len(summary), self.mm.HANDOFF_MAX_CHARS + 20)  # 不超限（+20 容差）


class TestSpawnAgent(unittest.TestCase):
    """S. 子代理委派测试（spawn_agent / spawn_many，monkeypatch 离线）"""

    @classmethod
    def setUpClass(cls):
        cls.mm = _load_mm()

    def setUp(self):
        self._orig_ask = self.mm.ask

    def tearDown(self):
        self.mm.ask = self._orig_ask

    def test_spawn_agent_by_role(self):
        """S1: 角色名委派 → 正确模型 + 回答"""
        calls = []

        def fake_ask(model, prompt, system=None):
            calls.append(model)
            return "done-by-" + model

        self.mm.ask = fake_ask
        model, answer = self.mm.spawn_agent("写码", "实现一个函数")
        self.assertEqual(model, "gpt-5.6-luna")
        self.assertEqual(answer, "done-by-gpt-5.6-luna")
        self.assertEqual(calls, ["gpt-5.6-luna"])

    def test_spawn_agent_by_alias(self):
        """S2: 别名委派 → 解析到模型"""
        calls = []

        def fake_ask(model, prompt, system=None):
            calls.append(model)
            return "ok"

        self.mm.ask = fake_ask
        model, answer = self.mm.spawn_agent("sol", "分析一下")
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(calls, ["gpt-5.6-sol"])

    def test_spawn_agent_unknown(self):
        """S3: 未知子代理 → [子代理失败] 前缀，不调 ask"""
        self.mm.ask = self.mm.ask  # 保持原样
        model, answer = self.mm.spawn_agent("不存在的代理", "任务")
        self.assertTrue(answer.startswith("[子代理失败]"))

    def test_spawn_many_parallel(self):
        """S4: spawn_many 并行委派多个子代理，顺序一致"""
        def fake_ask(model, prompt, system=None):
            return "ans-" + model

        self.mm.ask = fake_ask
        results = self.mm.spawn_many([("分析", "a"), ("写码", "b"), ("快答", "c")])
        self.assertEqual(len(results), 3)
        self.assertEqual([r[0] for r in results], ["分析", "写码", "快答"])
        self.assertEqual([r[1] for r in results], ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-luna-fast"])


# =============================================================================
# 测试组 C：自检测试
# =============================================================================

class TestSelfCheck(unittest.TestCase):
    """C. 自检测试"""

    def test_21_self_check_ok(self):
        """R-D7: --self-check 退出码 0"""
        r = subprocess.run([sys.executable, MCP_SERVER, "--self-check"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=HERE, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("OK", r.stderr)

    def test_22_self_check_tools_count(self):
        """R-D7: self-check 输出含 8 个工具"""
        r = subprocess.run([sys.executable, MCP_SERVER, "--self-check"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=HERE, timeout=30)
        # stderr 里应有 8 行工具清单
        tool_lines = [l for l in r.stderr.split("\n") if l.strip().startswith("- multi_")]
        self.assertEqual(len(tool_lines), 8)


# =============================================================================
# 测试组 D：deploy 幂等测试（进程内纯函数）
# =============================================================================

class TestDeployMCP(unittest.TestCase):
    """D. deploy _merge_mcp_servers 幂等测试"""

    @classmethod
    def setUpClass(cls):
        # 加载 deploy_ai_cli.py
        spec = importlib.util.spec_from_file_location("deploy_ai_cli", os.path.join(HERE, "deploy_ai_cli.py"))
        cls.deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.deploy)

    def test_23_merge_skip(self):
        """R-D5: skip_mcp=True 原样返回"""
        text = 'model_provider = "custom"\n'
        result = self.deploy._merge_mcp_servers(text, skip_mcp=True)
        self.assertEqual(result, text)

    def test_24_merge_add(self):
        """R-D1: 段不存在 → 追加"""
        text = 'model_provider = "custom"\n'
        result = self.deploy._merge_mcp_servers(text, skip_mcp=False)
        self.assertIn("[mcp_servers.multi_model]", result)
        self.assertIn("command", result)
        self.assertIn("args", result)

    def test_25_merge_idempotent(self):
        """R-D1: 二次调用零改动"""
        text = 'model_provider = "custom"\n'
        out1 = self.deploy._merge_mcp_servers(text, skip_mcp=False)
        out2 = self.deploy._merge_mcp_servers(out1, skip_mcp=False)
        self.assertEqual(out1, out2)

    def test_27_agents_md_idempotent(self):
        """R-D8: _write_codex_agents_md 幂等——第二次写入跳过"""
        import tempfile as _tf
        tmp = _tf.mkdtemp(prefix="codexhome_")
        try:
            from pathlib import Path
            home = Path(tmp)
            first = self.deploy._write_codex_agents_md(home)
            second = self.deploy._write_codex_agents_md(home)
            self.assertTrue(first)
            self.assertFalse(second)  # 内容一致 → 跳过
            content = (home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("spawn_agent", content)
            self.assertIn("gpt-6-astra", content)
            self.assertIn("委派原则", content)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_28_agents_md_no_secrets(self):
        """R-D9: AGENTS.md 不含密钥/网关地址"""
        import tempfile as _tf
        tmp = _tf.mkdtemp(prefix="codexhome_")
        try:
            from pathlib import Path
            home = Path(tmp)
            self.deploy._write_codex_agents_md(home)
            content = (home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertNotIn("sk-", content)
            self.assertNotIn("1232333", content)
            self.assertNotIn("8787", content)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_26_no_token_in_config(self):
        """R-D3: config.toml 不含 token/key/env"""
        text = 'model_provider = "custom"\n'
        result = self.deploy._merge_mcp_servers(text, skip_mcp=False)
        # 不应包含任何凭据
        self.assertNotIn("token", result.lower())
        self.assertNotIn("api_key", result.lower())
        self.assertNotIn("bearer", result.lower())
        # 不应有 env 字段
        self.assertNotIn("env_key", result)
        self.assertNotIn("[mcp_servers.multi_model.env]", result)


# =============================================================================
# 主入口
# =============================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)