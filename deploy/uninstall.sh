#!/usr/bin/env bash
# ============================================================
#  Minecraft 管理员行为审计 · 卸载
#
#  用法：
#      sudo bash deploy/uninstall.sh                    # 保留台账，删除程序与服务
#      sudo bash deploy/uninstall.sh --purge            # 连台账一起删除
#      sudo bash deploy/uninstall.sh --keep-kubejs      # 保留服务端上的 KubeJS 采集端
#
#  选项：
#      --dir DIR        安装目录（默认 /opt/minecraft-op-audit）
#      --server-dir DIR 服务端目录（用于移除 KubeJS 采集端脚本）
#      --port N         看板端口（默认 25567，用于清理防火墙规则）
#      --purge          同时删除日志与台账
#      --keep-kubejs    不删除服务端 kubejs/server_scripts/admin_audit.js
#      -y, --yes        不再交互确认
# ============================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

IDIR="/opt/minecraft-op-audit"
SERVER_DIR=""
PORT=""
PURGE=0
KEEP_KUBEJS=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)         IDIR="${2:-}";       shift 2 ;;
    --server-dir)  SERVER_DIR="${2:-}"; shift 2 ;;
    --port)        PORT="${2:-}";       shift 2 ;;
    --purge)       PURGE=1;             shift ;;
    --keep-kubejs) KEEP_KUBEJS=1;       shift ;;
    -y|--yes)      ASSUME_YES=1;        shift ;;
    -h|--help)     sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[ "$(id -u)" = "0" ] || die "需要 root 权限运行"

# 未显式指定端口时，优先从安装目录的 config.json 推导。
# 否则在「装机端口非 25567」的场景下会去删一个不相干端口的防火墙规则。
if [ -z "$PORT" ] && [ -f "$IDIR/config.json" ]; then
  PORT="$(grep -o '"dash_port"[[:space:]]*:[[:space:]]*[0-9]\+' "$IDIR/config.json" 2>/dev/null \
          | grep -o '[0-9]\+$' | head -1 || true)"
fi
PORT_KNOWN=1
if [ -z "$PORT" ]; then
  PORT="25567"
  PORT_KNOWN=0
fi

say "将要执行："
say "  停止并删除 systemd 服务  minecraft-op-audit-collector / minecraft-op-audit-dashboard"
say "  删除程序文件            $IDIR"
if [ "$PURGE" = "1" ]; then
  say "  ${RED}同时删除台账与日志${RST}  $IDIR/log/"
else
  say "  保留台账与日志          $IDIR/log/"
fi
if [ "$KEEP_KUBEJS" = "1" ]; then
  say "  保留服务端 KubeJS 采集端脚本"
elif [ -n "$SERVER_DIR" ]; then
  say "  移除服务端 KubeJS 采集端脚本（$SERVER_DIR）"
else
  say "  移除服务端 KubeJS 采集端脚本（自动查找）"
fi
if [ "$PORT_KNOWN" = "1" ]; then
  say "  移除防火墙放行规则      $PORT/tcp"
else
  say "  防火墙规则清理          ${YEL}跳过${RST}（找不到 config.json，可用 --port 指定）"
fi

if [ "$ASSUME_YES" != "1" ]; then
  printf '\n确认继续？[y/N] '
  read -r a || a=""
  case "$a" in y|Y|yes|YES) ;; *) say "已取消。"; exit 0 ;; esac
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now minecraft-op-audit-collector minecraft-op-audit-dashboard >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/minecraft-op-audit-collector.service \
        /etc/systemd/system/minecraft-op-audit-dashboard.service
  systemctl daemon-reload
  ok "服务已停止并删除"
else
  pkill -f "$IDIR/audit_watcher.py" 2>/dev/null || true
  pkill -f "$IDIR/audit_dashboard.py" 2>/dev/null || true
  ok "进程已停止"
fi

# KubeJS 采集端
if [ "$KEEP_KUBEJS" != "1" ]; then
  FOUND=0
  if [ -n "$SERVER_DIR" ] && [ -f "$SERVER_DIR/kubejs/server_scripts/admin_audit.js" ]; then
    rm -f "$SERVER_DIR/kubejs/server_scripts/admin_audit.js"
    FOUND=1
    ok "已移除 $SERVER_DIR/kubejs/server_scripts/admin_audit.js"
  fi
  if [ "$FOUND" = "0" ]; then
    for f in /opt/minecraft/*/kubejs/server_scripts/admin_audit.js; do
      if [ -f "$f" ]; then
        rm -f "$f"; ok "已移除 $f"; FOUND=1
      fi
    done
  fi
  if [ "$FOUND" = "1" ]; then
    warn "KubeJS 脚本已删除，但运行中的服务端仍保留在内存里的旧副本"
    warn "可在控制台执行一次 kubejs reload server_scripts 使其立即失效（或下次开服自然失效）"
  else
    warn "没有找到服务端上的 admin_audit.js，跳过"
  fi
fi

# 防火墙
if [ "$PORT_KNOWN" != "1" ]; then
  warn "跳过防火墙规则清理（无法确定端口）"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw delete allow "$PORT/tcp" >/dev/null 2>&1 && ok "ufw 规则已移除" || warn "ufw 规则移除失败或不存在"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --remove-port="$PORT/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 \
    && ok "firewalld 规则已移除" || warn "firewalld 规则移除失败或不存在"
elif command -v iptables >/dev/null 2>&1; then
  if iptables -D INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
    ok "iptables 规则已移除"
  else
    warn "iptables 无对应规则"
  fi
fi

# 文件
if [ -d "$IDIR" ]; then
  if [ "$PURGE" = "1" ]; then
    rm -rf "$IDIR"
    ok "已删除 $IDIR"
  else
    KEEP_DIR="${IDIR}-log-$(date +%Y%m%d%H%M%S)"
    mkdir -p "$KEEP_DIR"
    if [ -d "$IDIR/log" ]; then
      cp -a "$IDIR/log" "$KEEP_DIR/" 2>/dev/null || true
    fi
    if [ -f "$IDIR/config.json" ]; then
      cp -a "$IDIR/config.json" "$KEEP_DIR/" 2>/dev/null || true
    fi
    rm -rf "$IDIR"
    ok "程序已删除，台账与配置备份到 $KEEP_DIR"
  fi
else
  warn "$IDIR 不存在，跳过"
fi

say ""
ok "卸载完成"
say "服务端本身未做任何改动（本项目的所有写入都只发生在服务端目录之外，"
say "除了一个可随时删除的 KubeJS 脚本）。"
