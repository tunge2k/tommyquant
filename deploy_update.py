"""
更新部署到 iStoreOS NAS (增量: 只传改动文件 + rebuild + 验证)
用法: py -3.13 deploy_update.py
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_libs"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_libs", "scp-0.16.2"))
import paramiko
from scp import SCPClient

try:                                   # 本机私密配置 (不入库)
    import nas_conf
    NAS = dict(host=nas_conf.NAS_HOST, port=nas_conf.NAS_PORT,
               user=nas_conf.NAS_USER, password=nas_conf.NAS_PASSWORD)
except ImportError:                    # 或者走环境变量
    NAS = dict(host=os.environ["NAS_HOST"],
               port=int(os.environ.get("NAS_PORT", 22)),
               user=os.environ.get("NAS_USER", "root"),
               password=os.environ["NAS_PASSWORD"])
REMOTE_DIR = "/volume1/docker/limit-up-hunter"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

FILES = ["streamlit_app.py", "backtest_engine.py", "fast_backtest.py",
         "price_limits.py", "robust_backtest.py",
         "data_store.py", "data_manager.py", "perf_report.py",
         "news_fetcher.py", "update_worker.py", "doh_patch.py",
         "htbr_source.py", "exchange_source.py",
         "world_map_data.py",
         "entrypoint.sh",
         "Dockerfile", "docker-compose.yml", "requirements.txt"]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(NAS["host"], port=NAS["port"], username=NAS["user"],
            password=NAS["password"], timeout=15)


def run(cmd, timeout=15):
    try:
        _, out, err = ssh.exec_command(cmd, timeout=timeout)
        return (out.read().decode(errors="replace") + err.read().decode(errors="replace")).strip()
    except Exception:
        return "(timeout)"


# 仅验证模式: py deploy_update.py --check
if "--check" in sys.argv:
    print("=== 验证模式 (不部署) ===")
    print("  容器:", run("docker ps --filter name=tommyquant --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'", timeout=10))
    print("  健康:", run("curl -sf http://localhost:8501/_stcore/health 2>&1", timeout=10))
    print("  Worker日志:", run("docker logs tommyquant 2>&1 | tail -8", timeout=10))
    ssh.close()
    sys.exit(0)

print("=== 1. 上传更新文件 ===")
scp = SCPClient(ssh.get_transport())
for f in FILES:
    p = os.path.join(LOCAL_DIR, f)
    if os.path.exists(p):
        scp.put(p, f"{REMOTE_DIR}/{f}")
        print(f"  ✅ {f}")
# streamlit 主题配置 (需建目录)
cfg = os.path.join(LOCAL_DIR, ".streamlit", "config.toml")
if os.path.exists(cfg):
    run(f"mkdir -p {REMOTE_DIR}/.streamlit")
    scp.put(cfg, f"{REMOTE_DIR}/.streamlit/config.toml")
    print("  ✅ .streamlit/config.toml")

# 华泰柏瑞技能运行时 (ETF 增强数据源): 只传代码/文档/配置, 不传 __pycache__
vendor_src = os.path.join(LOCAL_DIR, "vendor", "htbr")
if os.path.isdir(vendor_src):
    for root, dirs, files in os.walk(vendor_src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, vendor_src).replace("\\", "/")
        remote = f"{REMOTE_DIR}/vendor/htbr" + ("" if rel == "." else "/" + rel)
        run(f"mkdir -p {remote}")
        for f in files:
            if f.endswith((".py", ".md", ".json")):
                scp.put(os.path.join(root, f), f"{remote}/{f}")
    print("  ✅ vendor/htbr (技能运行时)")

# 凭据: 只读挂载到容器 /root/.htbr, 不进镜像与版本库
cred_local = os.path.join(os.path.expanduser("~"), ".htbr", "credentials.json")
if os.path.exists(cred_local):
    run(f"mkdir -p {REMOTE_DIR}/secrets")
    scp.put(cred_local, f"{REMOTE_DIR}/secrets/credentials.json")
    run(f"chmod 600 {REMOTE_DIR}/secrets/credentials.json")
    print("  ✅ secrets/credentials.json (只读挂载)")
else:
    print("  ⚠ 未找到本机 ~/.htbr/credentials.json, 跳过凭据同步")

scp.close()

print("\n=== 2. 后台重建容器 ===")
# 记录旧容器 ID: 重建脚本是后台跑的, 旧容器在构建期间仍然 healthy,
# 必须等容器 ID 变化, 否则会把旧容器的健康检查当成部署成功
old_id = run("docker inspect -f '{{.Id}}' tommyquant 2>/dev/null", timeout=10)
run(f"cd {REMOTE_DIR} && docker compose up -d --build --force-recreate > /tmp/deploy.log 2>&1 &")
time.sleep(3)

print("\n=== 3. 等待构建/启动 ===")
for i in range(40):
    time.sleep(10)
    new_id = run("docker inspect -f '{{.Id}}' tommyquant 2>/dev/null", timeout=8)
    hc = run("curl -sf http://localhost:8501/_stcore/health 2>&1", timeout=8)
    running = run("docker ps --filter name=tommyquant --format '{{.Status}}'", timeout=8)
    replaced = bool(new_id) and new_id != old_id
    print(f"  [{(i+1)*10:>3}s] replaced={replaced} status={running or '...'} health={hc or '...'}")
    if replaced and hc == "ok":
        break

print("\n=== 4. 最终验证 ===")
print("  容器:", run("docker ps --filter name=tommyquant --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'", timeout=10))
hc = run("curl -sf http://localhost:8501/_stcore/health 2>&1", timeout=10)
print("  健康:", hc)
if hc != "ok":
    print("\n  最近日志:")
    for line in run("tail -20 /tmp/deploy.log 2>/dev/null", timeout=10).splitlines():
        print("   ", line)

ssh.close()
print(f"\n🌐 http://{NAS['host']}:8501")
