"""Word 文档生成器的测试。

为什么要测一个"生成文档的脚本"
------------------------------
这份 Word 文档是要拿去答辩/教学的，出问题很难被发现：
· 生成器悄悄丢图片 → 打开才发现少图
· 嵌套的行内格式没处理 → 正文里出现一堆反引号
· 表格/代码块没渲染 → 页面排版乱掉

所以这里对生成结果做**结构断言**：解析生成的 .docx（本质是个 zip），
检查 document.xml 里有没有该有的东西。
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "tools" / "make_docx.py"


def find_sample_image() -> Path | None:
    """找一张用来测试"图片能嵌进 docx"的图。

    仓库布局和交付包布局（`06_测试/` 与 `05_文档/` 分开）不一样，
    所以这里按候选路径找，找到就转成**绝对路径**塞进样例 Markdown ——
    绝对路径在两种布局下都能解析。
    """
    candidates = [
        ROOT / "docs" / "images" / "edge_center_BA.png",
        ROOT.parent / "05_文档" / "docs" / "images" / "edge_center_BA.png",
        ROOT.parent / "docs" / "images" / "edge_center_BA.png",
        ROOT / ".." / "docs" / "images" / "edge_center_BA.png",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def load_module():
    spec = importlib.util.spec_from_file_location("make_docx_under_test",
                                                  _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SAMPLE = """# 标题一

普通段落，含 **粗体** 与 `行内代码`。

## 标题二

- 无序项
- 另一项

1. 有序项

| 列 A | 列 B |
|---|---|
| 值 1 | 值 2 |

### 标题三

```python
x = 1  # 代码
y = 2  # 第二行
```

![图注](__IMAGE__)

> 引用文字

**注意是 `cxcywh`，不是 `xyxy`。**

<!-- pagebreak -->

