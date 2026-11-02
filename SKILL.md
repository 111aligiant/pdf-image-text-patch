---
name: pdf-image-text-patch
description: 在 PDF 或位图截图里精准改写少量文字（表格字段、占位符、截图里的数字或短文本），要求字体、字号、基线、颜色与原内容严丝合缝。当需要替换 PDF 表格字段值或占位符、修改扫描件与截图里的文字、且改动不能被一眼看出时使用。关键词：PDF改字、替换字段、占位符、截图改字、改数字、字体反查、光学对齐、redaction。
agent_created: true
---

# PDF / 图片文字精准改写

改一两个字符，让改动融进原件——不是盖个白块写上去。

## 什么时候用

- PDF 表格里某字段要换成新值（编号、型号、日期、金额）
- 截图/照片里要改数字或短文本，周围文字必须原样不动
- 手上有"改前/改后"要对比，改动处经不起放大看

**不适用**：整段重写、换语言、重排版。那属于重新生成文档，用 docx/pdf 类技能。

## 第零步：先分流

| 载体 | 判断 | 走哪条路 |
|---|---|---|
| PDF，文字可选可搜 | `page.get_text()` 有内容 | **矢量路径**（redaction + 重新嵌入字体） |
| PDF，扫描件 | 抽不出文字 | 先转图，走**位图路径** |
| JPG / PNG / 截图 | —— | **位图路径** |

判定命令：`python scripts/pdf_field_replace.py --pdf x.pdf --value XXX --probe`
有输出且能定位到标签，就是矢量 PDF。

## 通用六步

```
① 定位  →  ② 取样  →  ③ 遮除  →  ④ 写入  →  ⑤ 光学对齐  →  ⑥ 校验
```

两条路径共用这个骨架，区别只在"像素"还是"文本对象"。细节见
`references/pdf-workflow.md` 和 `references/image-workflow.md`。

**贯穿全程的第一原则：只改必须改的那几个字符。**
要改 `12:34` 里的 `34`，就别碰 `12:`——锚点字原样保留，"像不像"由原图自己保证。
整段重画要同时赌中字形、字重、字距、基线，翻车概率高一个量级。

## 环境

```bash
python -c "import fitz, PIL, numpy; print('ok')"
```

缺库就装：`pip install pymupdf pillow numpy`。
注意有些环境里的托管解释器没装 PDF 库，换成系统解释器再试。

## 矢量路径（PDF）

```bash
# 1) 摸底：只定位不写入，打印标签/值/坐标/同列参照
python scripts/pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --probe

# 2) 精确对齐：--ink-left 给目标墨迹左缘（最可控，两遍求解 dx）
python scripts/pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --ink-left 225.15

# 3) 不知道目标位置时，按同列其他值自动对齐（只能"大致摆正"）
python scripts/pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --align-ink

# 4) 手动微调 / 只出 PDF
python scripts/pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --dx 1.1 --no-images
```

脚本已内置的关键决策，改之前先读懂，别退回旧做法：

- **redaction 真删**，不是盖白块。盖白块会留隐藏文字，复制/检索时新旧值并存。
  必须传 `images=NONE, graphics=NONE, text=REMOVE`，否则连表格线和底图一起干掉。
- **按 `span['origin']` 基线写入**，不是 bbox 左上角，保证与相邻字段同基线。
- **光学对齐**：bbox 左缘一致 ≠ 看着齐。首字符左侧留白差异很大
  （数字 `1` 约 1.33pt、`6` 约 0.38pt、字母 `P` 约 0.18pt）。字母开头一般要 `+0.8~1.2pt`。
- **字体真嵌入**（如 `times.ttf`），别用 base14 的 `tiro` 顶替，换机器会掉字。

## 位图路径（截图）

```bash
# 1) 字体反查：拿图里已有的字当参考答案，遍历系统字体打分
python scripts/img_font_match.py --img a.jpg --box 700,1150,820,1200 \
    --text "12:34" --target "39"

# 2) 按反查结果改写
python scripts/img_text_replace.py --img a.jpg --out b.jpg \
    --erase 760,1150,800,1200 \        # 抹除框（要改的那几个字的范围，含抗锯齿边）
    --ink 762,1155,30,23 \             # 目标墨迹框 x,y,w,h
    --text 39 --font C:/Windows/Fonts/Deng.ttf --size 30 --stroke 5 \
    --fg 44,108,203 --blur 0.65 --subsampling 0
```

四个必调参数，每个都有依据，别凭感觉给：

| 参数 | 怎么定 |
|---|---|
| `--font / --size / --stroke` | `img_font_match.py` 输出，**前 3 名都要渲染比一眼**（分数差 0.01 内属噪声） |
| `--fg` | 与背景欧氏距离最大的前 3% 像素取中位数。**别用"最暗像素"**，JPEG 过冲会让颜色偏灰 |
| `--blur` | 按梯度对齐扫。原图平均梯度约 0.29 时 `blur≈0.65`；`0` 太锐、`0.85` 比原图还糊 |
| `--ink` | 用反查给出的自然墨迹尺寸，缩放 **≤3%**；超 5% 字形会被各向异性拉伸搞坏 |

## 校验清单（两条路径都要过）

**矢量**
- [ ] 全文再搜不到旧值
- [ ] 能搜到新值，字体名不是 `tiro`/`helv`（base14 回退说明没嵌入）
- [ ] 页面能正常渲染，尺寸不变

**位图**
- [ ] 抹除后色偏 < 40（原文字处 >120，>40 说明有残留）
- [ ] 目标区外全图最大差异 < 15、均值 < 1（属 JPEG 重编码噪声）
- [ ] 峰值 alpha = 1.0（模糊没把字压淡）
- [ ] 8 倍放大逐字比对：笔画粗细、字高、基线、颜色、边缘柔度

## 交付习惯

- 源文件只读，**永不覆盖**。输出到独立目录。
- 附一张改前改后对比图，放大到能看清笔画。
- 中间产物（探针图、扫描对比图）用完即删，别留在工作区。

## 什么时候该停下来

处理第三方出具、带核验机制的文件（证书、票据、报告、合同）前，先说明：
改动可能破坏原件的可核验性，且多数情况下不可逆。
建议单独目录存放、文件名标明改动，不要覆盖原件。
不要默认用户已经想清楚这件事。

## 参考

- `references/pdf-workflow.md` — 矢量路径全量细节：锚定逻辑、redaction 参数、光学对齐量测
- `references/image-workflow.md` — 位图路径全量细节：字体打分、背景重建、柔边与颜色标定、尺寸微调
