#!/usr/bin/env python3
"""Atomically apply all OpenAI channel modifications to qoder_proxy.py."""
import re

PATH = r"C:\GitHub\llm-proxy\qoder_proxy.py"

with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

original_len = len(src)
replacements = []

def replace(old, new, desc, required=True):
    """Replace old with new in src. Track for reporting."""
    global src
    count = src.count(old)
    if count == 0:
        if required:
            print(f"FAIL: {desc} - old string not found")
            return False
        else:
            print(f"SKIP: {desc} - not found (optional)")
            return True
    if count > 1:
        print(f"WARN: {desc} - {count} matches, replacing all")
    src = src.replace(old, new)
    replacements.append((desc, count))
    return True

# ── 1. Docstring: update upstream description ──
replace(
    """上游是 Qoder CN 的推理网关（逆向自客户端 worker runtime）：
  POST https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation
       ?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1
  GET  https://gateway.qoder.com.cn/algo/api/v2/model/list
  两者都要求 Authorization: Bearer COSY.<base64>.<sig> 一整套 Cosy-* 签名头，
  且请求体要按 Encode=1 编码 —— 签名与编码实现在客户端的 Rust/WASM 模块
  qoder_auth_wasm_bg.wasm 里，纯 Python 无法复刻（实测自造签名一律 403 Signature invalid）。
  因此签名交给 sidecar qoder_sign.mjs（直接驱动同一个 WASM），本文件只做协议转换与账号管理。

脱离桌面端：只读磁盘上的登录态文件，不启动、不连接 Qoder CN 进程；
签名器是独立 node 子进程，客户端可以完全没打开。""",
    """上游是 Qoder CN 的推理网关（OpenAI 兼容通道，逆向自客户端 worker runtime）：
  POST https://gateway.qoder.com.cn/model/v1/chat/completions
  仅需 Authorization: Bearer <token>，无签名、无加密，请求体标准 OpenAI Chat Completions，
  SSE 标准格式。同网关 host，同登录态 token。
  --login 设备码登录仍走签名 sidecar（qoder_sign.mjs），仅用于获取初始 token。

脱离桌面端：只读磁盘上的登录态文件，不启动、不连接 Qoder CN 进程。
聊天通道不需要 Node；--login 需要 Node + qoder_sign.mjs。""",
    "docstring upstream description"
)

# ── 2. Remove import queue and import shutil (only used by _Signer) ──
replace("import queue\n", "", "remove import queue")
replace("import shutil\n", "", "remove import shutil")

# ── 3. Path constants: MODEL_LIST_PATH/INFER_PATH/INFER_QUERY → CHAT_PATH ──
replace(
    'MODEL_LIST_PATH = "/algo/api/v2/model/list"\n'
    'INFER_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"\n'
    'INFER_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"\n',
    'CHAT_PATH = "/model/v1/chat/completions"\n',
    "path constants"
)

# ── 4. Delete SignerError + _Signer class + _auth_of + _signer instance ──
# This is a big block from "# ── 签名 sidecar" to "_signer = _Signer()"
signer_block_start = "# ── 签名 sidecar（qoder_sign.mjs）────────────────────────────────────────────\nclass SignerError(Exception):"
signer_block_end = "_signer = _Signer()\n"

# Find the block
start_idx = src.find(signer_block_start)
if start_idx < 0:
    print("FAIL: signer block start not found")
else:
    end_idx = src.find(signer_block_end, start_idx)
    if end_idx < 0:
        print("FAIL: signer block end not found")
    else:
        end_idx += len(signer_block_end)
        # Keep the section header for the HTTP pool that follows
        src = src[:start_idx] + src[end_idx:]
        replacements.append(("delete SignerError+_Signer+_auth_of+_signer", 1))

# ── 5. Delete _signed_send function ──
signed_send_start = "def _signed_send(acct, params, timeout=None):\n"
signed_send_end = "    return resp.status, resp\n"

start_idx = src.find(signed_send_start)
if start_idx < 0:
    print("FAIL: _signed_send start not found")
else:
    end_idx = src.find(signed_send_end, start_idx)
    if end_idx < 0:
        print("FAIL: _signed_send end not found")
    else:
        end_idx += len(signed_send_end)
        # Remove the function and surrounding blank lines
        src = src[:start_idx] + src[end_idx:]
        replacements.append(("delete _signed_send", 1))

