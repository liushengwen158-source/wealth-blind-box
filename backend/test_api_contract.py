# 契约字段的守门测试。
#
# 为什么单独一个文件：docs/api.md 是和另外 4 个分支签的字。
# 前端（选手端、排行榜、后台）是照着文档先做 Mock 的，他们取 data.points[].ts、
# data.positionCount 这些名字。我方代码一旦把字段改名或漏给，前端拿到 undefined，
# 页面就变成空白 —— 而单元测试全绿，只有联调那天才发现。所以字段名本身也要测。
#
# 这里只断言「文档要求的键在不在、类型对不对」，不测具体数值（数值由别的测试管）。
# 临时库路径由 conftest.py 统一设置，不要再在这个文件里改 db.数据库文件。
import db                               # noqa: E402

import main as S                     # noqa: E402
from trading_core import 规则拒绝       # noqa: E402

初始资金 = 10_000_000


def 清场():
    """让引擎处于可交易状态，账户清零，行情价钉死。"""
    S.引擎.开赛(总回合数=1, 回合毫秒=600_000)
    S.行情表["600030"]["last"] = 2450
    c = db.连接()
    c.execute("DELETE FROM orders")
    c.execute("DELETE FROM round_results")
    c.execute("UPDATE users SET cash = initial_cash, frozen_cash = 0")
    c.execute("UPDATE positions SET quantity=0, available_qty=0, frozen_qty=0, avg_cost=0")
    c.commit()
    return c


def 用户行(c, uid=1):
    return c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def 买100手():
    return S.下单请求(clientOrderId="c1", symbol="600030", side="BUY",
                    type="LIMIT", price=2450, quantity=100)


def 有键(数据, 键们):
    缺 = [k for k in 键们 if k not in 数据]
    assert not 缺, f"契约要求的字段没给：{缺}；实际给的是 {sorted(数据)}"


# ---------------- 账户 / 持仓 ----------------

def test_账户字段和文档一致():
    c = 清场()
    d = S.查账户(用户行(c))["data"]
    有键(d, ["initialCash", "cash", "frozenCash", "marketValue",
             "totalAssets", "pnl", "pnlPct", "positionCount"])
    assert d["positionCount"] == 0


def test_持仓字段和文档一致_含浮盈浮亏():
    c = 清场()
    S._下单(c, 1, 买100手())
    S.行情表["600030"]["last"] = 2500          # 涨上去了
    d = S.查持仓(用户行(c))["data"]
    项 = d["items"][0]
    有键(项, ["symbol", "name", "quantity", "availableQty", "avgCost",
              "last", "marketValue", "pnl", "pnlPct"])
    # 成本 2450，现价 2500，100 股 → 浮盈 5000 分 = 50 元
    assert 项["pnl"] == 5000


def test_成交字段和文档一致_尤其ts():
    c = 清场()
    S._下单(c, 1, 买100手())
    d = S.成交明细(None, 1, 20, 用户行(c))["data"]
    项 = d["items"][0]
    有键(项, ["tradeId", "orderId", "symbol", "side", "price", "quantity",
              "amount", "fee", "tax", "transferFee", "ts"])
    assert isinstance(项["ts"], str) and 项["ts"]   # 前端直接塞进时间轴，不能是 null


# ---------------- 资产曲线 ----------------

def test_资产曲线给的是points不是items():
    """这是最容易踩的一个坑：我原来只给 items（给页面用的中文标签版），
    文档要的是 points[{ts,totalAssets}]。前端按文档取 data.points 会拿到 undefined。"""
    c = 清场()
    d = S.资产曲线(用户行(c))["data"]
    有键(d, ["points"])
    assert d["points"], "曲线至少要有一个点"
    for p in d["points"]:
        有键(p, ["ts", "totalAssets"])
        assert isinstance(p["totalAssets"], int)


