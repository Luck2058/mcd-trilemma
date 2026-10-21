# 麦当劳 MCP 工具速查（三路比价视角）

以下参数与返回结构均来自**真实接口实测**。服务地址 `https://mcp.mcd.cn`，Streamable HTTP，
请求头 `Authorization: Bearer <MCD_MCP_TOKEN>`，限流 600 次/分钟。

## 一、先记住四个坑

1. **金额单位不统一**：`calculate-price` 是「分」，`query-meals` 是「元」。
2. **`orderType` / `beType` / `beCode` 三元组**按渠道变化，见下表。
3. **`query-meals` 的 `data.meals` 是字典**（`{编码: 详情}`），不是数组。
4. **`query-nearby-stores` 吃城市+关键词**，不吃地址；`city` 不能带「市」。

| channel | orderType | beType | beCode |
|---|---:|---:|---|
| 麦乐送（外送） | 2 | 2 | 必传，来自 `delivery-query-stores` |
| 到店取餐 | 1 | 1 | **不传**，传了报错 |
| 得来速车道 | 1 | 5 | 必传，来自 `query-nearby-stores` |

---

## 二、门店查询

### `query-nearby-stores`（到店 / 得来速）

```json
{ "beType": 1, "searchType": 2, "city": "成都", "keyword": "麦当劳" }
```

- `beType`：`1` 到店自取，`5` 得来速车道
- `searchType`：`2` 按位置搜索（**必须用这个**）；`1` 是查收藏餐厅，没收藏会返回 `600050`
- `city`：**不带「市」**。传 `"雅安市"` 返回 `600058 城市名或者关键词不能为空`
- `keyword`：门店名关键词，如 `"四川农业大学"`

返回 `data[]`，每项含 `storeCode` / `storeName` / `address` / `distance`（米）/
`businessStatus` / `businessStartTime` / `businessEndTime`。
**只有 `beType=5` 的门店会返回 `beCode`**。

> 没有得来速门店时返回 `600047 当前位置附近没有找到合适的餐厅`——这是正常结果，不是错误。

### `delivery-query-addresses`（外送）

无入参。返回 `data.addresses[]`，取 `addressId`。
**空列表表示用户没有收货地址，整条外送链路不可用。** 不要伪造地址。

### `delivery-query-stores`（外送）

```json
{ "addressId": "xxx", "beType": 2 }
```

返回可配送门店，含 `storeCode` 与 `beCode`（外送必须带 `beCode`）。

---

## 三、菜单与券

### `query-meals`

```json
{ "storeCode": "3450336", "orderType": 1, "beType": 1 }
```

返回结构（**注意 `meals` 是字典**）：

```json
{ "data": {
    "categories": [ { "name": "人气热卖", "meals": [ {"code": "9900016081", "tags": ["..."]} ] } ],
    "meals": {
      "1440": { "name": "麦辣鸡腿汉堡", "currentPrice": "23", "originalPrice": "23",
                "image": "https://...", "canWithOrder": false }
    }
} }
```

- 名称与价格在 `data.meals`（字典），`currentPrice` 单位是**元**（字符串）；
- `data.categories` 只有编码和标签，用来做分类导航；
- `canWithOrder` 实测在多个门店都为 `false`，但它并非「不可售」的意思，**不要用它过滤**。

### `query-store-coupons`

```json
{ "storeCode": "3450192", "orderType": 1, "beType": 1 }
```

返回 `data[]`（券列表）。用户无可用券时返回空数组——**空数组是正常情况**，
此时到手价就是无券价，不要报错也不要编造券。

### `query-meal-detail`

查套餐组成与可替换项。`orderType=1 && beType=1` 时**不要传 `beCode`**，
传了会报错（官方描述："到店自提场景 beCode 是个无效参数，不要传"）。

---

## 四、核价（核心）

### `calculate-price` ⭐

```json
{
  "storeCode": "3450192",
  "orderType": 1,
  "beType": 1,
  "items": [ { "productCode": "9900005456", "quantity": 1 } ],
  "beCode": "仅外送/得来速/团餐必传"
}
```

返回：

```json
{ "data": {
    "productOriginalPrice": 4350,
    "productPrice": 3850,
    "originalPrice": 4350,
    "discount": 500,
    "price": 3850,
    "productList": [ { "productCode": "9900005456", "productName": "麦辣鸡腿汉堡三件套",
                       "quantity": 1, "originalSubtotal": 3350, "subtotal": 3350 } ]
} }
```

**`price` 单位是分，¥38.50。** 这就是本项目的真值来源：

- 到手价 = `data.price / 100`
- 商品金额 = `data.productPrice / 100`
- 优惠额 = `data.discount / 100`

券放在 `items[].couponId`（逐项）。对每张候选券各调一次，比较 `price` 取最低。

> 注意 `productPrice` 已经是**促销后**的商品金额（本例 3850，而 `productOriginalPrice` 是 4350）。
> 再叠加券的核价才是最终到手价，两者不要重复扣减。

---

## 五、其他

| 工具 | 用途 | 本项目是否使用 |
|---|---|---|
| `now-time-info` | 当前时间，判断早餐时段 | 是（可选） |
| `list-nutrition-foods` | 餐品营养 | 否（问题域不同） |
| `create-order` | 下单 | **默认不调**，需用户明确确认 |
| `auto-bind-coupons` | 一键领券 | 否（替用户做消费决策） |
| `campaign-calendar` | 当月活动日历 | 否 |
| `order-list` | 历史订单 | 否 |

## 六、一次完整比价的调用预算

```
外送   4 + 券数      取地址 + 取门店 + 取菜单 + 取券 + 核价(1 + 券数)
到店   3 + 券数
得来速 3 + 券数
```

实测：成都某门店两路核价（外送因无地址跳过）共 9 次 `tools/call`。
