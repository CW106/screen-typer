# -*- coding: utf-8 -*-
"""把畫面上「真正的選取反白色塊」還原成和頁面一樣的深字淺底（強化版）。

舊版 strategy_unhl 用「HSV 飽和度 >= 110」當作反白的判準。飽和度是純顏色特徵，
分不出「彩色的底」和「彩色的字」——VS Code Light+ 的關鍵字 #0000FF 和選取底色
#0078D7 的 S 都是 255，所以只要畫面上有語法高亮、超連結、紅色錯誤訊息、彩色
中文標題，整頁文字都會被當成色塊重畫成空心鬼影。這一版改用結構特徵：

  1. 候選底色來自量化調色盤，不看飽和度，所以低飽和、半透明、灰色選取也抓得到；
     頁面底色從畫面「邊框那一圈」決定，整頁被選取時也不會把選取色當成底色。
  2. 真正的反白是**填滿的矩形色塊**：在一個約等於文字帶高的視窗裡，底色的填滿率
     要夠高才算色塊。彩色文字筆畫再密，放到這個尺度的視窗裡也只有兩三成，
     直接被篩掉（實測門檻兩側：彩色文字 0.26~0.42，真正的反白 0.55~1.00）。
  3. 連通區還要通過矩形度、最小高度／寬度、「內部非底色比例」（= 色塊裡字的
     比例，真反白 0.09~0.30，被閉起來的彩色字 0.40 以上）等檢查。
  4. 色塊內部的字用**洞填充**取出，不是寫死的閉運算核心，字多大都不會被黏成
     實心塊，也不會把細筆畫的彩色文字撐大。
  5. 只有「色塊內文字極性和頁面相反」（或底色亮度和頁面差很多）才動手；
     螢光筆那種深字淺底本來就是對的，原樣保留。
  6. 重畫用亮度映射而不是塗兩個平面色，抗鋸齒保留下來，密排中文不會糊成一團。
  7. 前景色用「墨色低分位數」而不是面積眾數，並強制最低對比，畫面被選取一大半
     或旁邊有大片中灰 UI 時也不會把前景估成中灰。
  8. 任何一關過不了就原樣返回——寧可不做，不要亂改。

全部核心大小都是從畫面自己量到的筆畫粗細／文字帶高推出來的比例，沒有寫死的 9。
"""
from __future__ import annotations

from PIL import Image, ImageChops, ImageFilter

# ---- 可調參數（全部是「相對於量到的行高 / 筆畫粗細」的比例，不是絕對像素）----
COLOR_TOL = 30          # 判斷像素是否屬於某個底色的最大單通道色差
DENS_WX = 0.45          # 密度視窗半徑（水平）= 文字帶高 * 這個比例
DENS_WY = 0.40          # 密度視窗半徑（垂直）= 文字帶高 * 這個比例
DENS_THR = 0.46         # 視窗內底色填滿率要多少才算「色塊內部」
CLOSE_RATIO = 0.25      # 封孔用的小閉運算核心 = 文字帶高 * 這個比例
MIN_H_RATIO = 0.70      # 色塊最小高度 = 文字帶高 * 這個比例
MIN_W_RATIO = 0.35      # 色塊最小寬度 = 文字帶高 * 這個比例
RECT_RATIO = 0.80       # 連通區面積 / 外接矩形面積，至少要這麼「方」
GLYPH_LO = 0.02         # 色塊裡非底色（= 字）的比例下限（純色塊沒字就不用救）
GLYPH_HI = 0.38         # 上限（超過就不像「底 + 字」了）
MAX_COVER = 0.90        # 重畫面積超過整張圖這個比例就整張放棄
MIN_CONTRAST = 40       # 色塊裡底與字的亮度差至少要這麼多
PAGE_CONTRAST = 150     # 重畫後頁面前景／背景至少要這麼多對比
PAGE_PCT = 0.10         # 取墨色的第幾分位當前景（越小越深，避免被中灰 UI 拉走）
CORE_FRAC = 0.05        # 連通區裡要有這麼大比例是「色塊內部」才算數
MAX_REGIONS = 40        # 連通區太多 = 誤判，放棄
PALETTE_COLORS = 24     # 量化調色盤大小
RIM_RATIO = 1.0         # 色塊外緣要抹掉的抗鋸齒彩邊寬度 = 筆畫粗細 * 這個比例
RAMP_LO = 0.40          # 亮度映射的起點（相對於「底 -> 字」的距離）
RAMP_HI = 0.60          # 終點；區間縮窄 = 筆畫邊緣變銳利，實測 OCR 明顯偏好
BG_SHIFT = 45           # 極性相同時，底色亮度和頁面背景差這麼多才值得重畫

