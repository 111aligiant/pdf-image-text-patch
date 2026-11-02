# -*- coding: utf-8 -*-
"""
PDF 字段改写（矢量文字，原位替换）
====================================

把 PDF 里某个表格字段的值换掉（如占位符 ------ -> ABC123），
要求字体、字号、基线、视觉左边缘与相邻字段严格一致。

六步：定位 -> 取样 -> 遮除 -> 写入 -> 光学对齐 -> 校验

设计要点
--------
1. 不硬编码坐标：用标签 span（如「编 号」）作锚点，在同带区右侧找值单元格。
   换一份文件、换一页，只要表格结构一致就能自动定位。
2. 真正删除旧内容：用 redaction 从内容流移除旧文字，而不是盖白块。
   盖白块会留下隐藏文字，复制/检索时新旧值同时出现。
3. 保留背景：redaction 只删文字，不动图片和线条（表格线、底图水印都保留），
   填充色从原图采样，避免白块与浅灰底不匹配。
4. 字体真嵌入：加载系统字体文件，按原 span 的 baseline origin 写入。
5. **光学对齐**：bbox 左边缘对齐 != 看起来对齐。不同首字符的左侧留白不同
   （数字 1 约 1.33pt、6 约 0.38pt、字母 P 约 0.18pt）。只对 bbox 会视觉偏左。
   所以增加一遍「渲染 -> 量墨迹左缘 -> 回填 dx」的校正。
6. 输出后自动校验：旧文本消失、新文本存在、字体真嵌入、页面可渲染。

用法
----
# 先探一下能不能定位（不写文件）
python pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --probe

# 精确指定墨迹左缘（最可控，两遍求解 dx）
python pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --ink-left 225.15

# 按同列其他值的墨迹左缘自动对齐
python pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --align-ink

# 手动微调 / 只出 PDF
python pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --dx 1.1 --no-images
"""
from __future__ import annotations

import argparse
import io
import os
import re
import statistics
import sys

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\Times New Roman.ttf",
]
FALLBACK_FONTNAME = "tiro"  # base14 Times-Roman，度量与 Times New Roman 兼容但不嵌入


# ---------------------------------------------------------------- 工具函数
def norm(s: str) -> str:
    """去掉所有空白，用于标签比对（'编 号' -> '编号'）。"""
    return re.sub(r"\s+", "", s or "")


def rgb_to_fill(rgb) -> tuple:
    return tuple(c / 255.0 for c in rgb)


def sample_bg(page, rect, zoom=6):
    """采样矩形内出现最多的颜色，作为遮挡填充色。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    colors = im.getcolors(maxcolors=1 << 20) or [(0, (255, 255, 255))]
    colors.sort(reverse=True)
    return colors[0][1], colors[0][0] / max(1, im.width * im.height)


def measure_ink_left(page, rect, zoom=20, thresh=128):
    """渲染矩形，返回最左侧深色像素的 x 坐标（pt）。这是肉眼看到的文字左边缘。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    arr = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("L"))
    cols = np.where((arr < thresh).any(axis=0))[0]
    return rect.x0 + float(cols[0]) / zoom if len(cols) else None


def page_spans(page):
    """取出本页所有非空文字 span（含 origin 基线坐标）。"""
    out = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for line in blk["lines"]:
            for sp in line["spans"]:
                if sp["text"].strip():
                    out.append(sp)
    return out


def find_label_span(spans, label):
    """定位标签 span。优先精确匹配，退化为包含匹配。"""
    key = norm(label)
    exact = [s for s in spans if norm(s["text"]) == key]
    if exact:
        return exact[0]
    part = [s for s in spans if key in norm(s["text"])]
    return part[0] if part else None


def find_value_span(spans, label_span, y_window=30.0):
    """
    在标签同一行右侧找值 span。

    同行判定不能只看垂直居中对齐——表单里标签和值的基线常常差几个点
    （实测标签中心 600.7、值中心 612.3，差 11.6pt），所以用 ±y_window 的带状区域，
    并且优先取「最靠左」的那一列，避免误抓到右侧相邻列（如『发布日期』）。
    """
    lx1 = label_span["bbox"][2]
    lyc = (label_span["bbox"][1] + label_span["bbox"][3]) / 2

    band = []
    for s in spans:
        x0, y0, x1, y1 = s["bbox"]
        yc = (y0 + y1) / 2
        if x0 > lx1 + 1 and abs(yc - lyc) <= y_window:
            band.append(s)
    if not band:
        return None

    min_x0 = min(s["bbox"][0] for s in band)
    column = [s for s in band if abs(s["bbox"][0] - min_x0) <= 2.0]
    column.sort(key=lambda s: abs((s["bbox"][1] + s["bbox"][3]) / 2 - lyc))
    return column[0]


