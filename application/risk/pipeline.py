"""
application/risk/pipeline.py  —  可插拔风控管道

责任链（Chain of Responsibility）模式。

新增风控规则：
  1. 创建 MyMiddleware(RiskMiddleware)
  2. container.py 的 RiskPipeline([..., MyMiddleware()])
  零改动已有代码。

中间件可以：
  - 拦截：return RiskResult.reject(...)
  - 修改：return call_next(amended_order, ctx)
  - 放行：return call_next(order, ctx)
  - 记录软告警：return call_next(order, ctx)  # + 写日志
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Callable

from core.domain.order import Order
from core.ports.risk import RiskContext, RiskResult

log = logging.getLogger(__name__)

Next = Callable[[Order, RiskContext], RiskResult]


class RiskMiddleware:
    """风控中间件基类（也满足 RiskPort 结构化子类型）。"""
    name: str = "base"
    # True: reduce_only 平仓单跳过本中间件检查（多数风控只约束开仓）。
    # 子类设为 False 表示该检查对平仓单同样生效（如最小名义价值、在途数量）。
    check_reduce_only: bool = True

    def process(self, order: Order, ctx: RiskContext,
                call_next: Next) -> RiskResult:
        if self.check_reduce_only and order.reduce_only:
            return call_next(order, ctx)   # 平仓单直接放行
        return self._do_check(order, ctx, call_next)

    def _do_check(self, order: Order, ctx: RiskContext,
                  call_next: Next) -> RiskResult:
        raise NotImplementedError(
            f"{type(self).__name__} 必须实现 _do_check()"
        )

    @staticmethod
    def _resolve_est_price(order: Order, ctx: RiskContext) -> "Decimal | None":
        """
        解析订单的预估成交价：优先用 limit_price，否则取 cache 最新价。
        两者都不可用时返回 None（由调用方决定拒绝还是跳过）。
        """
        est_price = order.limit_price
        if est_price is not None and est_price > 0:
            return est_price
        cache_price = ctx.extra.get("price")
        if cache_price is not None and cache_price > 0:
            return cache_price
        return None


class RiskPipeline:
    """
    组合多个 RiskMiddleware，按注册顺序执行。

    示例：
        pipeline = RiskPipeline([
            PositionLimitMiddleware(max_weight=0.1),
            MaxLeverageMiddleware(global_max=10),
            DrawdownMiddleware(max_drawdown=0.05),
            FundingRateMiddleware(max_rate=0.003),
        ])
        result = pipeline.check(order, ctx)
    """

    def __init__(self, middlewares: list[RiskMiddleware]) -> None:
        self._mws = list(middlewares)
        names = [m.name for m in self._mws]
        log.info("RiskPipeline 初始化，中间件顺序: %s", names)

    def check(self, order: Order, ctx: RiskContext) -> RiskResult:
        """执行完整风控检查链。"""
        chain = self._build_chain(0)
        return chain(order, ctx)

    def _build_chain(self, index: int) -> Next:
        if index >= len(self._mws):
            return lambda o, c: RiskResult.approve()
        mw   = self._mws[index]
        rest = self._build_chain(index + 1)
        return lambda o, c: mw.process(o, c, rest)

    def append(self, middleware: RiskMiddleware) -> RiskPipeline:
        """返回添加了新中间件的新 Pipeline（不可变更新）。"""
        return RiskPipeline(self._mws + [middleware])

    @property
    def middleware_names(self) -> list[str]:
        return [m.name for m in self._mws]

    # 满足 RiskPort 协议
    def check_port(self, order: Order, ctx: RiskContext) -> RiskResult:
        return self.check(order, ctx)
