# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.1] - 2026-09-17

### 修复：死因与击杀者在真机上会静默变成空字符串

1.1.0 照直觉写的三个取值方法，在 Minecraft 1.20.1 + Forge + KubeJS 6（Rhino）
真机上**全部不可用**；因为外面套了 `try/catch`，失败被静默吞掉 ——
看板照常运行、死亡次数照常统计，只是每条死亡都变成"死因：未知"、击杀者空白。
这类「看起来正常、实际全错」的失败最难发现，已在真机上逐个 dump 后改正：

| 原写法 | 真机结果 | 改为 |
|---|---|---|
| `DamageSource.getMsgId()` | `TypeError: Cannot find function getMsgId` | `DamageSource.type().msgId()`，兜底从 `String(ds)` 抠 `DamageSource (lava)` |
| `DamageSource.getEntity()` / `.getDirectEntity()` | 方法不存在 | `DamageSource.getActual()`，`getImmediate()` 兜底 |
| `DamageSource.getLocalizedDeathMessage()` | `InternalError: Can't find method ...DamageSource.m_6157_()` | `entity.getCombatTracker().getDeathMessage()` |

- `kubejs/admin_audit.js` 顶部注释补充了这四条兼容性结论（`V`/`X` 标注），
  并在死因、击杀者两处写明"为什么不能用直觉写法"。

### 修复：死因 id 用错命名法，原版死因大面积漏译

`DamageType.msgId()` 返回的是**驼峰短名**，与 `/damage` 里写的注册名并不一致。
1.1.0 的 `CAUSE_CN` 全部按注册名（蛇形）写，导致以下死因查不到中文：

| 注册名 | 实际 `msgId()` |
|---|---|
| `player_attack` | `player` |
| `mob_attack` | `mob` |
| `in_wall` | `inWall` |
| `out_of_world` | `outOfWorld` |
| `lightning_bolt` | `lightningBolt` |
| `hot_floor` | `hotFloor` |
| `generic_kill` | `genericKill` |

- 新增 `cause_key()`：查表前把驼峰转成蛇形，两种写法命中同一张表。
- `CAUSE_CN` 以实测 id 为准重写，保留 `mob_attack` / `player_attack` 等同义别名。
- 事件新增 `cause_cn` 字段（`cause` 仍保留原始 msgId），看板死亡卡片直接显示中文死因标签。

### 测试

- `tests/test_watcher.py`：52 → **63 项**。新增的一组用例直接标注了
  「2026-09-17 真机实测 dump」来源，覆盖 `player` / `mob` 与五个驼峰 id，
  防止有人"顺手改回"注册名写法。

## [1.1.0] - 2026-09-17

### 新增：玩家死亡统计

- **采集端**：新增 `EntityEvents.death` 监听，只上报**玩家**死亡，非玩家实体直接返回
  （`EntityEvents.death` 对所有生物触发，刷怪塔每秒可能死几百只怪，因此判断放在最前面）。
- **结构化死因**：不解析日志里的本地化死讯文本（可能是中文 / 英文 / 被模组改写），
  改用与语言无关的死因 id（详见 1.1.1 对取值方法的更正），例如 `fall` / `player` / `mob`；
  击杀者取 `DamageSource.getActual()`（射箭的箭会正确回溯到射手）；
  另存 `CombatTracker.getDeathMessage()` 的游戏内死讯原文，供对照。
- **死因中文化**：`collector/audit_watcher.py` 新增 `CAUSE_CN` 字典（50 余条），
  未收录的模组伤害类型原样显示，不丢信息。
- 采集器解析事件新增 `ev` 字段用于区分事件种类（`cmd` / `death`）。
  **不带 `ev` 的旧日志行仍按命令解析**，向后兼容。
- 日志格式新增一行形态：
  `[MCAUDIT] {"ev":"death","actor":"..","cause":"fall","killer":"Zombie","msg":".."}`

### 新增：每日排行榜（百分位配色参考 FF14 Logs）

