#!/usr/bin/env python3
"""把 Markdown 转成真正的 Word（.docx）文档 —— 不依赖 python-docx / pandoc。

为什么要自己写
--------------
板子上没装 `python-docx`，也没有 pandoc；LibreOffice 虽然能转 docx，
但**走 HTML 中间格式时会把本地图片全部丢掉**（试过 `file://` 绝对路径和
相对路径都不行），而原理讲解里恰恰需要插图。

所以这里直接生成 OOXML：一个 .docx 就是一个 zip，里面放
`word/document.xml` 等几个部件。自己拼 XML 的好处是：
  · 零依赖（只用标准库 + Pillow 量图片尺寸）
  · 图片、表格、代码块、分页都能精确控制
  · 结果是确定性的，可重复生成

支持的 Markdown 子集（够写技术文档了）
-------------------------------------
    # / ## / ### / ####      标题（映射到 Word 的 Heading 1~4）
    - 或 *                    无序列表
    1.                        有序列表
    > 引用                    引用块
    | a | b |                 表格（第二行是 |---|---| 分隔行）
    ```代码```                代码块（等宽字体 + 灰底）
    **粗体**、`等宽`          行内格式
    ![说明](图片路径)          插图（自动缩放到页宽内）
    <!-- pagebreak -->        强制分页
    ---                       分隔线（转成空段落，避免 Word 里出现横线乱版）

用法::

    python3 tools/make_docx.py docs/关键原理讲解-教学版.md
    python3 tools/make_docx.py 输入.md -o 输出.docx
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

# ── 版面常量 ──────────────────────────────────────────────────────────
#: 1 英寸 = 914400 EMU；图片按 96 dpi 折算
EMU_PER_PX = 9525
#: A4 页宽 21cm，左右各 2.2cm 边距 → 可用宽度约 16.6cm
MAX_IMAGE_WIDTH_CM = 15.5
CM_TO_EMU = 360000

FONT_BODY = "Noto Sans CJK SC"
FONT_BODY_FALLBACK = "微软雅黑"
FONT_MONO = "DejaVu Sans Mono"
FONT_MONO_CJK = "Noto Sans Mono CJK SC"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="jpeg" ContentType="image/jpeg"/>
  <Default Extension="jpg" ContentType="image/jpeg"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>
"""

CORE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{title}</dc:title>
  <dc:creator>SmartClock 视觉端项目</dc:creator>
  <cp:lastModifiedBy>SmartClock 视觉端项目</cp:lastModifiedBy>
