# 财富盲盒 API 契约（v1）

> 这份文档是 5 个分支的接口约定，先定字段，再写代码。
> 前端可以按本文示例先做 Mock；后端按本文实现。字段变更必须全员同意。
> 文中出现的时长、金额等数值都只是示例，实际由管理员在后台配置。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| 基础路径 | `/api/v1` |
| 数据格式 | `application/json; charset=utf-8` |
| 认证方式 | `Authorization: Bearer <token>` |
| 金额与价格 | **整数，单位：分**（例如 12.34 元写作 `1234`） |
| 数量 | 整数，单位：股；A 股按 100 股整手 |
| 费率 | 小数，例如万分之三写作 `0.0003` |
| 时间 | ISO 8601，带时区，例如 `2026-11-04T19:30:00+08:00` |
| 模拟日期 | `YYYY-MM-DD`，用于 T+1 判断 |
| ID | 用户、订单、成交用整数或字符串；事件用事件编码 |
| 分页 | `page`（从 1 开始）、`pageSize`（默认 20，最大 100） |
| 幂等 | 下单必须带 `clientOrderId`，同一用户内唯一 |

### 统一返回结构

成功：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

失败：

```json
{
  "code": "INSUFFICIENT_CASH",
  "message": "可用资金不足",
  "data": null
}
```

- `code` 为 0 表示成功，其余为错误码（见第 12 节）。
- HTTP 状态码同时表达大类：`401` 未登录、`403` 无权限、`404` 不存在、`409` 重复提交、`422` 业务规则拒绝、`500` 服务异常。

### 分页返回

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [],
    "page": 1,
    "pageSize": 20,
    "total": 0
  }
}
```

## 2. 认证

### POST /auth/login

选手登录。

```json
{
  "studentNo": "20260001",
  "nickname": "小王",
  "inviteCode": "WB2026"
}
```

返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "token": "eyJhbGciOi...",
    "user": { "id": 12, "studentNo": "20260001", "nickname": "小王", "role": "PLAYER" }
  }
}
```

### POST /auth/admin/login

管理员登录。系统只有一个管理员账号 `admin`。

```json
{ "username": "admin", "password": "由环境变量配置" }
```

返回结构同上，`role` 为 `ADMIN`。

### GET /auth/me

返回当前登录用户与比赛席位。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": 12,
    "studentNo": "20260001",
    "nickname": "小王",
    "role": "PLAYER",
    "status": "ACTIVE"
  }
}
```

## 3. 比赛状态与回合

### GET /game/state

比赛大厅和选手端都用这个接口。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "status": "RUNNING",
    "roundIndex": 3,
    "totalRounds": 12,
    "roundRemainMs": 180000,
    "simDate": "2015-06-15",
    "serverNow": "2026-11-04T19:33:00+08:00",
    "event": {
      "id": "EV201506_LEVERAGE_CRASH",
      "title": "2015 年 6 月去杠杆股灾",
      "date": "2015-06-12",
      "category": "CRISIS",
      "summary": "清查场外配资，杠杆资金撤离，指数快速回落。",
      "direction": "BEAR",
      "dataQuality": "DAILY"
    }
  }
}
```

- `status`：`IDLE`、`RUNNING`、`PAUSED`、`SETTLED`。
- `roundRemainMs`：当前回合剩余毫秒数；暂停时冻结。
- `dataQuality`：`MINUTE` 或 `DAILY`，用于页面提示。

### GET /results/me

个人每回合结果与最终成绩。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "rounds": [
      {
        "roundIndex": 1,
        "eventId": "EV201506_LEVERAGE_CRASH",
        "startEquity": 100000000,
        "endEquity": 98000000,
        "pnl": -2000000,
        "pnlPct": -0.02,
        "forcedClose": true
      }
    ],
    "final": {
      "totalAssets": 98000000,
      "totalReturnPct": -0.02
    }
  }
}
```

### GET /account/equity

资产曲线数据。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "points": [
      { "ts": "2026-11-04T19:20:00+08:00", "totalAssets": 100000000 },
      { "ts": "2026-11-04T19:25:00+08:00", "totalAssets": 101200000 }
    ]
  }
}
```

## 4. 历史事件

### GET /events

