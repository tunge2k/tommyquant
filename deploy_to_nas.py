"""
部署脚本 —— 把项目部署到 iStoreOS NAS 的 Docker 容器里

用法: python deploy_to_nas.py
"""
import sys
import os
import time

# 先把 deploy_libs 加到 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "deploy_libs"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "deploy_libs", "scp-0.16.2"))

import paramiko
from scp import SCPClient

# ============ NAS 配置 (私密信息从 nas_conf.py / 环境变量读取) ============
try:
    import nas_conf
    NAS_HOST = nas_conf.NAS_HOST
    NAS_PORT = nas_conf.NAS_PORT
    NAS_USER = nas_conf.NAS_USER
    NAS_PASS = nas_conf.NAS_PASSWORD
except ImportError:
    NAS_HOST = os.environ["NAS_HOST"]
    NAS_PORT = int(os.environ.get("NAS_PORT", 22))
    NAS_USER = os.environ.get("NAS_USER", "root")
    NAS_PASS = os.environ["NAS_PASSWORD"]

# 远端路径
REMOTE_DIR = "/volume1/docker/limit-up-hunter"
REMOTE_CACHE = f"{REMOTE_DIR}/cache"

# 本地项目目录
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_FILES = [
    "streamlit_app.py",
    "backtest_engine.py",
    "fast_backtest.py",
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
]

# ============ 连接 ============
print("=" * 60)
print("🚀 涨停猎手 Pro —— NAS Docker 部署")
print("=" * 60)

print(f"\n[1/5] 连接 NAS ({NAS_USER}@{NAS_HOST}:{NAS_PORT})...")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

try:
    ssh.connect(NAS_HOST, port=NAS_PORT, username=NAS_USER, password=NAS_PASS,
                timeout=15, banner_timeout=15, auth_timeout=15)
    print("  ✅ SSH 连接成功")
except Exception as e:
    print(f"  ❌ SSH 连接失败: {e}")
    print("  请检查:")
    print("    1. NAS 和本机在同一局域网")
    print("    2. iStoreOS 已启用 SSH 服务")
    print("    3. IP 地址和密码是否正确")
    sys.exit(1)

# ============ 检查 Docker ============
print("\n[2/5] 检查 NAS Docker 环境...")
stdin, stdout, stderr = ssh.exec_command("docker --version && docker compose version 2>/dev/null || docker-compose --version")
out = stdout.read().decode().strip()
err = stderr.read().decode().strip()

if out:
    print(f"  ✅ Docker 可用:\n     {out.splitlines()[0]}\n     {out.splitlines()[-1]}")
else:
    print(f"  ❌ Docker 不可用")
    print(f"     错误: {err}")
    print("  请在 NAS 应用中心安装 Docker")
    ssh.close()
    sys.exit(1)

# 检查端口
stdin, stdout, _ = ssh.exec_command("ss -tlnp | grep 8501 || echo '8501 free'")
port_status = stdout.read().decode().strip()
if "8501 free" in port_status or "8501" not in port_status:
    print("  ✅ 端口 8501 空闲")
else:
    print(f"  ⚠️  端口 8501 已被占用: {port_status}")

# ============ 上传项目 ============
print(f"\n[3/5] 创建远端目录 {REMOTE_DIR}...")
ssh.exec_command(f"mkdir -p {REMOTE_DIR} {REMOTE_CACHE}")
time.sleep(1)

print("  📤 上传项目文件...")
scp = SCPClient(ssh.get_transport())

for fname in LOCAL_FILES:
    local_path = os.path.join(LOCAL_DIR, fname)
    if os.path.exists(local_path):
        scp.put(local_path, f"{REMOTE_DIR}/{fname}")
        print(f"    ✅ {fname}")
    else:
        print(f"    ⚠️  {fname} 不存在，跳过")

# 上传 cache 目录（如果存在）
local_cache = os.path.join(LOCAL_DIR, "cache")
if os.path.isdir(local_cache):
    print("  📤 上传日线缓存 (可能较慢)...")
    cache_files = [f for f in os.listdir(local_cache) if f.endswith(".csv")]
    print(f"     {len(cache_files)} 个 CSV 文件")
    for i, fname in enumerate(cache_files):
        scp.put(os.path.join(local_cache, fname), f"{REMOTE_CACHE}/{fname}")
        if (i + 1) % 50 == 0:
            print(f"     进度 {i+1}/{len(cache_files)}")
    print("    ✅ 缓存上传完成")

scp.close()

# ============ 构建 & 启动 ============
print(f"\n[4/5] 在 NAS 上构建并启动容器...")

# 先停旧容器
print("  🔄 停止旧容器 (如果有)...")
ssh.exec_command(f"cd {REMOTE_DIR} && docker compose down 2>/dev/null")
time.sleep(2)

# 构建并启动
print("  🔨 docker compose up -d --build")
stdin, stdout, stderr = ssh.exec_command(
    f"cd {REMOTE_DIR} && docker compose up -d --build 2>&1",
    timeout=300
)

# 实时输出
print("  构建日志:")
while True:
    line = stdout.readline()
    if not line:
        break
    print(f"    {line.rstrip()}")
print(stderr.read().decode())

# 等容器启动
time.sleep(5)

# ============ 验证 ============
print("\n[5/5] 验证部署结果...")
stdin, stdout, _ = ssh.exec_command(f"cd {REMOTE_DIR} && docker compose ps")
ps_out = stdout.read().decode().strip()
print(f"  容器状态:\n     {ps_out}")

# 健康检查
time.sleep(3)
stdin, stdout, stderr = ssh.exec_command(
    f"curl -sf http://localhost:8501/_stcore/health 2>&1 || echo 'health_check_failed'"
)
health = stdout.read().decode().strip()
if health == "ok":
    print("  ✅ Streamlit 健康检查通过!")
else:
    print(f"  ⚠️  健康检查: {health}")
    # 看日志
    stdin, stdout, _ = ssh.exec_command(f"cd {REMOTE_DIR} && docker compose logs --tail=20")
    print(f"  最近日志:\n     {stdout.read().decode()}")

ssh.close()

print("\n" + "=" * 60)
print("🎉 部署完成!")
print("=" * 60)
print(f"""
  🌐 访问地址: http://{NAS_HOST}:8501
  
  📋 常用命令 (SSH 到 NAS):
    cd {REMOTE_DIR}
    docker compose ps              # 查看状态
    docker compose logs -f         # 实时日志
    docker compose restart         # 重启
    docker compose down            # 停止
    docker compose pull && docker compose up -d  # 更新

  💾 数据持久化:
    缓存目录挂在 ./cache → 容器重建不丢数据
""")
