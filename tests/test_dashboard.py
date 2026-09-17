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

print("\n通过 %d/%d" % (ok, total))
sys.exit(0 if ok == total else 1)
