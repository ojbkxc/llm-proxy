# -*- coding: utf-8 -*-
"""qoder_cosy 的离线回归测试：向量来自客户端 wasm 的确定性回放（全部 dummy 凭据）。"""
import base64
import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qoder_cosy as qc

ENC_VECTORS = [
 [
  "",
  ""
 ],
 [
  "A",
  "$p$p"
 ],
 [
  "AA",
  "$aTp"
 ],
 [
  "AAA",
  "dagp"
 ],
 [
  "AAAA",
  "$$gdpppa"
 ],
 [
  "AAAAA",
  "T$gdpapa"
 ],
 [
  "AAAAAA",
  "gdgdpapa"
 ],
 [
  "AAAAAAA",
  "pp$$pagdpagd"
 ],
 [
  "AAAAAAAA",
  "paT$pagdpagd"
 ],
 [
  "AB",
  "$aBp"
 ],
 [
  "ABC",
  "RaKp"
 ],
 [
  "ABCD",
  "$$KRC_pa"
 ],
 [
  "ABCDE",
  "a$KRCTpa"
 ],
 [
  "ABCDEF",
  "FHKRCTpa"
 ],
 [
  "ABCDEFG",
  "Cf$$CTFHpaKR"
 ],
 [
  "ABCDEFGH",
  "Cw&$CTFHpaKR"
 ],
 [
  "hello",
  "q$FruHPH"
 ],
 [
  "0123456789abcdef",
  "#(CLD&$$jMNIlSg*VRTYV^p."
 ],
 [
  "{\"session_id\":\"probe8\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"model_config\":{\"key\":\"qfmodel\",\"source\":\"system\"},\"stream\":true}",
  "n*NSDtu(CLuoBrBEjbJxKMD,ByBEjQNkCLu,KWGoK^JZKL#Sw*lECYJSFWK.N(FYB*f*#(WhJHFhJoByBOi)BE.JGoKtu(CLugWMu(QOPSN*lEr*P(FQBMmYK^Dxj^PSWhx(LzBMn*NZKb#OaIB*f*uSF^N(gEDxV*lLtXBEKbuHa*l*"
 ]
]

AES_CASE = {
 "key_hex": "31326432316634326164303634323232",
 "iv_hex": "31326432316634326164303634323232",
 "plain_hex": "7b22756964223a227531222c2273656375726974795f6f617574685f746f6b656e223a2264756d6d792d746f6b656e2d30313233343536373839222c226f7267616e697a6174696f6e5f6964223a226f726731222c226f7267616e697a6174696f6e5f74616773223a5b2261222c2262225d2c22646174615f706f6c6963795f616772656564223a747275657d",
 "cipher_b64": "JBSFUtmbMnBpwjmwRCcEG4Ym/rqJoCavxkuYe27R5pgnC6IF6ePzJueX7qQb9KOkHJNLE8aYfx8z/POKSmXGfJ+L2tjplR4sLwBtA74yInqG0dBFsYaDfQzTB2Xme3jjaHfhCj3x9/o174vOM5s542B611Lmr2nOKw6S68O2jbqaOmwdLn9QdVlyvqRcx1K6"
}

RSA_CASE = {
 "m_hex": "31326432316634326164303634323232",
 "uuid_raw_hex": "12d21f42ad0642220000000000000000",
 "ps_hex": "2e60dd3f0fd6b1d6cece185ed212a9b117a94bb7aa7858c487d572d026b0334fc90b9adf8b304e2ba66584d41efe6d111d42fc9aa2bde125c6f79ddf0372b1a9579c22cbfcefa8db266c77c23f4d4ab8bed6fd106a55f7b42391b7b83d8302e785ae7a48e7a3056be86c690c24",
 "key_b64": "dxc+2NyFhtyzjUVL74CdWiviflGq+uuW4cfS+VHBOukE1uz2G9Rf1VOYCQSCJXIT/QRNABiU8wMHmICZJ6qGDN2mowdxZYBuBWIZ+A6T7bIbLmJvtpTT6E3tCVzGkaQWdQFGLuk4jUTK3nAs5z7w0WFU78OKyeea8usMJvPk+jI=",
 "info_b64": "JBSFUtmbMnBpwjmwRCcEG4Ym/rqJoCavxkuYe27R5pgnC6IF6ePzJueX7qQb9KOkHJNLE8aYfx8z/POKSmXGfJ+L2tjplR4sLwBtA74yInqG0dBFsYaDfQzTB2Xme3jjaHfhCj3x9/o174vOM5s542B611Lmr2nOKw6S68O2jbqaOmwdLn9QdVlyvqRcx1K6",
 "plain": "{\"uid\":\"u1\",\"security_oauth_token\":\"dummy-token-0123456789\",\"organization_id\":\"org1\",\"organization_tags\":[\"a\",\"b\"],\"data_policy_agreed\":true}"
}

