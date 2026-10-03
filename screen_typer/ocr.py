"""螢幕擷取 + Windows 內建 OCR（免安裝任何 OCR 引擎）。"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field

from PIL import Image, ImageGrab, ImageOps

from . import highlight
from .i18n import ENGLISH_UI, tr

_HERE = os.path.dirname(os.path.abspath(__file__))
_PS1 = os.path.join(_HERE, "win_ocr.ps1")
_POWERSHELL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
)


# --------------------------------------------------------------------------- DPI
def enable_dpi_awareness() -> None:
    """讓 tkinter 座標 == 實體像素，否則在縮放 125%/150% 的螢幕會框錯位置。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, width, height) 涵蓋所有螢幕的虛擬桌面。"""
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
            u.GetSystemMetrics(78), u.GetSystemMetrics(79))


# --------------------------------------------------------------------------- 擷取
def grab(bbox: tuple[int, int, int, int]) -> Image.Image:
    """bbox = (left, top, right, bottom)，虛擬桌面絕對座標（可為負）。"""
    return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")



# --------------------------------------------------------------------------- 反白還原
def _highlight_boxes(img, paint: bool):
    boxes = []
    out = highlight.normalize(img, boxes_out=boxes, paint=paint)
    return out, boxes


def find_highlight(img: Image.Image):
    """找出畫面上的反白色塊。打字測驗的游標就是它，標的是下一個要打的字。

    有多塊時取閱讀順序最前面的那一塊（最上面那一行、該行最左邊），
    因為那裡就是該從哪裡開始打。
    """
    _, boxes = _highlight_boxes(img, paint=False)
    if not boxes:
        return None
    top = min(b[1] for b in boxes)
    same_line = [b for b in boxes if b[1] - top <= (b[3] - b[1]) * 0.6]
    return min(same_line, key=lambda b: b[0])


def remove_highlights(img: Image.Image) -> Image.Image:
    """把反白／選取色塊還原成和周圍一樣的深字淺底。

    Windows OCR 對「彩色底 + 白字」這種局部極性相反的小區塊容易漏字或認錯
    （實測打字測驗的游標會害 Pets 被認成 e ts）。判定用結構特徵而不是顏色：
    色塊要夠填滿、夠方正、裡面要有極性相反的文字，判不出來就原樣返回。
    舊版用飽和度判斷，在 55 張攻擊樣本裡有 29 張比完全不處理還差。
    """
    return highlight.normalize(img)


def preprocess(img: Image.Image, scale: float = 2.0, binarize: bool = False,
               unhighlight: bool = True) -> Image.Image:
    """放大 → 還原反白 → 可選二值化。Windows OCR 對小字很敏感，放大後準確率明顯提升。"""
    if scale and scale != 1.0:
        w, h = img.size
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    if unhighlight:
        img = remove_highlights(img)
    if binarize:
        g = ImageOps.autocontrast(ImageOps.grayscale(img))
        img = g.point(lambda p: 255 if p > 140 else 0).convert("RGB")
    return img


# --------------------------------------------------------------------------- OCR
@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def contains_center_of(self, other: "Word") -> bool:
        return (self.x <= other.cx <= self.x + self.w
                and self.y <= other.cy <= self.y + self.h)


@dataclass
class OcrResult:
    ok: bool
    text: str = ""
    lines: list[str] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    lang: str = ""
    error: str = ""
    cursor_index: int | None = None      # 反白游標對應到 text 的第幾個字


_ERR_HINTS = {
    "WINRT_INIT_FAILED": tr("無法載入 Windows OCR 介面（WinRT）。請確認是 Windows 10/11。",
                            "Could not load Windows OCR (WinRT). Check that you use Windows 10 or 11."),
    "NO_ENGINE_FOR_LANG": tr("這個語言沒有安裝 OCR 語言包。請到「設定 → 時間與語言 → 語言與地區」"
                             "點該語言的「語言選項」，加裝「光學字元辨識」。",
                             "OCR support is not installed for this language. Open Settings > Time & language > Language & region > Language options and install Optical character recognition."),
    "IMAGE_NOT_FOUND": tr("暫存圖檔不見了（可能被防毒軟體攔截）。",
                          "Temporary image is missing (possibly blocked by antivirus)."),
    "OCR_FAILED": tr("OCR 執行失敗。", "OCR failed."),
}


def _explain(err: str) -> str:
    for code, hint in _ERR_HINTS.items():
        if err.startswith(code):
            detail = err[len(code):].lstrip(": ").strip()
            if not detail:
                return hint
            return f"{hint} ({detail})" if ENGLISH_UI else f"{hint}（{detail}）"
    return err


def _run_ps(args: list[str]) -> dict:
    cmd = [_POWERSHELL, "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-File", _PS1] + args
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    p = subprocess.run(cmd, capture_output=True, creationflags=flags)
    out = p.stdout.decode("utf-8", "replace").strip()
    if not out:
        err = p.stderr.decode("utf-8", "replace").strip()
        return {"ok": False, "error": err or tr(f"PowerShell 無輸出 (exit {p.returncode})",
                                                  f"No PowerShell output (exit {p.returncode})")}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": tr(f"無法解析 OCR 輸出: {out[:400]}",
                                          f"Could not parse OCR output: {out[:400]}")}


def available_languages() -> list[str]:
    data = _run_ps(["-ImagePath", "x", "-Lang", "?"])
    return list(data.get("languages") or []) if data.get("ok") else []


# --------------------------------------------------------------------------- 排版
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3000 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xFF00 <= o <= 0xFF60 or 0x20000 <= o <= 0x2FA1F)


def join_words(words: list[str]) -> str:
    """Windows OCR 會把中文每個字當成一個 word，用空白接起來會變『你 好 嗎』。
    兩側都是 CJK（或全形標點）時不補空白，其餘照英文規則補一個空白。"""
    out = ""
    for w in words:
        if not w:
            continue
        if out:
            prev, cur = out[-1], w[0]
            if not (_is_cjk(prev) or _is_cjk(cur)):
                out += " "
        out += w
    return out


def _space_threshold(gaps: list[float]) -> float | None:
    """同一行的字距通常只有兩種：單字之間的空格，和被切開的同一個字。

    後者會明顯偏小（實測反白切開的字是 20px，真正的空格是 59~74px）。
    把間距分成兩群，回傳分界；分不出兩群（正常文字就是這樣）時回傳 None，
    代表每個間距都是空格，維持原本的行為。
    """
    pos = sorted(g for g in gaps if g > 0)
    if len(pos) < 3:
        return None
    med = pos[len(pos) // 2]
    small = [g for g in pos if g < med * 0.5]
    if not small:
        return None
    big = [g for g in pos if g >= med * 0.5]
    return (max(small) + min(big)) / 2.0


def _line_text(row: list[Word]) -> str:
    """把一行的字組成文字，依實際間距決定要不要補空白。"""
    row = sorted(row, key=lambda w: w.x)
    if len(row) == 1:
        return row[0].text
    gaps = [b.x - (a.x + a.w) for a, b in zip(row, row[1:])]
    thr = _space_threshold(gaps)
    out = row[0].text
    for gap, w in zip(gaps, row[1:]):
        if not w.text:
            continue
        glue = (_is_cjk(out[-1]) or _is_cjk(w.text[0])) if out else True
        if not glue and thr is not None and gap < thr:
            glue = True                    # 間距太小：同一個字被切開了
        out += ("" if glue else " ") + w.text
    return out


def group_rows(words: list[Word]) -> list[list[Word]]:
    """依 y 座標把字分行，行內依 x 排序。"""
    if not words:
        return []
    heights = sorted(w.h for w in words)
    tol = max(4.0, heights[len(heights) // 2] * 0.6)
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: w.cy):
        if rows and abs(w.cy - rows[-1][0].cy) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w.x) for r in rows]


def words_to_lines(words: list[Word]) -> list[str]:
    return [_line_text(row) for row in group_rows(words)]


def _assemble(rows, sep: str):
    """組成每一行，同時記錄每個字在「整段文字」裡的起始索引。"""
    lines, spans, base = [], [], 0
    for row in rows:
        gaps = [b.x - (a.x + a.w) for a, b in zip(row, row[1:])]
        thr = _space_threshold(gaps)
        line = ""
        for i, w in enumerate(row):
            if not w.text:
                continue
            if line:
                glue = _is_cjk(line[-1]) or _is_cjk(w.text[0])
                if not glue and thr is not None and gaps[i - 1] < thr:
                    glue = True
                line += "" if glue else " "
            spans.append((w, base + len(line)))
            line += w.text
        lines.append(line)
        base += len(line) + len(sep)
    return lines, spans


def _cursor_index(spans, box) -> int | None:
    """把反白色塊的位置換算成「整段文字的第幾個字」。"""
    if not box or not spans:
        return None
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    for w, start in spans:
        if w.y - w.h * 0.4 <= cy <= w.y + w.h * 1.4 and w.x - 2 <= cx <= w.x + w.w + 2:
            cw = w.w / max(1, len(w.text))
            off = int(round((x0 - w.x) / cw)) if cw else 0
            return start + max(0, min(len(w.text), off))
    # 落在空白上：取同一行右邊最近的那個字
    right = [(w, st) for w, st in spans
             if w.y - w.h * 0.4 <= cy <= w.y + w.h * 1.4 and w.x >= x0 - 2]
    if right:
        return min(right, key=lambda t: t[0].x)[1]
    return None


def _parse_words(data: dict) -> list[Word]:
    out: list[Word] = []
    for line in data.get("lines") or []:
        for w in line.get("words") or []:
            if isinstance(w, dict) and w.get("t"):
                out.append(Word(w["t"], int(w.get("x", 0)), int(w.get("y", 0)),
                                int(w.get("w", 0)), int(w.get("h", 0))))
    return out


# --------------------------------------------------------------------------- 主要入口
def _ocr_once(img: Image.Image, lang: str) -> tuple[bool, list[Word], str, str]:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="stype_")
    os.close(fd)
    try:
        img.save(path, "PNG")
        args = ["-ImagePath", path] + (["-Lang", lang] if lang else [])
        data = _run_ps(args)
        if not data.get("ok"):
            return False, [], "", _explain(data.get("error", tr("未知錯誤", "Unknown error")))
        return True, _parse_words(data), data.get("lang", ""), ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def assemble(words: list[Word], sep: str = " ", cursor_box=None):
    """把 OCR 的字組成文字。回傳 (每行, 整段文字, 游標索引)。

    切換「換行併成空格」時可以直接重組，不用再跑一次 OCR。
    """
    raw_lines, spans = _assemble(group_rows(words), sep)
    lines = [unicodedata.normalize("NFC", x) for x in raw_lines]
    return lines, sep.join(lines), _cursor_index(spans, cursor_box)


def ocr_image(img: Image.Image, lang: str = "", handle_inverted: bool = False,
              cursor_box=None, sep: str = " ") -> OcrResult:
    """辨識一張圖。

    handle_inverted=True 時會再辨識一次反相的圖再依座標合併。實測 Windows OCR
    本身就會自動處理黑白極性，所以預設關閉（開了只是慢一倍）。
    """
    ok, words, used_lang, err = _ocr_once(img, lang)
    if not ok:
        return OcrResult(False, error=err)

    if handle_inverted:
        ok2, inv_words, _, _ = _ocr_once(ImageOps.invert(img), lang)
        if ok2:
            for iw in inv_words:
                # 原圖已經認出同一個位置的字就不重複加
                if not any(w.contains_center_of(iw) or iw.contains_center_of(w)
                           for w in words):
                    words.append(iw)

    lines, text, ci = assemble(words, sep, cursor_box)
    return OcrResult(True, text=text, lines=lines, words=words,
                     lang=used_lang, cursor_index=ci)


def prepare(img: Image.Image, scale: float = 2.0, binarize: bool = False,
            unhighlight: bool = True):
    """記下游標反白的位置 → 還原反白 → 放大 → 可選二值化。

    色塊偵測要在放大之後做：偵測靠的是文字帶高與筆畫粗細這類結構量測，
    原尺寸的小字會低於它的下限而整個跳過（實測原尺寸完全偵測不到游標）。
    """
    if scale and scale != 1.0:
        w, h = img.size
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    img, boxes = _highlight_boxes(img, paint=unhighlight)
    box = None
    if boxes:
        top = min(b[1] for b in boxes)
        same = [b for b in boxes if b[1] - top <= (b[3] - b[1]) * 0.6]
        box = min(same, key=lambda b: b[0])
    if binarize:
        g = ImageOps.autocontrast(ImageOps.grayscale(img))
        img = g.point(lambda p: 255 if p > 140 else 0).convert("RGB")
    return img, box


def ocr_region(bbox, lang="", scale=2.0, binarize=False, unhighlight=True,
               handle_inverted=False, sep=" ") -> OcrResult:
    img, box = prepare(grab(bbox), scale, binarize, unhighlight)
    return ocr_image(img, lang, handle_inverted, cursor_box=box, sep=sep)
