"""全螢幕框選（支援多螢幕、負座標）。"""
from __future__ import annotations

import tkinter as tk

from .ocr import virtual_screen
from .i18n import tr


def pick_region(root: tk.Misc | None = None) -> tuple[int, int, int, int] | None:
    """讓使用者拖曳出一塊區域。回傳 (left, top, right, bottom) 或 None（取消）。"""
    vx, vy, vw, vh = virtual_screen()
    owns_root = root is None
    if owns_root:
        root = tk.Tk()
        root.withdraw()

    top = tk.Toplevel(root)
    top.overrideredirect(True)
    top.geometry(f"{vw}x{vh}+{vx}+{vy}")
    top.attributes("-topmost", True)
    top.attributes("-alpha", 0.30)
    top.configure(bg="#101010")
    top.config(cursor="crosshair")

    cv = tk.Canvas(top, bg="#101010", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    cv.create_text(vw // 2, 40, text=tr("拖曳選取要辨識的區域　·　Esc 取消",
                                       "Drag to select the OCR area · Esc to cancel"),
                   fill="#ffffff", font=("Microsoft JhengHei UI", 16))

    state = {"x0": 0, "y0": 0, "rect": None, "box": None}

    def on_press(e):
        state["x0"], state["y0"] = e.x, e.y
        if state["rect"]:
            cv.delete(state["rect"])
        state["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y,
                                            outline="#4ea1ff", width=2, fill="#4ea1ff",
                                            stipple="gray25")

    def on_drag(e):
        if state["rect"]:
            cv.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def on_release(e):
        x0, y0 = state["x0"], state["y0"]
        x1, y1 = e.x, e.y
        l, r = sorted((x0, x1))
        t, b = sorted((y0, y1))
        if r - l >= 5 and b - t >= 5:
            state["box"] = (l + vx, t + vy, r + vx, b + vy)
        top.destroy()

    def cancel(_=None):
        state["box"] = None
        top.destroy()

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_drag)
    cv.bind("<ButtonRelease-1>", on_release)
    top.bind("<Escape>", cancel)
    top.focus_force()
    top.grab_set()
    top.wait_window()

    if owns_root:
        root.destroy()
    return state["box"]
