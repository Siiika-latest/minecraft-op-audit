#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采集器解析逻辑单测

样本格式完全对齐 kubejs/admin_audit.js 的真实输出，不依赖外部 assets。
运行：python3 tests/test_watcher.py
"""
import atexit
import csv
import gzip
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location("audit_watcher",
                                              os.path.join(ROOT, "collector", "audit_watcher.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

CONF = {"log_other_commands": False, "log_tz": "UTC"}

# 用一份小的映射表代替真实 item_names.json，保证测试自包含
FAKE = {
    "id2zh": {"minecraft:diamond": "钻石", "minecraft:netherite_ingot": "下界合金锭",
              "minecraft:enchanted_golden_apple": "附魔金苹果", "create:zinc_ore": "锌矿石",
              "minecraft:bedrock": "基岩", "minecraft:emerald": "绿宝石",
              "minecraft:gold_ingot": "金锭", "minecraft:diamond_sword": "钻石剑",
              "minecraft:diamond_helmet": "钻石头盔", "minecraft:experience": "经验"},
    "id2en": {"minecraft:diamond": "Diamond", "minecraft:netherite_ingot": "Netherite Ingot",
              "minecraft:enchanted_golden_apple": "Enchanted Golden Apple",
              "create:zinc_ore": "Zinc Ore", "minecraft:bedrock": "Bedrock",
              "minecraft:emerald": "Emerald", "minecraft:gold_ingot": "Gold Ingot",
              "minecraft:diamond_sword": "Diamond Sword", "minecraft:diamond_helmet": "Diamond Helmet",
              "minecraft:experience": "Experience"},
}
FAKE["by_en"] = {}
FAKE["by_zh"] = {}
for _i, _n in FAKE["id2en"].items():
    FAKE["by_en"].setdefault(_n, []).append(_i)
for _i, _n in FAKE["id2zh"].items():
    FAKE["by_zh"].setdefault(_n, []).append(_i)

_tmp = tempfile.mkdtemp(prefix="mcaudit-test-")
atexit.register(shutil.rmtree, _tmp, True)   # 退出时清掉临时目录
_names_path = os.path.join(_tmp, "item_names.json")
with open(_names_path, "w", encoding="utf-8") as f:
    json.dump(FAKE, f, ensure_ascii=False)
names = w.Names(_names_path)

ok = 0
total = 0


def check(label, got, want):
    global ok, total
    total += 1
    good = got == want
    ok += 1 if good else 0
    print(("[OK ] " if good else "[!! ] ") + "%s: %r（期望 %r）" % (label, got, want))


def mkline(actor, cmd, gm="", ms="1789606800000", head="17Sep2026 09:00:00.000"):
    """构造一行 KubeJS 采集端真实输出"""
    payload = json.dumps({"ms": ms, "actor": actor, "cmd": cmd, "gm": gm}, ensure_ascii=False)
    return ("[%s] [Server thread/INFO] [KubeJS Server/]: "
            "admin_audit.js#45: [MCAUDIT] " % head + payload)


def mkdeath(actor, cause="fall", killer="", msg="", gm="", ms="1789606800000",
            head="17Sep2026 09:00:00.000"):
    """构造一行死亡事件（采集端 EntityEvents.death 的真实输出）"""
    payload = json.dumps({"ev": "death", "ms": ms, "actor": actor, "cause": cause,
                          "killer": killer, "msg": msg, "gm": gm}, ensure_ascii=False)
    return ("[%s] [Server thread/INFO] [KubeJS Server/]: "
            "admin_audit.js#180: [MCAUDIT] " % head + payload)


def parse(line):
    ev = w.parse_line(line, names, CONF)
    return w.enrich(ev, names) if ev else None


print("=== 命令解析 ===")
CASES = [
    ("give 带数量", mkline("Player1", "give Player1 minecraft:diamond 64", "creative"),
     "give", "Player1", "minecraft:diamond", "钻石", 64),
    ("give 默认数量1", mkline("Player1", "give Player1 minecraft:netherite_ingot"),
     "give", "Player1", "minecraft:netherite_ingot", "下界合金锭", 1),
    ("give 给他人", mkline("Player1", "give Player2 minecraft:enchanted_golden_apple 1"),
     "give", "Player2", "minecraft:enchanted_golden_apple", "附魔金苹果", 1),
    ("give 省略命名空间", mkline("Player1", "give Player1 diamond 1"),
     "give", "Player1", "minecraft:diamond", "钻石", 1),
    ("give 模组物品", mkline("Player1", "give Player1 create:zinc_ore 32"),
     "give", "Player1", "create:zinc_ore", "锌矿石", 32),
    ("give 带 NBT", mkline("Player1",
                          'give Player1 minecraft:diamond_sword{Enchantments:[{id:"sharpness",lvl:5}]} 1'),
     "give", "Player1", "minecraft:diamond_sword", "钻石剑", 1),
    ("item replace entity", mkline("Player1",
                                   "item replace entity Player2 armor.head with minecraft:diamond_helmet 1"),
     "item_set", "Player2", "minecraft:diamond_helmet", "钻石头盔", 1),
    ("切自己模式", mkline("Player1", "gamemode creative"),
     "gamemode", "Player1", "", "", None),
    ("切他人模式", mkline("Player1", "gamemode survival Player2"),
     "gamemode_other", "Player2", "", "", None),
    ("清背包", mkline("Player1", "clear Player2"),
     "clear", "Player2", "", "", None),
    ("授权 OP", mkline("Player1", "op Player2"),
     "op", "Player2", "", "op", None),
    ("控制台执行", mkline("console", "give Player1 minecraft:bedrock 1"),
     "give", "Player1", "minecraft:bedrock", "基岩", 1),
    ("兼容前导斜杠", mkline("Player1", "/give Player1 minecraft:emerald 3"),
     "give", "Player1", "minecraft:emerald", "绿宝石", 3),
]
for label, line, t, tgt, iid, izh, cnt in CASES:
    ev = parse(line)
    if ev is None:
        check(label, None, "解析出事件")
        continue
    check(label, (ev["type"], ev["target"], ev["item_id"], ev["item_zh"], ev["count"]),
          (t, tgt, iid, izh, cnt))

print("\n=== 物品标签 ===")
tag = parse(mkline("Player1", "give Player1 #minecraft:planks 8"))
check("标签前缀保留", tag["item_id"], "#minecraft:planks")
check("标签有中文占位", tag["item_zh"], "（物品标签）")

print("\n=== 噪声过滤（不应入库）===")
for label, line in [
    ("time set day", mkline("console", "time set day")),
    ("weather clear", mkline("Player1", "weather clear")),
    ("say 广播", mkline("Player1", "say hello")),
    ("list 查询", mkline("Player1", "list")),
    ("非审计行", "[17Sep2026 09:00:01.000] [Server thread/INFO] "
                 "[net.minecraft.server.MinecraftServer/]: Set the time to 1000"),
    ("空命令", mkline("Player1", "")),
]:
    check(label, parse(line), None)

print("\n=== 时间换算（一律输出北京时间）===")
check("epoch 毫秒优先", parse(mkline("Player1", "give Player1 minecraft:diamond 1"))["ts"],
      "2026-09-17 09:00:00")
check("毫秒缺失 → 日志时间戳(UTC→北京)",
      parse(mkline("Player1", "give Player1 minecraft:diamond 1", ms="",
                   head="17Sep2026 01:02:03.004"))["ts"], "2026-09-17 09:02:03")
check("毫秒垃圾值 → 日志时间戳",
      parse(mkline("Player1", "give Player1 minecraft:diamond 1", ms="garbage",
                   head="17Sep2026 23:59:59.000"))["ts"], "2026-09-18 07:59:59")
check("跨日归档字段", parse(mkline("Player1", "give Player1 minecraft:diamond 1", ms="",
                                head="17Sep2026 23:59:59.000"))["date_bj"], "2026-09-18")

print("\n=== 目标选择器还原 ===")
check("@s → 执行者本人",
      parse(mkline("Player1", "give @s minecraft:diamond 64", "creative"))["target"], "Player1")
check("控制台 @s 保持原样",
      parse(mkline("console", "give @s minecraft:diamond 64"))["target"], "@s")

print("\n=== 执行者与模式 ===")
ev = parse(mkline("Player1", "give Player1 minecraft:diamond 1", "creative"))
check("执行者", ev["actor"], "Player1")
check("当时模式", ev["gm"], "creative")
check("详情含模式", "creative" in ev["detail_cn"], True)

print("\n=== 玩家死亡事件 ===")
d1 = parse(mkdeath("Player1", cause="player_attack", killer="Player2",
                   msg="Player1 was slain by Player2", gm="survival"))
check("类型为 death", d1["type"], "death")
check("执行者=死亡玩家", d1["actor"], "Player1")
check("击杀者写进详情", "Player2" in d1["detail_cn"], True)
check("死因中文化", "被玩家击杀" in d1["detail_cn"], True)
check("数量为空", d1["count"], None)
check("无物品字段", (d1["item_id"], d1["item_zh"]), ("", ""))
check("游戏内死讯保留", d1["msg"], "Player1 was slain by Player2")
check("模式后缀", "survival" in d1["detail_cn"], True)
check("★ 击杀者不写进 target（否则僵尸会混进玩家榜）", d1["target"], "")
check("时间戳正常换算", d1["ts"], "2026-09-17 09:00:00")

d2 = parse(mkdeath("Player2", cause="fall"))
check("无凶手时的详情", d2["detail_cn"], "Player2 死亡（死因：坠落）")

d3 = parse(mkdeath("Player3", cause="mob_attack", killer="Zombie"))
check("生物击杀者保留原样", "Zombie" in d3["detail_cn"], True)
check("生物击杀者同样不进 target", d3["target"], "")

d4 = parse(mkdeath("Player4", cause="some_mod:weird_damage"))
check("未收录死因原样显示", "some_mod:weird_damage" in d4["detail_cn"], True)

# ── 下面这组 id 是 2026-09-17 在 AFoP 服务端（1.20.1 / KubeJS 6）用 /damage 一个个
#    实测 dump 出来的真实 DamageType.msgId()，**不是**猜的注册名。别改回去。
#    两个最容易踩的：mob_attack 的 msgId 是 "mob"、player_attack 的 msgId 是 "player"。
d5 = parse(mkdeath("Player5", cause="player", killer="Player6"))
check("★ 实测 id: player（不是 player_attack）", "被玩家击杀" in d5["detail_cn"], True)
d6 = parse(mkdeath("Player6", cause="mob", killer="Skeleton"))
check("★ 实测 id: mob（不是 mob_attack）", "被生物击杀" in d6["detail_cn"], True)
d7 = parse(mkdeath("Player7", cause="inWall"))
check("★ 驼峰 id 能查表: inWall", "卡在方块里" in d7["detail_cn"], True)
d8 = parse(mkdeath("Player8", cause="outOfWorld"))
check("★ 驼峰 id 能查表: outOfWorld", "掉出世界" in d8["detail_cn"], True)
d9 = parse(mkdeath("Player9", cause="genericKill"))
check("★ 驼峰 id 能查表: genericKill", "被清除" in d9["detail_cn"], True)
d10 = parse(mkdeath("Player10", cause="lightningBolt"))
check("★ 驼峰 id 能查表: lightningBolt", "被雷劈" in d10["detail_cn"], True)
check("★ cause 字段保留原始 msgId", d7["cause"], "inWall")
check("★ 另外给出 cause_cn 供看板直接显示", d7["cause_cn"], "卡在方块里")
check("cause_key: 驼峰转蛇形", w.cause_key("hotFloor"), "hot_floor")
check("cause_key: 不动模组自造 id", w.cause_key("some_mod:weird_damage"), "some_mod:weird_damage")
check("cause_key: 空值", w.cause_key(""), "")

check("死因映射: lava", w.CAUSE_CN.get("lava"), "岩浆")
check("死因映射: out_of_world", w.CAUSE_CN.get("out_of_world"), "掉出世界")
check("默认配置已启用 death", "death" in w.DEFAULT_CONF["enabled_types"], True)
check("类型中文名", w.TYPE_CN.get("death"), "玩家死亡")

# 采集器必须能把死亡行与命令行区分开
check("旧格式（无 ev 字段）仍按命令解析",
      parse(mkline("Player1", "give Player1 minecraft:diamond 1"))["type"], "give")
check("ev=cmd 显式声明也算命令",
      parse(mkline("Player1", "gamemode creative"))["type"], "gamemode")

print("\n=== 映射表缺失时的降级 ===")
empty = w.Names(os.path.join(_tmp, "not-exist.json"))
ev2 = w.enrich(w.parse_line(mkline("Player1", "give Player1 minecraft:diamond 1"), empty, CONF), empty)
check("仍保留物品 ID", ev2["item_id"], "minecraft:diamond")
check("无翻译名时不报错", ev2["item_zh"], "")

# ============================================================
# v1.2.0：stats / gmpatrol 新事件、模式归一化、巡检去重、
#         原版日志兜底、跨源去重、轮转归档补读、持久去重
# ============================================================
print("\n=== v1.2.0 行为统计事件（ev=stats）===")


def mkstats(actor, broken=0, placed=0, mob=0, pvp=0, span=300, ms="1789606800000",
            head="17Sep2026 09:00:00.000"):
    payload = json.dumps({"ev": "stats", "ms": ms, "actor": actor, "broken": broken,
                          "placed": placed, "mob": mob, "pvp": pvp, "span": span},
                         ensure_ascii=False)
    return ("[%s] [Server thread/INFO] [KubeJS Server/]: "
            "admin_audit.js#60: [MCAUDIT] " % head + payload)


def mkgm(actor, gm, ms="1789606800000", head="17Sep2026 09:00:00.000"):
    payload = json.dumps({"ev": "gmpatrol", "ms": ms, "actor": actor, "gm": gm},
                         ensure_ascii=False)
    return ("[%s] [Server thread/INFO] [KubeJS Server/]: "
            "admin_audit.js#90: [MCAUDIT] " % head + payload)


def mkissued(who, cmd, head="17Sep2026 01:00:00.000"):
    """原版日志兑底行。head 默认 UTC 01:00 —— 与 mkline 的 epoch 毫秒
    （北京 09:00 = UTC 01:00）对齐，这样跨源去重测试里的两行才是同一次命令。"""
    return ("[%s] [Server thread/INFO] [minecraft/MinecraftServer/]: "
            "%s issued server command: %s" % (head, who, cmd))


st = parse(mkstats("Alice", broken=12, placed=3, mob=7, pvp=1, span=300))
check("stats 类型", st["type"], "stats")
check("stats 执行者", st["actor"], "Alice")
check("stats 数值透传", (st["broken"], st["placed"], st["mob_kills"], st["pvp_kills"]),
      (12, 3, 7, 1))
check("stats 无物品无对象", (st["target"], st["item_id"]), ("", ""))
check("stats 详情含汇总", ("破坏方块 12" in st["detail_cn"]) and ("击杀玩家 1" in st["detail_cn"]), True)
st0 = parse(mkstats("Bob", broken=0, placed=0, mob=0, pvp=0))
check("stats 全零详情", st0["detail_cn"], "Bob 近 300 秒：无行为")
check("stats 入默认白名单", "stats" in w.DEFAULT_CONF["enabled_types"], True)
check("stats 类型中文名", w.TYPE_CN.get("stats"), "行为统计汇总")
check("stats 没有命令原文", st["cmd"], "")

print("\n=== v1.2.0 模式巡检事件（ev=gmpatrol）===")
gp = parse(mkgm("yinghong_yh", "creative"))
check("巡检事件按 gamemode 归类", gp["type"], "gamemode")
check("巡检标记 patrol", gp.get("patrol"), True)
check("巡检对象=自己", gp["target"], "yinghong_yh")
check("巡检详情含来源", "巡检发现" in gp["detail_cn"], True)
check("巡检详情含中文模式", "创造" in gp["detail_cn"], True)
check("巡检没有命令原文", gp["cmd"], "")

print("\n=== 模式归一化 ===")
check("norm_mode: 数字", w.norm_mode("1"), "creative")
check("norm_mode: 数字0", w.norm_mode("0"), "survival")
check("norm_mode: 缩写 sp", w.norm_mode("sp"), "spectator")
check("norm_mode: 全称", w.norm_mode("adventure"), "adventure")
check("norm_mode: 未知保留", w.norm_mode("mod:weird"), "mod:weird")
check("norm_mode: 空", w.norm_mode(""), "")
check("classify /gamemode 1 归一化",
      parse(mkline("P1", "gamemode 1"))["mode"], "creative")
check("classify /gamemode c 归一化",
      parse(mkline("P1", "gamemode c"))["mode"], "creative")
check("命令切模式详情用中文", "创造" in parse(mkline("P1", "gamemode creative"))["detail_cn"], True)
check("切他人模式详情用中文",
      "生存" in parse(mkline("P1", "gamemode 0 P2"))["detail_cn"], True)

print("\n=== 玩家死亡事件新增 kp 标记 ===")
dk = parse(mkdeath("P1", cause="player", killer="P2", gm="survival"))
check("kp 缺省为 0（旧采集端数据）", dk.get("kp"), 0)
dline = ("[17Sep2026 09:00:00.000] [Server thread/INFO] [KubeJS Server/]: "
         "admin_audit.js#180: [MCAUDIT] " + json.dumps(
             {"ev": "death", "ms": "1789606800000", "actor": "P1", "cause": "player",
              "killer": "P2", "kp": 1, "msg": "P1 was slain by P2", "gm": "survival"}))
dkp = parse(dline)
check("kp=1 透传（供看板 PVP 统计）", dkp.get("kp"), 1)

print("\n=== 原版日志兜底行解析（issued server command）===")
fb = w.parse_issued_line(mkissued("Alice", "/gamemode creative"), names, CONF)
check("兜底行解析出事件", fb["type"], "gamemode")
check("兜底行执行者", fb["actor"], "Alice")
check("兜底行命令", fb["cmd"], "/gamemode creative")
check("兜底行带 fallback 标记", fb.get("fallback"), "vanilla")
check("兜底行时间非空（来自行首时间戳）", bool(fb["ts"]), True)
check("兜底行日期字段", fb["date_bj"], "2026-09-17")
check("非命令行不误报", w.parse_issued_line(
    "[17Sep2026 09:00:01.000] [Server thread/INFO]: Set the time to 1000", names, CONF), None)
check("MCAUDIT 行不走兜底", w.parse_issued_line(
    mkline("P1", "give P1 minecraft:diamond 1"), names, CONF), None)
check("噪声命令不产生兜底事件", w.parse_issued_line(
    mkissued("Alice", "/list"), names, CONF), None)

print("\n=== 跨源去重（merge_batch）===")
# 同一次 /gamemode：审计行与兑底行相差 0.5 秒 → 兑底行被丢弃
aud = parse(mkline("Alice", "gamemode creative", ms="1789606800000",
                   head="17Sep2026 01:00:00.500"))
fbv = w.parse_issued_line(mkissued("Alice", "/gamemode creative",
                                    head="17Sep2026 01:00:01.000"), names, CONF)
merged = w.merge_batch([aud], [fbv])
check("同命令 ±1 秒 → 只留审计行", [e["raw"] for e in merged], [aud["raw"]])
# 时间相差 3 秒 → 视为两次不同执行，兑底行保留
fbv2 = w.parse_issued_line(mkissued("Alice", "/gamemode creative",
                                     head="17Sep2026 01:00:04.000"), names, CONF)
merged2 = w.merge_batch([aud], [fbv2])
check("相差 3 秒 → 兑底行保留", len(merged2), 2)
# 审计行缺失（KubeJS 失效窗口）→ 兑底行独苗入库
merged3 = w.merge_batch([], [fbv])
check("审计行缺失 → 兑底行入库", len(merged3), 1)
# 不同玩家不互删
fbo = w.parse_issued_line(mkissued("Bob", "/gamemode creative",
                                   head="17Sep2026 01:00:01.000"), names, CONF)
check("不同玩家不误删", len(w.merge_batch([aud], [fbo])), 2)

print("\n=== 巡检去重器（GmPatrolFilter）===")
gmd = w.GmPatrolFilter(window=120)
gmd.note_command("Alice", "creative", 1000.0)
check("命令刚记录 → 巡检丢弃", gmd.allow_patrol("Alice", "creative", 1030.0), False)
check("窗口外 → 巡检保留", gmd.allow_patrol("Alice", "creative", 1000.0 + 121), True)
check("模式不同 → 巡检保留", gmd.allow_patrol("Alice", "survival", 1030.0), True)
check("其他玩家不受影响", gmd.allow_patrol("Bob", "creative", 1030.0), True)
gmd.note_command("Bob", "spectator", 2000.0)
check("第二条命令记录覆盖", gmd.allow_patrol("Bob", "spectator", 2050.0), False)

print("\n=== ingest_lines：兜底 + 巡检 + 白名单整链路 ===")
tmp_sink_dir = tempfile.mkdtemp(prefix="mcaudit-sink-")
atexit.register(shutil.rmtree, tmp_sink_dir, True)
w.LOG_DIR = tmp_sink_dir
w.CSV_FILE = os.path.join(tmp_sink_dir, "events.csv")
w.WATCH_LOG = os.path.join(tmp_sink_dir, "watcher.log")
sink = w.Sink()
gmd2 = w.GmPatrolFilter()
enabled = {"give", "gamemode", "gamemode_other", "death", "stats"}
lines = [
    mkline("Alice", "gamemode creative"),                       # 命令：切模式（建立去重锚点）
    mkgm("Alice", "creative"),                                  # 巡检：同一变化 → 应被丢弃
    mkgm("Alice", "survival"),                                  # 巡检：另一变化 → 保留
    mkline("Alice", "give Alice minecraft:diamond 2"),          # 普通命令
    mkissued("Alice", "/gamemode creative"),                    # 兜底：与命令行同秒 → 丢弃
    mkissued("Bob", "/op Charlie"),                             # 兜底：op 不在白名单 → 丢弃
    mkline("console", "time set day"),                          # 噪声 → 丢弃
    mkstats("Alice", broken=5, placed=2, mob=1, pvp=0),         # 统计汇总
    mkdeath("Carol", cause="lava", killer="", msg=""),          # 死亡
]
n = w.ingest_lines(lines, names, CONF, enabled, sink, gmd2)
check("整链路入库条数（5 = 命令/巡检/give/stats/death）", n, 5)
csv_rows = list(csv.reader(open(w.CSV_FILE, encoding="utf-8-sig")))
check("CSV 表头行数（1 表头 + 5 事件）", len(csv_rows), 6)
patrol_rows = [r for r in csv_rows[1:] if "巡检发现" in r[10]]
check("只保留了模式不同的那条巡检", len(patrol_rows), 1)
check("巡检行详情正确", patrol_rows[0][10], "Alice 把自己的游戏模式切为 生存（巡检发现）")
check("兜底行未双记（无 fallback 行混入）",
      [r for r in csv_rows[1:] if r[12].endswith("issued server command: /gamemode creative")], [])
check("统计汇总行已写入 CSV",
      [r[2] for r in csv_rows[1:] if r[2] == "行为统计汇总"], ["行为统计汇总"])

print("\n=== 持久化去重（Sink 稳定哈希窗口）===")
snap = sink.snapshot()
sink2 = w.Sink(snap)
# 重新构造同一 raw 的事件验证跨进程去重（crc32 哈希跨进程一致）
ev_dup = w.parse_line(mkline("Alice", "give Alice minecraft:diamond 2"), names, CONF)
check("重复 raw 第二次被拒", sink2.write(ev_dup), False)
ev_new = w.parse_line(mkline("Alice", "give Alice minecraft:diamond 3"), names, CONF)
check("不同 raw 正常入库", sink2.write(ev_new), True)

print("\n=== 轮转归档补读（*.log.gz）===")
gz_dir = tempfile.mkdtemp(prefix="mcaudit-gz-")
atexit.register(shutil.rmtree, gz_dir, True)
missed = mkline("yinghong_yh", "gamemode creative", ms="1789600000000",
                head="17Sep2026 08:00:00.000")          # 停机窗口里漏掉的那行
noise = "[17Sep2026 08:00:01.000] [Server thread/INFO]: Set the time to 1000"
for name, content in [("2026-09-17-1.log.gz", missed + "\n" + noise + "\n"),
                      ("2026-09-17-2.log.gz", mkstats("yinghong_yh", broken=9) + "\n")]:
    with gzip.open(os.path.join(gz_dir, name), "wt", encoding="utf-8") as f:
        f.write(content)
st_gz = {}
fake_log = os.path.join(gz_dir, "latest.log")
n_gz = w.scan_rotated_archives(fake_log, st_gz, names, CONF, enabled, sink, gmd2)
check("归档补读已处理新归档", n_gz, True)
check("归档补读写入条数（漏掉的切模式 + 统计汇总）", sink.count, 7)
check("gz 处理清单已记录", st_gz.get("gz_done"),
      ["2026-09-17-1.log.gz", "2026-09-17-2.log.gz"])
# 同一批归档再扫一遍：靠持久去重不重复入库
sink3 = w.Sink(sink.snapshot())
n_gz2 = w.scan_rotated_archives(fake_log, st_gz, names, CONF, enabled, sink3, gmd2)
check("归档二次扫描不重复入库", n_gz2, 0)
check("无归档目录返回 False",
      w.scan_rotated_archives(os.path.join(_tmp, "no-such.log"), {}, names, CONF,
                              enabled, sink3, gmd2), False)

print("\n通过 %d/%d" % (ok, total))
sys.exit(0 if ok == total else 1)

