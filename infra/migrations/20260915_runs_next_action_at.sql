-- 「到点再来找我」：时间本身成为一种唤醒条件（checklist I03.11）。
--
-- 没有这一列时，能推进一条线的只有「有人批了」「清洗轮开了」「WIP 降下来了」。
-- 「已批准，等今晚 01:00 的窗口」无处表达 —— 要么被立刻拉起来（窗口外执行），
-- 要么每分钟问一遍。NULL = 没有排期，随时可推进。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS next_action_at DOUBLE PRECISION;
