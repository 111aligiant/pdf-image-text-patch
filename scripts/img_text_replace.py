#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
图片文字局部替换（位图，保留原字体观感）
======================================

场景：截图里某个数字/短文本要改，周围文字必须原样不动。
做法沿用 PDF 那套六步：定位 -> 取样 -> 遮除 -> 写入 -> 光学对齐 -> 校验。

关键点
------
1. 只抹掉要改的那几个字符，锚点字符（如 "12:"）原样保留 —— "像不像"由原图自己保证。
   整段重画要同时赌中字形/字重/字距/基线，风险高一个量级。
2. 抹除不是盖白块，而是用上下干净行逐列线性插值重建背景，渐变不会被破坏。
3. 字形先用 img_font_match.py 按形状相关系数挑字体，再用 stroke_width 微调笔画粗细，
   最后缩放到目标墨迹框（缩放别超过原尺寸 5%，否则字形被拉坏）。
4. 写入按 alpha 混合，不是硬贴，边缘抗锯齿与原图一致。
5. 原图经 JPEG 压缩，边缘本来就钝。--blur 补一层高斯柔边，否则一眼看出是贴上去的。
   芯部颜色也要实测（与背景距离最大的前 3% 取中位数），别用"最暗像素"。

用法
----
python img_text_replace.py --img in.jpg --out out.jpg \
    --erase 760,1150,800,1200 --ink 762,1155,30,23 \
    --text 39 --font C:/Windows/Fonts/Deng.ttf --size 30 --stroke 5 \
    --fg 44,108,203 --blur 0.65 --subsampling 0
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

SS = 6  # 超采样倍数


def parse_ints(s):
    return [int(v) for v in s.replace(' ', '').split(',')]


def sample_bg(a, box, band=6):
    """在抹除框上下各取一条干净带，逐列取中位数，作为背景重建的上下端点。"""
    x0, y0, x1, y1 = box
    top = np.median(a[y0 - band:y0, x0:x1], axis=0)
    bot = np.median(a[y1:y1 + band, x0:x1], axis=0)
    return top, bot


def rebuild_bg(top, bot, h, w):
    """逐列在 top / bot 之间线性插值，保留原图的纵向渐变。"""
    t = np.linspace(0, 1, h)[:, None, None]
    return top[None, :, :] * (1 - t) + bot[None, :, :] * t


def render_alpha(text, font_path, size, stroke):
    """超采样渲染，返回裁到墨迹边界的 alpha 数组（0~1）。"""
    f = ImageFont.truetype(font_path, int(round(size * SS)))
    side = int(size * SS * 4)
    img = Image.new('L', (side, side), 0)
    ImageDraw.Draw(img).text(
        (int(size * SS), int(size * SS)), text, fill=255, font=f,
        stroke_width=stroke, stroke_fill=255)
    arr = np.asarray(img, dtype=float) / 255.0
    ys, xs = np.nonzero(arr > 0.02)
    return arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--img', required=True, help='源图（只读）')
    ap.add_argument('--out', help='输出图，默认 <原名>-edited.jpg')
    ap.add_argument('--erase', required=True, help='抹除框 x0,y0,x1,y1（右下开区间）')
    ap.add_argument('--ink', required=True, help='目标墨迹框 x,y,w,h')
    ap.add_argument('--text', required=True, help='新文字，如 29')
    ap.add_argument('--font', default='C:/Windows/Fonts/Deng.ttf')
    ap.add_argument('--size', type=float, default=32)
    ap.add_argument('--stroke', type=int, default=6, help='超采样下的描边宽度（SS=6 时 6 ≈ 1px/边）')
    ap.add_argument('--fg', default='48,110,194', help='文字颜色 r,g,b')
    ap.add_argument('--bg-band', type=int, default=6, help='上下取样带高度')
    ap.add_argument('--blur', type=float, default=0.0,
                    help='对 alpha 加高斯模糊（1x 像素），用于贴合原图的柔边')
    ap.add_argument('--subsampling', type=int, default=0,
                    help='JPEG 色度抽样：0=4:4:4 / 1=4:2:2 / 2=4:2:0（跟源图一致最自然）')
    ap.add_argument('--quality', type=int, default=96)
    args = ap.parse_args()

    src = Image.open(args.img).convert('RGB')
    a = np.asarray(src, dtype=float).copy()
    H, W = a.shape[:2]

    ex0, ey0, ex1, ey1 = parse_ints(args.erase)
    ix, iy, iw, ih = parse_ints(args.ink)
    fg = np.array([float(v) for v in args.fg.split(',')])

    # ② 取样 + ③ 遮除
    top, bot = sample_bg(a, (ex0, ey0, ex1, ey1), args.bg_band)
    a[ey0:ey1, ex0:ex1] = rebuild_bg(top, bot, ey1 - ey0, ex1 - ex0)
    erased = a[ey0:ey1, ex0:ex1].copy()  # 留一份抹除后的样子用于自检

    # ④ 写入：渲染 -> 缩放到目标墨迹框 -> alpha 混合
    alpha = render_alpha(args.text, args.font, args.size, args.stroke)
    alpha = np.asarray(
        Image.fromarray((alpha * 255).astype('uint8')).resize((iw, ih), Image.LANCZOS),
        dtype=float) / 255.0

    # ④' 柔边：原图经 JPEG 压缩，边缘过渡本来就钝；不补模糊会一眼看出是贴上去的
    if args.blur > 0:
        pad = int(np.ceil(args.blur * 3)) + 2
        canvas = np.zeros((ih + pad * 2, iw + pad * 2), dtype=float)
        canvas[pad:pad + ih, pad:pad + iw] = alpha
        img = Image.fromarray((canvas * 255).astype('uint8')).filter(
            ImageFilter.GaussianBlur(args.blur))
        canvas = np.asarray(img, dtype=float) / 255.0
        alpha = canvas[pad:pad + ih, pad:pad + iw]

    # ⑤ 光学对齐：墨迹左上角直接对齐目标框左上角
    hh = min(ih, H - iy)
    ww = min(iw, W - ix)
    al = alpha[:hh, :ww][:, :, None]
    region = a[iy:iy + hh, ix:ix + ww]
    a[iy:iy + hh, ix:ix + ww] = region * (1 - al) + fg * al

    out = np.clip(a, 0, 255).astype('uint8')
    dst = args.out or os.path.splitext(args.img)[0] + '-edited.jpg'
    Image.fromarray(out).save(dst, quality=args.quality, subsampling=args.subsampling)

    # ⑥ 校验
    print(f'源图      : {args.img}  {W}x{H}')
    print(f'抹除框    : ({ex0},{ey0})-({ex1},{ey1})')
    print(f'背景上下端: top≈{top.mean(axis=0).round(1)}  bot≈{bot.mean(axis=0).round(1)}')
    print(f'写入      : "{args.text}"  {os.path.basename(args.font)}  size={args.size} stroke={args.stroke}')
    print(f'墨迹框    : ({ix},{iy}) {iw}x{ih}')
    print(f'输出      : {dst}')

    # 自检：抹除后、写入前的区域里是否还有旧墨迹（蓝通道相对红通道的异常）
    blueness = (erased[:, :, 2] - erased[:, :, 0]).max()
    print(f'校验·抹除后蓝偏: {blueness:.1f}（原文字处 >120，>40 说明有残留）')


if __name__ == '__main__':
    main()
