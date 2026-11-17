# -*- coding: utf-8 -*-
"""按版本号备份到 GitHub

用法:
    py -3.13 git_release.py 1.1.0 "新增涨跌停不可成交建模"
    py -3.13 git_release.py 1.1.1 "修复: 成交流水原因显示"

流程: git add -A → commit → 打 tag v<版本号> → push main 与 tags
令牌复用 origin 远端里已有的凭据 (首次推送时配置), 不需要每次输入。
"""
import subprocess
import sys


def run(*args):
    """执行 git 命令并回显输出, 失败时终止"""
    p = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    if out.strip():
        print(out.strip())
    if p.returncode != 0:
        sys.exit(f"❌ git {' '.join(args)} 失败")
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)

    version = sys.argv[1].lstrip("v")
    message = sys.argv[2]
    tag = f"v{version}"

    # 版本号格式校验
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        sys.exit(f"❌ 版本号需为 MAJOR.MINOR.PATCH, 收到: {version}")

    # 确认不是重复标签
    existing = run("tag", "-l", tag)
    if existing.strip() == tag:
        sys.exit(f"❌ 标签 {tag} 已存在")

    print(f"\n📦 发布 {tag}: {message}\n")
    run("add", "-A")
    run("commit", "-m", message)
    run("tag", "-a", tag, "-m", f"{tag} {message}")
    run("push", "origin", "main")
    run("push", "origin", tag)

    print(f"\n✅ 已发布 {tag}")
    print("🌐 https://github.com/tunge2k/tommyquant/releases")


if __name__ == "__main__":
    main()
