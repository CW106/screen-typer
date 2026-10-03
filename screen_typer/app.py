"""ScreenTyper — 框一塊螢幕 → OCR 認字 → 模擬鍵盤打出來（速度可調成像真人）。"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk

from . import keyboard as kb
from .i18n import ENGLISH_UI, tr
from .ocr import (assemble, available_languages, enable_dpi_awareness, grab,
                  ocr_image, prepare)
from .region import pick_region

CONFIG = os.path.join(os.path.expanduser("~"), ".screen_typer.json")
CAPTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "captures")
FONT = ("Segoe UI" if ENGLISH_UI else "Microsoft JhengHei UI", 10)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.bbox: tuple[int, int, int, int] | None = None
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.watching = threading.Event()
        self.last_typed = ""

        root.title(tr("ScreenTyper — 螢幕辨識自動打字", "ScreenTyper — OCR Auto Typing"))
        root.geometry("980x660" if ENGLISH_UI else "780x660")
        root.minsize(900 if ENGLISH_UI else 700, 580)

        self._build()
        self._load_cfg()
        self._start_hotkeys()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = dict(padx=8, pady=4)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text=tr("① 選取區域 (F9)", "① Select area (F9)"), command=self.choose_region).pack(side="left")
        ttk.Button(bar, text=tr("② 辨識 (F10)", "② Recognize (F10)"), command=self.do_ocr).pack(side="left", padx=6)
        ttk.Button(bar, text=tr("③ 開始打字 (F11)", "③ Start typing (F11)"), command=self.start_typing).pack(side="left")
        ttk.Button(bar, text=tr("停止 (Esc)", "Stop (Esc)"), command=self.stop).pack(side="left", padx=6)

        self.lbl_region = ttk.Label(bar, text=tr("未選取區域", "No area selected"), font=FONT, foreground="#888")
        self.lbl_region.pack(side="right")

        o = ttk.LabelFrame(self.root, text=tr(" 辨識 ", " Recognition "))
        o.pack(fill="x", **pad)
        ttk.Label(o, text=tr("語言", "Language"), font=FONT).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.var_lang = tk.StringVar()
        self.cmb_lang = ttk.Combobox(o, textvariable=self.var_lang, width=14, state="readonly")
        self.cmb_lang.grid(row=0, column=1, padx=4)
        ttk.Label(o, text=tr("放大倍率", "Scale"), font=FONT).grid(row=0, column=2, padx=(16, 4))
        self.var_scale = tk.DoubleVar(value=2.0)
        ttk.Spinbox(o, from_=1.0, to=4.0, increment=0.5, width=5,
                    textvariable=self.var_scale).grid(row=0, column=3)
        self.var_bin = tk.BooleanVar(value=False)
        ttk.Checkbutton(o, text=tr("高對比（深色底或淡字時勾）", "High contrast (dark background or faint text)"), variable=self.var_bin
                        ).grid(row=0, column=4, padx=12)
        # 打字測驗的題目是連續文字，換行只是排版。若照著按 Enter 會把測驗打壞。
        self.var_oneline = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text=tr("換行併成空格（打字測驗要勾）", "Join lines with spaces (for typing tests)"), variable=self.var_oneline,
                        command=self._reflow).grid(row=1, column=0, columnspan=3,
                                                   sticky="w", padx=6, pady=(0, 6))
        # 游標反白的那個字若不還原，會被認錯（實測 Pets -> e ts）
        self.var_unhl = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text=tr("還原反白選取的字", "Restore highlighted text"), variable=self.var_unhl
                        ).grid(row=1, column=3, columnspan=2, sticky="w", padx=6, pady=(0, 6))
        # 打字測驗的畫面會隨著你打字往上捲，藍色反白標的就是下一個要打的字
        self.var_from_cursor = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text=tr("從游標反白處開始打（前面打過的跳過）", "Start at highlighted cursor (skip typed text)"),
                        variable=self.var_from_cursor, command=self._reflow
                        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))
        # 畫面邊緣被截斷的字、或題目還沒載入的佔位方塊，照著打就是錯字
        self.var_trim_tail = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text=tr("不打最後一個未完成的字", "Skip the last incomplete word"), variable=self.var_trim_tail,
                        command=self._update_estimate
                        ).grid(row=2, column=3, columnspan=2, sticky="w", padx=6, pady=(0, 6))

        s = ttk.LabelFrame(self.root, text=tr(" 打字節奏 ", " Typing rhythm "))
        s.pack(fill="x", **pad)
        self.var_wpm = tk.DoubleVar(value=160)
        self.var_acc = tk.DoubleVar(value=95)
        self.var_jit = tk.DoubleVar(value=0.35)
        self.var_typo = tk.DoubleVar(value=0.0)
        self.var_delay = tk.DoubleVar(value=3.0)
        self._slider(s, 0, tr("速度 WPM（含停頓的實際平均）", "Speed WPM (average incl. pauses)"), self.var_wpm, 150, 500, "{:.0f}")
        self._slider(s, 1, tr("正確率 %（錯的不修正）", "Accuracy % (uncorrected errors)"), self.var_acc, 85, 100, "{:.1f}")
        self._slider(s, 2, tr("節奏隨機度", "Rhythm variation"), self.var_jit, 0, 1.0, "{:.2f}")
        self._slider(s, 3, tr("手誤後自己退格改掉的機率", "Chance of correcting a typo"), self.var_typo, 0, 0.08, "{:.3f}")
        self._slider(s, 4, tr("開始前等待（秒）", "Start delay (seconds)"), self.var_delay, 0, 15, "{:.1f}")

        w = ttk.Frame(s)
        w.grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(2, 8))
        self.var_watch = tk.BooleanVar(value=False)
        ttk.Checkbutton(w, text=tr("自動監看：區域一出現新文字就自動打出來", "Auto-watch: type new text when it appears"),
                        variable=self.var_watch, command=self.toggle_watch).pack(side="left")
        ttk.Label(w, text=tr("每", "Every"), font=FONT).pack(side="left", padx=(12, 2))
        self.var_interval = tk.DoubleVar(value=2.0)
        ttk.Spinbox(w, from_=0.5, to=30, increment=0.5, width=5,
                    textvariable=self.var_interval).pack(side="left")
        ttk.Label(w, text=tr("秒掃描一次", "seconds"), font=FONT).pack(side="left", padx=2)

        t = ttk.LabelFrame(self.root, text=tr(" 要打出來的文字（可直接編輯） ", " Text to type (editable) "))
        t.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(t, wrap="word", font=("Microsoft JhengHei UI", 11),
                           undo=True, height=10)
        sb = ttk.Scrollbar(t, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt.tag_configure("done", foreground="#9aa0a6")
        self.txt.bind("<<Modified>>", self._on_text_change)

        self.status = ttk.Label(self.root, text=tr("就緒", "Ready"), font=FONT, anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 4))
        self.prog = ttk.Progressbar(self.root, mode="determinate")
        self.prog.pack(fill="x", padx=12, pady=(0, 10))

    def _slider(self, parent, row, label, var, lo, hi, fmt):
        ttk.Label(parent, text=label, font=FONT).grid(row=row, column=0, sticky="w",
                                                      padx=6, pady=3)
        val = ttk.Label(parent, text=fmt.format(var.get()), font=FONT, width=6)

        def on(_=None):
            val.config(text=fmt.format(var.get()))
            self._update_estimate()

        ttk.Scale(parent, from_=lo, to=hi, variable=var, command=on,
                  length=430).grid(row=row, column=1, sticky="we", padx=4)
        val.grid(row=row, column=2, sticky="w")
        parent.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------ 動作
    def profile(self) -> kb.Profile:
        return kb.Profile(
            wpm=max(150.0, self.var_wpm.get()),
            accuracy=self.var_acc.get() / 100.0,
            jitter=self.var_jit.get(),
            typo_rate=self.var_typo.get(),
            start_delay=self.var_delay.get(),
        )

    def choose_region(self):
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)
        box = pick_region(self.root)
        self.root.deiconify()
        self.root.lift()
        if box:
            self.bbox = box
            self.lbl_region.config(
                text=tr(f"區域 {box[2] - box[0]}x{box[3] - box[1]} @ ({box[0]},{box[1]})",
                        f"Area {box[2] - box[0]}x{box[3] - box[1]} @ ({box[0]},{box[1]})"),
                foreground="#1a7f37")
            self.do_ocr()
        else:
            self.set_status(tr("已取消選取", "Selection cancelled"))

    def do_ocr(self):
        if not self.bbox:
            self.set_status(tr("請先選取區域", "Select an area first"))
            return
        self.set_status(tr("辨識中…", "Recognizing…"))
        self.root.update_idletasks()
        try:
            raw = grab(self.bbox)
            self._save_capture(raw)
            img, box = prepare(raw, self.var_scale.get(), self.var_bin.get(),
                               self.var_unhl.get())
            res = ocr_image(img, self.var_lang.get(), cursor_box=box, sep=self._sep())
        except Exception as e:                       # noqa: BLE001
            self.set_status(tr(f"擷取失敗：{e}", f"Capture failed: {e}"))
            return
        if not res.ok:
            self.set_status(tr(f"辨識失敗：{res.error}", f"Recognition failed: {res.error}"))
            return
        self._last_words, self._last_box = res.words, box
        self._show(res.lines, res.text, res.cursor_index)
        if not res.text:
            self.set_status(tr("沒有辨識到文字 — 試著提高放大倍率，或勾『高對比』。",
                               "No text found — increase Scale or enable High contrast."))
        elif res.cursor_index:
            self.set_status(tr(f"辨識完成（{res.lang}）— 前面 {res.cursor_index} 個字已經打過，會跳過",
                               f"Recognition complete ({res.lang}) — skipping {res.cursor_index} already typed characters"))
        else:
            self.set_status(tr(f"辨識完成（{res.lang}）", f"Recognition complete ({res.lang})"))

    def _save_capture(self, img, keep: int = 20) -> None:
        """把原始擷取存到 captures/，辨識有問題時可以直接看到當時抓了什麼。"""
        try:
            os.makedirs(CAPTURES, exist_ok=True)
            img.save(os.path.join(CAPTURES,
                                  datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"))
            files = sorted(f for f in os.listdir(CAPTURES) if f.endswith(".png"))
            for old in files[:-keep]:
                os.remove(os.path.join(CAPTURES, old))
        except Exception:                            # noqa: BLE001
            pass

    def _sep(self) -> str:
        return " " if self.var_oneline.get() else chr(10)

    def _show(self, lines, text, cursor_index):
        """把文字放進框裡，已經打過的那一段標成灰色，並記下開始打字的位置。"""
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", text)
        start = "1.0"
        if self.var_from_cursor.get() and cursor_index:
            start = "1.0 + %d chars" % cursor_index
            self.txt.tag_add("done", "1.0", start)
        self.txt.mark_set("typestart", start)
        self.txt.mark_gravity("typestart", "left")
        self._update_estimate()

    def _pending(self) -> str:
        """真正要打出去的那一段（游標之後，必要時切掉結尾沒打完的字）。"""
        try:
            text = self.txt.get("typestart", "end-1c")
        except tk.TclError:
            text = self.txt.get("1.0", "end-1c")
        return self._trim_tail(text)

    def _trim_tail(self, text: str) -> str:
        """切掉結尾那個可能被截斷的字。整段只有一個字時就不動它。"""
        if not self.var_trim_tail.get():
            return text
        stripped = text.rstrip()
        cut = max(stripped.rfind(" "), stripped.rfind(chr(10)))
        return stripped[:cut + 1] if cut > 0 else text

    def _reflow(self, _=None):
        """切換選項時用上一次的辨識結果重組，不用再跑一次 OCR。"""
        words = getattr(self, "_last_words", None)
        if words:
            lines, text, ci = assemble(words, self._sep(), getattr(self, "_last_box", None))
            self._show(lines, text, ci)

    def start_typing(self):
        if self.worker and self.worker.is_alive():
            self.set_status(tr("正在輸入中…（Esc 可停止）", "Already typing… (Esc to stop)"))
            return
        text = self._pending()
        if not text.strip():
            self.set_status(tr("沒有文字可以打", "No text to type"))
            return
        self.stop_flag.clear()
        self.worker = threading.Thread(target=self._type_job,
                                       args=(text, self.profile()), daemon=True)
        self.worker.start()

    def _type_job(self, text: str, p: kb.Profile):
        total = kb.plan_for(text, p)[1]["keys"]
        self.root.after(0, lambda: self.prog.config(maximum=total, value=0))
        left = p.start_delay
        while left > 0 and not self.stop_flag.is_set():
            self.set_status_async(tr(f"{left:.0f} 秒後開始 — 現在切到你要輸入的視窗（Esc 取消）",
                                     f"Starting in {left:.0f} seconds — switch to the target window (Esc to cancel)"))
            time.sleep(min(0.25, left))
            left -= 0.25
            if kb.esc_pressed():
                self.stop_flag.set()
        if self.stop_flag.is_set():
            self.set_status_async(tr("已取消", "Cancelled"))
            return

        t0 = time.perf_counter()

        def prog(done, tot):
            self.root.after(0, lambda: self.prog.config(value=done))
            if done % 8 == 0 or done == tot:
                el = time.perf_counter() - t0
                self.set_status_async(tr(
                    f"輸入中 {done}/{tot}　{done / max(el, 0.001) * 12:.0f} WPM　（Esc 停止）",
                    f"Typing {done}/{tot}  {done / max(el, 0.001) * 12:.0f} WPM  (Esc to stop)"))

        done = kb.type_text(text, p, should_stop=self.stop_flag.is_set, on_progress=prog)
        el = time.perf_counter() - t0
        head = tr("完成", "Complete") if done >= total else tr("已停止", "Stopped")
        self.set_status_async(tr(
            f"{head}：送出 {done}/{total} 鍵，{el:.1f} 秒（實際 {done / max(el, 0.001) * 12:.0f} WPM，設定正確率 {p.accuracy * 100:.1f}%）",
            f"{head}: sent {done}/{total} keys in {el:.1f} seconds (actual {done / max(el, 0.001) * 12:.0f} WPM; accuracy setting {p.accuracy * 100:.1f}%)"))

    def stop(self):
        self.stop_flag.set()
        if self.var_watch.get():
            self.var_watch.set(False)
            self.watching.clear()
        self.set_status(tr("已停止", "Stopped"))

    # ------------------------------------------------------------- 自動監看
    def toggle_watch(self):
        if self.var_watch.get():
            if not self.bbox:
                self.var_watch.set(False)
                self.set_status(tr("請先選取區域", "Select an area first"))
                return
            self.watching.set()
            threading.Thread(target=self._watch_job, daemon=True).start()
            self.set_status(tr("監看中：偵測到新文字就會自動打出來（Esc 停止）",
                               "Watching: new text will be typed automatically (Esc to stop)"))
        else:
            self.watching.clear()

    def _watch_job(self):
        prev = ""
        while self.watching.is_set():
            if kb.esc_pressed():
                self.root.after(0, self.stop)
                return
            res, box = None, None
            try:
                raw = grab(self.bbox)
                img, box = prepare(raw, self.var_scale.get(), self.var_bin.get(),
                                   self.var_unhl.get())
                res = ocr_image(img, self.var_lang.get(), cursor_box=box, sep=self._sep())
                if res.ok and self.var_from_cursor.get() and res.cursor_index:
                    cur = res.text[res.cursor_index:]
                else:
                    cur = res.text if res.ok else ""
                cur = self._trim_tail(cur.strip())
            except Exception:                        # noqa: BLE001
                cur = ""
            # 連續兩次辨識結果相同才算畫面穩定，避免打到只顯示一半的字
            if cur and cur == prev and cur != self.last_typed:
                self.last_typed = cur
                self._last_words, self._last_box = res.words, box
                self.root.after(0, lambda t=cur: (self.txt.delete("1.0", "end"),
                                                  self.txt.insert("1.0", t),
                                                  self.txt.mark_set("typestart", "1.0")))
                self.stop_flag.clear()
                self._type_job(cur, self.profile())
            prev = cur
            t_end = time.time() + max(0.5, self.var_interval.get())
            while time.time() < t_end and self.watching.is_set():
                time.sleep(0.1)

    # --------------------------------------------------------------- 熱鍵
    def _start_hotkeys(self):
        vks = {"F9": 0x78, "F10": 0x79, "F11": 0x7A}
        actions = {"F9": self.choose_region, "F10": self.do_ocr, "F11": self.start_typing}

        def loop():
            state = {k: False for k in vks}
            while True:
                for name, vk in vks.items():
                    down = bool(kb._user32.GetAsyncKeyState(vk) & 0x8000)
                    if down and not state[name]:
                        self.root.after(0, actions[name])
                    state[name] = down
                time.sleep(0.04)

        threading.Thread(target=loop, daemon=True).start()

    # --------------------------------------------------------------- 雜項
    def set_status(self, msg: str):
        self.status.config(text=msg)

    def set_status_async(self, msg: str):
        self.root.after(0, lambda: self.status.config(text=msg))

    def _on_text_change(self, _=None):
        self.txt.edit_modified(False)
        self._update_estimate()

    def _update_estimate(self):
        text = self._pending()
        if not text.strip():
            return
        p = self.profile()
        est = kb.estimate(text, p)
        base = self.status.cget("text").split("　│")[0]
        warn = tr(f"　⚠ 超過硬體上限 {est['max_wpm']:.0f} WPM",
                  f"  ⚠ Above hardware limit {est['max_wpm']:.0f} WPM") if est["capped"] else ""
        self.status.config(
            text=tr(f"{base}　│ {len(text)} 字，預估 {est['seconds']:.1f} 秒 "
                    f"= {est['wpm']:.0f} WPM / {est['cpm']:.0f} 字每分，"
                    f"正確率 {est['accuracy'] * 100:.1f}%{warn}",
                    f"{base}　│ {len(text)} chars, ~{est['seconds']:.1f}s "
                    f"= {est['wpm']:.0f} WPM / {est['cpm']:.0f} CPM, "
                    f"accuracy {est['accuracy'] * 100:.1f}%{warn}"))

    def _load_cfg(self):
        langs = available_languages() or [""]
        self.cmb_lang["values"] = langs
        cfg = {}
        if os.path.exists(CONFIG):
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:                        # noqa: BLE001
                cfg = {}
        pref = cfg.get("lang")
        self.var_lang.set(pref if pref in langs else
                          next((x for x in langs if x.startswith("zh")), langs[0]))
        for key, var in (("wpm", self.var_wpm), ("acc", self.var_acc), ("jitter", self.var_jit),
                         ("typo", self.var_typo), ("delay", self.var_delay),
                         ("scale", self.var_scale), ("interval", self.var_interval)):
            if key in cfg:
                var.set(cfg[key])
        for key, var in (("oneline", self.var_oneline), ("unhl", self.var_unhl),
                         ("from_cursor", self.var_from_cursor),
                         ("trim_tail", self.var_trim_tail)):
            if key in cfg:
                var.set(bool(cfg[key]))
        if cfg.get("bbox"):
            b = tuple(cfg["bbox"])
            self.bbox = b
            self.lbl_region.config(text=tr(f"上次區域 {b[2] - b[0]}x{b[3] - b[1]}",
                                            f"Last area {b[2] - b[0]}x{b[3] - b[1]}"),
                                   foreground="#1a7f37")

    def _save_cfg(self):
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump({"lang": self.var_lang.get(), "wpm": self.var_wpm.get(),
                           "acc": self.var_acc.get(),
                           "oneline": self.var_oneline.get(),
                           "unhl": self.var_unhl.get(),
                           "from_cursor": self.var_from_cursor.get(),
                           "trim_tail": self.var_trim_tail.get(),
                           "jitter": self.var_jit.get(), "typo": self.var_typo.get(),
                           "delay": self.var_delay.get(), "scale": self.var_scale.get(),
                           "interval": self.var_interval.get(), "bbox": self.bbox},
                          f, ensure_ascii=False)
        except Exception:                            # noqa: BLE001
            pass

    def on_close(self):
        self.watching.clear()
        self.stop_flag.set()
        self._save_cfg()
        self.root.destroy()


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