INFER_CASE = {
 "uid": "u1",
 "machine_id": "machine-id-xyz",
 "cosy_version": "1.1.69",
 "info": "7hMVP51IjbPUmiYMpVfyOUhzdx8i2ZmLIFsLRS1nZRI5Q0Oc7FW5MYPLTS9/O6P1QQ725Cy+Rv9ucdLXiHAqspa5MzJ99EQw8MHuaXUc1xrHwGE3IbH6TfwXNkCWVm1cqUT+Cqtr6rWpKDgntlDdoi3uPFikwziI/NQERdxcjapb08V5K3QXZ5Dy2xFV/Ual",
 "key": "ARtAYQclUISlGmMAvTq9SE6jHyf1s+uGOvVqtzfdeQqGbRcfsi/hAUB3ke789pyA41MMZxFLcRMBU+jc8lo6gjfCj/wUU3lWrJOrgz2vjZzh3dhhd5iFaqVXu0ZCxZ+mKjLX7C0TVrJgCHtl+UJWkQvzRSCXpANSrbTiyB47t1E=",
 "date": 1792203839,
 "request_id": "1c756373-7805-45e0-9fff-a7e93b819104",
 "endpoint": "https://gateway.qoder.com.cn",
 "body": "{\"session_id\":\"probe8\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"model_config\":{\"key\":\"qfmodel\",\"source\":\"system\"},\"stream\":true}",
 "model": "qfmodel",
 "source": "system",
 "expect": {
  "url": "https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1",
  "body": "n*NSDtu(CLuoBrBEjbJxKMD,ByBEjQNkCLu,KWGoK^JZKL#Sw*lECYJSFWK.N(FYB*f*#(WhJHFhJoByBOi)BE.JGoKtu(CLugWMu(QOPSN*lEr*P(FQBMmYK^Dxj^PSWhx(LzBMn*NZKb#OaIB*f*uSF^N(gEDxV*lLtXBEKbuHa*l*",
  "headers": {
   "Accept": "text/event-stream",
   "Cache-Control": "no-cache",
   "Connection": "keep-alive",
   "Content-Type": "application/json",
   "Cosy-Business-Product": "cli",
   "Cosy-Business-Type": "agent",
   "Cosy-ClientType": "5",
   "Cosy-MachineId": "machine-id-xyz",
   "Cosy-MachineToken": "machine-id-xyz",
   "Cosy-MachineType": "5",
   "Cosy-Scene": "assistant",
   "Cosy-Version": "1.1.69",
   "Login-Version": "v2",
   "X-Model-Key": "qfmodel",
   "X-Model-Source": "system",
   "Authorization": "Bearer COSY.eyJ2ZXJzaW9uIjoidjEiLCJyZXF1ZXN0SWQiOiIxYzc1NjM3My03ODA1LTQ1ZTAtOWZmZi1hN2U5M2I4MTkxMDQiLCJpbmZvIjoiN2hNVlA1MUlqYlBVbWlZTXBWZnlPVWh6ZHg4aTJabUxJRnNMUlMxblpSSTVRME9jN0ZXNU1ZUExUUzkvTzZQMVFRNzI1Q3krUnY5dWNkTFhpSEFxc3BhNU16Sjk5RVF3OE1IdWFYVWMxeHJId0dFM0liSDZUZndYTmtDV1ZtMWNxVVQrQ3F0cjZyV3BLRGdudGxEZG9pM3VQRmlrd3ppSS9OUUVSZHhjamFwYjA4VjVLM1FYWjVEeTJ4RlYvVWFsIiwiY29zeVZlcnNpb24iOiIxLjEuNjkiLCJpZGVWZXJzaW9uIjoiIn0=.96956fd1ad48755fba87b247a01f6598",
   "Cosy-Key": "ARtAYQclUISlGmMAvTq9SE6jHyf1s+uGOvVqtzfdeQqGbRcfsi/hAUB3ke789pyA41MMZxFLcRMBU+jc8lo6gjfCj/wUU3lWrJOrgz2vjZzh3dhhd5iFaqVXu0ZCxZ+mKjLX7C0TVrJgCHtl+UJWkQvzRSCXpANSrbTiyB47t1E=",
   "Cosy-User": "u1",
   "Cosy-Date": "1792203839",
   "Cosy-Data-Policy": "disagree"
  }
 }
}

