# -*- coding: utf-8 -*-
"""把我们的账号池（国内3+国际5）转换成 qoder2api-hub Account JSON 格式，
直接写到 qoder2api-hub/accounts/ 目录。"""
import json, os, re, time

def convert(in_file, realm, domain):
    with open(in_file, encoding="utf-8") as f:
        data = json.load(f)
    accs = data.get("accounts", data) if isinstance(data, dict) else data
    out = []
    for a in accs:
        uid = a.get("uid", "")
        if not uid:
            continue
        rec = {
            "uid": uid,
            "nickname": a.get("displayName") or a.get("label") or uid[:8],
            "realm": realm,
            "domain": domain,
            "accessToken": a.get("token", ""),
            "refreshToken": a.get("refreshToken", ""),
            "expiresAt": a.get("exp") or a.get("expiresAt") or 0,
            "source": "import",
            "enabled": True,
            "addedAt": time.time(),
        }
        out.append(rec)
    return out

cn = convert("/opt/llm-proxy/qoder_accounts.json", "cn", "qoder.com.cn")
intl = convert("/opt/llm-proxy/qoder_intl_accounts.json", "intl", "qoder.com")
all_accs = cn + intl

outdir = "/opt/llm-proxy/qoder2api-hub/accounts"
os.makedirs(outdir, exist_ok=True)
for a in all_accs:
    safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", a["uid"]).strip("_ ")
    path = os.path.join(outdir, safe_uid + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=2)
    print("写入: %s (%s, %s)" % (a["nickname"][:14], a["realm"], a["uid"][:8]))
print("共导入 %d 个账号（国内 %d + 国际 %d）" % (len(all_accs), len(cn), len(intl)))