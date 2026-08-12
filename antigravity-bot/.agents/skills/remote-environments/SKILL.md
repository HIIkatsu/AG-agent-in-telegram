---
name: remote-environments
description: Lists configured SSH environments, exposes the bot public key, and runs an explicitly requested command on a named remote machine. Use only when the user asks to inspect or operate a configured remote environment.
---

# Remote Environments

Use the task-scoped `ag-ssh` capability broker instead of reading credentials or
opening an unrelated direct SSH session. The sandbox never exposes the bot source,
database or private SSH keys to the agent.

## Safety rules

1. Use remote access only for a request that explicitly requires it.
2. Never guess an environment name. Run `list` or ask the user when it is unclear.
3. Pass the environment, command, and working directory as separate shell arguments.
4. Do not reveal private keys, tokens, passwords, or secret file contents.
5. Never shut down a machine unless the user directly asks to shut it down.
6. Every remote command requires a Telegram confirmation from the user. Wait for
   that confirmation; do not claim success on failure or denial.
7. Report the remote exit code and any relevant error; do not claim success on failure.

## Commands

```bash
ag-ssh list
ag-ssh pubkey
ag-ssh exec "environment name" "command" --cwd "/remote/path"
```

For an explicitly requested soft shutdown:

```bash
ag-ssh exec "environment name" "sudo shutdown -h now"
```
