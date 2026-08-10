"""Agy CLI runner with error surface detection, STDIN permission confirmations, backups, timeouts, and Russian language enforcement."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot

from bot.config import settings
from bot.services.permissions import permission_handler
from bot.services.tracker import TaskTracker
from bot.utils.sanitizer import IncrementalStreamDecoder

logger = logging.getLogger(__name__)

# System rules written to .agents/AGENTS.md in workdir — CLI reads this
# as workspace-scoped rules natively, without polluting conversation history.
_AGENTS_MD_CONTENT = """\
# Antigravity Bot — Workspace Rules

## Language
- Отвечай строго на русском языке.
- Всегда отвечай кратко и по делу.

## STRICT PROHIBITIONS
- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО создавать файлы, изображения, скрипты или любые артефакты
  если пользователь ЯВНО об этом не попросил.
- ЗАПРЕЩЕНО использовать инструмент generate_image без прямой просьбы пользователя.
- ЗАПРЕЩЕНО создавать файлы "для примера" или "для демонстрации" — отвечай текстом.
- ЗАПРЕЩЕНО упоминать или выводить пользователю технические пути системных папок
  (вроде /tmp/workspaces/..., /root/... или scratch).
  Просто подтверждай создание и описывай реализованный функционал.

## Behavior
- Если пользователь задаёт вопрос — отвечай текстом, НЕ создавая файлы.
- Создавай файлы ТОЛЬКО когда пользователь явно просит создать/написать/сделать файл.
- Все файлы создавай и редактируй в своей текущей рабочей директории.

## Formatting
- Форматируй ответы для удобного чтения в Telegram.
- Не выводи raw-markdown для блоков кода длиннее 30 строк — лучше опиши что сделано.
"""

_WEB_SEARCH_RULE = """
## Web Search
- Обязательно используй веб-поиск или спарси нужную страницу
  через bash/curl перед формулированием ответа.
