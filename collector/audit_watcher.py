#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft 管理员行为审计 · 事件采集器（v1.2.0）

数据源：服务端日志中的 [MCAUDIT] {...} 行
        （由 kubejs/server_scripts/admin_audit.js 产生）
兜底源：原版日志的 "<玩家> issued server command: /..." 行（可关，vanilla_fallback）
        —— KubeJS 脚本失效/未加载的窗口期，原版日志仍留有一份玩家命令记录，
           跨源去重保证同一次命令不会记两条。

输出：  <安装目录>/log/events.csv               全量台账（Excel 可直接打开）
        <安装目录>/log/events-YYYY-MM-DD.jsonl  按天明细（Web 看板数据源）
        <安装目录>/log/watcher.log              采集器自身日志

设计原则：完全旁路。只读取服务端日志，不修改服务端目录下任何文件。
"""
import csv
import glob
import gzip
import json
import os
import re
import shlex
import time
import zlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# 安装目录：优先环境变量（供 systemd 指定），否则取脚本所在目录
BASE = os.environ.get("MCAUDIT_HOME") or os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "log")
NAMES_FILE = os.path.join(BASE, "item_names.json")
STATE_FILE = os.path.join(BASE, "state.json")
CONF_FILE = os.path.join(BASE, "config.json")
WATCH_LOG = os.path.join(LOG_DIR, "watcher.log")
CSV_FILE = os.path.join(LOG_DIR, "events.csv")

TZ_BJ = ZoneInfo("Asia/Shanghai")
TZ_UTC = timezone.utc

DEFAULT_CONF = {
    # Minecraft 服务端日志文件（必须是 latest.log；安装脚本会自动探测填写）
    "server_log": "",
    "poll_seconds": 2,
    "first_run_from_end": True,
    # 服务端日志行时间戳所在时区。仅在事件缺毫秒时间戳时用于兜底换算北京时间
    "log_tz": "UTC",
    # 需要记录的敏感行为类型（death = 玩家死亡，stats = 玩家行为统计汇总）
    "enabled_types": ["give", "item_set", "loot", "clear", "gamemode", "gamemode_other",
                      "enchant", "effect", "xp", "op", "summon", "death", "stats"],
    # 是否把所有其它命令也记进台账（默认关闭，避免噪音）
    "log_other_commands": False,
    # 是否解析原版日志的 "issued server command" 行作为 KubeJS 失效时的兜底。
    # 同一次命令与 [MCAUDIT] 行之间按「执行者 + 命令 + ±1 秒」跨源去重，不会双记。
    "vanilla_fallback": True,
    # 日志轮转时是否补读 logs/ 下未处理过的 *.log.gz（防采集器停机跨轮转丢数据）
    "rotated_archive_scan": True,
}

CSV_HEADER = ["时间(北京)", "日期", "事件类型", "执行者", "执行者模式", "对象玩家",
              "物品ID", "物品中文名", "物品英文名", "数量", "详情", "命令原文", "原始日志"]

TYPE_CN = {
    "give": "管理员取物",
    "give_failed": "取物失败",
    "item_set": "管理员设物",
    "loot": "管理员掉落物",
    "gamemode": "切换游戏模式",
    "gamemode_other": "切换他人模式",
    "enchant": "管理员附魔",
    "effect": "管理员给药水",
    "xp": "管理员给经验",
    "op": "权限变更",
    "summon": "管理员刷实体",
    "clear": "清空玩家物品",
    "death": "玩家死亡",
    "stats": "行为统计汇总",
    "other": "其它命令",
    "raw_cmd": "命令原文(兜底)",
}

# 游戏模式归一化：命令里 /gamemode 1、/gamemode c 与巡检上报的 creative
# 必须落到同一个值，否则「命令记录」与「巡检记录」会被当成两次不同切换。
MODE_NORM = {
    "survival": "survival", "s": "survival", "0": "survival",
    "creative": "creative", "c": "creative", "1": "creative",
    "adventure": "adventure", "a": "adventure", "2": "adventure",
    "spectator": "spectator", "sp": "spectator", "3": "spectator",
}
MODE_CN = {"survival": "生存", "creative": "创造",
           "adventure": "冒险", "spectator": "旁观"}


def norm_mode(m):
    """/gamemode 参数（0/1/2/3、s/c/a/sp 缩写或全称）→ 规范模式名。
    未知写法原样保留（兼容 mod 自定义模式）。"""
    s = (m or "").strip().lower()
    if not s:
        return ""
    return MODE_NORM.get(s, s)


def mode_cn(m):
    return MODE_CN.get(m, m or "未知")


# 死因 id → 中文。未收录的原样显示。
#
# ⚠ 左侧一律写「规范化后的 msgId」：MC 的 DamageType.msgId() 实际返回**驼峰**
#   （inWall / outOfWorld / genericKill / lightningBolt / hotFloor），
#   而伤害类型的注册名是蛇形（in_wall / out_of_world / generic_kill …）。
#   查表前会先跑 cause_key() 把驼峰转成蛇形，所以同一张表能同时接住两种写法。
#   另外两个容易写错的特例（1.20.1 实测）：
#     mob_attack  → msgId 是 "mob"
#     player_attack → msgId 是 "player"
#   两个都列在表里，避免写成 mob_attack / player_attack 之后查不到。
CAUSE_CN = {
    # ── 环境伤害
    "fall": "坠落", "lava": "岩浆", "hot_floor": "踩到岩浆块",
    "fire": "着火", "on_fire": "被火烧", "in_fire": "身处火中",
    "drown": "溺水", "starve": "饿死", "freeze": "冻死", "cactus": "仙人掌",
    "sweet_berry_bush": "甜浆果丛", "lightning_bolt": "被雷劈",
    "fly_into_wall": "撞墙（鞘翅）", "in_wall": "卡在方块里", "cramming": "被挤压",
    "out_of_world": "掉出世界", "generic": "通用伤害", "generic_kill": "被清除",
    "falling_block": "被方块砸死", "falling_stalactite": "被钟乳石砸死",
    "stalagmite": "踩到石笋", "anvil": "被铁砧砸死",
    # ── 战斗（"mob"/"player" 是实测值，"mob_attack"/"player_attack" 是同义别名）
    "mob": "被生物击杀", "mob_attack": "被生物击杀",
    "player": "被玩家击杀", "player_attack": "被玩家击杀",
    "mob_attack_no_aggro": "被中立生物击杀", "no_aggro": "无仇恨伤害",
    "arrow": "被箭射死", "trident": "被三叉戟戳死", "thrown": "被投掷物击中",
    "fireball": "被火球击中", "unattributed_fireball": "被火球击中",
    "wither_skull": "被凋零之首击中", "wind_charge": "风弹", "mace_smash": "锤击",
    "sting": "被蛰", "thorns": "反伤", "sonic_boom": "监守者音爆",
    # ── 魔法 / 爆炸
    "magic": "魔法伤害", "indirect_magic": "间接魔法", "wither": "凋零效果",
    "dragon_breath": "龙息",
    "explosion": "爆炸", "explosion.player": "玩家引爆的爆炸",
    "player_explosion": "玩家引爆的爆炸",
}


def cause_key(cause):
    """死因 id → 查表用的规范化 key（驼峰转蛇形 + 小写）。

    MC 的 DamageType.msgId() 给的是驼峰（inWall），注册名是蛇形（in_wall）；
    模组自造的 id（如 some_mod:weird_damage）没有大写字母，转换后原样保留。
    """
    s = (cause or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return s.lower()


def cause_cn(cause):
    """死因 → 中文描述；未收录的原样返回，空则「未知」"""
    if not cause:
        return "未知"
    key = cause_key(cause)
    if key in CAUSE_CN:
        return CAUSE_CN[key]
    return CAUSE_CN.get(cause, cause)


def log(msg):
    line = "[%s] %s" % (datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(WATCH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def trim_watch_log(max_bytes=5 * 1024 * 1024, keep_bytes=200 * 1024):
    """watcher.log 超过 5MB 时保留尾部 200KB，防止长期运行无限增长。"""
    try:
        if os.path.exists(WATCH_LOG) and os.path.getsize(WATCH_LOG) > max_bytes:
            with open(WATCH_LOG, "rb") as f:
                f.seek(-keep_bytes, 2)
                data = f.read()
            with open(WATCH_LOG, "wb") as f:
                f.write("[watcher.log 已自动截断，仅保留最近 200KB]\n".encode("utf-8") + data)
            print("[watcher] watcher.log 超过 %dMB，已截断保留尾部" % (max_bytes // 1024 // 1024),
                  flush=True)
    except OSError:
        pass


# ---------------- 物品名映射 ----------------
class Names:
    """物品 id → 中文名 / 英文名 映射表。

    映射表由 tools/build_item_names.py 扫服务端 jar 生成。
    文件缺失时退化为空表：台账仍会记录物品 ID，只是没有翻译名。
    """

    def __init__(self, path):
        self.id2zh = {}
        self.id2en = {}
        self.by_en = {}
        self.by_zh = {}
        if not os.path.exists(path):
            log("提示：未找到物品名称映射表 %s（台账将只记录物品 ID，无翻译名）" % path)
            log("      可用 tools/build_item_names.py 生成")
            return
        log("加载物品名称映射表 ...")
        try:
            d = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError) as e:
            log("!! 映射表读取失败，退化为只用 ID：%r" % e)
            return
        self.id2zh = d.get("id2zh", {})
        self.id2en = d.get("id2en", {})
        self.by_en = d.get("by_en", {})
        self.by_zh = d.get("by_zh", {})
        log("映射表就绪: id2zh=%d id2en=%d" % (len(self.id2zh), len(self.id2en)))

    def by_id(self, iid):
        return self.id2zh.get(iid, ""), self.id2en.get(iid, "")

    RE_ID = re.compile(r"^[a-z0-9_\-\.]+:[a-z0-9_\-\./]+$", re.I)

    @staticmethod
    def _rank(ids):
        def score(i):
            ns = i.split(":", 1)[0]
            return (0 if ns == "minecraft" else 1, len(i), i)
        return sorted(ids, key=score)

    def resolve_name(self, display_name):
        """反查（仅在命令里给的是显示名而非 id 时使用）"""
        n = (display_name or "").strip()
        if not n:
            return "", ""
        ids = self.by_en.get(n) or self.by_zh.get(n)
        if not ids:
            return "", n
        return self._rank(ids)[0], n


def norm_item(raw):
    """把命令里的物品参数规范化成 (id, nbt, is_tag)"""
    s = (raw or "").strip().strip('"')
    nbt = ""
    i = s.find("{")
    if i >= 0:
        nbt, s = s[i:], s[:i]
    i = s.find("[")                      # 物品修饰/组件（1.20.5+ 语法，这里兼容）
    if i >= 0:
        s = s[:i]
    tag = s.startswith("#")
    if tag:
        s = s[1:]
    if s and ":" not in s:
        s = "minecraft:" + s
    return s.lower(), nbt, tag


# ---------------- 命令解析 ----------------
RE_MCAUDIT = re.compile(r"\[MCAUDIT\]\s+(\{.*\})\s*$")
# Forge / 原版日志行首时间戳，形如 [17Sep2026 00:59:29.895] 或 [00:59:29]
RE_FORGE_TS = re.compile(r"^\[(\d{2})([A-Za-z]{3})(\d{4}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})\]")
RE_VANILLA_TS = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# 原版日志兜底：玩家执行命令时服务端会写一行 "<玩家> issued server command: /命令"。
# 这行与语言无关、始终存在 —— KubeJS 脚本失效的窗口期靠它兜底。
RE_ISSUED = re.compile(r"(?P<who>\S+) issued server command:\s*(?P<cmd>/.*)$")


def log_ts(line, tz_name):
    """从日志行首取时间戳，返回带时区的 datetime；失败返回 None"""
    tz = ZoneInfo(tz_name or "UTC")
    m = RE_FORGE_TS.match(line)
    if m:
        d, mon, y, H, M, S, ms = m.groups()
        mo = MONTHS.get(mon.lower())
        if not mo:
            return None
        try:
            return datetime(int(y), mo, int(d), int(H), int(M), int(S),
                            int(ms) * 1000, tzinfo=tz)
        except ValueError:
            return None
    m = RE_VANILLA_TS.match(line)
    if m:
        H, M, S = m.groups()
        now = datetime.now(tz)
        try:
            return now.replace(hour=int(H), minute=int(M), second=int(S), microsecond=0)
        except ValueError:
            return None
    return None


def _int(x, d=None):
    try:
        return int(x)
    except (TypeError, ValueError):
        return d


def classify(cmd, actor, log_other=False):
    """把命令原文解析成审计事件；不关注的命令返回 None"""
    c = (cmd or "").strip()
    if c.startswith("/"):
        c = c[1:]
    if not c:
        return None
    try:
        parts = shlex.split(c)
    except ValueError:
        parts = c.split()
    if not parts:
        return None
    name, args = parts[0].lower(), parts[1:]

    # /give <目标> <物品> [数量]
    if name == "give" and len(args) >= 2:
        return dict(type="give", target=args[0], item_raw=args[1],
                    count=_int(args[2], 1) if len(args) > 2 else 1)

    # /item replace entity <目标> <槽位> with <物品> [数量]
    if name == "item" and len(args) >= 2 and args[0].lower() == "replace":
        if args[1].lower() == "entity" and len(args) >= 6 and args[4].lower() == "with":
            return dict(type="item_set", target=args[2], item_raw=args[5],
                        count=_int(args[6], 1) if len(args) > 6 else 1)
        if args[1].lower() == "block" and len(args) >= 8 and args[6].lower() == "with":
            return dict(type="item_set", target="坐标 %s %s %s" % (args[2], args[3], args[4]),
                        item_raw=args[7], count=_int(args[8], 1) if len(args) > 8 else 1)

    # /loot give <目标> <来源>
    if name == "loot" and args and args[0].lower() == "give" and len(args) >= 2:
        return dict(type="loot", target=args[1], item_raw=" ".join(args[2:]) or "loot", count=None)

    # /gamemode <模式> [目标]
    if name == "gamemode" and args:
        mode = norm_mode(args[0])
        if len(args) >= 2:
            return dict(type="gamemode_other", target=args[1], mode=mode, count=None)
        return dict(type="gamemode", target=actor, mode=mode, count=None)

    if name == "defaultgamemode" and args:
        return dict(type="gamemode_other", target="(服务器默认)", mode=norm_mode(args[0]), count=None)

    # /clear [目标] [物品] [上限]
    if name == "clear":
        return dict(type="clear", target=args[0] if args else actor,
                    item_raw=args[1] if len(args) > 1 else "",
                    count=_int(args[2]) if len(args) > 2 else None)

    # /enchant <目标> <附魔> [等级]
    if name == "enchant" and len(args) >= 2:
        return dict(type="enchant", target=args[0], item_raw=args[1],
                    count=_int(args[2]) if len(args) > 2 else None)

    # /effect give <目标> <效果> [...]
    if name == "effect" and len(args) >= 3 and args[0].lower() == "give":
        return dict(type="effect", target=args[1], item_raw=args[2], count=None)

    # /xp add <目标> <点数>
    if name == "xp" and args and args[0].lower() == "add" and len(args) >= 3:
        return dict(type="xp", target=args[1], item_raw="经验", count=_int(args[2]), is_xp=True)

    # /op、/deop <目标>
    if name in ("op", "deop") and args:
        return dict(type="op", target=args[0], item_raw=name, count=None, is_perm=True)

    # /summon <实体> [...]
    if name == "summon" and args:
        return dict(type="summon", target=args[0], item_raw=args[0], count=None)

    if log_other:
        return dict(type="other", target="", item_raw="", count=None, cmd_only=True)
    return None


def enrich(ev, names):
    """补物品 id / 中英文名 / 中文详情"""
    actor = ev.get("actor", "console")
    cmd = ev.get("cmd", "")
    # @s 表示"执行者自己"，直接还原成执行者名，便于按玩家统计
    if ev.get("target") == "@s" and actor and actor not in ("console", "unknown-player"):
        ev["target"] = actor

    # 玩家死亡：没有物品，也没有「对象玩家」。
    # 击杀者刻意**不写进 target** —— 否则僵尸、骷髅会被当成玩家混进玩家榜。
    if ev.get("type") == "death":
        ev["item_id"], ev["item_zh"], ev["item_en"] = "", "", ""
        ev["target"] = ""
        cause = ev.get("cause", "") or ""
        cz = cause_cn(cause)
        ev["cause_cn"] = cz                     # 供看板直接显示（cause 保留原始 msgId）
        killer = ev.get("killer", "") or ""
        if killer:
            ev["detail_cn"] = "%s 被 %s 击杀（死因：%s）" % (actor, killer, cz)
        else:
            ev["detail_cn"] = "%s 死亡（死因：%s）" % (actor, cz)
        if ev.get("gm"):
            ev["detail_cn"] += "（当时模式：%s）" % ev["gm"]
        return ev

    # 行为统计汇总（v1.2.0）：一条记录代表一个统计周期内的聚合值
    if ev.get("type") == "stats":
        ev["item_id"], ev["item_zh"], ev["item_en"] = "", "", ""
        ev["target"] = ""
        parts = []
        if ev.get("broken"):
            parts.append("破坏方块 %d" % ev["broken"])
        if ev.get("placed"):
            parts.append("放置方块 %d" % ev["placed"])
        if ev.get("mob_kills"):
            parts.append("击杀生物 %d" % ev["mob_kills"])
        if ev.get("pvp_kills"):
            parts.append("击杀玩家 %d" % ev["pvp_kills"])
        span = ev.get("span") or 0
        ev["detail_cn"] = "%s 近 %d 秒：%s" % (actor, span, "、".join(parts) or "无行为")
        return ev

    if ev.pop("is_xp", False):
        iid, izh, ien = "minecraft:experience", "经验", "Experience"
    elif ev.pop("is_perm", False):
        iid, izh, ien = "", ev["item_raw"], ev["item_raw"]
    elif ev.pop("cmd_only", False):
        iid, izh, ien = "", "", ""
    elif ev.get("type") in ("enchant", "effect"):
        iid, izh, ien = "", "", ev.get("item_raw", "")
    else:
        iid, nbt, tag = norm_item(ev.get("item_raw", ""))
        izh, ien = names.by_id(iid)
        if tag:
            iid = "#" + iid
            izh = izh or "（物品标签）"
        if not izh and not ien and ev.get("item_raw"):
            iid2, en2 = names.resolve_name(ev["item_raw"])
            if iid2:
                iid = iid2
                izh, ien = names.by_id(iid2)
            else:
                ien = ev["item_raw"]

    ev["item_id"], ev["item_zh"], ev["item_en"] = iid, izh, ien

    t, a, tg, n = ev["type"], actor, ev.get("target", ""), ev.get("count")
    item = izh or ien or ev.get("item_raw", "")
    cnt = ("" if n is None else " %s 个" % n)

    if t == "give":
        ev["detail_cn"] = "%s 给予 %s%s %s" % (a, tg, cnt, item)
    elif t == "item_set":
        ev["detail_cn"] = "%s 把 %s 的物品槽设为%s %s" % (a, tg, cnt, item)
    elif t == "loot":
        ev["detail_cn"] = "%s 让 %s 获得掉落物（%s）" % (a, tg, ev.get("item_raw", ""))
    elif t == "gamemode":
        # patrol=True 表示这条来自「模式巡检」而非命令（v1.2.0 兜底采集）
        tag = "（巡检发现）" if ev.get("patrol") else ""
        ev["detail_cn"] = "%s 把自己的游戏模式切为 %s%s" % (a, mode_cn(ev.get("mode", "")), tag)
    elif t == "gamemode_other":
        ev["detail_cn"] = "%s 把 %s 的游戏模式切为 %s" % (a, tg, mode_cn(ev.get("mode", "")))
    elif t == "enchant":
        ev["detail_cn"] = "%s 给 %s 的物品附魔 %s" % (a, tg, item)
    elif t == "effect":
        ev["detail_cn"] = "%s 给 %s 施加效果 %s" % (a, tg, item)
    elif t == "xp":
        ev["detail_cn"] = "%s 给 %s 增加 %s 点经验" % (a, tg, n)
    elif t == "op":
        ev["detail_cn"] = "%s 对 %s 执行了 /%s（权限变更）" % (a, tg, ev.get("item_raw", ""))
    elif t == "summon":
        ev["detail_cn"] = "%s 生成了 entity：%s" % (a, item)
    elif t == "clear":
        ev["detail_cn"] = "%s 清空了 %s 的物品%s" % (a, tg, cnt)
    elif t == "other":
        ev["detail_cn"] = "%s 执行命令 /%s" % (a, cmd)
    else:
        ev["detail_cn"] = ev.get("raw", "")
    # patrol 巡检事件：detail 已含目标模式，不再追加「当时模式」后缀，避免同一条信息重复两遍
    if ev.get("gm") and not ev.get("patrol"):
        ev["detail_cn"] += "（当时模式：%s）" % ev["gm"]
    return ev


def _fill_time(ev, d_ms, line, conf, now_utc=None):
    """时间优先级：事件自带 epoch 毫秒 → 日志行时间戳 → 采集时刻。
    同时写入内部字段 _e（epoch 秒，供跨源去重与巡检窗口判断，入库前剔除）。"""
    now_utc = now_utc or datetime.now(TZ_UTC)
    dt = None
    try:
        dt = datetime.fromtimestamp(int(str(d_ms)) / 1000.0, TZ_UTC).astimezone(TZ_BJ)
    except (TypeError, ValueError, OSError, OverflowError):
        dt = None
    if dt is None:
        ldt = log_ts(line, conf.get("log_tz", "UTC"))
        dt = ldt.astimezone(TZ_BJ) if ldt else now_utc.astimezone(TZ_BJ)
    ev["ts"] = dt.strftime("%Y-%m-%d %H:%M:%S")
    ev["date_bj"] = dt.strftime("%Y-%m-%d")
    try:
        ev["_e"] = dt.timestamp()
    except (OSError, OverflowError, ValueError):
        ev["_e"] = 0.0
    return ev


def parse_line(line, names, conf, now_utc=None):
    """解析一行 [MCAUDIT] 日志；非审计行返回 None"""
    line = line.rstrip("\r\n")
    if "[MCAUDIT]" not in line:
        return None
    m = RE_MCAUDIT.search(line)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return None
    cmd = str(d.get("cmd", ""))
    actor = str(d.get("actor", "console") or "console")
    gm = str(d.get("gm", "") or "")
    evname = str(d.get("ev", "") or "")
    if evname == "death":
        # 玩家死亡：不走命令分类器，直接构造事件
        ev = dict(type="death", target="", item_raw="", count=None,
                  cause=str(d.get("cause", "") or ""),
                  killer=str(d.get("killer", "") or ""),
                  kp=_int(d.get("kp"), 0),
                  msg=str(d.get("msg", "") or ""))
        cmd = ""
    elif evname == "stats":
        # 行为统计汇总（采集端每 5 分钟 / 下线时聚合输出一次）
        ev = dict(type="stats", target="", item_raw="", count=None,
                  broken=_int(d.get("broken"), 0) or 0,
                  placed=_int(d.get("placed"), 0) or 0,
                  mob_kills=_int(d.get("mob"), 0) or 0,
                  pvp_kills=_int(d.get("pvp"), 0) or 0,
                  span=_int(d.get("span"), 0) or 0)
        cmd = ""
    elif evname == "gmpatrol":
        # 模式巡检兜底（采集端 5 秒轮询发现的游戏模式变化，非命令途径也能记到）
        ev = dict(type="gamemode", target=actor,
                  mode=norm_mode(str(d.get("gm", "") or "")),
                  count=None, patrol=True)
        cmd = ""
    else:
        # 没有 ev 字段的老格式（只有命令事件）也走这里，保证向后兼容
        ev = classify(cmd, actor, conf.get("log_other_commands", False))
        if not ev:
            return None
    ev["actor"] = actor
    ev["gm"] = gm
    ev["cmd"] = cmd
    _fill_time(ev, d.get("ms", ""), line, conf, now_utc)
    ev["raw"] = line
    return ev


def parse_issued_line(line, names, conf, now_utc=None):
    """解析一行原版日志 "<玩家> issued server command: /命令"（KubeJS 失效兜底）。

    仅作为 [MCAUDIT] 行的**替补**：主流程会先收集本批 [MCAUDIT] 事件，
    再用 merge_batch() 按「执行者 + 命令 + ±1 秒」跨源去重 —— 同一次命令
    已有审计行时，兜底行会被丢弃，不会双记。
    """
    line = line.rstrip("\r\n")
    if "[MCAUDIT]" in line or "issued server command" not in line:
        return None
    m = RE_ISSUED.search(line)
    if not m:
        return None
    actor = m.group("who")
    cmd = m.group("cmd")
    ev = classify(cmd, actor, False)       # 兜底只记白名单内的管理命令
    if not ev:
        return None
    ev["actor"] = actor
    ev["gm"] = ""
    ev["cmd"] = cmd
    _fill_time(ev, "", line, conf, now_utc)
    ev["raw"] = line
    ev["fallback"] = "vanilla"
    return ev


def dedup_key(ev):
    """跨源去重键：（执行者, 去斜杠的命令, 小写）"""
    c = (ev.get("cmd", "") or "").strip()
    if c.startswith("/"):
        c = c[1:]
    return (ev.get("actor", ""), c.lower())


def merge_batch(primary, fallback):
    """[MCAUDIT] 事件 + 原版兜底事件 → 合并去重后的列表。

    兜底事件若与某条审计事件「执行者与命令相同、发生时间相差 ≤1 秒」，
    判定为同一次命令，丢弃兜底行（审计行的信息更全：带 gm、带毫秒时间戳）。
    """
    base = {}
    for ev in primary:
        base.setdefault(dedup_key(ev), []).append(ev.get("_e", 0.0))
    out = list(primary)
    for ev in fallback:
        s = ev.get("_e", 0.0)
        if any(abs(s - s2) <= 1.0 for s2 in base.get(dedup_key(ev), ())):
            continue
        out.append(ev)
    return out


class GmPatrolFilter:
    """「巡检发现的游戏模式变化」与「命令记录」的去重器。

    命令途径切换会先产生一条 gamemode/gamemode_other 事件；随后（≤5 秒）
    巡检也会发现这次变化。若不做处理，同一次切换会记两条。
    规则：命令事件入账时记录 (被切玩家, 目标模式, 时间)；巡检事件到来时，
    同玩家同模式且在窗口期（默认 120 秒）内 → 判为同一次，丢弃巡检事件。
    """

    def __init__(self, window=120):
        self.window = window
        self.recent = {}       # 玩家名 -> (mode, epoch_sec)

    def note_command(self, target, mode, now):
        if target and mode:
            self.recent[target] = (mode, now or 0.0)

    def allow_patrol(self, actor, mode, now):
        r = self.recent.get(actor)
        if r and r[0] == mode and abs((now or 0.0) - r[1]) <= self.window:
            return False        # 命令已经记过这次切换
        return True


# ---------------- 落盘 ----------------
class Sink:
    """台账落盘 + 原始行去重。

    去重用 crc32 稳定哈希（跨进程一致），窗口持久化到 state.json ——
    这样「日志轮转时补读 *.log.gz 归档」不会把已入库的行再记一遍。
    """

    def __init__(self, saved=None):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.csv_ready = os.path.exists(CSV_FILE) and os.path.getsize(CSV_FILE) > 0
        self.recent = list(saved or [])
        self.recent_set = set(self.recent)
        self.count = 0

    @staticmethod
    def _hash(key):
        return zlib.crc32(key.encode("utf-8", "replace")) & 0xFFFFFFFF

    def _dup(self, key):
        h = self._hash(key)
        if h in self.recent_set:
            return True
        self.recent_set.add(h)
        self.recent.append(h)
        if len(self.recent) > 8000:
            self.recent = self.recent[-5000:]
            self.recent_set = set(self.recent)
        return False

    def snapshot(self, limit=5000):
        """用于持久化的最近哈希窗口"""
        return self.recent[-limit:]

    def write(self, ev):
        if self._dup(ev["raw"]):
            return False
        ev.pop("_e", None)
        p = os.path.join(LOG_DIR, "events-%s.jsonl" % ev["date_bj"])
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        new = not self.csv_ready
        with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CSV_HEADER)
                self.csv_ready = True
            w.writerow([
                ev["ts"], ev["date_bj"], TYPE_CN.get(ev["type"], ev["type"]),
                ev["actor"], ev.get("gm", ""), ev.get("target", ""),
                ev.get("item_id", ""), ev.get("item_zh", ""), ev.get("item_en", ""),
                "" if ev.get("count") is None else ev["count"],
                ev.get("detail_cn", ""),
                ("/" + ev["cmd"]) if ev.get("cmd") else "",   # 死亡/统计事件没有命令，留空
                ev["raw"],
            ])
        self.count += 1
        return True


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def read_conf():
    conf = dict(DEFAULT_CONF)
    if os.path.exists(CONF_FILE):
        try:
            # utf-8-sig：兼容带 BOM 的配置文件（Windows 记事本/PowerShell 5.1 默认写 BOM）
            conf.update(json.load(open(CONF_FILE, encoding="utf-8-sig")))
        except (OSError, ValueError) as e:
            log("配置读取失败，用默认值: %s" % e)
    return conf


def ingest_lines(lines, names, conf, enabled, sink, gmd, now_utc=None):
    """一批日志行 → 事件入库（主循环与轮转归档补读共用）。

    流程：解析 [MCAUDIT] 行 → 原版 issued 兜底行（可关）→ 跨源去重 →
    类型白名单 → 巡检/命令模式去重 → enrich → 落盘。返回写入条数。
    """
    now_utc = now_utc or datetime.now(TZ_UTC)
    vanilla_on = conf.get("vanilla_fallback", True)
    prim, fb = [], []
    for line in lines:
        ev = parse_line(line, names, conf, now_utc)
        if ev is not None:
            prim.append(ev)
            continue
        if vanilla_on:
            bev = parse_issued_line(line, names, conf, now_utc)
            if bev is not None:
                fb.append(bev)
    batch = merge_batch(prim, fb) if vanilla_on else prim

    n = 0
    for ev in batch:
        # 命令型模式切换无论是否入库都要入账，供巡检事件去重判断
        if ev.get("type") in ("gamemode", "gamemode_other") and not ev.get("patrol"):
            gmd.note_command(ev.get("target"), ev.get("mode"), ev.get("_e", 0.0))
        if ev["type"] not in enabled:
            continue
        if ev.get("type") == "gamemode" and ev.get("patrol"):
            if not gmd.allow_patrol(ev.get("actor"), ev.get("mode"), ev.get("_e", 0.0)):
                log("巡检去重：%s 切为 %s 已由命令记录，跳过" %
                    (ev.get("actor"), ev.get("mode")))
                continue
        ev = enrich(ev, names)
        if sink.write(ev):
            n += 1
            log("事件: %s | %s | %s" % (ev["ts"], TYPE_CN.get(ev["type"]), ev["detail_cn"]))
    return n


def scan_rotated_archives(server_log, state, names, conf, enabled, sink, gmd):
    """补读服务端日志目录下未处理过的 *.log.gz 归档。

    解决的问题：采集器停机（或 systemd 重启间隙）恰逢日志轮转时，旧
    latest.log 里尚未读到的那段会随压缩归档而「永久跳过」—— inode 已变，
    offset 只对新文件有效。轮转时刻主动扫描归档，未处理过的全部读一遍，
    靠 Sink 的持久化哈希窗口去重，已入库的行不会重复记录。

    仅在检测到轮转的那一次调用（首次运行不回灌历史）。
    返回是否有新归档被处理（用于判断是否需要保存 state）。
    """
    if not conf.get("rotated_archive_scan", True):
        return False
    log_dir = os.path.dirname(os.path.abspath(server_log))
    done = set(state.get("gz_done") or [])
    todo = [p for p in sorted(glob.glob(os.path.join(log_dir, "*.log.gz")))
            if os.path.basename(p) not in done]
    if not todo:
        return False
    total = 0
    for p in todo:
        bn = os.path.basename(p)
        n = 0
        try:
            with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            n = ingest_lines(lines, names, conf, enabled, sink, gmd)
        except OSError as e:
            log("!! 归档 %s 读取失败（跳过）：%r" % (bn, e))
        done.add(bn)
        total += n
        log("轮转归档补读 %s：写入 %d 条" % (bn, n))
    state["gz_done"] = sorted(done)
    return True


def main():
    conf = read_conf()
    if not conf.get("server_log"):
        raise SystemExit("[watcher] 未配置 server_log。请先运行 deploy/install.sh，"
                         "或手动编辑 %s" % CONF_FILE)
    trim_watch_log()
    enabled = set(conf.get("enabled_types") or DEFAULT_CONF["enabled_types"])
    names = Names(NAMES_FILE)
    st = load_state()
    sink = Sink(st.get("dedup") or [])
    gmd = GmPatrolFilter()
    log("启动采集器: 日志=%s 轮询=%ss 类型=%s 兜底=%s" %
        (conf["server_log"], conf["poll_seconds"], sorted(enabled),
         "开" if conf.get("vanilla_fallback", True) else "关"))
    if "stats" not in enabled:
        log("提示：enabled_types 未包含 stats，玩家行为统计（破坏/放置/击杀）不会入库")

    ino, off = st.get("inode"), st.get("offset", 0)
    gz_done_changed = False
    path = conf["server_log"]
    warned = False
    while True:
        try:
            if not os.path.exists(path):
                if not warned:
                    log("!! 日志文件不存在: %s（等待出现…）" % path)
                    warned = True
                time.sleep(conf["poll_seconds"])
                continue
            warned = False
            stat = os.stat(path)
            if ino is None:
                off = stat.st_size if conf.get("first_run_from_end", True) else 0
                ino = stat.st_ino
                log("首次运行：从偏移 %d 开始" % off)
            if stat.st_ino != ino:
                log("日志轮转（inode %s -> %s），先补读归档再从头读取新文件" % (ino, stat.st_ino))
                # 停机/轮转间隙可能漏读旧文件尾部：补读所有未处理过的 *.log.gz
                gz_done_changed = scan_rotated_archives(
                    path, st, names, conf, enabled, sink, gmd)
                ino, off = stat.st_ino, 0
            if stat.st_size < off:
                log("日志被截断，重置偏移")
                off = 0

            wrote = 0
            if stat.st_size > off:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(off)
                    chunk = f.read()
                    off = f.tell()
                wrote = ingest_lines(chunk.splitlines(), names, conf, enabled, sink, gmd)
                if wrote:
                    log("本批写入 %d 条，累计 %d 条" % (wrote, sink.count))
            if wrote or gz_done_changed or ino != st.get("inode") or off != st.get("offset"):
                st["inode"], st["offset"] = ino, off
                st["dedup"] = sink.snapshot()
                save_state(st)
                gz_done_changed = False
            elif "dedup" not in st:
                st["dedup"] = sink.snapshot()
                save_state(st)
        except Exception as e:
            log("!! 采集异常: %r" % e)
        time.sleep(conf["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("采集器退出")
