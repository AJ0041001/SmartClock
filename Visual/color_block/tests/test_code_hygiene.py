"""代码卫生测试 —— 用 AST 静态检查捕捉人工审查容易漏的问题。

起因
----
开发过程中出现过这样一次真实事故：给流水线加 `open()` 时，
类里已经有了一个同名方法，新增的直接覆盖了旧的 ——
**ROI 校正和目录创建逻辑被静默丢掉**，而且不报任何错。

Python 允许同名方法重复定义（后者胜出），所以这类 bug 只能靠静态检查抓。
"""

from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = [PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"]


def iter_python_files():
    """遍历项目里的 Python 源文件（跳过测试与缓存）。"""
    for directory in SOURCE_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.name.startswith("."):
                continue
            yield path


def class_method_names(class_node: ast.ClassDef) -> list[str]:
    """取类里所有方法名（含重复项）。"""
    names = []
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return names


class TestNoDuplicateMethods(unittest.TestCase):
    """同一个类里不允许出现重名方法。

    重复定义时，先定义的那个会被完全覆盖 —— 如果它包含必要的初始化逻辑，
    就会造成"代码看着在、实际不执行"的隐蔽 bug。
    """

    def test_no_duplicate_methods(self) -> None:
        offenders: list[str] = []

        for path in iter_python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"),
                                 filename=str(path))
            except SyntaxError as exc:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} 语法错误：{exc}")
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                counts = Counter(class_method_names(node))
                duplicates = {name: n for name, n in counts.items() if n > 1}
                for name, count in duplicates.items():
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)} :: "
                        f"class {node.name} 中 '{name}' 定义了 {count} 次"
                        f"（第 {node.lineno} 行起）"
                    )

        self.assertEqual(
            offenders, [],
            "发现重复方法定义（后者会覆盖前者）：\n  " + "\n  ".join(offenders),
        )


class TestNoDuplicateModuleLevelNames(unittest.TestCase):
    """模块级也不该重复定义同名函数（同样是静默覆盖）。"""

    def test_no_duplicate_functions(self) -> None:
        offenders: list[str] = []

        for path in iter_python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"),
                                 filename=str(path))
            except SyntaxError:
                continue

            names = [
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            duplicates = {n: c for n, c in Counter(names).items() if c > 1}
            for name, count in duplicates.items():
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)} :: "
                    f"函数 '{name}' 定义了 {count} 次"
                )

        self.assertEqual(
            offenders, [],
            "发现模块级重复函数定义：\n  " + "\n  ".join(offenders),
        )


class TestImportsResolve(unittest.TestCase):
    """核心模块必须能导入（顺带验证依赖没写错）。"""

    def test_core_modules_import(self) -> None:
        import importlib

        modules = [
            "src.protocol",
            "src.serialport",
            "src.camera",
            "src.detector",
            "src.mapper",
            "src.config",
            "src.pipeline",
            "src.yolo",
            "src.rknn_ctypes",
            "src.card_detector",
        ]
        for name in modules:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_scripts_are_syntactically_valid(self) -> None:
        for path in sorted((PROJECT_ROOT / "scripts").glob("*.py")):
            with self.subTest(script=path.name):
                ast.parse(path.read_text(encoding="utf-8"),
                          filename=str(path))


class TestDetectorInterfaceParity(unittest.TestCase):
    """两种检测器必须提供相同的方法，否则流水线切换会炸。"""

    def test_public_methods_present(self) -> None:
        from src.card_detector import CardDetector
        from src.detector import ColorDetector

        required = ("detect", "detect_all", "draw", "draw_roi")
        for cls in (CardDetector, ColorDetector):
            for name in required:
                with self.subTest(cls=cls.__name__, method=name):
                    self.assertTrue(
                        callable(getattr(cls, name, None)),
                        f"{cls.__name__} 缺少 {name}()",
                    )


class TestEngineDispatch(unittest.TestCase):
    """`detector.engine` 配置必须被真正遵守。

    真实事故：`scripts/preview.py` 直接 new 了 ColorDetector，
    完全忽略配置里的 `engine: yolo` —— 于是配了 YOLO 却还在跑 HSV，
    行为与 main.py 不一致，排查时极易误判。

    修复方式是所有脚本统一走 `build_detector(config)`。这里锁住两点：
      1. 工厂函数确实按 engine 分派到不同类
      2. 脚本不出现"绕过工厂直接 new ColorDetector"的写法
    """

    @staticmethod
    def _config(engine: str):
        from src.config import AppConfig

        config = AppConfig()
        config.detector.engine = engine
        return config

    def test_yolo_engine_builds_card_detector(self) -> None:
        from unittest import mock

        from src.card_detector import CardDetector
        from src.pipeline import build_detector

        with mock.patch("src.card_detector.RKNNLite"), \
             mock.patch("src.card_detector.find_librknnrt",
                        return_value="/fake.so"):
            detector = build_detector(self._config("yolo"))
        self.assertIsInstance(detector, CardDetector)

    def test_hsv_engine_builds_color_detector(self) -> None:
        from src.detector import ColorDetector
        from src.pipeline import build_detector

        detector = build_detector(self._config("hsv"))
        self.assertIsInstance(detector, ColorDetector)

    def test_two_engines_give_different_types(self) -> None:
        """两种引擎不能返回同一种检测器 —— 那说明分派没生效。"""
        from unittest import mock

        from src.pipeline import build_detector

        hsv = type(build_detector(self._config("hsv")))
        with mock.patch("src.card_detector.RKNNLite"), \
             mock.patch("src.card_detector.find_librknnrt",
                        return_value="/fake.so"):
            yolo = type(build_detector(self._config("yolo")))
        self.assertNotEqual(hsv, yolo)

    def test_scripts_do_not_bypass_factory(self) -> None:
        """脚本里不应出现裸的 ColorDetector(...) 而完全不走工厂。

        例外（显式列出，而不是悄悄放过）：
          · ``preview.py`` —— HSV 模式下每帧重建检测器是刻意的
            （HSV 无状态、取色标定需要立刻生效），文件里同时用了
            build_detector，所以自然通过。
          · ``color_pick.py`` —— 它**本身就是 HSV 取色标定工具**，
            用 ColorDetector 是它的职责所在，与 engine 配置无关。
        """
        allowlist = {"color_pick.py"}

        offenders = []
        for path in sorted((PROJECT_ROOT / "scripts").glob("*.py")):
            if path.name in allowlist:
                continue
            source = path.read_text(encoding="utf-8")
            if "ColorDetector(" not in source:
                continue
            if "build_detector" in source:
                continue
            offenders.append(path.name)

        self.assertEqual(
            offenders, [],
            "这些脚本直接 new 了 ColorDetector 却没走 build_detector，"
            "会忽略 detector.engine 配置：\n  " + "\n  ".join(offenders),
        )

    def test_allowlisted_script_is_really_hsv_specific(self) -> None:
        """allowlist 里只应放确实与 HSV 强绑定的脚本。

        防止有人图省事把普通脚本塞进白名单绕过检查。
        """
        for name in ("color_pick.py",):
            with self.subTest(script=name):
                source = (PROJECT_ROOT / "scripts" / name).read_text(
                    encoding="utf-8"
                )
                # 这类脚本必须真的在处理 HSV
                self.assertIn("sample_region_color", source)