DEBUG = False


def _dbg(*a):
    if DEBUG:
        print("   [unhl2]", *a)


# ------------------------------------------------------------------ 形態學工具
def _shift(im: Image.Image, dx: int, dy: int, fill: int) -> Image.Image:
    out = Image.new("L", im.size, fill)
    out.paste(im, (dx, dy))
    return out


def _morph1(m: Image.Image, n: int, axis: int, dilate: bool) -> Image.Image:
    """沿單一軸做 n 長度的膨脹／侵蝕，用倍增位移，成本是 log(n) 次全圖運算。"""
    n = int(n)
    if n <= 1:
        return m
    op = ImageChops.lighter if dilate else ImageChops.darker
    fill = 0 if dilate else 255
    r, done, step = m, 1, 1
    while done < n:
        s = min(step, n - done)
        dx, dy = (s, 0) if axis == 0 else (0, s)
        r = op(r, _shift(r, dx, dy, fill))
        done += s
        step *= 2
    off = n // 2
    return _shift(r, -off if axis == 0 else 0, 0 if axis == 0 else -off, fill)


def _morph(m: Image.Image, nx: int, ny: int, dilate: bool) -> Image.Image:
    return _morph1(_morph1(m, nx, 0, dilate), ny, 1, dilate)


def _close(m: Image.Image, nx: int, ny: int) -> Image.Image:
    return _morph(_morph(m, nx, ny, True), nx, ny, False)


def _count(m: Image.Image) -> int:
    return sum(m.histogram()[128:])


def _bin(m: Image.Image, thr: int) -> Image.Image:
    return m.point(lambda v, t=thr: 255 if v >= t else 0)


def _color_mask(img: Image.Image, color, tol: int = COLOR_TOL) -> Image.Image:
    """單通道最大色差 <= tol 的像素。"""
    d = ImageChops.difference(img, Image.new("RGB", img.size, tuple(color)))
    r, g, b = d.split()
    dm = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return dm.point(lambda v, t=tol: 255 if v <= t else 0)


# ------------------------------------------------------------------ 版面量測
def _row_profile(ink: Image.Image) -> list[int]:
    h = ink.size[1]
    return list(ink.resize((1, h), Image.BOX).getdata())


