"""Claude Monitor — Claude の使用状況をデスクトップに表示するウィジェット (Windows)

- コンテキストウィンドウ (最後にやり取りした会話)
- 5時間制限 / 週間の使用率、使用クレジット、クラウドセッションクレジット
- Claude デスクトップアプリの起動/終了に合わせて表示/非表示
- ドラッグで移動 / ダブルクリックで再取得 / 右クリックでメニュー

非公式ツールです。Anthropic とは関係ありません。
"""
import json, os, sys, glob, threading, tkinter as tk
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
REFRESH_MS = 180_000
def claude_desktop_running():
    """Claude デスクトップアプリが起動中か"""
    import ctypes
    from ctypes import wintypes
    psapi, k32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
    arr = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return False
    buf = ctypes.create_unicode_buffer(1024)
    for pid in arr[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        size = wintypes.DWORD(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        if ok:
            p = buf.value.lower()
            # デスクトップアプリ (通常版 / Microsoft Store 版) を検出し、Claude Code 本体は除外
            if p.endswith("\\claude.exe") and not any(
                    x in p for x in ("claude-code", "\\.local\\", "npm", "node_modules")):
                return True
    return False

def fetch_plan_usage():
    """プランの使用量上限 (5時間 / 週間) を公式APIから取得。トークンは読むだけで更新しない。"""
    import time, urllib.request
    path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    with open(path, encoding="utf-8") as f:
        o = json.load(f)["claudeAiOauth"]
    if o.get("expiresAt", 0) / 1000 < time.time():
        raise RuntimeError("ログイン期限切れ: ターミナルで claude を一度起動してください")
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + o["accessToken"], "anthropic-beta": "oauth-2025-04-20"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

_title_cache = {}

def context_status():
    """最後に更新されたセッション (= 今開いている会話) のコンテキスト使用量"""
    files = glob.glob(os.path.join(LOG_DIR, "*", "*.jsonl"))
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 2_000_000))
        lines = f.read().decode("utf-8", "ignore").splitlines()
    usage = model = None
    for line in reversed(lines):
        if '"usage"' in line and '"assistant"' in line:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("isSidechain"):
                continue
            usage, model = d["message"]["usage"], d["message"].get("model", "")
            break
    if not usage:
        return None
    used = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))
    m = model.lower()
    limit = 1_000_000 if ("1m" in m or "opus-5" in m or "fable" in m or "sonnet-5" in m) else 200_000
    mtime = os.path.getmtime(path)
    if _title_cache.get(path, (None, 0))[1] != mtime:
        title = ""
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"custom-title"' in line:
                    try:
                        title = json.loads(line).get("customTitle", title)
                    except Exception:
                        pass
        _title_cache[path] = (title, mtime)
    title = _title_cache[path][0] or os.path.basename(path)[:8]
    return {"used": used, "limit": limit, "title": title, "model": model}

