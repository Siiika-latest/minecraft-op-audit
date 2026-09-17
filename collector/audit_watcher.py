#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft 管理员行为审计 · 事件采集器

数据源：服务端日志中的 [MCAUDIT] {...} 行
        （由 kubejs/server_scripts/admin_audit.js 产生）

输出：  <安装目录>/log/events.csv               全量台账（Excel 可直接打开）
        <安装目录>/log/events-YYYY-MM-DD.jsonl  按天明细（Web 看板数据源）
        <安装目录>/log/watcher.log              采集器自身日志

设计原则：完全旁路。只读取服务端日志，不修改服务端目录下任何文件。
"""
import csv
import json
import os
import re
import shlex
import time
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
    # 需要记录的敏感行为类型（death = 玩家死亡）
    "enabled_types": ["give", "item_set", "loot", "clear", "gamemode", "gamemode_other",
                      "enchant", "effect", "xp", "op", "summon", "death"],
    # 是否把所有其它命令也记进台账（默认关闭，避免噪音）
    "log_other_commands": False,
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
    "other": "其它命令",
    "raw_cmd": "命令原文(兜底)",
}

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
# 兜底：原版 admin 广播 / 服务端 issued server command
RE_ADMIN_TAIL = re.compile(r"\[(?P<who>[^\[\]:]{1,32}): (?P<msg>.+)\]\s*$")
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
        if len(args) >= 2:
            return dict(type="gamemode_other", target=args[1], mode=args[0], count=None)
        return dict(type="gamemode", target=actor, mode=args[0], count=None)

    if name == "defaultgamemode" and args:
        return dict(type="gamemode_other", target="(服务器默认)", mode=args[0], count=None)

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
        ev["detail_cn"] = "%s 把自己的游戏模式切为 %s" % (a, ev.get("mode", ""))
    elif t == "gamemode_other":
        ev["detail_cn"] = "%s 把 %s 的游戏模式切为 %s" % (a, tg, ev.get("mode", ""))
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
    if ev.get("gm"):
        ev["detail_cn"] += "（当时模式：%s）" % ev["gm"]
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
    if str(d.get("ev", "") or "") == "death":
        # 玩家死亡：不走命令分类器，直接构造事件
        ev = dict(type="death", target="", item_raw="", count=None,
                  cause=str(d.get("cause", "") or ""),
                  killer=str(d.get("killer", "") or ""),
                  msg=str(d.get("msg", "") or ""))
        cmd = ""
    else:
        # 没有 ev 字段的老格式（只有命令事件）也走这里，保证向后兼容
        ev = classify(cmd, actor, conf.get("log_other_commands", False))
        if not ev:
            return None
    ev["actor"] = actor
    ev["gm"] = gm
    ev["cmd"] = cmd
    now_utc = now_utc or datetime.now(TZ_UTC)
    # 时间优先级：事件自带 epoch 毫秒 → 日志行时间戳 → 采集时刻
    dt = None
    try:
        dt = datetime.fromtimestamp(int(str(d.get("ms", ""))) / 1000.0, TZ_UTC).astimezone(TZ_BJ)
    except (TypeError, ValueError, OSError, OverflowError):
        dt = None
    if dt is None:
        ldt = log_ts(line, conf.get("log_tz", "UTC"))
        dt = ldt.astimezone(TZ_BJ) if ldt else now_utc.astimezone(TZ_BJ)
    ev["ts"] = dt.strftime("%Y-%m-%d %H:%M:%S")
    ev["date_bj"] = dt.strftime("%Y-%m-%d")
    ev["raw"] = line
    return ev


# ---------------- 落盘 ----------------
class Sink:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.csv_ready = os.path.exists(CSV_FILE) and os.path.getsize(CSV_FILE) > 0
        self.recent = []
        self.count = 0

    def _dup(self, key):
        h = hash(key)
        if h in self.recent:
            return True
        self.recent.append(h)
        if len(self.recent) > 500:
            self.recent = self.recent[-300:]
        return False

    def write(self, ev):
        if self._dup(ev["raw"]):
            return False
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
                ("/" + ev["cmd"]) if ev.get("cmd") else "",   # 死亡事件没有命令，留空
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
            conf.update(json.load(open(CONF_FILE, encoding="utf-8")))
        except (OSError, ValueError) as e:
            log("配置读取失败，用默认值: %s" % e)
    return conf


def main():
    conf = read_conf()
    if not conf.get("server_log"):
        raise SystemExit("[watcher] 未配置 server_log。请先运行 deploy/install.sh，"
                         "或手动编辑 %s" % CONF_FILE)
    enabled = set(conf.get("enabled_types") or DEFAULT_CONF["enabled_types"])
    names = Names(NAMES_FILE)
    sink = Sink()
    st = load_state()
    log("启动采集器: 日志=%s 轮询=%ss 类型=%s" %
        (conf["server_log"], conf["poll_seconds"], sorted(enabled)))

    ino, off = st.get("inode"), st.get("offset", 0)
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
                log("日志轮转（inode %s -> %s），从头读取新文件" % (ino, stat.st_ino))
                ino, off = stat.st_ino, 0
            if stat.st_size < off:
                log("日志被截断，重置偏移")
                off = 0

            if stat.st_size > off:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(off)
                    chunk = f.read()
                    off = f.tell()
                n = 0
                for line in chunk.splitlines():
                    ev = parse_line(line, names, conf)
                    if not ev or ev["type"] not in enabled:
                        continue
                    ev = enrich(ev, names)
                    if sink.write(ev):
                        n += 1
                        log("事件: %s | %s | %s" % (ev["ts"], TYPE_CN.get(ev["type"]), ev["detail_cn"]))
                if n:
                    log("本批写入 %d 条，累计 %d 条" % (n, sink.count))
            st["inode"], st["offset"] = ino, off
            save_state(st)
        except Exception as e:
            log("!! 采集异常: %r" % e)
        time.sleep(conf["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("采集器退出")
