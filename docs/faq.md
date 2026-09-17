# 常见问题

## 看板打不开 / 提示 401 需要访问令牌

默认策略是**局域网免令牌、外网必须带令牌**。

- 局域网访问 `http://<IP>:25567/` 应该直接能开。开不了先看服务：
  `systemctl status minecraft-op-audit-dashboard`
- 外网访问需要 `http://<IP>:25567/?k=<令牌>`。令牌在 `config.json` 的 `dash_token`：

  ```bash
  python3 -c "import json;print(json.load(open('/opt/minecraft-op-audit/config.json'))['dash_token'])"
  ```

- 想彻底关掉令牌：把 `require_token` 改成 `false`，然后重启看板服务。

## 台账一直是空的

按这个顺序排查：

```bash
# ① 采集端脚本在不在
ls -l <服务端>/kubejs/server_scripts/admin_audit.js

# ② 服务端日志里有没有采集端产出的行
grep -c '\[MCAUDIT\]' <服务端>/logs/latest.log

# ③ 有没有 KubeJS 报错
grep -n 'Error in' <服务端>/logs/latest.log | tail
```

| 现象 | 原因 | 处理 |
|---|---|---|
| ② 有报错 | 采集端脚本报错 | 用仓库里的原版覆盖回去，然后 `kubejs reload server_scripts` |
| ② = 0，③ 无报错 | 脚本没被加载 | 在控制台执行 `kubejs reload server_scripts` |
| ② > 0，台账仍空 | 采集器没读对文件 | 核对 `config.json` 的 `server_log` |
| 服务端根本没开 | 旁路设计使然 | 开服后才有新记录 |

还有一种情况：**你只是没触发被记录的命令**。`/time`、`/weather`、`/say` 默认不记录，
试 `/give` 或 `/gamemode`。

## 原版物品为什么没有中文名

因为**服务端资源里没有原版中文语言文件**。Mojang 的服务端 jar 只带 `en_us`，
`zh_cn` 只存在于客户端的资源文件里。所以：

- 模组物品 → 中文名 + 英文名都有（模组自带 `zh_cn.json`）
- 原版物品 → 只有英文名（如 `Diamond (minecraft:diamond)`）

想要中文名，用你自己电脑上的客户端资源补一次：

```bash
python3 tools/build_item_names.py \
    --server-dir /opt/minecraft/server \
    --minecraft-dir "C:/Users/你/AppData/Roaming/.minecraft" \
    --out item_names.json
```

把生成的 `item_names.json` 传到服务器覆盖安装目录下的同名文件，再：

```bash
systemctl restart minecraft-op-audit-collector
```

若客户端是官方启动器，资源目录通常是 `%APPDATA%\.minecraft`；
HMCL / PCL 等启动器则用其「版本隔离」目录，指到含 `assets/indexes` 的那一层即可。

## 如何重建物品名称映射表

```bash
# 服务端上直接跑
python3 tools/build_item_names.py --server-dir /opt/minecraft/server --out /opt/minecraft-op-audit/item_names.json
systemctl restart minecraft-op-audit-collector
```

装了新模组后需要重建一次，否则新物品显示为「（未匹配到 ID）」。

## 能记录「在创造模式物品栏里直接拿物品」吗

**不能**，这是机制限制：从创造模式物品栏拖拽物品不产生任何命令，
原版也没有对应事件。这类行为只能通过两个间接线索判断：

1. `gamemode` 事件 —— 某人把自己切成创造模式，本身就值得关注
2. 如果服务端另有容器/物品流水相关模组的日志，可自行对接

## 会不会拖慢服务器

不会。

- **采集端**在最热路径上的开销是：拼一个字符串 + 一次日志输出，微秒级，且不涉及磁盘 I/O 之外的操作
- **死亡事件**对所有生物触发，但第一行就判断"是不是玩家"，非玩家死亡只多一次 `instanceof`，
  不产生日志。刷怪塔那种每秒几百只怪的场景也扛得住
- **采集器**每 2 秒读一次日志的增量部分，只做字符串正则与本地文件追加写
- 看板的数据在内存里缓存，文件没变就不重新解析

