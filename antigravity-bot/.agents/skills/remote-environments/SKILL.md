---
name: remote-environments
description: Lists configured SSH environments, exposes the bot public key, and runs an explicitly requested command on a named remote machine. Use only when the user asks to inspect or operate a configured remote environment.
---

# Remote Environments

Use the bot-owned SSH broker instead of reading credentials or opening an unrelated
direct SSH session. The runner provides `AGY_BOT_ROOT` and `AGY_BOT_PYTHON`.

## Safety rules

1. Use remote access only for a request that explicitly requires it.
2. Never guess an environment name. Run `list` or ask the user when it is unclear.
3. Pass the environment, command, and working directory as separate shell arguments.
4. Do not reveal private keys, tokens, passwords, or secret file contents.
5. Never shut down a machine unless the user directly asks to shut it down.
6. Report the remote exit code and any relevant error; do not claim success on failure.

## Commands

```bash
PYTHONPATH="$AGY_BOT_ROOT" "$AGY_BOT_PYTHON" -m bot.services.ssh_tool list
PYTHONPATH="$AGY_BOT_ROOT" "$AGY_BOT_PYTHON" -m bot.services.ssh_tool pubkey
PYTHONPATH="$AGY_BOT_ROOT" "$AGY_BOT_PYTHON" -m bot.services.ssh_tool exec "environment name" "command" --cwd "/remote/path"
```

For an explicitly requested soft shutdown:

```bash
PYTHONPATH="$AGY_BOT_ROOT" "$AGY_BOT_PYTHON" -m bot.services.power_tool shutdown "environment name"
```
