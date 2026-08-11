"""IDE workflow commands: queue/status/search/context/memory/diff/test/run."""

from __future__ import annotations

import asyncio
import html
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.db import db
from bot.services.git_manager import git_manager
from bot.utils.telegram_renderer import render_markdown, strip_telegram_html

router = Router(name="ide")

IGNORED_RG = [".git", "node_modules", ".venv", "venv", "dist", "build", ".cache", "__pycache__", ".agents"]


def _thread_id(message: Message) -> int | None:
    return message.message_thread_id


def _safe_relpath(ws: str, raw: str) -> str | None:
    root = Path(ws).resolve()
    target = (root / raw).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return None


def _task_line(row: dict) -> str:
    status_icon = {
        "queued": "⏳",
        "running": "▶️",
        "done": "✅",
        "failed": "❌",
        "cancelled": "⏹",
        "timeout": "⏱",
        "interrupted": "⚠️",
    }.get(row.get("status"), "•")
    prompt = html.escape((row.get("prompt") or "")[:80])
    return f"{status_icon} <b>#{row['id']}</b> <code>{html.escape(row.get('status', ''))}</code> — {prompt}"


async def build_task_card(task_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    from bot.services.task_service import get_task
    from bot.services.task_workspace import task_workspace_manager

    task = await get_task(task_id)
    if not task:
        return "Задача не найдена.", None

    cur = await db.conn.execute("SELECT * FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 12", (task_id,))
    logs = [dict(r) for r in await cur.fetchall()]
    lines = [
        f"🧠 <b>Задача #{task['id']}</b>",
        f"Статус: <code>{html.escape(task['status'])}</code>",
        f"Режим: <i>{html.escape(task.get('mode') or 'code')}</i>",
        f"Модель: <i>{html.escape(task.get('model') or 'default')}</i>",
        "",
        "<b>Prompt:</b>",
        html.escape((task.get("prompt") or "")[:900]),
    ]
    if task.get("error"):
        lines += ["", "<b>Ошибка:</b>", f"<code>{html.escape(task['error'])}</code>"]
    if task.get("result_summary"):
        lines += ["", "<b>Результат:</b>", html.escape(task["result_summary"][:900])]
    workspace = await task_workspace_manager.get(task_id)
    if workspace:
        lines += ["", f"Изменения: <code>{html.escape(workspace.state)}</code>"]
        if workspace.error:
            lines.append(f"<code>{html.escape(workspace.error[:500])}</code>")
    if logs:
        lines += ["", "<b>Последние события:</b>"]
        for row in reversed(logs):
            lines.append(f"• <code>{html.escape(row['level'])}</code> {html.escape(row['message'][:180])}")

    buttons = [[
        InlineKeyboardButton(text="📄 Логи", callback_data=f"t:lg:{task_id}"),
        InlineKeyboardButton(text="🔁 Retry", callback_data=f"t:rt:{task_id}"),
    ]]
    if workspace and workspace.state in {"pending", "conflict"}:
        buttons.append([
            InlineKeyboardButton(text="👀 Diff", callback_data=f"t:df:{task_id}"),
            InlineKeyboardButton(text="✅ Применить", callback_data=f"t:ac:{task_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="🗑 Отбросить", callback_data=f"t:rb:{task_id}")
        ])
    elif task["status"] in {"queued", "running"} or (workspace and workspace.state == "active"):
        buttons.append([
            InlineKeyboardButton(text="⏹ Cancel", callback_data=f"t:st:{task_id}")
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(lines), kb


@router.message(Command("queue", "tasks"))
async def cmd_queue(message: Message) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    cur = await db.conn.execute(
        "SELECT * FROM tasks WHERE thread_id = ? ORDER BY id DESC LIMIT 15",
        (thread_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        await message.answer("📋 Очередь пуста, задач ещё нет.")
        return
    text = "📋 <b>Последние задачи</b>\n\n" + "\n".join(_task_line(r) for r in rows)
    await message.answer(text, parse_mode="HTML")


@router.message(Command("task", "status"))
async def cmd_task_status(message: Message, command: CommandObject) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    task_id: int | None = None
    if command.args:
        try:
            task_id = int(command.args.strip().split()[0])
        except ValueError:
            await message.reply("Использование: /task <id>")
            return
    if task_id is None:
        cur = await db.conn.execute("SELECT id FROM tasks WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (thread_id,))
        row = await cur.fetchone()
        task_id = row[0] if row else None
    if task_id is None:
        await message.answer("Задач пока нет.")
        return
    text, kb = await build_task_card(task_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    from bot.handlers.message import _active_tasks
    from bot.services.task_service import cancel_queue

    entry = _active_tasks.get(thread_id)
    if entry:
        tracker, agy_task = entry
        await tracker.cancel()
        if agy_task and not agy_task.done():
            agy_task.cancel()
            try:
                await agy_task
            except asyncio.CancelledError:
                pass
    await cancel_queue(thread_id)
    await message.answer("⏹ Текущая задача и очередь отменены.")


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.reply("Использование: /search <query>")
        return
    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    cmd = ["rg", "--line-number", "--column", "--hidden", "--glob", "!{.git,node_modules,.venv,venv,dist,build,.cache,__pycache__,.agents}/**", "--", query, "."]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ws,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(20):
            stdout, _stderr = await proc.communicate()
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        await message.answer("🔍 Поиск остановлен по timeout 20s.")
        return
    output = stdout.decode("utf-8", errors="ignore").strip()
    if not output:
        await message.answer("🔍 Ничего не найдено.")
        return
    lines = output.splitlines()[:30]
    body = "\n".join(html.escape(line) for line in lines)
    await message.answer(f"🔍 <b>Результаты поиска:</b> <code>{html.escape(query)}</code>\n\n<pre>{body}</pre>", parse_mode="HTML")


@router.message(Command("context"))
async def cmd_context(message: Message, command: CommandObject) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    args = (command.args or "").strip()
    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    if args.startswith("add "):
        rel = _safe_relpath(ws, args[4:].strip())
        if not rel:
            await message.reply("Путь вне workspace запрещён.")
            return
        await db.add_context_file(thread_id, rel)
        await message.answer(f"🧠 Добавлено в контекст: <code>{html.escape(rel)}</code>", parse_mode="HTML")
        return
    if args.startswith("rm "):
        rel = _safe_relpath(ws, args[3:].strip())
        if rel:
            await db.remove_context_file(thread_id, rel)
        await message.answer("🧹 Удалено из контекста.")
        return
    if args == "clear":
        await db.clear_context(thread_id)
        await message.answer("🧹 Контекст очищен.")
        return
    if args.startswith("note "):
        await db.add_context_note(thread_id, args[5:].strip())
        await message.answer("📝 Заметка добавлена в контекст.")
        return
    files = await db.list_context_files(thread_id)
    notes = await db.list_context_notes(thread_id)
    lines = ["🧠 <b>Контекст проекта</b>", ""]
    lines.append("<b>Файлы:</b>")
    lines += [f"• <code>{html.escape(row['path'])}</code>" for row in files] or ["— пусто"]
    lines.append("\n<b>Заметки:</b>")
    lines += [f"• {html.escape(row['note'])}" for row in notes[:10]] or ["— пусто"]
    lines.append("\n<code>/context add path</code> · <code>/context rm path</code> · <code>/context note text</code> · <code>/context clear</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("memory"))
async def cmd_memory(message: Message, command: CommandObject) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    args = (command.args or "").strip()
    if args.startswith("add "):
        await db.add_memory_note(thread_id, args[4:].strip())
        await message.answer("🧠 Запомнил для проекта.")
        return
    if args.startswith("rm "):
        try:
            note_id = int(args[3:].strip())
        except ValueError:
            await message.reply("Использование: /memory rm <id>")
            return
        await db.delete_memory_note(note_id, thread_id)
        await message.answer("🧹 Заметка удалена.")
        return
    notes = await db.list_memory_notes(thread_id)
    lines = ["🧠 <b>Память проекта</b>", ""]
    lines += [f"<b>#{row['id']}</b> {html.escape(row['note'])}" for row in notes] or ["— пусто"]
    lines.append("\n<code>/memory add текст</code> · <code>/memory rm id</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("diff"))
async def cmd_diff(message: Message, bot: Bot) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    try:
        status = await git_manager.status_async(ws)
    except Exception as exc:
        await message.answer(f"Git diff недоступен: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        return
    if not status:
        await message.answer("✅ Изменений нет.")
        return
    text = "👀 <b>Diff исходного проекта (только чтение)</b>\n\n<pre>" + html.escape("\n".join(status[:60])) + "</pre>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 diff.html", callback_data=f"view_diff:{thread_id}"), InlineKeyboardButton(text="📦 patch", callback_data=f"diff_patch:{thread_id}")],
        [InlineKeyboardButton(text="🧪 Run tests", callback_data=f"run_tests:{thread_id}")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("test"))
async def cmd_test(message: Message, bot: Bot) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    cmd = _detect_test_command(ws)
    if not cmd:
        await message.answer("🧪 Не нашёл test command. Настрой позже в project settings или используй /run <command>.")
        return
    await _run_command_and_report(message, ws, cmd, thread_id)


@router.message(Command("run"))
async def cmd_run(message: Message, command: CommandObject) -> None:
    thread_id = _thread_id(message)
    if thread_id is None:
        await message.reply("Команда работает только внутри топика проекта.")
        return
    cmd = (command.args or "").strip()
    if not cmd:
        await message.reply("Использование: /run <command>")
        return
    session = await db.get_or_create_session(thread_id)
    await _run_command_and_report(message, session["workdir"], cmd, thread_id)


def _detect_test_command(ws: str) -> str | None:
    root = Path(ws)
    if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists() or list(root.glob("test_*.py")) or (root / "tests").exists():
        return "python -m pytest"
    if (root / "package.json").exists():
        return "npm test"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    return None


async def _run_command_and_report(message: Message, ws: str, command: str, thread_id: int) -> None:
    run_id = await db.create_command_run(thread_id, command)
    status_msg = await message.answer(f"🖥 <b>Терминал:</b> <code>starting</code>\n> {html.escape(command)}", parse_mode="HTML")
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=ws,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = ""
    try:
        async with asyncio.timeout(settings.task_timeout_seconds):
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(2048)
                if not chunk:
                    break
                output += chunk.decode("utf-8", errors="ignore")
                preview = html.escape(output[-2500:])
                try:
                    await status_msg.edit_text(
                        f"🖥 <b>Терминал:</b> <code>running</code>\n> {html.escape(command)}\n\n<pre>{preview}</pre>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            rc = await proc.wait()
            status = "done" if rc == 0 else "failed"
    except TimeoutError:
        proc.kill()
        await proc.wait()
        status = "timeout"
        output += f"\n[TIMEOUT after {settings.task_timeout_seconds}s]"
    await db.finish_command_run(run_id, status, output[-12000:])
    rendered = render_markdown(output[-3500:]).html
    icon = "✅" if status == "done" else "❌" if status == "failed" else "⏱"
    await status_msg.edit_text(
        f"{icon} <b>Терминал:</b> <code>{status}</code>\n> {html.escape(command)}\n\n<pre>{html.escape(strip_telegram_html(rendered))}</pre>",
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("diff_patch:"))
async def cb_diff_patch(cq: CallbackQuery, bot: Bot) -> None:
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена", show_alert=True)
        return
    raw_diff = await git_manager.get_diff_async(session["workdir"])
    if not raw_diff.strip():
        await cq.answer("Изменений нет", show_alert=True)
        return
    patch_path = Path("/tmp") / f"thread_{thread_id}.patch"
    patch_path.write_text(raw_diff, encoding="utf-8")
    await bot.send_document(cq.message.chat.id, FSInputFile(str(patch_path), filename="changes.patch"), message_thread_id=thread_id)  # type: ignore[union-attr]
    await cq.answer("Patch отправлен")


@router.callback_query(F.data.startswith("run_tests:"))
async def cb_run_tests(cq: CallbackQuery, bot: Bot) -> None:
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена", show_alert=True)
        return
    cmd = _detect_test_command(session["workdir"])
    await cq.answer("Запускаю тесты..." if cmd else "Test command не найден", show_alert=not bool(cmd))
    if cmd and cq.message:
        fake_message = cq.message
        await _run_command_and_report(fake_message, session["workdir"], cmd, thread_id)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("prompt_run:"))
async def cb_prompt_run(cq: CallbackQuery) -> None:
    await cq.answer("Отправьте команду для выполнения в формате: /run <ваша команда>", show_alert=True)

@router.callback_query(F.data.startswith("ctx:add:"))
async def cb_context_add(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")  # type: ignore[union-attr]
    thread_id = int(parts[2])
    path_id = int(parts[3])
    path = await db.get_callback_path(path_id)
    if not path:
        await cq.answer("Путь устарел", show_alert=True)
        return
    await db.add_context_file(thread_id, path)
    await cq.answer("Файл добавлен в контекст")


@router.callback_query(F.data.startswith("open_context:"))
async def cb_open_context(cq: CallbackQuery) -> None:
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    files = await db.list_context_files(thread_id)
    notes = await db.list_context_notes(thread_id)
    lines = ["🧠 <b>Контекст проекта</b>", "", "<b>Файлы:</b>"]
    lines += [f"• <code>{html.escape(row['path'])}</code>" for row in files] or ["— пусто"]
    lines.append("\n<b>Заметки:</b>")
    lines += [f"• {html.escape(row['note'])}" for row in notes[:10]] or ["— пусто"]
    await cq.message.edit_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]
    await cq.answer()


@router.callback_query(F.data.startswith("open_diff:"))
async def cb_open_diff(cq: CallbackQuery) -> None:
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена", show_alert=True)
        return
    try:
        status = await git_manager.status_async(session["workdir"])
    except Exception as exc:
        await cq.answer(
            f"Git diff недоступен: {str(exc).replace(chr(10), ' ')[:140]}",
            show_alert=True,
        )
        return
    if not status:
        await cq.answer("Изменений нет", show_alert=True)
        return
    text = "👀 <b>Diff исходного проекта (только чтение)</b>\n\n<pre>" + html.escape("\n".join(status[:60])) + "</pre>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 diff.html", callback_data=f"view_diff:{thread_id}"), InlineKeyboardButton(text="📦 patch", callback_data=f"diff_patch:{thread_id}")],
        [InlineKeyboardButton(text="🧪 Run tests", callback_data=f"run_tests:{thread_id}")],
    ])
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)  # type: ignore[union-attr]
    await cq.answer()
