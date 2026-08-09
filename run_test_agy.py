import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("87.58.205.235", port=22, username="root", password="sJjuXb4MB3fz3", timeout=15)

script = """
import json, subprocess
proc=subprocess.run(['/opt/antigravity-bot/venv/bin/agy', '--print', 'write hello to text.txt and run ls', '--output-format', 'stream-json'], capture_output=True, text=True)
for line in proc.stdout.split('\\n'):
    if line.strip():
        data = json.loads(line)
        event = data.get('event')
        if event == 'step_update':
            step = data.get('step_update', {})
            print(f"step_update: type={step.get('step_type')} state={step.get('state')} tool={step.get('tool_name')}")
"""

stdin, stdout, stderr = client.exec_command(f'python3 -c "{script}"')
print("STDOUT:", stdout.read().decode())
print("STDERR:", stderr.read().decode())
client.close()
