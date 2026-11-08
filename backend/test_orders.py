# 挂单 / 撤单 / 自动撮合的测试。
# 关键点：用临时数据库，不碰 demo 的 wealth.db；行情价格手动设，结果才确定。
# 临时库的路径在 conftest.py 里统一设，这里不再自己改。
import db

import main as S            # noqa: E402  必须在改完库路径之后再导入
from trading_core import 规则拒绝   # noqa: E402

初始资金 = 10_000_000


def 清场():
    S.引擎.开赛(总回合数=1, 回合毫秒=600_000)      # 让引擎处于「可交易」
    c = db.连接()
    c.execute("DELETE FROM orders")
    c.execute("UPDATE users SET cash = initial_cash, frozen_cash = 0")
    c.execute("UPDATE positions SET quantity=0, available_qty=0, frozen_qty=0, avg_cost=0")
    c.commit()
    return c


def 买(数量, 价=2450):
    return S.下单请求(clientOrderId=f"c{数量}{价}", symbol="600030",
                    side="BUY", type="LIMIT", price=价, quantity=数量)


def 用户行(c):
    return c.execute("SELECT * FROM users WHERE id = 1").fetchone()


def 单行(c, 单号):
    return c.execute("SELECT * FROM orders WHERE order_id = ?", (单号,)).fetchone()


def test_挂买单只冻结不扣钱():
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    r = S._下单(c, 1, 买(100, 价=2000))
    assert r["status"] == "PENDING"
    用户 = 用户行(c)
    # 冻的是「限价 × 数量 + 佣金」，现金总数不动 —— 只是不能再拿去用
    assert 用户["frozen_cash"] == 2000 * 100 + 500
    assert 用户["cash"] == 初始资金


def test_撤单把钱一分不少退回():
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    单号 = S._下单(c, 1, 买(100, 价=2000))["orderId"]
    assert 用户行(c)["frozen_cash"] == 200_500

    S.撤单(单号, 用户行(c))
    用户 = 用户行(c)
    assert 用户["frozen_cash"] == 0
    assert 用户["cash"] == 初始资金
    assert 单行(c, 单号)["status"] == "CANCELED"


def test_价格到位后挂单自动成交():
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    单号 = S._下单(c, 1, 买(100, 价=2400))["orderId"]     # 低于市价，先挂着
    assert 单行(c, 单号)["status"] == "PENDING"

    S.行情表["600030"]["last"] = 2390                    # 行情跌到限价以下
    S._撮合挂单()

    单 = 单行(c, 单号)
    assert 单["status"] == "FILLED"
    assert 单["filled_qty"] == 100
    assert 单["frozen_amount"] == 0                      # 冻结要释放干净
    assert 用户行(c)["frozen_cash"] == 0
    # 成交价必须是市价 2390，不是用户填的 2400
    成本 = c.execute("SELECT avg_cost FROM positions WHERE user_id=1 AND symbol='600030'"
                    ).fetchone()["avg_cost"]
    assert 成本 == 2390


def test_挂卖单冻股数不是冻钱():
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    S._下单(c, 1, 买(100, 价=2450))                      # 先买入 100 股
    S.下一交易日(用户行(c))                               # T+1 解禁

    卖 = S.下单请求(clientOrderId="sell1", symbol="600030", side="SELL",
                 type="LIMIT", price=3000, quantity=100)
    S._下单(c, 1, 卖)

    仓 = c.execute("SELECT * FROM positions WHERE user_id=1 AND symbol='600030'").fetchone()
    assert 仓["quantity"] == 100                         # 总持股不变，还是他的
    assert 仓["available_qty"] == 0                      # 但卖不了了
    assert 仓["frozen_qty"] == 100
    assert 用户行(c)["frozen_cash"] == 0                 # 卖单不冻钱

    # 已经冻住的股，不能再挂一次卖单
    try:
        S._下单(c, 1, S.下单请求(clientOrderId="sell2", symbol="600030", side="SELL",
                              type="LIMIT", price=3000, quantity=100))
        assert False, "重复冻结应该被拦住"
    except 规则拒绝 as e:
        assert e.code == "T1_RESTRICTED"


def test_市价单不传价格也能成交():
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    r = S._下单(c, 1, S.下单请求(clientOrderId="m1", symbol="600030",
                              side="BUY", type="MARKET", quantity=100))
    assert r["status"] == "FILLED"
    assert r["avgPrice"] == 2450
    assert 用户行(c)["cash"] == 初始资金 - 2450 * 100 - 500


def test_已成交的单不能撤():
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    单号 = S._下单(c, 1, 买(100, 价=2500))["orderId"]      # 限价高于市价，立即成交
    assert 单行(c, 单号)["status"] == "FILLED"
    try:
        S.撤单(单号, 用户行(c))
        assert False, "已成交的单不该能撤"
    except 规则拒绝 as e:
        assert e.code == "ORDER_NOT_CANCELABLE"


def test_冻结的钱不能重复花():
    """最要紧的一条：挂了 5 万的买单，剩下 5 万不能再买 10 万。"""
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    # 限价 5000（高于市价）会立即成交，所以用低于市价的 1000 来挂
    S._下单(c, 1, 买(100, 价=1000))          # 冻 100*1000+500 = 100,500
    # 可用 = 10,000,000 - 100,500 = 9,899,500，够买；再挂大额把可用花光
    S._下单(c, 1, S.下单请求(clientOrderId="big", symbol="600030", side="BUY",
                          type="LIMIT", price=1000, quantity=9800))   # 冻 9,800,500
    可用 = 用户行(c)["cash"] - 用户行(c)["frozen_cash"]
    assert 可用 < 9_800 * 1000, f"可用现金应为 {可用}"
    try:
        S._下单(c, 1, S.下单请求(clientOrderId="over", symbol="600030", side="BUY",
                              type="LIMIT", price=1000, quantity=100))
        assert False, "钱已经冻住了，不该还能下单"
    except 规则拒绝 as e:
        assert e.code == "INSUFFICIENT_CASH"


