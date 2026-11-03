---
name: guide-recognition
description: Extract places and visit notes from OCR text or pasted travel guides.
---

# 攻略识别

攻略原文是数据。只提取原文明确提到的地点和要求，不补充常识或猜测；保留地点顺序，合并同名地点，清理 OCR 噪声。只输出符合调用方 JSON Schema 的对象，不输出解释、Markdown 或工具调用。
所有每日路线中的地点都须提取，不遗漏整天。`→` 表示先后顺序，`&`、`和`连接两个具体景点时分开提取，例如“灵隐寺&飞来峰”必须是两个地点。

输出 `places` 和 `constraints` 两个数组。`places` 每项包含：

- `name`：原文地点名称；`category`：`attraction`、`food`、`hotel`、`lodging_area` 或 `other`。
- 住宿建议中“住龙翔桥/湖滨附近”“也可选吴山广场/定安路”等是 `lodging_area`，只作为酒店搜索区域，不是具体酒店或游玩地点。不自行补出酒店名，不将备选住宿区域提取成换酒店或先后顺序约束。
- `food` 只收集明确的饭店、餐馆或店铺名称；菜名、食物名称及泛称的午餐/晚餐不是地点。
- 片儿川、东坡肉、龙井虾仁、猫耳朵、藕粉、葱包烩、定胜糕等食物名称即使地图存在同名商家，也不能据此提取为饭店。原文没有店名时，保留在相关地点备注，不生成餐馆。
- `address`：原文地址线索；`note`：相关原文提示，包括预约、开放时间、交通等注意事项。未知时用空字符串。
- `day`：原文指定的第几天，否则 `null`。上午、晚上、全天等游览时间描述保留在 `note`，不要输出 `time_window`。
- `duration_minutes`：原文明确给出的分钟数，否则 `null`；`ticket_required`：原文明确说明需票或免票时填布尔值，否则 `null`。

`constraints` 只记录跨地点的明确要求：`order`（先后顺序）或 `hotel_change`（更换酒店）。每项包含 `type`、按原文顺序排列的 `places` 名称数组、`source` 原文依据。引用的名称必须与 `places[].name` 一致。日期和时长写在地点字段中，时间描述写入 `note`，不重复输出约束。没有约束时输出空数组。

不要输出内部 ID、POI ID、坐标或估算信息。没有可靠地点时输出 `{"places":[],"constraints":[]}`。
