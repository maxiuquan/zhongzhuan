#!/usr/bin/env bash
# zhongzhuan VPS 一键部署脚本
# 支持两种证书路径：
#   1) Let's Encrypt（需域名 A 记录指向本机）
#   2) 自签 CA + 叶子证书（仅需公网 IP）
#
# 用法：
#   交互模式：  sudo ./deploy.sh
#   非交互模式：sudo ./deploy.sh --yes --cert-path selfsign --ip 1.2.3.4
#               sudo ./deploy.sh --yes --cert-path letsencrypt --domain zh.example.com
#   查看帮助：  ./deploy.sh --help

set -euo pipefail

# ===========================================================================
# 颜色与日志
# ===========================================================================
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; CYAN=''; BOLD=''; NC=''
fi

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_step()  { echo -e "\n${BLUE}${BOLD}=== $* ===${NC}"; }
log_note()  { echo -e "${CYAN}[NOTE]${NC} $*"; }

die() { log_error "$*"; exit 1; }

confirm() {
  local prompt="$1" default="${2:-n}"
  if [[ "$ASSUME_YES" == "1" ]]; then
    echo -e "$prompt [y/N] ${GREEN}y${NC} (auto)" >&2
    return 0
  fi
  local ans
  read -r -p "$prompt [y/N] " ans || true
  [[ "${ans:-$default}" =~ ^[Yy]$ ]]
}

# ===========================================================================
# 默认配置
# ===========================================================================
ASSUME_YES=0
CERT_PATH=""              # letsencrypt | selfsign
DOMAIN=""
PUBLIC_IP=""
CN_NAME="zhongzhuan-vps"
PROXY_PORT=8443
ADMIN_PORT=8089
SKIP_FIREWALL=0
SKIP_SYSTEMD=0
SKIP_DEPS=0
PYTHON_BIN=""

# 运行时填充
SCRIPT_DIR=""
INSTALL_DIR=""
OS_ID=""
CERT_FILE=""
KEY_FILE=""
CA_FILE=""                # 仅 selfsign 路径有值
SERVICE_NAME="zhongzhuan"
FINAL_BASE_URL=""
FINAL_ACCESS_TOKEN=""

# ===========================================================================
# 参数解析
# ===========================================================================
usage() {
  cat <<'EOF'
zhongzhuan VPS 一键部署脚本

用法:
  sudo ./deploy.sh [选项]

选项:
  -y, --yes                 非交互模式（全部使用默认/已传参值）
      --cert-path <PATH>    证书路径: letsencrypt | selfsign
      --domain <DOMAIN>     Let's Encrypt 路径用的域名
      --ip <IP>             自签路径用的公网 IP
      --cn <CN>             自签证书 Common Name（默认 zhongzhuan-vps）
      --proxy-port <PORT>   代理端口（默认 8443）
      --admin-port <PORT>   管理端口（默认 8089）
      --install-dir <DIR>   安装目录（默认脚本所在目录）
      --python <BIN>        指定 python 解释器（默认自动检测 python3）
      --no-firewall         跳过防火墙配置
      --no-systemd          跳过 systemd 服务安装（仅前台启动验证）
      --skip-deps           跳过系统/Python 依赖安装（依赖已装的环境用）
  -h, --help                显示本帮助

示例:
  交互模式:
    sudo ./deploy.sh

  非交互 - 自签（仅有 IP）:
    sudo ./deploy.sh -y --cert-path selfsign --ip 203.0.113.10

  非交互 - Let's Encrypt（有域名）:
    sudo ./deploy.sh -y --cert-path letsencrypt --domain zh.example.com
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes)        ASSUME_YES=1; shift ;;
      --cert-path)     CERT_PATH="$2"; shift 2 ;;
      --domain)        DOMAIN="$2"; shift 2 ;;
      --ip)            PUBLIC_IP="$2"; shift 2 ;;
      --cn)            CN_NAME="$2"; shift 2 ;;
      --proxy-port)    PROXY_PORT="$2"; shift 2 ;;
      --admin-port)    ADMIN_PORT="$2"; shift 2 ;;
      --install-dir)   INSTALL_DIR="$2"; shift 2 ;;
      --python)        PYTHON_BIN="$2"; shift 2 ;;
      --no-firewall)   SKIP_FIREWALL=1; shift ;;
      --no-systemd)    SKIP_SYSTEMD=1; shift ;;
      --skip-deps)     SKIP_DEPS=1; shift ;;
      -h|--help)       usage; exit 0 ;;
      *)               die "未知参数: $1（用 --help 查看用法）" ;;
    esac
  done

  # 校验 cert-path
  if [[ -n "$CERT_PATH" ]]; then
    case "$CERT_PATH" in
      letsencrypt|selfsign) ;;
      *) die "--cert-path 只能是 letsencrypt 或 selfsign" ;;
    esac
  fi
}

