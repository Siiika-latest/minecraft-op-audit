#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板单测：鉴权逻辑 + 数据聚合（玩家板块 / 时间轴筛选）

运行：python3 tests/test_dashboard.py
"""
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location("audit_dashboard",
                                              os.path.join(ROOT, "dashboard", "audit_dashboard.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

ok = 0
total = 0


def check(label, got, want):
    global ok, total
    total += 1
    good = got == want
    ok += 1 if good else 0
    color = "[OK ] " if good else "[!! ] "
    print(color + "%-34s %r%s" % (label, got, "" if good else "（期望 %r）" % (want,)))


# ============================================================
# 一、鉴权
# ============================================================
print("=== 鉴权 ===")
H = d.Handler


class Stub:
    _authed = H._authed
    _cookies = H._cookies

    def __init__(self, conf, ip, cookie=None):
        self.conf = conf
        self.client_address = (ip, 34567)
        self.headers = {"Cookie": cookie} if cookie else {}
        self._issue_ck = False


TOK = "SECRETTOKEN"
LOCKED = {"require_token": True, "lan_no_token": True, "dash_token": TOK}
OPEN = {"require_token": False, "lan_no_token": True, "dash_token": TOK}
STRICT = {"require_token": True, "lan_no_token": False, "dash_token": TOK}

AUTH = [
    ("关闭校验-外网放行", OPEN, "122.228.149.10", {}, None, True),
    ("关闭校验-内网放行", OPEN, "192.168.1.50", {}, None, True),
    ("内网免令牌", LOCKED, "192.168.1.50", {}, None, True),
    ("本机免令牌", LOCKED, "127.0.0.1", {}, None, True),
    ("IPv6 映射内网免令牌", LOCKED, "::ffff:192.168.1.50", {}, None, True),
    ("10 段内网免令牌", LOCKED, "10.0.0.7", {}, None, True),
    ("外网无令牌拒绝", LOCKED, "122.228.149.10", {}, None, False),
    ("外网带令牌放行", LOCKED, "122.228.149.10", {"k": [TOK]}, None, True),
    ("外网错令牌拒绝", LOCKED, "122.228.149.10", {"k": ["wrong"]}, None, False),
    ("外网 Cookie 放行", LOCKED, "122.228.149.10", {}, "mcaudit_k=" + TOK, True),
    ("外网错 Cookie 拒绝", LOCKED, "122.228.149.10", {}, "mcaudit_k=nope", False),
    ("关闭内网豁免后拒绝", STRICT, "192.168.1.50", {}, None, False),
]
for label, conf, ip, q, ck, want in AUTH:
    s = Stub(conf, ip, ck)
    check(label, s._authed(q, s.headers), want)

s = Stub(LOCKED, "122.228.149.10")
s._authed({"k": [TOK]}, s.headers)
check("URL 带令牌 → 下发 Cookie", s._issue_ck, True)
s2 = Stub(LOCKED, "192.168.1.50")
s2._authed({}, s2.headers)
check("内网豁免 → 不下发 Cookie", s2._issue_ck, False)

check("内网判定 192.168", d.is_lan("192.168.1.1"), True)
check("内网判定 8.8.8.8", d.is_lan("8.8.8.8"), False)
check("IPv6 映射去前缀", d.peer_ip("::ffff:10.1.2.3"), "10.1.2.3")

# ============================================================
# 二、数据聚合
# ============================================================
print("\n=== 数据聚合 ===")
tmp = tempfile.mkdtemp(prefix="mcaudit-dash-")
atexit.register(shutil.rmtree, tmp, True)   # 退出时清掉临时目录
d.LOG_DIR = tmp
d._cache["sig"] = None

EVENTS = [
    # 日期, 类型, 执行者, 对象, 物品id, 物品中文
    ("2026-09-17", "give", "Alice", "Alice", "minecraft:diamond", "钻石"),
    ("2026-09-17", "give", "Alice", "Bob", "minecraft:emerald", "绿宝石"),
    ("2026-09-17", "gamemode", "Alice", "Alice", "", ""),
    ("2026-09-17", "give", "Bob", "@a", "minecraft:stone", "石头"),   # 选择器不算玩家
    ("2026-09-16", "give", "Carol", "Carol", "minecraft:gold_ingot", "金锭"),
    ("2026-09-16", "op", "console", "Carol", "", ""),
]
with open(os.path.join(tmp, "events-2026-09-17.jsonl"), "w", encoding="utf-8") as f:
    for date, t, actor, target, iid, izh in EVENTS[:4]:
        f.write(json.dumps({"ts": date + " 12:00:00", "date_bj": date, "type": t,
                            "actor": actor, "target": target, "item_id": iid,
                            "item_zh": izh, "detail_cn": t, "raw": "%s|%s|%s" % (t, actor, target)},
                           ensure_ascii=False) + "\n")
with open(os.path.join(tmp, "events-2026-09-16.jsonl"), "w", encoding="utf-8") as f:
    for date, t, actor, target, iid, izh in EVENTS[4:]:
        f.write(json.dumps({"ts": date + " 12:00:00", "date_bj": date, "type": t,
                            "actor": actor, "target": target, "item_id": iid,
                            "item_zh": izh, "detail_cn": t, "raw": "%s|%s|%s" % (t, actor, target)},
                           ensure_ascii=False) + "\n")

conf = {"site_title": "测试标题", "dash_token": TOK, "require_token": True}

p = d.build_payload({"days": ["all"]}, conf)
check("总记录数", p["total_all"], 6)
check("站点标题透传", p["site_title"], "测试标题")

players = {x["name"]: x for x in p["players"]}
check("玩家列表不含选择器 @a", "@a" in players, False)
check("玩家列表含 console", "console" in players, True)
check("Alice 记录数", players["Alice"]["count"], 3)
check("Alice 作为执行者次数", players["Alice"]["as_actor"], 3)
check("Alice 取物次数", players["Alice"]["give"], 2)
check("Alice 模式切换次数", players["Alice"]["gamemode"], 1)
check("Bob 作为对象次数", players["Bob"]["as_target"], 1)
check("Carol 作为对象次数", players["Carol"]["as_target"], 2)
check("自己给自己不重复计数（Carol 2 事件）", players["Carol"]["count"], 2)
check("玩家按记录数降序", [x["name"] for x in p["players"]][0], "Alice")

p2 = d.build_payload({"days": ["all"], "player": ["Alice"]}, conf)
check("按玩家筛选条数", p2["total_filtered"], 3)
check("筛选结果带 _view 标记", sorted({e["_view"] for e in p2["events"]}), ["他执行"])

p3 = d.build_payload({"days": ["all"], "player": ["Bob"]}, conf)
check("Bob 视角同时含「他执行」与「作用对象」",
      sorted({e["_view"] for e in p3["events"]}), ["他执行", "作用对象"])
check("Bob 视角条数（他执行 1 + 作为对象 1）", p3["total_filtered"], 2)

p4 = d.build_payload({"days": ["all"], "types": ["give"]}, conf)
check("按类型筛选", p4["total_filtered"], 4)

p5 = d.build_payload({"days": ["all"], "q": ["钻石"]}, conf)
check("关键词搜索", p5["total_filtered"], 1)

p6 = d.build_payload({"days": ["1"]}, conf)
check("按天数筛选（最近 1 个有数据的日期）", p6["total_filtered"], 4)

p7 = d.build_payload({"days": ["all"]}, conf)
check("类型统计 give", p7["stats"].get("give"), 4)
check("高频物品含钻石", "minecraft:diamond" in dict(p7["top_items"]), True)

check("空目录不报错", d.build_payload({"days": ["all"]}, conf)["total_all"] >= 0, True)

# ============================================================
# 三、排行榜 / 百分位
# ============================================================
print("\n=== 排行榜与百分位 ===")
tmp2 = tempfile.mkdtemp(prefix="mcaudit-lb-")
atexit.register(shutil.rmtree, tmp2, True)
d.LOG_DIR = tmp2
d._cache["sig"] = None


def ev(date, t, actor, target="", killer=""):
    return {"ts": date + " 12:00:00", "date_bj": date, "type": t, "actor": actor,
            "target": target, "killer": killer, "item_id": "", "item_zh": "",
            "detail_cn": t, "raw": "%s|%s|%s" % (t, actor, target)}


LB = []
# 2026-09-20：Alice 6 管理 + 2 死亡，Bob 3 管理 + 5 死亡，Carol 各 1
#            Dave 只作为「对象」出现，没有任何自己的行为 → 两个榜都不该有他
LB += [ev("2026-09-20", "give", "Alice", "Alice")] * 5
LB += [ev("2026-09-20", "death", "Alice")] * 2
LB += [ev("2026-09-20", "gamemode", "Bob", "Bob")] * 3
LB += [ev("2026-09-20", "death", "Bob")] * 5
LB += [ev("2026-09-20", "give", "Carol"), ev("2026-09-20", "death", "Carol")]
LB += [ev("2026-09-20", "give", "console", "Dave")]      # 控制台不是玩家
LB += [ev("2026-09-20", "give", "Alice", "@a")]          # 选择器对象不是玩家；Alice 仍是执行者
# 2026-09-19：Eve 与 Frank 并列第一，Gina 垫底，且当天无人死亡
LB += [ev("2026-09-19", "give", "Eve")] * 4
LB += [ev("2026-09-19", "give", "Frank")] * 4
LB += [ev("2026-09-19", "give", "Gina")]

for _day in ("2026-09-19", "2026-09-20"):
    with open(os.path.join(tmp2, "events-%s.jsonl" % _day), "w", encoding="utf-8") as f:
        for _e in LB:
            if _e["date_bj"] == _day:
                f.write(json.dumps(_e, ensure_ascii=False) + "\n")

pl = d.build_payload({"days": ["all"]}, conf)
lb = pl["leaderboard"]
check("榜单可用日期", lb["days"], ["2026-09-19", "2026-09-20"])
check("默认取最新一天", lb["date"], "2026-09-20")
check("★ 管理榜最高分为 100", lb["admin"][0]["pct"], 100)
check("★ 100 对应金色", lb["admin"][0]["tier"], "gold")
check("管理榜完整结果（名次/次数/分值/色阶）",
      [(r["name"], r["value"], r["pct"], r["tier"]) for r in lb["admin"]],
      [("Alice", 6, 100, "gold"), ("Bob", 3, 99, "pink"), ("Carol", 1, 91, "orange")])
check("死亡榜第一名", lb["death"][0]["name"], "Bob")
check("★ 死亡榜最高分也是 100 金色", (lb["death"][0]["pct"], lb["death"][0]["tier"]), (100, "gold"))
check("死亡榜完整顺序", [(r["name"], r["value"]) for r in lb["death"]],
      [("Bob", 5), ("Alice", 2), ("Carol", 1)])
check("★ 控制台不进玩家榜", [r["name"] for r in lb["admin"]].count("console"), 0)
check("★ 只作为对象、自己没有行为的玩家不上榜", [r["name"] for r in lb["admin"]].count("Dave"), 0)
check("★ 同一个人也不会因「只作为对象」进榜（Dave）",
      [r["name"] for r in lb["death"]].count("Dave"), 0)
check("选择器 @a 不算玩家", "@a" in [r["name"] for r in lb["admin"]], False)
check("管理行为合计（不含控制台）", lb["admin_total"], 10)
check("死亡次数合计", lb["death_total"], 8)
check("名次连续", [r["rank"] for r in lb["admin"]], [1, 2, 3])

p19 = d.build_payload({"days": ["all"], "lb": ["2026-09-19"]}, conf)
lb19 = p19["leaderboard"]
check("指定日期生效", lb19["date"], "2026-09-19")
check("★ 并列第一同为 100 金色，第三名照常 99 粉（dense rank）",
      [(r["name"], r["pct"], r["tier"]) for r in lb19["admin"]],
      [("Eve", 100, "gold"), ("Frank", 100, "gold"), ("Gina", 99, "pink")])
check("当日无人死亡 → 死亡榜为空", lb19["death"], [])
check("不存在的日期回落到最新",
      d.build_payload({"days": ["all"], "lb": ["1999-01-01"]}, conf)["leaderboard"]["date"],
      "2026-09-20")

check("榜单不受页面筛选影响", len(d.build_payload(
    {"days": ["1"], "player": ["Bob"], "types": ["death"]}, conf)["leaderboard"]["admin"]), 3)

plx = {x["name"]: x for x in pl["players"]}
check("玩家板块新增死亡计数", plx["Bob"]["deaths"], 5)
check("★ 死亡不并入「作为执行者」", plx["Bob"]["as_actor"], 3)
check("死亡计入该玩家记录数", plx["Bob"]["count"], 8)
check("Alice 记录数（6 管理 + 2 死亡）", plx["Alice"]["count"], 8)
check("Alice 死亡计数", plx["Alice"]["deaths"], 2)
check("只作为对象的玩家仍在玩家板块（Dave）", plx["Dave"]["as_target"], 1)
check("Dave 自己没有任何行为", (plx["Dave"]["as_actor"], plx["Dave"]["deaths"]), (0, 0))
check("类型统计含 death", pl["stats"].get("death"), 8)

print("\n=== 色阶边界 ===")
TIER_CASES = [(100, "gold"), (99, "pink"), (98, "orange"), (95, "orange"), (91, "orange"),
              (90, "purple"), (76, "purple"), (75, "blue"), (60, "blue"), (51, "blue"),
              (50, "green"), (26, "green"), (25, "gray"), (10, "gray"), (1, "gray"), (0, "gray")]
for _v, _want in TIER_CASES:
    check("tier_of(%d)" % _v, d.tier_of(_v), _want)

# ============================================================
# v1.2.0：玩家行为统计聚合（破坏/放置/击杀）与 PVP 推导、四张新榜
# ============================================================
print("\n=== v1.2.0 行为统计与 PVP ===")
tmp3 = tempfile.mkdtemp(prefix="mcaudit-stats-")
atexit.register(shutil.rmtree, tmp3, True)
d.LOG_DIR = tmp3
d._cache["sig"] = None


def stat(date, actor, broken=0, placed=0, mob=0, pvp=0):
    return {"ts": date + " 12:00:00", "date_bj": date, "type": "stats", "actor": actor,
            "target": "", "broken": broken, "placed": placed,
            "mob_kills": mob, "pvp_kills": pvp, "count": None,
            "item_id": "", "item_zh": "", "detail_cn": "汇总", "raw": "stats|%s|%s" % (date, actor)}


def dth(date, actor, killer="", kp=0):
    return {"ts": date + " 12:00:00", "date_bj": date, "type": "death", "actor": actor,
            "target": "", "killer": killer, "kp": kp, "cause": "player",
            "item_id": "", "item_zh": "", "detail_cn": "死亡", "raw": "death|%s|%s|%s" % (date, actor, killer)}


SE = [
    # 2026-09-21：Alice 两次汇总 broken 10+5=15、placed 4+1=5、mob 6+2=8
    stat("2026-09-21", "Alice", broken=10, placed=4, mob=6),
    stat("2026-09-21", "Alice", broken=5, placed=1, mob=2),
    stat("2026-09-21", "Bob", broken=3, placed=8),
    dth("2026-09-21", "Carol", killer="Alice", kp=1),     # PVP：kp 标记
    dth("2026-09-21", "Carol", killer="Zombie", kp=0),    # 生物击杀者不算 PVP
    dth("2026-09-21", "Dave", killer="Bob", kp=0),        # 旧数据兜底：Bob 在已知玩家集合
    {"ts": "2026-09-21 12:00:00", "date_bj": "2026-09-21", "type": "give", "actor": "Alice",
     "target": "Alice", "item_id": "minecraft:diamond", "item_zh": "钻石", "count": 1,
     "detail_cn": "give", "raw": "give|0921|Alice"},
    # 2026-09-20：一天 give，保证榜单默认取最新日期
    {"ts": "2026-09-20 12:00:00", "date_bj": "2026-09-20", "type": "give", "actor": "Eve",
     "target": "Eve", "item_id": "", "item_zh": "", "count": 1, "detail_cn": "give", "raw": "give|0920|Eve"},
]
with open(os.path.join(tmp3, "events-2026-09-21.jsonl"), "w", encoding="utf-8") as f:
    for _e in SE:
        if _e["date_bj"] == "2026-09-21":
            f.write(json.dumps(_e, ensure_ascii=False) + "\n")
with open(os.path.join(tmp3, "events-2026-09-20.jsonl"), "w", encoding="utf-8") as f:
    for _e in SE:
        if _e["date_bj"] == "2026-09-20":
            f.write(json.dumps(_e, ensure_ascii=False) + "\n")

sp = d.build_payload({"days": ["all"]}, conf)
spx = {x["name"]: x for x in sp["players"]}
check("stats 数值跨多条汇总累加（broken）", spx["Alice"]["broken"], 15)
check("stats 数值累加（placed）", spx["Alice"]["placed"], 5)
check("stats 数值累加（mob_kills）", spx["Alice"]["mob_kills"], 8)
check("stats 汇总不并入 as_actor", spx["Alice"]["as_actor"], 1)      # 只有那条 give
check("stats 计入记录数（2 条汇总 + 1 give）", spx["Alice"]["count"], 3)
check("PVP 从死亡事件 kp 标记推导", spx["Alice"]["pvp_kills"], 1)
check("PVP 旧数据按已知玩家名兜底", spx["Bob"]["pvp_kills"], 1)
check("生物击杀者不计 PVP", "Zombie" in spx, False)
check("nums 破坏合计", sp["nums"]["broken"], 18)
check("nums 放置合计", sp["nums"]["placed"], 13)
check("nums 击杀生物合计", sp["nums"]["mob_kills"], 8)
check("nums PVP 合计", sp["nums"]["pvp_kills"], 2)
check("类型统计含 stats", sp["stats"].get("stats"), 3)

# killer_player / known_player_names 单元行为
kn = d.known_player_names(SE)
check("已知玩家名含 Alice", "Alice" in kn, True)
check("已知玩家名不含 console", "console" in kn, False)
check("killer_player: kp 标记优先", d.killer_player(dth("2026-09-21", "X", "Alice", kp=1), kn), "Alice")
check("killer_player: 生物名被拒", d.killer_player(dth("2026-09-21", "X", "Zombie", kp=0), kn), "")
check("killer_player: 兜底名命中集合", d.killer_player(dth("2026-09-21", "X", "Bob", kp=0), kn), "Bob")
check("killer_player: 空击杀者", d.killer_player(dth("2026-09-21", "X", ""), kn), "")

# 四张新榜（默认取最新有数据的日期 2026-09-21）
slb = sp["leaderboard"]
check("行为榜默认日期", slb["date"], "2026-09-21")
check("破坏榜第一", [(r["name"], r["value"]) for r in slb["broken"][:1]], [("Alice", 15)])
check("破坏榜百分位金色", slb["broken"][0]["tier"], "gold")
check("破坏榜第二名 Bob", [(r["name"], r["value"]) for r in slb["broken"][1:]], [("Bob", 3)])
check("放置榜第一", [(r["name"], r["value"]) for r in slb["placed"][:1]], [("Bob", 8)])
check("击杀生物榜第一", [(r["name"], r["value"]) for r in slb["kills"][:1]], [("Alice", 8)])
check("PVP 榜并列第一（Alice/Bob 各 1）",
      sorted([(r["name"], r["pct"], r["tier"]) for r in slb["pvp"]]),
      sorted([("Alice", 100, "gold"), ("Bob", 100, "gold")]))
check("PVP 榜合计", slb["pvp_total"], 2)
check("破坏榜合计", slb["broken_total"], 18)

# ── v1.2.1：力工天榜（破坏 + 放置之和）与凶手视角筛选 ──
check("力工天榜第一名（Alice 15+5）",
      [(r["name"], r["value"]) for r in slb["labor"][:1]], [("Alice", 20)])
check("力工天榜第二名（Bob 3+8）",
      [(r["name"], r["value"]) for r in slb["labor"][1:]], [("Bob", 11)])
check("力工天榜合计（18+13）", slb["labor_total"], 31)
check("力工天榜百分位金色", slb["labor"][0]["tier"], "gold")
check("旧日期力工天榜为空",
      d.build_payload({"days": ["all"], "lb": ["2026-09-20"]}, conf)["leaderboard"]["labor"], [])
# 凶手视角：查 Alice 时，她作为击杀者的死亡事件（Carol 之死）必须出现
pka = d.build_payload({"days": ["all"], "player": ["Alice"]}, conf)
carol_death = [e for e in pka["events"] if e.get("type") == "death"]
check("击杀记录：凶手视角能看到被杀事件", len(carol_death), 1)
check("击杀记录：标记为他是凶手", carol_death[0]["_view"], "他是凶手")
check("击杀记录：记录了谁杀谁",
      (carol_death[0]["actor"], carol_death[0]["killer"]), ("Carol", "Alice"))
check("击杀记录：带时间戳", bool(carol_death[0]["ts"]), True)
check("击杀记录：总条数含击杀事件（2 stats + 1 give + 1 凶手）", pka["total_filtered"], 4)
# 对照：受害者视角标记为「他死亡」（死亡不是他执行的行为）
pkc = d.build_payload({"days": ["all"], "player": ["Carol"]}, conf)
check("受害者视角标记为「他死亡」",
      sorted({e["_view"] for e in pkc["events"]}), ["他死亡"])

check("旧日期（09-20）行为榜为空",
      d.build_payload({"days": ["all"], "lb": ["2026-09-20"]}, conf)["leaderboard"]["broken"], [])
check("生物名不进 PVP 榜", "Zombie" in [r["name"] for r in slb["pvp"]], False)
check("类型中文名含 stats", d.TYPE_CN.get("stats"), "行为统计汇总")

# ============================================================
# v1.2.2：色阶最低分制（分值 = 所在色的最低分，优先铺满 7 色）
# ============================================================
print("\n=== v1.2.2 色阶最低分制 ===")
# 7 人互不相同 → 正好一人一色（100/99/91/76/51/26/1）
c7 = {"P1": 70, "P2": 60, "P3": 50, "P4": 40, "P5": 30, "P6": 20, "P7": 10}
rows7 = d.percentile_rows(c7)
check("★ 7 人互不相同 → 一人一色（分值/色阶）",
      [(r["pct"], r["tier"]) for r in rows7],
      [(100, "gold"), (99, "pink"), (91, "orange"), (76, "purple"),
       (51, "blue"), (26, "green"), (1, "gray")])
check("★ 7 人名次 1-7", [r["rank"] for r in rows7], [1, 2, 3, 4, 5, 6, 7])
# 第 8 个不同名次起 → 灰色 1 分
c8 = dict(c7)
c8["P8"] = 5
rows8 = d.percentile_rows(c8)
check("★ 第 8 名起为灰色 1 分", (rows8[7]["pct"], rows8[7]["tier"]), (1, "gray"))
# 并列：同名次同分同色，下一名照常推进（dense rank，第二高值永远 99）
ct = {"A": 50, "B": 50, "C": 40}
rowst = d.percentile_rows(ct)
check("★ 并列第一同 100 金",
      [(r["name"], r["rank"], r["pct"], r["tier"]) for r in rowst],
      [("A", 1, 100, "gold"), ("B", 1, 100, "gold"), ("C", 2, 99, "pink")])
# 少于 7 人：从金色起依次使用
c2 = {"X": 9, "Y": 3}
rows2 = d.percentile_rows(c2)
check("2 人 → 金 + 粉", [(r["pct"], r["tier"]) for r in rows2],
      [(100, "gold"), (99, "pink")])
check("1 人 → 金", [(r["pct"], r["tier"]) for r in d.percentile_rows({"Solo": 1})],
      [(100, "gold")])
check("空榜", d.percentile_rows({}), [])
check("零值不进榜", d.percentile_rows({"A": 0, "B": 2}),
      [{"name": "B", "value": 2, "pct": 100, "tier": "gold", "rank": 1}])
check("RANK_PCT 表与 tier_of 自洽",
      [d.tier_of(p) for p in d.RANK_PCT + [1]],
      ["gold", "pink", "orange", "purple", "blue", "green", "gray"])

print("\n通过 %d/%d" % (ok, total))
sys.exit(0 if ok == total else 1)
