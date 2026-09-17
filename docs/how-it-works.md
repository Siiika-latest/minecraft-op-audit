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
   │     输出：一行 [MCAUDIT] {"ev":"cmd",...}
   │
玩家死亡
   │
   ├─ EntityEvents.death 事件（KubeJS）
   │     先判断"是不是玩家"，不是玩家直接返回（该事件对所有生物触发）
   │     取出：死者 / DamageSource.type().msgId() / DamageSource.getActual() / 死讯原文
   │     输出：一行 [MCAUDIT] {"ev":"death",...}
   │
   ▼
服务端 logs/latest.log
   │
   ├─ 采集器按行正则匹配 \[MCAUDIT\]\s+(\{.*\})\s*$
   │     按 ev 分流：cmd → 命令解析器；death → 死亡事件（死因中文化）
   │     物品 id → 中英文名
   │
   ▼
events-YYYY-MM-DD.jsonl （看板数据源）  +  events.csv （人读台账）
   │
   ▼
看板 HTTP 服务 → 排行榜 / 时间轴 / 玩家板块
```

## KubeJS 6 的三个实测陷阱

这三个坑都很隐蔽，已在 `kubejs/admin_audit.js` 顶部注释里保留说明。

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

### 3. 没有 `PlayerEvents.death`，只有 `EntityEvents.death`

KubeJS 6 里**不存在** `PlayerEvents.death`。玩家死亡和其他生物死亡走的是同一个事件
`EntityEvents.death`（对应 `LivingEntityDeathEventJS`），它提供的方法只有
`getEntity()` 与 `getSource()`；`getPlayer()` 来自父类 `EntityEventJS`，
实体不是玩家时返回 `null`——正好可以拿来做「是不是玩家」的判定。

另一个容易踩的点：**顶层变量/函数名**。脚本重载时，
`var server` 会与 KubeJS 的全局同名而报错，`const` 会触发 Rhino 的重声明检查。
所以 `admin_audit.js` 里两个处理器各自带一份玩家取名逻辑（复制一小段），
刻意不做成公共函数 —— 重载报错的排查成本比复制高得多。

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

## 玩家死亡为什么不用日志文本解析

服务端日志里确实会出现死讯（`server.sendSystemMessage` 会落盘），但拿它做数据源有三个问题：

1. **本地化**：死讯是翻译键渲染出来的文本，中英文服、被模组改过的语言文件都不一样，
   正则匹配等于在追一个会变的字符串。
2. **没有结构化字段**：`oMoSiKa was slain by Zombie` 里，"Zombie" 到底是被杀的生物名、
   还是别的什么，靠文本切分很容易误判。
3. **看不到死因 id**：`坠亡` 与 `被推下悬崖` 在文本上可能都长一样，但
   `type().msgId()` 分别给出 `fall` 和别的值。

所以改成监听 KubeJS 的 `EntityEvents.death`，直接取结构化字段：

| 字段 | 取值 | 说明 |
|---|---|---|
| 死者 | `event.getPlayer()` | 非玩家实体返回 `null`，**用它做第一道过滤** |
| 死因 | `DamageSource.type().msgId()` | 与语言无关，如 `fall` / `player` / `lava` |
| 击杀者 | `DamageSource.getActual()` | 箭、三叉戟等投掷物会回溯到发射者 |
| 死讯原文 | `CombatTracker.getDeathMessage()` | 仅供对照，取不到不影响统计 |

### 这三个取值方法是被实测"打脸"之后改的

最初照直觉写的是 `DamageSource.getMsgId()` / `.getEntity()` / `.getLocalizedDeathMessage()`，
在真机上全部不可用 —— 而且因为外面套了 `try/catch`，**错误被静默吞掉，死因和击杀者
会变成空字符串**，看板照样能跑、死亡次数照样统计，只是每条死亡都显示"死因：未知"。
这类"看起来正常、实际全错"的失败最难发现，所以在真机上逐个 dump 了一遍：

| 直觉写法 | 实际结果 | 改用 |
|---|---|---|
| `getSource().getMsgId()` | `TypeError: Cannot find function getMsgId in object DamageSource (lava)` | `getSource().type().msgId()`；再兜底从 `String(getSource())` 抠 `"DamageSource (lava)"` |
| `getSource().getEntity()` / `.getDirectEntity()` | 方法不存在 | `getSource().getActual()`，`getImmediate()` 兜底 |
| `getSource().getLocalizedDeathMessage()` | `InternalError: Can't find method ...DamageSource.m_6157_()`（SRG 映射缺失） | `entity.getCombatTracker().getDeathMessage()` |

