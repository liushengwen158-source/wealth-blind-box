# 交易 + 赛制 + 结算（分支二 `feat/trade-core` 的后端）

这个目录只负责 README 里分给分支二的三件事：**交易规则、回合推进、回合结算**。
行情回放（分支一）、选手端页面（分支三）、排行榜（分支四）、后台（分支五）不在这里实现。

## 怎么跑起来

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8000        # 打开 http://127.0.0.1:8000
pytest -q                           # 跑自测，37 项
```

第一次启动会自动建 `wealth.db` 并塞 3 个测试选手（小王 / 小李 / 小张），每人 10 万元。
接口文档：<http://127.0.0.1:8000/docs>（FastAPI 自动生成）。

## 文件是干什么的

| 文件 | 负责什么 |
|---|---|
| `trading_core.py` | 纯计算：手续费、买卖后的现金/持仓/成本。**不碰数据库**，所以能单独测 |
| `round_engine.py` | 回合状态机：第几回合、还剩几毫秒、暂停/继续/结算/锁定。**不碰数据库** |
| `db.py` | 建表、连接、老库补列、锁冲突重试 |
| `main.py` | 所有 HTTP 接口 + 一个自己用的演示页面（页面只是方便手点，分支三交付后删掉） |
| `conftest.py` + `test_*.py` | 自测。用临时数据库，不会弄脏 `wealth.db` |

## 已实现，且与 `docs/api.md` 对得上的接口

| 接口 | 说明 |
|---|---|
| `GET /api/v1/game/state` | 比赛状态、当前事件、剩余毫秒。暂停时倒计时冻结 |
| `POST /api/v1/orders` | 下单（LIMIT / MARKET），必须带 `clientOrderId` |
| `DELETE /api/v1/orders/{orderId}` | 撤单，冻结的钱股原路退回 |
| `GET /api/v1/orders` | 委托列表，分页 + 按状态筛 |
| `GET /api/v1/trades` | 成交明细（只有真正成交的） |
| `GET /api/v1/positions` | 持仓，含 `availableQty`（T+1）、浮盈 `pnl` |
| `GET /api/v1/account` | 资产总览：现金、冻结、市值、总资产、收益率 |
| `GET /api/v1/account/equity` | 资产曲线 `points[{ts,totalAssets}]` |
| `GET /api/v1/results/me` | 每回合成绩 + 最终成绩 |
| `GET /api/v1/ranking` | 排行榜（按收益率排） |
| `POST /api/v1/admin/game/start` | 开赛，体里给 `rounds`、`seconds` |
| `POST /api/v1/admin/game/pause` `resume` `skip` | 暂停 / 继续 / 跳过本回合 |
| `POST /api/v1/admin/game/settle` | 立刻结束整场并结算 |
| `POST /api/v1/admin/game/end` | 结束并锁定，不再接受下单 |

## 规则口径（联调时按这个算，别各算一套）

- 金额、价格、费用**一律整数分**，接口里不出现小数元。`费率` 才是小数。
- 费用：买入只收佣金（万分之三，最低 5 元）；卖出再加印花税（千分之一）和过户费（十万分之一）。
- 向上取整用 `int(x + 0.5)`，**不能用 Python 的 `round()`** —— 它是银行家舍入，`round(2.5)` 得 2，算钱会差一分。
- 限价只决定「能不能成交」，**成交价永远是当时的市价**，不是你的限价。
- 校验顺序固定：回合状态 → 标的是否可交易 → 整手 → 涨跌停 → T+1 → 资金/持仓。先撞哪条就报哪个错。
- 挂单冻结：买单冻 `限价×数量+预估佣金`，卖单冻股数。撤单全额退回，一分不差。
- 回合结束强制平仓**免手续费**，按最后成交价把持仓变成现金，钱带入下一回合。
- 幂等：`UNIQUE(user_id, clientOrderId)`，重复提交返回 409 `DUPLICATE_CLIENT_ORDER_ID`。

## 和 `docs/api.md` 的差异（必须知道的几处）

这几处是演示版有意简化的，正式联调前需要补或改，**改动前请在群里同步，按文档第 14 节的变更规则走**：

1. **没有 `/auth/login`**。现在 `Authorization: Bearer <用户id>`，token 直接就是整数 id。属于分支三/五的活，那边做好后把 `认人()` 换掉即可，签名的依赖注入点只有一处。
2. **行情是内存里的假数据**（`main.py` 顶部 `行情表`，4 只票，价格随机游走）。分支一的 `feat/market-event` 交付后，把 `行情表` 换成读它的接口/CSV 即可，交易侧不用改。
3. **没有 WebSocket**（`/ws/market`、`/ws/me` 等）。现在是页面 3 秒轮询，分支三/四接了 WS 之后可以把轮询去掉。
4. **`/admin/**` 没有校验 `role=ADMIN`**，任何人能调。正式版必须加，否则选手能自己给自己开赛。
5. **没做盲盒（`/boxes`）和新闻（`/news`）**，不在分支二范围内。
6. **排行榜的并列规则**只做到「收益率 → 交易次数」，最大回撤还没算（要历史资产序列，等曲线数据齐了再补）。

## 状态机（画页面的同学按这个来）

```
IDLE ──开赛──> RUNNING ──暂停──> PAUSED ──继续──> RUNNING
                  │  │                              │
                  │  └──到点/跳过──> 结算本回合 ──还有回合──> RUNNING（下一回合）
                  │                        └──没有回合了──> SETTLED
                  └──结束并锁定──> ENDED（不解算，账停在当前）
```

只有 `RUNNING` 能下单。`PAUSED` / `SETTLED` / `ENDED` 下单都会返回 422 `ROUND_NOT_RUNNING`。