"""


def _ensure_agents_md(workspace_dir: str, mode: str = "code", web_search: str = "off") -> None:
    """Write/update .agents/AGENTS.md in workspace so CLI reads rules natively."""
    agents_dir = Path(workspace_dir) / ".agents"
    agents_dir.mkdir(exist_ok=True)
    rules_path = agents_dir / "AGENTS.md"
    
    from bot.modes import get_mode_config
    cfg = get_mode_config(mode)
    
    content = _AGENTS_MD_CONTENT
    content += f"\n## Mode: {cfg['name']}\n- {cfg['prompt']}\n"
    
    if web_search in ("auto", "required"):
        content += _WEB_SEARCH_RULE
    try:
        # Only rewrite if content changed (avoid unnecessary disk writes)
        if rules_path.exists() and rules_path.read_text(encoding="utf-8") == content:
            return
        rules_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write AGENTS.md: %s", exc)


async def run_agy(
    prompt: str,
    conversation_id: str,
    workspace_dir: str,
    on_chunk: Callable[[str], Awaitable[None]],
    bot: Bot,
    chat_id: int,
    tracker: TaskTracker | None = None,
    web_search: str = "off",
    model: str = "",
    mode: str = "code",
    thread_id: int | None = None,
) -> str:
    """Execute agy CLI process with 5-minute timeout, STDIN permissions, backups, error capture, and Russian language enforcement."""
    os.makedirs(workspace_dir, exist_ok=True)
    _ensure_agents_md(workspace_dir, mode=mode, web_search=web_search)

    # CLEAN prompt — no [SYSTEM:] injection. Rules come from .agents/AGENTS.md
    full_prompt = prompt

    cmd = [
        settings.agy_path,
        "--print", full_prompt,
        "--project", conversation_id,
        "--continue",
        "--add-dir", workspace_dir,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--print-timeout", settings.agy_print_timeout,
    ]

    # Model passed as exact CLI string name, e.g. "Claude Sonnet 4.6 (Thinking)"
    if model:
        cmd.extend(["--model", model])

    logger.info("agy start: conv=%s model='%s'", conversation_id, model or "default")

    project_id_str = ""
    if thread_id is not None:
        from bot.db import db
        session = await db.get_session(thread_id)
        if session and session.get("project_id"):
            project_id_str = str(session["project_id"])

    env = {
        **os.environ,
        "TERM": "dumb", "NO_COLOR": "1",
        "LANG": "ru_RU.UTF-8", "LC_ALL": "ru_RU.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "AGY_TG_THREAD_ID": str(thread_id) if thread_id is not None else "",
        "AGY_TG_PROJECT_ID": project_id_str,
    }

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace_dir,
        env=env,
    )

    decoder = IncrementalStreamDecoder("utf-8", errors="replace")
    full_response = ""
    stderr_response = ""
    line_buffer = ""

    async def _read_stderr():
        nonlocal stderr_response
        if proc.stderr:
            err_bytes = await proc.stderr.read()
            if err_bytes:
                stderr_response = err_bytes.decode("utf-8", errors="ignore")

    stderr_task = asyncio.create_task(_read_stderr())

    try:
        async with asyncio.timeout(settings.task_timeout_seconds):
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(1024)
                if not chunk:
                    break

                text = decoder.decode(chunk, final=False)
                line_buffer += text

                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        full_response += line + "\n"
                        await on_chunk(line + "\n")
                        continue

                    event = data.get("event", "")

                    if event == "step_update":
                        step = data.get("step_update", {})
                        step_type = step.get("step_type", "")
                        state = step.get("state", "")
                        tool_name = step.get("tool_name", "")
                        tool_info = step.get("tool_info", {})
                        params = tool_info.get("parameters", {}) if isinstance(tool_info, dict) else {}

                        # 1. Text delta
                        delta = step.get("text_delta")
                        if isinstance(delta, str) and delta:
                            full_response += delta
                            await on_chunk(delta)

                        # 2. Handle error step types
                        if step_type == "error_message":
                            err_text = step.get("error_message", step.get("text_delta", ""))
                            if err_text:
                                err_formatted = f"\n\n❌ <b>Ошибка CLI:</b>\n<pre><code>{err_text}</code></pre>"
                                full_response += err_formatted
                                await on_chunk(err_formatted)

                        # 3. Tool Lifecycle and Permission Handler
                        if step_type == "tool" and tool_name:
                            label = _tool_label(tool_name, params)

                            if state == "ACTIVE":
                                if tracker:
                                    await tracker.on_tool_start(tool_name, label)

                                approved = await permission_handler.handle_permission(
                                    bot=bot,
                                    chat_id=chat_id,
                                    tool_name=tool_name,
                                    parameters=params,
                                    proc=proc,
                                    thread_id=thread_id,
                                )
                                if not approved:
                                    logger.warning("Tool execution denied by user: %s", tool_name)
                                    if tracker:
                                        await tracker.on_tool_end(tool_name, "DENIED")
                                    return full_response + "\n\n❌ Действие отклонено пользователем."

                            elif state in ("DONE", "ERROR"):
                                if tracker:
                                    await tracker.on_tool_end(tool_name, state)
                                
                                if state == "ERROR" and isinstance(tool_info, dict):
                                    tool_err = tool_info.get("error", {})
                                    err_msg = tool_err.get("message", str(tool_err)) if isinstance(tool_err, dict) else str(tool_err)
                                    if err_msg and "User denied permission" not in err_msg and "Permission denied" not in err_msg:
                                        err_formatted = f"\n\n❌ **Ошибка инструмента ({tool_name}):**\n```\n{err_msg}\n```"
                                        full_response += err_formatted
                                        await on_chunk(err_formatted)

                    elif event == "result":
                        result = data.get("result", {})
                        status = result.get("status", "")
                        result_err = result.get("error", "")
                        response = result.get("response", "")

                        if status == "ERROR":
                            err_text = result_err or 'Превышен лимит сообщений или ошибка модели.'
                            
                            is_limit_error = any(kw in err_text.lower() for kw in ("limit", "token", "quota", "rate", "превышен", "ошибка модели"))
                            has_response = isinstance(response, str) and len(response) > 20
                            
                            if is_limit_error or not has_response:
                                err_formatted = f"\n\n❌ **Ошибка выполнения / Лимиты:**\n```\n{err_text}\n```"
                                full_response += err_formatted
                                await on_chunk(err_formatted)
                        
                        if isinstance(response, str) and response:
                            remainder = response[len(full_response):]
                            if remainder:
                                full_response += remainder
                                await on_chunk(remainder)

                        if proc.stdin and not proc.stdin.is_closing():
                            try:
                                proc.stdin.close()
                            except Exception:
                                pass
                        break

            # Flush decoder remainder strictly after while loop
            remainder = decoder.decode(b"", final=True)
            if remainder:
                line_buffer += remainder
                if line_buffer.strip():
                    full_response += line_buffer

            await proc.wait()
            await stderr_task

            if proc.returncode and proc.returncode != 0:
                logger.warning("agy process exited with code %d", proc.returncode)
                if stderr_response.strip() and "Ошибка" not in full_response:
                    err_msg = f"\n\n❌ **Ошибка выполнения:**\n```\n{stderr_response.strip()[:1000]}\n```"
                    full_response += err_msg
                    await on_chunk(err_msg)

    except TimeoutError:
        logger.error("agy execution timed out (%ds limit)", settings.task_timeout_seconds)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        minutes = settings.task_timeout_seconds // 60
        timeout_msg = f"\n\n❌ **Ошибка выполнения:**\n```\nПревышен таймаут выполнения задачи ({minutes} минут). Процесс принудительно остановлен.\n```"
        full_response += timeout_msg
        await on_chunk(timeout_msg)
    except asyncio.CancelledError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.exception("agy execution error")
        err_msg = f"\n\n❌ **Ошибка выполнения:**\n```\n{exc}\n```"
        full_response += err_msg
        await on_chunk(err_msg)

    logger.info("agy finished: %d chars response", len(full_response))
    return full_response


def _tool_label(tool_name: str, parameters: dict) -> str:
    # Use native agent toolAction or toolSummary if available
    action = parameters.get("toolAction")
    if action:
        # Append file name if relevant
        file_param = parameters.get("TargetFile") or parameters.get("AbsolutePath")
        if file_param:
            return f"{action}: {os.path.basename(str(file_param))}"
        return str(action)
        
    labels = {
        "run_command": f"Выполнение: {parameters.get('CommandLine', '')[:50]}",
        "write_to_file": f"Запись файла: {os.path.basename(str(parameters.get('TargetFile', '')))}",
        "replace_file_content": f"Правка файла: {os.path.basename(str(parameters.get('TargetFile', '')))}",
        "multi_replace_file_content": f"Правка файла: {os.path.basename(str(parameters.get('TargetFile', '')))}",
        "view_file": f"Чтение файла: {os.path.basename(str(parameters.get('AbsolutePath', '')))}",
        "list_dir": "Чтение директории",
        "grep_search": f"Поиск: {parameters.get('Query', '')[:30]}",
        "search_web": f"Поиск в вебе: {parameters.get('query', '')[:30]}",
        "read_url_content": "Чтение веб-страницы",
        "generate_image": "Генерация изображения",
    }
    return labels.get(tool_name, tool_name)
