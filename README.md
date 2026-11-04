# pdf-image-text-patch

在 PDF、扫描件或截图里精准改写少量文字，让改动融进原件——不是盖个白块写上去。

适用于修正表单字段、替换占位符、修补截图里的错字或错误数字。目标是字体、字号、
基线、颜色、边缘柔度都和原件对齐，放大 8 倍也看不出接缝。

三条路径，按载体分流：

| 载体 | 走哪条 |
|---|---|
| PDF，文字可选可搜 | 矢量路径：redaction 真删 + 字体嵌入 |
| 扫描件 / 截图，灰度或彩色 | 位图路径：逐列重建背景 + alpha 混合 + 柔边标定 |
| **扫描件，纯二值（1bit）** | 二值路径：1bit 进 1bit 出，靠相位对齐字形 |

## 它解决什么问题

改字容易，改得"像"难。四个地方最容易翻车：

1. **旧内容没真删**——盖白块只是视觉遮挡，复制粘贴时新旧两套值并存。
   这里用 redaction 把旧文字从内容流里真正移除。
2. **bbox 对齐了但看着不齐**——不同字符的左侧留白差异可达 1.3pt，
   数字 `1` 最宽、字母 `P` 几乎贴边。所以要按墨迹边缘对齐，不是按 bbox。
3. **边缘太锐、颜色偏灰**——一眼看出是贴上去的。这里按梯度标定柔边，
   按"离背景最远的前 3% 像素"取文字色。
4. **往二值图里塞灰度字**——黑白扫描件没有过渡像素，带抗锯齿的新字放大就是补丁。
   二值路径反过来做：超采样渲染 → 面积平均 → 阈值 0.5，并搜索亚像素相位。

## 四个脚本

| 脚本 | 用途 |
|---|---|
| `scripts/pdf_field_replace.py` | 矢量 PDF 字段替换：redaction 真删 + 字体嵌入 + 光学对齐 |
| `scripts/img_text_replace.py` | 位图局部改写：逐列重建背景 + alpha 混合 + 柔边标定 |
| `scripts/img_font_match.py` | 字体反查：从图里已有的字反推字体 / 字号 / 字重 |
| `scripts/binary_scan_patch.py` | 二值扫描件改写：1bit 栅格直改，含字体反查与逐像素校验 |

`img_font_match.py` 会遍历系统全部字体打分，先按字高解字号再比形状，
几百个字体约 8 秒。`binary_scan_patch.py --fit` 同样按字高定标，
19 个字体连同相位精修约 3 秒。分数只做候选筛选，前 3 名仍需肉眼确认。

## 安装

```bash
git clone https://github.com/111aligiant/pdf-image-text-patch.git \
  ~/.workbuddy/skills/pdf-image-text-patch
```

依赖：

```bash
pip install pymupdf pillow numpy
```

## 快速上手

```bash
# PDF：先摸底，确认能不能定位（只读不写）
python scripts/pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --probe

# PDF：精确对齐后写入
python scripts/pdf_field_replace.py --pdf a.pdf --label 编号 --value ABC123 --ink-left 225.15

# 图片：反查字体
python scripts/img_font_match.py --img a.jpg --box 120,1140,210,1180 \
    --text "12:34" --target "39"

# 图片：改写
python scripts/img_text_replace.py --img a.jpg --out b.jpg \
    --erase 170,1145,205,1180 --ink 171,1150,30,22 \
    --text 39 --font C:/Windows/Fonts/Deng.ttf --size 30 --stroke 5 \
    --fg 44,108,203 --blur 0.65

# 扫描件：先确认是不是纯二值
python scripts/binary_scan_patch.py --pdf a.pdf --probe

# 扫描件：切字 + 反查字体（约 3 秒）
python scripts/binary_scan_patch.py --pdf a.pdf --box 1085,385,1330,430 \
    --ref-text "2026年1月15日" --fit-chars 206 --text 28 --fit

# 扫描件：改写
python scripts/binary_scan_patch.py --pdf a.pdf --out b.pdf \
    --erase 1252,390,1292,424 --text 28 \
    --font C:/Windows/Fonts/simfang.ttf --size 29 --stroke 2 --phase 6,2 \
    --ink-left 1270 --baseline 416
```

## 六步法

```
定位  →  取样  →  遮除  →  写入  →  光学对齐  →  校验
```

三条路径共用这个骨架，区别只在操作对象是"像素"、"文本对象"还是"bit"。

- 完整流程与调参依据：`SKILL.md`
- 矢量路径细节：`references/pdf-workflow.md`
- 位图路径细节：`references/image-workflow.md`
- 二值路径细节：`references/binary-scan-workflow.md`

## 边界

这是文档编辑工具，不是伪造工具。

改动第三方出具、带核验机制的文件（证书、票据、合同）会破坏其可核验性，
且通常不可逆。动手前请确认你有权修改，并始终保留原件、另存输出。

## License

MIT