</cp:coreProperties>
"""


def _styles_xml() -> str:
    """最小但够用的样式表：正文 + 4 级标题 + 代码 + 表格 + 图注。"""

    def heading(idx: int, size_half_pt: int, color: str,
                before: int, after: int) -> str:
        return f"""
  <w:style w:type="paragraph" w:styleId="Heading{idx}">
    <w:name w:val="heading {idx}"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="{before}" w:after="{after}" w:line="300" w:lineRule="auto"/>
      <w:outlineLvl w:val="{idx - 1}"/>
    </w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="{FONT_BODY}" w:hAnsi="{FONT_BODY}" w:eastAsia="{FONT_BODY}"/>
      <w:b/>
      <w:color w:val="{color}"/>
      <w:sz w:val="{size_half_pt}"/>
      <w:szCs w:val="{size_half_pt}"/>
    </w:rPr>
  </w:style>"""

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="{FONT_BODY}" w:hAnsi="{FONT_BODY}" w:eastAsia="{FONT_BODY}" w:cs="{FONT_BODY}"/>
        <w:sz w:val="21"/>
        <w:szCs w:val="21"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr>
        <w:spacing w:after="120" w:line="320" w:lineRule="auto"/>
      </w:pPr>
    </w:pPrDefault>
  </w:docDefaults>

  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:spacing w:before="0" w:after="240" w:line="360" w:lineRule="auto"/>
      <w:jc w:val="center"/>
    </w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="{FONT_BODY}" w:hAnsi="{FONT_BODY}" w:eastAsia="{FONT_BODY}"/>
      <w:b/><w:color w:val="1A3D6D"/><w:sz w:val="44"/><w:szCs w:val="44"/>
    </w:rPr>
  </w:style>
{heading(1, 34, "1A3D6D", 360, 180)}
{heading(2, 28, "26527F", 300, 140)}
{heading(3, 24, "2F6690", 240, 120)}
{heading(4, 22, "3A7CA5", 200, 100)}

  <w:style w:type="paragraph" w:styleId="CodeBlock">
    <w:name w:val="Code Block"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:shd w:val="clear" w:color="auto" w:fill="F4F5F7"/>
      <w:spacing w:before="0" w:after="0" w:line="260" w:lineRule="auto"/>
      <w:ind w:left="120" w:right="120"/>
      <w:contextualSpacing/>
    </w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="{FONT_MONO}" w:hAnsi="{FONT_MONO}" w:eastAsia="{FONT_MONO_CJK}"/>
      <w:sz w:val="18"/><w:szCs w:val="18"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="CodeFirst">
    <w:name w:val="Code First"/>
    <w:basedOn w:val="CodeBlock"/>
    <w:pPr>
      <w:shd w:val="clear" w:color="auto" w:fill="F4F5F7"/>
      <w:spacing w:before="120" w:after="0" w:line="260" w:lineRule="auto"/>
      <w:ind w:left="120" w:right="120"/>
    </w:pPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="CodeLast">
    <w:name w:val="Code Last"/>
    <w:basedOn w:val="CodeBlock"/>
    <w:pPr>
      <w:shd w:val="clear" w:color="auto" w:fill="F4F5F7"/>
      <w:spacing w:before="0" w:after="160" w:line="260" w:lineRule="auto"/>
      <w:ind w:left="120" w:right="120"/>
    </w:pPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Quote">
    <w:name w:val="Quote"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:pBdr>
        <w:left w:val="single" w:sz="18" w:space="8" w:color="8FB8DE"/>
      </w:pBdr>
      <w:ind w:left="200"/>
      <w:spacing w:before="80" w:after="160"/>
    </w:pPr>
    <w:rPr><w:i/><w:color w:val="3F4A56"/></w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Caption2">
    <w:name w:val="Figure Caption"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:jc w:val="center"/>
      <w:spacing w:before="40" w:after="200"/>
    </w:pPr>
    <w:rPr><w:color w:val="5A6472"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="ListParagraph">
    <w:name w:val="List Paragraph"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="420" w:hanging="220"/></w:pPr>
  </w:style>

  <w:style w:type="table" w:default="1" w:styleId="TableGrid">
    <w:name w:val="Table Grid"/>
    <w:tblPr>
      <w:tblBorders>
        <w:top w:val="single" w:sz="6" w:color="9AA5B1"/>
        <w:left w:val="single" w:sz="6" w:color="9AA5B1"/>
        <w:bottom w:val="single" w:sz="6" w:color="9AA5B1"/>
        <w:right w:val="single" w:sz="6" w:color="9AA5B1"/>
        <w:insideH w:val="single" w:sz="4" w:color="C3CBD5"/>
        <w:insideV w:val="single" w:sz="4" w:color="C3CBD5"/>
      </w:tblBorders>
      <w:tblCellMar>
        <w:top w:w="60" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/>
        <w:left w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/>
      </w:tblCellMar>
    </w:tblPr>
  </w:style>
</w:styles>
"""


NUMBERING_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="●"/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:ind w:left="420" w:hanging="220"/></w:pPr>
      <w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>
    </w:lvl>
    <w:lvl w:ilvl="1">
      <w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="○"/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:ind w:left="840" w:hanging="220"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:ind w:left="420" w:hanging="220"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>