def fmt(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(n)

class App:
    BG, FG, SUB, ACC = "#1f1e1d", "#f0eee6", "#9a968c", "#d97757"

    def __init__(self):
        self.root = r = tk.Tk()
        r.title("Claude Monitor")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", 0.92)
        r.configure(bg=self.BG)
        r.geometry(f"+{r.winfo_screenwidth()-280}+60")
        self.topmost = tk.BooleanVar(value=True)

        tk.Label(r, text="● Claude Usage", bg=self.BG, fg=self.ACC,
                 font=("Yu Gothic UI", 10, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        self.W = 250
        self.cv = tk.Canvas(r, width=self.W, height=150, bg=self.BG, highlightthickness=0)
        self.cv.pack(padx=12)
        self.foot = tk.Label(r, bg=self.BG, fg=self.SUB, font=("Yu Gothic UI", 8), text="集計中…")
        self.foot.pack(anchor="w", padx=12, pady=(2, 8))
        self.login_btn = tk.Label(r, text="🔑 ログインを更新する", bg="#3a3835", fg=self.FG,
                                  font=("Yu Gothic UI", 9, "bold"), padx=10, pady=4, cursor="hand2")
        self.login_btn.bind("<Button-1>", lambda e: self._renew_login())

        for w in (r, self.cv, self.foot):
            w.bind("<Button-1>", self._start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", self._menu)
            w.bind("<Double-Button-1>", lambda e: self.refresh())

        self.m = tk.Menu(r, tearoff=0)
        self.m.add_command(label="今すぐ更新", command=self.refresh)
        self.m.add_checkbutton(label="常に最前面", variable=self.topmost,
                               command=lambda: r.attributes("-topmost", self.topmost.get()))
        self.m.add_command(label="スタートアップに登録", command=self._startup)
        self.m.add_separator()
        self.m.add_command(label="終了", command=r.destroy)
        self.visible = True
        self.plan = self.ctx = None
        self._startup(quiet=True)  # 常にバックグラウンドで待機し、Claude の起動を検知する
        self._interval = REFRESH_MS
        self._loop()
        self._ctx_loop()
        # 起動したことが分かるよう、最初の5秒は必ず表示してから判定を始める
        self.foot.config(text="起動しました · Claude アプリを開くと表示されます")
        self.root.after(5000, self._watch)

    def _watch(self):
        """Claude デスクトップアプリの起動/終了に合わせて表示/非表示"""
        running = claude_desktop_running()
        if running and not self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", self.topmost.get())
            self.visible = True
            self.refresh()
            self._render()
        elif not running and self.visible:
            self.root.withdraw()
            self.visible = False
        self.root.after(3000, self._watch)

    def _start(self, e): self._x, self._y = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
    def _drag(self, e): self.root.geometry(f"+{e.x_root-self._x}+{e.y_root-self._y}")
    def _menu(self, e): self.m.tk_popup(e.x_root, e.y_root)

    def _startup(self, quiet=False):
        folder = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        with open(os.path.join(folder, "claude_monitor.bat"), "w", encoding="mbcs") as f:
            f.write(f'@echo off\nstart "" "{exe}" "{os.path.abspath(__file__)}"\n')
        if not quiet:
            self.foot.config(text="✓ スタートアップに登録しました")

    def refresh(self):
        """手動/表示時の即時取得 (連打しても多重実行しない)"""
        if getattr(self, "_busy", False):
            return
        self._busy = True
        threading.Thread(target=self._work, daemon=True).start()

    def _loop(self):
        """定期取得 (これだけが次回を予約する)"""
        if self.visible:
            self.refresh()
        self.root.after(self._interval, self._loop)

    def _work(self):
        try:
            self.plan = fetch_plan_usage()
            self.root.after(0, self._show_login_btn, False)
            self._interval = REFRESH_MS
            self.root.after(0, self._render)
            self.root.after(0, self.foot.config, {"text": f"更新 {datetime.now():%H:%M:%S} · ダブルクリックで再取得"})
        except Exception as ex:
            if "429" in str(ex):  # 取得しすぎ → 間隔を広げる (前回の値は表示したまま)
                self._interval = min(self._interval * 2, 30 * 60_000)
                ex = f"取得制限中 ({self._interval // 60000}分後に再試行)"
            self.root.after(0, self.foot.config, {"text": f"⚠ {ex}"[:60]})
            if "ログイン期限切れ" in str(ex) or "401" in str(ex):
                self.root.after(0, self._show_login_btn, True)
        finally:
            self._busy = False

    def _show_login_btn(self, show):
        if show:
            self.login_btn.pack(anchor="w", padx=12, pady=(0, 10))
        else:
            self.login_btn.pack_forget()

    def _renew_login(self):
        """ターミナルで claude を起動 → 自動で終了 (ログイントークンが更新される)"""
        import shutil, subprocess
        exe = shutil.which("claude")
        if not exe:
            found = sorted(glob.glob(os.path.join(os.environ["APPDATA"], "Claude", "claude-code", "*", "claude.exe")),
                           key=os.path.getmtime)
            exe = found[-1] if found else None
        if not exe:
            self.foot.config(text="⚠ claude が見つかりません")
            return
        self.login_btn.config(text="⏳ 更新中…")

        def run():
            try:
                # ホームフォルダで1回だけ応答させて終了 (ごく短い入力なので使用量はほぼゼロ)
                subprocess.run([exe, "-p", "ok", "--model", "haiku"], cwd=os.path.expanduser("~"),
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=120, creationflags=0x08000000)
            except Exception:
                pass
            self.root.after(0, self.login_btn.config, {"text": "🔑 ログインを更新する"})
            self.refresh()
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _color(ratio):
        return "#6fbf73" if ratio < 0.6 else "#e0b44c" if ratio < 0.85 else "#e0604c"

    def _meter(self, y, title, right, ratio, color, sub):
        cv, W = self.cv, self.W
        cv.create_text(0, y, anchor="nw", text=title, fill=self.FG, font=("Yu Gothic UI", 9, "bold"))
        cv.create_text(W, y, anchor="ne", text=right, fill=self.FG, font=("Yu Gothic UI", 9))
        by = y + 20
        cv.create_rectangle(0, by, W, by + 10, fill="#3a3835", outline="")
        if ratio > 0:
            cv.create_rectangle(0, by, max(3, W * min(ratio, 1)), by + 10, fill=color, outline="")
        cv.create_text(0, by + 14, anchor="nw", text=sub, fill=self.SUB, font=("Yu Gothic UI", 8))

    @staticmethod
    def _until(iso):
        """リセット時刻 → (残り表示, 時刻表示)"""
        t = datetime.fromisoformat(iso)
        secs = max(0, int((t - datetime.now(timezone.utc)).total_seconds()))
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        left = f"{d}日{h}時間後" if d else f"{h}時間{rem // 60}分後"
        wd = "月火水木金土日"[t.astimezone().weekday()]
        return left, f"{t.astimezone():%m/%d}({wd}) {t.astimezone():%H:%M}"

    def _ctx_loop(self):
        if self.visible:
            try:
                self.ctx = context_status()
            except Exception:
                self.ctx = None
            self._render()
        self.root.after(5000, self._ctx_loop)

    def _render(self):
        self.cv.delete("all")
        y = 0
        c = self.ctx
        if c:
            r = c["used"] / c["limit"]
            self._meter(y, "🧠 コンテキストウィンドウ", f"{fmt(c['used'])} / {fmt(c['limit'])} ({r*100:.0f}%)",
                        r, self._color(r), c["title"][:30])
            y += 50
        u = self.plan
        if not u:
            self.cv.config(height=max(y - 6, 40))
            return
        for key, title in (("five_hour", "⚡ 5時間制限"), ("seven_day", "📅 週間・全モデル")):
            b = u.get(key)
            if not b:
                continue
            r = (b.get("utilization") or 0) / 100
            left, at = self._until(b["resets_at"]) if b.get("resets_at") else ("-", "-")
            self._meter(y, title, f"{r*100:.0f}%", r, self._color(r), f"{left}にリセット · {at}")
            y += 50
        ex = u.get("extra_usage") or {}
        if ex.get("is_enabled"):
            used = (ex.get("used_credits") or 0) / 10 ** ex.get("decimal_places", 2)
            lim = ex.get("monthly_limit")
            lim = lim / 10 ** ex.get("decimal_places", 2) if lim else None
            r = used / lim if lim else 0
            self._meter(y, "💳 使用クレジット", f"${used:.2f}" + (f" / ${lim:.0f}" if lim else ""),
                        r, self._color(r), "プラン上限を超えた分に使用" + ("" if lim else " · 上限なし"))
            y += 50
        cl = u.get("iguana_necktie")  # クラウドセッションクレジット
        if cl and cl.get("limit_dollars"):
            rest, lim = cl.get("remaining_dollars") or 0, cl["limit_dollars"]
            exp = self._until(cl["resets_at"])[1] if cl.get("resets_at") else "-"
            self._meter(y, "☁ クラウドセッションクレジット", f"残り ${rest:.0f} / ${lim:.0f}",
                        rest / lim, "#6fa8dc", f"{exp} に期限切れ")
            y += 50
        self.cv.config(height=max(y - 6, 40))


if __name__ == "__main__":
    App().root.mainloop()