# ===========================================================================
# 环境检查
# ===========================================================================
require_root() {
  if [[ $EUID -ne 0 ]]; then
    die "需要 root 权限运行（用于安装系统包、配置防火墙、注册 systemd 服务）。请用 sudo。"
  fi
}

detect_os() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    log_info "检测到系统: $OS_ID ${VERSION_ID:-}（$PRETTY_NAME）"
  else
    die "无法识别操作系统（缺少 /etc/os-release），仅支持 Linux"
  fi
  case "$OS_ID" in
    ubuntu|debian|raspbian) ;;
    centos|rhel|fedora|rocky|almalinux|amzn) ;;
    *) log_warn "未正式支持的发行版: $OS_ID，脚本会尝试 apt/dnf 通用路径" ;;
  esac
}

detect_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "指定的 python 不存在: $PYTHON_BIN"
    return
  fi
  for cand in python3 python3.11 python3.12 python3.13 python3.14; do
    if command -v "$cand" >/dev/null 2>&1; then
      PYTHON_BIN="$cand"
      return
    fi
  done
  die "未找到 python3，请先安装 Python >= 3.10"
}

detect_install_dir() {
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -z "$INSTALL_DIR" ]]; then
    INSTALL_DIR="$SCRIPT_DIR"
  fi
  # 校验项目结构：必须有 src/zhongzhuan
  if [[ ! -d "$INSTALL_DIR/src/zhongzhuan" ]]; then
    die "在 $INSTALL_DIR 下未找到 src/zhongzhuan，请把脚本放在项目根目录运行，或用 --install-dir 指定"
  fi
  log_info "安装目录: $INSTALL_DIR"
  log_info "Python:   $PYTHON_BIN ($("$PYTHON_BIN" --version))"
}

detect_public_ip() {
  if [[ -n "$PUBLIC_IP" ]]; then return; fi
  local ip=""
  ip=$(curl -s --max-time 5 https://ifconfig.me 2>/dev/null || true)
  if [[ -z "$ip" ]]; then
    ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || true)
  fi
  if [[ -z "$ip" ]]; then
    ip=$(curl -s --max-time 5 https://ipinfo.io/ip 2>/dev/null || true)
  fi
  PUBLIC_IP="$ip"
}

# ===========================================================================
# 依赖安装
# ===========================================================================
install_system_deps() {
  log_step "安装系统依赖"
  # 注意：certbot 不在这里装——apt 版与系统 cryptography 版本冲突，
  # 由独立的 install_certbot() 用 snap/pip 方式装
  case "$OS_ID" in
    ubuntu|debian|raspbian)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq python3 python3-pip python3-venv ufw curl openssl \
        >/dev/null
      ;;
    centos|rhel|rocky|almalinux|amzn)
      if command -v dnf >/dev/null 2>&1; then PM=dnf; else PM=yum; fi
      $PM install -y -q python3 python3-pip firewalld curl openssl \
        >/dev/null
      ;;
    fedora)
      dnf install -y -q python3 python3-pip firewalld curl openssl \
        >/dev/null
      ;;
    *)
      log_warn "未知发行版，跳过系统包安装。请确保已装: python3 python3-pip ufw/firewalld curl openssl"
      ;;
  esac
  log_info "系统依赖安装完成"
}

