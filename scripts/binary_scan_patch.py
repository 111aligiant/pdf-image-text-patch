#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
二值扫描件改字（1bit 扫描 PDF / 图片，原地补字）
================================================

扫描件大多没有文本层，整页就是一张图。如果这张图是**纯二值**（像素只有 0 和 255，
常见于 200dpi 黑白扫描、传真模式、复印件再扫），位图路径那套「alpha 混合 + 高斯柔边
+ 渐变背景重建」全都不适用——那些手段是为灰度/彩色抗锯齿准备的，往二值图里放一个
带灰度边的字，放大就是一块补丁。

这条路径的规矩反过来：**新字也必须是二值的**。链路是

    超采样渲染 → 面积平均降采样 → 阈值 0.5 → 1bit 字形

顺带一个好处：降采样前的位置可以按 1/SS 像素平移，于是「扫描采样相位」变成可搜索
参数——同一套字体/字号，换个相位出来的二值图案能差出好几个像素的笔画。

六步同源，只是每一步的操作对象都是 bit：
    ① 定位（列投影切字） ② 取样（二值无需取背景） ③ 遮除（直接刷白）
    ④ 写入（二值化渲染） ⑤ 对齐（锚点字墨迹左缘 + 基线） ⑥ 校验（逐像素比对）

用法
----
# 1) 摸底：这文件是不是二值？这一块的每个字落在哪几列？
python binary_scan_patch.py --pdf a.pdf --probe
python binary_scan_patch.py --pdf a.pdf --box 1080,385,1330,430 \
    --ref-text "2026年1月15日" --probe

# 2) 反查字体：拿原图里已有的字当参考答案，搜 字体×字号×描边×相位
python binary_scan_patch.py --pdf a.pdf --box 1080,385,1330,430 \
    --ref-text "2026年1月15日" --fit-chars 206 --fit

# 3) 改写：把 15 换成 28
python binary_scan_patch.py --pdf a.pdf --out b.pdf \
    --erase 1252,390,1292,424 --text 28 \
    --font C:/Windows/Fonts/simfang.ttf --size 31 --stroke 2 --phase 5,4 \
    --ink-left 1270 --baseline 416
