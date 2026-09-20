#!/usr/bin/env bash
# ============================================================
#  Minecraft 管理员行为审计 · 一键部署
#
#  用法（在仓库根目录执行）：
#      sudo bash deploy/install.sh
#
#  常用选项：
#      --server-dir DIR   指定 Minecraft 服务端目录（默认自动探测）
#      --log PATH         指定服务端日志文件（默认 <服务端>/logs/latest.log）
#      --dir DIR          安装目录（默认 /opt/minecraft-op-audit）
#      --port N           看板端口（默认 25567）
#      --title "TEXT"     看板标题
#      --no-token         关闭访问令牌（任何能访问到的人都可查看）
#      --no-names         跳过物品名称映射表构建
#      --no-kubejs        不安装 KubeJS 采集端脚本
#      --no-firewall      不改动防火墙
#      -y, --yes          不再交互确认
#
#  幂等：可重复执行。已有的 config.json 会被保留并补齐缺失字段，
#        台账 log/ 目录不会被清空。
# ============================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYN=$'\033[36m'; BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
hdr()  { printf '\n%s=== %s ===%s\n' "$BLD" "$*" "$RST"; }

# ---------- 0. 参数 ----------
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IDIR="/opt/minecraft-op-audit"
PORT="25567"
TITLE="Minecraft 管理员行为审计"
SERVER_DIR=""
LOG_PATH=""
USE_TOKEN=1
BUILD_NAMES=1
INSTALL_KUBEJS=1
FIREWALL=1
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --server-dir) SERVER_DIR="${2:-}"; shift 2 ;;
    --log)        LOG_PATH="${2:-}";   shift 2 ;;
    --dir)        IDIR="${2:-}";       shift 2 ;;
    --port)       PORT="${2:-}";       shift 2 ;;
    --title)      TITLE="${2:-}";      shift 2 ;;
    --no-token)   USE_TOKEN=0;         shift ;;
    --no-names)   BUILD_NAMES=0;       shift ;;
    --no-kubejs)  INSTALL_KUBEJS=0;    shift ;;
    --no-firewall) FIREWALL=0;         shift ;;
    -y|--yes)     ASSUME_YES=1;        shift ;;
    -h|--help)    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

[ "$(id -u)" = "0" ] || die "需要 root 权限运行：sudo bash $0"

[ -f "$SRC_DIR/collector/audit_watcher.py" ] || die "找不到源文件，请在仓库根目录执行：$SRC_DIR"
[ -f "$SRC_DIR/dashboard/audit_dashboard.py" ] || die "缺少 dashboard/audit_dashboard.py"

# ---------- 1. Python ----------
hdr "检查运行环境"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys,zoneinfo; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  fi
done
[ -n "$PY" ] || die "需要 Python 3.9+（用到了标准库 zoneinfo）。请先安装后重试。"
ok "Python：$PY（$("$PY" -V 2>&1)）"

if ! "$PY" -c 'import http.server' 2>/dev/null; then
  die "Python 缺少标准库 http.server"
fi
ok "标准库齐全（无第三方依赖）"

# ---------- 2. 探测服务端目录与日志 ----------
hdr "定位 Minecraft 服务端"

is_server_dir() {
  [ -d "$1" ] || return 1
  [ -f "$1/logs/latest.log" ] || [ -f "$1/server.properties" ]
}

