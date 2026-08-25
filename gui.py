"""
AI 网文写作系统 GUI
"""

import json
import os
import re
import shutil
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from pathlib import Path
from datetime import datetime

from project_store import ProjectStore, atomic_write_text, create_project, event_identifier, validate_project_name

# ==================== 常量 ====================

import sys

# 数据目录：exe 运行时用固定位置，脚本运行时用脚本目录
if getattr(sys, 'frozen', False):
    # PyInstaller exe：数据放在 exe 同目录下的 data 文件夹
    _DATA_DIR = Path(sys.executable).parent / "data"
else:
    # 脚本运行：放在脚本目录
    _DATA_DIR = Path(__file__).parent

ROOT_DIR = _DATA_DIR
# Older source versions and the user's existing workspace projects live one
# level above the application folder.  They remain visible without copying or
# rewriting their chapter files.
LEGACY_PROJECTS_DIR = ROOT_DIR.parent / "projects"
if not getattr(sys, "frozen", False) and LEGACY_PROJECTS_DIR.exists():
    PROJECTS_DIR = LEGACY_PROJECTS_DIR
else:
    PROJECTS_DIR = ROOT_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# 停止标志（全局，用于中断写作线程）
_stop_event = threading.Event()

C_BG       = "#F7F3EE"
C_CARD     = "#FFFFFF"
C_INPUT    = "#F0EDE8"
C_DARK     = "#2C2C2C"
C_TEXT     = "#1A1A1A"
C_TEXT2    = "#6B6B6B"
C_MUTED    = "#AAAAAA"
C_ACCENT   = "#C04030"
C_SUCCESS  = "#4A8C5C"
C_ERROR    = "#C04030"
C_BORDER   = "#E0DCD6"
C_DIVIDER  = "#E8E4DE"

FONT = "Microsoft YaHei UI"

PLACEHOLDER_IDEA = (
    "输入你的故事创意...\n\n"
    "例如：一个少年穿越到修仙世界，获得了一本能推演万物演化规律的古籍..."
)

# 常见 API 预设
API_PRESETS = [
    ("OpenAI",    "https://api.openai.com/v1/chat/completions",       "gpt-4o"),
    ("DeepSeek",  "https://api.deepseek.com/v1/chat/completions",    "deepseek-chat"),
    ("智谱 GLM",  "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash"),
    ("通义千问",  "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-turbo"),
    ("Kimi",      "https://api.moonshot.cn/v1/chat/completions",     "moonshot-v1-8k"),
    ("硅基流动",  "https://api.siliconflow.cn/v1/chat/completions",  "deepseek-ai/DeepSeek-V3"),
    ("自定义",    "", ""),
]


# ==================== 卡片（简洁 Frame） ====================

def make_card(parent, **pack_kw):
    """创建白色卡片 Frame"""
    f = tk.Frame(parent, bg=C_CARD, relief=tk.FLAT, bd=0, highlightthickness=1,
                 highlightbackground=C_BORDER, highlightcolor=C_BORDER)
    f.pack(**pack_kw)
    return f


# ==================== 项目管理 ====================

class ProjectManager:
    def __init__(self):
        self._d = PROJECTS_DIR; self._d.mkdir(parents=True, exist_ok=True); self._cur = None

    def _roots(self):
        roots = [self._d]
        try:
            if LEGACY_PROJECTS_DIR.resolve() != self._d.resolve() and LEGACY_PROJECTS_DIR.exists():
                roots.append(LEGACY_PROJECTS_DIR)
        except OSError:
            pass
        return roots

    def list(self):
        names = {}
        for root in self._roots():
            for directory in root.iterdir():
                if directory.is_dir() and not directory.name.startswith("."):
                    names.setdefault(directory.name, directory)
        return sorted(names)

    def dir(self, name=None):
        if name:
            for root in self._roots():
                candidate = root / name
                if candidate.is_dir():
                    return candidate
            return self._d / name
        return self._cur

    def create(self, name, premise=""):
        return create_project(self._d, name, premise=premise).project_dir

    def delete(self, name):
        d = self.dir(name)
        allowed = [root.resolve() for root in self._roots()]
        if d.exists() and any(d.resolve().parent == root for root in allowed):
            shutil.rmtree(d)

    def set(self, name):
        self._cur = self.dir(name)

    def exists(self, name):
        return self.dir(name).is_dir()


pm = ProjectManager()


