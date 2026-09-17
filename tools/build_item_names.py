#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
物品名称映射表构建工具

从服务端 jar / 模组 jar / 客户端 jar / KubeJS 资源里提取
  assets/<namespace>/lang/en_us.json
  assets/<namespace>/lang/zh_cn.json
把语言文件条目转换成  物品 id → 中文名 / 英文名  的映射表，
供采集器把日志里的 minecraft:diamond 显示成「钻石 (Diamond)」。

用法示例
--------
# 最省事：直接指向服务端目录，自动找 mods/ 与顶层 jar
python3 tools/build_item_names.py --server-dir /opt/minecraft/server

# 显式指定多个来源
python3 tools/build_item_names.py \
    --mods /opt/minecraft/server/mods \
    --jar  /opt/minecraft/server/forge-1.20.1-47.4.16-universal.jar \
    --jar  ~/.minecraft/versions/1.20.1/1.20.1.jar \
    --out  item_names.json

输出为 JSON：
    {"meta":{...}, "id2en":{...}, "id2zh":{...}, "by_en":{...}, "by_zh":{...}}
"""
import argparse
import json
import os
import re
import sys
import zipfile

# 语言文件内的键：item.<ns>.<path> / block.<ns>.<path>
KEY_PREFIX = ("item.", "block.")
LANG_RE = re.compile(r"^assets/([^/]+)/lang/(en_us|zh_cn)\.json$", re.I)
# 合法 id 的 path 段（用于剔除语言文件里的「子键」，例如
# xxx.smithing_template.diamond.ingredients 这种并非真实物品的键）
PATH_OK = re.compile(r"^[a-z0-9_\-/]+$")


def log(*a):
    print(*a, flush=True)


def id_from_key(key):
    """item.minecraft.diamond -> minecraft:diamond；不合法返回 None"""
    if not key.startswith(KEY_PREFIX):
        return None
    tail = key.split(".", 1)[1]          # minecraft.diamond
    if "." not in tail:
        return None
    return tail.replace(".", ":", 1)


def valid_id(iid):
    if ":" not in iid:
        return False
    ns, path = iid.split(":", 1)
    return bool(ns) and bool(PATH_OK.match(path))


def take_lang(data, tgt):
    """把一份语言文件的条目并入 tgt（不覆盖已有）"""
    n = 0
    for k, v in data.items():
        if not isinstance(v, str):
            continue
        iid = id_from_key(k)
        if not iid or not valid_id(iid):
            continue
        if iid not in tgt:
            tgt[iid] = v
            n += 1
    return n


def scan_jar(path, en, zh):
    """扫一个 jar，返回 (en 新增, zh 新增)"""
    a = b = 0
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                m = LANG_RE.match(n)
                if not m:
                    continue
                try:
                    data = json.loads(z.read(n).decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if m.group(2).lower() == "zh_cn":
                    b += take_lang(data, zh)
                else:
                    a += take_lang(data, en)
    except (zipfile.BadZipFile, OSError) as e:
        log("  ! 跳过 %s：%s" % (os.path.basename(path), e))
    return a, b


def scan_minecraft_dir(mc, en, zh):
    """从官方启动器的资源目录（.minecraft/assets）提取原版语言文件。

    原版中文名只存在于客户端资源里 —— 服务端 jar 只带 en_us，
    所以想让 minecraft:diamond 显示成「钻石」，需要指向客户端 .minecraft。
    资源文件按 assets/indexes/*.json 里的 sha1 存放在 assets/objects/<前两位>/<sha1>。
    """
    idx_dir = os.path.join(mc, "assets", "indexes")
    if not os.path.isdir(idx_dir):
        return 0, 0
    obj_root = os.path.join(mc, "assets", "objects")
    a = b = 0
    for f in sorted(os.listdir(idx_dir)):
        if not f.endswith(".json"):
            continue
        try:
            idx = json.load(open(os.path.join(idx_dir, f), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        objs = idx.get("objects") or {}
        if not isinstance(objs, dict):
            continue
        for loc, tgt in (("en_us", en), ("zh_cn", zh)):
            info = objs.get("minecraft/lang/%s.json" % loc)
            if not isinstance(info, dict):
                continue
            h = info.get("hash", "")
            if len(h) < 2:
                continue
            p = os.path.join(obj_root, h[:2], h)
            if not os.path.exists(p):
                continue
            try:
                data = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                n = take_lang(data, tgt)
                if loc == "zh_cn":
                    b += n
                else:
                    a += n
    if a or b:
        log("  官方资源目录 %s  en+%d zh+%d" % (mc, a, b))
    return a, b


def scan_lang_dir(root, en, zh):
    """扫 KubeJS 等资源目录下的 assets/**/lang/*.json"""
    a = b = 0
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            low = f.lower()
            if low not in ("en_us.json", "zh_cn.json"):
                continue
            try:
                data = json.load(open(os.path.join(dp, f), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                if low.startswith("zh"):
                    b += take_lang(data, zh)
                else:
                    a += take_lang(data, en)
    return a, b


def collect_jars(args):
    """汇总所有要扫描的 jar"""
    jars = []
    for j in args.jar or []:
        if os.path.isfile(j):
            jars.append(j)
        else:
            log("  ! --jar 路径不存在：%s" % j)
    for m in args.mods or []:
        if not os.path.isdir(m):
            log("  ! --mods 目录不存在：%s" % m)
            continue
        jars += [os.path.join(m, f) for f in sorted(os.listdir(m)) if f.endswith(".jar")]
    for d in args.server_dir or []:
        if not os.path.isdir(d):
            log("  ! --server-dir 不存在：%s" % d)
            continue
        # 1) mods/ 下的模组 jar
        mdir = os.path.join(d, "mods")
        if os.path.isdir(mdir):
            jars += [os.path.join(mdir, f) for f in sorted(os.listdir(mdir)) if f.endswith(".jar")]
        # 2) 服务端顶层 jar（Forge / 整合包启动 jar）
        for f in sorted(os.listdir(d)):
            if f.endswith(".jar"):
                jars.append(os.path.join(d, f))
        # 3) libraries/ 递归：Forge 安装器下载的原版资源在这里，
        #    形如 libraries/net/minecraft/server/<版本>/server-<版本>-extra.jar，
        #    它是「原版物品中英文名」的唯一来源，漏掉它会导致 minecraft:xxx 全部没有翻译名
        for sub in ("libraries",):
            base = os.path.join(d, sub)
            if not os.path.isdir(base):
                continue
            for dp, dn, fn in os.walk(base):
                dn[:] = [x for x in dn if x not in ("logs", "world")]
                for f in fn:
                    if f.endswith(".jar"):
                        jars.append(os.path.join(dp, f))
    # 去重（按真实路径）
    seen, out = set(), []
    for j in jars:
        rp = os.path.realpath(j)
        if rp not in seen:
            seen.add(rp)
            out.append(j)
    return out


def main():
    ap = argparse.ArgumentParser(description="构建物品名称映射表 item_names.json")
    ap.add_argument("--server-dir", action="append", default=[], metavar="DIR",
                    help="Minecraft 服务端目录（自动扫描 mods/ 与顶层 jar），可重复")
    ap.add_argument("--mods", action="append", default=[], metavar="DIR",
                    help="模组目录，可重复")
    ap.add_argument("--jar", action="append", default=[], metavar="FILE",
                    help="额外扫描的 jar（如客户端 jar、Forge universal jar），可重复")
    ap.add_argument("--lang-dir", action="append", default=[], metavar="DIR",
                    help="散装语言文件目录（如 <服务端>/kubejs/assets），可重复")
    ap.add_argument("--minecraft-dir", action="append", default=[], metavar="DIR",
                    help="官方启动器 .minecraft 目录 —— 补齐「原版物品的中文名」，可重复")
    ap.add_argument("--out", default="item_names.json", metavar="FILE",
                    help="输出文件，默认 ./item_names.json")
    args = ap.parse_args()

    if not (args.server_dir or args.mods or args.jar or args.lang_dir or args.minecraft_dir):
        ap.error("至少要指定 --server-dir / --mods / --jar / --lang-dir / --minecraft-dir 之一")

    en, zh = {}, {}
    jars = collect_jars(args)
    log("扫描 %d 个 jar ..." % len(jars))
    for j in jars:
        a, b = scan_jar(j, en, zh)
        if a or b:
            log("  %-52s en+%d zh+%d" % (os.path.basename(j)[:52], a, b))

    lang_roots = list(args.lang_dir)
    for d in args.server_dir or []:
        p = os.path.join(d, "kubejs", "assets")
        if os.path.isdir(p):
            lang_roots.append(p)
    for r in lang_roots:
        a, b = scan_lang_dir(r, en, zh)
        if a or b:
            log("  资源目录 %s  en+%d zh+%d" % (r, a, b))

    for mc in args.minecraft_dir or []:
        scan_minecraft_dir(mc, en, zh)

    log("提取完成：en=%d  zh=%d" % (len(en), len(zh)))

    by_en, by_zh = {}, {}
    for iid, name in en.items():
        by_en.setdefault(name, []).append(iid)
    for iid, name in zh.items():
        by_zh.setdefault(name, []).append(iid)

    out = {
        "meta": {
            "generator": "tools/build_item_names.py",
            "sources": (args.server_dir or []) + (args.mods or []) + (args.jar or []),
            "jar_count": len(jars),
            "counts": {"id2en": len(en), "id2zh": len(zh),
                       "uniq_en": len(by_en), "uniq_zh": len(by_zh)},
        },
        "id2en": en, "id2zh": zh, "by_en": by_en, "by_zh": by_zh,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log("写出 %s（%d KB）" % (args.out, os.path.getsize(args.out) // 1024))

    if not en and not zh:
        log("警告：一个名称都没提到。若服务端语言文件在其他位置，"
            "可加 --jar <客户端 jar 路径> 补充。")
        return 1
    for probe in ("minecraft:diamond", "minecraft:netherite_ingot", "minecraft:stone"):
        log("  %-32s en=%-22r zh=%r" % (probe, en.get(probe), zh.get(probe)))

    # 诊断：原版物品的中文名只在客户端资源里，服务端 jar 没有
    mc_en = [k for k in en if k.startswith("minecraft:")]
    mc_zh = [k for k in zh if k.startswith("minecraft:")]
    if len(mc_en) > 100 and len(mc_zh) < len(mc_en) // 2:
        log("")
        log("提示：原版物品的英文名有 %d 条，中文名只有 %d 条。" % (len(mc_en), len(mc_zh)))
        log("      服务端自带资源里没有原版中文语言文件，属正常现象。")
        log("      若需要中文名，加一个参数指向你的客户端资源目录即可：")
        log("        --minecraft-dir \"<客户端 .minecraft 目录>\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