参数：`era`、`category`、`enabled`，均可选。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": "EV201506_LEVERAGE_CRASH",
        "title": "2015 年 6 月去杠杆股灾",
        "date": "2015-06-12",
        "era": "2010s",
        "category": "CRISIS",
        "region": "CN",
        "direction": "BEAR",
        "dataQuality": "DAILY",
        "enabled": true
      }
    ],
    "page": 1,
    "pageSize": 20,
    "total": 14
  }
}
```

### GET /events/{id}

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": "EV201506_LEVERAGE_CRASH",
    "title": "2015 年 6 月去杠杆股灾",
    "date": "2015-06-12",
    "era": "2010s",
    "category": "CRISIS",
    "region": "CN",
    "summary": "清查场外配资，杠杆资金撤离，指数快速回落。",
    "direction": "BEAR",
    "sectors": [
      { "name": "证券", "direction": "BEAR" },
      { "name": "创业板", "direction": "BEAR" }
    ],
    "timeline": [
      { "date": "2015-06-12", "fact": "上证指数盘中见 5178 点" },
      { "date": "2015-06-15", "fact": "监管要求清理场外配资" }
    ],
    "news": [
      "监管层要求券商自查场外配资，杠杆资金开始撤离。"
    ],
    "symbols": ["600030", "601688", "300059"],
    "dataQuality": "DAILY",
    "dataSource": "AKShare 历史行情",
    "enabled": true
  }
}
```

## 5. 行情

### GET /market/symbols

参数：`eventId`（可选，不传则返回当前回合的股票池）。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "symbol": "600030",
        "name": "中信证券",
        "sector": "证券",
        "lotSize": 100,
        "prevClose": 2450,
        "limitUp": 2695,
        "limitDown": 2205,
        "tradable": true
      }
    ]
  }
}
```

### GET /market/quotes

参数：`symbols`，多个用逗号分隔。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "symbol": "600030",
        "last": 2480,
        "open": 2450,
        "high": 2495,
        "low": 2432,
        "change": 30,
        "changePct": 0.0122,
        "volume": 1200000,
        "ts": "2015-06-15T10:03:00+08:00"
      }
    ]
  }
}
```

### GET /market/kline

参数：`symbol`、`period`（`1m` 或 `1d`）、`limit`（默认 240）、`eventId`（可选）。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "symbol": "600030",
    "period": "1m",
    "bars": [
      { "ts": "2015-06-15T10:00:00+08:00", "open": 2450, "high": 2495, "low": 2432, "close": 2480, "volume": 120000 }
    ]
  }
}
```

## 6. 交易与账户

### POST /orders

```json
{
  "clientOrderId": "u12-20261104-0001",
  "symbol": "600030",
  "side": "BUY",
  "type": "LIMIT",
  "price": 2480,
  "quantity": 100
}
```

返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "orderId": "ORD20261104123456",
    "status": "FILLED",
    "filledQty": 100,
    "avgPrice": 2480,
    "fee": 744,
    "tax": 0,
    "transferFee": 2
  }
}
```

- `side`：`BUY` / `SELL`。
- `type`：`LIMIT` / `MARKET`。
- 市价单可不传 `price`。
- 买入费用 = 佣金；卖出费用 = 佣金 + 印花税 + 过户费。
- 被规则拒绝时返回 `422` + 错误码，不创建订单。

### DELETE /orders/{orderId}

撤单。返回该订单最新状态。

### GET /orders

参数：`status`（`PENDING` / `PARTIAL` / `FILLED` / `CANCELED` / `REJECTED`）、`page`、`pageSize`。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "orderId": "ORD20261104123456",
        "clientOrderId": "u12-20261104-0001",
        "symbol": "600030",
        "side": "BUY",
        "type": "LIMIT",
        "price": 2480,
        "quantity": 100,
        "filledQty": 100,
        "status": "FILLED",
        "rejectReason": null,
        "createdAt": "2026-11-04T19:24:00+08:00"
      }
    ],
    "page": 1,
    "pageSize": 20,
    "total": 1
  }
}
```

### GET /trades

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "tradeId": "TRD20261104123456",
        "orderId": "ORD20261104123456",
        "symbol": "600030",
        "side": "BUY",
        "price": 2480,
        "quantity": 100,
        "amount": 248000,
        "fee": 744,
        "tax": 0,
        "transferFee": 2,
        "ts": "2026-11-04T19:24:01+08:00"
      }
    ],
    "page": 1,
    "pageSize": 20,
    "total": 1
  }
}
```

### GET /positions

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "symbol": "600030",
        "name": "中信证券",
        "quantity": 100,
        "availableQty": 0,
        "avgCost": 2480,
        "last": 2490,
        "marketValue": 249000,
        "pnl": 928,
        "pnlPct": 0.0037
      }
    ]
  }
}
```

> `availableQty` 受 T+1 和冻结影响，买入当天为 0。

### GET /account

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "initialCash": 100000000,
    "cash": 99551856,
    "frozenCash": 0,
    "marketValue": 249000,
    "totalAssets": 99800856,
    "pnl": -199144,
    "pnlPct": -0.001991,
    "positionCount": 1
  }
}
```

