# 实现原理

## 为什么不能用原版的 `logAdminCommands`

原版有个 `logAdminCommands` 游戏规则，打开后管理命令会以 `[玩家名: /命令]` 的格式广播 ——
但**只广播给在线的 OP 玩家（聊天栏）**，管理员源（控制台 / 命令方块）执行的命令
不会写进日志文件。实测确认：

```
# 打开 logAdminCommands 后从控制台执行 /time set day，日志里只有：
[17Sep2026 00:59:29.895] [Server thread/INFO] [net.minecraft.server.MinecraftServer/]:
    Set the time to 1000
# 完全没有 [玩家: 内容] 形式的方括号行
```

也就是说，纯日志方案**看不到任何由控制台/面板发起的操作**，而那恰恰是最需要审计的部分。
所以本项目改用 KubeJS 的命令事件。

## 数据链路

```
命令执行
   │
   ├─ ServerEvents.command 事件（KubeJS）
   │     取出：命令原文 / 执行者 / 执行者当时模式 / epoch 毫秒
   │     输出：一行 [MCAUDIT] {json}
   │
   ▼
服务端 logs/latest.log
   │
   ├─ 采集器按行正则匹配 \[MCAUDIT\]\s+(\{.*\})\s*$
   │     解析命令 → 结构化事件（类型/对象/物品/数量）
   │     物品 id → 中英文名
   │
   ▼
events-YYYY-MM-DD.jsonl （看板数据源）  +  events.csv （人读台账）
   │
   ▼
看板 HTTP 服务 → 时间轴 / 玩家板块
```

## KubeJS 6 的两个实测陷阱

这两个坑都很隐蔽，已在 `kubejs/admin_audit.js` 顶部注释里保留说明。

### 1. `java()` 全局已被移除，且错误会逃逸 `try/catch`

KubeJS 6 里 `java.lang.System.currentTimeMillis()` 会直接抛
`'java()' is no longer supported`。麻烦的是：**这个错误不受脚本内 `try/catch` 保护**，
整个事件处理器每次都炸，但日志里只留下一个 `Error in 'ServerEvents.command'`，
现象上很像「脚本根本没加载」。

正确做法是用 JS 原生的 `new Date().getTime()`。

### 2. `getScoreboardName()` 不可见 —— 会导致玩家被静默误标为 `console`

最阴的一条。用 `player.getScoreboardName()` 取玩家名会抛错，而这个错误**被兜底逻辑吃掉**，
最终 `actor` 落回默认值 `console`。结果是：功能看起来完全正常（有记录、有时间、有物品），
但**所有玩家行为都被记成了控制台行为** —— 数据全错，却不报错。

可用的取值方式（按优先级）：

```js
player.getGameProfile().getName()   // 首选
player.getName().getString()        // 兜底 1
player.username                     // 兜底 2
```

这个问题是靠 **Forge `FakePlayer` 伪造一个「玩家命令源」** 测出来的：
在没有真人在线的服务器上，构造一个 FakePlayer 作为命令来源，观察采集端是否把它识别成玩家名。

## 时间戳的三级优先级

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 事件自带的 `ms`（epoch 毫秒） | 绝对时间，与服务器时区无关，直接换算北京时间 |
| 2 | 日志行首时间戳 | 仅在 `ms` 缺失/非法时使用，按 `log_tz` 解释 |
| 3 | 采集时刻 | 兜底 |

支持两种日志行格式：

- Forge：`[17Sep2026 00:59:29.895]`
- 原版 / Paper：`[00:59:29]`

## 日志跟随

采集器**不使用** `tail -F`，而是自己管理读取位置：

- 记录 `(inode, offset)` 到 `state.json`，重启后从上次位置继续
- **inode 变化** → 判定日志轮转，从新文件头开始读
- **文件大小小于 offset** → 判定日志被截断，重置到 0
- 首次运行默认从**文件末尾**开始（`first_run_from_end`），避免把海量历史日志一次性收进来

## 去重

同一行日志可能被重复读到（例如轮询边界）。采集器对 `raw` 行取 hash，
维护最近 500 条的记忆窗口，重复行直接丢弃。

## 玩家聚合

看板的「玩家板块」基于**全量事件**聚合（不受当前筛选影响，便于对照）：

- `as_actor` / `as_target`：作为执行者 / 作为对象的次数
- `count`：参与的事件数
- `give` / `gamemode`：取物 / 模式切换次数
- `last`：最近一次出现时间

两个细节：

- **选择器不算玩家**：`@a`、`@p`、`@s`、坐标、`N entities` 这类字符串会被排除在玩家列表外。
  其中 `@s` 在采集阶段就会被还原成执行者本人。
- **自己给自己只算一次**：`/give Alice ... @s` 这类事件中执行者与对象是同一人，
  `as_actor` 与 `as_target` 各记一次（语义不同），但事件数 `count` 只记一次，否则会翻倍。

## 物品名映射表

由 `tools/build_item_names.py` 生成，扫描顺序：

1. `<服务端>/mods/*.jar` —— 模组物品的中英文名（大多数模组都自带 `zh_cn.json`）
2. `<服务端>/*.jar` —— 整合包启动 jar
3. `<服务端>/libraries/**/*.jar` —— **原版物品英文名的来源**。
   Forge 安装器会把原版资源放在
   `libraries/net/minecraft/server/<版本>/server-<版本>-extra.jar`，漏扫这里会导致
   `minecraft:xxx` 全部没有名字
4. `<服务端>/kubejs/assets` —— KubeJS 自定义资源
5. `--minecraft-dir <客户端 .minecraft>` —— **原版物品中文名的唯一来源**（可选）

语言文件里的键形如 `item.<命名空间>.<路径>` / `block.<命名空间>.<路径>`，
转换为 `命名空间:路径`。转换后会过滤掉「子键」—— 像
`xxx.smithing_template.diamond.ingredients` 这种并非真实物品 id 的键。

## 数据格式

`events-YYYY-MM-DD.jsonl` 每行一个 JSON：

```json
{
  "type": "give",
  "target": "Steve",
  "item_raw": "minecraft:diamond",
  "count": 64,
  "actor": "Alice",
  "gm": "creative",
  "cmd": "give Steve minecraft:diamond 64",
  "ts": "2026-09-17 12:04:11",
  "date_bj": "2026-09-17",
  "item_id": "minecraft:diamond",
  "item_zh": "钻石",
  "item_en": "Diamond",
  "detail_cn": "Alice 给予 Steve 64 个 钻石（当时模式：creative）",
  "raw": "[17Sep2026 04:04:11.123] ... [MCAUDIT] {...}"
}
```

`raw` 保留了完整原始日志行，便于事后核对。