if [ -z "$SERVER_DIR" ]; then
  CAND=""
  for pat in /opt/minecraft/* /srv/minecraft/* /srv/* /home/*/minecraft/* /home/*/* /root/minecraft /opt/*/server /data/minecraft/*; do
    for d in $pat; do
      if is_server_dir "$d"; then CAND="$CAND$d
"; fi
    done
  done
  CAND="$(printf '%s' "$CAND" | grep -v '^$' | sort -u || true)"
  n="$(printf '%s\n' "$CAND" | grep -c . || true)"
  if [ "$n" = "0" ]; then
    warn "没有自动找到服务端目录。"
    warn "可稍后用 --server-dir 指定，或手动编辑 $IDIR/config.json 的 server_log。"
  elif [ "$n" = "1" ]; then
    SERVER_DIR="$(printf '%s\n' "$CAND" | head -1)"
    ok "服务端目录：$SERVER_DIR"
  else
    say "找到多个候选服务端目录："
    i=1
    while IFS= read -r d; do say "  $i) $d"; i=$((i+1)); done <<EOF
$CAND
EOF
    printf '请选择编号（默认 1）：'
    read -r pick || pick=""
    if [ -z "$pick" ]; then pick=1; fi
    SERVER_DIR="$(printf '%s\n' "$CAND" | sed -n "${pick}p")"
    [ -n "$SERVER_DIR" ] || die "无效的选择"
    ok "服务端目录：$SERVER_DIR"
  fi
elif is_server_dir "$SERVER_DIR"; then
  ok "服务端目录：$SERVER_DIR"
else
  warn "指定的目录不像服务端目录（没有 logs/latest.log 或 server.properties）：$SERVER_DIR"
fi

if [ -z "$LOG_PATH" ] && [ -n "$SERVER_DIR" ]; then
  LOG_PATH="$SERVER_DIR/logs/latest.log"
fi
[ -n "$LOG_PATH" ] || LOG_PATH="/opt/minecraft/server/logs/latest.log"
if [ -f "$LOG_PATH" ]; then
  ok "服务端日志：$LOG_PATH"
else
  warn "日志文件当前不存在：$LOG_PATH（开服后会自动出现，采集器会等待）"
fi

# 日志时间戳的时区：JVM 未显式指定时跟随系统时区
LOG_TZ=""
if [ -n "$SERVER_DIR" ]; then
  LOG_TZ="$(grep -rhoE 'user\.timezone=[A-Za-z0-9_/+-]+' "$SERVER_DIR" 2>/dev/null \
            | head -1 | cut -d= -f2 || true)"
fi
if [ -z "$LOG_TZ" ]; then
  LOG_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
fi
[ -n "$LOG_TZ" ] || LOG_TZ="$(cat /etc/timezone 2>/dev/null || true)"
[ -n "$LOG_TZ" ] || LOG_TZ="UTC"
if [ -z "$(echo "$LOG_TZ" | tr -d ' ')" ]; then LOG_TZ="UTC"; fi
ok "日志时区：$LOG_TZ（仅用于事件缺毫秒时间戳时兜底）"

# ---------- 3. 确认 ----------
hdr "即将执行"
say "  安装目录    $IDIR"
say "  服务端目录  ${SERVER_DIR:-（未指定，仅部署看板）}"
say "  日志文件    $LOG_PATH"
say "  看板端口    $PORT"
say "  访问令牌    $([ "$USE_TOKEN" = 1 ] && echo '启用' || echo '关闭')"
say "  KubeJS 采集端 $([ "$INSTALL_KUBEJS" = 1 ] && echo '安装' || echo '跳过')"
if [ "$ASSUME_YES" != "1" ]; then
  printf '\n继续？[y/N] '
  read -r a || a=""
  case "$a" in y|Y|yes|YES) ;; *) say "已取消。"; exit 0 ;; esac
fi

# ---------- 4. 安装文件 ----------
hdr "安装程序文件"
mkdir -p "$IDIR/log"
cp -f "$SRC_DIR/collector/audit_watcher.py"   "$IDIR/"
cp -f "$SRC_DIR/dashboard/audit_dashboard.py" "$IDIR/"
cp -f "$SRC_DIR/dashboard/dashboard.html"     "$IDIR/"
chmod 755 "$IDIR/audit_watcher.py" "$IDIR/audit_dashboard.py"
ok "已复制采集器与看板到 $IDIR"

# 生成 / 合并 config.json
NEW_TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(12))')"
"$PY" - "$IDIR/config.json" "$LOG_PATH" "$LOG_TZ" "$PORT" "$TITLE" "$USE_TOKEN" "$NEW_TOKEN" <<'PY'
import json, os, sys
path, log, tz, port, title, use_token, new_token = sys.argv[1:8]
conf = {}
if os.path.exists(path):
    try:
        conf = json.load(open(path, encoding="utf-8"))
    except Exception:
        conf = {}
conf["server_log"] = log
conf["log_tz"] = tz
conf["dash_port"] = int(port)
conf["site_title"] = title
conf.setdefault("poll_seconds", 2)
conf.setdefault("first_run_from_end", True)
conf.setdefault("enabled_types", ["give", "item_set", "loot", "clear", "gamemode",
                                  "gamemode_other", "enchant", "effect", "xp", "op", "summon",
                                  "death", "stats"])
conf.setdefault("log_other_commands", False)
conf.setdefault("vanilla_fallback", True)
conf.setdefault("rotated_archive_scan", True)
conf.setdefault("dash_host", "0.0.0.0")
conf.setdefault("dash_ports", [])
conf.setdefault("lan_no_token", True)
conf.setdefault("cookie_days", 365)
if use_token == "1":
    conf["require_token"] = True
    if not conf.get("dash_token"):
        conf["dash_token"] = new_token
else:
    conf["require_token"] = False
json.dump(conf, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("  配置已写入：%s" % path)
print("  令牌：%s" % (conf.get("dash_token") or "（未启用）"))
PY
ok "config.json 就绪"

# ---------- 5. 物品名称映射表 ----------
hdr "构建物品名称映射表"
if [ "$BUILD_NAMES" != "1" ]; then
  warn "按参数跳过"
elif [ -z "$SERVER_DIR" ]; then
  warn "未定位到服务端目录，跳过。台账将只记录物品 ID"
  warn "可稍后手动执行：$PY $SRC_DIR/tools/build_item_names.py --server-dir <服务端目录> --out $IDIR/item_names.json"
else
  # 若本机存在客户端资源目录，一并扫入以补齐「原版物品的中文名」
  NAMES_EXTRA=()
  for mc in "$HOME/.minecraft" /root/.minecraft /home/*/.minecraft; do
    if [ -d "$mc/assets/indexes" ]; then
      NAMES_EXTRA+=(--minecraft-dir "$mc")
      ok "发现客户端资源目录，将一并提取原版中文名：$mc"
      break
    fi
  done

  if "$PY" "$SRC_DIR/tools/build_item_names.py" --server-dir "$SERVER_DIR" \
        ${NAMES_EXTRA[@]+"${NAMES_EXTRA[@]}"} --out "$IDIR/item_names.json"; then
    ok "映射表已生成"
  else
    warn "映射表构建失败或结果为空。台账仍可用，只是没有物品翻译名"
    warn "可稍后手动重试：$PY $SRC_DIR/tools/build_item_names.py --server-dir $SERVER_DIR --out $IDIR/item_names.json"
  fi