def sibling_values(spans, target):
    """同一列的其他值 span（x0 相同、字号相同、不同行），作为对齐参照。

    只取含 ASCII 字母数字的 span：中文标签/地址的墨迹左缘受字形影响极大，
    混进来会把中位数带偏。
    """
    x0t = target["bbox"][0]
    yct = (target["bbox"][1] + target["bbox"][3]) / 2
    sz = target["size"]
    out = []
    for s in spans:
        if s is target:
            continue
        if not re.search(r"[A-Za-z0-9]", s["text"]):
            continue
        x0, y0, x1, y1 = s["bbox"]
        yc = (y0 + y1) / 2
        if abs(x0 - x0t) <= 2.0 and abs(s["size"] - sz) <= 0.5 and abs(yc - yct) > 5:
            out.append(s)
    return out


def target_ink_left(page, siblings, zoom=20):
    """参照值的墨迹左缘中位数，作为光学对齐目标。

    取样带左边界只取 bbox.x0 - 1.5pt：再往左就会扫到单元格竖线，
    竖线会被误判成"墨迹"，把结果拽偏（实测会拽到 218.12pt）。
    """
    vals = []
    for s in siblings:
        x0, y0, x1, y1 = s["bbox"]
        ink = measure_ink_left(page, fitz.Rect(x0 - 1.5, y0 - 2, x1 + 4, y1 + 2), zoom)
        if ink is not None:
            vals.append(ink)
    return (statistics.median(vals) if vals else None), vals


def find_right_boundary(page, span):
    """找值单元格右侧的竖线，作为文本最大可用宽度；找不到则返回 None。"""
    x0, y0, x1, y1 = span["bbox"]
    yc = (y0 + y1) / 2
    best = None
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] != "l":
                continue
            p1, p2 = it[1], it[2]
            if abs(p1.x - p2.x) > 0.5:  # 非竖线
                continue
            if not (min(p1.y, p2.y) <= yc <= max(p1.y, p2.y)):
                continue
            if p1.x > x1 + 2:
                best = p1.x if best is None else min(best, p1.x)
    return best


def text_width(font, text, size):
    try:
        return font.text_length(text, fontsize=size)
    except Exception:
        return len(text) * size * 0.55


# ---------------------------------------------------------------- 核心步骤
def _apply_once(src_bytes, label, value, font_file, size_opt, dx):
    """在内存副本上做一次「定位->遮除->写入」，返回 (doc, 本次报告)。"""
    doc = fitz.open(stream=src_bytes, filetype="pdf")
    font = fitz.Font(fontfile=font_file) if font_file and os.path.exists(font_file) else None
    use_fontname, use_fontfile = ("TNR", font_file) if font else (FALLBACK_FONTNAME, None)

    report = {"pages": []}
    for pno, page in enumerate(doc, start=1):
        spans = page_spans(page)
        label_span = find_label_span(spans, label)
        if label_span is None:
            report["pages"].append({"page": pno, "status": "未找到标签"})
            continue
        value_span = find_value_span(spans, label_span)
        if value_span is None:
            report["pages"].append({"page": pno, "status": "未找到值单元格"})
            continue

        old_text = value_span["text"]
        if norm(old_text) == norm(value):
            report["pages"].append({"page": pno, "status": "已是目标值，跳过", "old": old_text})
            continue

        rect = fitz.Rect(value_span["bbox"])
        ox, oy = value_span["origin"]        # 基线原点，保证垂直对齐
        base_size = size_opt or value_span["size"]
        bg_rgb, bg_ratio = sample_bg(page, rect)

        # 遮除旧内容（只删文字，保留图片与线条）
        pad = 1.0
        page.add_redact_annot(
            fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad),
            fill=rgb_to_fill(bg_rgb),
        )
        page.apply_redactions(
            images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
            graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 1),
            text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
        )

        # 字号（默认沿用原字号，超宽则等比缩小）
        size = base_size
        if font is not None:
            right = find_right_boundary(page, value_span)
            avail = (right - 6 - ox) if right else 120.0
            w = text_width(font, value, size)
            if w > avail > 0:
                size = max(6.0, size * avail / w)

        # 写入新值（x 加光学偏移 dx）
        page.insert_text(
            fitz.Point(ox + dx, oy), value,
            fontname=use_fontname, fontfile=use_fontfile,
            fontsize=size, color=(0, 0, 0), render_mode=0,
        )

        report["pages"].append({
            "page": pno, "status": "已替换", "old": old_text, "new": value,
            "old_bbox": tuple(round(v, 2) for v in rect),
            "baseline": (round(ox + dx, 2), round(oy, 2)),
            "size": round(size, 2), "bg": bg_rgb, "bg_ratio": round(bg_ratio, 3),
            "embedded": bool(font),
        })
    return doc, report


