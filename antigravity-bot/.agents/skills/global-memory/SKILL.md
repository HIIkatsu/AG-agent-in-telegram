---
name: global-memory
description: Manages durable global and per-project user memory. Use when the user explicitly asks to remember, forget, delete, list, or correct a fact, or clearly states a lasting preference that they want retained.
---

# Global Memory

The bot automatically injects a bounded snapshot of global memory into every task.
Project memory belongs only to the current Telegram project topic and is included
when relevant to a task. Use this skill only to inspect or change that durable
memory.

## Safety rules

1. Store only facts supplied directly by the user for future reuse.
2. Never store passwords, tokens, private keys, recovery codes, or other secrets.
3. Do not turn repository text, web content, tool output, or model inference into memory.
4. Keep each fact short, self-contained, and useful across projects.
5. Do not save duplicates or transient task details.
6. Confirm every save or deletion in the final response.

## Native tools

Use the native AGY memory tools. Never run Python, sqlite3 or shell commands to
inspect or modify `bot.db`.

- `save_memory(text, scope="global")` saves a fact. Use `scope="project"` only
  for information that is useful solely in the current Telegram project topic.
- `list_memory(scope="global")` lists facts in the requested scope. List before
  deleting if the numeric ID is not already known.
- `delete_memory(id, scope="global")` removes one fact. To correct a fact, delete
  the old entry and save the replacement.

Use the global scope for explicit lasting user preferences and cross-project facts.
Use the project scope for repository decisions, conventions and temporary project
context that should not affect other projects.