## 7. 盲盒

### GET /boxes/me

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "box": null
  }
}
```

开赛后：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "box": {
      "id": 3,
      "status": "UNOPENED",
      "seedHash": "9f2c...",
      "card": null
    }
  }
}
```

### POST /boxes/open

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "boxId": 3,
    "card": {
      "code": "NO_COMMISSION",
      "name": "免佣卡",
      "rarity": "RARE",
      "description": "本场前 3 笔交易免佣金",
      "effect": { "type": "FEE_FREE", "tradeCount": 3 }
    },
    "seed": "a1b2c3"
  }
}
```

## 8. 新闻

### GET /news

参数：`since`（可选，ISO 时间）。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": "N20150615001",
        "eventId": "EV201506_LEVERAGE_CRASH",
        "ts": "2015-06-15T09:35:00+08:00",
        "title": "监管要求券商自查场外配资",
        "summary": "杠杆资金开始撤离，市场情绪转弱。",
        "isHistorical": true,
        "source": "历史事件摘要"
      }
    ]
  }
}
```

所有新闻 `isHistorical` 恒为 `true`，页面必须标注“历史重演，仅用于教学”。

## 9. 排行榜

### GET /ranking

参数：`limit`（默认 100）。

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "rank": 1,
        "userId": 8,
        "nickname": "小李",
        "totalAssets": 112000000,
        "returnPct": 0.12,
        "roundReturnPct": 0.035,
        "trades": 12
      }
    ],
    "updatedAt": "2026-11-04T19:25:00+08:00"
  }
}
```

排行榜只有个人总榜，没有团队榜。

## 10. 管理员接口

所有 `/admin/**` 接口要求 `role=ADMIN`，选手 token 返回 `403`。

### GET /admin/config

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "durationMinutes": 60,
    "roundMinutes": 5,
    "initialCash": 100000000,
    "randomSeed": "WB2026",
    "allowRepeatEvent": false,
    "eventPool": {
      "eras": ["2010s", "2020s"],
      "categories": ["CRISIS", "POLICY"]
    },
    "forceCloseFeeFree": true,
    "newsEnabled": true,
    "fees": {
      "commissionRate": 0.0003,
      "minCommission": 500,
      "stampTaxRate": 0.0005,
      "transferFeeRate": 0.00001
    }
  }
}
```

### PUT /admin/config

请求体同 `GET /admin/config` 的 `data`。保存前必须校验：时长与回合数为正整数、初始资金大于 0、事件池不为空。

### GET /admin/events

返回事件库列表（含未启用的）。

### PUT /admin/events/{id}

```json
{ "enabled": true }
```

### 比赛控制

| 接口 | 说明 |
|---|---|
| `POST /admin/game/start` | 开始比赛 |
| `POST /admin/game/pause` | 暂停，冻结倒计时与行情 |
| `POST /admin/game/resume` | 继续 |
| `POST /admin/game/skip` | 立即结束当前回合，强制平仓并进入下一回合 |
| `POST /admin/game/settle` | 立即结束整场比赛并结算 |
| `POST /admin/game/end` | 结束并锁定，不再进入下一回合 |

以上接口统一返回最新 `game/state`。

### 新闻管理

| 接口 | 说明 |
|---|---|
| `GET /admin/news` | 新闻列表，含已撤回 |
| `POST /admin/news` | 手动发布一条历史摘要 |
| `DELETE /admin/news/{id}` | 撤回新闻 |

```json
{
  "eventId": "EV201506_LEVERAGE_CRASH",
  "title": "监管要求券商自查场外配资",
  "summary": "杠杆资金开始撤离，市场情绪转弱。"
}
```

### 成绩导出

`GET /admin/export?type=results|trades|orders|events`

返回文件流，文件名通过 `Content-Disposition` 给出，格式为 CSV 或 XLSX。

## 11. WebSocket

连接地址：

```
ws://<host>/api/v1/ws/market?token=<token>
ws://<host>/api/v1/ws/me?token=<token>
ws://<host>/api/v1/ws/ranking?token=<token>
ws://<host>/api/v1/ws/game?token=<token>
ws://<host>/api/v1/ws/news?token=<token>
```

统一消息结构：

```json
{
  "type": "tick",
  "ts": "2026-11-04T19:25:03+08:00",
  "data": {}
}
```

| type | 频道 | 说明 |
|---|---|---|
| `tick` | market | 行情推送 |
| `round_start` | game | 新回合开始，带事件信息 |
| `round_end` | game | 回合结束，带强制平仓结果 |
| `game_state` | game | 开始/暂停/继续/结算 |
| `news` | news | 新新闻 |
| `ranking` | ranking | 排行榜快照 |
| `order_update` | me | 订单状态变化 |
| `account_update` | me | 账户与持仓变化 |
| `box_update` | me | 盲盒状态变化 |

行情订阅：连接后发送

```json
{ "type": "subscribe", "data": { "symbols": ["600030", "601688"] } }
```

心跳：服务端每 30 秒发送 `{"type":"heartbeat"}`，客户端 60 秒未收到应重连。

## 12. 错误码

| 错误码 | HTTP | 含义 |
|---|---|---|
| `AUTH_REQUIRED` | 401 | 未登录或 token 失效 |
| `AUTH_INVALID` | 401 | 账号或密码错误 |
| `FORBIDDEN` | 403 | 无权限访问 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `DUPLICATE_CLIENT_ORDER_ID` | 409 | 重复下单请求 |
| `ROUND_NOT_RUNNING` | 422 | 当前不在交易状态 |
| `MARKET_CLOSED` | 422 | 非交易时段 |
| `LIMIT_UP` | 422 | 涨停不可买入 |
| `LIMIT_DOWN` | 422 | 跌停不可卖出 |
| `T1_RESTRICTED` | 422 | T+1 未解禁 |
| `INVALID_LOT` | 422 | 数量不是整手 |
| `INSUFFICIENT_CASH` | 422 | 可用资金不足 |
| `INSUFFICIENT_POSITION` | 422 | 可卖持仓不足 |
| `SYMBOL_NOT_TRADABLE` | 422 | 标的不在当前回合或停牌 |
| `ORDER_NOT_FOUND` | 404 | 订单不存在 |
| `RATE_LIMITED` | 429 | 下单过于频繁 |
| `CONFIG_INVALID` | 422 | 后台配置不合法 |
| `INTERNAL_ERROR` | 500 | 服务异常 |

## 13. 示例数据（Mock）

前端在没有后端时，直接用下面的数据启动：

`GET /game/state`

```json
{
  "status": "RUNNING",
  "roundIndex": 2,
  "totalRounds": 12,
  "roundRemainMs": 240000,
  "simDate": "2015-06-15",
  "event": {
    "id": "EV201506_LEVERAGE_CRASH",
    "title": "2015 年 6 月去杠杆股灾",
    "date": "2015-06-12",
    "summary": "清查场外配资，杠杆资金撤离。",
    "direction": "BEAR",
    "dataQuality": "DAILY"
  }
}
```

`GET /market/symbols`

```json
{
  "items": [
    { "symbol": "600030", "name": "中信证券", "sector": "证券", "lotSize": 100, "prevClose": 2450, "limitUp": 2695, "limitDown": 2205, "tradable": true },
    { "symbol": "601688", "name": "华泰证券", "sector": "证券", "lotSize": 100, "prevClose": 1830, "limitUp": 2013, "limitDown": 1647, "tradable": true }
  ]
}
```

`GET /account`

```json
{
  "initialCash": 100000000,
  "cash": 95000000,
  "frozenCash": 0,
  "marketValue": 4800000,
  "totalAssets": 99800000,
  "pnl": -200000,
  "pnlPct": -0.002,
  "positionCount": 1
}
```

`GET /ranking`

```json
{
  "items": [
    { "rank": 1, "userId": 8, "nickname": "小李", "totalAssets": 112000000, "returnPct": 0.12, "roundReturnPct": 0.035, "trades": 12 },
    { "rank": 2, "userId": 12, "nickname": "小王", "totalAssets": 99800000, "returnPct": -0.002, "roundReturnPct": -0.01, "trades": 3 }
  ],
  "updatedAt": "2026-11-04T19:25:00+08:00"
}
```

## 14. 变更规则

1. 本文档在 `main` 分支维护，分支五负责收集变更。
2. 任何字段增删改，先在群里说明，全员同意后由分支五更新本文档。
3. 字段只增不删；必须删时保留旧字段并标记为废弃。
4. 前端按本文档做 Mock，后端按本文档实现，联调时以本文档为准。
5. 金额、价格一律用整数分，不允许用浮点数传输。