# ── 6. _fetch_models: replace signed fetch with hardcoded ──
replace(
    """# ── 模型清单 ──────────────────────────────────────────────────────────────────
_models = {"at": 0.0, "list": [], "by_id": {}, "alias": {}}


def _slug(name):""",
    """# ── 模型清单 ──────────────────────────────────────────────────────────────────
_models = {"at": 0.0, "list": [], "by_id": {}, "alias": {}}

# 硬编码模型清单（避免调 /api/v2/model/list，该端点需要 Cosy 签名）。
# 来源：客户端日志 model_config + T2 逆向报告 §2.3。
_HARDCODED_MODELS = [
    {"key": "qfmodel", "source": "system", "name": "Qwen3.8-Flash",
     "reasoning": True, "vision": True, "default": True, "enabled": True,
     "ctx": 180000, "price_factor": None, "format": "openai", "scene": "chat"},
]


def _slug(name):""",
    "add _HARDCODED_MODELS"
)

replace(
    """def _fetch_models(force=False):
    \"\"\"签名拉 /algo/api/v2/model/list，缓存 MODELS_TTL 秒。\"\"\"
    now = time.time()
    if not force and _models["list"] and now - _models["at"] < MODELS_TTL:
        return _models
    acct = _read_session()
    if acct is None:
        raise RuntimeError("账号池为空：先运行 python qoder_proxy.py --capture")
    params = {"endpoint": GATEWAY, "path": MODEL_LIST_PATH, "method": "GET"}
    _log("拉取模型清单")
    status, resp = _signed_send(acct, params)
    try:
        raw = resp.read().decode("utf-8", "replace")
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if status != 200:
        raise RuntimeError("模型清单 HTTP %s: %s" % (status, raw[:200]))
    lst = _normalize_models(json.loads(raw))
    _models["at"] = now
    _models["list"] = lst
    _models["by_id"] = {m["key"]: m for m in lst}
    alias = {}
    for m in lst:
        alias[m["key"].lower()] = m
        alias[_slug(m["name"])] = m
    _models["alias"] = alias
    _log("模型清单 %d 个" % len(lst))
    return _models""",
    """def _fetch_models(force=False):
    \"\"\"返回硬编码模型清单（缓存 MODELS_TTL 秒，force 强制刷新）。\"\"\"
    now = time.time()
    if not force and _models["list"] and now - _models["at"] < MODELS_TTL:
        return _models
    lst = [dict(m) for m in _HARDCODED_MODELS]
    _models["at"] = now
    _models["list"] = lst
    _models["by_id"] = {m["key"]: m for m in lst}
    alias = {}
    for m in lst:
        alias[m["key"].lower()] = m
        alias[_slug(m["name"])] = m
    _models["alias"] = alias
    _log("模型清单 %d 个（硬编码）" % len(lst))
    return _models""",
    "_fetch_models hardcoded"
)

# ── 7. _build_infer_payload: Cosy → standard OpenAI ──
replace(
    """# ── 上游聊天请求体 ───────────────────────────────────────────────────────────
def _session_id(acct):
    \"\"\"同一账号稳定复用同一 session_id（上游按它做上下文与计费聚合）。\"\"\"
    seed = "%s:%s" % (_label_of(acct), acct.get("session_seed") or "")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "qoder-proxy:" + seed))


def _build_infer_payload(oa, model, acct):
    mid = str(uuid.uuid4())
    payload = {
        "session_id": _session_id(acct),
        "request_id": mid,
        "request_set_id": mid,
        "source_session_id": _session_id(acct),
        "scene": SCENE,
        "messages": oa.get("messages") or [],
        "model_config": {"key": model["key"], "source": model.get("source") or "system"},
        "business": {"type": BUSINESS_TYPE, "product": BUSINESS_PRODUCT},
        "stream": True,
    }
    for k in ("tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop",
              "presence_penalty", "frequency_penalty", "seed", "response_format"):
        if oa.get(k) is not None:
            payload[k] = oa[k]
    if payload["model_config"].get("key") in (None, "auto", ""):
        payload["model_config"]["key"] = DEFAULT_MODEL
    return payload""",
    """# ── 上游聊天请求体 ───────────────────────────────────────────────────────────
def _build_infer_payload(oa, model, acct):
    \"\"\"构造标准 OpenAI Chat Completions 请求体（上游 /model/v1/chat/completions）。\"\"\"
    payload = {
        "model": model["key"],
        "messages": oa.get("messages") or [],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    for k in ("tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop",
              "presence_penalty", "frequency_penalty", "seed", "response_format"):
        if oa.get(k) is not None:
            payload[k] = oa[k]
    if not payload.get("model") or payload["model"] in ("auto", ""):
        payload["model"] = DEFAULT_MODEL
    return payload""",
    "_build_infer_payload standard OpenAI"
)