服务端不开服时采集器几乎不消耗资源。

## 死亡记录为什么只有玩家，没有生物

这是刻意的。`EntityEvents.death` 一视同仁地覆盖所有生物，但把刷怪塔的鸡和玩家的死亡
混在一个台账里，榜单就废了 —— 所以采集端在最前面就把非玩家实体过滤掉了。

如果你确实想要全生物死亡流水，把 `kubejs/admin_audit.js` 里死亡处理器开头的

```js
if (!pd) { return }        // 不是玩家 → 直接放过
```

去掉即可。但注意：**玩家榜会因此被僵尸、骷髅之类的名字污染**，还需要同步调整
`collector/audit_watcher.py` 的判断与看板的 `clean_name` 过滤。

## 排行榜的百分位是怎么算的 / 为什么我是灰色

```
百分位 = 100 × 当天「次数 ≤ 你的玩家数」 ÷ 当天上榜玩家数
```

也就是「**你胜过或战平了多少比例的人**」。当天次数最高的人一定是 100。

排查"为什么排名不好看"时按顺序看：

1. **当天是不是只有你一个人上线** —— 人少的时候第一就是 100，垫底也可能就是 0
2. **看的是不是同一天** —— 榜单切换日期在排行榜视图左上角，默认是**最近有数据的一天**，
   不一定是今天
3. **次数是不是 0** —— 次数为 0 的玩家不上榜（既不显示金色也不显示灰色，而是根本不出现）
4. **你用的是不是控制台 / 命令方块** —— `console` 不是一个玩家，永远不进榜

色阶：`100` 金 · `99` 粉 · `91–98` 橙 · `76–90` 紫 · `51–75` 蓝 · `26–50` 绿 · `1–25` 灰。

> 这个数字是**当天服务器内的相对排名**，不是绝对评价。人少的时候排到 100 只是因为当天只有你在动，
> 跨天比较没有意义。

## 死亡时间不准 / 死因显示成英文 id / 死因全是"未知"

- **时间**：优先用事件自带的 epoch 毫秒（KubeJS 侧 `new Date().getTime()`），
  它缺失时才退回日志行时间戳，再退回采集时刻。若时间整体偏移，
  检查 `config.json` 的 `log_tz` 是否与服务端日志时区一致。
- **死因变成英文 id**（如 `some_mod:weird_damage`）：说明这个伤害类型不在
  `collector/audit_watcher.py` 的 `CAUSE_CN` 字典里。字典收录了 50 多个原版死因，
  模组新增的伤害类型会原样显示（这样不会丢信息）。想中文化就照格式加一行，
  重启采集器即可。注意要照**实测的 msgId** 写，它和 `/damage` 里的注册名不一样
  （如 `player_attack` 的 msgId 是 `player`）。
- **死因全是"未知"、击杀者也全空**：这是 `kubejs/admin_audit.js` 里的死因取值
  在你这版 KubeJS 上失效了。脚本里那三处调用（`type().msgId()` / `getActual()` /
  `getCombatTracker().getDeathMessage()`）外面都套了 `try/catch`，
  **失效时不会报错，只会静默留空**。用下面的办法复测：

  ```bash
  # 造一只僵尸并当场击杀，观察是否出现带 cause 的 [MCAUDIT] 行
  # （把命令注入服务端控制台；记得先给目标位置 forceload，否则实体会被卸载）
  execute positioned 0 250 0 summon minecraft:zombie run damage @s 1000 minecraft:player_attack by @s
  grep -a '\[MCAUDIT\]' logs/latest.log | grep 'ev..:.death' | tail -3
  ```

  注意 `EntityEvents.death` 只对**玩家**上报，所以上面这条测法需要把脚本里的
  `if (!pd) { return }` 临时改成 `if (!pd) { pd = event.getEntity() }` 再测 ——
  改完记得改回来，然后 `kubejs reload server_scripts`。

## 玩家能看到或篡改审计数据吗

看不到也改不了。

- KubeJS 脚本没有任何玩家可见的输出（`console.info` 只写服务端日志）
- 玩家无法执行任何影响采集器的命令
- 台账文件在服务端目录之外（默认 `/opt/minecraft-op-audit/`），普通玩家自然没有文件权限

