// ============================================================
//  Minecraft 管理员行为审计 · 采集端（KubeJS）
//
//  作用：把每一件需要审计的事写成一行日志，交给同项目的 Python 采集器解析：
//
//    [MCAUDIT] {"ev":"cmd",   "ms":"...","actor":"...","cmd":"...","gm":"..."}
//    [MCAUDIT] {"ev":"death", "ms":"...","actor":"...","cause":"fall",
//                             "killer":"...","msg":"...","gm":"..."}
//
//  · ev=cmd    —— 有人执行了命令（含控制台 / 命令方块）
//  · ev=death  —— 玩家死亡（cause 为死因 id，killer 为击杀者，msg 为游戏内死讯原文）
//
//  采集器把「没有 ev 字段」的行也按 cmd 处理，因此旧版本留下的日志仍可解析。
//
//  性质：纯旁路只读监听。不改变任何游戏行为、不授予任何权限、不写存档。
//        删除本文件（或执行 kubejs reload server_scripts）即刻失效。
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
//   V  player.isCreative()
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
//
//  兼容性：KubeJS 6（Minecraft 1.20.1 / Forge）实测通过。
//          KubeJS 7（1.21+ / NeoForge）未实测，若 API 变动请对照上方注释调整。
//
//  注：下面两个处理器里各有一份玩家取名逻辑。刻意不做成公共函数 ——
//      KubeJS 重载脚本时，顶层函数/变量的重声明会报错，复制一小段比踩坑便宜。
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
                try { gm = p.isCreative() ? 'creative' : 'survival' } catch (e2) { gm = '' }
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

// ── 二、玩家死亡事件 ────────────────────────────────────────
// EntityEvents.death 会对**所有**生物死亡触发，所以第一件事是判断"是不是玩家"。
// 判断方式：EntityEventJS.getPlayer() 在实体不是玩家时返回 null（实测）。
// 这一步必须放在最前面 —— 刷怪塔每秒可能死几百只怪，后面的取值都要花钱。
EntityEvents.death(event => {
    (function () {
        try {
            var pd = null
            try { pd = event.getPlayer() } catch (p0) { pd = null }
            if (!pd) { return }                       // 不是玩家 → 直接放过

            // 玩家名（与命令处理器同一套三级兜底）
            var nm = ''
            try {
                var gp = pd.getGameProfile()
                if (gp) { nm = String(gp.getName() || '') }
            } catch (n1) { nm = '' }
            if (!nm || nm === 'undefined' || nm === 'null') {
                try { nm = String(pd.getName().getString() || '') } catch (n2) { nm = '' }
            }
            if (!nm || nm === 'undefined' || nm === 'null') {
                try { nm = String(pd.username || '') } catch (n3) { nm = '' }
            }
            // 拿不到名字就不上报：宁可漏一条，也不能把非玩家实体污染成玩家记录
            if (!nm || nm === 'undefined' || nm === 'null') { return }

            var gm = ''
            try { gm = pd.isCreative() ? 'creative' : 'survival' } catch (g0) { gm = '' }

            // 死因（与语言无关的稳定 id，如 fall / lava / player_attack）
            //
            // ⚠ 实测坑：DamageSource.getMsgId() 在 KubeJS/Rhino 里**不存在**
            //   （TypeError: Cannot find function getMsgId in object DamageSource (lava)），
            //   getLocalizedDeathMessage() 也会因 SRG 映射缺失报 InternalError。
            //   可用的是 ds.type().msgId()；再兜底从 String(ds) 里抠
            //   "DamageSource (lava)" 这段（DamageSource.toString() 就是 msgId）。
            //   两者都不写 catch 之外的日志，取不到就留空，绝不因此漏记一条死亡。
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

            // 击杀者
            //
            // ⚠ 同一个坑：getEntity() / getDirectEntity() 也不存在（实测）。
            //   可用的是 getActual()（箭 → 返回射箭的人）；getImmediate() 兜底（弩箭本身）。
            var killer = ''
            try {
                var ds2 = event.getSource()
                if (ds2) {
                    var ke = null
                    try { if (typeof ds2.getActual === 'function') { ke = ds2.getActual() } } catch (k0) { ke = null }
                    if (!ke) {
                        try { if (typeof ds2.getImmediate === 'function') { ke = ds2.getImmediate() } } catch (k0b) { ke = null }
                    }
                    if (ke) {
                        var kn = ''
                        try {
                            var kgp = ke.getGameProfile()
                            if (kgp) { kn = String(kgp.getName() || '') }
                        } catch (k1) { kn = '' }
                        if (!kn || kn === 'undefined' || kn === 'null') {
                            try { kn = String(ke.getName().getString() || '') } catch (k2) { kn = '' }
                        }
                        if (!kn || kn === 'undefined' || kn === 'null') {
                            try { kn = String(ke.getType() || '') } catch (k3) { kn = '' }
                        }
                        if (kn && kn !== 'undefined' && kn !== 'null') { killer = kn }
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

            var ms = ''
            try {
                ms = String(new Date().getTime())
            } catch (t0) {
                try { ms = String(Java.loadClass('java.lang.System').currentTimeMillis()) } catch (t1) { ms = '' }
            }

            console.info('[MCAUDIT] ' + JSON.stringify({
                ev: 'death',
                ms: ms,
                actor: nm,
                cause: cause,
                killer: killer,
                msg: msg,
                gm: gm
            }))
        } catch (err) {
            try { console.info('[MCAUDIT-FAIL] death ' + err) } catch (e9) {}
        }
    })()
})