"""


# ──────────────────────────────────────────────────────────────────────
# 行内格式：**粗体**、`等宽`、*斜体*
# ──────────────────────────────────────────────────────────────────────

INLINE_RE = re.compile(
    r"(\*\*.+?\*\*|`[^`]+`|\*[^*\n]+?\*)",
    re.S,
)


def _run(text: str, *, bold: bool = False, mono: bool = False,
         italic: bool = False, color: str | None = None) -> str:
    props = []
    if mono:
        props.append(
            f'<w:rFonts w:ascii="{FONT_MONO}" w:hAnsi="{FONT_MONO}" '
            f'w:eastAsia="{FONT_MONO_CJK}"/>'
        )
        props.append('<w:shd w:val="clear" w:color="auto" w:fill="F2F3F5"/>')
        props.append('<w:sz w:val="19"/><w:szCs w:val="19"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return (f'<w:r>{rpr}<w:t xml:space="preserve">'
            f"{escape(text)}</w:t></w:r>")


def inline_runs(text: str, bold: bool = False, italic: bool = False) -> str:
    """把一行 Markdown 行内格式转成若干 ``w:r``。

    **递归**处理嵌套：``**注意 `cxcywh` 不是 `xyxy`**`` 里粗体套着代码，
    只做一层匹配的话反引号会被当成普通字符原样画出来。
    """
    out: list[str] = []
    pos = 0
    for match in INLINE_RE.finditer(text):
        if match.start() > pos:
            out.append(_run(text[pos:match.start()], bold=bold, italic=italic))
        piece = match.group(0)
        if piece.startswith("**") and len(piece) > 4:
            out.append(inline_runs(piece[2:-2], bold=True, italic=italic))
        elif piece.startswith("`") and len(piece) > 2:
            out.append(_run(piece[1:-1], mono=True, bold=bold, italic=italic))
        elif piece.startswith("*") and len(piece) > 2:
            out.append(inline_runs(piece[1:-1], bold=bold, italic=True))
        pos = match.end()
    if pos < len(text):
        out.append(_run(text[pos:], bold=bold, italic=italic))
    return "".join(out) or _run("")


def paragraph(text: str, style: str | None = None,
              number_id: int | None = None, level: int = 0) -> str:
    ppr = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if number_id is not None:
        ppr.append(
            f'<w:numPr><w:ilvl w:val="{level}"/>'
            f'<w:numId w:val="{number_id}"/></w:numPr>'
        )
    ppr_xml = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr_xml}{inline_runs(text)}</w:p>"


def code_paragraph(line: str, first: bool, last: bool) -> str:
    style = "CodeFirst" if first else ("CodeLast" if last else "CodeBlock")
    body = _run(line if line else " ", mono=True)
    return (f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{body}</w:p>')


def image_paragraph(rel_id: str, width_emu: int, height_emu: int,
                    index: int, name: str) -> str:
    drawing = f"""<w:r><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0">
  <wp:extent cx="{width_emu}" cy="{height_emu}"/>
  <wp:effectExtent l="0" t="0" r="0" b="0"/>
  <wp:docPr id="{index}" name="{escape(name)}"/>
  <wp:cNvGraphicFramePr/>
  <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
    <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
      <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
        <pic:nvPicPr>
          <pic:cNvPr id="{index}" name="{escape(name)}"/>
          <pic:cNvPicPr/>
        </pic:nvPicPr>
        <pic:blipFill>
          <a:blip r:embed="{rel_id}"/>
          <a:stretch><a:fillRect/></a:stretch>
        </pic:blipFill>
        <pic:spPr>
          <a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </pic:spPr>
      </pic:pic>
    </a:graphicData>
  </a:graphic>
