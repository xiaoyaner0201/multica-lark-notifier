#!/usr/bin/env bash
# 生成可上传到 Multica 的插件产物包。
#
# 为什么不提供预编译 release：Multica 在 manifest 校验阶段就要求
# transport.url 是带真实 hostname 的 HTTPS 地址，且 net: scope 的域名
# 要通过 netDomainPattern（必须含点、不接受占位符），运行时又是
# 精确字符串比较、无通配。所以域名无法留空后填——每个部署必须
# 打自己的包。这个脚本就是干这件事的。
#
# 用法：
#   ./scripts/build-bundle.sh notifier.yourcompany.com
#   ./scripts/build-bundle.sh notifier.yourcompany.com /custom/callback
set -euo pipefail

DOMAIN="${1:-}"
HOOK_PATH="${2:-/issue-callbacks}"

if [[ -z "$DOMAIN" ]]; then
    cat >&2 <<'USAGE'
用法: ./scripts/build-bundle.sh <域名> [回调路径]

示例:
  ./scripts/build-bundle.sh notifier.yourcompany.com
  ./scripts/build-bundle.sh notifier.yourcompany.com /hooks/multica

域名就是回调服务对外的 HTTPS 地址，不带协议前缀。
USAGE
    exit 2
fi

# 上游 netDomainPattern 要求至少一个点，且只允许 a-z0-9 与连字符。
# Docker 默认服务名（如 my-notifier）会在这里被拒，提前挡住比上传后被拒清楚。
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
    echo "错误: 域名 '$DOMAIN' 不符合 Multica 的 net scope 规则" >&2
    echo "      必须全小写、至少含一个点，例如 notifier.example.com" >&2
    echo "      （Docker 服务名这类无点主机名不被接受）" >&2
    exit 1
fi

if [[ "$DOMAIN" == http*://* ]]; then
    echo "错误: 只填域名，不要带 https:// 前缀" >&2
    exit 1
fi

if [[ "$HOOK_PATH" != /* ]]; then
    echo "错误: 回调路径必须以 / 开头，当前为 '$HOOK_PATH'" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/plugin/manifest.json"
OUT_DIR="$REPO_ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

[[ -f "$SRC" ]] || { echo "错误: 找不到 $SRC" >&2; exit 1; }

VERSION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$SRC")"
BUNDLE="$OUT_DIR/lark-status-notifier-${VERSION}.zip"

# 文件名必须是 multica.plugin.json —— 上游 ParseBundle 按这个常量找，
# 叫 manifest.json 会被当成没有 manifest 而拒绝。
python3 - "$SRC" "$STAGE/multica.plugin.json" "$DOMAIN" "$HOOK_PATH" <<'PY'
import json, sys

src, dst, domain, hook_path = sys.argv[1:5]
m = json.load(open(src, encoding="utf-8"))

# 三处必须逐字一致，否则运行时 hostInNetScopes 精确比较会失败。
m["scopes"] = [s for s in m["scopes"] if not s.startswith("net:")]
m["scopes"].append(f"net:{domain}")
m["contributes"]["hooks"][0]["transport"]["url"] = f"https://{domain}{hook_path}"

json.dump(m, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

mkdir -p "$OUT_DIR"
rm -f "$BUNDLE"
# -X 去掉时间戳等额外属性，让相同输入产出字节一致的包，便于校验。
(cd "$STAGE" && zip -q -X "$BUNDLE" multica.plugin.json)

echo "已生成 $BUNDLE"
echo
echo "包内容（只有一个声明文件，不含任何密钥或服务代码）："
unzip -l "$BUNDLE" | sed -n '4,$p' | head -3
echo
echo "回调地址: https://${DOMAIN}${HOOK_PATH}"
echo "net scope: net:${DOMAIN}"
echo
echo "下一步："
echo "  1. Multica → 设置 → 插件 → 上传产物包，选择上面的 zip"
echo "  2. 在「发布插件」里点安装，确认 issues:read 与 net:${DOMAIN} 两个权限"
echo "  3. 记下安装时展示的 whsec_ 密钥，填入回调服务的 .env"
echo "  4. 确认 backend 的 MULTICA_PLUGIN_DEV_ORIGINS 含 https://${DOMAIN}"