fi

# ---------- 6. KubeJS 采集端 ----------
hdr "安装 KubeJS 采集端"
if [ "$INSTALL_KUBEJS" != "1" ]; then
  warn "按参数跳过"
elif [ -z "$SERVER_DIR" ]; then
  warn "未定位到服务端目录，跳过"
elif [ ! -d "$SERVER_DIR/kubejs" ]; then
  warn "$SERVER_DIR/kubejs 不存在 —— 该服务端似乎没有安装 KubeJS"
  warn "请先安装 KubeJS 模组，然后重跑本脚本（或手动把 kubejs/admin_audit.js 放到 kubejs/server_scripts/）"
else
  mkdir -p "$SERVER_DIR/kubejs/server_scripts"
  cp -f "$SRC_DIR/kubejs/admin_audit.js" "$SERVER_DIR/kubejs/server_scripts/admin_audit.js"
  ok "已写入 $SERVER_DIR/kubejs/server_scripts/admin_audit.js"
  if pgrep -f 'java .*server|java @' >/dev/null 2>&1; then
    warn "检测到服务端正在运行 —— 需在控制台执行一次 kubejs reload server_scripts 生效"
    warn "（或在面板终端执行 /reload，下次开服则自动加载）"
  else
    ok "服务端未运行，下次开服会自动加载该脚本"
  fi
fi

# ---------- 7. systemd ----------
hdr "注册系统服务"
SVC_COL="minecraft-op-audit-collector"
SVC_DASH="minecraft-op-audit-dashboard"

if command -v systemctl >/dev/null 2>&1; then
  cat > "/etc/systemd/system/$SVC_COL.service" <<EOF
[Unit]
Description=Minecraft Admin Behavior Audit - Collector
After=network.target

[Service]
Type=simple
Environment=MCAUDIT_HOME=$IDIR
WorkingDirectory=$IDIR
ExecStart=$PY -u $IDIR/audit_watcher.py
Restart=always
RestartSec=5
StandardOutput=append:$IDIR/log/service.out
StandardError=append:$IDIR/log/service.err

[Install]
WantedBy=multi-user.target
EOF

  cat > "/etc/systemd/system/$SVC_DASH.service" <<EOF
[Unit]
Description=Minecraft Admin Behavior Audit - Web Dashboard
After=network.target

[Service]
Type=simple
Environment=MCAUDIT_HOME=$IDIR
WorkingDirectory=$IDIR
ExecStart=$PY -u $IDIR/audit_dashboard.py
Restart=always
RestartSec=5
StandardOutput=append:$IDIR/log/dash.out
StandardError=append:$IDIR/log/dash.err

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now "$SVC_COL" "$SVC_DASH" >/dev/null 2>&1 || true
  sleep 3
  ok "collector：$(systemctl is-active $SVC_COL)"
  ok "dashboard：$(systemctl is-active $SVC_DASH)"
