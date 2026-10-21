# MCP 集成说明 · MCP_INTEGRATION

本文说明本项目**实际调用了哪些**麦当劳 MCP 能力、**按什么顺序调用**、**真实参数字段是什么样的**，
以及这套调用链路产生的业务价值。文中参数与返回结构均来自真实接口实测，非文档推测。

## 一、接入的 MCP Server

| 项 | 值 |
|---|---|
| 服务名 | 麦当劳中国 MCP Server（`mcd-mcp`，serverInfo.version `1.0.0`） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0 + SSE 响应解析） |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 工具总数 | 35 个 |
| 官方文档 | [M-China/mcd-mcp-server](https://github.com/M-China/mcd-mcp-server) |
| 限流 | 600 次 / 分钟（429 时指数退避重试） |

配置模板见 [`mcp-config.example.json`](mcp-config.example.json)，**仅含环境变量占位符，无任何真实凭证**。

客户端实现：`skills/mcd-trilemma/scripts/mcp_client.py`（纯标准库，零依赖）。
完整实现 `initialize` → `notifications/initialized` → `tools/list` → `tools/call`，
处理 `Mcp-Session-Id` 会话头、SSE `data:` 分帧、以及 `content[].text` 与 `structuredContent` 两种返回形态。

## 二、实际调用的 Tool

| # | Tool | 在项目中承担的角色 | 调用次数 |
|---|---|---|---:|
| 1 | `delivery-query-addresses` | 外送链路起点，取 `addressId` | 1 |
| 2 | `delivery-query-stores` | 按 `addressId` 取可配送门店与 `beCode` | 1 |
| 3 | `query-nearby-stores` | 到店 / 得来速取门店，按 `beType` 区分 | 1–2 |
| 4 | `query-meals` | 取门店菜单与真实 `productCode` | 每条链路 1 |
| 5 | `query-store-coupons` | 三条链路各自的券池 | 每条链路 1 |
| 6 | **`calculate-price`** | **核价真值来源，本项目核心** | 1 + 券数 |
| 7 | `now-time-info` | 判断早餐时段，避免推荐已过供应时段的餐品 | 0–1 |
| 8 | `query-meal-detail` | 套餐内替换时使用；比价主链路跳过 | 0 |
| 9 | `list-nutrition-foods` | 取 160 项官方营养数据，算「摄入 − 步行消耗」 | 1 |

**刻意不调用**：`create-order`、`cancel-order`、`auto-bind-coupons`、`mall-create-order`、
`draw-lottery`、`party-order-create`。理由见第五节。

## 三、实测发现的参数差异（这是本项目最容易翻车的地方）

官方文档给出的是业务语义，实际入参有四个必须记住的坑：

### 1. `orderType` / `beType` / `beCode` 三元组

三条链路的组合各不相同：

| channel | orderType | beType | beCode |
|---|---:|---:|---|
| 麦乐送（外送） | `2` | `2` | **必传**，来自 `delivery-query-stores` |
| 到店取餐 | `1` | `1` | **不传**，传了直接报错 |
| 得来速车道 | `1` | `5` | **必传**，来自 `query-nearby-stores` |

实测报错样例：`beType=5` 不传 `beCode` 时返回
`{"code": 600048, "message": "外送场景下，beType=2/5/6时必传beCode"}`。

而 `beType=1` 的门店对象里**根本不存在 `beCode` 字段**，只有 `beType=5` 的门店才会返回——
所以不能写死「从门店结果里取 beCode」，必须按渠道判断。

### 2. 金额单位不统一（最隐蔽的坑）

| 来源 | 字段 | 单位 |
|---|---|---|
| `calculate-price` | `data.price` / `productPrice` / `discount` | **分**（整数，`3850` = ¥38.50） |
| `query-meals` | `data.meals[code].currentPrice` | **元**（字符串，`"33.5"` = ¥33.50） |

项目里用两个不同的转换函数分开处理（`_fen()` 除 100，`_num()` 直接用）。
统一除 100 会把菜单价显示成 ¥0.34，统一不除会让到手价变成 ¥3850。

### 3. `query-meals` 的返回结构是字典不是数组

```
data.meals = { "1440": {"name": "麦辣鸡腿汉堡", "currentPrice": "23", ...}, ... }
data.categories[].meals[] = [{"code": "1440", "tags": [...]}, ...]   // 只有编码和标签
```

菜单价与名称在 `data.meals`（字典），分类导航在 `data.categories`（只有编码）。
遍历字典取 `items()`，按列表遍历会拿不到价格。

### 4. `query-nearby-stores` 用城市 + 关键词，不吃地址

```json
{ "beType": 1, "searchType": 2, "city": "成都", "keyword": "麦当劳" }
```

- `city` 不能带「市」——传 `"雅安市"` 返回 `600058 城市名或者关键词不能为空`，传 `"雅安"` 才正常；
- `searchType=1` 是「收藏餐厅」，用户没收藏过会返回 `600050`，必须用 `searchType=2` 按位置搜；
- 返回的门店带 `distance`（米），本项目用它**自动推算步行时间**（75 米/分钟 ≈ 4.5 km/h），
  用户不填步行分钟数时也能算准。

### 5. `list-nutrition-foods` 返回的是一张「文本表格」

它没有结构化 schema（入参为空对象），`data` 字段是一段字符串：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1210,289,4,12,38,165,18
  ...
```

首行 `{...}` 是列名，之后是逗号分隔的行。本项目在 `nutrition.py` 里解析成结构，
再用三档匹配（exact / approx / none）打到菜单名上——因为**菜单名和营养库产品名并不一致**：

| 菜单里的名字 | 营养库里的名字 |
|---|---|
| 薯条 | 中薯条 / 大薯条 / 小薯条 / 迷你薯条 |
| 可乐 | 可乐中杯 / 可乐大杯 / 可乐小杯 |

匹配不上时**如实标注「未匹配」，不按 0 计**——按 0 会把热量算低，用户会做出错误决策。

## 四、三条履约链路的调用流程

三条路线的取数顺序**各不相同**，这是本项目的核心工程难点——不能复用同一次门店查询结果。

### 外送（麦乐送）

```
delivery-query-addresses              → data.addresses[]，取 addressId
      ↓（地址为空则整条链路不可用，如实上报，不伪造数据）
delivery-query-stores(addressId, beType=2)  → storeCode + beCode【外送专属门店集合】
      ↓
query-meals(storeCode, orderType=2, beType=2, beCode)
      ↓
query-store-coupons(同上参数)          → 外送券池
      ↓
calculate-price(..., items[])         × (1 + 券数)
      ↓
到手价 = data.price / 100 的最小值
```

### 到店取餐

```
query-nearby-stores(beType=1, searchType=2, city, keyword)  → storeCode【无 beCode】
      ↓
query-meals(storeCode, orderType=1, beType=1)   ← 不传 beCode
      ↓
query-store-coupons(同参数)
      ↓
calculate-price(..., items[]) × (1 + 券数)
```

### 得来速车道

```
query-nearby-stores(beType=5, ...)    → storeCode + beCode【仅车道门店有 beCode】
      ↓
query-meals(storeCode, orderType=1, beType=5, beCode)
      ↓
query-store-coupons / calculate-price（同参数带 beCode）
```

> 实测：雅安查 `beType=5` 返回 `600047 当前位置附近没有找到合适的餐厅`。
> 本项目如实输出「雅安附近没有查到支持得来速车道的门店」，不退化成普通到店——
> 「这个城市没有得来速」本身就是用户需要知道的有效答案。

## 五、关键设计：为什么到手价必须由 `calculate-price` 给出

本项目**不在本地重算价格与优惠**。本地只做一件事：**枚举「用哪张券」并比较官方返回的应付总额**。

原因有三个，都是踩过的坑：

1. **满减门槛口径**：门槛按商品金额判断，而不是券后金额。按券后判断会让本该可用的券被跳过。
2. **折扣封顶**：折扣券常有封顶（如 8.5 折最高减 15），大额订单不封顶会算出负数优惠。
3. **免配送券的抵扣口径**：它只把配送费置零，**不能再抵扣商品金额**。
   这条错误最隐蔽——重复抵扣会让到手价凭空少 ¥9，结果看起来更划算，用户不会质疑，但决策就错了。
   `selftest.py` 有专项断言锁死这个口径。

麦当劳的优惠规则会变，本地硬编码的规则迟早与官方不一致。
**把核价权交还给官方接口，只让代码负责枚举与比较**，是这套链路最重要的架构决定。

## 六、刻意不用的能力

| Tool | 为什么不用 |
|---|---|
| `create-order` | 比价是帮用户看清楚，下单是替用户花钱。中间必须留一次明确确认，默认不调。 |
| `auto-bind-coupons` | 一键领完麦麦省属于替用户做消费决策，超出比价职责。 |
| `draw-lottery` / `mall-create-order` / `party-order-create` | 与「履约路线决策」的问题域无关，混进来只会让输出变糊。 |

## 七、调用预算与性能

一次完整三路比价的 `tools/call` 次数：

```
外送   4 + 券数      取地址 + 取门店 + 取菜单 + 取券 + 核价(1 + 券数)
到店   3 + 券数
得来速 3 + 券数
营养   1             整个会话只拉一次，本地缓存
合计   11 + 3 × 券数
```

券池较大时单次比价可能到 20 次以上。优化手段：

- **券池剪枝**：用券的 `threshold` 与当前商品金额比对，门槛未达的券直接跳过，不做无意义的核价；
- **门店与菜单缓存**：同一 `storeCode` 的菜单在一次会话内只拉一次（`LivePricer._menu_cache`）；
- **429 退避**：客户端内置指数退避重试，尊重 600 次/分钟的限流。

**实测记录**：成都某门店，两路核价（外送因无地址跳过）共发起 **9 次** `tools/call`。

## 八、容错与降级

| 情形 | 处理 |
|---|---|
| 某条路线取门店失败 | 该路线标记为「不可用 + 原因」，**不中断**其余两条的比价 |
| 账号无外送地址 | 外送链路标记不可用，提示「先在外卖 App 里存一个地址」，**不伪造地址** |
| 城市无得来速门店 | 得来速链路标记不可用并说明原因 |
| 某张券核价报错 | 视为该券不可用，继续尝试下一张（单张券失效属正常业务情况） |
| Token 失效（401） | 抛出明确错误提示重新申请 Token，不静默返回假数据 |
| 触发限流（429） | 指数退避重试 3 次，仍失败则降级为 `--offline` 并**明确标注样例数据** |
| 餐品编码不在菜单 | 报「该门店菜单不含：xxx」，`--search` 可查真实编码 |
| 字段名变化 | `deep_find()` 多候选键名容错提取；`--probe` 打印原始返回便于快速适配 |

## 九、业务价值

1. **把「省不省钱」变成「值不值得」**。绝大多数点餐工具停在「哪条路便宜」，
   本项目补的是后半句——用临界时薪把价差折算成时间成本，让结论能和用户自己的工资对话。
2. **把三套券池的差异显性化**。同一份餐，外送券、到店券、车道券互不通用，
   用户手里的券在哪条路上能用、能省多少，本项目给出确定答案。
3. **揭示反直觉的凑单套利**。差 ¥2.5 够不到免配送门槛时，加一份商品反而可能让总价下降
   （样例中加 ¥8 的香芋派，到手价从 ¥42.50 降到 ¥41.50）。算的是**边际代价**，不是商品原价。
4. **自动推算步行时间**。门店返回 `distance`，直接换算成单程步行分钟数，用户不用自己估。
5. **同一趟路，第二笔账也算了**。三条路的餐是同一份，摄入一样，但走过去会消耗、坐着等不会。
   本项目把「摄入 − 步行消耗 = 净热量」并进主表，让「下楼」这件事同时有金钱和时间两侧的回报。
   **只做收支对照，不给「每元热量」这类指标**——那会诱导「花更少的钱吃更多热量」，属于宣扬不健康饮食。
6. **可复用于任意履约场景**。模型抽象为「价格 × 时间」二维决策，
   临界时薪这套算法同样适用于生鲜配送、同城跑腿、快递自提。

## 十、如何验证本文描述与实际一致

```bash
# 1. 打印 tools/list 与关键工具的原始返回
python skills/mcd-trilemma/scripts/trilemma.py --probe

# 2. 查门店真实菜单（拿到可用的 productCode）
python skills/mcd-trilemma/scripts/trilemma.py --search 麦辣鸡腿 --city 成都

# 3. 走真实 MCP 三路核价
MCD_MCP_TOKEN=xxx python skills/mcd-trilemma/scripts/trilemma.py \
  --live --city 成都 --request sample_data/request_live.json

# 4. 离线验证核价逻辑（30 项断言，不需要 Token）
python skills/mcd-trilemma/scripts/selftest.py
```
