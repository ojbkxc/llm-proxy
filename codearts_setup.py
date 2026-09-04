# -*- coding: utf-8 -*-
"""
codearts_setup.py — 把 ws-proxy 注册进 CodeArts Agent 的自定义模型列表

原理：CodeArts Agent 内核（agent-kernel）启动时读取
      ~/.codeartsdoer/codearts-data/codearts.json 里的 provider.*，
      把每个 @ai-sdk/openai-compatible provider 注册为自定义模型，
      请求直接 POST {options.baseURL}/chat/completions（仅 Bearer 鉴权）。

本脚本做的事：
  1. 备份 codearts.json
  2. 写入/更新 provider "openai-wsproxy"：baseURL 指向本机 ws-proxy，
     模型列表 = proxy.py 的 MODEL_ALIAS 键（别名原样透传给 ws-proxy 解析）
  3. --list 查看 / --remove 还原 / --dry-run 预览（不写盘）

用法：
  python codearts_setup.py             # 写入（先关掉 CodeArts IDE）
  python codearts_setup.py --list
  python codearts_setup.py --dry-run
  python codearts_setup.py --remove

注意：apiKey 写明文占位 "ws-local"（ws-proxy 不校验 key），
      内核加载时会自动把它加密为 enc:v3: 回写。
"""
import argparse
import json
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".codeartsdoer", "codearts-data", "codearts.json")
PROVIDER_KEY = "openai-wsproxy"          # 内核自定义 provider 键（沿用 openai- 前缀习惯）
WS_PROXY_BASE = os.environ.get("WS_PROXY_BASE", "http://127.0.0.1:8787/v1")
API_KEY_PLACEHOLDER = "ws-local"         # ws-proxy 不校验，仅占位

# 模型上下文窗口（与既有自定义模型条目风格一致；0 = 由服务端决定）
CTX = {"contextWindow": 200000, "inputContextWindow": 184000, "outputContextWindow": 16000}


def load_alias() -> dict:
    """从 proxy.py 复用 MODEL_ALIAS（单一来源），失败则退回内置表。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import proxy  # noqa: 模块级只有常量初始化，无副作用
        return dict(proxy.MODEL_ALIAS)
    except Exception as e:
        print("[warn] 读取 proxy.py MODEL_ALIAS 失败(%s)，使用内置表" % e)
        return {
            "deepseek-v4-pro": "deepseek_v4",
            "gpt-5.6-luna": "gpt-5.6-luna",
            "qwen3.8-max": "qwen3.8-max",
            "glm-5.2": "aliyun-glm-5.2",
            "hw-glm-5": "hw-glm-5",
        }


def build_provider(alias: dict) -> dict:
    """构造 provider 条目（结构逐字段对齐内核 loadAndRegisterCustomModels 的期望）。"""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    options = {"apiKey": API_KEY_PLACEHOLDER, "baseURL": WS_PROXY_BASE}
    models = {}
    for alias_id in sorted(alias):
        options[alias_id] = {
            "sourceType": "custom",
            "providerType": PROVIDER_KEY,
            "provider": PROVIDER_KEY.removeprefix("openai-"),
            "modelId": alias_id,
            "modelName": alias_id,
            "modelType": "textConversation",
            "apiFormat": "openai",
            "displayEnabled": True,
            "isCustomModel": True,
            "maxTokens": 0,
            "truncateLength": 0,
            "inputLength": 0,
            "inputContextWindow": CTX["inputContextWindow"],
            "outputContextWindow": CTX["outputContextWindow"],
            "contextWindow": CTX["contextWindow"],
            "createdAt": now,
            "updatedAt": now,
        }
        models[alias_id] = {
            "id": alias_id,
            "limit": {"context": CTX["contextWindow"], "output": CTX["outputContextWindow"]},
        }
    return {"name": PROVIDER_KEY, "npm": "@ai-sdk/openai-compatible",
            "options": options, "models": models}


def read_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_config(cfg: dict):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    shutil.move(tmp, CONFIG_PATH)


def backup() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = CONFIG_PATH + ".bak-" + stamp
    shutil.copy2(CONFIG_PATH, dst)
    return dst


def cmd_list(cfg: dict):
    prov = cfg.get("provider") or {}
    print("配置文件: %s" % CONFIG_PATH)
    print("provider 共 %d 个:" % len(prov))
    for k in sorted(prov):
        v = prov[k]
        base = (v.get("options") or {}).get("baseURL", "")
        models = ", ".join(sorted((v.get("models") or {}).keys()))
        mark = "  <-- ws-proxy" if k == PROVIDER_KEY else ""
        print("  %-28s %s\n      models: %s%s" % (k, base, models or "-", mark))


def cmd_apply(cfg: dict, dry_run: bool) -> int:
    alias = load_alias()
    if not alias:
        print("[error] MODEL_ALIAS 为空，不写入")
        return 1
    prov = cfg.setdefault("provider", {})
    old = prov.get(PROVIDER_KEY)
    entry = build_provider(alias)
    if old == entry:
        print("已是最新，无需变更。")
        cmd_list(cfg)
        return 0
    if dry_run:
        print("[dry-run] 将写入 provider %r（baseURL=%s）：" % (PROVIDER_KEY, WS_PROXY_BASE))
        print("  模型: %s" % ", ".join(sorted(alias)))
        if old:
            print("  (覆盖已有同名 provider，旧 models: %s)" %
                  ", ".join(sorted((old.get("models") or {}).keys())))
        return 0
    print("备份: %s" % backup())
    prov[PROVIDER_KEY] = entry
    write_config(cfg)
    print("已写入 %d 个模型 → %s" % (len(alias), CONFIG_PATH))
    print("下一步: 重启 CodeArts IDE，模型选择里会出现上述模型（走 127.0.0.1:8787 ws-proxy）。")
    if os.path.exists(CONFIG_PATH.replace("codearts.json", "") ):
        pass
    return 0


def cmd_remove(cfg: dict, dry_run: bool) -> int:
    prov = cfg.get("provider") or {}
    if PROVIDER_KEY not in prov:
        print("未找到 %r，无需移除。" % PROVIDER_KEY)
        return 0
    if dry_run:
        print("[dry-run] 将移除 provider %r" % PROVIDER_KEY)
        return 0
    print("备份: %s" % backup())
    del prov[PROVIDER_KEY]
    write_config(cfg)
    print("已移除。重启 CodeArts IDE 生效。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把 ws-proxy 注册为 CodeArts Agent 自定义模型")
    ap.add_argument("--list", action="store_true", help="只查看当前 provider")
    ap.add_argument("--remove", action="store_true", help="移除本脚本写入的 provider")
    ap.add_argument("--dry-run", action="store_true", help="预览变更，不写盘")
    args = ap.parse_args()

    if not args.list and not os.path.exists(CONFIG_PATH):
        print("[error] 找不到 %s（未安装 CodeArts Agent？）" % CONFIG_PATH)
        return 1

    try:
        cfg = read_config()
    except Exception as e:
        print("[error] 解析 %s 失败: %s\n（文件可能含注释，请手工编辑）" % (CONFIG_PATH, e))
        return 1

    if args.list:
        cmd_list(cfg)
        return 0
    if args.remove:
        return cmd_remove(cfg, args.dry_run)

    if not args.dry_run:
        print("[hint] 建议先关闭 CodeArts IDE 再写入（避免内核回写覆盖）。")
    return cmd_apply(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
