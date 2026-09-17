// ============================================================
//  Minecraft 管理员行为审计 · 采集端（KubeJS）
//
//  作用：把每一条被执行的命令写成一行日志：
//          [MCAUDIT] {"ms":"...","actor":"...","cmd":"...","gm":"..."}
//        由同项目的 Python 采集器读取、解析、落盘为审计台账与 Web 看板。
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
//   V  event.getInput()                          → 命令原文
//   V  event.getParseResults().getContext().getSource().getPlayer()
//   V  player.getGameProfile().getName()
//   V  player.getName().getString()
//   V  player.username
//   V  player.isCreative()
//
//  兼容性：KubeJS 6（Minecraft 1.20.1 / Forge）实测通过。
//          KubeJS 7（1.21+ / NeoForge）未实测，若 API 变动请对照上方注释调整。
// ============================================================
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
