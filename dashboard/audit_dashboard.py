#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft 管理员行为审计 · Web 看板

只读展示 <安装目录>/log/ 下的采集结果。
纯 Python 标准库实现，无任何第三方依赖。

两个视图：
  · 时间轴   —— 按日期分组的事件流
  · 玩家板块 —— 左侧玩家列表，点选后查看该玩家的全部行为与概览
"""
import glob
import ipaddress
import json
import os
import secrets
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

BASE = os.environ.get("MCAUDIT_HOME") or os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "log")
CONF_FILE = os.path.join(BASE, "config.json")
HTML_FILE = os.path.join(BASE, "dashboard.html")
TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_TITLE = "Minecraft 管理员行为审计"

TYPE_CN = {
    "give": "管理员取物", "give_failed": "取物失败", "item_set": "管理员设物",
    "loot": "管理员掉落物", "gamemode": "切换游戏模式", "gamemode_other": "切换他人模式",
    "enchant": "管理员附魔", "effect": "管理员给药水", "xp": "管理员给经验",
    "op": "权限变更", "summon": "管理员刷实体", "clear": "清空玩家物品",
    "death": "玩家死亡", "stats": "行为统计汇总", "other": "其它命令", "raw_cmd": "命令原文",
}

# ---------------- 排行榜 / 百分位（参考 FF14 Logs 的配色分档） ----------------
# 百分位 = 「你胜过或战平的人」占当天上榜玩家的比例（四舍五入）。
# 因此当天数值最高的人必定拿到 100，并列者同色。
TIER_TABLE = [
    (100, 100, "gold"),      # 金色
    (99,   99, "pink"),      # 粉色
    (91,   98, "orange"),    # 橙色
    (76,   90, "purple"),    # 紫色
    (51,   75, "blue"),      # 蓝色
    (26,   50, "green"),     # 绿色
    (1,    25, "gray"),      # 灰色
]


def tier_of(pct):
    """百分位 → 色阶 key"""
    for lo, hi, key in TIER_TABLE:
        if lo <= pct <= hi:
            return key
    return "gray"


def clean_name(name):
    """过滤掉选择器、坐标、实体计数这类会被误当成玩家的字符串"""
    n = (name or "").strip()
    if (not n or n.startswith("@") or n == "unknown-player"
            or n.endswith(" players") or n.startswith("坐标") or n.endswith("entities")):
        return ""
    return n


# 排行榜只排玩家：控制台 / 未知来源不是人，不该跟玩家比高低
NON_PLAYERS = {"console", "unknown-player", "server", "Server", "CONSOLE"}


def known_player_names(evs):
    """全量事件里出现过的玩家名集合（执行者 + 对象）。
    用于旧数据（死亡事件无 kp 标记）判断击杀者是不是玩家的兑底。"""
    known = set()
    for e in evs:
        for n in (clean_name(e.get("actor", "")), clean_name(e.get("target", ""))):
            if n and n not in NON_PLAYERS:
                known.add(n)
    return known


def killer_player(e, known):
    """死亡事件里「击杀者是玩家」时返回击杀者名，否则返回 ""。
    优先用采集端的 kp 标记（可靠）；旧数据无标记时兑底：击杀者名出现在
    已知玩家名集合中才算（避免 Zombie 这类生物名混进 PVP 榜）。"""
    k = clean_name(e.get("killer", ""))
    if not k or k in NON_PLAYERS:
        return ""
    if e.get("kp") == 1 or k in known:
        return k
    return ""


def percentile_rows(counter):
    """{玩家: 次数} → 带名次与百分位的排行行（只列次数 > 0 的玩家）"""
    vals = [v for v in counter.values() if v > 0]
    n = len(vals)
    if not n:
        return []
    rows = []
    for name, v in counter.items():
        if v <= 0:
            continue
        le = sum(1 for x in vals if x <= v)          # 胜过或战平的人数
        pct = max(1, min(100, round(100.0 * le / n)))
        rows.append({"name": name, "value": v, "pct": pct, "tier": tier_of(pct)})
    rows.sort(key=lambda r: (-r["value"], r["name"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def build_leaderboard(evs, day=""):
    """按「天」出榜：管理行为次数榜 + 死亡次数榜，以及四张行为统计榜
    （破坏方块 / 放置方块 / 击杀生物 / PVP 击杀）。

    刻意不受页面上的 天数/玩家/类型 筛选影响 —— 榜单要能横着比。
    行为统计榜的数值 = 当天该玩家所有 stats 汇总事件的数值之和；
    PVP 榜 = 当天死亡事件中击杀者为玩家的次数（不与 stats 双计）。
    """
    days = sorted({e.get("date_bj", "") for e in evs if e.get("date_bj")})
    date = day if day in days else (days[-1] if days else "")
    known = known_player_names(evs)
    admin, death = {}, {}
    broken, placed, kills, pvp = {}, {}, {}, {}
    for e in evs:
        if e.get("date_bj") != date:
            continue
        t = e.get("type")
        if t == "stats":
            actor = clean_name(e.get("actor", ""))
            if not actor or actor in NON_PLAYERS:
                continue
            for src, dst in (("broken", broken), ("placed", placed), ("mob_kills", kills)):
                v = e.get(src) or 0
                if v > 0:
                    dst[actor] = dst.get(actor, 0) + v
            continue
        if t == "death":
            actor = clean_name(e.get("actor", ""))
            if actor and actor not in NON_PLAYERS:
                death[actor] = death.get(actor, 0) + 1
            kk = killer_player(e, known)
            if kk:
                pvp[kk] = pvp.get(kk, 0) + 1
            continue
        actor = clean_name(e.get("actor", ""))
        if not actor or actor in NON_PLAYERS:
            continue
        admin[actor] = admin.get(actor, 0) + 1
    a_rows, d_rows = percentile_rows(admin), percentile_rows(death)
    b_rows, p_rows = percentile_rows(broken), percentile_rows(placed)
    k_rows, v_rows = percentile_rows(kills), percentile_rows(pvp)
    return {
        "date": date,
        "days": days,
        "admin": a_rows,
        "death": d_rows,
        "broken": b_rows,
        "placed": p_rows,
        "kills": k_rows,
        "pvp": v_rows,
        "admin_total": sum(admin.values()),
        "death_total": sum(death.values()),
        "broken_total": sum(broken.values()),
        "placed_total": sum(placed.values()),
        "kills_total": sum(kills.values()),
        "pvp_total": sum(pvp.values()),
        "tiers": [{"lo": lo, "hi": hi, "key": k} for lo, hi, k in TIER_TABLE],
    }

# 与采集器保持一致的台账表头（台账为空时用于下载占位）
CSV_HEADER = ["时间(北京)", "日期", "事件类型", "执行者", "执行者模式", "对象玩家",
              "物品ID", "物品中文名", "物品英文名", "数量", "详情", "命令原文", "原始日志"]


def load_conf():
    c = {}
    if os.path.exists(CONF_FILE):
        try:
            # utf-8-sig：兼容带 BOM 的配置文件（Windows 记事本/PowerShell 5.1 默认写 BOM）
            c = json.load(open(CONF_FILE, encoding="utf-8-sig"))
        except (OSError, ValueError):
            c = {}
    c.setdefault("dash_host", "0.0.0.0")
    c.setdefault("dash_port", 25567)
    c.setdefault("dash_ports", [])           # 可同时监听多个端口；为空则用 dash_port
    c.setdefault("require_token", True)
    c.setdefault("lan_no_token", True)       # 局域网/本机免令牌
    c.setdefault("cookie_days", 365)
    c.setdefault("site_title", DEFAULT_TITLE)
    if c["require_token"] and not c.get("dash_token"):
        c["dash_token"] = secrets.token_urlsafe(12)
        try:
            json.dump(c, open(CONF_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except OSError:
            pass
    return c


def peer_ip(addr):
    """取对端 IP；统一去掉 IPv6 映射前缀 / zone id"""
    ip = (addr or "").split("%")[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def is_lan(ip):
    """内网或本机地址"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback


_cache = {"sig": None, "events": []}


def load_events():
    files = sorted(glob.glob(os.path.join(LOG_DIR, "events-*.jsonl")))
    sig = tuple((os.path.basename(p), os.path.getsize(p)) for p in files)
    if _cache["sig"] == sig:
        return _cache["events"]
    evs = []
    for p in files:
        try:
            # utf-8-sig：兼容带 BOM 的台账文件（Windows 工具人工编辑后可能引入）
            with open(p, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evs.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    evs.sort(key=lambda e: e.get("ts", ""), reverse=True)
    _cache["sig"] = sig
    _cache["events"] = evs
    return evs


def build_payload(q, conf):
    evs = load_events()
    days = q.get("days", ["7"])[0]
    player = (q.get("player", [""])[0] or "").strip()
    types = [t for t in (q.get("types", [""])[0] or "").split(",") if t]
    kw = (q.get("q", [""])[0] or "").strip().lower()
    try:
        limit = int(q.get("limit", ["800"])[0] or 800)
    except ValueError:
        limit = 800

    # 玩家聚合基于全量数据，不受筛选影响，便于对照
    known = known_player_names(evs)
    all_players = {}
    for e in evs:
        t = e.get("type")
        actor = clean_name(e.get("actor", ""))
        target = clean_name(e.get("target", ""))
        # 同一条事件中「自己给自己」（如 /give Alice ... @s）：
        # 这个人既算执行者也算对象，但事件数 count 只能记一次，否则会翻倍
        involved = [n for n in dict.fromkeys([actor, target]) if n]
        if not involved and not (t == "death" and killer_player(e, known)):
            continue
        for name in involved:
            d = all_players.setdefault(name, {"name": name, "count": 0, "give": 0, "gamemode": 0,
                                              "deaths": 0, "last": "",
                                              "as_actor": 0, "as_target": 0,
                                              "broken": 0, "placed": 0,
                                              "mob_kills": 0, "pvp_kills": 0})
            d["count"] += 1
            if name == actor:
                if t == "death":
                    d["deaths"] += 1          # 死亡单独计数，不并入「作为执行者」
                elif t == "stats":
                    pass                      # 行为统计汇总也不算管理行为，只累加数值
                else:
                    d["as_actor"] += 1
            if name == target:
                d["as_target"] += 1
            if t == "give":
                d["give"] += 1
            if t in ("gamemode", "gamemode_other"):
                d["gamemode"] += 1
            if t == "stats":
                d["broken"] += e.get("broken") or 0
                d["placed"] += e.get("placed") or 0
                d["mob_kills"] += e.get("mob_kills") or 0
            if e.get("ts", "") > d["last"]:
                d["last"] = e.get("ts", "")
        # PVP 击杀数：从死亡事件的击杀者推导（不与 stats 的 pvp_kills 双计）。
        # 只杀人、无任何其他记录的玩家也要有个条目，否则玩家板块看不到他。
        if t == "death":
            kk = killer_player(e, known)
            if kk:
                kd = all_players.setdefault(kk, {"name": kk, "count": 0, "give": 0, "gamemode": 0,
                                                  "deaths": 0, "last": "",
                                                  "as_actor": 0, "as_target": 0,
                                                  "broken": 0, "placed": 0,
                                                  "mob_kills": 0, "pvp_kills": 0})
                kd["pvp_kills"] += 1

    sel = evs
    if player:
        sel = [e for e in sel if e.get("actor") == player or e.get("target") == player]
    if types:
        sel = [e for e in sel if e.get("type") in types]
    if days and days != "all":
        try:
            n = int(days)
            dates = sorted({e.get("date_bj", "") for e in evs if e.get("date_bj")})[-n:]
            keep = set(dates)
            sel = [e for e in sel if e.get("date_bj") in keep]
        except ValueError:
            pass
    if kw:
        sel = [e for e in sel if kw in json.dumps(e, ensure_ascii=False).lower()]

    stats = {}
    items = {}
    nums = {"broken": 0, "placed": 0, "mob_kills": 0, "pvp_kills": 0}
    for e in sel:
        stats[e.get("type")] = stats.get(e.get("type"), 0) + 1
        key = e.get("item_id") or e.get("item_en") or e.get("item_raw") or ""
        if key and e.get("type") in ("give", "item_set"):
            items[key] = items.get(key, 0) + 1
        if e.get("type") == "stats":
            for f in ("broken", "placed", "mob_kills"):
                nums[f] += e.get(f) or 0
        elif e.get("type") == "death" and killer_player(e, known):
            nums["pvp_kills"] += 1

    shown = sel[:limit]
    if player:
        for e in shown:
            e["_view"] = "他执行" if e.get("actor") == player else "作用对象"
    return {
        "site_title": conf.get("site_title") or DEFAULT_TITLE,
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "total_all": len(evs),
        "total_filtered": len(sel),
        "shown": len(shown),
        "events": shown,
        "players": sorted(all_players.values(), key=lambda x: (-x["count"], x["name"])),
        "leaderboard": build_leaderboard(evs, (q.get("lb", [""])[0] or "").strip()),
        "stats": stats,
        "nums": nums,
        "top_items": sorted(items.items(), key=lambda x: -x[1])[:20],
        "type_cn": TYPE_CN,
    }


class Handler(BaseHTTPRequestHandler):
    conf = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._ck_header()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._ck_header()
        self.end_headers()
        self.wfile.write(body)

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _ck_header(self):
        """本次请求带了有效令牌 -> 下发长效 Cookie，之后无需再带 ?k="""
        if getattr(self, "_issue_ck", False):
            days = int(self.conf.get("cookie_days", 365) or 365)
            self.send_header("Set-Cookie",
                             "mcaudit_k=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                             % (self.conf.get("dash_token", ""), days * 86400))

    def _authed(self, q, headers):
        if not self.conf.get("require_token"):
            return True
        tok = (q.get("k", [""])[0] or headers.get("X-Auth-Token", "")
               or self._cookies().get("mcaudit_k", ""))
        if tok and tok == self.conf.get("dash_token"):
            if q.get("k", [""])[0] or headers.get("X-Auth-Token", ""):
                self._issue_ck = True
            return True
        if self.conf.get("lan_no_token", True) and is_lan(peer_ip(self.client_address[0])):
            return True
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                if not self._authed(q, self.headers):
                    return self._html("<h2 style='font-family:sans-serif'>401 需要访问令牌</h2>"
                                      "<p style='font-family:sans-serif'>请在网址后加 "
                                      "<code>?k=令牌</code> 访问；若无令牌请查看服务器上的 "
                                      "<code>config.json</code>。</p>", 401)
                try:
                    return self._html(open(HTML_FILE, encoding="utf-8").read())
                except OSError:
                    return self._html("<h2>dashboard.html 缺失</h2>", 500)
            if u.path == "/api/data":
                if not self._authed(q, self.headers):
                    return self._json({"error": "unauthorized"}, 401)
                return self._json(build_payload(q, self.conf))
            if u.path == "/api/health":
                return self._json({"ok": True,
                                   "files": len(glob.glob(os.path.join(LOG_DIR, "events-*.jsonl")))})
            if u.path == "/download/events.csv":
                if not self._authed(q, self.headers):
                    return self._json({"error": "unauthorized"}, 401)
                p = os.path.join(LOG_DIR, "events.csv")
                if not os.path.exists(p):
                    # 台账还没有数据：返回只有表头的空表，避免 404 让人误以为出错
                    body = ("\ufeff" + ",".join(CSV_HEADER) + "\r\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="events.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self._ck_header()
                    self.end_headers()
                    return self.wfile.write(body)
                return self._file(p, "text/csv")
            if u.path.startswith("/download/"):
                if not self._authed(q, self.headers):
                    return self._json({"error": "unauthorized"}, 401)
                name = os.path.basename(u.path[len("/download/"):])
                if not (name.startswith("events-") and name.endswith(".jsonl")):
                    return self._json({"error": "bad name"}, 400)
                return self._file(os.path.join(LOG_DIR, name), "application/x-ndjson")
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)

    def _file(self, path, ctype):
        if not os.path.exists(path):
            return self._json({"error": "file not found"}, 404)
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(path))
        self.send_header("Content-Length", str(len(data)))
        self._ck_header()
        self.end_headers()
        self.wfile.write(data)


def main():
    conf = load_conf()
    Handler.conf = conf
    host = conf["dash_host"]
    os.makedirs(LOG_DIR, exist_ok=True)
    ports = [int(p) for p in (conf.get("dash_ports") or [])] or [int(conf["dash_port"])]
    servers = []
    for p in ports:
        try:
            srv = ThreadingHTTPServer((host, p), Handler)
        except OSError as e:
            print("[dashboard] 端口 %d 绑定失败：%s" % (p, e), flush=True)
            continue
        servers.append((srv, p))
        print("[dashboard] 监听 %s:%d" % (host, p), flush=True)
    if not servers:
        raise SystemExit("[dashboard] 没有任何端口绑定成功，退出")
    for srv, _p in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("[dashboard] 监听端口: %s" % ", ".join(str(_p) for _s, _p in servers), flush=True)
    print("[dashboard] 访问 http://%s:%d/" % ("<服务器IP>" if host == "0.0.0.0" else host,
                                             servers[0][1]), flush=True)
    if conf.get("require_token"):
        print("[dashboard] 外网需带令牌: /?k=%s" % conf["dash_token"], flush=True)
    else:
        print("[dashboard] 已关闭令牌校验（局域网免令牌=%s）" % conf.get("lan_no_token"), flush=True)
    servers[0][0].serve_forever()


if __name__ == "__main__":
    main()
