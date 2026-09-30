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

# 同じファイルを使う他のツールと数字を共有する。ファイルが無くても単独で動く
# t=最後に取りに行った時刻 / fetched=最後に取れた時刻 / wait_until=429のあと問い合わせを止める期限
SHARED_PATH = os.path.join(os.path.expanduser("~"), ".claude", "usage_shared.json")
MIN_GAP = 180          # 自動更新で窓口へ問い合わせる最短間隔（秒）
MIN_GAP_MANUAL = 30    # 手動更新（ダブルクリック等）の最短間隔（秒）

def _shared_read():
    try:
        with open(SHARED_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _shared_write(d):
    tmp = f"{SHARED_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, SHARED_PATH)
    except Exception:
        pass

def fetch_plan_usage(manual=False):
    """プランの使用量上限 (5時間 / 週間) を公式APIから取得。トークンは読むだけで更新しない。
    戻り値: (データ, 取得した時刻, 注記)。他のツールが最近取った数字があればそれを使う"""
    import time, urllib.request, urllib.error
    now = time.time()
    sh = _shared_read()
    if now < sh.get("wait_until", 0):
        if sh.get("data"):
            return sh["data"], sh.get("fetched", 0), f"取得制限中 · {int((sh['wait_until'] - now) // 60) + 1}分後に再試行"
        raise RuntimeError("429 取得制限中")
    if sh.get("data") and now - sh.get("t", 0) < (MIN_GAP_MANUAL if manual else MIN_GAP):
        return sh["data"], sh.get("fetched", 0), ""
    sh["t"] = now
    _shared_write(sh)  # 先に書いて、他のツールと同時に問い合わせないようにする
    path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    with open(path, encoding="utf-8") as f:
        o = json.load(f)["claudeAiOauth"]
    if o.get("expiresAt", 0) / 1000 < now:
        raise RuntimeError("ログイン期限切れ: ターミナルで claude を一度起動してください")
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + o["accessToken"], "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = int(e.headers.get("retry-after") or 300)
            except ValueError:
                wait = 300
            sh["wait_until"] = now + max(60, wait)
            _shared_write(sh)
        raise
    sh.update(data=data, fetched=now, wait_until=0)
    _shared_write(sh)
    return data, now, ""

# ウィジェット自身の設定（位置・コンパクト表示）
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claude_monitor_settings.json")

def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(d):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass

def on_screen(x, y):
    """保存した位置がいまの画面（複数モニター含む）の中にあるか"""
    import ctypes
    gsm = ctypes.windll.user32.GetSystemMetrics
    vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)  # 仮想スクリーン全体
    return vx <= x < vx + vw - 40 and vy <= y < vy + vh - 40

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
        self.settings = load_settings()
        # 位置は「右上の角」で覚える（通常/コンパクト/大きさ変更で角がずれないように）
        pos = self.settings.get("pos_tr")
        if pos and on_screen(pos[0] - 40, pos[1]):
            self._right, self._top = pos
        else:
            self._right, self._top = r.winfo_screenwidth() - 30, 60
        self.S = min(max(float(self.settings.get("scale", 1.0)), 0.7), 2.0)  # 表示の大きさ
        self.topmost = tk.BooleanVar(value=True)
        self.compact = tk.BooleanVar(value=bool(self.settings.get("compact")))

        self.header = tk.Label(r, text="● Claude Usage", bg=self.BG, fg=self.ACC, font=self._f(10, True))
        self.header.pack(anchor="w", padx=12, pady=(8, 4))
        self.W = round(250 * self.S)
        self.cv = tk.Canvas(r, width=self.W, height=150, bg=self.BG, highlightthickness=0)
        self.cv.pack(padx=12)
        self.foot = tk.Label(r, bg=self.BG, fg=self.SUB, font=self._f(8), text="集計中…")
        self.foot.pack(anchor="w", padx=12, pady=(2, 8))
        self.login_btn = tk.Label(r, text="🔑 ログインを更新する", bg="#3a3835", fg=self.FG,
                                  font=self._f(9, True), padx=10, pady=4, cursor="hand2")
        self.login_btn.bind("<Button-1>", lambda e: self._renew_login())
        self.grip = tk.Label(r, text="◢", bg=self.BG, fg="#5a5750", font=("Yu Gothic UI", 8), cursor="size_nw_se")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<Button-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonRelease-1>", self._resize_end)

        for w in (r, self.header, self.cv, self.foot):
            w.bind("<Button-1>", self._start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save_pos)
            w.bind("<Button-3>", self._menu)
            w.bind("<Double-Button-1>", lambda e: self.refresh(manual=True))

        self.m = tk.Menu(r, tearoff=0)
        self.m.add_command(label="今すぐ更新", command=lambda: self.refresh(manual=True))
        self.m.add_checkbutton(label="コンパクト表示", variable=self.compact, command=self._toggle_compact)
        self.m.add_checkbutton(label="常に最前面", variable=self.topmost,
                               command=lambda: r.attributes("-topmost", self.topmost.get()))
        self.m.add_command(label="スタートアップに登録", command=self._startup)
        self.m.add_separator()
        self.m.add_command(label="終了", command=r.destroy)
        self.visible = True
        self.plan = self.ctx = None
        self._apply_compact()
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
            self._hide_tip()
            self.root.withdraw()
            self.visible = False
        self.root.after(3000, self._watch)

    def _f(self, size, bold=False):
        """大きさ設定に合わせたフォント"""
        return ("Yu Gothic UI", max(6, round(size * self.S))) + (("bold",) if bold else ())

    def _place(self):
        """右上の角を固定したまま、中身に合わせて配置し直す"""
        self.root.update_idletasks()
        if getattr(self, "_rs", None):  # つまみでの大きさ変更中は左上を固定（カーソルに付いてくる）
            self._right = self._rs[2] + self.root.winfo_reqwidth()
        self.root.geometry(f"+{self._right - self.root.winfo_reqwidth()}+{self._top}")
        self.grip.lift()

    def _resize_start(self, e):
        self._rs = (e.x_root, self.S, self.root.winfo_x())

    def _resize(self, e):
        """つまみを右へ引くと大きく、左へ押すと小さくなる（右上の角は固定）"""
        x0, s0, _ = self._rs
        base = 250 * s0
        s = min(max(s0 * (base + (e.x_root - x0)) / base, 0.7), 2.0)
        if abs(s - self.S) < 0.02:
            return
        self.S = s
        self.W = round(250 * s)
        self.header.config(font=self._f(10, True))
        self.foot.config(font=self._f(8))
        self.login_btn.config(font=self._f(9, True))
        self._render()

    def _resize_end(self, e):
        self._rs = None
        self.settings["pos_tr"] = [self._right, self._top]
        self.settings["scale"] = round(self.S, 2)
        save_settings(self.settings)

    def _start(self, e): self._x, self._y = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
    def _drag(self, e): self.root.geometry(f"+{e.x_root-self._x}+{e.y_root-self._y}")
    def _menu(self, e): self.m.tk_popup(e.x_root, e.y_root)

    def _save_pos(self, e=None):
        """ドラッグし終えた位置を（右上の角で）覚える"""
        self._right = self.root.winfo_x() + self.root.winfo_width()
        self._top = self.root.winfo_y()
        self.settings["pos_tr"] = [self._right, self._top]
        save_settings(self.settings)

    def _toggle_compact(self):
        self.settings["compact"] = self.compact.get()
        save_settings(self.settings)
        self._apply_compact()
        self._render()

    def _apply_compact(self):
        """コンパクト表示では見出しと下の行を隠し、1行だけにする"""
        if self.compact.get():
            self.header.pack_forget()
            self.foot.pack_forget()
            self.cv.pack_configure(pady=6)
        else:
            self.cv.pack_forget()
            self.header.pack(anchor="w", padx=12, pady=(8, 4))
            self.cv.pack(padx=12)
            self.foot.pack(anchor="w", padx=12, pady=(2, 8), after=self.cv)
        self._place()

    def _startup(self, quiet=False):
        folder = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        with open(os.path.join(folder, "claude_monitor.bat"), "w", encoding="mbcs") as f:
            f.write(f'@echo off\nstart "" "{exe}" "{os.path.abspath(__file__)}"\n')
        if not quiet:
            self.foot.config(text="✓ スタートアップに登録しました")

    def refresh(self, manual=False):
        """手動/表示時の即時取得 (連打しても多重実行しない)"""
        if getattr(self, "_busy", False):
            return
        self._busy = True
        threading.Thread(target=self._work, args=(manual,), daemon=True).start()

    def _loop(self):
        """定期取得 (これだけが次回を予約する)"""
        if self.visible:
            self.refresh()
        self.root.after(self._interval, self._loop)

    def _work(self, manual=False):
        try:
            self.plan, fetched, note = fetch_plan_usage(manual)
            self.root.after(0, self._show_login_btn, False)
            self._interval = REFRESH_MS
            self.root.after(0, self._render)
            text = f"更新 {datetime.fromtimestamp(fetched):%H:%M:%S} · " + (note or "ダブルクリックで再取得")
            self.root.after(0, self.foot.config, {"text": text})
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
            self.refresh(manual=True)
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _color(ratio):
        return "#6fbf73" if ratio < 0.6 else "#e0b44c" if ratio < 0.85 else "#e0604c"

    ROW = 52  # メーター1つ分の高さ（大きさ1.0のとき）

    def _meter(self, y, title, right, ratio, color, sub):
        cv, W, S = self.cv, self.W, self.S
        cv.create_text(0, y, anchor="nw", text=title, fill=self.FG, font=self._f(9, True))
        cv.create_text(W, y, anchor="ne", text=right, fill=self.FG, font=self._f(9))
        by = y + 20 * S
        cv.create_rectangle(0, by, W, by + 10 * S, fill="#3a3835", outline="")
        if ratio > 0:
            cv.create_rectangle(0, by, max(3, W * min(ratio, 1)), by + 10 * S, fill=color, outline="")
        cv.create_text(0, by + 14 * S, anchor="nw", text=sub, fill=self.SUB, font=self._f(8))

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

    def _render_compact(self):
        """1行表示: 🧠15%  ⚡7%  📅24%（数字の色は使用率に応じて変わる。カーソルを乗せると説明が出る）"""
        self._hide_tip()
        items = []
        if self.ctx:
            c = self.ctx
            r = c["used"] / c["limit"]
            items.append(("🧠", r, f"コンテキストウィンドウ  {fmt(c['used'])} / {fmt(c['limit'])} ({r*100:.0f}%)\n{c['title'][:30]}"))
        u = self.plan or {}
        for key, icon, name in (("five_hour", "⚡", "5時間制限"), ("seven_day", "📅", "週間・全モデル")):
            b = u.get(key)
            if b:
                r = (b.get("utilization") or 0) / 100
                left, at = self._until(b["resets_at"]) if b.get("resets_at") else ("-", "-")
                items.append((icon, r, f"{name}  {r*100:.0f}%\n{left}にリセット · {at}"))
        x = 0
        for i, (icon, r, tip) in enumerate(items or [("…", None, "取得中")]):
            tag = f"item{i}"
            t = self.cv.create_text(x, 11 * self.S, anchor="w", text=icon, fill=self.FG, font=self._f(10), tags=tag)
            x = self.cv.bbox(t)[2] + 2
            if r is not None:
                t = self.cv.create_text(x, 11 * self.S, anchor="w", text=f"{r*100:.0f}%", tags=tag,
                                        fill=self._color(r), font=self._f(10, True))
                x = self.cv.bbox(t)[2] + 12
            self.cv.tag_bind(tag, "<Enter>", lambda e, s=tip: self._show_tip(e, s))
            self.cv.tag_bind(tag, "<Leave>", lambda e: self._hide_tip())
        self.cv.config(width=max(x - 10 + 14 * self.S, 40), height=round(22 * self.S))  # 右端はつまみの分あける
        self._place()

    def _show_tip(self, e, text):
        """カーソルの近くに説明を出す"""
        self._hide_tip()
        tip = self._tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg="#3a3835", fg=self.FG, justify="left",
                 font=("Yu Gothic UI", 9), padx=8, pady=4).pack()
        tip.update_idletasks()
        x, y = e.x_root + 12, e.y_root + 16
        if x + tip.winfo_width() > self.root.winfo_screenwidth():  # 画面右端からはみ出さない
            x = e.x_root - tip.winfo_width() - 12
        tip.geometry(f"+{x}+{y}")

    def _hide_tip(self):
        if getattr(self, "_tip", None):
            self._tip.destroy()
            self._tip = None

    def _render(self):
        self.cv.delete("all")
        if self.compact.get():
            return self._render_compact()
        self._hide_tip()
        self.cv.config(width=self.W)
        y = 0
        c = self.ctx
        if c:
            r = c["used"] / c["limit"]
            self._meter(y, "🧠 コンテキストウィンドウ", f"{fmt(c['used'])} / {fmt(c['limit'])} ({r*100:.0f}%)",
                        r, self._color(r), c["title"][:30])
            y += self.ROW * self.S
        u = self.plan
        if not u:
            self.cv.config(height=max(y, 40))
            self._place()
            return
        for key, title in (("five_hour", "⚡ 5時間制限"), ("seven_day", "📅 週間・全モデル")):
            b = u.get(key)
            if not b:
                continue
            r = (b.get("utilization") or 0) / 100
            left, at = self._until(b["resets_at"]) if b.get("resets_at") else ("-", "-")
            self._meter(y, title, f"{r*100:.0f}%", r, self._color(r), f"{left}にリセット · {at}")
            y += self.ROW * self.S
        ex = u.get("extra_usage") or {}
        if ex.get("is_enabled"):
            used = (ex.get("used_credits") or 0) / 10 ** ex.get("decimal_places", 2)
            lim = ex.get("monthly_limit")
            lim = lim / 10 ** ex.get("decimal_places", 2) if lim else None
            r = used / lim if lim else 0
            self._meter(y, "💳 使用クレジット", f"${used:.2f}" + (f" / ${lim:.0f}" if lim else ""),
                        r, self._color(r), "プラン上限を超えた分に使用" + ("" if lim else " · 上限なし"))
            y += self.ROW * self.S
        cl = u.get("iguana_necktie")  # クラウドセッションクレジット
        if cl and cl.get("limit_dollars"):
            rest, lim = cl.get("remaining_dollars") or 0, cl["limit_dollars"]
            exp = self._until(cl["resets_at"])[1] if cl.get("resets_at") else "-"
            self._meter(y, "☁ クラウドセッションクレジット", f"残り ${rest:.0f} / ${lim:.0f}",
                        rest / lim, "#6fa8dc", f"{exp} に期限切れ")
            y += self.ROW * self.S
        self.cv.config(height=max(y, 40))  # 最後の行の小さい文字まで入る高さ
        self._place()


if __name__ == "__main__":
    App().root.mainloop()