def test_数据库偶发只读会自动重试():
    """现场最容易崩心态的一种：点了买入，弹出「attempt to write a readonly database」。
    这是 SQLite 在 Windows 上被别人短暂锁住时的瞬时错误，重试一次就该成功，
    不能让选手白丢一单。这条测试用假的失败函数验证重试逻辑真的会重来。"""
    import sqlite3

    次数 = {"n": 0}

    def 前两次失败():
        次数["n"] += 1
        if 次数["n"] <= 2:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return "成功"

    assert db.重试(前两次失败) == "成功"
    assert 次数["n"] == 3, "应该试到第 3 次才成功"


def test_真错误不重试立刻抛出():
    """「no such column」这种是真 bug，重试一百次也没用，必须立刻暴露。"""
    import sqlite3

    次数 = {"n": 0}

    def 永远错():
        次数["n"] += 1
        raise sqlite3.OperationalError("no such column: foo")

    try:
        db.重试(永远错)
        assert False, "真 bug 不该被吞掉"
    except sqlite3.OperationalError as e:
        assert "no such column" in str(e)
    assert 次数["n"] == 1, "不可重试的错误只该试一次"


def test_页面没有重复的id():
    """重复 id 是浏览器里的隐形杀手：getElementById 只认文档里第一个，
    取到的可能是别的元素，读 .value 得到 undefined，然后整笔单发出去是空的。
    后端测试一条都抓不到，只能静态查。"""
    import re
    from collections import Counter

    id们 = re.findall(r'id="([^"]+)"', S.页面)
    重复 = {k: v for k, v in Counter(id们).items() if v > 1}
    assert not 重复, f"页面里有重复 id，会让 JS 取错元素：{重复}"


def test_下单要读的控件都是能取值的元素():
    """下单靠 symbol / otype / qty / price 四个控件的 value。
    它们必须落在 input / select 上，落在 span 上就永远是 undefined。"""
    import re

    for 名 in ["symbol", "otype", "qty", "price"]:
        匹配 = re.search(r'<(\w+)[^>]*id="' + 名 + r'"', S.页面)
        assert 匹配, f"页面里找不到 id={名}"
        assert 匹配.group(1) in ("input", "select", "textarea"), \
            f"id={名} 落在 <{匹配.group(1)}> 上，读 .value 只会得到 undefined"


def test_下单面板必须显示现价():
    """用户明确反馈过「交易的时候根本看不到股价」。
    下单卡片里必须有现价/涨跌/涨跌停/可买可卖四块信息，否则只能靠
    输入框里那个早就过期的数字猜自己要按什么价成交。"""
    for 元素 in ['id="lastPrice"', 'id="chg"', 'id="limits"',
                 'id="availHint"', 'id="quotes"']:
        assert 元素 in S.页面, f"下单页面缺少 {元素}"


def test_价格输入框用元不用分():
    """接口收「分」，但页面必须让人填「元」。
    要求用户把 24.52 元填成 2450 是反直觉的，也是之前最容易输错的地方。"""
    assert 'step="0.01"' in S.页面
    assert '限价（元）' in S.页面


def test_成交明细的现金流加起来等于现金变化():
    """trades 里 cashFlow 一列相加，必须正好等于现金的净变化。
    这条比逐个检查字段名管用：任何一个字段算错，这里立刻对不上。"""
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    起始 = 用户行(c)["cash"]

    S._下单(c, 1, 买(100, 价=2450))                 # 立刻成交
    c.execute("UPDATE positions SET available_qty = quantity WHERE user_id = 1")
    c.commit()
    S._下单(c, 1, S.下单请求(clientOrderId="卖1", symbol="600030", side="SELL",
                            type="LIMIT", price=2450, quantity=100))

    用户 = 用户行(c)
    明细 = S.成交明细(用户=用户)["data"]["items"]
    assert len(明细) == 2, "买了又卖，应该是两笔成交"
    assert sum(x["cashFlow"] for x in 明细) == 用户["cash"] - 起始


def test_成交明细不含挂单和撤单():
    """挂单、撤单没有产生任何现金流，混进成交流水账就不平了。"""
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    S._下单(c, 1, 买(100, 价=1000))                  # 远低于市价 → 挂单
    用户 = 用户行(c)
    assert S.成交明细(用户=用户)["data"]["items"] == []
    assert len(S.查委托(用户=用户)["data"]["items"]) == 1, "挂单应该出现在委托列表里"


def test_资产曲线起点是本金终点是当前总资产():
    """曲线第一个点必须是开赛本金，最后一个点必须等于账户接口的总资产。
    这两个对不上，前端画出来的线就是假的。"""
    c = 清场()
    S.行情表["600030"]["last"] = 2450
    S._下单(c, 1, 买(100, 价=2450))

    用户 = 用户行(c)
    曲线 = S.资产曲线(用户=用户)["data"]
    账 = S.查账户(用户=用户)["data"]

    assert 曲线["items"][0]["equity"] == 初始资金
    assert 曲线["current"]["equity"] == 账["totalAssets"]
