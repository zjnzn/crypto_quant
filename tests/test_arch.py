"""
tests/test_arch.py  —  架构依赖规则自动检查

此测试保证架构边界不被破坏。CI 必跑。
失败 = 有人在 core/ 里 import 了 redis，或在 application/ 里 import 了 adapters。

规则（依赖只能由外向内）：
  core/        ← 零外部依赖（只有 stdlib）
  application/ ← 只能 import core/
  adapters/    ← 可 import core/ + 第三方库，不能 import application/services/
  env/         ← 只能 import core/
  container.py ← 特权文件，可 import 全部
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

# 确保项目根目录在 sys.path
ROOT = pathlib.Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def get_top_level_imports(path: pathlib.Path) -> set[str]:
    """返回文件中所有顶层 import 的模块名（取第一段）。"""
    try:
        source = path.read_text(encoding="utf-8")
        tree   = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return set()

    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                result.add(node.module.split(".")[0])
    return result


def get_full_imports(path: pathlib.Path) -> set[str]:
    """返回所有完整 import 路径（用于细粒度检查）。"""
    try:
        source = path.read_text(encoding="utf-8")
        tree   = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return set()

    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                result.add(node.module)
    return result


def py_files(directory: str) -> list[pathlib.Path]:
    base = ROOT / directory
    if not base.exists():
        return []
    return [p for p in base.rglob("*.py") if p.name != "__init__.py"]


# ── 允许的 stdlib 模块集合 ────────────────────────────────────────────────────

STDLIB = {
    "__future__", "abc", "ast", "asyncio", "collections", "contextlib",
    "copy", "dataclasses", "datetime", "decimal", "enum", "functools",
    "importlib", "io", "itertools", "logging", "math", "operator",
    "pathlib", "pickle", "queue", "re", "sys", "threading", "time",
    "traceback", "types", "typing", "uuid", "warnings", "weakref",
}

# ── 测试：core/ 零外部依赖 ────────────────────────────────────────────────────

@pytest.mark.parametrize("path", py_files("core"))
def test_core_zero_external_dependency(path: pathlib.Path) -> None:
    """
    core/ 只能 import stdlib 和 core/ 自身。
    不能 import adapters/、application/、第三方库（redis、kafka 等）。
    """
    imports = get_top_level_imports(path)
    forbidden = imports - STDLIB - {"core"}
    assert not forbidden, (
        f"\n{path.relative_to(ROOT)} 违反零依赖规则\n"
        f"发现外部 import: {sorted(forbidden)}\n"
        f"core/ 是架构内核，只能使用 Python 标准库。"
    )

# ── 测试：application/ 不能 import adapters/ ─────────────────────────────────

@pytest.mark.parametrize("path", py_files("application"))
def test_application_does_not_import_adapters(path: pathlib.Path) -> None:
    """
    application/ 不能 import adapters/。
    业务逻辑只依赖接口（core/ports/），不依赖具体实现。
    """
    imports = get_top_level_imports(path)
    assert "adapters" not in imports, (
        f"\n{path.relative_to(ROOT)} 违反依赖规则\n"
        f"application/ 不能 import adapters/（具体实现）\n"
        f"请使用 core/ports/ 中的 Protocol 接口。"
    )

# ── 测试：adapters/ 不能 import application/services/ ────────────────────────

@pytest.mark.parametrize("path", py_files("adapters"))
def test_adapters_do_not_import_application_services(path: pathlib.Path) -> None:
    """
    adapters/ 可以 import application/events.py（事件定义），
    但不能 import application/services/（防止反向依赖）。
    """
    full_imports = get_full_imports(path)
    service_imports = {imp for imp in full_imports
                       if imp.startswith("application.services")}
    assert not service_imports, (
        f"\n{path.relative_to(ROOT)} 违反依赖规则\n"
        f"adapters/ 不能 import application/services/\n"
        f"发现: {sorted(service_imports)}"
    )

# ── 测试：env/ 不能 import adapters/ ─────────────────────────────────────────

@pytest.mark.parametrize("path", py_files("env"))
def test_env_does_not_import_adapters(path: pathlib.Path) -> None:
    """env/ 环境抽象层只能依赖 core/，不依赖具体 adapter 实现。"""
    imports = get_top_level_imports(path)
    assert "adapters" not in imports, (
        f"\n{path.relative_to(ROOT)} 违反依赖规则\n"
        f"env/ 不能 import adapters/。"
    )

# ── 测试：只有 container.py 可以 import adapters/ ────────────────────────────

def test_only_container_imports_adapters() -> None:
    """
    container.py 和 run_*.py 入口脚本可以 import adapters/（装配点特权）。
    其他业务文件不能直接引用具体实现。
    """
    # 允许直接引用 adapters 的文件（装配点 / 入口脚本）
    ALLOWED = {"container.py"}
    ALLOWED_PREFIXES = ("run_",)   # run_backtest.py 等入口脚本

    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        rel   = path.relative_to(ROOT)
        parts = rel.parts
        name  = path.name

        # 跳过 adapters/ 自身、tests/、允许文件
        if parts[0] in ("adapters", "tests"):
            continue
        if name in ALLOWED:
            continue
        if any(name.startswith(p) for p in ALLOWED_PREFIXES):
            continue

        imports = get_top_level_imports(path)
        if "adapters" in imports:
            violations.append(str(rel))

    assert not violations, (
        f"\n以下文件不是装配点，但 import 了 adapters/：\n"
        + "\n".join(f"  {v}" for v in violations) + "\n"
        f"只有 container.py 和 run_*.py 允许引用具体实现。"
    )
