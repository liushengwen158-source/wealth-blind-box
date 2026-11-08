# 对账测试。运行：pytest -v
#
# 这东西的价值：以后你为了赶进度改了手续费代码，跑一下这条命令，
# 5 个测试全绿就说明钱还是对的，不用手动点网页试半天。

import random
import pytest

from trading_core import 默认费率, 算费用, 校验, 买入, 卖出, 规则拒绝

初始 = 10_000_000      # 10 万元
价格 = 2450            # 24.50 元
涨停, 跌停 = 2695, 2205


def test_最低佣金生效():
    # 买 100 股 = 24.5 元 * 100 = 2450 元，万分之三只有 0.735 元，低于 5 元
    assert 算费用(2450 * 100, 是卖出=False).佣金 == 500


def test_卖出才收印花税和过户费():
    买 = 算费用(2450 * 100, 是卖出=False)
    卖 = 算费用(2450 * 100, 是卖出=True)
    assert 买.印花税 == 0 and 买.过户费 == 0
    # 245000 分 × 0.0005 = 122.5 分，四舍五入 = 123
    # 坑：Python 的 round(122.5) 返回 122（银行家舍入），所以不能用 round，必须 int(x+0.5)
    assert 卖.印花税 == 123 and 卖.过户费 == 2


def test_买入后总资产只少一笔佣金():
    现金, 持股, 成本, 费 = 买入(初始, 0, 0, 价格, 100)
    assert 现金 == 初始 - 245000 - 500
    assert 持股 == 100
    assert 现金 + 持股 * 价格 == 初始 - 费.合计


def test_卖出后剩余持仓成本价不变():
    现金, 持股, 成本, _ = 买入(初始, 0, 0, 价格, 300)
    assert 成本 == 2450
    现金, 持股, 成本, _ = 卖出(现金, 持股, 成本, 2500, 100)
    assert 持股 == 200
    assert 成本 == 2450


def test_规则拒绝():
    with pytest.raises(规则拒绝) as 异常:
        校验("BUY", 价格, 150, 初始, 0, 0, 涨停, 跌停)      # 不是整手
    assert 异常.value.code == "INVALID_LOT"

    with pytest.raises(规则拒绝) as 异常:
        校验("BUY", 2695, 100, 初始, 0, 0, 涨停, 跌停)      # 涨停价买
    assert 异常.value.code == "LIMIT_UP"

    with pytest.raises(规则拒绝) as 异常:
        校验("SELL", 价格, 100, 初始, 100, 0, 涨停, 跌停)   # T+1 不可卖
    assert 异常.value.code == "T1_RESTRICTED"

    with pytest.raises(规则拒绝) as 异常:
        校验("BUY", 价格, 10_000, 初始, 0, 0, 涨停, 跌停)   # 钱不够
    assert 异常.value.code == "INSUFFICIENT_CASH"


def test_随机瞎买瞎卖三百次_钱不会凭空多出来():
    """这条是真正的对账。价格不变，所以总资产必须正好等于 初始 - 累计手续费。"""
    随机 = random.Random(7)
    现金, 持股, 成本, 累计费用 = 初始, 0, 0, 0

    for _ in range(300):
        if 随机.random() < 0.5:
            数量 = 100 * 随机.randint(1, 5)
            try:
                校验("BUY", 价格, 数量, 现金, 持股, 持股, 99999, 1)
            except 规则拒绝:
                continue
            现金, 持股, 成本, 费 = 买入(现金, 持股, 成本, 价格, 数量)
        else:
            if 持股 < 100:
                continue
            数量 = 100 * 随机.randint(1, min(5, 持股 // 100))
            现金, 持股, 成本, 费 = 卖出(现金, 持股, 成本, 价格, 数量)
        累计费用 += 费.合计

    assert 现金 >= 0, "现金不能为负"
    assert 持股 >= 0, "持股不能为负"
    assert 现金 + 持股 * 价格 == 初始 - 累计费用, "账对不上：有钱凭空出现或消失"
    assert 累计费用 > 0, "300 笔交易不可能零手续费"