# ── 8. _unwrap_frame: simplify for standard OpenAI SSE ──
replace(
    """def _unwrap_frame(data_text):
    \"\"\"上游信封帧 data:{\"headers\":..,\"body\":\"<chunk JSON>\",\"statusCodeValue\":200} → 内层。

    同时兼容没有信封的标准 OpenAI SSE（直接原样返回）。
    \"\"\"
    if data_text is None:
        return None
    s = data_text.strip()
    if s == "[DONE]":
        return "[DONE]"
    try:
        obj = json.loads(s)
    except Exception:
        return s
    if not isinstance(obj, dict):
        return s
    if "body" in obj and ("statusCodeValue" in obj or "headers" in obj):
        code = obj.get("statusCodeValue")
        if isinstance(code, int) and code >= 400:
            return json.dumps({\"error\": {\"message\": str(obj.get(\"body\") or \"\")[:400],
                                         \"type\": \"upstream_error\", \"code\": code}},
                              ensure_ascii=False)
        inner = obj.get("body")
        if isinstance(inner, str):
            return inner.strip() or None
        return json.dumps(inner, ensure_ascii=False)
    return s""",
    """def _unwrap_frame(data_text):
    \"\"\"标准 OpenAI SSE：data:<JSON> 或 data:[DONE]。原样返回内层文本。\"\"\"
    if data_text is None:
        return None
    s = data_text.strip()
    if not s:
        return None
    return s""",
    "_unwrap_frame simplified"
)

# ── 9. _open_upstream: Bearer + _send directly ──
replace(
    """def _open_upstream(oa, model):
    \"\"\"签名 + 发上游。401/403 换号重试；签名器异常直接 502（换号也没用）。\"\"\"
    with _pool_lock:
        n_acc = len(_load_pool_locked())
    last = None
    for _ in range(max(2, min(n_acc, 4))):
        acct = _read_session()
        if acct is None:
            raise ProxyError(401, "账号池为空：先运行 python qoder_proxy.py --capture")
        params = {"endpoint": GATEWAY, "method": "POST", "path": INFER_PATH,
                  "infer": {"model": model["key"], "source": model.get("source") or "system"},
                  "body": _build_infer_payload(oa, model, acct)}
        try:
            status, resp = _signed_send(acct, params)
        except Exception as e:
            raise ProxyError(502, "签名/连接失败: %s" % e)
        if status in (401, 403):
            detail = _drain(resp)
            _mark_account_failed(acct, status)
            _log("账号 %s 上游 %s，换号: %s" % (_label_of(acct), status, detail[:120]))
            last = ProxyError(401, "上游鉴权失败(%s)，请重新 --capture：%s"
                              % (status, detail[:200]))
            continue
        if status >= 400:
            detail = _drain(resp)
            raise ProxyError(_map_upstream_error(status, detail),
                             "上游 %s: %s" % (status, detail[:300]))
        return acct, resp
    raise last or ProxyError(502, "上游请求失败")""",
    """def _open_upstream(oa, model):
    \"\"\"Bearer token + 发上游 /model/v1/chat/completions。401/403 换号重试。\"\"\"
    with _pool_lock:
        n_acc = len(_load_pool_locked())
    last = None
    for _ in range(max(2, min(n_acc, 4))):
        acct = _read_session()
        if acct is None:
            raise ProxyError(401, "账号池为空：先运行 python qoder_proxy.py --capture")
        token = acct.get("token") or ""
        if not token:
            _mark_account_failed(acct, 401)
            last = ProxyError(401, "账号 %s 无 token" % _label_of(acct))
            continue
        body = json.dumps(_build_infer_payload(oa, model, acct),
                           ensure_ascii=False).encode("utf-8")
        req_id = str(uuid.uuid4())
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Request-ID": req_id,
            "User-Agent": USER_AGENT,
        }
        scheme, host, _ = _split_url(GATEWAY)
        try:
            resp = _send(scheme, host, "POST", CHAT_PATH, headers, body, TIMEOUT_SEC)
            status = resp.status
        except Exception as e:
            raise ProxyError(502, "连接失败: %s" % e)
        if status in (401, 403):
            detail = _drain(resp)
            _mark_account_failed(acct, status)
            _log("账号 %s 上游 %s，换号: %s" % (_label_of(acct), status, detail[:120]))
            last = ProxyError(401, "上游鉴权失败(%s)，请重新 --capture：%s"
                              % (status, detail[:200]))
            continue
        if status >= 400:
            detail = _drain(resp)
            raise ProxyError(_map_upstream_error(status, detail),
                             "上游 %s: %s" % (status, detail[:300]))
        return acct, resp
    raise last or ProxyError(502, "上游请求失败")""",
    "_open_upstream Bearer+_send"
)

