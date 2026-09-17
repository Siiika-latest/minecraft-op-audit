# minecraft-op-audit · Minecraft 管理员行为审计

**一条命令装好的旁路审计系统**：记录每位玩家（尤其是 OP）执行了哪些管理命令 —— 给谁发了什么物品、把谁切成了创造模式、开了谁的权限 —— 并统计每位玩家的**死亡次数**，以 **排行榜**、**时间轴**、**玩家板块** 三种视图呈现。

排行榜按天出榜，百分位配色参考 **FF14 Logs**：最高 <span>100</span> 金色、99 粉色、91–98 橙色……

> 完全旁路：不改动服务端本体、不修改存档、不引入新的权限体系。
> 全部写入都发生在服务端目录之外，唯一例外是一个可随时删除的 KubeJS 脚本。

```
排行榜（2026-09-19）                          时间轴
──────────────────────────────               ──────────────────────────────
管理员行为榜              死亡次数榜          ▸ 2026-09-17（12 条）
★ Alice    6 次  [ 100 ]  Bob    5 次 [ 100 ]  12:04:11 [管理员取物] Alice → Steve
  Bob      3 次  [  67 ]  Alice  2 次 [  67 ]           钻石 ×64  (Diamond)
  Carol    1 次  [  33 ]  Carol  1 次 [  33 ]            minecraft:diamond
                          （越靠左越高，色阶见下） 12:03:20 [玩家死亡] oMoSiKa
                                                    被 Zombie 击杀（死因：被生物击杀）
```

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [看板使用](#看板使用)
- [排行榜与色阶](#排行榜与色阶)
- [记录哪些行为](#记录哪些行为)
- [台账文件](#台账文件)
- [配置项](#配置项)
- [访问控制](#访问控制)
- [已知限制](#已知限制)
- [兼容性](#兼容性)
- [开发与测试](#开发与测试)
- [目录结构](#目录结构)
- [English](#english)

---

## 它解决什么问题

Minecraft 里 OP 的权力很大，但**原版几乎没有留下可查的记录**：

| 想查的事 | 原版能力 |
|---|---|
| 谁给谁发了物品？ | 控制台不落盘；只有在线 OP 能在聊天栏看到一眼 |
| 谁把自己切成了创造模式？ | 同上 |
| 谁被授予了 OP？ | `ops.json` 只有当前结果，没有历史 |
| 事后追溯某人一整晚的权限操作？ | 无 |

即使打开 `logAdminCommands=true`，**控制台执行的命令也不会把管理广播写入日志**（它只向在线 OP 的聊天栏广播，而聊天不落盘）。命令方块、面板终端执行的管理命令同样查不到。

本项目补上了这块：把每一次命令执行落成结构化台账，并提供一个能按玩家查阅的看板。

---

## 工作原理

```
┌─────────────── Minecraft 服务端（本体零改动）──────────────────┐
│                                                                │
│  kubejs/server_scripts/admin_audit.js                          │
│   ├─ ServerEvents.command  每条命令执行时触发                    │
│   │      → [MCAUDIT] {"ev":"cmd","ms":..,"actor":..,"cmd":..}   │
│   └─ EntityEvents.death    每次生物死亡时触发，仅玩家会上报       │
│          → [MCAUDIT] {"ev":"death","actor":..,"cause":"fall",   │
│                       "killer":"Zombie",..}                     │
│                            │                                   │
│                            ▼                                   │
│                    logs/latest.log                             │
└────────────────────────────┬───────────────────────────────────┘
                             │ 只读跟随（感知 inode 轮转 / 日志截断）
                             ▼
      ┌────────────────────────────────────────────────┐
      │  audit_watcher.py    采集器（systemd 常驻）      │
      │   ├─ 命令 → 事件类型 give / gamemode / ...       │
      │   ├─ 死亡 → 死因中文化 + 击杀者                  │
      │   ├─ 物品 id → 中英文名（item_names.json）       │
      │   └─ 落盘 events.csv + events-YYYY-MM-DD.jsonl  │
      └───────────────────────┬────────────────────────┘
                              ▼
      ┌────────────────────────────────────────────────┐
      │  audit_dashboard.py   看板（systemd 常驻）      │
      │   排行榜视图  ·  时间轴视图  ·  玩家板块视图      │
      │   百分位色阶参考 FF14 Logs                       │
      │   纯 Python 标准库，零第三方依赖                 │
      └────────────────────────────────────────────────┘
```

三个部件都是**只读或只写自己的目录**，与被审计的服务端解耦：服务端关了就自然停止记录，看板与台账不受影响。

---

## 快速开始

### 前置条件

| 项 | 要求 |
|---|---|
| 服务端 | Minecraft Java 版，**装有 KubeJS**（6.x 实测；With KubeJS 6 tested） |
| 系统 | Linux（systemd 更省事，但不是必须） |
| 运行环境 | Python **3.9+**（用到标准库 `zoneinfo`），无第三方包依赖 |

### 安装

```bash
git clone https://github.com/Siiika-latest/minecraft-op-audit.git
cd minecraft-op-audit
sudo bash deploy/install.sh
```

脚本会：

1. 自动探测服务端目录与 `logs/latest.log`（找不到会提示你指定）
2. 安装采集器与看板、生成配置、随机生成访问令牌
3. 扫描服务端 jar 生成**物品名称映射表**（id → 中英文名）
4. 把采集端脚本放进 `<服务端>/kubejs/server_scripts/`
5. 注册两个 systemd 服务并启动、放行看板端口
6. 打印访问地址并自检

**若服务端当时正在运行**，还需在控制台执行一次让采集端生效：

```
kubejs reload server_scripts
```

### 非交互 / 自定义

```bash
sudo bash deploy/install.sh \
    --server-dir /opt/minecraft/server \
    --port 25567 \
    --title "我的服务器审计" \
    -y
```

完整参数见 `bash deploy/install.sh --help`。

### 验证

```bash
curl http://127.0.0.1:25567/api/health
# {"ok": true, "files": 0}
```

然后在游戏里执行一条 `/give` 或 `/gamemode`，刷新看板即可看到记录。

### 卸载

```bash
sudo bash deploy/uninstall.sh            # 保留台账
sudo bash deploy/uninstall.sh --purge    # 连台账一起删除
```

---

## 看板使用

| 视图 | 说明 |
|---|---|
| **排行榜** | 按天出两张榜：**管理员行为榜** 与 **死亡次数榜**。每行显示名次、玩家、次数与百分位徽章（配色参考 FF14 Logs），可切换统计日期 |
| **时间轴** | 按日期分组的事件流。每条显示类型徽章、执行者 → 对象、物品（中文名 + ID + 英文名 + 数量），可展开原始日志行；死亡事件显示击杀者与游戏内死讯原文 |
| **玩家板块** | 左侧玩家列表（按记录数排序），点选任一玩家即查看**他的全部行为**，顶部带概览：作为执行者 N 次 / 作为对象 N 次 / 死亡 N 次 / 最近时间 / 取物 N / 模式切换 N / 该玩家当日榜单百分位 |

筛选条件（排行榜视图下自动隐藏，因为榜单只按日期统计）：**天数**（今天 / 3 天 / 7 天 / 30 天 / 全部）、**指定玩家**、**事件类型**标签、**关键词搜索**。

下载：

| 地址 | 内容 |
|---|---|
| `/download/events.csv` | 全量台账，Excel 可直接打开（UTF-8 BOM，中文不乱码） |
| `/download/events-YYYY-MM-DD.jsonl` | 按天明细，看板的数据源 |

---

## 排行榜与色阶

榜单按**天**统计，每天两张：

| 榜单 | 统计口径 |
|---|---|
| **管理员行为榜** | 当天该玩家作为**执行者**产生的管理事件数（取物 / 设物 / 切模式 / 附魔 / 药水 / 经验 / 清背包 / 权限变更 / 刷实体 / 掉落物） |
| **死亡次数榜** | 当天该玩家**死亡**的次数 |

### 百分位怎么算

```
百分位 = 100 × 当天「次数 ≤ 你的玩家数」 ÷ 当天上榜玩家数      （四舍五入，最小 1）
```

也就是「**你胜过或战平了多少比例的人**」。因此：

- **当天次数最高的玩家必定是 100**（并列第一则同为 100）
- 并列者拿到相同的百分位与颜色
- **次数为 0 的玩家不上榜**（否则一堆 0 分并列，榜单没有信息量）
- `console`（控制台）不是玩家，不进榜

举例，当天 3 人上榜，次数分别为 `5 / 3 / 1`：

| 玩家 | 次数 | 胜过或战平 | 百分位 | 色阶 |
|---|---|---|---|---|
| Alice | 5 | 3 / 3 | `100` | 金色 |
| Bob | 3 | 2 / 3 | `67` | 蓝色 |
| Carol | 1 | 1 / 3 | `33` | 绿色 |

### 色阶

| 百分位 | 颜色 | 含义 |
|---|---|---|
| `100` | 金色 | 当日第一 |
| `99` | 粉色 | 仅次于第一 |
| `91 – 98` | 橙色 | 顶尖 |
| `76 – 90` | 紫色 | 优秀 |
| `51 – 75` | 蓝色 | 中上 |
| `26 – 50` | 绿色 | 中下 |
| `1 – 25` | 灰色 | 垫底 |

配色沿用 FF14 Logs 的观感（深色主题下直接用原味配色；浅色主题下换成同色系深色版，保证白底可读）。

> 这个百分位是**队内相对排名**，不是绝对评价 —— 人少的时候排到 100 只是因为当天只有你在动。跨天比没意义，同一天横着比才有意义。

---

## 记录哪些行为

| 命令 | 事件类型 | 页面显示 |
|---|---|---|
| `/give <玩家> <物品> [数量]` | `give` | 管理员取物 |
| `/item replace entity\|block ... with <物品>` | `item_set` | 管理员设物 |
| `/loot give <玩家> <来源>` | `loot` | 管理员掉落物 |
| `/gamemode <模式>` | `gamemode` | 切换游戏模式（自己） |
| `/gamemode <模式> <玩家>` | `gamemode_other` | 切换他人模式 |
| `/enchant <玩家> <附魔>` | `enchant` | 管理员附魔 |
| `/effect give <玩家> <效果>` | `effect` | 管理员给药水 |
| `/xp add <玩家> <点数>` | `xp` | 管理员给经验 |
| `/clear [玩家]` | `clear` | 清空玩家物品 |
| `/op`、`/deop <玩家>` | `op` | 权限变更 |
| `/summon <实体>` | `summon` | 管理员刷实体 |
| （玩家死亡，非命令） | `death` | 玩家死亡（记录死因与击杀者） |

`time` / `weather` / `say` 这类噪声**默认不记录**。若想把所有命令都记进台账，把 `config.json` 的 `log_other_commands` 改成 `true`。

### 死亡统计是怎么来的

死亡**不是**从日志文本里猜的 —— 服务端日志里的死讯是本地化文本（可能是中文、可能是英文、可能被模组改掉），拿它做匹配非常脆。

采集端改为监听 KubeJS 的 `EntityEvents.death`，只取与语言无关的结构化字段：

| 字段 | 来源 | 例子 |
|---|---|---|
| 死者 | 事件实体 | `oMoSiKa` |
| 死因 | `DamageSource.type().msgId()` | `fall` → 坠落、`player` → 被玩家击杀、`mob` → 被生物击杀 |
| 击杀者 | `DamageSource.getActual()` | `Zombie`、`Alex`（射箭的箭会正确回溯到射手） |
| 游戏内死讯 | `CombatTracker.getDeathMessage()` | `oMoSiKa was slain by Zombie`（原文保留，供对照） |

`EntityEvents.death` 对**所有生物**都触发（刷怪塔每秒可能死几百只怪），所以处理器第一件事就是判断"是不是玩家"，不是玩家直接返回，不产生任何日志、不写台账。

#### ⚠ 三个实测踩过的坑（别照 API 文档想当然）

上面两个取值方法**不是** `DamageSource` 上最直觉的那两个 —— 直觉写法在这套环境里会静默变成空字符串：

| 直觉写法 | 实际结果 | 改用 |
|---|---|---|
| `getSource().getMsgId()` | `TypeError: Cannot find function getMsgId ...`（方法不存在） | `getSource().type().msgId()`，兜底从 `String(getSource())` 里抠 `"DamageSource (lava)"` |
| `getSource().getEntity()` / `.getDirectEntity()` | 方法不存在 | `getSource().getActual()`（箭 → 射箭的人），`getImmediate()` 兜底 |
| `getSource().getLocalizedDeathMessage()` | `InternalError: Can't find method ...DamageSource.m_6157_()`（SRG 映射缺失） | `entity.getCombatTracker().getDeathMessage()` |

实测环境：Minecraft 1.20.1 + Forge + KubeJS 6（Rhino 引擎）。这段结论写在 `kubejs/admin_audit.js` 顶部的兼容性注释里，换版本请先复测。

#### 死因 id 不是注册名

另一个容易踩的坑：`DamageType.msgId()` 返回的是**驼峰**短名，和伤害类型的注册名并不一致。下表是 2026-09-17 在真实服务端用 `/damage` 逐个 dump 出来的，不是猜的：

| 注册名（`/damage` 里写的） | 实际 `msgId()` | 中文 |
|---|---|---|
| `player_attack` | `player` | 被玩家击杀 |
| `mob_attack` | `mob` | 被生物击杀 |
| `in_wall` | `inWall` | 卡在方块里 |
| `out_of_world` | `outOfWorld` | 掉出世界 |
| `lightning_bolt` | `lightningBolt` | 被雷劈 |
| `hot_floor` | `hotFloor` | 踩到岩浆块 |
| `generic_kill` | `genericKill` | 被清除 |
| `fall` / `lava` / `drown` / `cactus` / `wither` / `freeze` / `arrow` / `trident` / `explosion` | 同名 | 坠落 / 岩浆 / 溺水 / 仙人掌 / 凋零 / 冻死 / 被箭射死 / 被三叉戟戳死 / 爆炸 |

死因 id 的中文映射表在 `collector/audit_watcher.py` 的 `CAUSE_CN`。查表前会先用 `cause_key()` 把驼峰转成蛇形，所以 `inWall` 和 `in_wall` 都能命中同一张表。未收录的模组伤害类型会原样显示（例如 `some_mod:weird_damage`），不会丢失信息；想补充就在那个字典里加一行。

其他行为：

- **`@s` 自动还原**：`/give @s diamond` 会记成「执行者给自己」，而不是留下一个看不懂的 `@s`
- **物品标签**：`/give @a #minecraft:planks` 记为 `#minecraft:planks`（（物品标签））
- **NBT 剥离**：`diamond_sword{Enchantments:[...]}` 会剥离 NBT 后匹配名字，页面保留完整原文
- **执行者当时模式**：每条记录附带执行者当时的 `creative` / `survival` 状态

---

## 台账文件

默认都在安装目录（`/opt/minecraft-op-audit`）下：

| 文件 | 说明 |
|---|---|
| `log/events.csv` | 全量台账，13 列：时间(北京)、日期、事件类型、执行者、执行者模式、对象玩家、物品ID、物品中文名、物品英文名、数量、详情、命令原文、原始日志 |
| `log/events-YYYY-MM-DD.jsonl` | 按天明细（JSON Lines），看板数据源 |
| `log/watcher.log` | 采集器运行日志 |
| `state.json` | 日志读取位置（inode + offset），重启不丢进度 |

直接拷走 `log/` 目录即可离线归档。

---

## 配置项

`config.json`（安装脚本生成，可手改后 `systemctl restart minecraft-op-audit-collector minecraft-op-audit-dashboard`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `server_log` | 安装时探测 | 服务端 `logs/latest.log` 路径 |
| `poll_seconds` | `2` | 日志轮询间隔 |
| `first_run_from_end` | `true` | 首次启动从文件末尾开始（避免把历史日志全部收进来） |
| `log_tz` | 安装时探测 | 日志行时间戳的时区（仅当事件缺毫秒时间戳时用于兜底） |
| `enabled_types` | 见上表 | 记录哪些事件类型 |
| `log_other_commands` | `false` | 是否把所有其它命令也记进台账 |
| `dash_host` / `dash_port` | `0.0.0.0` / `25567` | 看板监听地址与端口 |
| `site_title` | `Minecraft 管理员行为审计` | 看板标题 |
| `require_token` | `true` | 是否启用访问令牌 |
| `lan_no_token` | `true` | 局域网 / 本机访问免令牌（外网仍需令牌） |
| `dash_token` | 随机生成 | 访问令牌 |

---

## 访问控制

默认策略是**局域网免令牌、外网必须带令牌**：

- 本机与内网访问 `http://<IP>:25567/` 直接可看
- 外网访问需要 `http://<IP>:25567/?k=<dash_token>`，首次带令牌访问后会种一个长效 Cookie，之后不必再带

把它**暴露到公网前请务必想清楚**：审计数据本身就是敏感信息（谁在什么时候拿了什么）。可选做法：

```jsonc
// 1) 完全关闭令牌（仅限内网或已由反向代理鉴权）
{ "require_token": false }

// 2) 内网也要求令牌（最严）
{ "lan_no_token": false }
```

建议通过 Nginx / Caddy 反向代理加 HTTPS 与基础认证，而不是直接把端口暴露出去。

---

## 已知限制

这些是**机制上的**限制，不是 bug，值得先知道：

1. **创造模式物品栏里直接拿物品不会产生任何命令**，因此无法记录。这类行为只能通过「切换游戏模式」记录间接佐证 —— 这也是 `gamemode` 事件值得重点关注的原因。
2. **控制台 / 命令方块 / 管理面板终端**执行的命令，执行者记为 `console`（无法区分是谁在面板里点的）。
3. **服务端不开服时不产生新记录** —— 旁路设计的必然结果。
4. 原版物品的**中文名**在服务端资源里不存在（服务端 jar 只带英文语言文件）。页面会显示英文名 + 物品 ID；想补中文名见 [FAQ](docs/faq.md#原版物品为什么没有中文名)。

---

## 兼容性

| 项 | 状态 |
|---|---|
| Minecraft 1.20.1 + Forge 47.x + **KubeJS 6** | ✅ 实测通过 |
| 其他 Forge 版本 / 模组包 | 理论可用，KubeJS 6 环境下即可 |
| **KubeJS 7**（1.21+ / NeoForge） | ⚠️ 未实测。KubeJS 7 的 API 有变动，若报错请对照 `kubejs/admin_audit.js` 顶部的兼容性注释调整 |
| Paper / Spigot | ❌ KubeJS 不支持服务端插件；此类服务端可参考 `collector/audit_watcher.py` 里已备好的 `issued server command` 兜底正则自行对接 |
| 无 KubeJS 的纯原版服务端 | ❌ 无法获取命令事件 |

---

## 开发与测试

```bash
# 采集器解析逻辑（63 项：命令 / 死亡事件 / 死因 id 归一化 / 时间换算 / 选择器处理）
python3 tests/test_watcher.py

# 看板鉴权 + 数据聚合 + 排行榜百分位（83 项）
python3 tests/test_dashboard.py
```

测试全部自包含，不依赖服务端与外部文件。

单独使用采集器（不带 systemd）：

```bash
MCAUDIT_HOME=/opt/minecraft-op-audit python3 collector/audit_watcher.py
MCAUDIT_HOME=/opt/minecraft-op-audit python3 dashboard/audit_dashboard.py
```

单独重建物品名映射表：

```bash
python3 tools/build_item_names.py --server-dir /opt/minecraft/server --out item_names.json
# 想补原版物品中文名（用你自己电脑上的客户端资源）
python3 tools/build_item_names.py --server-dir /opt/minecraft/server \
        --minecraft-dir ~/.minecraft --out item_names.json
```

---

## 目录结构

```
minecraft-op-audit/
├── kubejs/
│   └── admin_audit.js            # 采集端（放到 <服务端>/kubejs/server_scripts/）
├── collector/
│   └── audit_watcher.py          # 采集器：日志 → 台账
├── dashboard/
│   ├── audit_dashboard.py        # 看板后端（标准库 HTTP 服务）
│   └── dashboard.html            # 看板前端（时间轴 + 玩家板块）
├── tools/
│   └── build_item_names.py       # 物品名称映射表构建
├── deploy/
│   ├── install.sh                # 一键部署
│   └── uninstall.sh              # 卸载
├── tests/
│   ├── test_watcher.py
│   └── test_dashboard.py
├── docs/
│   ├── quickstart.md
│   ├── how-it-works.md
│   └── faq.md
└── examples/
    └── config.example.json
```

---

## English

**minecraft-op-audit** — a sidecar audit system for Minecraft servers that records every administrative command (who gave what to whom, who switched gamemodes, who granted OP) plus **every player death**, and serves it through a web dashboard with a **daily leaderboard**, a timeline view and a per-player view.

- **Zero changes to the server itself.** A single KubeJS script emits one `[MCAUDIT]` log line per command and per player death; a Python collector tails `logs/latest.log` and writes CSV/JSONL ledgers; a dependency-free Python dashboard renders them.
- **Leaderboard percentiles** follow the FF14 Logs colour scheme: `100` gold, `99` pink, `91–98` orange, `76–90` purple, `51–75` blue, `26–50` green, `1–25` grey. The daily top scorer always gets 100; ties share a percentile; players with zero are not ranked.
- **Requires** Minecraft Java + KubeJS 6 on the server, and Python 3.9+ for the collector.
- **Install**: `git clone … && cd minecraft-op-audit && sudo bash deploy/install.sh`

See [docs/quickstart.md](docs/quickstart.md) for details.

---

## License

[MIT](LICENSE)