else
  warn "没有 systemctl（非 systemd 系统）。请自行以 nohup 方式后台运行："
  warn "  MCAUDIT_HOME=$IDIR nohup $PY -u $IDIR/audit_watcher.py   >/dev/null 2>&1 &"
  warn "  MCAUDIT_HOME=$IDIR nohup $PY -u $IDIR/audit_dashboard.py >/dev/null 2>&1 &"
fi

# ---------- 8. 防火墙 ----------
hdr "放行看板端口 $PORT"
if [ "$FIREWALL" != "1" ]; then
  warn "按参数跳过（--no-firewall）"
else
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "$PORT/tcp" >/dev/null 2>&1 && ok "ufw 已放行 $PORT/tcp" || warn "ufw 放行失败"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null 2>&1 \
      && firewall-cmd --reload >/dev/null 2>&1 \
      && ok "firewalld 已放行 $PORT/tcp" || warn "firewalld 放行失败"
  elif command -v iptables >/dev/null 2>&1; then
    if iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
      ok "iptables 规则已存在"
    elif iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
      ok "iptables 已放行 $PORT/tcp"
      warn "iptables 规则重启后不保留。如需持久化：apt install iptables-persistent && netfilter-persistent save"
    else
      warn "iptables 规则添加失败（不影响本机访问）"
    fi
  else
    warn "未发现 ufw / firewalld / iptables，跳过"
  fi
fi

# ---------- 9. 自检 ----------
hdr "自检"
sleep 1
say "服务状态："
if command -v systemctl >/dev/null 2>&1; then
  for s in "$SVC_COL" "$SVC_DASH"; do
    printf '  %-28s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || echo n/a)"
  done
else
  say "  （本机无 systemd，请确认进程已手动启动）"
fi

say "端口监听："
ss -lnt 2>/dev/null | grep -E ":$PORT\b" || warn "  $PORT 未监听"

TOKEN="$("$PY" -c "
import json
try: print(json.load(open('$IDIR/config.json'))['dash_token'])
except Exception: print('')
" 2>/dev/null || true)"
QS=""
if [ -n "$TOKEN" ]; then QS="?k=$TOKEN"; fi
say "接口探测："
if command -v curl >/dev/null 2>&1; then
  printf '  /api/health      %s\n' "$(curl -s -m 5 -o /dev/null -w 'HTTP %{http_code}' "http://127.0.0.1:$PORT/api/health" || echo '连接失败')"
  printf '  /api/data        %s\n' "$(curl -s -m 5 -o /dev/null -w 'HTTP %{http_code}' "http://127.0.0.1:$PORT/api/data$QS" || echo '连接失败')"
  printf '  /  (看板首页)    %s\n' "$(curl -s -m 5 -o /dev/null -w 'HTTP %{http_code}' "http://127.0.0.1:$PORT/$QS" || echo '连接失败')"
fi

IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1 || true)"
if [ -z "$IP" ]; then IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"; fi
if [ -z "$IP" ]; then IP="<服务器IP>"; fi

hdr "完成"
if [ -n "$TOKEN" ]; then
  say "${BLD}看板地址${RST}   http://$IP:$PORT/?k=$TOKEN"
  say "${BLD}访问令牌${RST}   $TOKEN   ${CYN}（也写在 $IDIR/config.json）${RST}"
  say "             局域网内可省略 ?k=；外网访问必须带令牌"
else
  say "${BLD}看板地址${RST}   http://$IP:$PORT/"
  say "             ${YEL}令牌校验已关闭：任何能访问到该地址的人都能看到审计数据${RST}"
fi
say "${BLD}台账文件${RST}   $IDIR/log/events.csv          （Excel 直接打开）"
say "             $IDIR/log/events-YYYY-MM-DD.jsonl （按天明细，看板数据源）"
say "${BLD}采集日志${RST}   $IDIR/log/watcher.log"
say ""
say "${YEL}首次生效需要一步：${RST}若服务端当时正在运行，请在控制台执行一次"
say "                 ${BLD}kubejs reload server_scripts${RST}"
say "                 然后执行一条 /give 或 /gamemode，看板就会出现记录。"
say ""
say "卸载：sudo bash deploy/uninstall.sh --dir $IDIR"
say "文档：docs/quickstart.md  ·  docs/how-it-works.md  ·  docs/faq.md"