- 看板新增**排行榜**视图，按天出两张榜：**管理员行为榜**与**死亡次数榜**，
  可切换统计日期（默认最新一天）。
- 百分位 = `100 × 当天「次数 ≤ 你的玩家数」 ÷ 当天上榜玩家数`，
  即「你胜过或战平了多少比例的人」。**当天最高的玩家必定是 100**，并列者同色。
- 色阶：`100` 金色 · `99` 粉色 · `91–98` 橙色 · `76–90` 紫色 · `51–75` 蓝色 ·
  `26–50` 绿色 · `1–25` 灰色。浅色主题用同色系深色版，深色主题用 FF14 Logs 原味配色。
- 次数为 0 的玩家不上榜；`console` / `@选择器` 等非玩家来源不进榜。
- 玩家板块新增「死亡 N 次」与「该玩家当日榜单百分位」徽章；统计卡片新增「玩家死亡」。

### 变更

- 玩家聚合新增 `deaths` 计数，**死亡不并入「作为执行者」**（死亡不是他做的行为）。
- 死亡事件的击杀者**刻意不写入 `target`**，否则僵尸、骷髅会被当成玩家混进玩家榜。
- CSV 台账的「命令原文」列对死亡事件留空（死亡没有命令，原先会写成一个孤零零的 `/`）。

### 测试

- `tests/test_watcher.py`：32 → **52 项**（新增死亡事件解析、死因中文化、
  击杀者不得污染玩家榜、新旧日志格式兼容）。
- `tests/test_dashboard.py`：39 → **83 项**（新增排行榜百分位、并列同色、
  色阶边界、控制台不上榜、零次不上榜、日期切换、筛选不影响榜单）。

## [1.0.0] - 2026-09-17

首个公开版本。

### 功能

- **采集端**（`kubejs/admin_audit.js`）：监听 `ServerEvents.command`，把每条命令写成一行
  `[MCAUDIT] {"ms","actor","cmd","gm"}` 日志。纯只读，不改动服务端本体。
- **采集器**（`collector/audit_watcher.py`）：跟随服务端日志，解析 11 类管理命令，
  补齐物品中英文名，输出 `events.csv` + `events-YYYY-MM-DD.jsonl`。
  支持日志 inode 轮转与文件截断，跨重启保持读取位置。
- **看板**（`dashboard/`）：纯标准库 HTTP 服务，两个视图 ——
  时间轴（按日期分组）与玩家板块（按玩家查阅其全部行为）。
  支持天数 / 玩家 / 类型 / 关键词筛选，台账下载（CSV、JSONL）。
- **一键部署**（`deploy/install.sh`）：自动探测服务端目录与日志、生成配置与随机令牌、
  构建物品名映射表、注册 systemd 服务、放行端口、自检并打印访问地址。
  幂等，可重复执行。
- **卸载**（`deploy/uninstall.sh`）：停止服务、清理防火墙规则，默认保留台账并备份。

### 兼容性

- Minecraft 1.20.1 + Forge 47.4.16 + KubeJS 6（`kubejs-forge-2001.6.5-build.16`）实测通过。
- 需要 Python 3.9+（标准库 `zoneinfo`），无第三方依赖。

### 记录到的兼容性陷阱

开发过程中实测确认的两个 KubeJS 6 行为（已写入脚本注释，避免后续踩坑）：

- `java()` 全局函数已移除，调用会抛错，**且该错误会逃逸脚本内的 `try/catch`**。
  取时间必须用 JS 原生的 `new Date()`。
- `player.getScoreboardName()` 在脚本中不可见。用它取玩家名会抛错，
  抛错被兜底逻辑吞掉后，**玩家行为会被静默误标为 `console`** ——
  功能看起来正常，实际全错。改用 `getGameProfile().getName()` 并加三层兜底。

### 修复

- 玩家聚合中，同一条事件里执行者与对象为同一人时（如 `/give Alice ... @s`），
  记录数、取物数、模式切换数会被重复计两次。现在每个玩家在同一条事件中只累计一次。
