#!/usr/bin/env python3
"""
panel.py — 多模型协作图形面板（Tkinter）

给 multi-model.py 套一个图形界面，不敲命令，鼠标点一点就能用。
直接复用 multi-model.py 的 ask/parallel/orchestrate/team 能力。

用法:
    python panel.py
"""
import os
import sys
import threading
import queue
import importlib.util

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

# 文件名 multi-model.py 带连字符，无法直接 import（会被当成减号），
# 这里按文件路径用 importlib 动态加载为模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("multi_model", os.path.join(_HERE, "multi-model.py"))
mm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mm)


class App:
    def __init__(self, root):
        self.root = root
        root.title("多模型协作编排器")
        root.geometry("920x680")
        self.q = queue.Queue()

        # 顶部：模型/命令选择
        top = ttk.LabelFrame(root, text="操作", padding=8)
        top.pack(fill="x", padx=10, pady=6)

        ttk.Label(top, text="命令:").grid(row=0, column=0, sticky="w")
        self.cmd_var = tk.StringVar(value="team")
        cmds = [
            ("真团队流水线（写代码+审查迭代）★推荐", "team"),
            ("指挥官拆解→并行→汇总", "orchestrate"),
            ("多模型同问对比", "parallel"),
            ("单模型问答", "ask"),
            ("列出可用模型", "list"),
        ]
        r = 0
        for label, val in cmds:
            ttk.Radiobutton(top, text=label, variable=self.cmd_var, value=val).grid(
                row=r, column=1, sticky="w", padx=6)
            r += 1

        # 工作目录（默认 D:\GitHub\AIGX，可改）
        ttk.Label(top, text="工作目录:").grid(row=0, column=2, sticky="e", padx=(20, 4))
        _default_dir = r"D:\GitHub\AIGX" if os.path.isdir(r"D:\GitHub\AIGX") else os.getcwd()
        self.dir_var = tk.StringVar(value=_default_dir)
        ttk.Entry(top, textvariable=self.dir_var, width=38).grid(row=0, column=3, sticky="we")
        ttk.Button(top, text="浏览…", command=self.pick_dir).grid(row=0, column=4, padx=4)

        # 输入框
        ttk.Label(top, text="任务/问题:").grid(row=1, column=0, sticky="w")
        self.prompt = scrolledtext.ScrolledText(top, height=4, wrap="word")
        self.prompt.grid(row=2, column=0, columnspan=5, sticky="we", pady=4)
        top.columnconfigure(3, weight=1)

        # 执行按钮
        btnrow = ttk.Frame(root)
        btnrow.pack(fill="x", padx=10, pady=4)
        self.run_btn = ttk.Button(btnrow, text="▶ 执行", command=self.run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btnrow, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btnrow, text="清空输出", command=self.clear).pack(side="left", padx=6)

        # 输出区
        out = ttk.LabelFrame(root, text="输出", padding=6)
        out.pack(fill="both", expand=True, padx=10, pady=6)
        self.out = scrolledtext.ScrolledText(out, wrap="word", state="disabled",
                                             font=("Consolas", 10))
        self.out.pack(fill="both", expand=True)

        self.running = False
        self._old_stdout = sys.stdout
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll()

    def pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_var.set(d)

    def clear(self):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")

    def write(self, s):
        self.out.configure(state="normal")
        self.out.insert("end", s)
        self.out.see("end")
        self.out.configure(state="disabled")

    def flush(self):
        pass

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("确认", "任务正在运行，确定退出？"):
                return
        self.stop()
        sys.stdout = self._old_stdout
        self.root.destroy()

    def stop(self):
        self.running = False

    def run(self):
        if self.running:
            return
        cmd = self.cmd_var.get()
        text = self.prompt.get("1.0", "end").strip()
        if cmd in ("team", "orchestrate", "parallel", "ask") and not text:
            messagebox.showwarning("提示", "请先输入任务或问题")
            return
        self.running = True
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._work, args=(cmd, text), daemon=True).start()

    def _work(self, cmd, text):
        # 重定向 stdout 到面板
        sys.stdout = self

        def emit(s):
            self.q.put(s)

        try:
            mm.WORKDIR = os.path.abspath(self.dir_var.get() or os.getcwd())
            if cmd == "team":
                emit("▶ 真团队流水线开始…\n")
                mm.team(text)
            elif cmd == "orchestrate":
                emit("▶ 指挥官编排开始…\n")
                mm.orchestrate(text)
            elif cmd == "parallel":
                emit("▶ 多模型并行对比…\n")
                for m, c in mm.parallel(text).items():
                    emit(f"\n===== {m} =====\n{c}\n")
            elif cmd == "ask":
                # 用 gpt-* 假名主力（gpt-5.6-luna → kimi-k2.7-code）
                emit("▶ 单模型问答 (gpt-5.6-luna)…\n")
                emit(mm.ask("gpt-5.6-luna", text) + "\n")
            elif cmd == "list":
                req = __import__("urllib.request").request.Request(
                    mm.BASE_URL + "/models",
                    headers={"Authorization": "Bearer " + mm.api_key(), "User-Agent": mm.UA},
                )
                import urllib.request, json
                resp = urllib.request.urlopen(req, timeout=30, context=mm._ssl_ctx())
                data = json.loads(resp.read().decode())
                for m in data["data"]:
                    emit("  - " + m["id"] + "\n")
            emit("\n✔ 完成\n")
        except Exception as e:
            emit(f"\n✘ 出错: {e}\n")
        finally:
            sys.stdout = self._old_stdout
            self.running = False
            self.q.put(("__DONE__",))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                else:
                    self.write(item)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()