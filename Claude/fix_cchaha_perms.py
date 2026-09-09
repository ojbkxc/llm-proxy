#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_cchaha_perms.py — 一键修复 cc-haha 会话权限模式

问题：cc-haha 的会话权限模式存在 sqlite 里（不是 json），全局 settings.json 的
      defaultMode=bypassPermissions 不会自动传给 GUI 弹框层，导致每条 Bash
      命令都弹确认框。

本脚本干两件事：
  1. 把 ~/.claude/cc-haha/db/index-v1.sqlite 里 sessions 表所有 permission_mode
     为 NULL 或非 bypassPermissions 的会话，统一改成 'bypassPermissions'。
  2. 在 ~/.claude.json 的 projects[<cwd>] 下写入
     {permissionMode, skipAutoPermissionPrompt, autoApprove}，让该目录以后
     新开的会话也默认跳过权限。

用法（任选一种）：
  python fix_cchaha_perms.py                 # 修当前目录
  python fix_cchaha_perms.py C:\\GitHub      # 修指定目录
  python fix_cchaha_perms.py --dry-run       # 只看不改

退出码：0 成功 / 1 找不到文件 / 2 sqlite 出错 / 3 json 出错

作者：Claude（给用户多机部署用）  2026-09-05
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TARGET_MODE = "bypassPermissions"
DRY_RUN = "--dry-run" in sys.argv

# ---------- 路径定位 ----------
home = Path.home()
claude_dir = home / ".claude"
db_path = claude_dir / "cc-haha" / "db" / "index-v1.sqlite"
claude_json_path = home / ".claude.json"

# 命令行参数指定项目目录，默认当前目录
args = [a for a in sys.argv[1:] if a != "--dry-run"]
project_dir = os.path.abspath(args[0]) if args else os.getcwd()


def log(msg):
    print(f"[fix] {msg}")


def fix_sqlite():
    """把所有会话 permission_mode 改成 bypassPermissions"""
    if not db_path.exists():
        log(f"找不到 sqlite: {db_path}")
        return 1
    # cc-haha 用 WAL 模式，进程可能正开着；timeout 兼容并发
    try:
        con = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
        cur = con.cursor()

        # 看现状
        cur.execute("SELECT permission_mode, COUNT(*) FROM sessions GROUP BY permission_mode")
        before = cur.fetchall()
        log(f"修改前分布: {before}")

        if DRY_RUN:
            cur.execute(
                "SELECT COUNT(*) FROM sessions WHERE permission_mode IS NULL OR permission_mode != ?",
                (TARGET_MODE,),
            )
            n = cur.fetchone()[0]
            log(f"[dry-run] 将更新 {n} 条会话到 {TARGET_MODE}")
            con.close()
            return 0

        # 先 checkpoint WAL，避免改完读不到
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

        cur.execute(
            "UPDATE sessions SET permission_mode = ? "
            "WHERE permission_mode IS NULL OR permission_mode != ?",
            (TARGET_MODE, TARGET_MODE),
        )
        affected = cur.rowcount
        log(f"已更新 {affected} 条会话 → {TARGET_MODE}")

        cur.execute("SELECT permission_mode, COUNT(*) FROM sessions GROUP BY permission_mode")
        after = cur.fetchall()
        log(f"修改后分布: {after}")
        con.close()
        return 0
    except sqlite3.Error as e:
        log(f"sqlite 出错: {e}")
        log("提示：cc-haha 正在运行时可能锁库，先关掉 cc-haha GUI 再跑本脚本")
        return 2


def fix_claude_json():
    """在 ~/.claude.json 的 projects[<dir>] 写入跳过权限字段"""
    if not claude_json_path.exists():
        log(f"找不到 {claude_json_path}，跳过 json 修复")
        return 0
    try:
        with open(claude_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"读 .claude.json 出错: {e}")
        return 3

    data.setdefault("projects", {})
    proj = data["projects"].get(project_dir, {})
    old = proj.get("permissionMode")
    proj["permissionMode"] = TARGET_MODE
    proj["skipAutoPermissionPrompt"] = True
    proj["autoApprove"] = True
    data["projects"][project_dir] = proj

    if DRY_RUN:
        log(f"[dry-run] 将在 projects[{project_dir}] 写入 {proj}")
        return 0

    try:
        with open(claude_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log(f"已写 .claude.json projects[{project_dir}] (原 permissionMode={old})")
        return 0
    except OSError as e:
        log(f"写 .claude.json 出错: {e}")
        return 3


def main():
    print(f"=== cc-haha 权限修复 ===")
    print(f"项目目录 : {project_dir}")
    print(f"sqlite   : {db_path}")
    print(f".claude  : {claude_json_path}")
    print(f"目标模式 : {TARGET_MODE}{' (dry-run)' if DRY_RUN else ''}")
    print()

    rc1 = fix_sqlite()
    rc2 = fix_claude_json()
    rc = rc1 or rc2

    print()
    if rc == 0:
        log("完成。重启 cc-haha / 新开会话后不再弹权限确认。")
    else:
        log(f"完成但有警告（退出码 {rc}）。")
    return rc


if __name__ == "__main__":
    sys.exit(main())