"""
from __future__ import annotations

import argparse
import io
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SS = 8  # 超采样倍数：相位分辨率 = 1/SS 像素

DEFAULT_FONTS = [
    "simfang.ttf", "simsun.ttc", "simkai.ttf", "simhei.ttf", "Deng.ttf",
    "msyh.ttc", "STSONG.TTF", "times.ttf", "timesbd.ttf", "arial.ttf",
    "arialbd.ttf", "cour.ttf", "courbd.ttf", "georgia.ttf", "cambria.ttc",
    "calibri.ttf", "consola.ttf", "verdana.ttf", "tahoma.ttf",
]
FONT_DIRS = [
    r"C:\Windows\Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/System/Library/Fonts",
    "/usr/share/fonts/truetype",
]


# ------------------------------------------------------------------ 源文件
class Source:
    """一个「整页就是一张图」的载体：PDF 的内嵌图，或者一张图片文件。"""

    def __init__(self, path, page=0):
        self.path = path
        self.page = page
        self.doc = None
        self.rect = None
        self.metadata = {}
        self.xref = None
        self.text_len = 0

        if path.lower().endswith(".pdf"):
            import fitz
            self.doc = fitz.open(path)
            pg = self.doc[page]
            self.rect = pg.rect
            self.metadata = self.doc.metadata
            self.text_len = len(pg.get_text().strip())
            infos = pg.get_images(full=True)
            if not infos:
                sys.exit("这一页没有内嵌图像，说明它是纯矢量页——请走矢量路径（pdf_field_replace.py）")
            if len(infos) > 1:
                print(f"提示：本页有 {len(infos)} 张图，只处理最大的一张")
            info = max(infos, key=lambda i: i[2] * i[3])
            self.xref = info[0]
            raw = self.doc.extract_image(self.xref)
            self.img = Image.open(io.BytesIO(raw["image"]))
            self.dpi = info[2] / (pg.rect.width / 72.0)
            self.page_rect = pg.rect
        else:
            self.img = Image.open(path)
            self.dpi = self.img.info.get("dpi", (200, 200))[0] or 200
            self.page_rect = None

        self.arr = np.asarray(self.img.convert("L")).copy()
        self.is_binary = self.check_binary()

    def check_binary(self):
        vals = np.unique(self.arr)
        return len(vals) <= 2 and set(int(v) for v in vals) <= {0, 255}

    def describe(self):
        print(f"源文件    : {self.path}" + (f"  第 {self.page + 1} 页" if self.doc else ""))
        print(f"图像      : {self.arr.shape[1]}x{self.arr.shape[0]}  mode={self.img.mode}")
        print(f"分辨率    : {self.dpi:.1f} dpi")
        if self.page_rect:
            print(f"页面      : {self.page_rect.width:.2f} x {self.page_rect.height:.2f} pt")
        vals = np.unique(self.arr)
        print(f"像素取值  : {vals[:6].tolist()}{' ...' if len(vals) > 6 else ''}"
              f"  共 {len(vals)} 种")
        print(f"二值      : {'是' if self.is_binary else '否 ← 走位图路径 img_text_replace.py'}")
        if self.text_len:
            print(f"注意      : 本页还有 {self.text_len} 个可提取字符，说明是「矢量文字 + 底图」混排。"
                  "\n            要改的是文字就优先走矢量路径（pdf_field_replace.py），"
                  "\n            本脚本只改底图上的像素。")


# ------------------------------------------------------------------ ① 定位
def column_runs(mask):
    """列投影：返回连续墨迹列区间 [(x0, x1_exclusive), ...]。"""
    cols = mask.any(axis=0)
    runs, s = [], None
    for i, v in enumerate(cols):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i))
            s = None
    if s is not None:
        runs.append((s, len(cols)))
    return runs


def glyph_boxes(arr, box):
    """在 box 内按列投影切字，返回 [(x0, x1, y0, y1, ink)]，x/y 是绝对坐标。"""
    x0, y0, x1, y1 = box
    sub = arr[y0:y1, x0:x1] == 0
    out = []
    for a, b in column_runs(sub):
        part = sub[:, a:b]
        ys, xs = np.nonzero(part)
        out.append((x0 + a, x0 + b - 1, y0 + ys.min(), y0 + ys.max(), int(part.sum())))
    return out


# ------------------------------------------------------------------ ④ 渲染
_font_cache = {}


def _font(path, size):
    key = (path, int(round(size * SS)))
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, key[1])
    return _font_cache[key]


def render_binary(text, font_path, size, stroke=0, phase=(0, 0)):
    """超采样渲染 → 面积平均降采样 → 阈值 0.5，得到 1bit 字形。

    phase 是 1/SS 像素的亚像素偏移，模拟「原稿这一笔落在像素格的哪个位置」。
    同一套字体/字号，换个 phase 出来的二值图案能差出好几个像素的笔画。

    画布按 textbbox 收紧（反查要跑上千次，画布小一圈快好几倍）。
    """
    kx, ky = phase
    f = _font(font_path, size)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
    pad = 4 * SS
    w = ((bb[2] - bb[0] + 2 * pad) // SS + 1) * SS
    h = ((bb[3] - bb[1] + 2 * pad) // SS + 1) * SS
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).text((pad - bb[0] + kx, pad - bb[1] + ky), text,
                             fill=255, font=f,
                             stroke_width=stroke, stroke_fill=255)
    a = np.asarray(img, dtype=np.float32) / 255.0
    a = a.reshape(h // SS, SS, w // SS, SS).mean(axis=(1, 3))
    return a > 0.5


def split_glyphs(mask):
    """把一串字按空列切成单字，返回 [(x0, x1_exclusive), ...]。"""
    return column_runs(mask)


def ink_box(mask):
    ys, xs = np.nonzero(mask)
    return xs.min(), ys.min(), xs.max(), ys.max()


def iou(ref, cand):
    u = (ref | cand).sum()
    return (ref & cand).sum() / u if u else 0.0


def fit_shape(g, ref):
    """把 g 缩放到 ref 的像素尺寸再比（尺寸一致时直接返回）。"""
    if g.shape == ref.shape:
        return g
    im = Image.fromarray((g * 255).astype("uint8")).resize(
        (ref.shape[1], ref.shape[0]), Image.BOX)
    return (np.asarray(im, dtype=np.float32) / 255.0) > 0.5


def glyph_mask(mask, run):
    a, b = run
    ys, xs = np.nonzero(mask[:, a:b])
    return mask[ys.min():ys.max() + 1, a + xs.min():a + xs.max() + 1]


# ------------------------------------------------------------------ 反查
def resolve_fonts(names):
    out = []
    for n in names:
        if os.path.isabs(n):
            out.append(n)
            continue
        for d in FONT_DIRS:
            p = os.path.join(d, n)
            if os.path.exists(p):
                out.append(p)
                break
    return out


def fit_font(src, box, ref_text, fit_chars, fonts, size_lo, size_hi, text_hint=None):
    """拿原图里已有的字当锚点，搜 字体 × 字号 × 描边 × 相位，按 IoU 排序。"""
    boxes = glyph_boxes(src.arr, box)
    chars = list(ref_text)
    if len(boxes) != len(chars):
        print(f"切出 {len(boxes)} 个字形，但 --ref-text 给了 {len(chars)} 个字：")
        for i, b in enumerate(boxes):
            print(f"  #{i}  x {b[0]}..{b[1]}  w {b[1] - b[0] + 1}  ink {b[4]}")
        sys.exit("请调整 --box 或 --ref-text，让两边数量对上")

    refs = {}
    print("切字结果：")
    for ch, b in zip(chars, boxes):
        print(f"  {ch}  x {b[0]}..{b[1]}  w {b[1] - b[0] + 1}  "
              f"y {b[2]}..{b[3]}  h {b[3] - b[2] + 1}  ink {b[4]}")
    mask = src.arr[box[1]:box[3], box[0]:box[2]] == 0
    local = column_runs(mask)
    for i, ch in enumerate(chars):
        if ch in fit_chars and ch not in refs:
            refs[ch] = glyph_mask(mask, local[i])
    if not refs:
        sys.exit(f"--fit-chars 里的字 {fit_chars!r} 一个都没在切字结果里找到")

    print(f"\n锚点字形：{' '.join(f'{k}{v.shape}' for k, v in refs.items())}")
    print(f"字体 {len(fonts)} 个，字号 {size_lo}..{size_hi}，阶段一（相位固定 0,0）…")

    # 先按字高定字号，再比形状。
    # 如果允许任意缩放去凑参考框，天生更宽的粗体字会被硬压进来，选出错的字体。
    # 二值图里字高随字号跳变，所以对每个 (字体, 描边) 解出「墨迹高最接近锚点」的字号。
    primary = next(iter(refs))
    ph = refs[primary].shape[0]
    stage1 = []
    for path in fonts:
        for stroke in range(0, 6):
            best_h = None
            for size in np.arange(size_lo, size_hi + 0.01, 1.0):
                m = render_binary(primary, path, float(size), stroke, (0, 0))
                bx = ink_box(m)
                d = abs((bx[3] - bx[1] + 1) - ph)
                if best_h is None or d < best_h[0]:
                    best_h = (d, float(size))
            if best_h[0] > 1:
                continue  # 字高都对不上，字形不用比了
            size = best_h[1]
            tot = []
            for ch, ref in refs.items():
                m = render_binary(ch, path, size, stroke, (0, 0))
                bx = ink_box(m)
                tot.append(iou(ref, fit_shape(m[bx[1]:bx[3] + 1, bx[0]:bx[2] + 1], ref)))
            stage1.append((float(np.mean(tot)), path, size, stroke))
    if not stage1:
        sys.exit("没有候选的字高能对上锚点，检查 --box 是否框准了、--size-range 是否覆盖")
    stage1.sort(key=lambda t: -t[0])

    print(f"阶段二：对前 8 名扫相位（{SS}x{SS}）…")
    final = []
    for base in stage1[:8]:
        _, path, size, stroke = base
        for kx in range(SS):
            for ky in range(SS):
                tot = []
                for ch, ref in refs.items():
                    m = render_binary(ch, path, size, stroke, (kx, ky))
                    bx = ink_box(m)
                    g = fit_shape(m[bx[1]:bx[3] + 1, bx[0]:bx[2] + 1], ref)
                    tot.append(iou(ref, g))
                final.append((float(np.mean(tot)), path, size, stroke, kx, ky, tot))
    final.sort(key=lambda t: -t[0])

    print("\n候选（分数差 0.01 以内属噪声，前 3 名都要渲染比一眼）：")
    print(f"{'IoU':>6}  {'字体':<18}{'字号':>5}{'描边':>5}{'相位':>8}  逐字")
    for t in final[:10]:
        per = " ".join(f"{v:.3f}" for v in t[6])
        print(f"{t[0]:6.3f}  {os.path.basename(t[1]):<18}{t[2]:5.0f}{t[3]:5d}"
              f"{t[4]:4d},{t[5]:<3d}  {per}")

    b = final[0]
    m = render_binary(primary, b[1], b[2], b[3], (b[4], b[5]))
    bx = ink_box(m)
    print(f"\n建议参数：--font {b[1]} --size {b[2]:.0f} --stroke {b[3]} "
          f"--phase {b[4]},{b[5]}")
    print(f"该参数下 '{primary}' 的自然墨迹 {bx[2] - bx[0] + 1}x{bx[3] - bx[1] + 1}，"
          f"原图 {refs[primary].shape[1]}x{refs[primary].shape[0]}")
    if text_hint:
        m2 = render_binary(text_hint, b[1], b[2], b[3], (b[4], b[5]))
        bx2 = ink_box(m2)
        print(f"'{text_hint}' 的自然墨迹 {bx2[2] - bx2[0] + 1}x{bx2[3] - bx2[1] + 1}"
              f"（贴图时按锚点字墨迹左缘 + 基线放，尺寸别再缩，缩了笔画就坏）")
    return b


# ------------------------------------------------------------------ ③④⑤ 改写
def apply_patch(src, args):
    A = src.arr.copy()
    ex0, ey0, ex1, ey1 = args.erase
    bg = int(np.median(A))
    A[ey0:ey1 + 1, ex0:ex1 + 1] = bg  # 二值图直接刷底色，没有抗锯齿残留

    phase = tuple(int(v) for v in args.phase.split(","))
    m = render_binary(args.text, args.font, args.size, args.stroke, phase)
    runs = split_glyphs(m)
    if len(runs) != len(args.text):
        sys.exit(f"'{args.text}' 切出 {len(runs)} 个字，与字符数 {len(args.text)} 不符"
                 "（可能笔画粘连，换个 --stroke 或 --size 试试）")

    bx = ink_box(m)
    patch = m[bx[1]:bx[3] + 1, bx[0]:bx[2] + 1]

    # ⑤ 对齐：锚点字墨迹左缘 + 整串底边（基线）
    idx = len(args.text) - 1
    if args.anchor_char:
        if args.anchor_char not in args.text:
            sys.exit(f"--anchor-char {args.anchor_char!r} 不在 --text 里")
        idx = args.text.index(args.anchor_char)
    anchor_x = runs[idx][0]
    ox = args.ink_left - anchor_x          # ox 是画布原点：画布坐标 anchor_x 要落在 ink_left
    oy = args.baseline - bx[3]             # 画布坐标 bx[3]（墨迹底边）要落在 baseline

    A[oy + bx[1]:oy + bx[3] + 1, ox + bx[0]:ox + bx[2] + 1] = np.where(patch, 0, bg)

    print(f"写入      : {args.text!r}  {os.path.basename(args.font)} "
          f"size={args.size} stroke={args.stroke} phase={phase}")
    print(f"字形切分  : " + "  ".join(f"{c}=[{r[0]},{r[1]})"
                                      for c, r in zip(args.text, runs)))
    print(f"贴图位置  : 锚点 {args.text[idx]!r} 墨迹左缘 x={args.ink_left}，"
          f"底边 y={args.baseline}；整串墨迹 x {ox + bx[0]}..{ox + bx[2]} "
          f"y {oy + bx[1]}..{oy + bx[3]}")
    return A


# ------------------------------------------------------------------ 输出
def write_output(src, A, out):
    img = Image.fromarray(A).convert("1")
    if out.lower().endswith(".pdf"):
        import fitz
        doc = fitz.open()
        w = src.page_rect.width if src.page_rect else A.shape[1] / src.dpi * 72
        h = src.page_rect.height if src.page_rect else A.shape[0] / src.dpi * 72
        pg = doc.new_page(width=w, height=h)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pg.insert_image(pg.rect, stream=buf.getvalue(), keep_proportion=False)
        if src.metadata:
            doc.set_metadata(src.metadata)
        doc.save(out, deflate=True, garbage=4)
        doc.close()
    else:
        img.save(out)
    print(f"输出      : {out}  ({os.path.getsize(out)} bytes)")


def verify(src, out, erase):
    """⑥ 校验：逐像素比对，差异必须全部落在抹除框内。"""
    import fitz
    d = fitz.open(out)
    pix = d[0].get_pixmap(dpi=int(round(src.dpi)))
    r = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, 0]
    d.close()
    if r.shape != src.arr.shape:
        print(f"校验·尺寸   : 渲染 {r.shape} != 原图 {src.arr.shape}（dpi 取整导致，忽略）")
        return
    diff = r != src.arr
    n = int(diff.sum())
    if n == 0:
        print("校验·差异   : 0 像素 —— 什么都没改？检查 --erase 是否框住了旧字")
        return
    ys, xs = np.nonzero(diff)
    bb = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    inside = (bb[0] >= erase[0] and bb[1] >= erase[1]
              and bb[2] <= erase[2] and bb[3] <= erase[3])
    print(f"校验·差异   : {n} 像素，bbox {bb}")
    print(f"校验·越界   : {'无（全部落在抹除框内）' if inside else '有！差异跑出抹除框，检查 --erase'}")


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(
        description="二值扫描件改字：1bit 进，1bit 出",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", help="源 PDF（只读）")
    ap.add_argument("--img", help="源图片（只读）；与 --pdf 二选一")
    ap.add_argument("--page", type=int, default=0, help="页码，从 0 起")
    ap.add_argument("--out", help="输出文件：.pdf 重建单页 PDF，其它扩展名存 1bit 图片")

    ap.add_argument("--probe", action="store_true", help="只摸底，不写入")
    ap.add_argument("--box", help="探查/反查区域 x0,y0,x1,y1（右下开区间）")
    ap.add_argument("--ref-text", help="区域里现有的文字，按顺序，用于切字对照")
    ap.add_argument("--fit", action="store_true", help="反查字体/字号/描边/相位")
    ap.add_argument("--fit-chars", default="0123456789",
                    help="参与打分的字（默认全部数字）")
    ap.add_argument("--fonts", help="候选字体，逗号分隔；默认一组常见中西文字体")
    ap.add_argument("--size-range", default="20,40", help="字号搜索范围 lo,hi")

    ap.add_argument("--erase", help="抹除框 x0,y0,x1,y1（闭区间，要盖住旧字墨迹）")
    ap.add_argument("--text", help="新文字")
    ap.add_argument("--font", default="simfang.ttf")
    ap.add_argument("--size", type=float, default=31)
    ap.add_argument("--stroke", type=int, default=2)
    ap.add_argument("--phase", default="0,0", help="亚像素相位 kx,ky（1/%d 像素）" % SS)
    ap.add_argument("--ink-left", type=int, help="锚点字的墨迹左缘 x")
    ap.add_argument("--baseline", type=int, help="墨迹底边 y（基线）")
    ap.add_argument("--anchor-char", help="锚点字，默认 --text 的最后一个字")
    args = ap.parse_args()

    if not args.pdf and not args.img:
        sys.exit("需要 --pdf 或 --img")
    src = Source(args.pdf or args.img, args.page)
    src.describe()

    if args.probe:
        if args.box:
            box = [int(v) for v in args.box.split(",")]
            boxes = glyph_boxes(src.arr, box)
            print(f"\n区域内切出 {len(boxes)} 个字形：")
            for i, b in enumerate(boxes):
                ch = args.ref_text[i] if args.ref_text and i < len(args.ref_text) else "?"
                print(f"  {ch}  x {b[0]}..{b[1]}  w {b[1] - b[0] + 1}  "
                      f"y {b[2]}..{b[3]}  h {b[3] - b[2] + 1}  ink {b[4]}")
        return

    if not src.is_binary:
        sys.exit("不是二值图 —— 请走位图路径 scripts/img_text_replace.py")

    if args.fit:
        if not (args.box and args.ref_text):
            sys.exit("--fit 需要 --box 和 --ref-text")
        fonts = resolve_fonts((args.fonts.split(",") if args.fonts else DEFAULT_FONTS))
        lo, hi = (float(v) for v in args.size_range.split(","))
        fit_font(src, [int(v) for v in args.box.split(",")], args.ref_text,
                 set(args.fit_chars), fonts, lo, hi, args.text)
        return

    for need in ("out", "erase", "text", "ink_left", "baseline"):
        if getattr(args, need) is None:
            sys.exit(f"改写需要 --{need.replace('_', '-')}")
    args.erase = [int(v) for v in args.erase.split(",")]

    A = apply_patch(src, args)
    write_output(src, A, args.out)
    if args.out.lower().endswith(".pdf"):
        verify(src, args.out, args.erase)
        print("\n提醒：输出是重建的单页 PDF（原文件只读，从未被覆盖）。")
    print("      交付前请把改动处放大 6~8 倍，与同行的其他字逐字比一眼。")


if __name__ == "__main__":
    main()
