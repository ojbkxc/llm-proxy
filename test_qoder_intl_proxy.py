# -*- coding: utf-8 -*-
"""qoder_intl_proxy.py 的离线测试：只测国际版独有的配置与 v3 请求体（不触网）。"""
import contextlib

import hashlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoder_intl_proxy as Q


ACCT = {
    "token": "dt-fake",
    "uid": "fake-uid",
    "machineId": "fake-machine",
    "session_seed": "fake-seed",
    "account": "fake",
    "organizationId": "",
    "organizationTags": [],
}

MODEL = {
    "key": "auto",
    "source": "system",
    "name": "Auto",
    "reasoning": True,
    "vision": True,
    "ctx": 200000,
    "max_output_tokens": 65536,
    "format": "openai",
    "thinking_config": {"enabled": {"efforts": {"low": {}, "high": {}}}, "disabled": True},
}


class ConfigTest(unittest.TestCase):
    def test_endpoints_and_paths(self):
        self.assertEqual(Q.GATEWAY, "https://api3.qoder.sh")
        self.assertEqual(Q.OPENAPI, "https://openapi.qoder.sh")
        self.assertEqual(Q.SITE, "https://qoder.com")
        self.assertEqual(Q.PORT, 8792)
        self.assertTrue(Q.USER_DATA.endswith(os.path.join("Roaming", "com.qoder.app.stable")))
        self.assertTrue(Q.QODER_HOME.endswith(os.sep + ".qoder"))
        self.assertTrue(Q.SIGN_JS.endswith("qoder_intl_sign.mjs"))

    def test_accounts_file_defaults_to_script_dir(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("QODER_INTL_PROXY_ACCOUNTS", None)
            path = Q._default_accounts_file()
        self.assertEqual(os.path.dirname(path), Q.HERE)
        self.assertTrue(path.endswith("qoder_intl_accounts.json"))

    def test_accounts_file_env_override(self):
        with mock.patch.dict(os.environ,
                             {"QODER_INTL_PROXY_ACCOUNTS": "X:/tmp/pool.json"}):
            self.assertEqual(Q._default_accounts_file(), "X:/tmp/pool.json")

    def test_paths_are_shared_with_cn_protocol(self):
        self.assertEqual(Q.MODEL_LIST_PATH, "/algo/api/v2/model/list")
        self.assertEqual(Q.INFER_PATH, "/algo/api/v2/service/pro/sse/agent_chat_generation")


class PayloadTest(unittest.TestCase):
    def _build(self, **oa_extra):
        oa = {"messages": [{"role": "system", "content": "be brief"},
                           {"role": "user", "content": "hi"}]}
        oa.update(oa_extra)
        return Q._build_infer_payload(oa, MODEL, ACCT)

    def test_v3_shape(self):
        p = self._build(max_tokens=123, temperature=0.2, top_p=0.9, tool_choice="auto")
        self.assertEqual(p["chat_task"], "FREE_INPUT")
        self.assertEqual(p["version"], "3")
        self.assertEqual(p["agent_id"], "agent_common")
        self.assertEqual(p["task_id"], "common")
        self.assertEqual(p["session_type"], "qodercli")
        self.assertTrue(p["stream"])
        self.assertEqual(p["request_id"], p["request_set_id"])
        self.assertEqual(p["request_id"], p["chat_record_id"])
        self.assertNotIn("scene", p)
        self.assertNotIn("reasoningEffort", p)
        self.assertNotIn("source_session_id", p)
        self.assertEqual(p["system"], "be brief")
        self.assertEqual(p["model_config"]["key"], "auto")
        self.assertTrue(p["model_config"]["is_reasoning"])
        self.assertEqual(p["model_config"]["max_input_tokens"], 200000)
        self.assertEqual(p["chat_context"]["text"], "hi")
        self.assertEqual(p["chat_context"]["extra"]["originalContent"], "hi")
        self.assertEqual(p["chat_context"]["extra"]["modelConfig"],
                         {"key": "auto", "is_reasoning": True})
        self.assertEqual(p["parameters"]["max_tokens"], 123)
        self.assertEqual(p["parameters"]["temperature"], 0.2)
        self.assertEqual(p["parameters"]["top_p"], 0.9)
        self.assertEqual(p["parameters"]["tool_choice"], "auto")
        self.assertEqual(p["tools"], [])

    def test_default_max_tokens_uses_model(self):
        p = self._build()
        self.assertEqual(p["parameters"]["max_tokens"], 65536)

    def test_thinking_none_disables_flag(self):
        p = self._build(reasoning_effort="none")
        self.assertFalse(p["model_config"]["is_reasoning"])
        self.assertFalse(p["chat_context"]["extra"]["modelConfig"]["is_reasoning"])
        self.assertFalse(p["parameters"]["enable_thinking"])
        self.assertEqual(p["parameters"]["reasoning_effort"], "none")

    def test_thinking_effort_is_in_parameters_not_top_level(self):
        p = self._build(reasoning_effort="low")
        self.assertEqual(p["parameters"]["reasoning_effort"], "low")
        self.assertTrue(p["parameters"]["enable_thinking"])
        self.assertNotIn("reasoningEffort", p)

    def test_normalize_models_keeps_max_output_tokens(self):
        raw = {"chat": [{"key": "auto", "display_name": "Auto", "max_input_tokens": 1000,
                         "max_output_tokens": 64, "is_reasoning": True}]}
        out = Q._normalize_models(raw)
        self.assertEqual(out[0]["max_output_tokens"], 64)


class UpstreamErrorMappingTest(unittest.TestCase):
    """上游状态码/首帧错误 → 对客状态码。首帧里的业务限制（110 当日额度用尽、
    10605/isQueued 排队、112/pricingUrl 额度用尽）都是 HTTP 200 + statusCodeValue 403，
    不能当鉴权失败。"""

    def test_business_limits_map_to_429_not_401(self):
        self.assertEqual(Q._map_upstream_error(
            403, '{"code":"110","message":"Billing daily count exceeded"}'), 429)
        self.assertEqual(Q._map_upstream_error(
            403, '{"code":"10605","message":"{\\"isQueued\\":true,\\"retryAfterSeconds\\":31}"}'), 429)
        self.assertEqual(Q._map_upstream_error(
            403, '{"code":"112","message":"{\\"pricingUrl\\":\\"https://qoder.com/pricing\\"}"}'), 429)

    def test_real_auth_failure_still_maps_to_401(self):
        self.assertEqual(Q._map_upstream_error(403, "invalid device token"), 401)
        self.assertEqual(Q._map_upstream_error(401), 401)


class CheckinTest(unittest.TestCase):
    """签到语义（回退对齐国内版 qoder_proxy.py 的实战验证行为）：
    账号池身份（每号独立 machineId，machineToken 空就用 machineId 顶替），
    只领列表里 CLAIM_BENEFIT+CLAIMABLE 的条目、逐个全领——不盲领、不派生身份；
    列表已 CLAIMED → 成功 no-op 不再 POST；
    BLOCKED+SAME_PERSON_ALREADY_CLAIMED = 上游拒发，未到账但不算调用失败。"""

    def _today_entry(self, cid="camp-1", status="CLAIMABLE"):
        return {"id": cid, "campaignKey": "act-20200101-910",
                "actionType": "CLAIM_BENEFIT", "claimStatus": status,
                "benefit": {"kind": "CREDITS", "amount": 100}}

    def _view_only_entry(self):
        return {"id": "v1", "campaignKey": "act-20200101-900",
                "actionType": "VIEW_DETAILS",
                "benefit": {"kind": "CREDITS", "amount": 100}}

    def _run_checkin(self, campaigns=None, claim_reply=None,
                     claim_http=200, usage_total=100):
        """mock _openapi_json 跑一次 _checkin_account；返回 (ok, rep, posted)。"""
        posted = []

        def fake_openapi(method, path, body=None, token="", timeout=20,
                         headers=None, ua=None):
            if method == "POST":
                posted.append(path)
                return claim_http, dict(claim_reply or {})
            if path == Q.USAGE_PATH:
                return 200, {"addOnQuota": {"total": usage_total}}
            if path == Q.CAMPAIGNS_PATH:
                return 200, {"claimable": True, "campaigns": list(campaigns or [])}
            raise AssertionError("unexpected %s %s" % (method, path))

        with mock.patch.object(Q, "_openapi_json", side_effect=fake_openapi):
            ok, rep = Q._checkin_account(ACCT)
        return ok, rep, posted

    # ---- 可领条目筛选（对齐国内版 _claimable_campaigns）----

    def test_claimable_campaigns_filters_targets(self):
        """只挑 actionType=CLAIM_BENEFIT 且 claimStatus=CLAIMABLE 且有 id 的条目。"""
        cps = [
            {"id": "a", "campaignKey": "act-1", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": 100}},
            {"id": "b", "campaignKey": "act-2", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "CLAIMED", "benefit": {"kind": "CREDITS", "amount": 100}},
            {"id": "c", "campaignKey": "act-3", "actionType": "VIEW_DETAILS",
             "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": 100}},
            {"campaignKey": "act-4", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": 100}},
            {"id": "d", "campaignKey": "act-5", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "BLOCKED", "benefit": {"kind": "CREDITS", "amount": 100}},
        ]
        self.assertEqual([c["id"] for c in Q._claimable_campaigns(cps)], ["a"])
        self.assertEqual(Q._claimable_campaigns([]), [])

    def test_claimable_campaigns_ignores_benefit_kind(self):
        """筛选不看 benefit.kind（国内版语义）：POINTS 等其它奖励照样领。"""
        cps = [{"id": "p", "campaignKey": "act-6", "actionType": "CLAIM_BENEFIT",
                "claimStatus": "CLAIMABLE", "benefit": {"kind": "POINTS", "amount": 10}}]
        self.assertEqual(len(Q._claimable_campaigns(cps)), 1)

    def test_blind_claim_helpers_are_removed(self):
        """盲领/派生身份两轮改动已回退：这些符号不允许再出现。"""
        for name in ("_checkin_device_headers", "_probe_daily_campaign_id",
                     "_daily_credits_campaign", "_daily_key_prefix"):
            self.assertFalse(hasattr(Q, name), name)

    # ---- 签到设备身份：一律账号池身份（对齐国内版 _cosy_device_headers）----

    def test_checkin_account_sends_pool_identity_for_campaigns(self):
        """签到全链路（首查 GET / POST claim / 复查 GET）都用账号池身份：
        Cosy-MachineId=acct.machineId、Cosy-MachineToken=machineToken（空则用
        machineId 顶替）、Cosy-ClientType=10（桌面端）。"""
        for mtok in ("own-machine-token", ""):
            acct = dict(ACCT, uid="u-checkin", machineId="own-machine-id",
                        machineToken=mtok)
            captured = []

            def fake_openapi(method, path, body=None, token="", timeout=20,
                             headers=None, ua=None):
                captured.append((method, path, dict(headers or {})))
                if method == "POST":
                    return 200, {"status": "CLAIMED", "replayed": False,
                                 "grantId": "g-pool", "grantedAt": "2026-10-20T02:00:00Z"}
                if path == Q.USAGE_PATH:
                    return 200, {"addOnQuota": {"total": 200}}
                if path == Q.CAMPAIGNS_PATH:
                    return 200, {"claimable": True, "campaigns": [self._today_entry()]}
                raise AssertionError("unexpected %s %s" % (method, path))

            with mock.patch.object(Q, "_openapi_json", side_effect=fake_openapi):
                ok, rep = Q._checkin_account(acct)
            self.assertTrue(ok, rep)
            self.assertTrue(rep["secured"])
            # 首查 GET、POST claim、复查 GET——三处的身份都是账号池这套
            campaign_calls = [(m, h) for m, p, h in captured
                              if p == Q.CAMPAIGNS_PATH or m == "POST"]
            self.assertEqual(len(campaign_calls), 3)
            for _m, h in campaign_calls:
                self.assertEqual(h["Cosy-MachineId"], "own-machine-id")
                self.assertEqual(h["Cosy-MachineToken"],
                                 mtok or "own-machine-id")   # 空 token 用 machineId 顶替
                self.assertEqual(h["Cosy-ClientType"], "10")

    def test_multiple_claimable_entries_all_claimed_in_order(self):
        """列表里多个 CLAIM_BENEFIT+CLAIMABLE 条目：逐个 POST，一个不落。"""
        entries = [self._today_entry(cid="camp-a"),
                   self._today_entry(cid="camp-b")]
        posted = []

        def fake_openapi(method, path, body=None, token="", timeout=20,
                         headers=None, ua=None):
            if method == "POST":
                posted.append(path)
                cid = path.rsplit("/", 2)[-2]
                return 200, {"status": "CLAIMED", "replayed": False,
                             "grantId": "g-" + cid, "grantedAt": "2026-10-20T02:00:00Z"}
            if path == Q.USAGE_PATH:
                return 200, {"addOnQuota": {"total": 300}}
            if path == Q.CAMPAIGNS_PATH:
                return 200, {"claimable": True, "campaigns": list(entries)}
            raise AssertionError("unexpected %s %s" % (method, path))

        with mock.patch.object(Q, "_openapi_json", side_effect=fake_openapi):
            ok, rep = Q._checkin_account(ACCT)
        self.assertTrue(ok, rep)
        self.assertEqual(posted, [Q.CAMPAIGNS_PATH + "/camp-a/claim",
                                  Q.CAMPAIGNS_PATH + "/camp-b/claim"])
        self.assertEqual([i["id"] for i in rep["claimed"]], ["camp-a", "camp-b"])
        self.assertTrue(rep["secured"])
        self.assertEqual(rep["addOnTotal"], 300)

    # ---- _checkin_account 回执解读 ----

    def test_claim_body_status_does_not_override_http_status(self):
        """国际版回执体 status:"CLAIMED" 不能覆盖 HTTP 200，否则成功被误判为失败。"""
        ok, rep, posted = self._run_checkin(
            campaigns=[self._today_entry()],
            claim_reply={"status": "CLAIMED", "grantId": "grant-1",
                         "claimedAt": "2026-10-20T00:00:00Z",
                         "grantedAt": "2026-10-20T00:00:01Z", "replayed": False})
        self.assertTrue(ok, rep)
        self.assertEqual(posted, [Q.CAMPAIGNS_PATH + "/camp-1/claim"])
        item = rep["claimed"][0]
        self.assertEqual(item["status"], 200)
        self.assertEqual(item["claimStatus"], "CLAIMED")
        self.assertEqual(item["grantId"], "grant-1")
        self.assertNotIn("blind", item)
        self.assertTrue(rep["secured"])
        self.assertIn("本轮已领取", rep["message"])
        self.assertEqual(rep["addOnTotal"], 100)
        self.assertNotIn("error", rep)


    def test_replayed_claimed_means_already_had(self):
        ok, rep, _posted = self._run_checkin(
            campaigns=[self._today_entry()],
            claim_reply={"status": "CLAIMED", "replayed": True,
                         "claimedAt": "2026-10-20T01:00:00Z", "grantId": "g-3"})
        self.assertTrue(ok, rep)
        self.assertTrue(rep["secured"])
        self.assertIn("此前已领", rep["message"])

    def test_blocked_same_person_is_success_without_share(self):
        """BLOCKED+SAME_PERSON 是"上游拒发、未到账"：ok=True 但不 secured，
        别被回执里同样存在的 grantId/benefit 误判成发放。"""
        ok, rep, _posted = self._run_checkin(
            campaigns=[self._today_entry()],
            claim_reply={"status": "BLOCKED", "replayed": True,
                         "failureCode": "SAME_PERSON_ALREADY_CLAIMED",
                         "claimedAt": "2026-10-20T01:23:45Z", "grantId": "g-other",
                         "benefit": {"kind": "CREDITS", "amount": 100}})
        self.assertTrue(ok, rep)
        self.assertFalse(rep["secured"])
        self.assertIn("SAME_PERSON_ALREADY_CLAIMED", rep["message"])
        self.assertIn("拒发", rep["message"])
        self.assertNotIn("error", rep)

    def test_claimed_entry_is_noop_without_post(self):
        """列表里已 CLAIMED → 成功 no-op：不再 POST claim（国内版语义与文案）。"""
        ok, rep, posted = self._run_checkin(
            campaigns=[self._today_entry(status="CLAIMED")])
        self.assertTrue(ok, rep)
        self.assertEqual(posted, [])                 # 已领就不再 POST claim
        self.assertTrue(rep["secured"])
        self.assertEqual(rep["message"], "今天的 Credits 已在手上（之前领过）")
        self.assertNotIn("addOnTotal", rep)          # no-op 不读额度桶

    def test_view_only_entry_is_not_posted(self):
        """VIEW_DETAILS 条目不是奖励，绝不能被 POST（国内版 no-op 文案）。"""
        ok, rep, posted = self._run_checkin(
            campaigns=[self._view_only_entry()])
        self.assertTrue(ok, rep)
        self.assertEqual(posted, [])
        self.assertFalse(rep["secured"])
        self.assertEqual(rep["message"], "当前没有可领取的活动（未到窗口或活动已结束）")

    def test_unexpected_receipt_is_failure(self):
        ok, rep, _posted = self._run_checkin(
            campaigns=[self._today_entry()],
            claim_reply={"status": "BLOCKED", "failureCode": "CAMPAIGN_NOT_STARTED",
                         "message": "not in window"})
        self.assertFalse(ok)
        self.assertFalse(rep["secured"])
        self.assertTrue(rep.get("error"))

    def test_claim_http_error_is_failure(self):
        ok, rep, posted = self._run_checkin(
            campaigns=[self._today_entry()], claim_http=409,
            claim_reply={"message": "conflict"})
        self.assertFalse(ok)
        self.assertEqual(len(posted), 1)
        self.assertIn("409", rep["error"])

    # ---- 批量调用（不探测、不盲领）----

    def test_checkin_round_calls_every_account(self):
        accs = [dict(ACCT, token="dt-0"), dict(ACCT, token="dt-1")]
        calls = []

        def fake_checkin(acct):
            calls.append(acct["token"])
            if acct["token"] == "dt-0":
                return True, {"secured": True, "claimed": []}
            # BLOCKED 回执：HTTP 200 + benefit 俱全，但不是新发放
            return True, {"secured": False, "message": "无份",
                          "claimed": [{"id": "x", "status": 200,
                                       "claimStatus": "BLOCKED", "replayed": True,
                                       "failureCode": "SAME_PERSON_ALREADY_CLAIMED",
                                       "kind": "CREDITS", "amount": 100,
                                       "grantId": "g-other"}]}

        logs = []
        with mock.patch.object(Q, "_load_pool_locked", return_value=accs), \
                mock.patch.object(Q, "_checkin_account", side_effect=fake_checkin), \
                mock.patch.object(Q, "_log", side_effect=logs.append):
            secured, total, all_ok = Q._checkin_round("测试")

        self.assertEqual(calls, ["dt-0", "dt-1"])
        self.assertEqual((secured, total, all_ok), (1, 2, True))
        # BLOCKED 的号不算"本轮新领"，走"签到无份"日志
        self.assertFalse(any("签到成功" in m for m in logs))
        self.assertTrue(any("签到无份" in m for m in logs))

    def test_cmd_checkin_calls_every_account(self):
        accs = [dict(ACCT, token="dt-0")]
        calls = []

        def fake_checkin(acct):
            calls.append(acct["token"])
            return True, {"status": 200, "campaigns": 1, "claimable": True,
                          "claimed": [], "secured": True, "message": "ok"}

        buf = io.StringIO()
        with mock.patch.object(Q, "_load_pool_locked", return_value=accs), \
                mock.patch.object(Q, "_checkin_account", side_effect=fake_checkin), \
                contextlib.redirect_stdout(buf):
            rc = Q._cmd_checkin()
        self.assertEqual(calls, ["dt-0"])
        self.assertEqual(rc, 0)


class CheckinShapeTest(unittest.TestCase):
    """签到请求形态对齐 Qoder 桌面客户端（reverse_asar_findings.md §1/§2/§4/§9）：
    Cosy-Version 用客户端 App 版本号（0.4.8，不是 CLI 的 1.1.69）、
    补 Cosy-MachineHostname（账号字段优先，缺省本机 hostname，DR0 规则 sanitize）、
    campaigns GET 与 claim POST 两种 UA（"Qoder" / Electron UA）、
    Cosy-* 头值 NYe 校验（trim 后非空且全为可打印 ASCII 才发，否则整头丢弃）。"""

    def _headers(self, **acct_extra):
        """mock 本机 hostname=DESKTOP-SHAPE1，保证断言确定性。"""
        acct = dict(ACCT, **acct_extra)
        with mock.patch("socket.gethostname", return_value="DESKTOP-SHAPE1"):
            return Q._cosy_device_headers(acct)

    # ---- 形态常量（集中定义，版本号只改一处）----

    def test_client_shape_constants(self):
        self.assertEqual(Q.CLIENT_APP_VERSION, "0.4.8")
        self.assertEqual(Q.CAMPAIGN_LIST_UA, "Qoder")
        self.assertTrue(Q.CLAIM_UA.startswith(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"))
        self.assertIn("qoder/" + Q.CLIENT_APP_VERSION, Q.CLAIM_UA)   # UA 随 App 版本变
        self.assertIn("Chrome/143.0.0.0", Q.CLAIM_UA)
        self.assertIn("Electron/43.1.1", Q.CLAIM_UA)
        # 对话链路的 CLI 版本常量不受影响
        self.assertEqual(Q.COSY_VERSION, "1.1.69")

    # ---- Cosy-Version：默认客户端 App 版本号，账号字段可覆盖 ----

    def test_cosy_version_defaults_to_client_app_version(self):
        self.assertEqual(self._headers()["Cosy-Version"], "0.4.8")

    def test_cosy_version_account_field_overrides(self):
        self.assertEqual(self._headers(cosyVersion="9.9.9")["Cosy-Version"], "9.9.9")

    # ---- Cosy-MachineHostname（DR0 规则，报告 §4）----

    def test_machine_hostname_defaults_to_local_hostname(self):
        """本机主机名可打印 ASCII → 原值直通（Windows 常规场景）。"""
        self.assertEqual(self._headers()["Cosy-MachineHostname"], "DESKTOP-SHAPE1")

    def test_machine_hostname_account_field_overrides(self):
        self.assertEqual(self._headers(machineHostname="MY-OWN-HOST")["Cosy-MachineHostname"],
                         "MY-OWN-HOST")

    def test_machine_hostname_exactly_96_chars_passthrough(self):
        self.assertEqual(self._headers(machineHostname="H" * 96)["Cosy-MachineHostname"],
                         "H" * 96)

    def test_machine_hostname_non_ascii_falls_back_to_hash(self):
        """全中文主机名：sanitize 后无可打印部分 → unknown-<sha256 前 8 hex>。"""
        raw = "深度学习工作站"
        h = self._headers(machineHostname=raw)
        want = "unknown-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(h["Cosy-MachineHostname"], want)
        self.assertRegex(h["Cosy-MachineHostname"], r"^[\x20-\x7e]{1,96}$")

    def test_machine_hostname_mixed_ascii_falls_back(self):
        """中英混合：可打印部分折叠保留 + -<hash8>。"""
        raw = "我的-PC-01"
        h = self._headers(machineHostname=raw)
        hash8 = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(h["Cosy-MachineHostname"], "PC-01-" + hash8)

    def test_machine_hostname_overlong_ascii_is_truncated_with_hash(self):
        """超长（>96）纯 ASCII：截到 87 + "-" + hash8，总长恰 96。"""
        raw = "H" * 100
        h = self._headers(machineHostname=raw)
        hash8 = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(h["Cosy-MachineHostname"], "H" * 87 + "-" + hash8)
        self.assertEqual(len(h["Cosy-MachineHostname"]), 96)

    def test_machine_hostname_blank_not_sent(self):
        """主机名解析为空 → 整头不发（客户端 NYe：不发空值）。"""
        self.assertNotIn("Cosy-MachineHostname", self._headers(machineHostname="   "))
        with mock.patch("socket.gethostname", return_value=""):
            h = Q._cosy_device_headers(dict(ACCT, machineHostname=""))
        self.assertNotIn("Cosy-MachineHostname", h)

    # ---- NYe 头值校验：空值 / 控制字符整头丢弃，合法值 trim ----

    def test_invalid_cosy_header_values_are_dropped(self):
        h = self._headers(machineId="", machineCode="bad\x01code",
                          machineToken="tok\x7f")
        self.assertNotIn("Cosy-MachineId", h)      # 空值不发
        self.assertNotIn("Cosy-MachineCode", h)    # 控制字符 \x01 不发
        self.assertNotIn("Cosy-MachineToken", h)   # DEL(\x7f) 不在 [\x20-\x7e] 内

    def test_cosy_header_values_are_trimmed(self):
        h = self._headers(machineCode="  code-1  ")
        self.assertEqual(h["Cosy-MachineCode"], "code-1")

    def test_cosy_device_headers_shape_against_client(self):
        """整组头的桌面端形态快照（MachineOS/ClientType/Version/Hostname）。"""
        h = self._headers()
        self.assertEqual(h["Cosy-ClientType"], "10")
        self.assertEqual(h["Cosy-MachineOS"], Q.MACHINE_OS)
        self.assertEqual(Q.MACHINE_OS, "x86_64_win32")
        # 对话链路的 ClientType=5 不许被签到改动波及
        ctx_headers = Q.CosyContext.from_account(
            dict(ACCT, machineId="m1", organizationId="", organizationTags=[]))._base_headers()
        self.assertEqual(ctx_headers["Cosy-ClientType"], "5")

    # ---- 签到链路 UA 区分：列表 GET="Qoder"、claim POST=Electron UA ----

    def test_checkin_uas_split_between_list_and_claim(self):
        captured = []

        def fake_openapi(method, path, body=None, token="", timeout=20,
                         headers=None, ua=None):
            captured.append((method, path, ua))
            if method == "POST":
                return 200, {"status": "CLAIMED", "replayed": False,
                             "grantId": "g1", "grantedAt": "2026-10-20T02:00:00Z"}
            if path == Q.USAGE_PATH:
                return 200, {"addOnQuota": {"total": 100}}
            if path == Q.CAMPAIGNS_PATH:
                return 200, {"claimable": True, "campaigns": [
                    {"id": "camp-1", "campaignKey": "act-1", "actionType": "CLAIM_BENEFIT",
                     "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": 100}}]}
            raise AssertionError("unexpected %s %s" % (method, path))

        with mock.patch.object(Q, "_openapi_json", side_effect=fake_openapi):
            ok, rep = Q._checkin_account(ACCT)
        self.assertTrue(ok, rep)
        uas = {(m, p): ua for m, p, ua in captured}
        self.assertEqual(uas[("GET", Q.CAMPAIGNS_PATH)], Q.CAMPAIGN_LIST_UA)      # 列表=Qoder
        self.assertEqual(uas[("POST", Q.CAMPAIGNS_PATH + "/camp-1/claim")], Q.CLAIM_UA)  # claim=Electron
        # 非签到的 usage 复查不带客户端 UA（保持代理默认）
        self.assertIsNone(uas[("GET", Q.USAGE_PATH)])

    def test_openapi_json_default_ua_and_override(self):
        """ua 参数真正落到请求头；不传时仍是全局 USER_AGENT（登录/续期不受影响）。"""
        reqs = []

        class _Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = mock.Mock()
        opener.open = lambda req, timeout=None: (reqs.append(req) or _Resp())
        with mock.patch.object(Q, "_OPENER", opener):
            Q._openapi_json("GET", "/x")
            Q._openapi_json("GET", "/x", ua=Q.CAMPAIGN_LIST_UA)
        # urllib Request 的 header 键是 capitalize 后的 "User-agent"
        self.assertEqual(reqs[0].headers["User-agent"], Q.USER_AGENT)
        self.assertEqual(reqs[1].headers["User-agent"], Q.CAMPAIGN_LIST_UA)


class DeviceTest(unittest.TestCase):
    """默认每个号一套设备身份：撞车的重发、原来那套留给续期、重跑幂等。"""

    def _pool(self, n=3, mid="host-machine"):
        return [{"account": "a%d" % i, "uid": "u%d" % i, "token": "dt-%d" % i,
                 "machineId": mid, "machineToken": "host-token",
                 "machineType": "abcdef0123456789ab",
                 "machineCode": "aaaaaaaaaaaaaaaaaa"} for i in range(n)]

    def test_duplicate_host_ids_become_one_per_account(self):
        accs = self._pool()
        self.assertEqual(Q._isolate_pool_devices(accs), ["a1", "a2"])
        self.assertEqual(len({a["machineId"] for a in accs}), 3)
        self.assertEqual(accs[0]["machineId"], "host-machine")
        for a in accs[1:]:
            self.assertEqual(a["loginMachineId"], "host-machine")
            self.assertEqual(a["loginMachineToken"], "host-token")
            self.assertEqual(len(a["machineId"]), 36)
            self.assertNotEqual(a["machineType"], a["machineCode"])
            self.assertRegex(a["machineType"], r"^[0-9a-f]{18}$")
            self.assertRegex(a["machineCode"], r"^[0-9a-f]{18}$")
            self.assertEqual(a["machineToken"], "")

    def test_second_run_is_stable(self):
        accs = self._pool()
        Q._isolate_pool_devices(accs)
        before = [a["machineId"] for a in accs]
        self.assertEqual(Q._isolate_pool_devices(accs), [])
        self.assertEqual([a["machineId"] for a in accs], before)

    def test_missing_id_gets_one_and_keeps_refresh_blind(self):
        accs = [{"account": "a0", "uid": "u0", "token": "t"}]
        self.assertEqual(Q._isolate_pool_devices(accs), ["a0"])
        self.assertNotIn("loginMachineId", accs[0])
        self.assertEqual(Q._refresh_device_fields(accs[0]),
                         {"machine_id": accs[0]["machineId"]})

    def test_distinct_ids_are_left_alone(self):
        accs = self._pool(2)
        accs[1]["machineId"] = "another-host"
        accs[1]["machineToken"] = "another-token"
        self.assertEqual(Q._isolate_pool_devices(accs), [])
        self.assertEqual(accs[1]["machineId"], "another-host")

    def test_host_mode_keeps_shared_identity(self):
        accs = self._pool()
        with mock.patch.object(Q, "DEVICE_MODE", "host"):
            self.assertEqual(Q._isolate_pool_devices(accs), [])
            self.assertEqual([a["machineId"] for a in accs], ["host-machine"] * 3)
            self.assertEqual(Q._device_state(accs[0], {}), "本机共用")

    def test_refresh_uses_the_device_the_token_was_issued_to(self):
        isolated = {"machineId": "11111111-2222-3333-4444-555555555555",
                    "machineToken": "",
                    "loginMachineId": "host-machine",
                    "loginMachineToken": "host-token"}
        self.assertEqual(Q._refresh_device_fields(isolated),
                         {"machine_id": "host-machine", "machine_token": "host-token"})
        self.assertEqual(Q._refresh_device_fields({"machineId": "only"}),
                         {"machine_id": "only"})
        self.assertEqual(Q._refresh_device_fields({}), {})

    def test_panel_device_state_and_summary(self):
        accs = self._pool()
        Q._isolate_pool_devices(accs)
        counts = Q._device_counts(accs)
        self.assertEqual([Q._device_state(a, counts) for a in accs], ["独立"] * 3)
        rows = [{"account": a["account"], "device": Q._device_state(a, counts)} for a in accs]
        self.assertEqual(Q._device_summary(rows), "3/3 各一套")
        shared = [{"account": "x", "machineId": "host-machine"}]
        self.assertEqual(Q._device_counts(shared + shared), {"host-machine": 2})
        self.assertEqual(Q._device_state(shared[0], Q._device_counts(shared + shared)), "共用")


# ── 管理层集成（qoder_admin）：多 Key 鉴权 / 管理 API / 模型映射 ─────────────
# 全部离线：KeyStore / 账号池 / 路由文件都指向临时目录，上游交互一律假回调。

import json
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import qoder_admin as QA


ADMIN_PW = "test-admin-pw-000"

MODEL_ADMIN = {
    "key": "up-max", "source": "system", "name": "Upstream Max",
    "reasoning": False, "vision": False, "ctx": 128000,
    "max_output_tokens": 8192, "default": True, "enabled": True,
    "price_factor": 1, "format": "openai", "scene": "chat",
    "thinking_config": None,
}

FAKE_ACCT_A = {
    "account": "acct-a", "uid": "aaaa1111", "token": "dt-fake-a",
    "refreshToken": "rt-fake-a", "expiresAt": "2030-01-01T00:00:00Z",
    "exp": 1893456000, "machineId": "fake-machine-aaaa", "machineToken": "",
    "machineType": "aaaaaaaaaaaaaaaaaa", "machineCode": "bbbbbbbbbbbbbbbbbb",
    "displayName": "Fake A", "session_seed": "acct-a",
    "organizationId": "", "organizationTags": [],
}

FAKE_ACCT_B = {
    "account": "acct b", "uid": "bbbb2222", "token": "dt-fake-b",
    "refreshToken": "rt-fake-b", "expiresAt": "2030-01-01T00:00:00Z",
    "exp": 1893456000, "machineId": "fake-machine-bbbb", "machineToken": "",
    "machineType": "cccccccccccccccccc", "machineCode": "dddddddddddddddddd",
    "displayName": "Fake B", "session_seed": "acct b",
    "organizationId": "", "organizationTags": [],
}


class _AdminEnv:
    """每个测试一副独立的管理层环境（临时目录 + 注入的 KeyStore 单例）。

    patch_all 把池文件 / KeyStore 文件 / 路由文件 / 密码 / 单 Key 全部指到
    测试值，并盖掉 _read_checkin_state（不碰用户真实的签到状态文件）。
    """

    def __init__(self, accounts=None):
        self.dir = tempfile.mkdtemp(prefix="qip-admin-")
        self.accounts_file = os.path.join(self.dir, "pool.json")
        self.keys_file = os.path.join(self.dir, "keys.json")
        self.routes_file = os.path.join(self.dir, "routes.json")
        if accounts is not None:
            with open(self.accounts_file, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "accounts": accounts}, f,
                          ensure_ascii=False, indent=2)
        self.store = QA.KeyStore(self.keys_file)

    def patch_all(self, password=ADMIN_PW, api_key=""):
        return mock.patch.multiple(
            Q,
            ACCOUNTS_FILE=self.accounts_file,
            MODEL_ROUTES_FILE=self.routes_file,
            ADMIN_PASSWORD=password,
            API_KEY=api_key,
            _key_store=self.store,
            _model_routes={"mappings": {}, "aliases": {}, "mtime": 0.0},
            _read_checkin_state=lambda: {},
        )

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class _LocalServer:
    """本地起服（127.0.0.1 随机端口），请求不触外网。"""

    def __init__(self):
        self.httpd = Q.QuietServer(("127.0.0.1", 0), Q.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)
        self.thread.start()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _http(method, url, body=None, headers=None):
    """发一条本地 HTTP 请求；4xx/5xx 不抛，返回 (status, headers, body)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class KeyAuthTest(unittest.TestCase):
    """多 Key 鉴权（qoder_admin.KeyStore 优先）+ 单 Key env 兼容 + 不鉴权。"""

    def _env(self, keys=None):
        env = _AdminEnv()
        for opts in (keys or []):
            env.store.create(opts.get("name", "k"), opts)
        return env

    def test_no_keys_no_env_allows_all(self):
        env = self._env()
        with env.patch_all(api_key=""):
            self.assertTrue(Q._check_key_auth(["anything"])[0])
            self.assertTrue(Q._check_key_auth([])[0])
        env.cleanup()

    def test_single_key_env_compat(self):
        env = self._env()
        with env.patch_all(api_key="legacy-single-key"):
            self.assertTrue(Q._check_key_auth(["legacy-single-key"])[0])
            ok, status, _msg, _t = Q._check_key_auth(["wrong"])
            self.assertFalse(ok)
            self.assertEqual(status, 401)
            # X-Api-Key 候选（Bearer 空串排前面）一样生效
            self.assertTrue(Q._check_key_auth(["", "legacy-single-key"])[0])
        env.cleanup()

    def test_single_key_env_wrong_when_both_candidates(self):
        env = self._env()
        with env.patch_all(api_key="legacy-single-key"):
            ok, status, _msg, _t = Q._check_key_auth(["bad-bearer", "bad-xkey"])
            self.assertFalse(ok)
            self.assertEqual(status, 401)
        env.cleanup()

    def test_keystore_takes_priority_over_env(self):
        env = self._env([{"key": "sk-multi-hit-000000001"}])
        with env.patch_all(api_key="legacy-should-be-ignored"):
            ok, _s, _m, _t = Q._check_key_auth(["sk-multi-hit-000000001"])
            self.assertTrue(ok)
            # KeyStore 非空时，env 单 Key 不再兜底
            ok, status, _m, _t = Q._check_key_auth(["legacy-should-be-ignored"])
            self.assertFalse(ok)
            self.assertEqual(status, 401)
            ok, status, _m, _t = Q._check_key_auth([])
            self.assertFalse(ok)
            self.assertEqual(status, 401)
        env.cleanup()

    def test_keystore_disabled_and_expired_401(self):
        env = self._env()
        row = env.store.create("d", {"key": "sk-disabled-key-00001"})
        env.store.patch(row["id"], {"enabled": 0})
        env.store.create("e", {"key": "sk-expired-key-000001",
                               "expires_at": 1000000})
        with env.patch_all():
            ok, status, _m, _t = Q._check_key_auth(["sk-disabled-key-00001"])
            self.assertFalse(ok)
            self.assertEqual(status, 401)
            ok, status, _m, _t = Q._check_key_auth(["sk-expired-key-000001"])
            self.assertFalse(ok)
            self.assertEqual(status, 401)
        env.cleanup()

    def test_keystore_quota_429_then_reset(self):
        env = self._env()
        row = env.store.create("q", {"key": "sk-quota-key-00000001",
                                     "max_calls": 1})
        with env.patch_all():
            self.assertTrue(Q._check_key_auth(["sk-quota-key-00000001"])[0])
            ok, status, _m, _t = Q._check_key_auth(["sk-quota-key-00000001"])
            self.assertFalse(ok)
            self.assertEqual(status, 429)
            self.assertEqual(env.store.reset_usage(row["id"]),
                             {"ok": True, "used": 0})
            self.assertTrue(Q._check_key_auth(["sk-quota-key-00000001"])[0])
        env.cleanup()


class ModelRouteTest(unittest.TestCase):
    """模型名路由：aliases 先（一次替换）、mappings 后（链式 + 防环）；透传。"""

    def _env(self, mappings=None, aliases=None):
        env = _AdminEnv()
        with open(env.routes_file, "w", encoding="utf-8") as f:
            json.dump({"mappings": mappings or {}, "aliases": aliases or {}}, f)
        return env

    def test_alias_resolved(self):
        env = self._env(aliases={"fast": "up-max"})
        with env.patch_all():
            self.assertEqual(Q._route_model_name("fast"), "up-max")
        env.cleanup()

    def test_mapping_resolved(self):
        env = self._env(mappings={"gpt-4o": "up-max"})
        with env.patch_all():
            self.assertEqual(Q._route_model_name("gpt-4o"), "up-max")
        env.cleanup()

    def test_alias_then_mapping_chain(self):
        env = self._env(mappings={"gpt-4o": "up-max"}, aliases={"fast": "gpt-4o"})
        with env.patch_all():
            self.assertEqual(Q._route_model_name("fast"), "up-max")
        env.cleanup()

    def test_chained_mapping(self):
        env = self._env(mappings={"a": "b", "b": "c"})
        with env.patch_all():
            self.assertEqual(Q._route_model_name("a"), "c")
        env.cleanup()

    def test_cycle_is_safe(self):
        env = self._env(mappings={"a": "b", "b": "a"})
        with env.patch_all():
            self.assertIn(Q._route_model_name("a"), ("a", "b"))
        env.cleanup()

    def test_passthrough_unknown(self):
        env = self._env(mappings={"gpt-4o": "up-max"}, aliases={"fast": "up-max"})
        with env.patch_all():
            self.assertEqual(Q._route_model_name("up-max"), "up-max")
            self.assertEqual(Q._route_model_name("claude-x"), "claude-x")
            self.assertEqual(Q._route_model_name(""), "")
        env.cleanup()

    def test_patch_routes_persist_and_reload(self):
        env = _AdminEnv()
        with env.patch_all():
            out = Q._admin_patch_model_routes(
                {"mappings": {"gpt-4o": "up-max"}, "aliases": {"fast": "gpt-4o"}})
            self.assertEqual(out["mappings"], {"gpt-4o": "up-max"})
            with open(env.routes_file, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["aliases"], {"fast": "gpt-4o"})
            # 换个全新内存状态（模拟另一进程/重启）重读同一文件
            with mock.patch.object(Q, "_model_routes",
                                   {"mappings": {}, "aliases": {}, "mtime": 0.0}):
                got = Q._admin_model_routes()
            self.assertEqual(got["mappings"], {"gpt-4o": "up-max"})
            self.assertEqual(Q._route_model_name("fast"), "up-max")
        env.cleanup()

    def test_patch_routes_filters_blank(self):
        env = _AdminEnv()
        with env.patch_all():
            out = Q._admin_patch_model_routes(
                {"mappings": {"": "x", "gpt-4o": "up-max"},
                 "aliases": {"fast": "", "fast2": "  "}})
            self.assertEqual(out["mappings"], {"gpt-4o": "up-max"})
            self.assertEqual(out["aliases"], {})
        env.cleanup()


class RequestStatsTest(unittest.TestCase):
    """今日请求计数：UTC 日切，进程内存；stats 面板读取快照。"""

    def test_bump_and_snapshot(self):
        with mock.patch.object(Q, "_req_stats", {"date": "", "requests": 0}):
            Q._bump_req_stats()
            Q._bump_req_stats()
            snap = Q._req_stats_snapshot()
            self.assertEqual(snap["requests"], 2)
            self.assertEqual(snap["total_tokens"], 0)   # 没有计量来源，不虚构

    def test_snapshot_other_day_is_zero(self):
        with mock.patch.object(Q, "_req_stats", {"date": "2000-01-01",
                                                 "requests": 99}):
            self.assertEqual(Q._req_stats_snapshot()["requests"], 0)


class CheckinSummaryTest(unittest.TestCase):
    """签到摘要 adapter：读签到状态文件，只汇报当天的全池汇总（不虚构分号明细）。"""

    def _state(self, date=None):
        return {"date": date or time.strftime("%Y-%m-%d"),
                "attempts": 1, "done": True, "secured_accounts": 2,
                "pool_accounts": 3, "last_at": "2026-10-21T11:30:00",
                "last_all_ok": True}

    def test_today_state_is_summarized(self):
        env = _AdminEnv()
        with env.patch_all(), \
                mock.patch.object(Q, "_read_checkin_state",
                                  return_value=self._state()):
            rows = Q._admin_latest_checkins()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["secured"])
            self.assertEqual(rows[0]["amount"], 200)      # 2 个号 × 100 Credits
            self.assertIn("2/3", rows[0]["message"])
        env.cleanup()

    def test_stale_state_is_hidden(self):
        env = _AdminEnv()
        with env.patch_all(), \
                mock.patch.object(Q, "_read_checkin_state",
                                  return_value=self._state(date="2000-01-01")):
            self.assertEqual(Q._admin_latest_checkins(), [])
        env.cleanup()


class AdminRouteTest(unittest.TestCase):
    """经 HTTP 层的 admin 路由 + LLM 端点多 Key 鉴权（本地起服，上游全假回调）。"""

    @classmethod
    def setUpClass(cls):
        cls.server = _LocalServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def _call(self, method, path, body=None, headers=None, admin_auth=True):
        h = dict(headers or {})
        if admin_auth and "Authorization" not in h:
            h["Authorization"] = "Bearer " + ADMIN_PW
        return _http(method, self.server.url(path), body=body, headers=h)

    def _jbody(self, raw):
        return json.loads(raw.decode("utf-8"))

    def _fake_models(self):
        cached = {"at": time.time(), "list": [dict(MODEL_ADMIN)],
                  "by_id": {}, "alias": {}}
        return mock.patch.object(Q, "_models", cached)

    # ---- /admin 页面与鉴权 ----

    def test_admin_page_served(self):
        env = _AdminEnv()
        with env.patch_all():
            st, hdrs, raw = self._call("GET", "/admin", admin_auth=False)
            self.assertEqual(st, 200)
            self.assertIn("text/html", hdrs.get("Content-Type", ""))
            self.assertIn(b"Qoder Admin", raw)
        env.cleanup()

    def test_admin_api_403_without_password(self):
        env = _AdminEnv()
        with env.patch_all(password=""):
            st, _, _raw = self._call("GET", "/api/keys")
            self.assertEqual(st, 403)
        env.cleanup()

    def test_admin_api_401_wrong_password_and_login(self):
        env = _AdminEnv()
        with env.patch_all():
            st, _, _raw = self._call("GET", "/api/keys",
                                     headers={"Authorization": "Bearer wrong"})
            self.assertEqual(st, 401)
            # 页面登录端点不吃 Bearer，直接比对密码
            st, _, _raw = self._call("POST", "/api/auth/login",
                                     body={"password": ADMIN_PW}, admin_auth=False)
            self.assertEqual(st, 200)
            st, _, _raw = self._call("POST", "/api/auth/login",
                                     body={"password": "nope"}, admin_auth=False)
            self.assertEqual(st, 401)
        env.cleanup()

    # ---- /api/keys CRUD + 数据面联动 ----

    def test_keys_crud_over_http(self):
        env = _AdminEnv()
        with env.patch_all(), self._fake_models():
            st, _, raw = self._call("POST", "/api/keys",
                                    body={"name": "测试甲",
                                          "key": "sk-http-0000000001",
                                          "max_calls": 2})
            self.assertEqual(st, 200)
            row = self._jbody(raw)
            self.assertEqual(row["key_value"], "sk-http-0000000001")
            kid = row["id"]

            st, _, raw = self._call("GET", "/api/keys")
            self.assertEqual([r["id"] for r in self._jbody(raw)["keys"]], [kid])

            st, _, raw = self._call("PATCH", "/api/keys/" + kid,
                                    body={"name": "改名"})
            self.assertEqual(st, 200)
            self.assertEqual(self._jbody(raw)["name"], "改名")

            # 数据面：X-Api-Key 头也认；max_calls=2 → 第三次 429
            for i in range(2):
                st, _, _raw = self._call("GET", "/v1/models", admin_auth=False,
                                         headers={"X-Api-Key": "sk-http-0000000001"})
                self.assertEqual(st, 200, i)
            st, _, _raw = self._call("GET", "/v1/models", admin_auth=False,
                                     headers={"X-Api-Key": "sk-http-0000000001"})
            self.assertEqual(st, 429)

            st, _, raw = self._call("POST", "/api/keys/%s/reset" % kid)
            self.assertEqual(st, 200)
            self.assertEqual(self._jbody(raw), {"ok": True, "used": 0})
            st, _, _raw = self._call("GET", "/v1/models", admin_auth=False,
                                     headers={"X-Api-Key": "sk-http-0000000001"})
            self.assertEqual(st, 200)

            st, _, raw = self._call("DELETE", "/api/keys/" + kid)
            self.assertEqual(st, 200)
            # 删光最后一个 Key 后 KeyStore 为空 → 按规格回退"不鉴权"
            # （KeyStore 空 且 env 无单 Key → 不鉴权）；已删的 key 不再有效
            st, _, _raw = self._call("GET", "/v1/models", admin_auth=False,
                                     headers={"X-Api-Key": "sk-http-0000000001"})
            self.assertEqual(st, 200)
        env.cleanup()

    # ---- /api/accounts（假池文件 + 假回调）----

    def test_accounts_list_and_lifecycle(self):
        env = _AdminEnv(accounts=[dict(FAKE_ACCT_A)])
        with env.patch_all(), self._fake_models():
            st, _, raw = self._call("GET", "/api/accounts")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertEqual(len(body["accounts"]), 1)
            row = body["accounts"][0]
            self.assertEqual(row["id"], "acct-a")
            self.assertEqual(row["label"], "acct-a")
            self.assertTrue(row["has_refresh"])
            self.assertFalse(row["refresh_dead"])
            self.assertNotIn("token", row)          # 公开视图不回显 token
            self.assertIn("checkins", body)

            # import：缺 token 的条目跳过；带 token 的入池
            st, _, raw = self._call("POST", "/api/accounts/import",
                                    body=[{"token": "dt-fake-b", "uid": "bbbb2222",
                                           "account": "acct-b"},
                                          {"uid": "no-token"}])
            self.assertEqual(st, 200)
            out = self._jbody(raw)
            self.assertEqual(out["imported"], 1)
            self.assertEqual(out["skipped"], 1)

            st, _, raw = self._call("GET", "/api/accounts")
            ids = [r["id"] for r in self._jbody(raw)["accounts"]]
            self.assertEqual(ids, ["acct-a", "acct-b"])

            # patch：未知 404；已知返回更新后的公开视图
            st, _, _raw = self._call("PATCH", "/api/accounts/zzz",
                                     body={"displayName": "x"})
            self.assertEqual(st, 404)
            st, _, raw = self._call("PATCH", "/api/accounts/acct-b",
                                    body={"displayName": "Fake B"})
            self.assertEqual(st, 200)
            self.assertEqual(self._jbody(raw)["label"], "acct-b")

            # delete 幂等 {ok:true}
            st, _, raw = self._call("DELETE", "/api/accounts/acct-b")
            self.assertEqual(st, 200)
            self.assertEqual(self._jbody(raw), {"ok": True})
            st, _, _raw = self._call("DELETE", "/api/accounts/acct-b")
            self.assertEqual(st, 200)
            st, _, raw = self._call("GET", "/api/accounts")
            ids = [r["id"] for r in self._jbody(raw)["accounts"]]
            self.assertEqual(ids, ["acct-a"])
        env.cleanup()

    def test_accounts_label_with_space_is_url_decoded(self):
        """账号标签带空格：浏览器 percent-encode，转接层要 unquote 回来才能命中。"""
        env = _AdminEnv(accounts=[dict(FAKE_ACCT_B)])
        with env.patch_all(), self._fake_models():
            quoted = urllib.parse.quote("acct b")
            st, _, raw = self._call("POST", "/api/accounts/%s/refresh" % quoted)
            self.assertEqual(st, 200)          # 命中假回调（见下）而非 404
        env.cleanup()

    def test_account_actions_with_fake_callbacks(self):
        env = _AdminEnv(accounts=[dict(FAKE_ACCT_A)])
        checkin_calls, refresh_calls, send_calls = [], [], []

        def fake_checkin(acct):
            checkin_calls.append(acct.get("account"))
            return True, {"status": 200, "campaigns": 1, "claimable": False,
                          "claimed": [], "secured": True,
                          "message": "今日 100 Credits 已到账（测试假回执）"}

        def fake_refresh(acct, force=False):
            refresh_calls.append((acct.get("account"), force))
            out = dict(acct)
            out.update({"expiresAt": "2031-01-01T00:00:00Z", "exp": 1990000000,
                        "refresh_dead": False})
            return out

        class _Resp:
            def read(self):
                return b'{"chat": [{"key": "m1", "display_name": "M1"}, {"key": "m2"}]}'

            def close(self):
                pass

        def fake_send(acct, params, timeout=None):
            send_calls.append((acct.get("account"), params.get("path")))
            return 200, _Resp()

        with env.patch_all(), \
                mock.patch.object(Q, "_checkin_account", side_effect=fake_checkin), \
                mock.patch.object(Q, "_refresh_account", side_effect=fake_refresh), \
                mock.patch.object(Q, "_signed_send", side_effect=fake_send):
            st, _, raw = self._call("POST", "/api/accounts/acct-a/checkin")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertTrue(body["ok"])
            self.assertTrue(body["rep"]["secured"])
            self.assertIn("message", body["rep"])
            self.assertEqual(checkin_calls, ["acct-a"])

            st, _, raw = self._call("POST", "/api/accounts/acct-a/refresh")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertTrue(body["ok"])
            self.assertEqual(body["expires_at"], "2031-01-01T00:00:00Z")
            self.assertEqual(refresh_calls, [("acct-a", True)])   # 手动续期 force

            st, _, raw = self._call("POST", "/api/accounts/acct-a/test")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertTrue(body["ok"])
            self.assertEqual(body["models"], 2)
            self.assertEqual(send_calls, [("acct-a", Q.MODEL_LIST_PATH)])

            st, _, _raw = self._call("POST", "/api/accounts/zzz/checkin")
            self.assertEqual(st, 404)
        env.cleanup()

    def test_account_test_upstream_failure_502(self):
        env = _AdminEnv(accounts=[dict(FAKE_ACCT_A)])

        class _Resp:
            def read(self):
                return b"boom"

            def close(self):
                pass

        def fake_send(acct, params, timeout=None):
            return 500, _Resp()

        with env.patch_all(), \
                mock.patch.object(Q, "_signed_send", side_effect=fake_send):
            st, _, raw = self._call("POST", "/api/accounts/acct-a/test")
            self.assertEqual(st, 502)
            self.assertIn("error", self._jbody(raw))
        env.cleanup()

    def test_account_refresh_without_refresh_token(self):
        acct = dict(FAKE_ACCT_A)
        acct.pop("refreshToken")
        env = _AdminEnv(accounts=[acct])
        with env.patch_all(), self._fake_models():
            st, _, raw = self._call("POST", "/api/accounts/acct-a/refresh")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertFalse(body["ok"])
            self.assertIn("refreshToken", body["error"])
        env.cleanup()

    # ---- /api/stats、/api/models、/api/model-routes ----

    def test_stats_models_routes(self):
        env = _AdminEnv(accounts=[dict(FAKE_ACCT_A)])
        with env.patch_all(), self._fake_models():
            st, _, raw = self._call("GET", "/api/stats")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertEqual(body["accounts"], 1)
            self.assertEqual(body["models"], 1)
            self.assertEqual(body["keys"], 0)          # AdminApi 自动补
            self.assertEqual(body["today"]["requests"], 0)   # 无 chat 调用，不虚构
            self.assertEqual(body["checkins"], [])     # 状态文件被盖成空

            st, _, raw = self._call("GET", "/api/models")
            self.assertEqual(st, 200)
            body = self._jbody(raw)
            self.assertEqual([m["id"] for m in body["models"]], ["up-max"])
            self.assertTrue(body["updated_at"])

            st, _, raw = self._call("PUT", "/api/model-routes",
                                    body={"mappings": {"gpt-4o": "up-max"},
                                          "aliases": {"fast": "gpt-4o"}})
            self.assertEqual(st, 200)
            st, _, raw = self._call("GET", "/api/model-routes")
            body = self._jbody(raw)
            self.assertEqual(body["mappings"], {"gpt-4o": "up-max"})
            self.assertEqual(body["aliases"], {"fast": "gpt-4o"})
        env.cleanup()

    def test_v1_models_includes_route_names(self):
        """/v1/models 附假名（aliases）与映射源名（mappings），指向目标模型元数据。"""
        env = _AdminEnv()
        with env.patch_all(), self._fake_models():
            self._call("PUT", "/api/model-routes",
                       body={"mappings": {"gpt-4o": "up-max"},
                             "aliases": {"fast": "gpt-4o"}})
            st, _, raw = self._call("GET", "/v1/models", admin_auth=False)
            self.assertEqual(st, 200)
            ids = [m["id"] for m in self._jbody(raw)["data"]]
            self.assertIn("up-max", ids)
            self.assertIn("fast", ids)        # 假名
            self.assertIn("gpt-4o", ids)      # 映射源名
            # 悬空映射（目标不在上游清单）不虚构条目
            self._call("PUT", "/api/model-routes",
                       body={"mappings": {"dangling": "no-such-upstream"}})
            st, _, raw = self._call("GET", "/v1/models", admin_auth=False)
            ids = [m["id"] for m in self._jbody(raw)["data"]]
            self.assertNotIn("dangling", ids)
        env.cleanup()

    # ---- LLM 端点鉴权（新 _check_api_key）----

    def test_llm_endpoint_bearer_auth(self):
        env = _AdminEnv()
        env.store.create("bk", {"key": "sk-bearer-00000000001"})
        with env.patch_all(api_key=""), self._fake_models():
            st, _, _raw = self._call("GET", "/v1/models", admin_auth=False)
            self.assertEqual(st, 401)
            st, _, _raw = self._call(
                "GET", "/v1/models", admin_auth=False,
                headers={"Authorization": "Bearer sk-bearer-00000000001"})
            self.assertEqual(st, 200)
            st, _, _raw = self._call(
                "GET", "/v1/models", admin_auth=False,
                headers={"Authorization": "Bearer nope"})
            self.assertEqual(st, 401)
        env.cleanup()

    def test_patch_delete_outside_admin_is_404(self):
        env = _AdminEnv()
        with env.patch_all():
            st, _, _raw = self._call("PATCH", "/v1/models", body={"x": 1},
                                     admin_auth=False)
            self.assertEqual(st, 404)
            st, _, _raw = self._call("DELETE", "/v1/chat/completions",
                                     admin_auth=False)
            self.assertEqual(st, 404)
        env.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