install_python_deps() {
  log_step "安装 Python 依赖"
  # 始终加 --break-system-packages 绕过 PEP 668（ubuntu 24+/debian 12+ 默认禁止 pip 全局安装）
  "$PYTHON_BIN" -m pip install -q --break-system-packages \
    "aiohttp>=3.9" "httpx>=0.27" "pyyaml>=6.0" "loguru>=0.7" \
    "python-dotenv" "aiosqlite" "cryptography" "bcrypt" "PyJWT" 2>&1 \
    | grep -v "already satisfied" || true
  log_info "Python 依赖安装完成"
}

# ===========================================================================
# certbot 安装（避免 apt 版与系统 cryptography 版本冲突）
# ===========================================================================
install_certbot() {
  # 已存在且能跑就直接用
  if command -v certbot >/dev/null 2>&1; then
    if certbot --version >/dev/null 2>&1; then
      log_info "certbot 已安装: $(certbot --version 2>&1)"
      return 0
    fi
    log_warn "检测到 certbot 但无法运行（可能依赖损坏），将重新安装"
  fi

  # 优先 snap 版：自带隔离的 Python 环境，不受系统 cryptography 版本影响
  if command -v snap >/dev/null 2>&1; then
    log_info "通过 snap 安装 certbot（推荐，避免依赖冲突）..."
    snap install core >/dev/null 2>&1 || true
    snap refresh core >/dev/null 2>&1 || true
    if snap install --classic certbot 2>/dev/null; then
      ln -sf /snap/bin/certbot /usr/local/bin/certbot 2>/dev/null || true
      if certbot --version >/dev/null 2>&1; then
        log_info "snap certbot 安装成功: $(certbot --version 2>&1)"
        return 0
      fi
    fi
    log_warn "snap 安装 certbot 失败，回退到 pip"
  fi

  # 降级方案：pip 装独立 certbot（与系统 cryptography 绑定，但版本较新通常兼容）
  log_info "通过 pip 安装 certbot..."
  "$PYTHON_BIN" -m pip install -q --break-system-packages certbot 2>&1 \
    | grep -v "already satisfied" || true

  if certbot --version >/dev/null 2>&1; then
    log_info "pip certbot 安装成功: $(certbot --version 2>&1)"
    return 0
  fi

  # 最后兜底：apt 版（可能与系统 cryptography 冲突，仅作最后手段）
  log_warn "snap/pip 均不可用，回退到 apt 版 certbot（可能依赖冲突）..."
  case "$OS_ID" in
    ubuntu|debian|raspbian) apt-get install -y -qq certbot >/dev/null ;;
    centos|rhel|rocky|almalinux|fedora|amzn) dnf install -y -q certbot >/dev/null ;;
  esac

  # 修复 apt 版 certbot 与新版 cryptography 的冲突：
  # josepy 依赖的 pyOpenSSL 用了已删除的 lib.GEN_EMAIL，降级 cryptography 到 <42
  if ! certbot --version >/dev/null 2>&1; then
    log_warn "apt certbot 依赖冲突，尝试降级 cryptography 修复..."
    "$PYTHON_BIN" -m pip install -q --break-system-packages "cryptography<42" 2>&1 \
      | grep -v "already satisfied" || true
  fi

  certbot --version >/dev/null 2>&1 \
    && log_info "certbot 安装成功: $(certbot --version 2>&1)" \
    || die "certbot 安装失败。请手动安装：snap install --classic certbot 或 pip install certbot"
}

