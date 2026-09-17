# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-09-17

首个公开版本。

### 功能

- **采集端**（`kubejs/admin_audit.js`）：监听 `ServerEvents.command`，把每条命令写成一行
  `[MCAUDIT] {"ms","actor","cmd","gm"}` 日志。纯只读，不改动服务端本体。
- **采集器**（`collector/audit_watcher.py`）：跟随服务端日志，解析 11 类管理命令，
  补齐物品中英文名，输出 `events.csv` + `events-YYYY-MM-DD.jsonl`。
  支持日志 inode 轮转与文件截断，跨重启保持读取位置。
- **看板**（`dashboard/`）：纯标准库 HTTP 服务，两个视图 ——
  时间轴（按日期分组）与玩家板块（按玩家查阅其全部行为）。
  支持天数 / 玩家 / 类型 / 关键词筛选，台账下载（CSV、JSONL）。
- **一键部署**（`deploy/install.sh`）：自动探测服务端目录与日志、生成配置与随机令牌、
  构建物品名映射表、注册 systemd 服务、放行端口、自检并打印访问地址。
  幂等，可重复执行。
- **卸载**（`deploy/uninstall.sh`）：停止服务、清理防火墙规则，默认保留台账并备份。

### 兼容性

- Minecraft 1.20.1 + Forge 47.4.16 + KubeJS 6（`kubejs-forge-2001.6.5-build.16`）实测通过。
- 需要 Python 3.9+（标准库 `zoneinfo`），无第三方依赖。

### 记录到的兼容性陷阱

开发过程中实测确认的两个 KubeJS 6 行为（已写入脚本注释，避免后续踩坑）：

- `java()` 全局函数已移除，调用会抛错，**且该错误会逃逸脚本内的 `try/catch`**。
  取时间必须用 JS 原生的 `new Date()`。
- `player.getScoreboardName()` 在脚本中不可见。用它取玩家名会抛错，
  抛错被兜底逻辑吞掉后，**玩家行为会被静默误标为 `console`** ——
  功能看起来正常，实际全错。改用 `getGameProfile().getName()` 并加三层兜底。

### 修复

- 玩家聚合中，同一条事件里执行者与对象为同一人时（如 `/give Alice ... @s`），
  记录数、取物数、模式切换数会被重复计两次。现在每个玩家在同一条事件中只累计一次。