def _stroke(ink: Image.Image) -> float:
    """筆畫粗細 = 水平方向連續墨色長度的中位數（忽略超長的實心色塊）。"""
    w, h = ink.size
    data = ink.tobytes()
    step = max(1, h // 400)
    cap = max(4, int(w * 0.25))
    lens = []
    for y in range(0, h, step):
        row = data[y * w:(y + 1) * w]
        pos = 0
        while True:
            i = row.find(255, pos)
            if i < 0:
                break
            j = row.find(0, i + 1)
            if j < 0:
                j = w
            if j - i <= cap:
                lens.append(j - i)
            pos = j + 1
    if not lens:
        return 2.0
    lens.sort()
    return float(lens[len(lens) // 2])


def _bands(ink: Image.Image) -> tuple[float, float, int]:
    """用水平投影估 (文字帶高, 行距)。"""
    prof = _row_profile(ink)
    bands, start = [], None
    for y, v in enumerate(prof):
        on = v > 3
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start >= 2:
                bands.append((start, y))
            start = None
    if start is not None and len(prof) - start >= 2:
        bands.append((start, len(prof)))
    if not bands:
        return 0.0, 0.0, 0
    heights = sorted(b[1] - b[0] for b in bands)
    bh = float(heights[len(heights) // 2])
    if len(bands) >= 3:
        gaps = sorted(bands[i + 1][0] - bands[i][0] for i in range(len(bands) - 1))
        pitch = float(gaps[len(gaps) // 2])
    else:
        pitch = bh * 1.35
    return bh, pitch, len(bands)


def _page_levels(gray: Image.Image, exclude: Image.Image | None) -> tuple[int, int]:
    """背景 = 眾數；前景 = 墨色的低分位數（不是面積眾數），並強制最低對比。"""
    if exclude is None:
        hist = gray.histogram()
    else:
        g1 = gray.point(lambda v: v if v else 1)
        hist = Image.composite(g1, Image.new("L", gray.size, 0),
                               ImageChops.invert(exclude)).histogram()
        hist[0] = 0
    total = sum(hist)
    if total <= 0:
        return 255, 0
    bg = max(range(256), key=lambda i: hist[i])
    light = bg >= 128
    rng = range(0, max(0, bg - 50)) if light else range(min(255, bg + 51), 256)
    n = sum(hist[i] for i in rng)
    fg = None
    if n >= max(150, total * 0.0008):
        want, acc = n * PAGE_PCT, 0
        for i in (rng if light else reversed(rng)):
            acc += hist[i]
            if acc >= want:
                fg = i
                break
    if fg is None:
        fg = 0 if light else 255
    if light:
        fg = max(0, min(fg, bg - PAGE_CONTRAST))
    else:
        fg = min(255, max(fg, bg + PAGE_CONTRAST))
    return bg, fg


# ------------------------------------------------------------------ 連通區
def _components(mask: Image.Image):
    w, h = mask.size
    data = mask.tobytes()
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    all_runs = []
    prev: list[tuple[int, int, int]] = []
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        cur = []
        pos = 0
        while True:
            i = row.find(255, pos)
            if i < 0:
                break
            j = row.find(0, i + 1)
            if j < 0:
                j = w
            rid = len(parent)
            parent.append(rid)
            cur.append((i, j, rid))
            all_runs.append((y, i, j, rid))
            pos = j + 1
        pi = 0
        for i, j, rid in cur:
            while pi < len(prev) and prev[pi][1] <= i:
                pi += 1
            k = pi
            while k < len(prev) and prev[k][0] < j:
                ra, rb = find(prev[k][2]), find(rid)
                if ra != rb:
                    parent[rb] = ra
                k += 1
        prev = cur

    comps: dict[int, dict] = {}
    for y, i, j, rid in all_runs:
        root = find(rid)
        c = comps.get(root)
        if c is None:
            comps[root] = {"runs": [(y, i, j)], "area": j - i,
                           "x0": i, "x1": j, "y0": y, "y1": y + 1}
        else:
            c["runs"].append((y, i, j))
            c["area"] += j - i
            if i < c["x0"]:
                c["x0"] = i
            if j > c["x1"]:
                c["x1"] = j
            c["y1"] = y + 1
    return list(comps.values())


def _mask_from_runs(size, runs_list) -> Image.Image:
    w, h = size
    buf = bytearray(w * h)
    for runs in runs_list:
        for y, x0, x1 in runs:
            base = y * w
            buf[base + x0:base + x1] = b"\xff" * (x1 - x0)
    return Image.frombytes("L", size, bytes(buf))


def _holefill(m: Image.Image) -> Image.Image:
    """把被遮罩完全包住的洞（= 色塊裡的字）補起來。不用寫死的閉運算核心。"""
    w, h = m.size
    holes = [c["runs"] for c in _components(ImageChops.invert(m))
             if c["x0"] > 0 and c["y0"] > 0 and c["x1"] < w and c["y1"] < h]
    if not holes:
        return m
    return ImageChops.lighter(m, _mask_from_runs((w, h), holes))


def _count_in_runs(data: bytes, w: int, runs) -> int:
    n = 0
    for y, x0, x1 in runs:
        base = y * w
        n += data.count(255, base + x0, base + x1)
    return n


# ------------------------------------------------------------------ 主流程
def normalize(img: Image.Image, boxes_out: list | None = None,
              paint: bool = True) -> Image.Image:
    """找不到可靠的選取色塊（或中途出任何狀況）就原樣返回原本那張圖。"""
    try:
        rgb = img if img.mode == "RGB" else img.convert("RGB")
        out = _normalize(rgb, boxes_out, paint)
        return img if out is rgb else out
    except Exception:
        if DEBUG:
            import traceback
            traceback.print_exc()
        return img


def _normalize(img: Image.Image, boxes_out: list | None = None,
               paint: bool = True) -> Image.Image:
    W, H = img.size
    npx = W * H
    if npx < 4000 or W < 24 or H < 12:
        return img

    gray = img.convert("L")
    bg_lum = max(range(256), key=lambda i, hh=gray.histogram(): hh[i])
    ink = gray.point(lambda v, b=bg_lum: 255 if abs(v - b) > 45 else 0)
    n_ink = _count(ink)
    if n_ink < 60:
        return img

    # 文字尺度：兩個互相獨立的筆畫粗細估計取小的那個——
    #   (a) 面積 / 侵蝕後面積：對純文字很準，但畫面上有大片實心色塊時會爆掉
    #   (b) 水平連續墨色長度的中位數（忽略超長的 run）：對實心色塊免疫
    # 再用水平投影量到的文字帶高，並用筆畫粗細把它夾在合理範圍內。
    # 所有核心大小都由這裡推出來，沒有任何寫死的像素值。
    n_er = _count(_morph(ink, 3, 3, False))
    ws_a = 2.0 * n_ink / max(1.0, n_ink - n_er) if n_ink > n_er else 2.0
    ws_b = _stroke(ink) * 1.6
    ws = min(max(ws_a if ws_a <= 3.0 * ws_b else ws_b, 1.5), 40.0)

    bh, _pitch, _nb = _bands(ink)
    if not bh:
        return img
    bh = min(max(bh, 6.0 * ws), 14.0 * ws)
    bh = min(max(bh, 6.0), H)

    # 工作解析度：讓小圖上的文字帶高大約 9~18 px
    R = max(1, int(bh // 11))
    R = max(1, min(R, max(1, W // 120), max(1, H // 60), 14))
    sw, sh = max(8, W // R), max(8, H // R)
    small = img.resize((sw, sh), Image.NEAREST) if R > 1 else img
    gray_s = gray.resize((sw, sh), Image.NEAREST) if R > 1 else gray
    bs = bh / R
    if bs < 4:
        return img

    _dbg(f"size={W}x{H} bg={bg_lum} ws={ws:.1f} bh={bh:.1f} "
         f"R={R} small={sw}x{sh} bs={bs:.1f}")
    r_dx = max(2.0, DENS_WX * bs)
    r_dy = max(1.0, DENS_WY * bs)
    k_seal = max(3, int(round(CLOSE_RATIO * bs)) | 1)
    min_h = max(4, int(MIN_H_RATIO * bs))
    min_w = max(3, int(MIN_W_RATIO * bs))
    min_area = max(30, int(0.30 * bs * bs))
    sn = sw * sh

    # ---- 候選底色 ----
    try:
        q = small.quantize(colors=PALETTE_COLORS, method=Image.FASTOCTREE)
    except Exception:
        return img
    pal = q.getpalette() or []
    counts = q.histogram()
    cand = [(counts[i], (pal[3 * i], pal[3 * i + 1], pal[3 * i + 2]))
            for i in range(min(256, len(pal) // 3)) if counts[i] > 0]
    if not cand:
        return img
    cand.sort(reverse=True, key=lambda t: t[0])
    page_rgb = cand[0][1]
    # 邊框那一圈通常就是頁面底色；整頁被選取時全域眾數會是選取色，靠邊框才對
    qb = q.tobytes()
    tk = 2
    frame = bytearray(qb[0:tk * sw]) + bytearray(qb[(sh - tk) * sw:])
    for y in range(tk, sh - tk):
        b0 = y * sw
        frame += qb[b0:b0 + tk]
        frame += qb[b0 + sw - tk:b0 + sw]
    fcnt = [0] * 256
    for v in frame:
        fcnt[v] += 1
    fi = max(range(256), key=lambda i: fcnt[i])
    if counts[fi] >= 0.08 * sw * sh and 3 * fi + 2 < len(pal):
        page_rgb = (pal[3 * fi], pal[3 * fi + 1], pal[3 * fi + 2])
    _dbg(f"page_rgb={page_rgb} (frame idx {fi}, {100.0*counts[fi]/(sw*sh):.1f}%)")

    def far(c1, c2, d):
        return max(abs(c1[0] - c2[0]), abs(c1[1] - c2[1]), abs(c1[2] - c2[2])) > d

    min_cnt = max(24, int(min_area * 0.6))
    chosen = []
    for cnt, col in cand:
        if cnt < min_cnt:
            break
        if not far(col, page_rgb, COLOR_TOL + 8):
            continue
        if any(not far(col, c, COLOR_TOL) for c in chosen):
            continue
        chosen.append(col)
        if len(chosen) >= 8:
            break
    if not chosen:
        return img

    # ---- 逐色偵測 ----
    found = []          # (color, acc_mask_small, fm_small)
    total_area = 0
    ncomp = 0
    for col in chosen:
        fm = _color_mask(small, col)
        dens = fm.filter(ImageFilter.BoxBlur((r_dx, r_dy)))
        core = _bin(dens, int(DENS_THR * 255))
        if not core.getbbox():
            continue
        # 用「底色連通區（洞已補起來）」當色塊本體，只留下含有足夠『內部』像素的
        # 那些：這樣色塊的邊界是它自己的邊界，不會被密度視窗的半徑切掉一截。
        blk = _holefill(_close(fm, k_seal, k_seal))
        if not blk.getbbox():
            continue
        fm_bytes = fm.tobytes()
        core_bytes = core.tobytes()
        keep = []
        for c in _components(blk):
            w = c["x1"] - c["x0"]
            h = c["y1"] - c["y0"]
            nfill = _count_in_runs(fm_bytes, sw, c["runs"])
            gfrac = 1.0 - nfill / float(c["area"])
            ncore = _count_in_runs(core_bytes, sw, c["runs"])
            ok = (h >= min_h and w >= min_w and c["area"] >= min_area
                  and ncore >= max(4, CORE_FRAC * c["area"])
                  and c["area"] >= RECT_RATIO * w * h
                  and GLYPH_LO <= gfrac <= GLYPH_HI)
            if DEBUG and (ok or c["area"] >= min_area):
                _dbg(f"  {col} comp=({c['x0']},{c['y0']},{c['x1']},{c['y1']}) "
                     f"w={w} h={h} area={c['area']} rect={c['area']/(w*h):.2f} "
                     f"gfrac={gfrac:.2f} core={ncore} -> {'ACCEPT' if ok else 'reject'}"
                     f" [min_h={min_h} min_w={min_w} min_area={min_area}]")
            if ok:
                keep.append(c)
        if not keep:
            continue
        ncomp += len(keep)
        acc = _mask_from_runs((sw, sh), [c["runs"] for c in keep])
        total_area += sum(c["area"] for c in keep)
        found.append((col, acc, fm))

    _dbg(f"colors={len(found)} comps={ncomp} cover={100.0*total_area/sn:.1f}%")
    if not found or ncomp > MAX_REGIONS or total_area > MAX_COVER * sn:
        _dbg("ABORT (no region / too many / too much coverage)")
        return img

    # ---- 頁面前景／背景（排除所有候選色塊）----
    allacc = found[0][1]
    for _, a, _f in found[1:]:
        allacc = ImageChops.lighter(allacc, a)
    excl = allacc.resize((W, H), Image.NEAREST) if R > 1 else allacc
    excl = _morph(excl, 2 * R + 1, 2 * R + 1, True)
    page_bg, page_fg = _page_levels(gray, excl)
    page_text_lighter = page_fg > page_bg

    # ---- 逐色判斷極性並重畫 ----
    out = None
    k_seal_full = max(3, (k_seal * R) | 1)
    for col, acc, fm in found:
        fillpix = ImageChops.darker(acc, fm)
        glyphpix = ImageChops.subtract(acc, fm)
        hf = Image.composite(gray_s.point(lambda v: v or 1),
                             Image.new("L", (sw, sh), 0), fillpix).histogram()
        hf[0] = 0
        hg = Image.composite(gray_s.point(lambda v: v or 1),
                             Image.new("L", (sw, sh), 0), glyphpix).histogram()
        hg[0] = 0
        nf, ng = sum(hf), sum(hg)
        if nf < 20 or ng < 10:
            continue
        L0 = max(range(256), key=lambda i: hf[i])
        mean_g = sum(i * hg[i] for i in range(256)) / ng
        up = mean_g > L0
        order = range(255, -1, -1) if up else range(256)
        want, acc_n, L1 = ng * 0.25, 0, None
        for i in order:
            acc_n += hg[i]
            if acc_n >= want:
                L1 = i
                break
        if L1 is None or abs(L1 - L0) < MIN_CONTRAST:
            _dbg(f"  {col} low block contrast L0={L0} L1={L1}")
            continue
        block_text_lighter = L1 > L0
        same = block_text_lighter == page_text_lighter
        shift = abs(L0 - page_bg)
        _dbg(f"  {col} L0={L0} L1={L1} page bg={page_bg} fg={page_fg} "
             f"same_polarity={same} bg_shift={shift}")
        if same and shift < BG_SHIFT:
            continue                      # 螢光筆之類本來極性就對的，別動

        # 全解析度精修：只在色塊外接矩形附近運算
        full = acc.resize((W, H), Image.NEAREST) if R > 1 else acc
        bb = full.getbbox()
        if not bb:
            continue
        pad = 3 * R + k_seal_full
        x0 = max(0, bb[0] - pad); y0 = max(0, bb[1] - pad)
        x1 = min(W, bb[2] + pad); y1 = min(H, bb[3] + pad)
        sub = img.crop((x0, y0, x1, y1))
        fm_f = _color_mask(sub, col)
        filled = _holefill(_close(fm_f, 3, 3))
        blk_f = ImageChops.lighter(filled, _close(filled, k_seal_full, k_seal_full))
        up_m = _morph(full.crop((x0, y0, x1, y1)), 2 * R + 1, 2 * R + 1, True)
        reg_f = ImageChops.darker(up_m, blk_f)
        if not reg_f.getbbox():
            continue

        span = float(L1 - L0)
        lo = L0 + RAMP_LO * span
        hi = L0 + RAMP_HI * span
        lut = []
        for v in range(256):
            t = (v - lo) / (hi - lo)
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            lut.append(int(round(page_bg + t * (page_fg - page_bg))))
        if boxes_out is not None:
            rb = reg_f.getbbox()
            if rb:
                boxes_out.append((x0 + rb[0], y0 + rb[1], x0 + rb[2], y0 + rb[3]))
        if not paint:
            continue
        painted = gray.crop((x0, y0, x1, y1)).point(lut).convert("RGB")
        if out is None:
            out = img.copy()
        # 色塊外緣那圈「底色 -> 頁面底色」的抗鋸齒彩邊不在 reg_f 裡，留著會變成
        # 一個彩色方框；先把它抹成頁面底色，再把色塊本體映射上去。
        rd = max(3, int(round(ws * RIM_RATIO)) | 1)
        rim = _morph(reg_f, rd, rd, True) if RIM_RATIO > 0 else reg_f
        flat = Image.new("RGB", sub.size, (page_bg, page_bg, page_bg))
        out.paste(flat, (x0, y0), rim)
        out.paste(painted, (x0, y0), reg_f)
        _dbg(f"  {col} REPAINT box=({x0},{y0},{x1},{y1}) px={_count(reg_f)}")

    return out if out is not None else img
