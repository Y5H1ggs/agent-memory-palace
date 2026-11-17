#!/bin/sh
# 安装仓库自带的 git 钩子到 .git/hooks/。
# 只在本地生效（.git/ 不进版本库），所以每个 clone 都要跑一次：
#   sh tools/git-hooks/install.sh

set -e
cd "$(dirname "$0")/../.."

if [ ! -d .git ]; then
    echo "不在 git 仓库根目录，退出" >&2
    exit 1
fi

cp tools/git-hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
echo "✓ 已安装 pre-commit：提交前会检查评审包是否最新"
echo "  绕过：git commit --no-verify"