# ==================== 设置对话框 ====================

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, project_dir=None):
        super().__init__(parent)
        self.title("设置"); self.geometry("540x540"); self.resizable(False, False)
        self.transient(parent); self.grab_set(); self.configure(bg=C_BG)
        self._ed = project_dir or ROOT_DIR
        self._load(); self._build(); self._fill()
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width()-540)//2
        y = parent.winfo_y() + (parent.winfo_height()-540)//2
        self.geometry(f"+{x}+{y}")

    def _load(self):
        f = self._ed / "config.json"
        self._cfg = json.loads(f.read_text("utf-8")) if f.exists() else {}

    def _build(self):
        tk.Label(self, text="API 配置", font=(FONT,14,"bold"), bg=C_BG, fg=C_TEXT,
                 anchor="w").pack(fill=tk.X, padx=20, pady=(14,6))

        # 快速选择
        r = tk.Frame(self, bg=C_BG); r.pack(fill=tk.X, padx=20)
        tk.Label(r, text="快速选择", font=(FONT,12), bg=C_BG, fg=C_TEXT2, width=8,
                 anchor="w").pack(side=tk.LEFT)
        self._pv = tk.StringVar(value="自定义")
        cb = ttk.Combobox(r, textvariable=self._pv, values=[n for n,_,_ in API_PRESETS],
                          state="readonly", width=16, font=(FONT,12))
        cb.pack(side=tk.LEFT, padx=8); cb.bind("<<ComboboxSelected>>", self._preset)

        # 字段
        self._vs = {}
        for lbl, key, ph in [("地址","url","api.openai.com"),
                              ("密钥","key","sk-..."),
                              ("模型","model","gpt-4o"),
                              ("超时(秒)","timeout","300")]:
            r = tk.Frame(self, bg=C_BG); r.pack(fill=tk.X, padx=20, pady=2)
            tk.Label(r, text=lbl, font=(FONT,12), bg=C_BG, fg=C_TEXT2, width=8,
                     anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar(); self._vs[key] = v
            kw = {"show":"•"} if key=="key" else {}
            tk.Entry(r, textvariable=v, font=(FONT,12), bg=C_INPUT, fg=C_TEXT,
                     relief=tk.FLAT, insertbackground=C_TEXT, **kw).pack(
                     side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(8,0))

        self._url_hint = tk.Label(self, text="输入域名即可自动补全，如 api.openai.com",
                                  font=(FONT,10), bg=C_BG, fg=C_MUTED, anchor="w")
        self._url_hint.pack(fill=tk.X, padx=20, pady=(0,6))

        # 测试
        r = tk.Frame(self, bg=C_BG); r.pack(fill=tk.X, padx=20, pady=(4,4))
        tk.Button(r, text="测试连接", font=(FONT,12), bg=C_TEXT, fg="#FFF",
                  relief=tk.FLAT, padx=12, pady=2, command=self._test).pack(side=tk.LEFT)
        self._tl = tk.Label(r, text="", font=(FONT,11), bg=C_BG, fg=C_TEXT2)
        self._tl.pack(side=tk.LEFT, padx=12)

        tk.Frame(self, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=20, pady=10)

        tk.Label(self, text="写作参数", font=(FONT,14,"bold"), bg=C_BG, fg=C_TEXT,
                 anchor="w").pack(fill=tk.X, padx=20, pady=(0,6))
        for lbl, key, d in [("目标字数","target_length","15000"),("最大轮次","max_rounds","3")]:
            r = tk.Frame(self, bg=C_BG); r.pack(fill=tk.X, padx=20, pady=2)
            tk.Label(r, text=lbl, font=(FONT,12), bg=C_BG, fg=C_TEXT2, width=8,
                     anchor="w").pack(side=tk.LEFT)
            self._vs[key] = tk.StringVar(value=d)
            tk.Entry(r, textvariable=self._vs[key], font=(FONT,12), bg=C_INPUT, fg=C_TEXT,
                     relief=tk.FLAT).pack(side=tk.LEFT, ipady=4, padx=(8,0))

        r = tk.Frame(self, bg=C_BG); r.pack(fill=tk.X, padx=20, pady=(16,0))
        tk.Button(r, text="取消", font=(FONT,12), bg=C_INPUT, fg=C_TEXT, relief=tk.FLAT,
                  padx=16, pady=4, command=self.destroy).pack(side=tk.RIGHT)
        tk.Button(r, text="保存", font=(FONT,12), bg=C_ACCENT, fg="#FFF", relief=tk.FLAT,
                  padx=16, pady=4, command=self._save).pack(side=tk.RIGHT, padx=(0,8))

    def _preset(self, e=None):
        for n, url, model in API_PRESETS:
            if n == self._pv.get():
                if url: self._vs["url"].set(url)
                if model: self._vs["model"].set(model)
                break

    def _norm(self, url):
        url = url.strip()
        if not url: return url
        if not url.startswith("http"): url = "https://" + url
        url = url.rstrip("/")
        if not url.endswith("/chat/completions"):
            tail = "/v1/chat/completions"
            if url.endswith("/v1"): url += "/chat/completions"
            elif "/v1/" not in url and "/chat/" not in url: url += tail
        return url

    def _fill(self):
        a = self._cfg.get("api",{})
        self._vs["url"].set(a.get("url",""))
        self._vs["key"].set(a.get("key",""))
        self._vs["model"].set(a.get("model",""))
        self._vs["timeout"].set(str(a.get("timeout",300)))
        w = self._cfg.get("writing",{})
        self._vs["target_length"].set(str(w.get("target_length",15000)))
        self._vs["max_rounds"].set(str(w.get("max_rounds",3)))

    def _test(self):
        import urllib.request
        raw = self._vs["url"].get().strip()
        key = self._vs["key"].get().strip()
        model = self._vs["model"].get().strip()
        if not raw: self._tl.config(text="请填地址", fg=C_ERROR); return
        if not key: self._tl.config(text="请填密钥", fg=C_ERROR); return
        url = self._norm(raw); self._vs["url"].set(url)
        self._tl.config(text="测试中...", fg=C_MUTED)
        self._url_hint.config(text=f"→ {url}", fg=C_TEXT2)
        def do():
            try:
                h = {"Content-Type":"application/json","Authorization":f"Bearer {key}"}
                d = json.dumps({"model":model,"messages":[{"role":"user","content":"hi"}],
                                "max_completion_tokens":5}).encode()
                req = urllib.request.Request(url, data=d, headers=h, method="POST")
                with urllib.request.urlopen(req, timeout=20) as r:
                    if "choices" in json.loads(r.read()):
                        self.after(0,lambda:self._tl.config(text="连接成功 ✓", fg=C_SUCCESS))
                    else:
                        self.after(0,lambda:self._tl.config(text="返回格式异常", fg=C_ERROR))
            except urllib.error.HTTPError as e:
                msgs = {401:"密钥无效",403:"无权限",404:"地址不存在",429:"请求频繁"}
                self.after(0,lambda:self._tl.config(text=msgs.get(e.code,f"HTTP {e.code}"),fg=C_ERROR))
            except Exception as e:
                s = str(e)
                if "getaddrinfo" in s: m = "域名无法解析"
                elif "refused" in s: m = "连接被拒绝"
                elif "timed out" in s: m = "连接超时"
                elif "SSL" in s: m = "SSL证书错误"
                else: m = s[:40]
                self.after(0,lambda:self._tl.config(text=m, fg=C_ERROR))
        threading.Thread(target=do, daemon=True).start()

    def _save(self):
        url = self._norm(self._vs["url"].get().strip())
        try: timeout = int(self._vs["timeout"].get())
        except ValueError: timeout = 300
        try: tlen = int(self._vs["target_length"].get())
        except ValueError: tlen = 15000
        try: rnds = int(self._vs["max_rounds"].get())
        except ValueError: rnds = 3
        cfg = {
            "api":{"url":url,"key":self._vs["key"].get().strip(),
                   "model":self._vs["model"].get().strip(),"timeout":timeout,"max_retries":3},
            "writing":{"target_length":tlen,"max_rounds":rnds,"max_events_per_volume":15},
            "review":{
                "pass_score":7.5,
                "dimensions":["爽感密度","设定自洽","节奏张力","人设一致","叙事衔接","追读引力"],
                "weights":{
                    "opening":{"爽感密度":2,"设定自洽":1,"节奏张力":2,"人设一致":1,"叙事衔接":2,"追读引力":2},
                    "rising":{"爽感密度":2,"设定自洽":1,"节奏张力":1,"人设一致":2,"叙事衔接":1,"追读引力":2},
                    "climax":{"爽感密度":3,"设定自洽":1,"节奏张力":2,"人设一致":2,"叙事衔接":1,"追读引力":1},
                    "daily":{"爽感密度":1,"设定自洽":2,"节奏张力":1,"人设一致":2,"叙事衔接":2,"追读引力":2}}},
            "output":{"show_progress":True,"save_raw":True,"auto_backup":True}
        }
        self._ed.mkdir(parents=True, exist_ok=True)
        from project_store import atomic_write_json
        atomic_write_json(self._ed/"config.json", cfg, backup=True)
        import agents; agents.CONFIG = cfg
        messagebox.showinfo("完成", "配置已保存", parent=self); self.destroy()


# ==================== 主窗口 ====================

class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AI 网文写作系统")
        self.root.geometry("1200x800"); self.root.minsize(960,640)
        self.root.configure(bg=C_BG)
        self._running = False; self._ph_active = True

        # 当前项目目录
        self._project_dir = ROOT_DIR

        self._build_menu(); self._build_body(); self._build_bar()
        from agents import setup_gui_callbacks
        setup_gui_callbacks(on_output=self._out, on_input=self._inp, on_confirm=self._conf)
        self.root.after(200, self._init)

    # ---- 菜单 ----

    def _build_menu(self):
        mb = tk.Menu(self.root); self.root.config(menu=mb)
        fm = tk.Menu(mb, tearoff=0); mb.add_cascade(label="文件", menu=fm)
        fm.add_command(label="打开项目目录", command=lambda: os.startfile(str(self._project_dir)))
        fm.add_command(label="打开项目根目录", command=lambda: os.startfile(str(PROJECTS_DIR)))
        fm.add_separator(); fm.add_command(label="退出", command=self.root.quit)
        sm = tk.Menu(mb, tearoff=0); mb.add_cascade(label="设置", menu=sm)
        sm.add_command(label="API 配置...", command=self._settings)
        hm = tk.Menu(mb, tearoff=0); mb.add_cascade(label="帮助", menu=hm)
        hm.add_command(label="关于", command=lambda: messagebox.showinfo("关于","AI 网文写作系统\n多Agent并行创作"))

    # ---- 主体 ----

    def _build_body(self):
        pw = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=C_BG,
                            sashwidth=4, sashrelief=tk.FLAT, opaqueresize=True)
        pw.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10,0))

        # ---- 左面板（可滚动） ----
        left_outer = tk.Frame(pw, bg=C_BG, width=380)
        pw.add(left_outer, stretch="never")

        # Canvas + 滚动条
        self._l_canvas = tk.Canvas(left_outer, bg=C_BG, highlightthickness=0, bd=0)
        l_scroll = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=self._l_canvas.yview)
        self._l_canvas.configure(yscrollcommand=l_scroll.set)
        l_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._l_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._l_inner = tk.Frame(self._l_canvas, bg=C_BG)
        self._l_win = self._l_canvas.create_window((0,0), window=self._l_inner, anchor="nw",
                                                    width=368)

        self._l_inner.bind("<Configure>", lambda e: self._l_canvas.configure(
            scrollregion=self._l_canvas.bbox("all")))
        self._l_canvas.bind("<Configure>", lambda e: self._l_canvas.itemconfig(
            self._l_win, width=e.width))

        # 鼠标滚轮滚动
        def _mousewheel(e):
            self._l_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        self._l_canvas.bind("<MouseWheel>", _mousewheel)

        self._build_left(self._l_inner)

        # ---- 右面板 ----
        right = tk.Frame(pw, bg=C_BG)
        pw.add(right, stretch="always")
        self._build_right(right)

    def _build_left(self, parent):
        sp = {"padx":4, "pady":(0, 6), "fill":tk.X}

        card = make_card(parent, **sp)
        self._proj_card(card)

        # 故事创意 - 固定高度，不扩展
        card = make_card(parent, **sp)
        self._idea_card(card)

        card = make_card(parent, **sp)
        self._action_card(card)

        card = make_card(parent, **sp)
        self._files_card(card)

    def _proj_card(self, p):
        tk.Label(p, text="项目", font=(FONT,13,"bold"), bg=C_CARD, fg=C_TEXT).pack(fill=tk.X, padx=12, pady=(10,4))
        r = tk.Frame(p, bg=C_CARD); r.pack(fill=tk.X, padx=12, pady=(0,10))
        self._pv = tk.StringVar()
        self._p_combo = ttk.Combobox(r, textvariable=self._pv, state="readonly", width=14, font=(FONT,12))
        self._p_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._p_combo.bind("<<ComboboxSelected>>", self._on_proj)
        for t,c in [("新建",self._new_proj),("删除",self._del_proj),("导入",self._imp_proj)]:
            tk.Button(r, text=t, font=(FONT,11), bg=C_INPUT, fg=C_TEXT, relief=tk.FLAT,
                      padx=5, pady=2, command=c).pack(side=tk.LEFT, padx=(3,0))

    def _idea_card(self, p):
        top = tk.Frame(p, bg=C_CARD); top.pack(fill=tk.X, padx=12, pady=(10,2))
        tk.Label(top, text="故事创意", font=(FONT,13,"bold"), bg=C_CARD, fg=C_TEXT).pack(side=tk.LEFT)
        tk.Button(top, text="清空", font=(FONT,10), bg=C_INPUT, fg=C_TEXT2, relief=tk.FLAT,
                  padx=4, command=self._clear_idea).pack(side=tk.RIGHT)

        # 偏好选择
        pref = tk.Frame(p, bg=C_CARD); pref.pack(fill=tk.X, padx=12, pady=(2,4))
        tk.Label(pref, text="偏好", font=(FONT,11), bg=C_CARD, fg=C_TEXT2).pack(side=tk.LEFT)
        self._pref_len = tk.StringVar(value="长篇")
        ttk.Combobox(pref, textvariable=self._pref_len, values=["长篇","短篇"], state="readonly",
                     width=5, font=(FONT,11)).pack(side=tk.LEFT, padx=(4,8))
        self._pref_style = tk.StringVar(value="轻松")
        ttk.Combobox(pref, textvariable=self._pref_style, values=["轻松","复杂"], state="readonly",
                     width=5, font=(FONT,11)).pack(side=tk.LEFT)

        # 文本框 - 固定高度
        f = tk.Frame(p, bg=C_CARD); f.pack(fill=tk.X, padx=12, pady=(0,8))
        self._idea = tk.Text(f, wrap=tk.WORD, font=(FONT,13), bg=C_INPUT, fg=C_TEXT,
                             relief=tk.FLAT, insertbackground=C_TEXT, padx=10, pady=6, height=4)
        s = ttk.Scrollbar(f, orient=tk.VERTICAL, command=self._idea.yview)
        self._idea.configure(yscrollcommand=s.set)
        self._idea.pack(side=tk.LEFT, fill=tk.X, expand=True); s.pack(side=tk.RIGHT, fill=tk.Y)
        self._setup_ph()

    def _action_card(self, p):
        tk.Label(p, text="操作", font=(FONT,13,"bold"), bg=C_CARD, fg=C_TEXT).pack(
            fill=tk.X, padx=12, pady=(10,2))

        # 生成框架按钮
        tk.Button(p, text="① 生成故事框架", font=(FONT,13), bg=C_ACCENT, fg="#FFF",
                  relief=tk.FLAT, padx=10, pady=4, command=self._gen_fw).pack(
                  fill=tk.X, padx=12, pady=(0,6))

        # 事件选择器
        er = tk.Frame(p, bg=C_CARD); er.pack(fill=tk.X, padx=12, pady=(0,4))
        self._ev = tk.StringVar()
        self._ec = ttk.Combobox(er, textvariable=self._ev, state="readonly", font=(FONT,12))
        self._ec.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._ec.bind("<<ComboboxSelected>>", self._on_evt)
        tk.Button(er, text="刷新", font=(FONT,10), bg=C_INPUT, fg=C_TEXT, relief=tk.FLAT,
                  padx=4, command=self._load_evts).pack(side=tk.LEFT, padx=(4,0))

        # 事件描述
        self._ed_lbl = tk.Label(p, text="选择事件后显示描述", font=(FONT,11), bg=C_CARD,
                                fg=C_MUTED, anchor="w", wraplength=340, justify="left")
        self._ed_lbl.pack(fill=tk.X, padx=12, pady=(0,6))

        # 写作按钮行
        br = tk.Frame(p, bg=C_CARD); br.pack(fill=tk.X, padx=12, pady=(0,4))
        tk.Button(br, text="写当前", font=(FONT,12), bg=C_TEXT, fg="#FFF",
                  relief=tk.FLAT, padx=8, pady=4, command=self._write_sel).pack(
                  side=tk.LEFT, fill=tk.X, expand=True, padx=(0,2))
        tk.Button(br, text="写整卷", font=(FONT,12), bg=C_TEXT, fg="#FFF",
                  relief=tk.FLAT, padx=8, pady=4, command=self._write_vol).pack(
                  side=tk.LEFT, fill=tk.X, expand=True, padx=(2,2))
        tk.Button(br, text="续写", font=(FONT,12), bg=C_ACCENT, fg="#FFF",
                  relief=tk.FLAT, padx=8, pady=4, command=self._continue_write).pack(
                  side=tk.LEFT, fill=tk.X, expand=True, padx=(2,0))

        tk.Button(p, text="恢复上次断点", font=(FONT,11), bg=C_INPUT, fg=C_TEXT,
                  relief=tk.FLAT, padx=8, pady=3, command=self._resume_checkpoint).pack(
                  fill=tk.X, padx=12, pady=(0,6))

        # 停止按钮
        self._btn_stop = tk.Button(p, text="停止", font=(FONT,11), bg=C_INPUT, fg=C_TEXT,
                                    relief=tk.FLAT, padx=8, pady=2,
                                    command=self._stop, state=tk.DISABLED)
        self._btn_stop.pack(fill=tk.X, padx=12, pady=(0,8))

    def _files_card(self, p):
        tk.Label(p, text="项目文件", font=(FONT,13,"bold"), bg=C_CARD, fg=C_TEXT).pack(fill=tk.X, padx=12, pady=(10,2))
        self._fl = tk.Listbox(p, height=3, font=(FONT,11), bg=C_CARD, fg=C_TEXT, relief=tk.FLAT,
                               selectbackground=C_ACCENT, selectforeground="#FFF", highlightthickness=0)
        self._fl.pack(fill=tk.X, padx=12, pady=(0,8)); self._fl.bind("<Double-1>", self._on_fclick)

    # ---- 右侧 ----

    def _build_right(self, p):
        bar = tk.Frame(p, bg=C_BG); bar.pack(fill=tk.X)
        self._tbs = {}; self._tfs = {}; self._active = None
        for k,t in [("fw","故事框架"),("ch","章节内容"),("log","运行日志")]:
            b = tk.Label(bar, text=t, font=(FONT,12), bg=C_BG, fg=C_TEXT2, padx=14, pady=6, cursor="hand2")
            b.pack(side=tk.LEFT); b.bind("<Button-1>",lambda e,k=k:self._tab(k))
            self._tbs[k] = b
        tk.Frame(p, bg=C_DIVIDER, height=2).pack(fill=tk.X)
        c = tk.Frame(p, bg=C_BG); c.pack(fill=tk.BOTH, expand=True)
        self._tfs["fw"] = self._tab_fw(c)
        self._tfs["ch"] = self._tab_ch(c)
        self._tfs["log"] = self._tab_log(c)
        self._tab("fw")

    def _tab(self, k):
        for kk, b in self._tbs.items():
            b.config(fg=C_ACCENT,font=(FONT,12,"bold")) if kk==k else b.config(fg=C_TEXT2,font=(FONT,12))
        for kk, f in self._tfs.items():
            f.pack(fill=tk.BOTH, expand=True) if kk==k else f.pack_forget()
        self._active = k

    def _editor(self, p):
        w = tk.Frame(p, bg=C_CARD)
        t = tk.Text(w, wrap=tk.WORD, font=(FONT,13), bg=C_CARD, fg=C_TEXT, relief=tk.FLAT,
                    insertbackground=C_TEXT, padx=10, pady=6)
        s = ttk.Scrollbar(w, orient=tk.VERTICAL, command=t.yview)
        t.configure(yscrollcommand=s.set); t.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        s.pack(side=tk.RIGHT, fill=tk.Y)
        return t

    def _tab_fw(self, p):
        f = tk.Frame(p, bg=C_BG)
        sb = tk.Frame(f, bg=C_BG); sb.pack(fill=tk.X, padx=8, pady=(8,0))
        self._fbs = {}; self._ffs = {}
        for k,t in [("plan","大纲"),("chars","角色"),("evts","事件")]:
            b = tk.Label(sb, text=t, font=(FONT,11), bg=C_BG, fg=C_MUTED, padx=10, pady=3, cursor="hand2")
            b.pack(side=tk.LEFT); b.bind("<Button-1>",lambda e,k=k:self._subfw(k))
            self._fbs[k] = b
        tk.Frame(f, bg=C_DIVIDER, height=1).pack(fill=tk.X, padx=8, pady=(2,0))
        tlb = tk.Frame(f, bg=C_BG); tlb.pack(fill=tk.X, padx=8, pady=4)
        tk.Button(tlb, text="加载文件", font=(FONT,10), bg=C_TEXT, fg="#FFF", relief=tk.FLAT,
                  padx=6, pady=1, command=self._load_fw).pack(side=tk.LEFT)
        tk.Button(tlb, text="保存修改", font=(FONT,10), bg=C_ACCENT, fg="#FFF", relief=tk.FLAT,
                  padx=6, pady=1, command=self._save_fw).pack(side=tk.LEFT, padx=4)

        card = make_card(f, fill=tk.BOTH, expand=True, padx=8, pady=(0,8))
        self._pt = self._editor(card); self._ct = self._editor(card)
        self._evt_t = self._editor(card)
        self._ffs["plan"] = self._pt.master; self._ffs["chars"] = self._ct.master
        self._ffs["evts"] = self._evt_t.master
        self._subfw("plan")
        return f

    def _subfw(self, k):
        for kk,b in self._fbs.items():
            b.config(fg=C_TEXT,font=(FONT,11,"bold")) if kk==k else b.config(fg=C_MUTED,font=(FONT,11))
        for kk,f in self._ffs.items():
            f.pack(fill=tk.BOTH, expand=True) if kk==k else f.pack_forget()

    def _tab_ch(self, p):
        f = tk.Frame(p, bg=C_BG)
        tb = tk.Frame(f, bg=C_BG); tb.pack(fill=tk.X, padx=8, pady=8)
        tk.Label(tb, text="章节", font=(FONT,12), bg=C_BG, fg=C_TEXT).pack(side=tk.LEFT)
        self._chv = tk.StringVar()
        self._chc = ttk.Combobox(tb, textvariable=self._chv, state="readonly", width=35, font=(FONT,12))
        self._chc.pack(side=tk.LEFT, padx=8); self._chc.bind("<<ComboboxSelected>>", self._on_ch)
        tk.Button(tb, text="刷新", font=(FONT,10), bg=C_TEXT, fg="#FFF", relief=tk.FLAT,
                  padx=6, command=self._rf_ch).pack(side=tk.LEFT, padx=8)
        card = make_card(f, fill=tk.BOTH, expand=True, padx=8, pady=(0,8))
        self._ch_t = self._editor(card); self._ch_t.master.pack(fill=tk.BOTH, expand=True)
        return f

    def _tab_log(self, p):
        f = tk.Frame(p, bg=C_BG)
        tb = tk.Frame(f, bg=C_BG); tb.pack(fill=tk.X, padx=8, pady=(8,0))
        tk.Button(tb, text="清空日志", font=(FONT,10), bg=C_INPUT, fg=C_TEXT2, relief=tk.FLAT,
                  padx=4, command=self._clr_log).pack(side=tk.LEFT)
        card = make_card(f, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._lg = tk.Text(card, wrap=tk.WORD, font=(FONT,11), bg=C_DARK, fg="#D4D4D4",
                           relief=tk.FLAT, insertbackground="#D4D4D4", state=tk.DISABLED, padx=10, pady=6)
        s = ttk.Scrollbar(card, orient=tk.VERTICAL, command=self._lg.yview)
        self._lg.configure(yscrollcommand=s.set)
        self._lg.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); s.pack(side=tk.RIGHT, fill=tk.Y)
        self._lg.tag_configure("info", foreground="#D4D4D4")
        self._lg.tag_configure("success", foreground="#6A9955")
        self._lg.tag_configure("warning", foreground="#DCDCAA")
        self._lg.tag_configure("error", foreground="#F44747")
        self._lg.tag_configure("header", foreground="#569CD6", font=(FONT,11,"bold"))
        return f

    # ---- 状态栏 ----

    def _build_bar(self):
        b = tk.Frame(self.root, bg=C_DIVIDER, height=26)
        b.pack(fill=tk.X, side=tk.BOTTOM)
        self._sl = tk.Label(b, text="就绪", font=(FONT,10), bg=C_DIVIDER, fg=C_TEXT2)
        self._sl.pack(side=tk.LEFT, padx=12)
        self._pl = tk.Label(b, text="", font=(FONT,10), bg=C_DIVIDER, fg=C_MUTED)
        self._pl.pack(side=tk.LEFT, padx=20)
        self._wl = tk.Label(b, text="", font=(FONT,10), bg=C_DIVIDER, fg=C_MUTED)
        self._wl.pack(side=tk.RIGHT, padx=12)

    # ==================== 占位符 ====================

    def _setup_ph(self):
        self._ph_active = True; self._idea.insert("1.0", PLACEHOLDER_IDEA)
        self._idea.config(fg=C_MUTED)
        self._idea.bind("<FocusIn>", lambda e: self._idea.mark_set(tk.INSERT,"1.0") if self._ph_active else None)
        self._idea.bind("<FocusOut>", lambda e: self._ph_restore() if not self._idea.get("1.0",tk.END).strip() else None)
        self._idea.bind("<Key>", self._ph_key)

    def _ph_restore(self):
        self._ph_active = True; self._idea.delete("1.0",tk.END)
        self._idea.insert("1.0", PLACEHOLDER_IDEA); self._idea.config(fg=C_MUTED)

    def _ph_key(self, e=None):
        if self._ph_active:
            self._ph_active = False; self._idea.delete("1.0",tk.END); self._idea.config(fg=C_TEXT)

    def _clear_idea(self):
        self._idea.delete("1.0",tk.END); self._idea.config(fg=C_TEXT)
        self._ph_active = False; self._idea.focus_set()

    # ==================== 项目 ====================

    def _init(self):
        self._rf_proj(); projects = pm.list()
        if not projects:
            # 首次使用 - 显示欢迎对话框
            self._show_welcome()
            pm.create("默认项目"); self._rf_proj(); projects = pm.list()
        # 恢复上次使用的项目
        last = self._load_last_project()
        if last and last in projects:
            self._p_combo.set(last); self._sw_proj(last)
        else:
            self._p_combo.set(projects[0]); self._sw_proj(projects[0])
        from agents import is_api_configured
        if not is_api_configured():
            if messagebox.askyesno("配置 API","尚未配置 API，是否现在配置？\n\nAPI 配置保存在项目目录的 config.json 中。"):
                self._settings()
        checkpoint = ProjectStore(self._project_dir).load_checkpoint()
        if checkpoint:
            self._log(f"[记] 检测到未完成断点：{checkpoint.get('event_name', '写作任务')}，可点击“恢复上次断点”继续")

    def _show_welcome(self):
        """首次使用的欢迎对话框"""
        welcome = tk.Toplevel(self.root)
        welcome.title("欢迎使用")
        welcome.geometry("420x300")
        welcome.resizable(False, False)
        welcome.transient(self.root)
        welcome.grab_set()
        welcome.configure(bg=C_BG)

        tk.Label(welcome, text="📖 AI 网文写作系统", font=(FONT,16,"bold"),
                 bg=C_BG, fg=C_TEXT).pack(pady=(20,10))

        steps = [
            "1. 配置 API（支持 OpenAI、DeepSeek 等）",
            "2. 输入故事创意，点击「生成故事框架」",
            "3. 选择事件，开始写作",
            "4. 支持断点续写，随时可中断",
        ]
        for s in steps:
            tk.Label(welcome, text=s, font=(FONT,12), bg=C_BG, fg=C_TEXT2,
                     anchor="w").pack(fill=tk.X, padx=30, pady=2)

        tk.Button(welcome, text="开始使用", font=(FONT,13), bg=C_ACCENT, fg="#FFF",
                  relief=tk.FLAT, padx=20, pady=6, command=welcome.destroy).pack(pady=20)

        self.root.wait_window(welcome)

    def _load_last_project(self):
        """读取上次使用的项目名"""
        f = ROOT_DIR / ".last_project"
        if f.exists():
            try: return f.read_text("utf-8").strip()
            except: return None
        return None

    def _save_last_project(self, name):
        """保存当前项目名"""
        f = ROOT_DIR / ".last_project"
        try: atomic_write_text(f, name)
        except: pass

    def _rf_proj(self): self._p_combo["values"] = pm.list()

    def _on_proj(self, e=None):
        if self._pv.get():
            if self._running:
                messagebox.showwarning("提示", "当前有写作任务正在运行，请先停止任务。", parent=self.root)
                return
            self._sw_proj(self._pv.get())

    def _sw_proj(self, name):
        pm.set(name); d = pm.dir()
        self._project_dir = d
        self._save_last_project(name)  # 记住当前项目
        import agents
        agents.PROJECT_DIR = d; agents.CHAPTERS_DIR = d/"chapters"; agents.RAW_DIR = d/"raw"
        agents.LOG_DIR = d/"logs"; agents.NOTES_FILE = d/"writing_notes.json"
        agents.CONFIG_FILE = d/"config.json"
        for s in ("chapters","raw","logs"): (d/s).mkdir(exist_ok=True)
        agents.CONFIG = agents.load_config()
        ProjectStore(d).ensure_structure(name=name)
        agents.configure_project_logging(d)
        agents.set_stop_checker(_stop_event.is_set)
        self._pl.config(text=name); self._load_fw(); self._rf_files(); self._rf_ch()
        # 更新状态栏进度
        from agents import WritingNotes
        notes = WritingNotes.load(d / "writing_notes.json")
        ch_count = len(list((d/"chapters").glob("ch*.md"))) if (d/"chapters").exists() else 0
        summary = ProjectStore(d).summary()
        checkpoint = summary.get("checkpoint")
        task_hint = f" | 待恢复：{checkpoint.get('event_name','写作任务')}" if checkpoint else ""
        self._wl.config(text=f"章节 {ch_count} | 事件 {len(notes.events_completed)} | 字数 {notes.total_words}{task_hint}")
        self._log(f"[切换] {name}")

    def _new_proj(self):
        if self._running:
            messagebox.showwarning("提示", "当前有写作任务正在运行，请先停止任务。", parent=self.root)
            return
        n = simpledialog.askstring("新建项目","项目名称：",parent=self.root)
        if not n or not n.strip(): return
        n = n.strip()
        # 防覆盖：如果已存在，自动用故事创意前几字作为后缀
        if pm.exists(n):
            idea = self._idea.get("1.0",tk.END).strip()
            if idea and idea != PLACEHOLDER_IDEA:
                # 取前6个有效字符（去除标点和空格）
                import re
                clean = re.sub(r'[^\w一-鿿]', '', idea)[:6]
                if clean: n = f"{n}_{clean}"
            else:
                n = f"{n}_{datetime.now().strftime('%m%d')}"
            # 如果还是重复，加序号
            base = n
            i = 2
            while pm.exists(n):
                n = f"{base}_{i}"; i += 1
            messagebox.showinfo("提示",f"项目名称已存在，自动重命名为：\n「{n}」")
        try:
            pm.create(n, premise="" if self._ph_active else self._idea.get("1.0", tk.END).strip())
        except Exception as e:
            messagebox.showerror("创建失败", str(e), parent=self.root)
            return
        self._rf_proj(); self._p_combo.set(n); self._sw_proj(n)

    def _del_proj(self):
        if self._running:
            messagebox.showwarning("提示", "当前有写作任务正在运行，请先停止任务。", parent=self.root)
            return
        n = self._pv.get()
        if not n: return
        if not messagebox.askyesno("确认",f"删除项目「{n}」？不可恢复。"): return
        pm.delete(n); self._rf_proj()
        r = pm.list()
        if r: self._p_combo.set(r[0]); self._sw_proj(r[0])
        else: pm.create("默认项目"); self._rf_proj(); self._p_combo.set("默认项目"); self._sw_proj("默认项目")

    def _imp_proj(self):
        if self._running:
            messagebox.showwarning("提示", "当前有写作任务正在运行，请先停止任务。", parent=self.root)
            return
        s = filedialog.askdirectory(title="选择项目文件夹", parent=self.root)
        if not s: return
        source = Path(s).resolve()
        n = source.name
        target = (PROJECTS_DIR / n).resolve()
        if source == target:
            messagebox.showinfo("导入项目", "所选目录已经是当前项目目录，无需重复导入。", parent=self.root)
            self._p_combo.set(n); self._sw_proj(n)
            return
        if (
            source == PROJECTS_DIR.resolve()
            or PROJECTS_DIR.resolve() in source.parents
            or source in target.parents
        ):
            messagebox.showerror("导入失败", "不能把项目根目录或其内部目录作为项目整体导入。", parent=self.root)
            return
        if pm.exists(n):
            if not messagebox.askyesno("提示",f"「{n}」已存在，覆盖？"): return
            pm.delete(n)
        shutil.copytree(source, target)
        ProjectStore(target).ensure_structure(name=n)
        self._rf_proj()
        self._p_combo.set(n); self._sw_proj(n)

    # ==================== 框架 ====================

    def _load_fw(self):
        d = self._project_dir
        for fname, w in [("story_plan.md",self._pt),("characters.md",self._ct),("events_config.json",self._evt_t)]:
            w.delete("1.0",tk.END)
            f = d/fname
            if f.exists():
                try: w.insert("1.0",f.read_text("utf-8"))
                except Exception as e: w.insert("1.0",f"[读取失败：{e}]")
        self._load_evts()

    def _refresh_progress(self):
        """Refresh the status bar from persisted state after any task."""
        if not self._project_dir or not self._project_dir.exists():
            return
        summary = ProjectStore(self._project_dir).summary()
        checkpoint = summary.get("checkpoint")
        task_hint = f" | 待恢复：{checkpoint.get('event_name','写作任务')}" if checkpoint else ""
        self._wl.config(
            text=f"章节 {summary['chapters']} | 事件 {summary['events']} | 字数 {summary['words']}{task_hint}"
        )

    @property
    def _ed(self): return self._project_dir

    def _save_fw(self):
        d = self._project_dir
        for fname,w in [("story_plan.md",self._pt),("characters.md",self._ct),("events_config.json",self._evt_t)]:
            c = w.get("1.0",tk.END).strip()
            if c:
                try:
                    from project_store import atomic_write_json, atomic_write_text
                    if fname.endswith(".json"):
                        atomic_write_json(d/fname, json.loads(c), backup=True)
                    else:
                        atomic_write_text(d/fname, c, backup=True)
                except json.JSONDecodeError:
                    messagebox.showerror("错误", "events_config.json 不是有效 JSON")
                    return
                except Exception as e: messagebox.showerror("错误",f"保存 {fname} 失败：{e}"); return
        messagebox.showinfo("完成","已保存")

    # ==================== 事件 ====================

    def _load_evts(self):
        self._evts_data = []; self._ec["values"] = []; self._ev.set("")
        self._ed_lbl.config(text="选择事件后显示描述")

        f = self._project_dir / "events_config.json"
        if not f.exists():
            self._ec["values"] = ["（未生成故事框架）"]; return
        try:
            store = ProjectStore(self._project_dir)
            store.ensure_event_indices()
            evts, _ = store.load_events()
            self._evts_data = evts
            names = []
            for i,evt in enumerate(evts):
                nm = evt.get("name",evt.get("event_name",f"事件{i+1}"))
                status = evt.get("status", "pending")
                status_label = {"completed": "已完成", "writing": "进行中", "paused": "已暂停"}.get(status, "待写")
                names.append(f"{i+1}. [{status_label}] {nm}")
            if names: self._ec["values"] = names; self._ec.set(names[0]); self._on_evt()
            else: self._ec["values"] = ["（事件列表为空）"]
        except Exception as e: self._ec["values"] = [f"（解析失败：{str(e)[:30]}）"]

    def _on_evt(self, e=None):
        sel = self._ev.get()
        if not sel or not self._evts_data: return
        try:
            idx = int(sel.split(".")[0])-1
            if 0 <= idx < len(self._evts_data):
                evt = self._evts_data[idx]
                sm = evt.get("summary",evt.get("event_summary",""))
                ch = evt.get("characters",[]); sc = evt.get("scene","")
                d = sm
                status = evt.get("status", "pending")
                d = f"状态：{ {'completed':'已完成','writing':'进行中','paused':'已暂停'}.get(status, '待写') }\n" + d
                if ch: d += f"\n角色：{'、'.join(ch)}"
                if sc: d += f"\n场景：{sc}"
                self._ed_lbl.config(text=d, fg=C_TEXT)
        except: pass

    def _write_sel(self):
        sel = self._ev.get()
        if not sel or not self._evts_data:
            messagebox.showwarning("提示","请先在左侧点击「生成故事框架」\n生成后选择事件再开始写作")
            return
        try:
            idx = int(sel.split(".")[0])-1
            if 0 <= idx < len(self._evts_data):
                evt = self._evts_data[idx]
                nm = evt.get("name",evt.get("event_name",""))
                sm = evt.get("summary",evt.get("event_summary",nm))
                event_index = event_identifier(evt)
                def do():
                    from agents import NovelWritingSystem; s = NovelWritingSystem(self._project_dir, _stop_event.is_set)
                    s.run_event(nm, sm, event_index=event_index)
                    self.root.after(0, self._rf_ch); self.root.after(0, self._rf_files)
                    self.root.after(0, lambda: self._log(f"[OK] 「{nm}」完成，可继续选择下一个事件"))
                self._run(do)
        except:
            messagebox.showerror("错误","事件选择无效")

    def _write_vol(self):
        from agents import WritingNotes
        notes = WritingNotes.load(self._project_dir / "writing_notes.json")
        done = len(notes.events_completed)
        if done > 0:
            if not messagebox.askyesno("断点续写",
                                       f"已写完 {done} 个事件\n"
                                       f"继续写整卷将从上次中断处开始。\n\n"
                                       f"是否继续？"):
                return
        def do():
            from agents import NovelWritingSystem; s = NovelWritingSystem(self._project_dir, _stop_event.is_set)
            s.run_volume(s.notes.current_volume)
            self.root.after(0, self._rf_ch); self.root.after(0, self._rf_files)
        self._run(do)

    def _resume_checkpoint(self):
        """Resume the active task saved in the current project's checkpoint."""
        store = ProjectStore(self._project_dir)
        checkpoint = store.load_checkpoint()
        if not checkpoint:
            messagebox.showinfo("恢复断点", "当前项目没有可恢复的任务。", parent=self.root)
            return
        stage = checkpoint.get("stage", "writing")
        event = checkpoint.get("event_name", "未命名事件")
        if not messagebox.askyesno(
            "恢复断点",
            f"检测到未完成任务：{event}\n当前阶段：{stage}\n\n是否继续？",
            parent=self.root,
        ):
            return

        def do():
            from agents import NovelWritingSystem
            system = NovelWritingSystem(self._project_dir, _stop_event.is_set)
            system.resume_checkpoint()
            self.root.after(0, self._load_fw)
            self.root.after(0, self._rf_ch)
            self.root.after(0, self._rf_files)
            self.root.after(0, lambda: self._log(f"[恢复] 已处理断点：{event}"))

        self._run(do)

    def _continue_write(self):
        """续写新篇章：基于已写章节内容，AI 自动生成后续事件"""
        d = self._project_dir
        ch_dir = d / "chapters"
        if not ch_dir.exists() or not list(ch_dir.glob("ch*.md")):
            messagebox.showwarning("提示",
                "当前项目还没有已完成的章节。\n\n"
                "请先生成故事框架并写完至少一个章节，\n"
                "才能使用续写功能。")
            return

        # 读取已有章节内容（取最近3章作为上下文）
        chapters = sorted(ch_dir.glob("ch*.md"))
        recent = chapters[-3:]  # 最近3章
        context_parts = []
        for ch in recent:
            try:
                content = ch.read_text("utf-8")
                # 每章取前500字 + 后300字作为摘要
                head = content[:500]
                tail = content[-300:] if len(content) > 800 else ""
                context_parts.append(f"【{ch.name}】\n{head}\n...\n{tail}" if tail else f"【{ch.name}】\n{head}")
            except Exception:
                pass

        if not context_parts:
            messagebox.showerror("错误", "无法读取已有章节内容")
            return

        story_so_far = "\n\n".join(context_parts)

        # 读取故事大纲（如果有）
        plan = ""
        plan_file = d / "story_plan.md"
        if plan_file.exists():
            try:
                plan = plan_file.read_text("utf-8")[:1000]  # 取前1000字
            except Exception:
                pass

        # 读取已有事件记录
        from agents import WritingNotes
        notes = WritingNotes.load(d / "writing_notes.json")
        done_events = "\n".join([f"- {e.get('name','')}" for e in notes.events_completed[-10:]])

        # 构建续写提示
        length = self._pref_len.get()
        style = self._pref_style.get()

        prompt = f"""请基于以下故事内容，规划后续 5-8 个事件。

## 故事大纲（参考）
{plan if plan else "（无）"}

## 已完成事件
{done_events if done_events else "（无）"}

## 最近章节内容
{story_so_far}

## 创作偏好
- 篇幅：{length}
- 风格：{style}

请输出 JSON 格式：
{{"events": [
  {{"name": "事件名称", "summary": "事件描述（50-100字）", "characters": ["角色1"], "scene": "场景"}},
  ...
]}}

要求：
1. 承接已有剧情，保持连贯
2. 角色性格与已有内容一致
3. 设置新的冲突和悬念
4. 适合{length}、{style}的风格"""

        # 确认
        if not messagebox.askyesno("续写新篇章",
            f"已读取 {len(recent)} 个章节作为上下文。\n"
            f"AI 将自动生成后续事件。\n\n"
            f"是否继续？"):
            return

        def do():
            from agents import call_api
            self.root.after(0, lambda: self._log("[续写] 正在分析已有内容，生成后续事件..."))
            result = call_api(
                system_prompt="你是一位资深网文编辑，擅长根据已有故事内容规划后续剧情。输出必须是合法的 JSON。",
                user_prompt=prompt,
                max_tokens=4000,
                temperature=0.8
            )
            if not result:
                self.root.after(0, lambda: messagebox.showerror("失败", "AI 未返回内容，请检查 API"))
                return

            # 解析 JSON
            try:
                json_match = __import__("re").search(r'\{.*\}', result, __import__("re").DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    events = data.get("events", [])
                else:
                    data = json.loads(result)
                    events = data.get("events", [])
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("失败", f"解析失败：{e}"))
                return

            if not events:
                self.root.after(0, lambda: messagebox.showerror("失败", "未生成有效事件"))
                return

            # 追加到 events_config.json
            evt_file = d / "events_config.json"
            existing = []
            wrapped = False
            if evt_file.exists():
                try:
                    ProjectStore(d).ensure_event_indices()
                    old = json.loads(evt_file.read_text("utf-8"))
                    wrapped = isinstance(old, dict)
                    existing = old if isinstance(old, list) else old.get("events", [])
                except Exception:
                    pass

            # 给新事件编号续接
            start_idx = len(existing)
            for i, evt in enumerate(events):
                evt["index"] = start_idx + i + 1
                if "status" not in evt:
                    evt["status"] = "pending"

            existing.extend(events)

            # 保存
            from project_store import atomic_write_json
            if wrapped:
                payload = dict(old) if isinstance(old, dict) else {}
                payload["events"] = existing
            else:
                payload = existing
            atomic_write_json(evt_file, payload, backup=True)
            ProjectStore(d).ensure_event_indices()

            # 刷新
            self.root.after(0, self._load_fw)
            self.root.after(0, self._rf_files)
            self.root.after(0, self._load_evts)
            self.root.after(0, lambda: self._log(f"[续写] 已生成 {len(events)} 个新事件"))
            self.root.after(0, lambda: messagebox.showinfo("完成",
                f"已生成 {len(events)} 个新事件！\n\n"
                f"事件选择器已更新，选择新事件即可开始写作。"))

        self._run(do)

    # ==================== 章节 ====================

    def _rf_ch(self):
        d = self._project_dir/"chapters"
        if not d.exists(): return
        fs = sorted(d.glob("ch*.md")); names = [f.name for f in fs]
        self._chc["values"] = names
        if names: self._chc.set(names[-1]); self._load_ch(fs[-1])

    def _on_ch(self, e=None):
        n = self._chv.get()
        if n: self._load_ch(self._project_dir/"chapters"/n)

    def _load_ch(self, p):
        try:
            self._ch_t.delete("1.0",tk.END);
            self._ch_t.insert("1.0",p.read_text("utf-8"))
        except Exception as e:
            self._ch_t.delete("1.0",tk.END); self._ch_t.insert("1.0",f"[{e}]")

    # ==================== 文件 ====================

    def _rf_files(self):
        d = self._project_dir; self._fl.delete(0,tk.END)
        if d and d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    self._fl.insert(tk.END, f.name)

    def _on_fclick(self, e=None):
        s = self._fl.curselection()
        if not s: return
        fn = self._fl.get(s[0]); p = self._project_dir/fn
        if fn.endswith(".md") and p.exists():
            self._pt.delete("1.0",tk.END); self._pt.insert("1.0",p.read_text("utf-8"))
            self._tab("fw")
        elif fn.endswith(".json") and p.exists():
            self._evt_t.delete("1.0",tk.END); self._evt_t.insert("1.0",p.read_text("utf-8"))
            self._tab("fw")

    # ==================== 生成框架 ====================

    def _gen_fw(self):
        idea = self._idea.get("1.0",tk.END).strip()
        if not idea or self._ph_active: messagebox.showwarning("提示","请先输入故事创意"); return
        # 拼接偏好信息
        length = self._pref_len.get()
        style = self._pref_style.get()
        full_idea = f"{idea}\n\n【创作偏好】\n- 篇幅：{length}\n- 风格：{style}"
        def do():
            from agents import generate_story_framework, save_story_framework
            fw = generate_story_framework(full_idea)
            if fw:
                save_story_framework(fw, project_dir=self._project_dir)
                self.root.after(0, self._load_fw); self.root.after(0, self._rf_files)
                self.root.after(0, lambda: messagebox.showinfo("完成","故事框架已生成！\n\n现在可以从左侧选择事件并开始写作。"))
            else:
                self.root.after(0, lambda: messagebox.showerror("失败","生成失败，请检查 API"))
        self._run(do)

    # ==================== 日志 ====================

    def _out(self, msg): self.root.after(0, self._log, msg)

    def _log(self, msg):
        self._lg.config(state=tk.NORMAL)
        tag = "info"
        if msg.startswith("[OK]") or "完成" in msg: tag = "success"
        elif msg.startswith("[X]") or "失败" in msg: tag = "error"
        elif msg.startswith("[WARN]"): tag = "warning"
        elif msg.startswith("=") or msg.startswith("[卷]") or msg.startswith("[书]") or msg.startswith("[切换]"): tag = "header"
        self._lg.insert(tk.END, msg+"\n", tag); self._lg.see(tk.END)
        self._lg.config(state=tk.DISABLED)
        if self._active != "log": self._tab("log")

    def _clr_log(self):
        self._lg.config(state=tk.NORMAL); self._lg.delete("1.0",tk.END); self._lg.config(state=tk.DISABLED)

    # ==================== 工具 ====================

    def _inp(self, prompt):
        import queue; q = queue.Queue()
        self.root.after(0,lambda: q.put(simpledialog.askstring("输入",prompt,parent=self.root) or "")); return q.get()

    def _conf(self, prompt):
        import queue; q = queue.Queue()
        self.root.after(0,lambda: q.put(messagebox.askyesno("确认",prompt,parent=self.root))); return q.get()

    def _set_r(self, r):
        self._running = r
        self._sl.config(text="运行中..." if r else "就绪")
        self._btn_stop.config(state=tk.NORMAL if r else tk.DISABLED)
        if not r:
            self._refresh_progress()

    def _run(self, func, *a, **kw):
        if self._running: messagebox.showwarning("提示","有任务正在运行"); return
        _stop_event.clear()
        self._set_r(True)
        def wrap():
            try: func(*a,**kw)
            except Exception as e: self.root.after(0,lambda:self._log(f"[X] 错误：{e}"))
            finally:
                self.root.after(0,self._set_r,False)
                self.root.after(0,self._refresh_progress)
                if _stop_event.is_set():
                    self.root.after(0,lambda:self._log("[!] 已停止"))
        threading.Thread(target=wrap, daemon=True).start()

    def _stop(self):
        _stop_event.set()
        self._log("[!] 已请求停止（当前阶段完成后保存断点）")

    def _settings(self):
        SettingsDialog(self.root, self._project_dir)

    def run(self):
        self.root.mainloop()


def launch_gui():
    MainWindow().run()

if __name__ == "__main__":
    launch_gui()