</wp:inline>
</w:drawing></w:r>"""
    return ('<w:p><w:pPr><w:jc w:val="center"/>'
            '<w:spacing w:before="160" w:after="40"/></w:pPr>'
            f"{drawing}</w:p>")


def table_xml(rows: list[list[str]], widths: list[int]) -> str:
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    body: list[str] = []
    for r_index, row in enumerate(rows):
        cells = []
        for c_index, cell in enumerate(row):
            width = widths[c_index] if c_index < len(widths) else widths[-1]
            shade = ('<w:shd w:val="clear" w:color="auto" w:fill="E8EEF6"/>'
                     if r_index == 0 else "")
            text = inline_runs(cell)
            if r_index == 0:
                text = _run(cell, bold=True)
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}'
                f'<w:vAlign w:val="center"/></w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:before="20" w:after="20"/>'
                f"</w:pPr>{text}</w:p></w:tc>"
            )
        trpr = ('<w:trPr><w:tblHeader/></w:trPr>' if r_index == 0 else "")
        body.append(f"<w:tr>{trpr}{''.join(cells)}</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="5000" w:type="pct"/>'
        '<w:tblLayout w:type="fixed"/></w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl>"
        '<w:p><w:pPr><w:spacing w:after="160"/></w:pPr></w:p>'
    )


def page_break() -> str:
    return ('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')


def horizontal_rule() -> str:
    return ('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" '
            'w:space="1" w:color="B9C2CC"/></w:pBdr>'
            '<w:spacing w:before="60" w:after="160"/></w:pPr></w:p>')


# ──────────────────────────────────────────────────────────────────────
# Markdown → 文档模型
# ──────────────────────────────────────────────────────────────────────


class DocxBuilder:
    def __init__(self, base_dir: Path, title: str) -> None:
        self.base_dir = base_dir
        self.title = title
        self.body: list[str] = []
        self.images: list[tuple[str, bytes]] = []
        self._img_index = 0

    # ── 图片 ──
    def _add_image(self, src: str, caption: str) -> None:
        path = (self.base_dir / src).resolve()
        if not path.exists():
            # 相对路径也可能相对当前工作目录写的，兜一下
            path = (Path.cwd() / src).resolve()
        if not path.exists():
            self.body.append(paragraph(f"[图片缺失：{src}]"))
            return
        try:
            from PIL import Image
        except ImportError:                       # pragma: no cover
            self.body.append(paragraph(f"[未安装 Pillow，无法嵌入图片：{src}]"))
            return

        with Image.open(path) as probe:
            px_w, px_h = probe.size
        max_w = int(MAX_IMAGE_WIDTH_CM * CM_TO_EMU)
        width = min(px_w * EMU_PER_PX, max_w)
        height = int(width * px_h / px_w)

        self._img_index += 1
        rel_id = f"rIdImg{self._img_index}"
        self.images.append((rel_id, path.read_bytes(),
                            path.suffix.lstrip(".").lower() or "png"))
        self.body.append(image_paragraph(rel_id, width, height,
                                         self._img_index, path.name))
        if caption:
            self.body.append(paragraph(caption, style="Caption2"))

    # ── 主解析 ──
    def parse(self, text: str) -> None:
        lines = text.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()

            # 分页
            if stripped in ("<!-- pagebreak -->", "<!--pagebreak-->"):
                self.body.append(page_break())
                index += 1
                continue

            # 代码块
            if stripped.startswith("```"):
                index += 1
                block: list[str] = []
                while index < len(lines) and \
                        not lines[index].strip().startswith("```"):
                    block.append(lines[index])
                    index += 1
                index += 1
                if not block:
                    block = [""]
                for offset, code_line in enumerate(block):
                    self.body.append(code_paragraph(
                        code_line, offset == 0, offset == len(block) - 1))
                continue

            # 表格
            if stripped.startswith("|") and index + 1 < len(lines) and \
                    re.match(r"^\|[\s:|-]+\|$", lines[index + 1].strip()):
                header = self._split_row(stripped)
                index += 2
                rows = [header]
                while index < len(lines) and lines[index].strip().startswith("|"):
                    rows.append(self._split_row(lines[index].strip()))
                    index += 1
                self.body.append(table_xml(rows, self._column_widths(header, rows)))
                continue

            # 标题
            match = re.match(r"^(#{1,4})\s+(.*)$", stripped)
            if match:
                level, text_value = len(match.group(1)), match.group(2)
                style = "Title" if level == 1 else f"Heading{level - 1}"
                self.body.append(paragraph(text_value, style=style))
                index += 1
                continue

            # 分隔线
            if stripped in ("---", "***", "___"):
                self.body.append(horizontal_rule())
                index += 1
                continue

            # 引用
            if stripped.startswith(">"):
                self.body.append(paragraph(stripped.lstrip("> ").strip(),
                                           style="Quote"))
                index += 1
                continue

            # 图片（单独一行）
            image_match = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", stripped)
            if image_match:
                self._add_image(image_match.group(2).strip(),
                                image_match.group(1).strip())
                index += 1
                continue

            # 无序列表
            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            if bullet:
                level = 1 if len(bullet.group(1)) >= 2 else 0
                self.body.append(paragraph(bullet.group(2),
                                           number_id=1, level=level))
                index += 1
                continue

            # 有序列表
            ordered = re.match(r"^(\s*)\d+[.)]\s+(.*)$", line)
            if ordered:
                self.body.append(paragraph(ordered.group(2), number_id=2))
                index += 1
                continue

            # 空行
            if not stripped:
                index += 1
                continue

            # 普通段落（连续行合并）
            buffer = [stripped]
            index += 1
            while index < len(lines):
                nxt = lines[index].strip()
                if (not nxt or nxt.startswith(("#", "|", ">", "```", "!["))
                        or re.match(r"^[-*+]\s", nxt)
                        or re.match(r"^\d+[.)]\s", nxt)
                        or nxt in ("---", "***", "___")):
                    break
                buffer.append(nxt)
                index += 1
            self.body.append(paragraph(" ".join(buffer)))

    @staticmethod
    def _split_row(row: str) -> list[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [cell.strip() for cell in row.split("|")]

    @staticmethod
    def _column_widths(header: list[str], rows: list[list[str]]) -> list[int]:
        """按各列最长内容粗略分配宽度（总宽 9000 dxa 左右）。"""
        total_dxa = 9000
        columns = max(len(r) for r in rows)
        weights = []
        for c in range(columns):
            longest = 0
            for row in rows:
                if c < len(row):
                    # 中文字符按 2 个宽度算
                    longest = max(longest, sum(
                        2 if ord(ch) > 127 else 1 for ch in row[c]))
            weights.append(max(longest, 4))
        weight_sum = sum(weights) or 1
        widths = [max(700, int(total_dxa * w / weight_sum)) for w in weights]
        # 归一化，避免累计误差把表格撑出页面
        scale = total_dxa / sum(widths)
        return [int(w * scale) for w in widths]

    # ── 输出 ──
    def save(self, path: Path) -> None:
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            "<w:body>"
            + "".join(self.body)
            + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
              '<w:pgMar w:top="1247" w:right="1247" w:bottom="1247" '
              'w:left="1247" w:header="851" w:footer="992" w:gutter="0"/>'
              "</w:sectPr>"
            "</w:body></w:document>"
        )

        rels = [
            '<Relationship Id="rIdStyles" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>',
            '<Relationship Id="rIdNumbering" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
            'Target="numbering.xml"/>',
        ]
        for rel_id, _data, _ext in self.images:
            rels.append(
                f'<Relationship Id="{rel_id}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/{rel_id}.png"/>'
            )
        doc_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + "</Relationships>"
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", CONTENT_TYPES)
            zf.writestr("_rels/.rels", ROOT_RELS)
            zf.writestr("docProps/core.xml",
                        CORE_XML.format(title=escape(self.title)))
            zf.writestr("word/document.xml", document)
            zf.writestr("word/styles.xml", _styles_xml())
            zf.writestr("word/numbering.xml", NUMBERING_XML)
            zf.writestr("word/_rels/document.xml.rels", doc_rels)
            for rel_id, data, _ext in self.images:
                zf.writestr(f"word/media/{rel_id}.png", data)


def convert(md_path: Path, out_path: Path | None = None) -> Path:
    text = md_path.read_text(encoding="utf-8")
    title = md_path.stem
    builder = DocxBuilder(base_dir=md_path.parent, title=title)
    builder.parse(text)
    target = out_path or md_path.with_suffix(".docx")
    builder.save(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="make_docx.py",
        description="把 Markdown 转成 Word(.docx)，零第三方依赖",
    )
    parser.add_argument("markdown", help="输入的 .md 文件")
    parser.add_argument("-o", "--output", help="输出的 .docx 路径")
    args = parser.parse_args()

    md_path = Path(args.markdown)
    if not md_path.exists():
        print(f"❌ 找不到文件：{md_path}", file=sys.stderr)
        return 1

    out = convert(md_path, Path(args.output) if args.output else None)
    size_kb = out.stat().st_size / 1024
    print(f"✅ 已生成：{out}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