GET_CASE = {
 "uid": "u1",
 "machine_id": "machine-id-xyz",
 "cosy_version": "1.1.69",
 "info": "X9MygJUJcMf528oYRhmJSGipqzUufGSjmECdi1kibCfKQAM/P0zo+O99MUGnx8vgh1/7lVdWq2d6tJjxLQqLrg00fUizJ9+Tx3wz3bt4Hs95Vwa9JxHUenIWAzS1Mv6vWymVWoDlSSIiBgLdZpatzULYsAPW/qk1FnNX76Fz1SQa3M7tGWwV+kLj87ipn0Wj",
 "key": "b21de+7WFC1o/1ZS23VXd1ZFsn/NYrJfwGnHn695RAZUd2ebWhYkWly4y4CjKsQyXb/5RHoif7OdegVqRpWzgKwnjM+PxlolFpdaivJM1b+8vUx3GIgIq+l2A5mJbjwdTyLKYOuvKFJaMhX8AU4ybnk8lpLnL1fUcvpwvMR6xcc=",
 "date": 1792203859,
 "request_id": "ae5b3acd-0068-404e-ad7c-31ba1be23a98",
 "endpoint": "https://gateway.qoder.com.cn",
 "path": "/api/v2/campaigns",
 "policy": "disagree",
 "expect": {
  "url": "https://gateway.qoder.com.cn/algo/api/v2/campaigns",
  "headers": {
   "Accept": "application/json",
   "Accept-Encoding": "identity",
   "Authorization": "Bearer COSY.eyJ2ZXJzaW9uIjoidjEiLCJyZXF1ZXN0SWQiOiJhZTViM2FjZC0wMDY4LTQwNGUtYWQ3Yy0zMWJhMWJlMjNhOTgiLCJpbmZvIjoiWDlNeWdKVUpjTWY1MjhvWVJobUpTR2lwcXpVdWZHU2ptRUNkaTFraWJDZktRQU0vUDB6bytPOTlNVUdueDh2Z2gxLzdsVmRXcTJkNnRKanhMUXFMcmcwMGZVaXpKOStUeDN3ejNidDRIczk1VndhOUp4SFVlbklXQXpTMU12NnZXeW1WV29EbFNTSWlCZ0xkWnBhdHpVTFlzQVBXL3FrMUZuTlg3NkZ6MVNRYTNNN3RHV3dWK2tMajg3aXBuMFdqIiwiY29zeVZlcnNpb24iOiIxLjEuNjkiLCJpZGVWZXJzaW9uIjoiIn0=.1ac46fba41b14843219f577e4f76c99a",
   "Content-Type": "application/json",
   "Cosy-Business-Product": "cli",
   "Cosy-Business-Type": "agent",
   "Cosy-ClientIp": "machine-id-xyz",
   "Cosy-ClientType": "5",
   "Cosy-Data-Policy": "disagree",
   "Cosy-Date": "1792203859",
   "Cosy-Key": "b21de+7WFC1o/1ZS23VXd1ZFsn/NYrJfwGnHn695RAZUd2ebWhYkWly4y4CjKsQyXb/5RHoif7OdegVqRpWzgKwnjM+PxlolFpdaivJM1b+8vUx3GIgIq+l2A5mJbjwdTyLKYOuvKFJaMhX8AU4ybnk8lpLnL1fUcvpwvMR6xcc=",
   "Cosy-MachineId": "machine-id-xyz",
   "Cosy-MachineToken": "machine-id-xyz",
   "Cosy-MachineType": "5",
   "Cosy-Scene": "assistant",
   "Cosy-User": "u1",
   "Cosy-Version": "1.1.69",
   "Login-Version": "v2"
  }
 }
}


def _fields_rng(raw_uuid, ps, total):
    stream = bytearray(bytes.fromhex(raw_uuid) + bytes.fromhex(ps) + bytes(total))
    pos = [0]

    def rng(n):
        chunk = bytes(stream[pos[0]:pos[0] + n])
        pos[0] += n
        return chunk
    return rng


def _ctx(case, fields, policy="disagree"):
    return qc.CosyContext(case["uid"], case["machine_id"], case["cosy_version"], fields,
                          data_policy=policy, clock=lambda: case["date"],
                          rng=lambda n: uuid.UUID(case["request_id"]).bytes
                          if n == 16 else b"\x01" * n)


