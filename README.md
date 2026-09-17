# minecraft-op-audit · Minecraft 管理员行为审计

**一条命令装好的旁路审计系统**：记录每位玩家（尤其是 OP）执行了哪些管理命令 —— 给谁发了什么物品、把谁切成了创造模式、开了谁的权限 —— 并以 **时间轴** 和 **玩家板块** 两种视图呈现。

> 完全旁路：不改动服务端本体、不修改存档、不引入新的权限体系。
> 全部写入都发生在服务端目录之外，唯一例外是一个可随时删除的 KubeJS 脚本。

```
时间轴                                        玩家板块
──────────────────────────────               ──────────────────────────────
▸ 2026-09-17（12 条）                        ┌ 玩家列表 ─┬ Alice 的完整行为 ─┐
  12:04:11 [管理员取物] Alice → Steve        │ Alice  12 │ 作为执行者 9 次   │
           钻石 ×64  (Diamond)               │ Bob     7 │ 作为对象 3 次     │
           minecraft:diamond                 │ Carol   5 │ 取物 7 · 切模式 2 │
  12:03:52 [切换游戏模式] Alice               │ console 3 │ 高频：钻石 ×128   │
           把自己的游戏模式切为 creative       └───────────┴───────────────────┘
```

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [看板使用](#看板使用)
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
│   └─ 监听 ServerEvents.command（每条命令执行时触发）             │
│        输出一行：  [MCAUDIT] {"ms":..,"actor":..,"cmd":..,"gm":..}
│                            │                                   │
│                            ▼                                   │
│                    logs/latest.log                             │
└────────────────────────────┬───────────────────────────────────┘
                             │ 只读跟随（感知 inode 轮转 / 日志截断）
                             ▼
      ┌────────────────────────────────────────────────┐
      │  audit_watcher.py    采集器（systemd 常驻）      │
      │   ├─ 解析命令 → 事件类型 give / gamemode / ...   │
      │   ├─ 物品 id → 中英文名（item_names.json）       │
      │   └─ 落盘 events.csv + events-YYYY-MM-DD.jsonl  │
      └───────────────────────┬────────────────────────┘
                              ▼
      ┌────────────────────────────────────────────────┐
      │  audit_dashboard.py   看板（systemd 常驻）      │
      │   时间轴视图  ·  玩家板块视图                    │
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
| **时间轴** | 按日期分组的事件流。每条显示类型徽章、执行者 → 对象、物品（中文名 + ID + 英文名 + 数量），可展开原始日志行 |
| **玩家板块** | 左侧玩家列表（按记录数排序），点选任一玩家即查看**他的全部行为**，顶部带概览：作为执行者 N 次 / 作为对象 N 次 / 最近时间 / 取物 N / 模式切换 N / 高频物品 |

筛选条件：**天数**（今天 / 3 天 / 7 天 / 30 天 / 全部）、**指定玩家**、**事件类型**标签、**关键词搜索**。

下载：

| 地址 | 内容 |
|---|---|
| `/download/events.csv` | 全量台账，Excel 可直接打开（UTF-8 BOM，中文不乱码） |
| `/download/events-YYYY-MM-DD.jsonl` | 按天明细，看板的数据源 |

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

`time` / `weather` / `say` 这类噪声**默认不记录**。若想把所有命令都记进台账，把 `config.json` 的 `log_other_commands` 改成 `true`。

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
# 采集器解析逻辑（32 项）
python3 tests/test_watcher.py

# 看板鉴权 + 数据聚合（39 项）
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

**minecraft-op-audit** — a sidecar audit system for Minecraft servers that records every administrative command (who gave what to whom, who switched gamemodes, who granted OP) and serves it through a web dashboard with a timeline view and a per-player view.

- **Zero changes to the server itself.** A single KubeJS script emits one `[MCAUDIT]` log line per command; a Python collector tails `logs/latest.log` and writes CSV/JSONL ledgers; a dependency-free Python dashboard renders them.
- **Requires** Minecraft Java + KubeJS 6 on the server, and Python 3.9+ for the collector.
- **Install**: `git clone … && cd minecraft-op-audit && sudo bash deploy/install.sh`

See [docs/quickstart.md](docs/quickstart.md) for details.

---

## License

[MIT](LICENSE)