实测环境：Minecraft 1.20.1 + Forge + KubeJS 6（Rhino）。结论同步写在
`kubejs/admin_audit.js` 顶部注释里，换版本先复测再改。

### 死因 id ≠ 注册名

`DamageType.msgId()` 返回驼峰短名，和 `/damage` 里写的注册名不是一回事。
两个最容易搞错的：`player_attack` 的 msgId 是 **`player`**、`mob_attack` 的是 **`mob`**；
另外 `in_wall` / `out_of_world` / `lightning_bolt` / `hot_floor` / `generic_kill`
分别是 `inWall` / `outOfWorld` / `lightningBolt` / `hotFloor` / `genericKill`。

采集器查表前会先跑 `cause_key()`（驼峰转蛇形 + 小写），所以两种写法都能命中
`CAUSE_CN` 里的同一行，不必为每个 id 写两遍。

**性能注意**：`EntityEvents.death` 对**所有生物**触发。刷怪塔每秒可能死几百只怪，
所以处理器第一行就是 `if (!event.getPlayer()) return;`，取值操作全在过滤之后 ——
非玩家死亡的开销只有一次 `instanceof` 判断，不产生日志。

## 排行榜与百分位

看板的「排行榜」视图按**天**统计两张榜：**管理员行为榜**（当天作为执行者产生的管理事件数）
与**死亡次数榜**（当天死亡次数）。

```
百分位 = 100 × 当天「次数 ≤ 你的玩家数」 ÷ 当天上榜玩家数      （四舍五入，最小 1）
```

即「你胜过或战平了多少比例的人」。这样定义有两个好处：

- **当天次数最高的玩家必定是 100**（满足"第一名拿满色"的直觉）；并列第一同为 100。
- 不需要额外配置分母，人多人少都自洽。

规则细节：

- **次数为 0 的玩家不上榜** —— 否则一堆 0 分并列，榜单没有信息量。
- **`console` 不进榜** —— 控制台不是玩家，不该跟玩家比高低（但它在玩家板块里保留，
  方便看清哪些操作来自面板 / 命令方块）。
- **榜单不受页面筛选影响** —— 天数 / 玩家 / 类型 / 关键词只作用于时间轴与事件列表；
  排行榜只按日期切换，否则"横着比"这件事就不成立了。
- 色阶：`100` 金 · `99` 粉 · `91–98` 橙 · `76–90` 紫 · `51–75` 蓝 · `26–50` 绿 · `1–25` 灰。

## 玩家聚合

看板的「玩家板块」基于**全量事件**聚合（不受当前筛选影响，便于对照）：

- `as_actor` / `as_target`：作为执行者 / 作为对象的次数
- `deaths`：死亡次数（**单独计数，不并入 `as_actor`** —— 死亡不是他做的行为）
- `count`：参与的事件数（含死亡）
- `give` / `gamemode`：取物 / 模式切换次数
- `last`：最近一次出现时间

三个细节：

- **选择器不算玩家**：`@a`、`@p`、`@s`、坐标、`N entities` 这类字符串会被排除在玩家列表外。
  其中 `@s` 在采集阶段就会被还原成执行者本人。
- **自己给自己只算一次**：`/give Alice ... @s` 这类事件中执行者与对象是同一人，
  `as_actor` 与 `as_target` 各记一次（语义不同），但事件数 `count` 只记一次，否则会翻倍。
- **击杀者不写进 `target`**：死亡事件的 `target` 刻意留空。否则 `Zombie`、`Skeleton`
  会被当成玩家名混进玩家榜与排行榜。

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

死亡事件（`type` 为 `death`）：

```json
{
  "type": "death",
  "target": "",
  "count": null,
  "cause": "mob",
  "cause_cn": "被生物击杀",
  "killer": "Zombie",
  "msg": "oMoSiKa was slain by Zombie",
  "actor": "oMoSiKa",
  "gm": "survival",
  "cmd": "",
  "ts": "2026-09-17 22:31:07",
  "date_bj": "2026-09-17",
  "item_id": "",
  "item_zh": "",
  "item_en": "",
  "detail_cn": "oMoSiKa 被 Zombie 击杀（死因：被生物击杀）（当时模式：survival）",
  "raw": "[17Sep2026 14:31:07.456] ... [MCAUDIT] {\"ev\":\"death\",...}"
}
```

> `cause` 是原样的死因 id（未中文化），`killer` 是击杀者名字，
> `msg` 是游戏内死讯原文。中文化只发生在 `detail_cn` —— 想自己加工就用前三个字段。