分页后。
"""


class TestMakeDocx(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        image = find_sample_image()
        text = SAMPLE.replace("__IMAGE__",
                              str(image) if image else "no/such/image.png")
        src = base / "sample.md"
        src.write_text(text, encoding="utf-8")
        cls.has_image = image is not None
        cls.out = base / "sample.docx"
        cls.module.convert(src, cls.out)
        cls.zip = zipfile.ZipFile(cls.out)
        cls.xml = cls.zip.read("word/document.xml").decode("utf-8")
        cls.text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", cls.xml, re.S))
        cls.base = base

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    # ── 基本结构 ──
    def test_is_valid_zip_with_required_parts(self) -> None:
        names = set(self.zip.namelist())
        for required in ("[Content_Types].xml", "_rels/.rels",
                         "word/document.xml", "word/styles.xml",
                         "word/_rels/document.xml.rels"):
            with self.subTest(part=required):
                self.assertIn(required, names)

    def test_headings_use_heading_styles(self) -> None:
        # `#` → Title，`##` → Heading1，`###` → Heading2
        for style in ("Title", "Heading1", "Heading2"):
            with self.subTest(style=style):
                self.assertIn(f'w:val="{style}"', self.xml)

    def test_table_rendered(self) -> None:
        self.assertIn("<w:tbl>", self.xml)
        self.assertIn("列 A", self.text)
        self.assertIn("值 2", self.text)

    def test_code_block_rendered(self) -> None:
        self.assertIn("CodeFirst", self.xml)
        self.assertIn("x = 1", self.text)

    def test_lists_rendered(self) -> None:
        self.assertIn("<w:numPr>", self.xml)

    def test_page_break_rendered(self) -> None:
        self.assertIn('w:type="page"', self.xml)

    def test_quote_rendered(self) -> None:
        self.assertIn('w:val="Quote"', self.xml)

    # ── 图片 ──
    def test_image_embedded(self) -> None:
        if not self.has_image:
            self.skipTest("测试环境里找不到样例图片")
        media = [n for n in self.zip.namelist() if n.startswith("word/media")]
        self.assertTrue(media, "图片没有被嵌入 docx")
        self.assertIn("w:drawing", self.xml)
        self.assertIn("r:embed=", self.xml)

    def test_missing_image_becomes_placeholder_not_crash(self) -> None:
        module = load_module()
        src = self.base / "missing.md"
        src.write_text("![找不到](no/such/image.png)\n", encoding="utf-8")
        out = self.base / "missing.docx"
        module.convert(src, out)          # 不该抛异常
        self.assertTrue(out.exists())

    # ── 行内格式 ──
    def test_nested_bold_and_code(self) -> None:
        """**粗体里套 `代码`** 不能把反引号原样画出来。"""
        self.assertNotIn("`", self.text,
                         "正文里残留了反引号 —— 嵌套行内格式没解析")
        self.assertIn("cxcywh", self.text)

    def test_bold_is_marked_bold(self) -> None:
        self.assertIn("<w:b/>", self.xml)

    # ── 健壮性 ──
    def test_empty_document(self) -> None:
        module = load_module()
        src = self.base / "empty.md"
        src.write_text("", encoding="utf-8")
        out = self.base / "empty.docx"
        module.convert(src, out)
        self.assertTrue(out.exists())

    def test_unclosed_code_fence_does_not_crash(self) -> None:
        module = load_module()
        src = self.base / "broken.md"
        src.write_text("# 标题\n\n```python\nx = 1\n", encoding="utf-8")
        out = self.base / "broken.docx"
        module.convert(src, out)
        self.assertTrue(out.exists())

    def test_newlines_in_code_preserved(self) -> None:
        """代码块每行必须是独立段落，否则会自动折行糊成一段。"""
        code_paras = re.findall(
            r'<w:p><w:pPr><w:pStyle w:val="Code\w*"/></w:pPr>.*?</w:p>',
            self.xml, re.S)
        self.assertGreaterEqual(len(code_paras), 2)


def find_docs_dir() -> Path | None:
    """定位 docs 目录（仓库布局与交付包布局不同）。"""
    for candidate in (ROOT / "docs",
                      ROOT.parent / "05_文档" / "docs",
                      ROOT / ".." / "docs"):
        if candidate.is_dir():
            return candidate
    return None


class TestRealDocumentsExist(unittest.TestCase):
    """仓库里那两份交付文档必须真的存在且能被解析。"""

    def setUp(self) -> None:
        self.docs = find_docs_dir()
        if self.docs is None:
            self.skipTest("找不到 docs 目录")

    def test_markdown_documents_exist(self) -> None:
        for name in ("项目功能与原理详解.md", "关键原理讲解-教学版.md"):
            path = self.docs / name
            with self.subTest(doc=name):
                self.assertTrue(path.exists(), f"缺少文档：{path}")
                self.assertGreater(path.stat().st_size, 5000,
                                   "文档内容似乎不完整")

    def test_teaching_doc_has_docx(self) -> None:
        docx = self.docs / "关键原理讲解-教学版.docx"
        self.assertTrue(docx.exists(), "教学版 Word 文档没有生成")
        with zipfile.ZipFile(docx) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:tbl>", xml, "Word 文档里没有表格")
        self.assertIn("w:drawing", xml, "Word 文档里没有插图")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPushViaApi(unittest.TestCase):
    """API 推送脚本的纯逻辑部分（不联网）。

    这个脚本要绕过被墙的 git 通道，靠的是**完全复刻本地提交**：
    tree 条目、mode、作者、时间戳、Message 都必须和本地一致，
    否则远端 commit SHA 就对不上，本地反而变成"分叉"。
    所以它的解析与拼接逻辑值得单独测。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        path = ROOT / "tools" / "push_via_api.py"
        spec = importlib.util.spec_from_file_location("push_api_under_test",
                                                      path)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.module)

    def test_parse_commit_object(self) -> None:
        raw = (
            "tree aaaa1111\n"
            "parent bbbb2222\n"
            "parent cccc3333\n"
            "author 张三 <zhang@example.com> 1789544205 +0800\n"
            "committer 李四 <li@example.com> 1789544300 +0800\n"
            "\n"
            "标题行\n\n正文第二段\n"
        )
        info = self.module.parse_commit_object(raw)
        self.assertEqual(info["tree"], "aaaa1111")
        self.assertEqual(info["parents"], ["bbbb2222", "cccc3333"])
        self.assertEqual(info["author"]["name"], "张三")
        self.assertEqual(info["author"]["email"], "zhang@example.com")
        self.assertEqual(info["message"], "标题行\n\n正文第二段\n")

    def test_iso_time_conversion(self) -> None:
        # 1789544205 → 2026-09-16（北京时间）
        iso = self.module.to_iso("1789544205 +0800")
        self.assertTrue(iso.startswith("2026-09-16T"))
        self.assertTrue(iso.endswith("+08:00"))

    def test_iso_time_negative_offset(self) -> None:
        iso = self.module.to_iso("0 -0500")
        self.assertTrue(iso.endswith("-05:00"))

    def test_tree_entries_use_git_modes(self) -> None:
        changes = [("A", "a.sh"), ("M", "b.py"), ("D", "gone.txt")]
        entries = self.module.build_tree_entries(
            changes, {"a.sh": "sha1", "b.py": "sha2"},
            {"a.sh": "100755", "b.py": "100644"})
        by_path = {e["path"]: e for e in entries}
        self.assertEqual(by_path["a.sh"]["mode"], "100755")
        self.assertEqual(by_path["b.py"]["mode"], "100644")
        # 删除的条目必须显式给 sha=None，否则远端不会删
        self.assertIsNone(by_path["gone.txt"]["sha"])

    def test_missing_blob_sha_is_loud(self) -> None:
        with self.assertRaises(KeyError):
            self.module.build_tree_entries([("A", "x.py")], {}, {})

    def test_dry_run_makes_no_write_calls(self) -> None:
        client = self.module.GitHub("tok", "o/r", dry_run=True)
        self.assertTrue(client.create_blob(b"data").startswith("dry-run-"))

    def test_token_is_scrubbed_from_errors(self) -> None:
        client = self.module.GitHub("ghp_SECRET1234567890", "o/r")
        self.assertNotIn("SECRET", client.scrub("err ghp_SECRET1234567890 x"))