def test_资产曲线首点是本金末点是现在():
    c = 清场()
    S.引擎.总回合数 = 1
    开 = S.资产曲线(用户行(c))["data"]["points"]
    assert 开[0]["totalAssets"] == 初始资金
    assert 开[-1]["totalAssets"] == 初始资金


def test_资产曲线横轴不倒退():
    """回合结算时刻是真实时间，点必须按时间从前到后，图才不会画成锯齿。"""
    c = 清场()
    S._下单(c, 1, 买100手())
    S._结算并推进()                    # 结算第 1 回合，写入一条带时刻的成绩
    点 = S.资产曲线(用户行(c))["data"]["points"]
    时刻们 = [p["ts"] for p in 点]
    assert 时刻们 == sorted(时刻们), f"时间轴乱序：{时刻们}"


# ---------------- 排行榜 ----------------

def test_排行榜字段和文档一致():
    d = S.排行榜()["data"]
    有键(d, ["items", "updatedAt"])
    assert d["items"]
    for 项 in d["items"]:
        有键(项, ["rank", "userId", "nickname", "totalAssets",
                  "returnPct", "roundReturnPct", "trades"])
    assert [i["rank"] for i in d["items"]] == list(range(1, len(d["items"]) + 1))


def test_排行榜按收益率排而不是按资产排():
    """资产多不等于收益高：初始资金不同的话，按 totalAssets 排会排错人。
    这里让两人资产正好差一档，但收益率必须由小到大反过来。"""
    c = 清场()
    c.execute("UPDATE users SET cash = 12000000, initial_cash = 20000000 WHERE id = 2")
    c.execute("UPDATE users SET cash = 10100000, initial_cash = 10000000 WHERE id = 3")
    c.commit()
    榜 = S.排行榜()["data"]["items"]
    收益率 = [i["returnPct"] for i in 榜]
    assert 收益率 == sorted(收益率, reverse=True)


# ---------------- 赛制：整场结算 / 结束锁定 ----------------

def test_结算整场只留一条成绩并且不再开新回合():
    c = 清场()
    S.引擎.开赛(总回合数=3, 回合毫秒=600_000)
    S.起始资产.clear()
    S._快照起始资产()
    状态 = S.结算整场()["data"]
    assert 状态["status"] == "SETTLED"
    assert 状态["totalRounds"] == 1, "整场结算后不该还剩 2 个回合没打"
    # 成绩表是按「人 × 回合」存的：3 个选手各一条，但只应该有第 1 回合这一批
    批次 = c.execute("SELECT round_index, COUNT(*) AS n FROM round_results "
                    "GROUP BY round_index").fetchall()
    assert [r["round_index"] for r in 批次] == [1], f"结算了不止一回合：{list(批次)}"
    人数 = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert 批次[0]["n"] == 人数, "每个人都该有且只有一条本回合成绩"


def test_结束锁定后不能下单也不改账():
    c = 清场()
    S.引擎.开赛(总回合数=3, 回合毫秒=600_000)
    钱前 = 用户行(c)["cash"]
    状态 = S.结束锁定()["data"]
    assert 状态["status"] == "ENDED"
    assert 用户行(c)["cash"] == 钱前, "锁定只是关门，不能动钱"
    try:
        S._下单(c, 1, 买100手())
        assert False, "锁定之后竟然还能下单"
    except 规则拒绝 as e:
        assert e.code == "ROUND_NOT_RUNNING"


def test_锁定会退掉没成交的挂单冻结款():
    c = 清场()
    S.引擎.开赛(总回合数=3, 回合毫秒=600_000)
    S.行情表["600030"]["last"] = 2450
    S._下单(c, 1, S.下单请求(clientOrderId="c9", symbol="600030", side="BUY",
                            type="LIMIT", price=2000, quantity=100))
    assert 用户行(c)["frozen_cash"] > 0
    S.结束锁定()
    assert 用户行(c)["frozen_cash"] == 0, "锁了就该把冻结的钱退回去，不能扣着"
