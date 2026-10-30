export const transportLabels = { train: "火车", flight: "飞机", bus: "大巴", drive: "自驾" };
export const categoryLabels = { attraction: "游玩地点", food: "吃饭地点", hotel: "酒店", other: "其他" };
export const isMovableStop = (item) => ["attraction", "food", "other"].includes(item.category);