class EncoderTest(unittest.TestCase):
    def test_vectors(self):
        for plain, want in ENC_VECTORS:
            self.assertEqual(qc.encode_body(plain.encode()).decode(), want, plain)

    def test_empty(self):
        self.assertEqual(qc.encode_body(b""), b"")

    def test_utf8(self):
        raw = "模型".encode()
        self.assertEqual(len(qc.encode_body(raw)), 4 * ((len(raw) + 2) // 3))


class CryptoTests(unittest.TestCase):
    def test_aes_known_answer(self):
        key = bytes.fromhex(AES_CASE["key_hex"])
        iv = bytes.fromhex(AES_CASE["iv_hex"])
        plain = bytes.fromhex(AES_CASE["plain_hex"])
        got = base64.b64encode(qc.aes128_cbc_encrypt(key, iv, plain)).decode()
        self.assertEqual(got, AES_CASE["cipher_b64"])

    def test_rsa_known_answer(self):
        m = bytes.fromhex(RSA_CASE["m_hex"])
        ps = bytes.fromhex(RSA_CASE["ps_hex"])
        rng = _fields_rng("", RSA_CASE["ps_hex"], 220)
        em = b"\x00\x02" + ps + b"\x00" + m
        want = pow(int.from_bytes(em, "big"), 65537, qc.RSA_N).to_bytes(128, "big")
        self.assertEqual(base64.b64encode(qc._rsa_encrypt(m, rng)).decode(),
                         base64.b64encode(want).decode())

    def test_auth_fields_replay(self):
        rng = _fields_rng(RSA_CASE["uuid_raw_hex"], RSA_CASE["ps_hex"], 220)
        fields = qc.generate_runtime_auth_fields(RSA_CASE["plain"], rng=rng)
        self.assertEqual(fields["key"], RSA_CASE["key_b64"])
        self.assertEqual(fields["encrypt_user_info"], RSA_CASE["info_b64"])

    def test_rsa_ps_nonzero(self):
        def rng(n):
            return bytes(0 if i % 2 else 0x41 for i in range(n))
        out = qc._rsa_encrypt(b"0123456789abcdef", rng)
        self.assertEqual(len(out), 128)


class RequestTests(unittest.TestCase):
    def test_infer_request_exact(self):
        case = INFER_CASE
        fields = {"encrypt_user_info": case["info"], "key": case["key"]}
        ctx = _ctx(case, fields)
        got = ctx.prepare_infer_request(case["endpoint"], case["body"],
                                        case["model"], case["source"])
        want = case["expect"]
        self.assertEqual(got["url"], want["url"])
        self.assertEqual(got["body"], want["body"])
        self.assertEqual(got["headers"], want["headers"])

    def test_get_request_exact(self):
        case = GET_CASE
        fields = {"encrypt_user_info": case["info"], "key": case["key"]}
        ctx = _ctx(case, fields, policy=case["policy"])
        got = ctx.prepare_request(case["endpoint"], case["path"], "GET")
        want = case["expect"]
        self.assertEqual(got["url"], want["url"])
        self.assertEqual(got["headers"], want["headers"])
        self.assertNotIn("body", got)

    def test_path_forms_equivalent(self):
        case = GET_CASE
        fields = {"encrypt_user_info": case["info"], "key": case["key"]}
        ctx = _ctx(case, fields, policy=case["policy"])
        a = ctx.prepare_request(case["endpoint"], "/api/v2/campaigns", "GET")
        b = ctx.prepare_request(case["endpoint"], "/algo/api/v2/campaigns", "GET")
        self.assertEqual(a, b)

    def test_query_kept_in_url_not_signature(self):
        case = GET_CASE
        fields = {"encrypt_user_info": case["info"], "key": case["key"]}
        ctx = _ctx(case, fields, policy=case["policy"])
        a = ctx.prepare_request(case["endpoint"], "/api/v2/campaigns?x=1", "GET")
        b = ctx.prepare_request(case["endpoint"], "/api/v2/campaigns?x=2", "GET")
        self.assertTrue(a["url"].endswith("?x=1"))
        self.assertEqual(a["headers"], b["headers"])

    def test_policy_flag(self):
        case = GET_CASE
        fields = {"encrypt_user_info": case["info"], "key": case["key"]}
        got = _ctx(case, fields, "agree").prepare_request(case["endpoint"], case["path"], "GET")
        self.assertEqual(got["headers"]["Cosy-Data-Policy"], "agree")

    def test_extra_headers(self):
        case = GET_CASE
        fields = {"encrypt_user_info": case["info"], "key": case["key"]}
        r = _ctx(case, fields).prepare_request(case["endpoint"], case["path"], "GET",
                                               extra_headers={"X-Test": "1"})
        self.assertEqual(r["headers"]["X-Test"], "1")

    def test_from_account_shape(self):
        acct = {"uid": "u1", "token": "dummy", "machineId": "mid",
                "cosyVersion": "1.1.69", "organizationId": "",
                "organizationTags": [], "dataPolicyAgreed": False}
        ctx = qc.CosyContext.from_account(acct)
        self.assertTrue(ctx.info)
        self.assertTrue(ctx.key)
        r = ctx.prepare_request("https://gateway.qoder.com.cn", "/api/v2/model/list", "GET")
        self.assertTrue(r["headers"]["Authorization"].startswith("Bearer COSY."))
        self.assertEqual(r["headers"]["Cosy-MachineId"], "mid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