# ── 10. _handle_health: remove _signer.peek() ──
replace(
    """        if not accs:
            out["error"] = "账号池为空，请先 --capture 或 --import"
        si = _signer.peek()
        out["signer"] = {"started": bool(si.get("started") or si.get("wasmReady")),
                         "node": si.get("node"), "cosyVersion": si.get("cosyVersion"),
                         "bundle": bool(si.get("bundle")), "error": si.get("error")}
        out["models"] = len(_models["list"])""",
    """        if not accs:
            out["error"] = "账号池为空，请先 --capture 或 --import"
        out["models"] = len(_models["list"])""",
    "_handle_health remove signer"
)

# ── 11. _handle_chat: remove SignerError handling ──
replace(
    """        if not _models["list"]:
            try:
                _fetch_models()
            except SignerError as e:
                return self._err(502, "签名器异常: %s" % e)
            except Exception as e:
                _log("模型清单不可用，模型名原样透传: %s" % e)""",
    """        if not _models["list"]:
            try:
                _fetch_models()
            except Exception as e:
                _log("模型清单不可用，模型名原样透传: %s" % e)""",
    "_handle_chat remove SignerError"
)

# ── 12. _cmd_check: remove _signer.info() ──
replace(
    """    info = _signer.info()
    print("签名器   node=%s cosy=%s wasm=%s bundle=%s%s"
          % (info.get("node"), info.get("cosyVersion"), info.get("wasmReady"),
             bool(info.get("bundle")),
             "" if not info.get("error") else " 错误: %s" % info.get("error")))
    if not isinstance(info.get("error"), str) and info.get("wasmReady"):
        try:
            cached = _fetch_models()
            print("模型     %d 个（清单可用）" % len(cached["list"]))
        except Exception as e:
            print("模型     清单拉取失败: %s" % e)""",
    """    try:
        cached = _fetch_models()
        print("模型     %d 个（清单可用）" % len(cached["list"]))
    except Exception as e:
        print("模型     清单不可用: %s" % e)""",
    "_cmd_check remove signer"
)

# ── 13. _serve: update display strings ──
replace(
    '    print("  上游        %s%s" % (GATEWAY, INFER_PATH))\n'
    '    print("  签名        qoder_sign.mjs（node 子进程，不需要开客户端）")',
    '    print("  上游        %s%s" % (GATEWAY, CHAT_PATH))\n'
    '    print("  鉴权        Bearer token（OpenAI 兼容通道，无签名）")',
    "_serve display strings"
)

# ── 14. _capture_current: relax machine_id check ──
replace(
    '    if not acct.get("machineId"):\n'
    '        raise RuntimeError("找不到 machine_id（%s），签名需要它" % MACHINE_ID_FILE)',
    '    if not acct.get("machineId"):\n'
    '        _log("提示：未找到 machine_id（%s），续期可能需要它" % MACHINE_ID_FILE)',
    "_capture_current relax machine_id"
)

# ── 15. _cmd_import: update message ──
replace(
    '    print("目标机不需要 Qoder CN 客户端，但签名器仍需要 Node；"\n'
    '          "客户端的 worker runtime 文件可用 QODER_WORKER_BUNDLE 指定")',
    '    print("目标机不需要 Qoder CN 客户端，聊天通道不需要 Node；"\n'
    '          "--login 设备码登录仍需要 Node + qoder_sign.mjs")',
    "_cmd_import message"
)

# ── 16. _enrich_account: update comment ──
replace(
    '    """补 uid / 组织信息（签名要用）；只存 id，不存邮箱姓名等 PII。"""',
    '    """补 uid / 组织信息；只存 id，不存邮箱姓名等 PII。"""',
    "_enrich_account comment"
)

# ── 17. _machine_identity: update comment ──
replace(
    '    """签名要用 machine_id；续期要用 umid 里的 machine_token/machineType/machineCode。"""',
    '    """续期要用 machine_id 和 umid 里的 machine_token/machineType/machineCode。"""',
    "_machine_identity comment"
)

# Write the modified file
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

print(f"\nDone: {len(replacements)} replacements applied")
print(f"File size: {original_len} -> {len(src)} chars ({len(src) - original_len:+d})")
for desc, count in replacements:
    print(f"  ✓ {desc} ({count} match{'es' if count > 1 else ''})")