class TestAsciiWindowNames(unittest.TestCase):
    """OpenCV 窗口标题必须是纯 ASCII。

    真实事故：流水线的预览窗口标题写成了 ``"SmartClock 视觉追踪"``，
    结果窗口弹出来了但**画面完全不渲染**（一片空白/黑）。

    原因：OpenCV 的 HighGUI 在 Linux（Qt/GTK 后端）对非 ASCII 字符串
    支持很差，传给底层窗口系统时可能失败或乱码。

    这个坑特别隐蔽 —— 画面数据本身完全正常（验证过：23 帧、均值 217、
    非零像素 98%），``imshow`` 也照常被调用，只是显示不出来。
    所以只能靠静态检查兜住。
    """

    def _iter_cv_sources(self):
        for directory in (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts",
                          PROJECT_ROOT / "examples"):
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                yield path

    def test_window_name_constants_are_ascii(self) -> None:
        """模块里定义的窗口名常量必须是 ASCII。"""
        import importlib

        # 直接检查已知的窗口名常量
        candidates = [
            ("src.pipeline", "ColorTrackingPipeline", "PREVIEW_WINDOW"),
        ]
        for module_name, class_name, attr in candidates:
            with self.subTest(module=module_name, attr=attr):
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                value = getattr(cls, attr)
                self.assertTrue(
                    value.isascii(),
                    f"{module_name}.{class_name}.{attr} = {value!r} "
                    f"含非 ASCII 字符，会导致 OpenCV 窗口不渲染",
                )

    def test_namedwindow_calls_use_ascii(self) -> None:
        """代码里 cv2.namedWindow(...) 的字符串参数必须是 ASCII。"""
        offenders = []

        for path in self._iter_cv_sources():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"),
                                 filename=str(path))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in ("namedWindow", "imshow"):
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if not isinstance(first, ast.Constant) or \
                        not isinstance(first.value, str):
                    continue
                if not first.value.isascii():
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} "
                        f"cv2.{func.attr}({first.value!r})"
                    )

        self.assertEqual(
            offenders, [],
            "OpenCV 窗口标题含非 ASCII 字符，窗口会弹出来但不渲染：\n  "
            + "\n  ".join(offenders),
        )

    def test_known_pitfall_is_documented(self) -> None:
        """这个坑要在代码注释里留痕，避免后人又改回中文。"""
        source = (PROJECT_ROOT / "src" / "pipeline.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ASCII", source)
        self.assertIn("HighGUI", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTestsCannotClobberUserConfig(unittest.TestCase):
    """测试**绝不能**覆盖用户现场标定好的 config.yaml。

    真实事故：有些测试没显式指定 config_path，RoiEditor 就用了默认的
    ``config.yaml``。只要测试里碰到一次"保存"，用户辛苦框好的两个 ROI
    就被测试假数据覆盖了 —— 而且下次启动才发现。

    两道保护，这里各测一条：
      1. 环境变量 SMARTCLOCK_CONFIG 把默认目标重定向到 /dev/null
      2. 测试代码里建 RoiEditor 必须显式传 config_path
    """

    def test_env_redirects_default_config_path(self) -> None:
        from src.roi_editor import default_config_path

        self.assertNotEqual(
            default_config_path(), "config.yaml",
            "测试环境下默认配置路径必须被重定向，不能指向仓库里的 config.yaml",
        )

    def test_tests_always_pass_config_path_to_roi_editor(self) -> None:
        offenders: list[str] = []
        for path in sorted((PROJECT_ROOT / "tests").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "RoiEditor":
                    continue
                if not any(kw.arg == "config_path" for kw in node.keywords):
                    offenders.append(
                        f"{path.name}:{node.lineno} 建 RoiEditor 没传 config_path"
                    )
        self.assertEqual(
            offenders, [],
            "以下位置可能把测试数据写进用户的 config.yaml：\n  "
            + "\n  ".join(offenders),
        )
