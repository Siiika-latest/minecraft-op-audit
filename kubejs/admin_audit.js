// ============================================================
//  Minecraft 管理员行为审计 · 采集端（KubeJS）v1.2.0
//
//  作用：把每一件需要审计的事写成一行日志，交给同项目的 Python 采集器解析：
//
//    [MCAUDIT] {"ev":"cmd",     "ms":"...","actor":"...","cmd":"...","gm":"..."}
//    [MCAUDIT] {"ev":"death",   "ms":"...","actor":"...","cause":"fall",
//                              "killer":"...","kp":1,"msg":"...","gm":"..."}
//    [MCAUDIT] {"ev":"gmpatrol","ms":"...","actor":"...","gm":"creative"}
//    [MCAUDIT] {"ev":"stats",   "ms":"...","actor":"...","broken":12,"placed":3,
//                              "mob":7,"pvp":1,"span":300}
//
//  · ev=cmd      —— 有人执行了命令（含控制台 / 命令方块）
//  · ev=death    —— 玩家死亡（cause 为死因 id，killer 为击杀者，kp=1 表示击杀者是玩家，
//                   msg 为游戏内死讯原文）
//  · ev=gmpatrol —— 巡检发现某玩家的游戏模式发生了变化（v1.2.0 兜底，见下）
//  · ev=stats    —— 玩家行为统计的周期性汇总（破坏/放置方块数、击杀生物/玩家数）
//
//  采集器把「没有 ev 字段」的行也按 cmd 处理，因此旧版本留下的日志仍可解析。
//
//  性质：纯旁路只读监听。不改变任何游戏行为、不授予任何权限、不写存档。
//        计数器只存在于脚本内存中，KubeJS reload / 重启服务端即清零（已入库的
//        统计不受影响）。删除本文件（或 kubejs reload server_scripts）即刻失效。
//
//  放置位置：<服务端目录>/kubejs/server_scripts/admin_audit.js
//
//  ── KubeJS 兼容性实测（Rhino 引擎，勿随意改写取值方式）─────────
//   X  java() 全局已被移除。调用会直接抛错，且该错误会逃逸 try/catch
//      → 取时间必须用 JS 原生 new Date()，不要用
//        java.lang.System.currentTimeMillis()
//   X  player.getScoreboardName() 在脚本中不可见。用它取玩家名会抛错，
//      抛错被兜底吞掉后，玩家行为会被静默误标成 console（功能看似正常，实际全错）
//   X  取玩家名不要用 const / var server 之类的常见名字：Rhino 会因重声明或
//      与 KubeJS 全局同名而报错，统一用带前缀的局部变量
//   V  event.getInput()                          → 命令原文
//   V  event.getParseResults().getContext().getSource().getPlayer()
//   V  player.getGameProfile().getName()
//   V  player.getName().getString()
//   V  player.username
//   V  player.isCreative()（isSpectator/isAdventure 同族方法，均以 try 链保护）
//   V  EntityEvents.death 的 event.getPlayer()（实体不是玩家时返回 null）
//   X  event.getSource().getMsgId()  —— 方法**不存在**（TypeError）。实测报错：
//        "Cannot find function getMsgId in object DamageSource (lava)"
//   X  event.getSource().getEntity() / .getDirectEntity() —— 同样不存在
//   X  event.getSource().getLocalizedDeathMessage() —— 报 InternalError:
//        Can't find method ...DamageSource.m_6157_()（SRG 映射缺失）
//   V  event.getSource().type().msgId()          → 'lava' / 'player_attack' / 'arrow' …
//   V  String(event.getSource())                 → 'DamageSource (lava)'（同上信息，兜底用）
//   V  event.getSource().getActual()             → 击杀者实体（箭 → 射箭的人）
//   V  event.getSource().getImmediate()          → 直接伤害源（兜底）
//   V  entity.getCombatTracker().getDeathMessage().getString() → 'X was shot by Y'
//   V  BlockEvents.broken / placed 的 event.player（可能为 null，必须判空）
//   X  PlayerEvents.logged_in / logged_out —— KubeJS 2001.6.5-build.16 实测报
//      "Unknown event"（typeof 探测返回动态存根 function，调用时才报 Unknown），
//      注册无效，相关处理器已移除（见下方「上线 / 下线」注释）
//   V  ServerEvents.tick(event)（每 game tick 触发；节流用自己的 JS 计数器，
//      不要调用 server.getTickCount() 之类 Java 方法——SRG 映射不一定可用）
//   ?  server.players / server.getPlayers() —— 用「属性 → 方法」双重 try 兑底，
//      全部失败则本轮巡检静默跳过，不影响其他功能
//
//  ★★ 语法红线：事件注册语句一律以分号结尾，独立 IIFE 一律以防御性分号
//      开头（;(function）。否则 JS 的 ASI 会把下一行的 ( 拼进调用链，
//      把注册函数的返回值 null 当函数调用 → "TypeError: null is not a
//      function, it is object"，整个脚本加载失败且报错行号指向脚本末行，
//      极难定位（v1.2.0 真机踩过）。
//
//  兼容性：KubeJS 6（Minecraft 1.20.1 / Forge）实测通过。
//          KubeJS 7（1.21+ / NeoForge）未实测，若 API 变动请对照上方注释调整。
//
//  ── v1.2.0 为什么需要「模式巡检」────────────────────────────
//  Forge 1.20.1 **没有**「玩家游戏模式变化」事件，KubeJS 6 也没有。命令事件只能
//  捕获走命令系统的 /gamemode；而整合包 GUI 一键切换、mod 直接调用
//  player.setGameMode() 等途径完全不产生命令 → 曾经出现过「玩家明明切了模式
//  却没有任何记录」的漏记。巡检每 5 秒对比一次在线玩家的模式快照，无论通过
//  什么途径切换都会被发现并上报（ev=gmpatrol）。采集器会把「刚被命令记录过
//  的同一次切换」去重，不会双记。
//
//  ── v1.2.0 行为统计的上报策略 ────────────────────────────────
//  破坏/放置方块与击杀事件量极大（刷怪塔每秒可能死几百只怪），**逐条写日志会撑爆
//  服务端日志**。因此全部在内存中聚合计数，每 5 分钟（6000 tick）或玩家下线时
//  汇总输出一条 ev=stats。挂机零行为的玩家不会产生任何输出。
//
//  注：顶层只允许「事件注册调用」，不允许 var/函数声明 —— KubeJS 重载脚本时
//      顶层重声明会报错。因此第（三）部分整体包在一个 IIFE 里：函数作用域内的
//      声明在每次 reload 时随新闭包重建，不会触发重声明错误。
// ============================================================

