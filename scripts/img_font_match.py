#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
字体反查：从图里已有的字反推它用的是哪个字体、多大字号、多粗的笔画
================================================================

位图改字最难的一步是"用哪个字体画"。肉眼猜基本没戏——同一段数字，
Arial / 等线 / 思源黑体的相关系数只差 0.05，但放大一看就露。

这个脚本把这件事变成可复现的打分：拿图里已有的字形当参考答案，
遍历系统字体 × 字号 × 描边宽度，算归一化相关系数，取最高分。

关键：**先按字高定字号，再比形状**。
如果允许任意缩放去凑参考框，天生更宽的粗体字会被硬压进来，选出错的字体。
所以每个 (字体, 描边) 组合先解出"自然墨迹高度 = 参考高度"的字号，再比形状，
宽度偏差只做扣分。

三阶段（360 个字体全量跑太慢）：
  阶段一  粗搜：低倍超采样，每个字体扫字号、描边固定 0，留前 N
  阶段二  精修：对前 N 个字体扫描边 0~8，字号按字高解出来，逐项打分
  阶段三  复算：对前 3 名用高倍超采样重算，给出准确的自然墨迹尺寸

输出：候选字体表、参考字的笔画粗细估计、以及用最优参数渲染 --target
时的自然墨迹尺寸（直接喂给 img_text_replace.py 的 --ink）。

用法
----
# 已知框内是 "12:34"，想知道字体，以及画 "39" 该用什么参数
python img_font_match.py --img a.jpg --box 120,1140,210,1180 --text "12:34" --target "39"

# 颜色已知时直接给，省掉自动估计
python img_font_match.py --img a.jpg --box 120,1140,210,1180 --text "12:34" \
    --fg 44,108,203 --bg 232,239,254
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = [
    r"C:\Windows\Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/System/Library/Fonts",
    "/usr/share/fonts",
]