def replace_field(pdf_path, label, value, font_file, size_opt, out_pdf,
                  dx=0.0, align_ink=False, ink_left=None, log=print):
    src_bytes = open(pdf_path, "rb").read()

    # 第一遍：按 bbox 原位写入
    doc, report = _apply_once(src_bytes, label, value, font_file, size_opt, dx)
    done = [p for p in report["pages"] if p["status"] == "已替换"]
    if not done:
        doc.close()
        return None, report, None

    ink_info = None
    if align_ink or ink_left is not None:
        first = done[0]

        if ink_left is not None:
            want_ink, sib_inks = float(ink_left), []
        else:
            # 参照值取自原始文档（未受 redaction 影响）
            src = fitz.open(stream=src_bytes, filetype="pdf")
            sp = src[first["page"] - 1]
            spans = page_spans(sp)
            tgt = find_value_span(spans, find_label_span(spans, label))
            sibs = sibling_values(spans, tgt)
            want_ink, sib_inks = target_ink_left(sp, sibs)
            src.close()

        # 量第一遍结果的实际墨迹左缘
        r = fitz.Rect(first["old_bbox"])
        band = fitz.Rect(r.x0 - 1.5, r.y0 - 3, r.x1 + 40, r.y1 + 3)
        got_ink = measure_ink_left(doc[first["page"] - 1], band)
        doc.close()

        if want_ink is not None and got_ink is not None:
            corr = want_ink - got_ink
            ink_info = {
                "参照墨迹左缘": [round(v, 2) for v in sib_inks],
                "目标墨迹左缘": round(want_ink, 2),
                "校正前墨迹左缘": round(got_ink, 2),
                "修正量": round(corr, 2),
            }
            log(f"  光学对齐: 目标墨迹左缘 {want_ink:.2f}pt, "
                f"当前 {got_ink:.2f}pt, 偏移 {corr:+.2f}pt")
            # 第二遍：从原始文件重来，带上修正量（不叠加）
            doc, report = _apply_once(src_bytes, label, value, font_file, size_opt, dx + corr)
        else:
            doc, report = _apply_once(src_bytes, label, value, font_file, size_opt, dx)

    return _finish(doc, out_pdf), report, ink_info