唯一的前提是：**OP 本身就有服务器文件权限**的话，理论上可以直接改台账。
本项目的定位是「记录普通玩家与 OP 的越权/误用行为」，不是防篡改审计链。
需要更强保证可把 `log/` 目录实时同步到另一台机器。

## 支持 Paper / Spigot 吗

不支持开箱即用 —— KubeJS 是 Forge/Fabric 的模组，不能装在插件服务端上。

`collector/audit_watcher.py` 里预留了一条兜底正则，可以解析 Paper 的

```
<玩家> issued server command: /命令
```

若你的插件服务端能把这些行写进日志（部分日志插件可以），采集器就能直接吃。
具体做法是给 `parse_line()` 加一个分支，把匹配到的行组装成同样的 JSON 结构。

## 支持 1.21 / KubeJS 7 / NeoForge 吗

采集端脚本面向 KubeJS 6 编写（1.20.1 / Forge 实测通过）。
KubeJS 7 的 API 有变动，采集端脚本顶部的兼容性注释列出了本项目的用法，
升级时对照调整 `ServerEvents.command` 里的事件对象取值方式即可，
采集器与看板两部分与游戏版本无关。

## 怎么改端口、标题

编辑 `config.json` 后重启：

```bash
vi /opt/minecraft-op-audit/config.json
systemctl restart minecraft-op-audit-collector minecraft-op-audit-dashboard
```

改端口还要记得放行新端口。`dash_ports` 可以填多个端口同时监听（如 `[80, 25567]`）。

## 只想记录某几类事件

编辑 `config.json` 的 `enabled_types`，只留下关心的：

```json
{ "enabled_types": ["give", "gamemode", "gamemode_other", "op"] }
```

可用的类型名：`give` `give_failed` `item_set` `loot` `gamemode` `gamemode_other`
`enchant` `effect` `xp` `op` `summon` `clear` `death`。

**不想要死亡统计**就把 `death` 从中删掉 —— 采集端仍会上报（KubeJS 侧不停），
但采集器会直接丢弃，台账里不会出现。想连上报都省掉，
就把 `kubejs/admin_audit.js` 里整个 `EntityEvents.death` 处理器删掉。

反过来，想把**所有**命令都记进台账（不推荐，噪音大）：

```json
{ "log_other_commands": true }
```

## 数据会不会把磁盘写满

单条记录约 300–500 字节。按每天 500 条管理命令估算，一年的台账约 **60–90 MB**，
不构成压力。长期运行可以定期归档：

```bash
# 按月打包旧的按天明细
cd /opt/minecraft-op-audit/log
tar czf /backup/audit-2026-09.tgz events-2026-09-*.jsonl
rm -f events-2026-09-*.jsonl
systemctl restart minecraft-op-audit-dashboard   # 让看板重新读数据
```

`events.csv` 是全量台账，删不掉但增长也很慢，建议直接定期拷走备份。

## 怎么确认采集端真的在工作

在服务端控制台执行一条测试命令，然后：

```bash
grep -a '\[MCAUDIT\]' <服务端>/logs/latest.log | tail -3
```

应该能看到类似：

```
[17Sep2026 04:04:11.123] [Server thread/INFO] [KubeJS Server/]:
    admin_audit.js#45: [MCAUDIT] {"ms":"1789606800000","actor":"Alice","cmd":"give Alice minecraft:diamond 1","gm":"creative"}
```

只要这行出现，说明采集端正常；接下来看采集器日志有没有把它变成「事件」。

## 服务端目录换了 / 换机器了

重跑一次安装脚本并指定新目录即可，台账不会丢：

```bash
sudo bash deploy/install.sh --server-dir /新/服务端/目录
```

记得把 `config.json` 里的 `dash_token` 抄回去（否则已分发的链接会失效），
或者干脆不关心令牌。

## 用了 MCSManager / 面板，能集成吗

不需要集成。面板只是启动服务端的工具，本项目从**日志文件**取数，
与面板无关。面板终端里执行的命令会被记为 `console`（无法区分操作人）。
