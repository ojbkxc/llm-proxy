# -*- coding: utf-8 -*-
"""qoder_intl_proxy.py 的离线测试：只测国际版独有的配置与 v3 请求体（不触网）。"""
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


class CheckinTest(unittest.TestCase):
    def test_claim_body_status_does_not_override_http_status(self):
        """国际版回执体 status:"CLAIMED" 不能覆盖 HTTP 200，否则成功被误判为失败。"""
        campaign = {"id": "camp-1", "campaignKey": "act-1",
                    "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
                    "benefit": {"kind": "CREDITS", "amount": 100}}
        posted = []

        def fake_openapi(method, path, body=None, token="", timeout=20, headers=None):
            if method == "POST":
                posted.append(path)
                return 200, {"status": "CLAIMED", "grantId": "grant-1",
                             "claimedAt": "2026-10-20T00:00:00Z", "replayed": False}
            if path == Q.USAGE_PATH:
                return 200, {"addOnQuota": {"total": 100}}
            if path == Q.CAMPAIGNS_PATH:
                if posted:
                    claimed = dict(campaign, claimStatus="CLAIMED")
                    return 200, {"claimable": False, "campaigns": [claimed]}
                return 200, {"claimable": True, "campaigns": [campaign]}
            raise AssertionError("unexpected %s %s" % (method, path))

        with mock.patch.object(Q, "_openapi_json", side_effect=fake_openapi):
            ok, rep = Q._checkin_account(ACCT)

        self.assertTrue(ok, rep)
        self.assertEqual(posted, [Q.CAMPAIGNS_PATH + "/camp-1/claim"])
        item = rep["claimed"][0]
        self.assertEqual(item["status"], 200)
        self.assertEqual(item["claimStatus"], "CLAIMED")
        self.assertEqual(item["grantId"], "grant-1")
        self.assertTrue(rep["secured"])
        self.assertEqual(rep["addOnTotal"], 100)
        self.assertNotIn("error", rep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
