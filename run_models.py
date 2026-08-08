import subprocess
r = subprocess.run(["/root/.local/bin/agy", "models"], capture_output=True, text=True)
print(r.stdout)