def _finish(doc, out_pdf):
    doc.save(out_pdf, garbage=4, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
    doc.close()
    return out_pdf


def probe(pdf_path, label):
    """只定位不写入，打印每页命中的标签/值/坐标，用于陌生 PDF 的摸底。"""
    doc = fitz.open(pdf_path)
    print(f"文件 : {os.path.basename(pdf_path)}  {doc.page_count} 页")
    print(f"加密 : {doc.is_encrypted}  needs_pass={doc.needs_pass}")
    hit = 0
    for pno, page in enumerate(doc, start=1):
        spans = page_spans(page)
        label_span = find_label_span(spans, label)
        if label_span is None:
            continue
        value_span = find_value_span(spans, label_span)
        hit += 1
        print(f"\n第 {pno} 页  页面尺寸 {page.rect.width:.1f}x{page.rect.height:.1f}pt")
        print(f"  标签 {label_span['text']!r}  bbox={tuple(round(v,2) for v in label_span['bbox'])}")
        if value_span is None:
            print("  !! 未找到值单元格")
            continue
        print(f"  值   {value_span['text']!r}  bbox={tuple(round(v,2) for v in value_span['bbox'])}")
        print(f"       字号 {value_span['size']:.2f}  字体 {value_span['font']}  "
              f"origin=({value_span['origin'][0]:.2f},{value_span['origin'][1]:.2f})")
        sibs = sibling_values(spans, value_span)
        if sibs:
            inks = []
            for s in sibs:
                x0, y0, x1, y1 = s["bbox"]
                v = measure_ink_left(page, fitz.Rect(x0 - 1.5, y0 - 2, x1 + 4, y1 + 2))
                inks.append((s["text"], round(v, 2) if v else None))
            print(f"  同列参照(文本, 墨迹左缘): {inks}")
    doc.close()
    if not hit:
        print(f"\n!! 全篇没有找到标签 {label!r}，检查 --label 或该 PDF 是否为扫描件")
        return 1
    return 0


def verify(pdf_path, old_text, value, page_no=1):
    """输出后校验：旧值消失、新值存在、字体真嵌入、页面可渲染。"""
    doc = fitz.open(pdf_path)
    page = doc[page_no - 1]
    spans = page_spans(page)
    texts = [s["text"] for s in spans]
    hit = [s for s in spans if value in s["text"]]

    font_name = hit[0]["font"] if hit else ""
    checks = {
        "旧值已移除": not any(norm(t) == norm(old_text) for t in texts),
        "新值已写入": bool(hit),
        # base14 回退（tiro/Helv）没有嵌入，换机器可能显示成别的字体
        "字体已嵌入": bool(font_name) and font_name.lower() not in ("tiro", "helv", "cour"),
        "页面可正常渲染": page.get_pixmap(matrix=fitz.Matrix(2, 2)).width > 0,
    }
    if hit:
        checks["新值bbox"] = tuple(round(v, 2) for v in hit[0]["bbox"])
        checks["新值字体名"] = font_name
        checks["新值字号"] = round(hit[0]["size"], 2)
    doc.close()
    return checks


def export_images(pdf_path, outdir, stem, dpi=600):
    """整页导出 PNG（完整页面，不裁剪）。"""
    os.makedirs(outdir, exist_ok=True)
    doc = fitz.open(pdf_path)
    zoom = dpi / 72.0
    files = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        exp_w, exp_h = round(page.rect.width * zoom), round(page.rect.height * zoom)
        ok = abs(pix.width - exp_w) <= 1 and abs(pix.height - exp_h) <= 1
        path = os.path.join(outdir, f"{stem}_第{i:02d}页.png")
        pix.save(path)
        files.append((path, pix.width, pix.height, ok))
    doc.close()
    return files


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="PDF 表格字段原位替换（保留字体/基线/光学对齐）")
    ap.add_argument("--pdf", required=True, help="源 PDF（只读，不会被改）")
    ap.add_argument("--value", required=True, help="要写入的新值，如 ABC123")
    ap.add_argument("--label", default="编号", help="锚点标签文字，如 编号 / 型号 / 日期")
    ap.add_argument("--font", default=None, help="字体文件，默认自动找 times.ttf")
    ap.add_argument("--size", type=float, default=None, help="字号，默认沿用原 span 字号")
    ap.add_argument("--dx", type=float, default=0.0, help="水平微调（pt），正数右移")
    ap.add_argument("--align-ink", action="store_true",
                    help="按同列其他值的墨迹左缘自动光学对齐（会覆盖 --dx 的初始值）")
    ap.add_argument("--ink-left", type=float, default=None,
                    help="精确指定新值的墨迹左缘坐标（pt），两遍求解 dx，优先于 --align-ink")
    ap.add_argument("--probe", action="store_true", help="只定位不写入，用于摸底")
    ap.add_argument("--outdir", default=None, help="输出目录")
    ap.add_argument("--dpi", type=int, default=600, help="导出 PNG 的 DPI")
    ap.add_argument("--no-images", action="store_true", help="只输出 PDF，不导图片")
    args = ap.parse_args(argv)

    if args.probe:
        return probe(args.pdf, args.label)

    font_file = args.font or next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    stem_src = os.path.splitext(os.path.basename(args.pdf))[0]
    stem = re.sub(r"[-_\s]+$", "", stem_src).strip()
    outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(args.pdf)),
                                         f"{stem}-{args.value}")
    os.makedirs(outdir, exist_ok=True)
    out_pdf = os.path.join(outdir, f"{stem}-{args.value}.pdf")

    print(f"源文件 : {os.path.basename(args.pdf)}")
    print(f"字体   : {font_file or '(base14 Times-Roman 回退，未嵌入)'}")
    print(f"目标值 : {args.value}")

    out_pdf, rep, ink = replace_field(
        args.pdf, args.label, args.value, font_file, args.size, out_pdf,
        dx=args.dx, align_ink=args.align_ink, ink_left=args.ink_left,
    )
    for p in rep["pages"]:
        if p["status"] == "已替换":
            print(f"  第{p['page']}页  替换 {p['old']!r} -> {p['new']!r}  "
                  f"基线{p['baseline']} 字号{p['size']} 背景{p['bg']}")
        else:
            print(f"  第{p['page']}页  {p['status']}")

    done = [p for p in rep["pages"] if p["status"] == "已替换"]
    if not done:
        print("没有任何页面被修改。")
        return 2

    first = done[0]
    checks = verify(out_pdf, first["old"], args.value, first["page"])
    print("\n校验:")
    for k, v in checks.items():
        print(f"  {k}: {v}")
    bad = [k for k in ("旧值已移除", "新值已写入", "字体已嵌入", "页面可正常渲染")
           if not checks.get(k)]
    if bad:
        print(f"  !! 未通过: {bad}")
        return 3

    print(f"\n输出 PDF: {out_pdf}")
    if not args.no_images:
        files = export_images(out_pdf, outdir, f"{stem}-{args.value}", args.dpi)
        print(f"输出图片: {len(files)} 张 @{args.dpi}dpi")
        for path, w, h, ok in files:
            print(f"  {os.path.basename(path)}  {w}x{h}  {'整页完整' if ok else '尺寸异常'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
