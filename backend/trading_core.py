# 交易核心的「纯逻辑」：只算钱、只查规则。
# 刻意不 import 数据库、不 import FastAPI、不碰网络。
# 为什么？因为这样它能被测试几千次，一次几毫秒。你以后所有 bug 都优先在这里暴露。

from dataclasses import dataclass


@dataclass(frozen=True)
class 费率:
    """所有费率都是小数，例如万分之三写 0.0003。"""
    佣金率: float = 0.0003
    最低佣金: int = 500          # 500 分 = 5 元
    印花税率: float = 0.0005     # 只有卖出收
    过户费率: float = 0.00001    # 只有卖出收


默认费率 = 费率()


@dataclass(frozen=True)
class 费用:
    佣金: int
    印花税: int
    过户费: int

    @property
    def 合计(self) -> int:
        return self.佣金 + self.印花税 + self.过户费


def _分(x: float) -> int:
    """四舍五入到分。注意：int() 是直接把小数砍掉，会少收钱，不能用。"""
    return int(x + 0.5)


def 算费用(金额: int, 是卖出: bool, 费率表: 费率 = 默认费率) -> 费用:
    佣 = max(_分(金额 * 费率表.佣金率), 费率表.最低佣金)
    税 = _分(金额 * 费率表.印花税率) if 是卖出 else 0
    过户 = _分(金额 * 费率表.过户费率) if 是卖出 else 0
    return 费用(佣, 税, 过户)


class 规则拒绝(Exception):
    """这笔单不许成交。code 用的就是 docs/api.md 第 12 节的错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def 校验(方向: str, 价格: int, 数量: int, 现金: int, 持股: int,
         可卖: int, 涨停价: int, 跌停价: int, 费率表: 费率 = 默认费率) -> None:
    """按固定顺序问 6 个问题。任何一问答"不行"就抛异常。
    顺序不能乱 —— 顺序变了，用户看到的错误提示就变了。"""
    if 数量 <= 0 or 数量 % 100 != 0:
        raise 规则拒绝("INVALID_LOT", "数量必须是 100 的整数倍")
    if 方向 == "BUY" and 价格 >= 涨停价:
        raise 规则拒绝("LIMIT_UP", "涨停价不可买入")
    if 方向 == "SELL" and 价格 <= 跌停价:
        raise 规则拒绝("LIMIT_DOWN", "跌停价不可卖出")
    if 方向 == "SELL" and 数量 > 可卖:
        raise 规则拒绝("T1_RESTRICTED", "T+1 未解禁，可卖数量不足")
    if 方向 == "BUY":
        金额 = 价格 * 数量
        if 金额 + 算费用(金额, False, 费率表).佣金 > 现金:
            raise 规则拒绝("INSUFFICIENT_CASH", "可用资金不足")
    else:
        if 数量 > 持股:
            raise 规则拒绝("INSUFFICIENT_POSITION", "可卖持仓不足")


def 买入(现金: int, 持股: int, 成本价: int, 价格: int, 数量: int,
         费率表: 费率 = 默认费率):
    """返回 (新现金, 新持股, 新成本价, 费用)。不动传进来的变量，纯函数。"""
    金额 = 价格 * 数量
    费 = 算费用(金额, 是卖出=False, 费率表=费率表)
    新持股 = 持股 + 数量
    新成本价 = (成本价 * 持股 + 金额) // 新持股      # 移动加权平均
    return 现金 - 金额 - 费.合计, 新持股, 新成本价, 费


def 卖出(现金: int, 持股: int, 成本价: int, 价格: int, 数量: int,
         费率表: 费率 = 默认费率):
    """卖出时成本价不变 —— 股价涨跌不改变你已经付出去的成本。"""
    金额 = 价格 * 数量
    费 = 算费用(金额, 是卖出=True, 费率表=费率表)
    新持股 = 持股 - 数量
    return 现金 + 金额 - 费.合计, 新持股, (成本价 if 新持股 else 0), 费