# ===========================================================================
# 证书路径选择
# ===========================================================================
choose_cert_path() {
  if [[ -n "$CERT_PATH" ]]; then
    log_info "已指定证书路径: $CERT_PATH"
    return
  fi
  if [[ "$ASSUME_YES" == "1" ]]; then
    CERT_PATH="selfsign"
    log_warn "非交互模式未指定 --cert-path，默认走自签路径"
    return
  fi
  echo
  echo -e "${BOLD}请选择证书路径：${NC}"
  echo -e "  ${CYAN}1)${NC} Let's Encrypt（需域名 A 记录指向本机，标准 CA，客户端无需额外信任）"
  echo -e "  ${CYAN}2)${NC} 自签 CA + 叶子证书（仅需公网 IP，客户端需配置 NODE_EXTRA_CA_CERTS）"
  local choice
  read -r -p "请选择 [1/2] (默认 2): " choice || true
  case "${choice:-2}" in
    1) CERT_PATH="letsencrypt" ;;
    2|'') CERT_PATH="selfsign" ;;
    *) die "无效选择: $choice" ;;
  esac
}

# ===========================================================================
# 路径 A: Let's Encrypt
# ===========================================================================
setup_letsencrypt() {
  log_step "配置 Let's Encrypt 证书"

  if [[ -z "$DOMAIN" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      die "Let's Encrypt 路径需要 --domain 参数"
    fi
    read -r -p "请输入指向本机的域名（如 zh.example.com）: " DOMAIN || true
    [[ -n "$DOMAIN" ]] || die "未输入域名"
  fi

  # 校验域名解析到本机
  detect_public_ip
  local resolved
  resolved=$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1 || true)
  if [[ -z "$resolved" ]]; then
    resolved=$(dig +short "$DOMAIN" 2>/dev/null | head -1 || true)
  fi
  if [[ -n "$resolved" && -n "$PUBLIC_IP" && "$resolved" != "$PUBLIC_IP" ]]; then
    log_warn "域名 $DOMAIN 解析到 $resolved，但本机公网 IP 检测为 $PUBLIC_IP"
    confirm "继续可能签发失败，是否继续?" || die "用户取消"
  fi

  # 安装 certbot（优先 snap，避免 apt 版与系统 cryptography 版本冲突）
  install_certbot

  # 临时放行 80（standalone 验证用）——本地防火墙
  if command -v ufw >/dev/null 2>&1; then
    ufw allow 80/tcp >/dev/null 2>&1 || true
  elif command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=80/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
  fi

  # 预检 80 端口：本地是否被占用
  if ss -tlnp 2>/dev/null | grep -q ':80 ' && [[ "$ASSUME_YES" != "1" ]]; then
    log_warn "检测到 80 端口已被占用，certbot standalone 需要绑 80。建议先停掉占用 80 的服务。"
    confirm "强行继续?" || die "用户取消"
  fi

  # 预检 80 端口：公网可达性（云安全组最容易漏）
  log_info "预检 80 端口公网可达性（云安全组最易漏放行）..."
  local port80_ok=1
  local check_url="http://$PUBLIC_IP:80/"
  # 用第三方探测服务从外部测 80 是否通
  local ext_check
  ext_check=$(curl -s --max-time 8 \
    "https://portchecker.co/check" \
    -d "target=$PUBLIC_IP&port=80" 2>/dev/null || true)
  if echo "$ext_check" | grep -qi "open\|reachable\|1"; then
    log_info "外部探测：80 端口可达"
  else
    port80_ok=0
    log_warn "外部探测 80 端口不可达（或探测服务超时）"
  fi

  CERT_FILE="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
  KEY_FILE="/etc/letsencrypt/live/$DOMAIN/privkey.pem"

  if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
    log_info "证书已存在，跳过签发: $CERT_FILE"
  else
    if [[ "$port80_ok" != "1" ]]; then
      echo
      echo -e "${RED}${BOLD}=== 80 端口公网不可达，certbot standalone 签发必失败 ===${NC}"
      echo -e "Let's Encrypt 验证服务器需要从公网访问 http://$DOMAIN:80/.well-known/acme-challenge/"
      echo -e "请按以下顺序排查："
      echo -e "  ${CYAN}1.${NC} 云厂商控制台安全组/防火墙规则：放行入站 TCP 80"
      echo -e "     - GCP: VPC 网络 → 防火墙 → 添加入站规则 TCP:80"
      echo -e "     - AWS: 安全组 → 入站规则 → TCP 80 0.0.0.0/0"
      echo -e "     - 阿里云: 安全组 → 入方向 → TCP 80"
      echo -e "     - 腾讯云: 安全组 → 入站规则 → TCP 80"
      echo -e "  ${CYAN}2.${NC} 本机 ufw/firewalld 已自动放行 80（脚本已处理）"
      echo -e "  ${CYAN}3.${NC} 域名 $DOMAIN 的 A 记录必须指向 $PUBLIC_IP"
      echo -e "  ${CYAN}4.${NC} 若 80 端口实在无法放行，改用自签路径："
      echo -e "     sudo ./deploy.sh --cert-path selfsign --ip $PUBLIC_IP"
      echo
      if [[ "$ASSUME_YES" != "1" ]]; then
        confirm "已放行云安全组 80 端口，继续签发?" || die "用户取消"
      else
        die "80 端口不可达，非交互模式拒绝继续。请放行云安全组 80 后重跑，或改用 --cert-path selfsign"
      fi
    fi

    log_info "运行 certbot 签发证书（standalone，临时占用 80 端口）..."
    certbot certonly --standalone \
      -d "$DOMAIN" \
      --non-interactive \
      --agree-tos \
      --register-unsafely-without-email \
      --keep-until-expiring \
      || {
        echo
        echo -e "${RED}certbot 签发失败。排查清单：${NC}"
        echo -e "  1. 域名 $DOMAIN 的 A 记录是否指向 $PUBLIC_IP？（dig +short $DOMAIN）"
        echo -e "  2. 云厂商安全组是否放行入站 TCP 80？（最常见原因）"
        echo -e "  3. 本机 80 端口是否被其他服务占用？（ss -tlnp | grep :80）"
        echo -e "  4. 若 80 无法放行，改用自签：sudo ./deploy.sh --cert-path selfsign --ip $PUBLIC_IP"
        die "certbot 签发失败"
      }
  fi

  FINAL_BASE_URL="https://$DOMAIN:$PROXY_PORT"
  log_info "Let's Encrypt 证书就绪: $CERT_FILE"
}

# ===========================================================================
# 路径 B: 自签
# ===========================================================================
setup_selfsign() {
  log_step "生成自签证书（CA + 叶子）"

  detect_public_ip
  if [[ -z "$PUBLIC_IP" ]]; then
    if [[ "$ASSUME_YES" == "1" ]]; then
      die "自签路径需要 --ip 参数（或本机能访问公网检测 IP）"
    fi
    read -r -p "未能自动检测公网 IP，请手动输入: " PUBLIC_IP || true
    [[ -n "$PUBLIC_IP" ]] || die "未提供公网 IP"
  fi
  log_info "公网 IP: $PUBLIC_IP"

  if [[ -z "$DOMAIN" ]] && [[ "$ASSUME_YES" != "1" ]]; then
    read -r -p "可选：是否有域名也指向本机？输入域名留空跳过: " DOMAIN || true
  fi

  local cert_dir="$INSTALL_DIR/data"
  mkdir -p "$cert_dir"
  CERT_FILE="$cert_dir/server.crt"
  KEY_FILE="$cert_dir/server.key"
  CA_FILE="$cert_dir/local-ca.crt"

  # 构造 SAN 参数
  local -a san_ip_args=("--san-ip" "$PUBLIC_IP")
  local -a san_dns_args=()
  # 总是加上 localhost 便于本机测试
  san_dns_args+=(--san-dns localhost)
  if [[ -n "$DOMAIN" ]]; then
    san_dns_args+=(--san-dns "$DOMAIN")
  fi

  log_info "调用 zhongzhuan 自签工具生成证书..."
  (
    cd "$INSTALL_DIR"
    PYTHONPATH="$INSTALL_DIR/src" "$PYTHON_BIN" -m zhongzhuan \
      --tls-selfsign \
      --cn "$CN_NAME" \
      "${san_ip_args[@]}" \
      "${san_dns_args[@]}" \
      --out-cert "$CERT_FILE" \
      --out-key  "$KEY_FILE" \
      --out-ca   "$CA_FILE" \
      --days 3650
  ) || die "自签证书生成失败"

  chmod 600 "$KEY_FILE"
  chmod 644 "$CERT_FILE" "$CA_FILE"

  # 决定 base_url 主机名：优先域名，否则 IP
  local host="$PUBLIC_IP"
  if [[ -n "$DOMAIN" ]]; then host="$DOMAIN"; fi
  FINAL_BASE_URL="https://$host:$PROXY_PORT"

  log_info "自签证书就绪:"
  log_info "  cert: $CERT_FILE"
  log_info "  key:  $KEY_FILE"
  log_info "  CA:   $CA_FILE  (需分发给 Claude Code 客户端)"
}

# ===========================================================================
# 生成 .env
# ===========================================================================
write_env_file() {
  log_step "生成 .env 配置"
  local env_file="$INSTALL_DIR/.env"
  cat > "$env_file" <<EOF
# zhongzhuan 部署配置（由 deploy.sh 自动生成）
# 监听
ZHONGZHUAN_PROXY_HOST=0.0.0.0
ZHONGZHUAN_PROXY_PORT=$PROXY_PORT
ZHONGZHUAN_ADMIN_HOST=0.0.0.0
ZHONGZHUAN_ADMIN_PORT=$ADMIN_PORT
# TLS
ZHONGZHUAN_TLS_ENABLED=true
ZHONGZHUAN_TLS_CERT=$CERT_FILE
ZHONGZHUAN_TLS_KEY=$KEY_FILE
# 鉴权（VPS 必开）
ZHONGZHUAN_PROXY_AUTH=true
EOF
  chmod 600 "$env_file"
  log_info "已写入 $env_file"
}

# ===========================================================================
# 防火墙
# ===========================================================================
setup_firewall() {
  log_step "配置防火墙"
  if command -v ufw >/dev/null 2>&1; then
    # 先放行 SSH，避免把自己锁外面
    ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow "$PROXY_PORT"/tcp >/dev/null 2>&1 || true
    # admin 端口也放行（对外暴露，依赖 admin 登录鉴权保护）
    ufw allow "$ADMIN_PORT"/tcp >/dev/null 2>&1 || true
    if ! ufw status | grep -q "Status: active"; then
      log_warn "即将启用 ufw 防火墙。已放行: 22/tcp, $PROXY_PORT/tcp, $ADMIN_PORT/tcp (admin)"
      if confirm "确认启用 ufw?" "y"; then
        yes | ufw enable >/dev/null 2>&1 || true
        log_info "ufw 已启用"
      else
        log_warn "跳过 ufw 启用，请手动配置防火墙"
      fi
    else
      log_info "ufw 已启用，规则已更新"
    fi
    ufw status numbered 2>/dev/null | head -20 || true
  elif command -v firewall-cmd >/dev/null 2>&1; then
    systemctl start firewalld >/dev/null 2>&1 || true
    systemctl enable firewalld >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-service=ssh >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port="$PROXY_PORT"/tcp >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port="$ADMIN_PORT"/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    log_info "firewalld 已配置（放行 22/tcp, $PROXY_PORT/tcp, $ADMIN_PORT/tcp）"
  else
    log_warn "未找到 ufw 或 firewalld，请手动配置防火墙：放行 22/tcp, $PROXY_PORT/tcp, $ADMIN_PORT/tcp"
  fi
}

# ===========================================================================
# systemd 服务
# ===========================================================================
setup_systemd() {
  log_step "安装 systemd 服务"
  local svc_file="/etc/systemd/system/${SERVICE_NAME}.service"
  cat > "$svc_file" <<EOF
[Unit]
Description=Zhongzhuan API Relay
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$PYTHON_BIN -m zhongzhuan
Restart=always
RestartSec=5
StandardOutput=append:/var/log/zhongzhuan.log
StandardError=append:/var/log/zhongzhuan.log

[Install]
WantedBy=multi-user.target
EOF
  log_info "服务文件: $svc_file"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  log_info "已设置开机自启"
}

# ===========================================================================
# 启动并验证
# ===========================================================================
start_and_verify() {
  log_step "启动服务并验证"

  if [[ "$SKIP_SYSTEMD" == "1" ]]; then
    # 不用 systemd：后台临时启动做健康检查，验证后保留运行（或交由用户管理）
    log_info "跳过 systemd，后台启动验证..."
    local pidfile="$INSTALL_DIR/.zhongzhuan.pid"
    # 先清理可能存在的旧进程
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
      kill "$(cat "$pidfile")" 2>/dev/null || true
      sleep 1
    fi
    # 后台启动，日志写文件
    cd "$INSTALL_DIR"
    PYTHONPATH="$INSTALL_DIR/src" \
      nohup "$PYTHON_BIN" -m zhongzhuan \
      > /tmp/zhongzhuan-deploy.log 2>&1 &
    echo $! > "$pidfile"
    disown || true
    log_info "后台启动 PID=$(cat "$pidfile" 2>/dev/null)，日志 /tmp/zhongzhuan-deploy.log"
  else
    log_info "重启 ${SERVICE_NAME}..."
    systemctl restart "$SERVICE_NAME" || die "服务启动失败，查看日志: journalctl -u $SERVICE_NAME -n 50"
  fi

  # 等待端口起来
  log_info "等待服务就绪..."
  local i
  for i in $(seq 1 15); do
    if curl -sk --max-time 2 "https://127.0.0.1:$PROXY_PORT/healthz" >/dev/null 2>&1; then
      log_info "服务已就绪（等待 ${i}s）"
      break
    fi
    sleep 1
    [[ $i -eq 15 ]] && {
      log_error "服务未在 15s 内就绪，最近日志："
      if [[ "$SKIP_SYSTEMD" == "1" ]]; then
        tail -n 30 /tmp/zhongzhuan-deploy.log 2>/dev/null || true
      else
        tail -n 30 /var/log/zhongzhuan.log 2>/dev/null || journalctl -u "$SERVICE_NAME" -n 30 --no-pager
      fi
      die "启动验证失败"
    }
  done

  # 健康检查
  local health
  health=$(curl -sk --max-time 5 "https://127.0.0.1:$PROXY_PORT/healthz" || true)
  log_info "健康检查: $health"

  # 状态
  if [[ "$SKIP_SYSTEMD" != "1" ]]; then
    systemctl --no-pager --lines=3 status "$SERVICE_NAME" 2>&1 | head -15 || true
  fi
}

# ===========================================================================
# 创建首个 access token（admin 本地 API）
# ===========================================================================
maybe_create_token() {
  log_step "尝试自动创建首个 access token"
  local admin_url="http://127.0.0.1:$ADMIN_PORT"
  local resp
  resp=$(curl -s --max-time 5 \
    -X POST "$admin_url/api/tokens" \
    -H "Content-Type: application/json" \
    -d '{"label":"deploy-script-auto"}' 2>/dev/null || true)

  if [[ -z "$resp" ]]; then
    log_warn "自动创建 token 失败（admin 启用了登录鉴权，需先登录拿 JWT）"
    log_note "请通过浏览器访问 admin 后台手动创建 token："
    log_note "  浏览器开: http://<VPS公网IP>:$ADMIN_PORT"
    log_note "  登录后到「令牌」页创建"
    return
  fi

  # 提取 token 字段
  local token
  token=$(echo "$resp" | "$PYTHON_BIN" -c "import sys, json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)
  if [[ -n "$token" ]]; then
    FINAL_ACCESS_TOKEN="$token"
    log_info "已创建 access token: ${token:0:12}...${token: -4}"
  else
    log_warn "响应中未找到 token 字段: $resp"
  fi
}

# ===========================================================================
# 输出客户端配置
# ===========================================================================
print_client_config() {
  log_step "Claude Code 客户端配置"

  echo
  echo -e "${BOLD}${GREEN}部署成功！${NC} Claude Code 客户端按以下配置接入："
  echo
  echo -e "${BOLD}代理地址:${NC} $FINAL_BASE_URL"
  echo -e "${BOLD}接入路径:${NC} $FINAL_BASE_URL/v1/messages"

  if [[ -n "$FINAL_ACCESS_TOKEN" ]]; then
    echo -e "${BOLD}access token:${NC} $FINAL_ACCESS_TOKEN"
  else
    echo -e "${BOLD}access token:${NC} <通过 admin 后台创建>"
  fi
  echo

  if [[ "$CERT_PATH" == "letsencrypt" ]]; then
    echo -e "${BOLD}客户端环境变量（Let's Encrypt 标准 CA，无需额外信任）:${NC}"
    echo "  export ANTHROPIC_BASE_URL=$FINAL_BASE_URL"
    echo "  export ANTHROPIC_API_KEY=$FINAL_ACCESS_TOKEN"
  else
    echo -e "${BOLD}客户端环境变量（自签 CA，需信任本地 CA）:${NC}"
    echo "  # 1. 先把 VPS 上的 CA 文件拷回本地："
    echo "  #    scp root@<VPS>:$CA_FILE ./local-ca.crt"
    echo "  # 2. 必须用绝对路径设置 NODE_EXTRA_CA_CERTS"
    echo "  export NODE_EXTRA_CA_CERTS=/本地绝对路径/local-ca.crt"
    echo "  export ANTHROPIC_BASE_URL=$FINAL_BASE_URL"
    echo "  export ANTHROPIC_API_KEY=$FINAL_ACCESS_TOKEN"
    echo
    echo -e "${YELLOW}注意:${NC}"
    echo "  - base_url 的主机名必须与证书 SAN 精确匹配（IP 证书用 IP，域名证书用域名）"
    echo "  - NODE_EXTRA_CA_CERTS 必须是绝对路径，相对路径在部分 Node 版本不生效"
    echo "  - 严禁使用 NODE_TLS_REJECT_UNAUTHORIZED=0（公网中间人风险）"
  fi

  echo
  echo -e "${BOLD}admin 后台访问:${NC}"
  echo "  浏览器直接开: http://<VPS公网IP>:$ADMIN_PORT"
  echo -e "  ${YELLOW}安全提示:${NC} admin 走明文 HTTP，登录密码会被嗅探。生产环境建议："
  echo "    - 用 SSH 隧道替代公网暴露: ssh -L $ADMIN_PORT:127.0.0.1:$ADMIN_PORT root@<VPS>"
  echo "    - 或用 nginx/caddy 反代 admin 走 HTTPS"
  echo "    - 确保已设置强密码（首次登录后到 /api/auth/change-password 修改）"
  echo
  echo -e "${BOLD}服务管理:${NC}"
  echo "  systemctl status $SERVICE_NAME"
  echo "  systemctl restart $SERVICE_NAME"
  echo "  tail -f /var/log/zhongzhuan.log"
  echo
  if [[ "$CERT_PATH" == "letsencrypt" ]]; then
    echo -e "${BOLD}证书续期:${NC}"
    echo "  certbot renew --deploy-hook \"systemctl restart $SERVICE_NAME\""
    echo "  或加 cron: 0 0,12 * * * certbot renew --deploy-hook \"systemctl restart $SERVICE_NAME\""
  fi
}

# ===========================================================================
# 主流程
# ===========================================================================
main() {
  parse_args "$@"
  require_root
  detect_os
  detect_python
  detect_install_dir

  if [[ "$SKIP_DEPS" != "1" ]]; then
    install_system_deps
    install_python_deps
  else
    log_info "跳过依赖安装（--skip-deps）"
  fi

  choose_cert_path
  if [[ "$CERT_PATH" == "letsencrypt" ]]; then
    setup_letsencrypt
  else
    setup_selfsign
  fi

  write_env_file
  [[ "$SKIP_FIREWALL" == "0" ]] && setup_firewall
  [[ "$SKIP_SYSTEMD" == "0" ]] && setup_systemd
  start_and_verify
  maybe_create_token
  print_client_config
}

main "$@"
