"""SendInput 模擬鍵盤 + 擬真打字節奏。純 ctypes，不需要任何套件。"""
from __future__ import annotations

import copy
import ctypes
import ctypes.wintypes as wt
import random
import time
from dataclasses import dataclass, field

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE = 0x08, 0x09, 0x0D, 0x1B


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_user32.SendInput.restype = wt.UINT


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _ki(vk=0, scan=0, flags=0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                              time=0, dwExtraInfo=0)))


def tap_vk(vk: int, hold: float = 0.012) -> None:
    """按一個虛擬鍵（Enter / Tab / Backspace ...）。"""
    scan = _user32.MapVirtualKeyW(vk, 0)
    _send([_ki(vk, scan, 0)])
    if hold:
        time.sleep(hold)
    _send([_ki(vk, scan, KEYEVENTF_KEYUP)])


def type_char(ch: str, hold: float = 0.012) -> None:
    """用 Unicode 注入打一個字元，中文 / emoji / 任何語言都可以。"""
    if ch == "\n":
        tap_vk(VK_RETURN, hold)
        return
    if ch == "\t":
        tap_vk(VK_TAB, hold)
        return
    if ch == "\r":
        return
    code = ord(ch)
    if code > 0xFFFF:  # 代理對（emoji 等）
        code -= 0x10000
        units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
    else:
        units = [code]
    _send([_ki(0, u, KEYEVENTF_UNICODE) for u in units])
    if hold:
        time.sleep(hold)
    _send([_ki(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for u in units])


def esc_pressed() -> bool:
    return bool(_user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


# --------------------------------------------------------------- 擬真節奏模型
_QWERTY_NEIGHBORS = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr", "f": "drtgvc",
    "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn", "k": "jiolm", "l": "kop",
    "m": "njk", "n": "bhjm", "o": "iklp", "p": "ol", "q": "wa", "r": "edft",
    "s": "awedxz", "t": "rfgy", "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc",
    "y": "tghu", "z": "asx", "1": "2q", "2": "13w", "3": "24e", "4": "35r", "5": "46t",
    "6": "57y", "7": "68u", "8": "79i", "9": "80o", "0": "9p",
}
_PUNCT = set("，。、；：！？…）】」』.,;:!?)]}\"'")
_PAUSE_AFTER = set("　 ，。、；：！？")

# 每個按鍵送 SendInput 的固定開銷（實測約 4 ms，不含 hold）
_PER_CHAR_OVERHEAD = 0.004


@dataclass
class Profile:
    """打字節奏設定。時間單位皆為秒。"""
    wpm: float = 160.0           # 目標速度（5 字元 = 1 word）
    sustain: bool = True         # True = wpm 指「含所有停頓的實際平均速度」
    accuracy: float = 0.95       # 最後留在畫面上的正確率（0.95 = 5% 錯字不修正）
    jitter: float = 0.35         # 每鍵間隔的隨機程度（lognormal sigma），0 = 機器人
    drift: float = 0.22          # 長時間的快慢起伏
    punct_pause: float = 2.6     # 標點後停頓倍率
    newline_pause: float = 5.0   # 換行後停頓倍率
    think_prob: float = 0.018    # 每個字有多少機率突然「想一下」
    think_min: float = 0.35
    think_max: float = 1.60
    typo_rate: float = 0.0       # 打錯後自己退格改掉的機率（不影響最終正確率）
    start_delay: float = 3.0     # 按下開始後等幾秒（讓你切到目標視窗）
    hold: float = 0.012          # 按鍵按住的時間
    seed: int | None = None
    _rng: random.Random = field(default=None, repr=False, compare=False)

    def rng(self) -> random.Random:
        if self._rng is None:
            self._rng = random.Random(self.seed)
        return self._rng

    @property
    def base_delay(self) -> float:
        return 60.0 / (max(1.0, self.wpm) * 5.0)

    @property
    def key_cost(self) -> float:
        """送一個按鍵最少要花的時間，決定了速度上限。"""
        return self.hold + _PER_CHAR_OVERHEAD


def max_wpm(p: Profile) -> float:
    """受 SendInput 開銷限制，實際能達到的最高速度。"""
    return 60.0 / (p.key_cost * 5.0)


def _char_factor(ch: str, prev: str) -> float:
    if ch == " ":
        return 0.85
    if ch == "\n":
        return 1.0
    if ch.isdigit():
        return 1.25
    if ch.isupper():
        return 1.30          # 要按 Shift
    if ch in _PUNCT:
        return 1.20
    if prev and ch == prev:
        return 0.80          # 連續同字較快
    if ord(ch) > 0x2E80:
        return 1.12          # 中文
    return 1.0


def iter_delays(text: str, p: Profile):
    """產生 (字元, 該字元打完後要等多久)。可單獨拿來檢查節奏是否自然。"""
    rng = p.rng()
    base = p.base_delay
    drift = 1.0
    prev = ""
    for ch in text:
        # 緩慢漂移的速度倍率，模擬人的忽快忽慢
        drift += rng.gauss(0, p.drift * 0.18)
        drift += (1.0 - drift) * 0.05
        drift = min(max(drift, 0.55), 1.8)

        d = base * drift * _char_factor(ch, prev)
        if p.jitter > 0:
            d *= rng.lognormvariate(0, p.jitter)
        if ch == "\n":
            d *= p.newline_pause
        elif ch in _PAUSE_AFTER:
            d *= p.punct_pause
        if rng.random() < p.think_prob:
            d += rng.uniform(p.think_min, p.think_max)
        yield ch, max(0.001, d)
        prev = ch


def _wrong_char(ch: str, text: str, rng: random.Random) -> str | None:
    """挑一個「打錯」會打成的字。挑不到就回 None（該字照打）。"""
    low = ch.lower()
    if low in _QWERTY_NEIGHBORS:
        w = rng.choice(_QWERTY_NEIGHBORS[low])
        return w.upper() if ch.isupper() else w
    if ord(ch) > 0x2E80 and ch not in _PUNCT:
        # 中文：從同一篇文字裡挑另一個字，最像選錯字
        pool = [c for c in set(text) if ord(c) > 0x2E80 and c != ch and c not in _PUNCT]
        if pool:
            return rng.choice(sorted(pool))
    return None


# 事件種類：("key", 字元) 打一個字 / ("back", None) 退格
def build_plan(text: str, p: Profile) -> tuple[list[tuple[str, str | None, float]], dict]:
    """把文字展開成實際的按鍵序列（含錯字、退格），並依 sustain 調整間隔。

    回傳 (events, info)。events = [(kind, char, delay_after), ...]
    info 帶 estimated / target / accuracy / capped 等資訊。
    """
    rng = p.rng()
    events: list[tuple[str, str | None, float]] = []
    typed_ok = 0            # 最後正確留在畫面上的字數

    # 先抽出「剛好」要打錯幾個字、錯在哪幾個位置，正確率才會精準
    # （每個字獨立擲骰的話，短文章的誤差會到好幾個百分點）
    acc = max(0.0, min(1.0, p.accuracy))
    n_err = int(round((1.0 - acc) * len(text)))
    eligible = [i for i, c in enumerate(text) if _wrong_char(c, text, random.Random(0)) is not None]
    err_at = set(rng.sample(eligible, min(n_err, len(eligible))))

    for i, (ch, delay) in enumerate(iter_delays(text, p)):
        # 1) 不修正的錯字 —— 直接決定最終正確率
        if i in err_at:
            wrong = _wrong_char(ch, text, rng)
            if wrong is not None:
                events.append(("key", wrong, delay))
                continue
        # 2) 打錯了又自己退格改掉（最終仍然正確）
        if p.typo_rate > 0 and rng.random() < p.typo_rate:
            wrong = _wrong_char(ch, text, rng)
            if wrong is not None:
                events.append(("key", wrong, delay * rng.uniform(0.8, 1.6)))
                events.append(("back", None, rng.uniform(0.05, 0.18)))
        events.append(("key", ch, delay))
        typed_ok += 1

    # ---- 讓「實際平均速度」等於設定的 WPM（含所有停頓與退格）
    n_keys = len(events)
    floor = n_keys * p.key_cost                       # 硬體下限
    raw = sum(e[2] for e in events) + floor
    target = len(text) / (max(1.0, p.wpm) * 5.0 / 60.0)
    capped = False
    if p.sustain:
        if target <= floor:
            target, capped = floor, True
        scale = max(0.0, (target - floor)) / max(1e-9, raw - floor)
        events = [(k, c, d * scale) for k, c, d in events]

    info = {
        "keys": n_keys,
        "chars": len(text),
        "correct": typed_ok,
        "accuracy": typed_ok / max(1, len(text)),
        "estimated": sum(e[2] for e in events) + floor,
        "target": target,
        "capped": capped,
        "max_wpm": max_wpm(p),
    }
    return events, info


def plan_for(text: str, p: Profile, seed_if_none: int = 12345):
    """用固定亂數種子做預估，不影響真正輸入時的隨機性。"""
    q = copy.copy(p)
    q._rng = random.Random(p.seed if p.seed is not None else seed_if_none)
    return build_plan(text, q)


def estimate_seconds(text: str, p: Profile) -> float:
    """不實際輸入，估算大概要打多久（給 UI 顯示用）。"""
    return plan_for(text, p)[1]["estimated"]


def estimate(text: str, p: Profile) -> dict:
    """回傳預估資訊：秒數、實際 WPM、正確率、是否被硬體上限卡住。"""
    info = plan_for(text, p)[1]
    secs = max(info["estimated"], 1e-9)
    info["wpm"] = len(text) / 5.0 / (secs / 60.0)
    info["cpm"] = len(text) / (secs / 60.0)
    info["seconds"] = secs
    return info


def type_text(text: str, p: Profile | None = None, *,
              should_stop=None, on_progress=None) -> int:
    """把 text 打出去。回傳實際送出的按鍵數。
    should_stop: callable -> bool，回 True 就中止（按 ESC 也會中止）。
    on_progress: callable(已送出, 總數)。"""
    p = p or Profile()
    events, _ = build_plan(text, p)
    total = len(events)
    done = 0

    def stopped() -> bool:
        return esc_pressed() or bool(should_stop and should_stop())

    for kind, ch, delay in events:
        if stopped():
            break
        if kind == "back":
            tap_vk(VK_BACK, p.hold)
        else:
            type_char(ch, p.hold)
        done += 1
        if on_progress:
            on_progress(done, total)
        end = time.perf_counter() + delay
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                break
            time.sleep(min(left, 0.03))
            if stopped():
                return done
    return done
