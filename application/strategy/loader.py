"""
application/strategy/loader.py

插件注册表 + 动态加载器。

注册方式：
  1. 直接注册实例：    registry.register(MyStrategy())
  2. 从模块路径加载：  loader.load_module("strategies.momentum.MomentumStrategy")
  3. 从文件路径加载：  loader.load_file("/path/to/alpha.py::AlphaStrategy")
  4. 从 config 批量：  loader.load_all(cfg.strategies)

config.yaml 示例：
  strategies:
    - module: "strategies.momentum.MomentumStrategy"
      params: {window: 20, scale: 0.05}
    - file: "/external/alpha.py::AlphaV2"
      params: {}
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any

from application.strategy.base import Strategy

log = logging.getLogger(__name__)


class PluginRegistry:
    """全局策略注册表。"""

    def __init__(self) -> None:
        self._plugins: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        if not isinstance(strategy, Strategy):
            raise TypeError(
                f"{type(strategy).__name__} 未实现 Strategy 协议\n"
                f"需要实现: name, version, on_trade, on_book, "
                f"on_funding, on_fill, on_start, on_stop"
            )
        if strategy.name in self._plugins:
            log.warning("覆盖已有策略: %s", strategy.name)
        self._plugins[strategy.name] = strategy
        log.info("注册策略: %s v%s", strategy.name, strategy.version)

    def get(self, name: str) -> Strategy:
        if name not in self._plugins:
            raise KeyError(f"策略 '{name}' 未注册，已注册: {list(self._plugins)}")
        return self._plugins[name]

    def all(self) -> list[Strategy]:
        return list(self._plugins.values())

    def __len__(self) -> int:
        return len(self._plugins)


class PluginLoader:
    """从模块路径或文件路径动态加载策略。"""

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def load_module(self, dotted_path: str,
                    params: dict[str, Any] | None = None) -> Strategy:
        """
        从 Python 模块路径加载并实例化策略。

        dotted_path: "strategies.momentum.MomentumStrategy"
        params:      传给策略构造函数的关键字参数
        """
        module_path, cls_name = dotted_path.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as e:
            raise ImportError(f"无法导入模块 '{module_path}': {e}") from e

        cls = getattr(module, cls_name, None)
        if cls is None:
            raise AttributeError(
                f"模块 '{module_path}' 中找不到类 '{cls_name}'"
            )

        strategy = cls(**(params or {}))
        self._registry.register(strategy)
        return strategy

    def load_file(self, spec: str,
                  params: dict[str, Any] | None = None) -> Strategy:
        """
        从文件路径加载策略（支持外部 Alpha 插件）。

        spec: "/path/to/alpha.py::AlphaStrategy"
        """
        if "::" not in spec:
            raise ValueError(f"格式错误，需要 'path.py::ClassName'，得到: {spec}")

        file_path, cls_name = spec.split("::", 1)
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"策略文件不存在: {file_path}")

        spec_obj = importlib.util.spec_from_file_location(path.stem, path)
        module   = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)

        cls = getattr(module, cls_name, None)
        if cls is None:
            raise AttributeError(
                f"文件 '{file_path}' 中找不到类 '{cls_name}'"
            )

        strategy = cls(**(params or {}))
        self._registry.register(strategy)
        return strategy

    def load_all(self, configs: list[dict[str, Any]]) -> None:
        """
        从配置列表批量加载策略。

        configs 格式：
          [
            {"module": "strategies.momentum.MomentumStrategy",
             "params": {"window": 20}},
            {"file": "/ext/alpha.py::Alpha",
             "params": {}},
          ]
        """
        for cfg in configs:
            params = cfg.get("params", {})
            if "module" in cfg:
                self.load_module(cfg["module"], params)
            elif "file" in cfg:
                self.load_file(cfg["file"], params)
            else:
                raise ValueError(f"无效策略配置（需要 'module' 或 'file'）: {cfg}")
