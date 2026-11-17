# -*- coding: utf-8 -*-
"""临时: 推送执行"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_libs"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_libs", "scp-0.16.2"))
import paramiko
from scp import SCPClient

NAME = sys.argv[1]
EXTRAS = sys.argv[2:]      # 额外只同步、不执行的依赖文件
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
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(NAS["host"], port=NAS["port"], username=NAS["user"],
            password=NAS["password"], timeout=20)


def run(cmd, timeout=900):
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read().decode(errors="replace") + err.read().decode(errors="replace")).strip()


scp = SCPClient(ssh.get_transport())
for f in [NAME] + EXTRAS:
    scp.put(f, f"{REMOTE_DIR}/{f}")
scp.close()
for f in [NAME] + EXTRAS:
    run(f"docker cp {REMOTE_DIR}/{f} tommyquant:/app/{f}")
print(run(f"docker exec -w /app tommyquant python {NAME}", timeout=900))
run(f"docker exec tommyquant rm -f /app/{NAME}")
for f in [NAME] + EXTRAS:
    run(f"rm -f {REMOTE_DIR}/{f}")
ssh.close()
