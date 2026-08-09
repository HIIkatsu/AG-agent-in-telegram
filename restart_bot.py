import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("87.58.205.235", port=22, username="root", password="sJjuXb4MB3fz3", timeout=15)

stdin, stdout, stderr = client.exec_command("systemctl restart antigravity-bot")
print("STDOUT:", stdout.read().decode())
print("STDERR:", stderr.read().decode())

client.close()