// ── 一、命令事件 ────────────────────────────────────────────
ServerEvents.command(event => {
    (function () {
        var input = ''
        var who = 'console'
        var gm = ''
        var ms = ''

        // 1) 命令原文（控制台来源不含前导斜杠）
        try {
            input = String(event.getInput())
        } catch (e1) {
            try { input = String(event.getCommandName()) } catch (e1b) { input = '' }
        }

        // 2) 执行者：玩家名。非玩家来源（控制台 / 命令方块）保持 console
        try {
            var src = event.getParseResults().getContext().getSource()
            var p = src.getPlayer()
            if (p) {
                var nm = ''
                try {
                    var gp = p.getGameProfile()
                    if (gp) { nm = String(gp.getName() || '') }
                } catch (n1) { nm = '' }
                if (!nm || nm === 'undefined' || nm === 'null') {
                    try { nm = String(p.getName().getString() || '') } catch (n2) { nm = '' }
                }
                if (!nm || nm === 'undefined' || nm === 'null') {
                    try { nm = String(p.username || '') } catch (n3) { nm = '' }
                }
                if (!nm || nm === 'undefined' || nm === 'null') { nm = 'unknown-player' }
                who = nm
                // v1.2.0：四态模式判定（旧版 isCreative()?creative:survival 会把
                // 旁观/冒险一律误标成 survival）。isSpectator/isAdventure 的包装层
                // 导出情况未逐一实测，全部用 try 链保护，取不到时退回旧二态写法。
                try {
                    gm = ''
                    if (p.isCreative()) { gm = 'creative' }
                    else if (p.isSpectator()) { gm = 'spectator' }
                    else if (p.isAdventure()) { gm = 'adventure' }
                    else { gm = 'survival' }
                } catch (e2) {
                    try { gm = p.isCreative() ? 'creative' : 'survival' } catch (e2b) { gm = '' }
                }
            }
        } catch (e3) {
            who = 'console'
        }

        // 3) 时间戳（epoch 毫秒）。JS 原生优先；Java.loadClass 仅作末位兜底
        try {
            ms = String(new Date().getTime())
        } catch (e4) {
            try { ms = String(Java.loadClass('java.lang.System').currentTimeMillis()) } catch (e5) { ms = '' }
        }

        // 4) 上报：单行 JSON，便于采集器按行解析
        try {
            console.info('[MCAUDIT] ' + JSON.stringify({
                ev: 'cmd',
                ms: ms,
                actor: who,
                cmd: input,
                gm: gm
            }))
        } catch (err) {
            try { console.info('[MCAUDIT-FAIL] ' + err) } catch (e9) {}
        }
    })()
})
// ★★ 下面的分号不是可有可无的排版：没有它，JS 的自动分号插入（ASI）会把
// 下一行的 IIFE 吞进本表达式的调用链，即「ServerEvents.command(回调)(IIFE 结果)」
// —— 而注册函数返回 null，null 被当函数调用，报
// "TypeError: null is not a function, it is object"，整个脚本加载失败。
// v1.2.0 在真机上踩过（v1.1.1 没炸是因为它后面跟的是 EntityEvents.death
// 标识符，ASI 会插入分号）。防御式写法：事件注册一律以分号结尾，
// 独立 IIFE 一律以防御性分号开头。
;(function () {
    // ---- 计数器（玩家名 → 数量；JVM 生命周期内有效，reload/重启清零） ----
    var _broken = {}     // 破坏方块
    var _placed = {}     // 放置方块
    var _mobK  = {}      // 击杀生物
    var _pvpK  = {}      // 击杀玩家
    var _gmSnap = {}     // 模式巡检快照：玩家名 → 上次巡检到的模式
    var _tick = 0        // 节流计数器（纯 JS 自增，不调任何 Java tick 计数方法）

    // ---- helper：玩家名三级兜底（与命令处理器同款，刻意复制——顶层函数无法共享） ----
    function _nm(p) {
        var nm = ''
        try {
            var gp = p.getGameProfile()
            if (gp) { nm = String(gp.getName() || '') }
        } catch (n1) { nm = '' }
        if (!nm || nm === 'undefined' || nm === 'null') {
            try { nm = String(p.getName().getString() || '') } catch (n2) { nm = '' }
        }
        if (!nm || nm === 'undefined' || nm === 'null') {
            try { nm = String(p.username || '') } catch (n3) { nm = '' }
        }
        if (!nm || nm === 'undefined' || nm === 'null') { return '' }
        if (nm === 'unknown-player') { return '' }
        return nm
    }

    // ---- helper：判断实体是不是玩家 ----
    // 不能用「能取到名字」来判断：Zombie 也有 getName().getString()。
    // KubeJS 的 isPlayer() 是包装层提供的（优先）；getGameProfile() 只有 Player 系
    // 实现了（次优先）。两者都失败视为非玩家。
    function _isPlayer(e) {
        try { if (e && typeof e.isPlayer === 'function' && e.isPlayer()) return true } catch (i1) {}
        try {
            var gp = e.getGameProfile()
            if (gp && gp.getName()) return true
        } catch (i2) {}
        return false
    }

    // ---- helper：当前游戏模式四态判定（全失败返回 ''，调用方自行跳过） ----
    function _gmOf(p) {
        try { if (p.isCreative && p.isCreative()) return 'creative' } catch (g1) {}
        try { if (p.isSpectator && p.isSpectator()) return 'spectator' } catch (g2) {}
        try { if (p.isAdventure && p.isAdventure()) return 'adventure' } catch (g3) {}
        try { if (p.isSurvival && p.isSurvival()) return 'survival' } catch (g4) {}
        return ''
    }

    function _nowMs() {
        try { return String(new Date().getTime()) }
        catch (t0) { try { return String(Java.loadClass('java.lang.System').currentTimeMillis()) } catch (t1) { return '' } }
    }

    function _out(o) {
        try { console.info('[MCAUDIT] ' + JSON.stringify(o)) }
        catch (o1) { try { console.info('[MCAUDIT-FAIL] ' + o1) } catch (o2) {} }
    }

    // ---- 汇总上报：把某玩家当前积攒的行为计数写成一条 ev=stats ----
    function _flushOne(name, span) {
        var b = _broken[name] || 0, pl = _placed[name] || 0
        var mk = _mobK[name] || 0, pk = _pvpK[name] || 0
        if (!b && !pl && !mk && !pk) { return }        // 挂机玩家零输出
        _broken[name] = 0; _placed[name] = 0; _mobK[name] = 0; _pvpK[name] = 0
        _out({ ev: 'stats', ms: _nowMs(), actor: name,
               broken: b, placed: pl, mob: mk, pvp: pk, span: span || 0 })
    }

    function _flushAll(span) {
        var seen = {}
        var k
        for (k in _broken) { seen[k] = 1 }
        for (k in _placed) { seen[k] = 1 }
        for (k in _mobK) { seen[k] = 1 }
        for (k in _pvpK) { seen[k] = 1 }
        for (k in seen) { _flushOne(k, span) }
    }

    // ---- 巡检单个玩家的模式，与快照不一致则上报 ----
    function _patrolOne(p, seen) {
        var nm = ''
        try { nm = _nm(p) } catch (n0) { nm = '' }
        if (!nm) { return }
        seen[nm] = 1
        var gm = ''
        try { gm = _gmOf(p) } catch (g0) { gm = '' }
        if (!gm) { return }
        if (Object.prototype.hasOwnProperty.call(_gmSnap, nm)) {
            if (_gmSnap[nm] !== gm) {
                _gmSnap[nm] = gm
                _out({ ev: 'gmpatrol', ms: _nowMs(), actor: nm, gm: gm })
            }
        } else {
            _gmSnap[nm] = gm      // 首次见到（刚上线）：只建快照，不上报
        }
    }

    // ---- 玩家死亡事件 ----
    // EntityEvents.death 会对**所有**生物死亡触发，第一件事是判断"是不是玩家"：
    // · 是玩家 → 走完整的死亡上报（死因/击杀者/死讯），击杀者是玩家时打 kp 标记
    // · 不是玩家 → 只做一件事：击杀者是玩家就给他的「击杀生物」计数 +1，然后放过
    //   （刷怪塔每秒可能死几百只怪，非玩家分支必须保持极轻量）
    EntityEvents.death(event => {
        (function () {
            try {
                var pd = null
                try { pd = event.getPlayer() } catch (p0) { pd = null }

                if (!pd) {
                    // —— 非玩家死亡：统计击杀者（若为玩家）——
                    try {
                        var src = event.getSource()
                        var ke = null
                        if (src) {
                            try { ke = src.getActual() } catch (a1) { ke = null }
                            if (!ke) { try { ke = src.getImmediate() } catch (a2) { ke = null } }
                        }
                        if (ke && _isPlayer(ke)) {
                            var kn = _nm(ke)
                            if (kn) { _mobK[kn] = (_mobK[kn] || 0) + 1 }
                        }
                    } catch (a3) {}
                    return
                }

                // —— 玩家死亡：完整上报 ——
                var nm = ''
                try { nm = _nm(pd) } catch (n9) { nm = '' }
                // 拿不到名字就不上报：宁可漏一条，也不能把非玩家实体污染成玩家记录
                if (!nm) { return }

                var gm = ''
                try { gm = _gmOf(pd) || '' } catch (g9) { gm = '' }
                if (!gm) { try { gm = pd.isCreative() ? 'creative' : 'survival' } catch (g9b) { gm = '' } }

                // 死因（与语言无关的稳定 id，如 fall / lava / player）
                //
                // ⚠ 实测坑：DamageSource.getMsgId() 在 KubeJS/Rhino 里**不存在**
                //   （TypeError: Cannot find function getMsgId in object DamageSource (lava)），
                //   getLocalizedDeathMessage() 也会因 SRG 映射缺失报 InternalError。
                //   可用的是 ds.type().msgId()；再兜底从 String(ds) 里抠
                //   "DamageSource (lava)" 这段（DamageSource.toString() 就是 msgId）。
                var cause = ''
                try {
                    var ds = event.getSource()
                    if (ds) {
                        try {
                            var ty = ds.type()
                            if (ty) {
                                var mid = ty.msgId()
                                if (mid && String(mid) !== 'undefined') { cause = String(mid) }
                            }
                        } catch (c1) { cause = '' }
                        if (!cause) {
                            try {
                                var m2 = /\(([^)]+)\)/.exec(String(ds))
                                if (m2 && m2[1]) { cause = String(m2[1]) }
                            } catch (c2) { cause = '' }
                        }
                    }
                } catch (c3) { cause = '' }

                // 击杀者 + kp 标记（kp=1 表示击杀者也是玩家）
                //
                // ⚠ getEntity() / getDirectEntity() 不存在（实测）。
                //   可用的是 getActual()（箭 → 返回射箭的人）；getImmediate() 兜底（弩箭本身）。
                //   v1.2.0：先判定击杀者是不是玩家——是玩家才允许进玩家取名逻辑，
                //   否则生物名会污染 _pvpK；生物击杀者按原样取显示名。
                var killer = ''
                var isKp = 0
                var kn = ''
                var knIsPlayer = false
                try {
                    var ds2 = event.getSource()
                    if (ds2) {
                        var ke = null
                        try { ke = ds2.getActual() } catch (k0) { ke = null }
                        if (!ke) { try { ke = ds2.getImmediate() } catch (k0b) { ke = null } }
                        if (ke) {
                            knIsPlayer = _isPlayer(ke)
                            if (knIsPlayer) {
                                kn = _nm(ke)
                            } else {
                                // 生物击杀者：与 v1.1.1 相同的取名顺序（显示名优先）
                                try { kn = String(ke.getName().getString() || '') } catch (k2) { kn = '' }
                                if (!kn || kn === 'undefined' || kn === 'null') {
                                    try { kn = String(ke.getType() || '') } catch (k3) { kn = '' }
                                }
                            }
                            if (kn && kn !== 'undefined' && kn !== 'null') {
                                killer = kn
                                if (knIsPlayer) {
                                    isKp = 1
                                    _pvpK[kn] = (_pvpK[kn] || 0) + 1   // 玩家击杀玩家，PVP 计数
                                }
                            }
                        }
                    }
                } catch (k4) { killer = '' }

                // 游戏内死讯原文（可选，取不到不影响统计）
                var msg = ''
                try {
                    var ct = pd.getCombatTracker()
                    if (ct) {
                        var dm = ct.getDeathMessage()
                        if (dm) { msg = String(dm.getString() || '') }
                    }
                } catch (m0) { msg = '' }

                _out({
                    ev: 'death',
                    ms: _nowMs(),
                    actor: nm,
                    cause: cause,
                    killer: killer,
                    kp: isKp,
                    msg: msg,
                    gm: gm
                })
            } catch (err) {
                try { console.info('[MCAUDIT-FAIL] death ' + err) } catch (e9) {}
            }
        })()
    });

    // ---- 方块破坏 / 放置（只在事件源是玩家时计数） ----
    BlockEvents.broken(event => {
        (function () {
            try {
                var p = null
                try { p = event.player } catch (b0) { p = null }
                if (!p) { return }
                var nm = ''
                try { nm = _nm(p) } catch (b1) { nm = '' }
                if (!nm) { return }
                _broken[nm] = (_broken[nm] || 0) + 1
            } catch (b2) {}
        })()
    });
    BlockEvents.placed(event => {
        (function () {
            try {
                var p = null
                try { p = event.player } catch (q0) { p = null }
                if (!p) { return }
                var nm = ''
                try { nm = _nm(p) } catch (q1) { nm = '' }
                if (!nm) { return }
                _placed[nm] = (_placed[nm] || 0) + 1
            } catch (q2) {}
        })()
    });

    // ---- 上线 / 下线：不可用（环境实测） ----
    // ★ KubeJS 2001.6.5-build.16（MC 1.20.1 / Forge）真机实测：
    //   PlayerEvents.logged_in / logged_out 注册时报
    //   "Unknown event 'PlayerEvents.logged_in'!"（探针 typeof 返回的是动态存根
    //   function，调用时才报 Unknown），注册无效。因此这两个处理器已移除。
    //   功能影响与替代：
    //   · 登录建快照 → 巡检已覆盖：首次见到玩家即建快照、不上报，效果等同；
    //   · 下线冲账 stats → 无法替代，未上报的计数会延迟到下一个 5 分钟汇总周期。
    //     若你的 KubeJS 版本支持 PlayerEvents（新版可用），可自行把下面两个
    //     处理器加回来（记得末尾分号）。

    // ---- 心跳：模式巡检（每 100 tick ≈ 5 秒）+ 行为汇总（每 6000 tick ≈ 5 分钟） ----
    // 节流用自己的 JS 计数器；tick 回调每次只做一次取模判断，开销可忽略。
    ServerEvents.tick(event => {
        (function () {
            _tick = (_tick + 1) % 2147483647
            try {
                if (_tick % 100 === 0 && event && event.server) {
                    var lst = null
                    try { lst = event.server.players } catch (s1) { lst = null }
                    if (!lst) { try { lst = event.server.getPlayers() } catch (s2) { lst = null } }
                    if (!lst) { return }
                    var seen = {}
                    var walked = false
                    try {
                        if (lst && typeof lst.forEach === 'function') {
                            lst.forEach(function (p) { try { _patrolOne(p, seen) } catch (w0) {} })
                            walked = true
                        }
                    } catch (s3) {}
                    if (!walked) {
                        try {
                            var n = 0
                            try { n = lst.size() } catch (s4) { try { n = lst.length } catch (s4b) { n = 0 } }
                            for (var i = 0; i < n; i++) {
                                var p = null
                                try { p = lst.get(i) } catch (s5) { try { p = lst[i] } catch (s5b) { p = null } }
                                if (p) { try { _patrolOne(p, seen) } catch (w1) {} }
                            }
                        } catch (s6) {}
                    }
                    // 清理已离线玩家的巡检快照（防内存缓涨 + 复登时误报）
                    for (var k in _gmSnap) {
                        if (!seen[k]) { delete _gmSnap[k] }
                    }
                }
            } catch (s7) {}
            try {
                if (_tick % 6000 === 0) { _flushAll(300) }
            } catch (s8) {}
        })()
    });
})()