# ---------------------------------------------------------------- 颜色/掩膜
def estimate_colors(arr):
    """背景取四角中位数，文字色取「离背景最远的前 3%」的中位数。

    不要用"最暗像素"当文字色：JPEG 会在笔画芯部产生过冲，取到的是过冲值，
    颜色会偏灰（实测 B 通道差 9，肉眼就是"这块发闷"）。
    """
    h, w = arr.shape[:2]
    k = max(2, min(h, w) // 10)
    corners = np.concatenate([
        arr[:k, :k].reshape(-1, 3), arr[:k, -k:].reshape(-1, 3),
        arr[-k:, :k].reshape(-1, 3), arr[-k:, -k:].reshape(-1, 3),
    ])
    bg = np.median(corners, axis=0)

    flat = arr.reshape(-1, 3)
    dist = np.linalg.norm(flat - bg, axis=1)
    fg = np.median(flat[dist >= np.percentile(dist, 97)], axis=0)
    return bg, fg


def to_alpha(arr, bg, fg):
    denom = np.maximum(np.abs(bg - fg), 1.0)
    return np.clip(((bg - arr) / denom).max(axis=2), 0.0, 1.0)


def ink_crop(a):
    """裁到 alpha>0.5 的墨迹边界。"""
    m = a > 0.5
    ys, xs = np.nonzero(m)
    if len(ys) == 0:
        return None
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def stroke_width(a):
    """估计笔画粗细：多条扫描线上的水平连续段中位数。"""
    m = a > 0.5
    h = m.shape[0]
    runs = []
    for y in [int(h * f) for f in (0.3, 0.45, 0.6, 0.75)]:
        if y >= h:
            continue
        row = m[y]
        start = None
        for i, v in enumerate(row):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append(i - start)
                start = None
        if start is not None:
            runs.append(len(row) - start)
    return float(np.median(runs)) if runs else 0.0


# ---------------------------------------------------------------- 渲染
def render(font_path, size, text, stroke, ss):
    """超采样渲染，返回裁到墨迹边界的 alpha（0~1）。失败返回 None。

    画布按内容收紧：宽 ≈ 字号×字数×0.8，高 ≈ 字号×2。
    用大方画布会白白多算几十倍像素，360 个字体跑下来要好几分钟。
    """
    if size * ss < 6:
        return None
    try:
        f = ImageFont.truetype(font_path, int(round(size * ss)))
    except Exception:
        return None
    unit = int(size * ss)
    W = max(unit, int(unit * 0.85 * len(text)) + unit)
    H = unit * 2
    img = Image.new("L", (W, H), 0)
    try:
        ImageDraw.Draw(img).text((unit // 4, unit // 2), text, fill=255, font=f,
                                 stroke_width=stroke, stroke_fill=255)
    except Exception:
        return None
    a = np.asarray(img, dtype=float) / 255.0
    ys, xs = np.nonzero(a > 0.02)
    if len(ys) == 0:
        return None
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def size_for_height(font_path, text, stroke, ss, want_h):
    """解出使自然墨迹高度 ≈ want_h 的字号（字高随字号近似线性，两步收敛）。"""
    size = 30.0
    for _ in range(2):
        got = render(font_path, size, text, stroke, ss)
        if got is None or got.shape[0] < 3:
            return None
        h = got.shape[0] / ss
        if h <= 0:
            return None
        size *= want_h / h
    if not (8 <= size <= 200):
        return None
    return size


def corr(ref, got):
    r = (ref - ref.mean()) / (ref.std() + 1e-6)
    q = (got - got.mean()) / (got.std() + 1e-6)
    return float((r * q).mean())


def score_pair(ref_ink, got_ink, ss):
    """把渲染结果缩放到参考墨迹框后算相关系数，并按宽度偏差扣分。"""
    rh, rw = ref_ink.shape
    gh, gw = got_ink.shape[0] / ss, got_ink.shape[1] / ss
    got = np.asarray(
        Image.fromarray((got_ink * 255).astype("uint8")).resize((rw, rh), Image.LANCZOS),
        dtype=float) / 255.0
    penalty = 0.3 * abs(np.log((gw / gh) / (rw / rh)))
    return corr(ref_ink, got) - penalty


def list_fonts():
    out = []
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith((".ttf", ".otf", ".ttc")):
                out.append(os.path.join(d, name))
    return out


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap = argparse.ArgumentParser(description="从图里的字形反查字体/字号/描边")
    ap.add_argument("--img", required=True)
    ap.add_argument("--box", required=True,
                    help="参考文字所在框 x0,y0,x1,y1（含字即可，别框进别的东西）")
    ap.add_argument("--text", required=True, help="框里是什么字，如 12:34")
    ap.add_argument("--target", default=None,
                    help="要渲染的新文字，如 39；用于输出自然墨迹尺寸")
    ap.add_argument("--fg", default="auto", help="文字色 r,g,b 或 auto")
    ap.add_argument("--bg", default="auto", help="背景色 r,g,b 或 auto")
    ap.add_argument("--top", type=int, default=8, help="输出前几名")
    ap.add_argument("--shortlist", type=int, default=12, help="阶段一保留多少个字体进精修")
    args = ap.parse_args(argv)

    x0, y0, x1, y1 = [int(v) for v in args.box.replace(" ", "").split(",")]
    arr = np.asarray(Image.open(args.img).convert("RGB"), dtype=float)[y0:y1, x0:x1]

    if args.fg == "auto" or args.bg == "auto":
        bg, fg = estimate_colors(arr)
    else:
        bg = np.array([float(v) for v in args.bg.split(",")])
        fg = np.array([float(v) for v in args.fg.split(",")])

    ref = ink_crop(to_alpha(arr, bg, fg))
    if ref is None:
        print("!! 框里没找到墨迹，检查 --box 或 --fg/--bg")
        return 2
    rh, rw = ref.shape
    sw = stroke_width(ref)
    print(f"参考文字 {args.text!r}  墨迹 {rw}x{rh}  宽高比 {rw/rh:.3f}")
    print(f"背景色 {bg.round(0)}  文字色 {fg.round(0)}")
    print(f"笔画粗细估计 {sw:.1f}px（字高 {rh}px，比值 {sw/rh:.3f}）")

    fonts = list_fonts()
    if not fonts:
        print("!! 没找到任何字体文件")
        return 2

    # ---------- 阶段一：粗搜，只为筛短名单
    SS1 = 2
    coarse = []
    for path in fonts:
        best = None
        for size in range(18, 54, 2):
            got = render(path, size, args.text, 0, SS1)
            if got is None or got.shape[0] < 4:
                continue
            s = score_pair(ref, got, SS1)
            if best is None or s > best[0]:
                best = (s, path)
        if best:
            coarse.append(best)
    coarse.sort(reverse=True)
    short = [c[1] for c in coarse[:args.shortlist]]
    print(f"\n粗搜: {len(fonts)} 个字体 -> 留 {len(short)} 个进精修")

    # ---------- 阶段二：短名单，扫描边，字号按字高解出来
    SS2 = 4
    results = []
    for path in short:
        best = None
        for stroke in range(0, 9):
            size = size_for_height(path, args.text, stroke, SS2, rh)
            if size is None:
                continue
            got = render(path, size, args.text, stroke, SS2)
            if got is None or got.shape[0] < 4:
                continue
            s = score_pair(ref, got, SS2)
            if best is None or s > best[0]:
                best = (s, path, size, stroke)
        if best:
            results.append(best)
    results.sort(reverse=True)

    # ---------- 阶段三：前 3 名用高倍重算，给出准确的自然墨迹尺寸
    SS3 = 6
    final = []
    for s, path, size, stroke in results[:3]:
        size3 = size_for_height(path, args.text, stroke, SS3, rh) or size
        got = render(path, size3, args.text, stroke, SS3)
        if got is None:
            final.append((s, path, size, stroke, None))
            continue
        s3 = score_pair(ref, got, SS3)
        final.append((s3, path, size3, stroke, got.shape))
    final.sort(key=lambda r: r[0], reverse=True)
    for s, path, size, stroke in results[3:args.top]:
        final.append((s, path, size, stroke, None))

    print(f"\n{'相关系数':>8}  {'字体':<26} {'字号':>6} {'描边':>4}  自然墨迹")
    print("-" * 80)
    for s, path, size, stroke, shape in final:
        name = os.path.basename(path)
        nat = f"{shape[1]/SS3:.2f}x{shape[0]/SS3:.2f}" if shape else "-"
        print(f"{s:8.4f}  {name:<26} {size:6.1f} {stroke:4d}  {nat}")

    if not final:
        print("!! 没有可用结果")
        return 2

    bs, bp, bsize, bstroke, bshape = final[0]
    print(f"\n最匹配: {os.path.basename(bp)}  size={bsize:.1f}  stroke={bstroke}")
    if bshape:
        print(f"        自然墨迹 {bshape[1]/SS3:.2f}x{bshape[0]/SS3:.2f}（参考 {rw}x{rh}）")

    if args.target:
        got = render(bp, bsize, args.target, bstroke, SS3)
        if got is not None:
            tw, th = got.shape[1] / SS3, got.shape[0] / SS3
            print(f"\n--target {args.target!r} 在该参数下的自然墨迹: {tw:.2f}x{th:.2f}")
            print(f"  建议 --ink x,y,{round(tw)},{round(th)}，"
                  f"按 ≤3% 微调（超 5% 字形会被拉坏）")
            print(f"  命令: --font \"{bp}\" --size {bsize:g} --stroke {bstroke}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
