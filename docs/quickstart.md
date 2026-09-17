# 部署指南

## 1. 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| 服务端 | Minecraft Java 版，装了 **KubeJS 6** | `ls <服务端>/mods \| grep -i kubejs` |
| Python | **3.9+**（需标准库 `zoneinfo`） | `python3 -V` |
| 权限 | root（要写 systemd 与防火墙） | `id -u` |
| 系统 | Linux（有 systemd 最省事） | `systemctl --version` |

> `zoneinfo` 依赖系统时区数据库。极简容器里若报 `ZoneInfoNotFoundError`，
> 安装 `tzdata` 即可：`apt install tzdata` 或 `yum install tzdata`。

## 2. 安装

```bash
git clone https://github.com/Siiika-latest/minecraft-op-audit.git
cd minecraft-op-audit
sudo bash deploy/install.sh
```

### 全部参数

```
--server-dir DIR   指定 Minecraft 服务端目录（默认自动探测）
--log PATH         指定服务端日志文件（默认 <服务端>/logs/latest.log）
--dir DIR          安装目录（默认 /opt/minecraft-op-audit）
--port N           看板端口（默认 25567）
--title "TEXT"     看板标题（默认「Minecraft 管理员行为审计」）
--no-token         关闭访问令牌
--no-names         跳过物品名称映射表构建
--no-kubejs        不安装 KubeJS 采集端脚本
--no-firewall      不改动防火墙
-y, --yes          不再交互确认
```

### 安装做了什么

1. 校验 Python 版本与标准库可用性
2. **探测服务端目录**：扫 `/opt/minecraft/*`、`/srv/*`、`/home/*`、`/root`、`/data` 等
   常见位置，按「有 `logs/latest.log` 或 `server.properties`」判定；找到多个会列出来让你选
3. 推断日志时区（优先读服务端启动参数里的 `-Duser.timezone`，否则用系统时区）
4. 拷贝采集器与看板到安装目录，生成 `config.json`（含随机令牌）
5. **构建物品名称映射表**：扫服务端 `mods/`、顶层 jar，以及 `libraries/` 下的
   `server-*-extra.jar`（原版物品英文名就在这里）
6. 写入 `<服务端>/kubejs/server_scripts/admin_audit.js`
7. 注册并启动 `minecraft-op-audit-collector`、`minecraft-op-audit-dashboard` 两个 systemd 服务
8. 放行看板端口（自动识别 ufw / firewalld / iptables）
9. 自检并打印访问地址

### 幂等性

可以反复执行。已有的 `config.json` 会被**保留并补齐缺失字段**，`log/` 台账不会被清空。

## 3. 让它立刻生效

**服务端关闭时**：下次开服自动加载采集端，无需操作。

**服务端正在运行时**：需要在控制台执行一次

```
kubejs reload server_scripts
```

然后随便执行一条 `/give` 或 `/gamemode`，看板就会出现记录。

> 若想连测试记录都不要，验证完后删掉 `log/events*.csv`、`log/events-*.jsonl`
> 和 `state.json`，再 `systemctl restart minecraft-op-audit-collector`。

## 4. 验证

```bash
# 服务状态
systemctl status minecraft-op-audit-collector minecraft-op-audit-dashboard

# 接口
curl -s http://127.0.0.1:25567/api/health
# {"ok": true, "files": 0}

# 采集器日志（能看到「事件: ...」就说明链路通了）
tail -f /opt/minecraft-op-audit/log/watcher.log

# 台账
cat /opt/minecraft-op-audit/log/events.csv
```

## 5. 排错

### 看板是空的，采集器日志也没有「事件」

按顺序查：

```bash
# ① 采集端脚本在不在？
ls -l <服务端>/kubejs/server_scripts/admin_audit.js

# ② 服务端日志里有没有 [MCAUDIT] 行？
grep -c '\[MCAUDIT\]' <服务端>/logs/latest.log

# ③ 服务端日志里有没有 KubeJS 报错？
grep -n 'Error in' <服务端>/logs/latest.log | tail
```

- ② 为 0 且 ③ 有 `Error in 'ServerEvents.command'` → 采集端脚本报错，
  检查是否被改写过（对照仓库里的原版）
- ② 为 0 且 ③ 无报错 → 脚本没被加载，执行 `kubejs reload server_scripts`
- ② 有行但台账空 → 采集器没读到：核对 `config.json` 里的 `server_log` 路径

### 端口通不了

```bash
ss -lnt | grep 25567              # 在不在监听
iptables -L INPUT -n | head       # 有没有被规则挡住
```

云服务器还需要在**安全组**里放行该端口。

### 中文名显示不出来 / 显示「（未匹配到 ID）」

映射表里没有这个物品。重建映射表（见 [FAQ](faq.md#如何重建物品名称映射表)）。

### 时间不对

事件时间戳优先取 KubeJS 提供的 epoch 毫秒，这个值是绝对时间，转北京时间是确定的。
只有当毫秒缺失时才回落到日志行时间戳，此时依赖 `config.json` 的 `log_tz` 是否正确。

```bash
# 看服务端 JVM 用的是哪个时区
grep -o 'user.timezone=[^ ]*' <服务端>/user_jvm_args.txt
```

## 6. 升级

```bash
cd minecraft-op-audit
git pull
sudo bash deploy/install.sh          # 会覆盖程序文件，保留 config 与台账
sudo systemctl restart minecraft-op-audit-collector minecraft-op-audit-dashboard
```

采集端脚本有更新时，还需在控制台 `kubejs reload server_scripts`。

## 7. 卸载

```bash
sudo bash deploy/uninstall.sh                    # 台账备份到 /opt/minecraft-op-audit-log-<时间戳>/
sudo bash deploy/uninstall.sh --purge            # 连台账一起删
sudo bash deploy/uninstall.sh --keep-kubejs      # 保留服务端上的采集端脚本
```
