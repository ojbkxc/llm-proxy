# -*- coding: utf-8 -*-
"""把合并版账号池 qoder_accounts.json 同步到 qoder2api-hub Account JSON 格式，
直接写到 qoder2api-hub/accounts/ 目录。合并后单池含 realm 字段（cn/intl）。"""
import json, os, re, time

REALM_DOMAIN = {"cn": "qoder.com.cn", "intl": "qoder.com"}

with open("/opt/llm-proxy/qoder_accounts.json", encoding="utf-8") as f:
    data = json.load(f)
accs = data.get("accounts", data) if isinstance(data, dict) else data

outdir = "/opt/llm-proxy/qoder2api-hub/accounts"
os.makedirs(outdir, exist_ok=True)

n_cn = n_intl = 0
for a in accs:
    uid = a.get("uid", "")
    if not uid:
        continue
    realm = a.get("realm") or ("intl" if "qoder.sh" in (a.get("domain") or "") or a.get("domain") == "qoder.com" else "cn")
    rec = {
        "uid": uid,
        "nickname": a.get("displayName") or a.get("label") or uid[:8],
        "realm": realm,
        "domain": REALM_DOMAIN.get(realm, "qoder.com.cn"),
        "accessToken": a.get("token", ""),
        "refreshToken": a.get("refreshToken", ""),
        "expiresAt": a.get("exp") or a.get("expiresAt") or 0,
        "source": "import",
        "enabled": True,
        "addedAt": time.time(),
    }
    safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", uid).strip("_ ")
    path = os.path.join(outdir, safe_uid + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print("写入: %s (%s, %s)" % (rec["nickname"][:14], realm, uid[:8]))
    if realm == "cn":
        n_cn += 1
    else:
        n_intl += 1

print("共导入 %d 个账号（国内 %d + 国际 %d）" % (n_cn + n_intl, n_cn, n_intl))