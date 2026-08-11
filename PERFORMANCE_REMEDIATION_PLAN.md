# Новый план исправления производительности и архитектуры Antigravity Telegram Bot

Этот документ заменяет старый `DEVELOPMENT_PLAN.md`.

Цель нового плана — исправить найденные в жёстком аудите проблемы производительности, архитектуры и UX, в первую очередь **Event Loop starvation**, долгие ответы в chat-mode, зависающие таймеры/анимации, лишнее использование tools на обычных сообщениях и тяжёлую пост-обработку задач.

План состоит из 4 фаз:

- **Фаза 1** — срочная стабилизация event loop и hot path.
- **Фаза 2** — разделение fast chat и code task pipeline.
- **Фаза 3** — архитектурная чистка фаз 1–4, безопасность, лимиты, observability.
- **Фаза 4** — перенесённая старая фаза 5: продвинутый агент, автоматизация, plugins/tools, polish и “лучше IDE”.

---

## Фаза 1 — срочная стабилизация Event Loop starvation и hot path

**Цель фазы:** убрать самые вероятные причины зависания фоновых таймеров, spinner-анимаций, typing-loop, callback-ов и задержки первого ответа на 10–20 секунд.

### 1.1. Вынести все git-операции из event loop в async executor

`bot/services/git_manager.py` использует `subprocess.run(...)`. Это блокирует event loop. А вызывается он в горячих местах:

- `message.py:_process_queue`:
  - `git_manager.create_checkpoint(ws, ...)`
  - `git_manager.get_diff(ws)`
- `tracker.py:build_tracker_kb`
  - `git_manager.has_changes(ws)`
- `dashboard.py:build_dashboard_content`
  - `git_manager.get_current_branch(ws)`
  - `git_manager.status(ws)`
- `callbacks.py`
  - diff/accept/rollback callbacks
- `ide.py`
  - `/diff`, callbacks diff/patch/tests

Самое плохое: `build_tracker_kb()` вызывается во время рендера tracker-а. То есть обычный spinner/status update может выполнять `git status`. Это прямой рецепт “анимация лагает, таймер стоит, бот тупит”. У вас spinner не spinner, а маленький CI pipeline каждые 0.4 секунды.

#### Задачи

1. В `antigravity-bot/bot/services/git_manager.py` добавьте async-обёртки для операций `status`, `create_checkpoint`, `has_changes`, `get_diff`, `rollback`, `rollback_to_commit`, `accept`, `get_current_branch`.
2. Реализуйте их через `asyncio.to_thread(...)` или перепишите на `asyncio.create_subprocess_exec`.
3. В async-коде заменить прямые вызовы:
   - `git_manager.create_checkpoint(...)` в `bot/handlers/message.py`
   - `git_manager.get_diff(...)` в `bot/handlers/message.py`, `bot/handlers/callbacks.py`, `bot/handlers/ide.py`
   - `git_manager.status(...)` в `bot/handlers/dashboard.py`, `bot/handlers/ide.py`
   - `git_manager.has_changes(...)` в `bot/services/tracker.py`
4. Ввести короткие timeout-ы на git-команды, например 3–5 секунд для status/diff и 10–15 секунд для checkpoint/rollback.
5. При timeout показывать деградированный UI: “git status недоступен/долго выполняется”, но не блокировать Telegram bot loop.

### 1.2. Убрать git status из горячего пути TaskTracker.render

Файл: `bot/services/tracker.py`

```python
has_changes = bool(ws_dir and git_manager.has_changes(ws_dir))
```

Это вызывается в `render()` при каждом update. При debounce `0.4` это потенциально 2.5 раза в секунду на активную задачу. `git status` на большом workspace, node_modules, venv, mounted-директории или сетевом диске легко даст секунды лагов.

Это, вероятно, один из главных источников подвисания фоновых таймеров.

#### Задачи

1. В `bot/services/tracker.py` измените `build_tracker_kb(...)`, чтобы она не вызывала `git_manager.has_changes(...)`.
2. Передавайте флаг `has_changes` явно в финальный render, вычисляя его один раз после завершения задачи.
3. Для running-state всегда показывайте только `Стоп` и `Статус`, без проверки diff.
4. Для done/error-state вычисляйте наличие изменений в `bot/handlers/message.py:_process_queue` один раз через async git wrapper после завершения `run_agy`.
5. Закешируйте результат в `TaskTracker`, например `self.has_changes_after_finish`.

### 1.3. Заменить полный snapshot os.walk на дешёвый artifact index

Файл: `bot/services/artifacts.py`

`snapshot_workspaces([ws])` ходит по:

- workspace
- `/root/.gemini/antigravity-cli/scratch`
- `/root/.gemini/antigravity-ide/scratch`

И делает `os.walk` + `stat` всех файлов с расширениями из `_TRACK_EXT`.

Вызывается дважды на каждую задачу:

```python
snap_before = snapshot_workspaces([ws])
...
snap_after = snapshot_workspaces([ws])
```

Это синхронная файловая операция в event loop. Если workspace большой, mounted, содержит `node_modules`, `dist`, `.cache`, vendor, картинки, логи — бот реально может “молчать” 20 секунд до первого ответа. Особенно до запуска `agy`.

Фаза 1.8 в плане прямо говорила “использовать git status/diff вместо полного os.walk, где возможно”. Сейчас это не выполнено нормально.

#### Задачи

1. В `bot/services/artifacts.py` убрать полный `snapshot_workspaces([ws])` из горячего пути задачи.
2. Для workspace-изменений использовать `git status --porcelain` и `git diff --name-only`, выполняемые через async git wrapper.
3. Scratchpad сканировать только ограниченно:
   - только top-level или max depth 2–3;
   - только файлы новее `task_started_at`;
   - лимит количества файлов, например 200;
   - лимит общего времени, например 1–2 секунды.
4. В `bot/handlers/message.py:_process_queue` заменить `snap_before/snap_after/diff_snapshots` на новый async API, например `collect_task_artifacts(ws, started_at)`.
5. Добавить ignore dirs: `.git`, `node_modules`, `.venv`, `venv`, `dist`, `build`, `.cache`, `__pycache__`, `.agents`, `.antigravity`, `vendor`, `target`.

### 1.4. Отправлять статус сразу, тяжёлую подготовку переносить после первого await к Telegram

В `message.py:_process_queue` порядок сейчас такой:

```python
commit_hash = git_manager.create_checkpoint(...)
...
status_msg = await bot.send_message(...)
await tracker.start()
snap_before = snapshot_workspaces([ws])
```

То есть пользователь отправляет простое сообщение, а бот **до первого видимого статуса** может:

- инициализировать git repo;
- сделать `git add .`;
- сделать `git commit --allow-empty`;
- пройтись по workspace;
- пройтись по scratchpad.

Поэтому “ответ на базовое сообщение занимает 20 секунд” очень правдоподобен. Бот даже не успевает быстро сказать “принял”.

#### Задачи

1. В `bot/handlers/message.py:_process_queue` переместить `bot.send_message(...)` и `tracker.start()` максимально вверх после получения `session/ws`.
2. До любых git/snapshot операций отправлять статус “Принял задачу, готовлю окружение...”.
3. `git_manager.create_checkpoint(...)` и подготовку артефактов выполнять после status message и только через async executor.
4. Если checkpoint занимает дольше 2–3 секунд, tracker должен показывать отдельный шаг “Создаю checkpoint”.
5. Для chat-mode разрешить пропуск checkpoint/snapshot, если задача не предполагает файловые изменения.

### 1.5. Сделать incremental/debounced rendering без полного markdown render на каждый тик

Файл: `bot/services/tracker.py`

Каждый render делает:

```python
rendered_response = render_markdown(buffer_copy.strip())
clean_text = rendered_response.html
...
chunk_text(rendered, max_len=4000)
```

Буфер растёт, а рендер каждые 0.4 секунды заново прогоняет весь текст. На больших stream-ответах это становится O(n²)-поведением. Чем длиннее ответ, тем больше CPU на каждом тике. Анимация начинает жрать loop, который должна анимировать. Красиво, но бессмысленно.

#### Задачи

1. В `bot/services/tracker.py` разделить rendering статуса и rendering ответа.
2. Во время running-state показывать только header + последние 500–1000 символов plain escaped preview.
3. Полный `render_markdown(...)` выполнять только на `finish(final=True)`.
4. Увеличить debounce для обычного текста до 1.5–2.0 секунд, а tool events рендерить force, но с rate limiter.
5. Добавить ограничение: если buffer больше 4000–8000 символов, live-preview не рендерит весь markdown, а показывает tail.
6. Хранить `_last_rendered_hash` или длину/tail, чтобы не делать лишние edit_text.

### 1.6. Добавить централизованный rate limiter для Telegram edit/send операций

Файл: `bot/services/tracker.py`

```python
await asyncio.sleep(int(m.group(1)) if m else 3)
```

Сам sleep не блокирует event loop глобально, но блокирует конкретный render flow. При частых `edit_text` и force-render на tool events можно получить очередь зависших обновлений/гонки.

Плюс Telegram может rate-limit не только tracker, но и другие сообщения. Сейчас нет централизованного Telegram API rate limiter.

#### Задачи

1. Создать сервис `bot/services/telegram_rate_limiter.py`.
2. Реализовать per-chat/per-message throttle для `edit_text`: не чаще одного edit в 1–2 секунды на status message.
3. В `TaskTracker.render` перед `_safe_edit` проверять rate limiter и пропускать non-final update, если лимит не позволяет.
4. При `retry after` сохранять cooldown для chat/message и не пытаться редактировать до истечения.
5. Force/final updates должны ждать cooldown, но обычные spinner ticks должны дропаться.

### 1.7. Батчить task logs и coalesce force-render tool events

Файл: `bot/services/tracker.py`

На каждый tool lifecycle:

- берётся lock;
- пишется `task_logs` в SQLite;
- делается `await self.render(force=True)`.

Если агент активно вызывает тулзы, вы сами создаёте шквал SQLite commits и Telegram edits. Это убивает отзывчивость, особенно вместе с `git_manager.has_changes()` внутри render.

#### Задачи

1. В `bot/services/tracker.py` убрать немедленный `await log_task_event(...)` на каждый tool event.
2. Добавить in-memory очередь событий и flush раз в 1–2 секунды или на finish.
3. `on_tool_start/on_tool_end` должны только обновлять состояние и ставить dirty-флаг.
4. Render loop должен объединять несколько tool events в один Telegram edit.
5. В `bot/services/task_service.py` добавить bulk insert API для `task_logs`.

### 1.8. Отключить HITL permission flow при dangerously-skip-permissions или сделать его явным режимом

Файл: `bot/services/agy_runner.py`

Команда запускается с:

```python
"--dangerously-skip-permissions"
```

Но при каждом `step_type == "tool"` и `state == "ACTIVE"` код всё равно вызывает:

```python
approved = await permission_handler.handle_permission(...)
```

Если permission handler ждёт пользователя/пишет в Telegram/держит future, то это может тормозить поток. А если permissions уже skip — это архитектурно противоречиво.

#### Задачи

1. В `bot/services/agy_runner.py` добавить setting `permissions_mode`: `skip`, `ask`, `deny-dangerous`.
2. Если используется `--dangerously-skip-permissions`, не вызывать `permission_handler.handle_permission(...)` на ACTIVE tool.
3. Если нужен HITL, убрать `--dangerously-skip-permissions` и оставить permission handler как отдельный режим.
4. В tracker показывать tool start без ожидания permission flow, если permissions skipped.
5. Добавить лог предупреждения при несовместимой конфигурации: `dangerously_skip_permissions=True` + `permissions_mode=ask`.

### 1.9. Переписать /search на asyncio subprocess

Файл: `bot/handlers/ide.py:cmd_search`

```python
res = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=20)
```

Это прямой блок event loop до 20 секунд. Если пользователь запускает `/search`, все анимации и ответы могут зависнуть.

#### Задачи

1. В `bot/handlers/ide.py:cmd_search` заменить `subprocess.run(...)` на `asyncio.create_subprocess_exec(...)`.
2. Использовать `asyncio.timeout(20)` вокруг `proc.communicate()`.
3. При timeout убивать процесс и ждать завершения.
4. Ограничить stdout через `--max-count`, `--max-filesize`, `--glob` и/или читать потоково с лимитом строк.
5. Добавить быстрый ответ пользователю “Ищу...” до запуска rg.

### 1.10. Добавить debounce и output limit для /run и /test

Файл: `bot/handlers/ide.py:_run_command_and_report`

```python
while True:
    chunk = await proc.stdout.read(2048)
    ...
    await status_msg.edit_text(...)
```

Если команда пишет много stdout, бот будет спамить edit_text. Telegram rate limit + event loop pressure гарантированы.

#### Задачи

1. В `bot/handlers/ide.py:_run_command_and_report` обновлять Telegram status не чаще одного раза в 1.5–2 секунды.
2. Хранить полный output не бесконечно, а rolling buffer, например последние 64–128 KB.
3. Для DB сохранять tail output, как сейчас, но не накапливать бесконечную строку `output += ...`.
4. При большом stdout отправлять файл `command_output.txt`.
5. Для final edit использовать sanitized plain text без двойного markdown/render преобразования.

### 1.11. Убрать auto-send diff.html из завершения задачи

Файл: `bot/handlers/message.py`

После задачи:

```python
raw_diff = git_manager.get_diff(ws)
diff_file_path = generate_diff_html_file(raw_diff, f"Task {task_id}")
await bot.send_document(...)
```

На больших diff это дорогая операция CPU/IO + Telegram upload. Делать это автоматически после каждой задачи — плохая идея. Diff должен быть lazy по кнопке.

#### Задачи

1. В `bot/handlers/message.py:_process_queue` удалить автоматический `git_manager.get_diff(ws)` и `generate_diff_html_file(...)` после каждой задачи.
2. После завершения показывать кнопку `👀 Diff`.
3. Генерировать diff.html только в callback `cb_view_diff`.
4. В callback использовать async git wrapper и timeout.
5. Для больших diff отправлять patch-файл или summary вместо HTML.

### 1.12. Добавить process registry и reaper для background processes

Файл: `bot/services/process_manager.py`

`start_process` создаёт subprocess и сохраняет только PID в DB. Для tunnel создаётся `_monitor_tunnel_output`, но для обычных background процессов:

- нет `await process.wait()`;
- нет мониторинга завершения;
- нет reap;
- статус может навсегда остаться `running`;
- zombie/defunct риск реален.

#### Задачи

1. В `bot/services/process_manager.py` добавить in-memory registry `process_id -> asyncio.subprocess.Process`.
2. Для каждого процесса создавать monitor task, который делает `await process.wait()` и обновляет DB status на `exited`.
3. Для stdout/stderr всегда обеспечивать drain или `DEVNULL`, чтобы процесс не блокировался.
4. На startup добавить reconciliation: проверять PID через `os.kill(pid, 0)` и исправлять DB status.
5. В `stop_process` если process handle есть — использовать его, затем process group fallback.

### 1.13. Сделать graceful shutdown фоновых процессов с ожиданием завершения

`process_manager.stop_process` после SIGTERM всегда ждёт ровно 1 секунду и потом может SIGKILL-ить уже завершающийся процесс.

Это не главная причина starvation, но архитектурно грубо. Для фоновых dev-server-ов лучше graceful timeout с polling/reap.

#### Задачи

1. В `bot/services/process_manager.py:stop_process` после SIGTERM ожидать завершения process handle через `await asyncio.wait_for(process.wait(), timeout=5)`, если handle доступен.
2. Если handle недоступен, polling `os.killpg(pid, 0)` делать с коротким async sleep в цикле до 5 секунд.
3. SIGKILL отправлять только если процесс реально жив после graceful timeout.
4. После остановки обновлять DB status с timestamp/exit code.

---

## Фаза 2 — fast chat pipeline, intent classifier и нормальное разделение chat/code

**Цель фазы:** сделать так, чтобы обычные сообщения в режиме чата отвечали быстро, не инициализировали полноценную IDE-среду, не запускали лишние tools, не делали git checkpoint/snapshot/diff и при этом сохраняли полноценную память.

### 2.1. Разделить быстрый chat pipeline и тяжёлый code-task pipeline

Файлы:

- `bot/handlers/message.py`
- `bot/services/agy_runner.py`
- `bot/modes.py`

Даже если пользователь пишет обычное сообщение в chat-mode, бот делает весь IDE-пайплайн:

- очередь tasks;
- git checkpoint;
- snapshot workspace/scratchpad;
- запись `.agents/AGENTS.md`;
- `agy --continue --add-dir workspace --dangerously-skip-permissions`;
- tool lifecycle;
- artifact scan;
- diff generation.

Это не chat-mode. Это code-task-mode с другим системным prompt-ом. Поэтому агент пытается инициализировать окружение, трогает память, контекст, тулзы, workspace и ведёт себя как IDE-агент даже на бытовом сообщении.

#### Задачи

1. В `bot/handlers/message.py:_process_queue` добавить ветвление по `mode`.
2. Для `mode == "chat"` использовать lightweight execution:
   - не делать `git_manager.create_checkpoint`;
   - не делать `snapshot_workspaces`;
   - не делать artifact delivery;
   - не делать auto diff;
   - не включать workspace как активный проект, если сообщение не требует файлов.
3. В `bot/services/agy_runner.py` добавить параметр `execution_profile`, например `chat`/`code`.
4. Для `chat` запускать `agy` с минимальным prompt/context и без `--add-dir`, если CLI это позволяет; если не позволяет — использовать отдельный пустой/lightweight workspace.
5. В chat-mode добавлять только память/контекст, но не провоцировать тулзы: явно добавить правило “не использовать инструменты, если вопрос можно ответить текстом”.
6. Для задач с явными триггерами “исправь файл”, “создай”, “запусти”, “проверь проект” автоматически переключать в code pipeline.

### 2.2. Нормализовать web_search policy по режимам и типу запроса

`bot/modes.py`:

```python
"chat": {
    ...
    "web": "auto"
}
```

Но в `_process_queue` берётся:

```python
web_search: str = session.get("web_search", "off")
```

То есть режимный `web: auto` не управляет поведением. При этом если пользователь где-то включил web_search `auto` или `required`, `_ensure_agents_md` добавляет правило:

```python
Обязательно используй веб-поиск...
```

Даже для обычного сообщения. Это может быть одной из причин “на обычные сообщения пытается использовать тулзы”.

#### Задачи

1. В `bot/handlers/message.py:_process_queue` вычислять effective web policy из `session.web_search` и `get_mode_config(mode)["web"]`.
2. Для `chat` сделать default `off` или `auto-but-cheap`, а не обязательный web.
3. В `bot/services/agy_runner.py:_ensure_agents_md` не добавлять `_WEB_SEARCH_RULE` при `web_search == "auto"`; добавлять только при `required`.
4. Для `auto` добавить мягкое правило: “используй веб только если вопрос требует актуальных данных”.
5. В UI явно показывать effective web mode, чтобы пользователь понимал, почему агент полез в web.

### 2.3. Добавить intent classifier для выбора fast chat или code task

Сейчас любое текстовое сообщение в topic превращается в полноценную задачу. Нет классификации:

- “привет”
- “объясни ошибку”
- “что такое X”
- “напомни контекст”
- “исправь файл”
- “запусти тесты”

В результате даже простые ответы проходят через workspace/code-agent/tool pipeline.

#### Задачи

1. В `bot/handlers/message.py:_process` перед enqueue определить intent простыми правилами.
2. Fast chat intent:
   - короткие вопросы;
   - отсутствие слов “файл”, “создай”, “измени”, “запусти”, “проверь проект”, “diff”, “commit”, “deploy”;
   - отсутствие вложений.
3. Code task intent:
   - упоминание файлов/команд/репозитория;
   - вложения;
   - явные команды на изменение.
4. Для fast chat ставить `mode="chat"` и `execution_profile="fast"`.
5. Для code task использовать текущий тяжёлый pipeline.
6. В UI показать выбранный профиль: `⚡ Chat` или `🛠 Code task`.

### 2.4. Добавить memory/context budgeting для быстрых ответов

Файл: `bot/handlers/message.py`

```python
memory_notes = await db.list_memory_notes(thread_id)
mem = "\n".join(f"- {row['note']}" for row in memory_notes[:20])
```

Это быстро на DB уровне, но плохо для LLM latency: каждый обычный запрос получает до 20 заметок + до 30 context files. Чем больше prompt, тем дольше первый токен. “Полноценная память” не должна означать “пихаем всё в каждый prompt”.

#### Задачи

1. В `bot/handlers/message.py:_process` ограничить память по символам/токенам, например 1500–2500 символов для chat-mode.
2. Для code-mode использовать отдельный больший budget.
3. Добавить relevance filter по простому keyword overlap или embedding позже.
4. Не добавлять список context files в chat-mode, если вопрос не про проект/файлы.
5. В prompt явно разделить `memory_summary`, `relevant_notes`, `pinned_files`.

### 2.5. Не подмешивать pinned context files в обычный chat prompt без необходимости

Файл: `bot/handlers/message.py`

```python
Закрепленный контекст проекта:
- path
```

Для chat вопроса это может заставить agent вызвать `view_file`. Пользователь жалуется именно на это: обычные сообщения провоцируют tools.

#### Задачи

1. В `bot/handlers/message.py:_process` добавлять context files только если intent == code/project.
2. Для chat-mode добавлять только краткую память/summary, без путей к файлам.
3. Если пользователь спрашивает “с учётом контекста/файла”, тогда добавлять pinned files.
4. В AGENTS rules для chat-mode добавить запрет читать файлы без явного запроса.

### 2.6. Закешировать AGENTS.md content и вынести запись правил из горячего пути

Файл: `bot/services/agy_runner.py`

Там на каждый task:

- `instructions_file.read_text(...)`
- `rules_path.read_text(...)`
- `rules_path.write_text(...)`
- `skills_path.read_text(...)`
- `skills_path.write_text(...)`

Обычно это мелочь, но на mounted workspace или медленном диске тоже может вносить latency. Главное — это часть горячего пути до запуска CLI.

#### Задачи

1. В `bot/services/agy_runner.py` закешировать содержимое `INSTRUCTIONS.md` на уровне модуля после первого чтения.
2. `_ensure_agents_md` выполнять через `asyncio.to_thread(...)` или вызвать до запуска задачи как отдельный tracker step.
3. Не читать `rules_path` полностью каждый раз; сравнивать hash/mtime или писать только при изменении режима/web policy.
4. Для chat fast-path вообще не писать `.agents/AGENTS.md`, если workspace не используется.

### 2.7. Не сохранять большие code blocks в workspace автоматически

Файл: `bot/services/tracker.py`

Если агент просто прислал длинный code block, tracker создаёт `snippet_1.py` в workspace. Это:

- нарушает правило “не создавать файлы без просьбы”;
- создаёт ложные изменения git;
- провоцирует artifact/diff workflow;
- может запускать лишнюю отправку файлов.

То есть простой chat-ответ с большим примером может превратиться в “кодовые изменения проекта”.

#### Задачи

1. В `bot/services/tracker.py:_extract_large_code_blocks` писать временные snippet-файлы в `tempfile.TemporaryDirectory`, а не в `self.ws_dir`.
2. После отправки документа удалять временный файл.
3. Не добавлять такие snippet-файлы в git/artifact detection.
4. В тексте писать “код отправлен файлом”, но не “сгенерирован файл” в проекте.
5. Если пользователь явно просил создать файл — это должен делать agent/tool pipeline, не renderer.

### 2.8. Сделать корректную отмену agy_task с ожиданием завершения subprocess

Файлы:

- `bot/handlers/callbacks.py:cb_cancel_task`
- `bot/handlers/ide.py:cmd_cancel`

Сейчас:

```python
agy_task.cancel()
await cancel_queue(thread_id)
```

Но нет `await agy_task` с обработкой `CancelledError`. В `run_agy` есть kill process, но если cancel не дошёл/не дождались, можно получить висящий subprocess, гонки finish/cancel и неправильный статус.

#### Задачи

1. В `bot/handlers/callbacks.py:cb_cancel_task` после `agy_task.cancel()` выполнить `await agy_task` внутри `try/except asyncio.CancelledError`.
2. То же сделать в `bot/handlers/ide.py:cmd_cancel`.
3. В `run_agy` после `proc.kill()` использовать timeout на `await proc.wait()`.
4. Если process не завершился, дополнительно kill process group.
5. После отмены не запускать artifact scan/diff generation для cancelled task.

### 2.9. Пропускать тяжёлую post-processing фазу для cancelled/timeout/failed без изменений

Файл: `bot/handlers/message.py:_process_queue`

После `except CancelledError` всё равно выполняется:

```python
snap_after = snapshot_workspaces([ws])
new_files = diff_snapshots(...)
await tracker.finish(...)
if synced_files:
    deliver...
    get_diff...
```

Для отмены это вредно: пользователь нажал stop, а бот может ещё секунды/десятки секунд сканировать workspace и генерировать diff. Визуально это выглядит как “стоп не работает”.

#### Задачи

1. В `bot/handlers/message.py:_process_queue` после выполнения `run_agy` проверять `task_status`.
2. Для `cancelled` сразу вызвать `tracker.finish("CANCELLED")` и перейти к следующей задаче без snapshot/diff/artifact delivery.
3. Для `timeout` делать post-processing только если есть явный признак файловых изменений.
4. Для `failed` не делать auto diff/artifact delivery по умолчанию; показывать кнопку “Проверить изменения”.
5. Перенести artifact scan в отдельную lazy callback-кнопку для тяжёлых случаев.

---

## Фаза 3 — архитектурная чистка фаз 1–4, безопасность, лимиты, observability

**Цель фазы:** добить ошибки реализации старых фаз 1–4, убрать небезопасные path checks, сделать очередь атомарной, artifact delivery безопасным, dashboard неблокирующим, добавить resource isolation и performance tracing.

### 3.1. Сделать атомарный claim задачи в SQLite и защитить queue loop lock-ом

Файл: `bot/services/task_service.py`

`pop_next_task` делает:

1. SELECT queued
2. UPDATE running
3. SELECT again

Это не атомарно. `_queue_loops` — in-memory set, не защищён lock-ом. В одном процессе обычно прокатит, но при гонках callback retry/restart/multiple updates можно получить два loop-а и двойной запуск задачи.

#### Задачи

1. В `bot/services/task_service.py:pop_next_task` заменить SELECT+UPDATE на атомарный `UPDATE ... WHERE id = (SELECT ...) AND status='queued' RETURNING *`, если версия SQLite поддерживает `RETURNING`.
2. Если `RETURNING` недоступен, обернуть SELECT+UPDATE в `BEGIN IMMEDIATE`.
3. В `bot/handlers/message.py` заменить `_queue_loops: set[int]` на структуру с `asyncio.Lock`.
4. Добавить функцию `ensure_queue_loop(thread_id, bot, chat_id)`, которая атомарно проверяет и запускает loop.
5. При restart учитывать queued tasks: startup должен запускать очереди или оставлять их явно queued без потери.

### 3.2. Сделать artifact delivery явным и безопасным

Файл: `bot/services/artifacts.py`

План 1.7 выполнен частично: `_DELIVER_EXT` ограничивает исходники, но `should_deliver` всё равно отправляет:

- все картинки, кроме bg/background/hero;
- всё из `artifacts/output/outputs`;
- `.html` всегда.

Плюс scratchpad-файлы копируются в workspace по basename:

```python
dst = os.path.join(ws, os.path.basename(fpath))
```

Это ломает одинаковые имена и может затирать файлы.

#### Задачи

1. В `bot/services/artifacts.py` добавить настройки `auto_send_artifacts`, `artifact_max_files`, `artifact_max_size_mb`, `auto_send_images`.
2. По умолчанию не отправлять найденные файлы автоматически; показывать summary + кнопки.
3. Scratchpad-файлы копировать в отдельную папку workspace, например `.antigravity/artifacts/<task_id>/`, а не в корень.
4. Дедупликацию делать по relative path + hash/mtime, не по basename.
5. Перед отправкой проверять размер файла и общий лимит отправки.

### 3.3. Исправить dedupe артефактов по relative path вместо basename

Файл: `bot/services/artifacts.py`

```python
name = os.path.basename(p)
if name not in seen_names:
```

Если изменились `src/config.py` и `tests/config.py`, один файл будет потерян. Это была задача фазы 1.8, но она не исправлена.

#### Задачи

1. В `bot/services/artifacts.py:diff_snapshots` заменить `seen_names` на dedupe по normalized absolute path или workspace-relative path.
2. Для scratchpad использовать relative path от scratch root.
3. Для workspace использовать `Path.resolve().relative_to(workspace_root.resolve())`.
4. Добавить unit cases на duplicate basename: `src/config.py` и `tests/config.py`.

### 3.4. Исправить проверку принадлежности пути workspace через Path.resolve().is_relative_to

Файл: `bot/handlers/message.py`

```python
if settings.workspaces_dir not in fpath and os.path.exists(fpath):
```

Это ломко:

- `/tmp/workspaces2/foo` ошибочно считается workspace;
- symlink/mount может обойти проверку;
- Windows/relative path не учитываются.

План 1.8 прямо требовал `Path.resolve()` и `is_relative_to()`. Не сделано.

#### Задачи

1. В `bot/handlers/message.py:_process_queue` заменить `settings.workspaces_dir not in fpath` на функцию `is_inside(path, root)`.
2. Использовать `Path(fpath).resolve().is_relative_to(Path(settings.workspaces_dir).resolve())` для Python 3.9+.
3. Для совместимости с Python <3.9 использовать `try: relative_to(...)`.
4. Аналогичную проверку применить в `bot/services/artifacts.py:deliver_and_cleanup_artifacts`.

### 3.5. Заменить startswith path checks на Path.resolve().relative_to в файловом браузере

Файл: `bot/handlers/files.py`

```python
if not full_path.startswith(os.path.abspath(ws)):
```

Путь `/tmp/workspaces/1234_evil` пройдёт проверку для workspace `/tmp/workspaces/1234`. Это не performance issue, но это баг безопасности.

#### Задачи

1. В `bot/handlers/files.py:build_file_explorer` заменить `startswith` на безопасную проверку через `Path.resolve().relative_to(...)`.
2. То же сделать в `cb_open_file`.
3. Добавить helper `_safe_join_workspace(ws, rel_path) -> Path | None`.
4. Использовать helper для открытия директорий и файлов.

### 3.6. Сделать dashboard lazy и неблокирующим

Файл: `bot/handlers/dashboard.py`

`/project` вызывает sync git operations прямо в handler. Если пользователь открыл dashboard во время активной задачи, он может заморозить spinner.

#### Задачи

1. В `bot/handlers/dashboard.py:build_dashboard_content` заменить sync git calls на async wrappers.
2. Добавить timeout 2 секунды на git status/branch.
3. Если timeout — показать `branch: loading/unknown`, `changed files: нажмите обновить`.
4. Кэшировать git status per workspace на 5–10 секунд.
5. Не вызывать `git_manager.init_workspace` из dashboard, если workspace ещё не создан.

### 3.7. Добавить глобальные semaphore-ы для тяжёлых операций

Сейчас каждый thread может стартовать свой `_process_queue`. Если несколько топиков активны одновременно, каждый может:

- делать git;
- сканировать workspace;
- запускать `agy`;
- генерировать diff;
- отправлять Telegram edits.

Нет глобального semaphore-а на тяжёлые операции. На VPS это легко съедает CPU и IO.

#### Задачи

1. Создать `bot/services/resource_limits.py`.
2. Добавить semaphore-ы:
   - `AGY_CONCURRENCY`, например 1–2;
   - `GIT_CONCURRENCY`, например 2;
   - `ARTIFACT_SCAN_CONCURRENCY`, например 1;
   - `TELEGRAM_EDIT_CONCURRENCY`, per-chat limiter.
3. В `bot/handlers/message.py:_process_queue` оборачивать `run_agy` в `async with agy_semaphore`.
4. Git wrappers оборачивать в git semaphore.
5. Artifact scan/diff generation оборачивать в отдельный semaphore.

### 3.8. Добавить performance tracing по этапам задачи

Код логирует старт/финиш `agy`, но не измеряет:

- enqueue latency;
- checkpoint duration;
- snapshot duration;
- time to first status message;
- time to first agy chunk;
- render duration;
- Telegram edit duration;
- git status duration;
- artifact scan duration.

Без этого вы будете чинить на ощупь.

#### Задачи

1. Создать `bot/services/perf.py` с context manager `trace_step(task_id, name)`.
2. В `bot/handlers/message.py:_process_queue` измерять:
   - `send_status_message`
   - `create_checkpoint`
   - `snapshot_before`
   - `run_agy_total`
   - `time_to_first_chunk`
   - `snapshot_after`
   - `artifact_delivery`
   - `diff_generation`
3. В `bot/services/tracker.py` измерять `render` и `_safe_edit`.
4. Логи писать в `task_logs` и обычный logger.
5. В `/task <id>` показывать top slow steps.

### 3.9. Закрыть незавершённые пункты фазы 1 без добавления новых IDE-фич

Фаза 1 выполнена частично и местами регрессивно.

- Long response chunking сделан частично.
- Queue появилась, но атомарность слабая.
- Timeout вынесен частично: есть `task_timeout_seconds`, `agy_print_timeout`, но нет режимов quick/chat/code/long.
- Единый renderer вроде появился, но error strings в `agy_runner.py` всё ещё используют markdown с `**` и ``` внутри HTML pipeline.
- Tracker стал богаче, но начал делать sync git status в render.
- Model selection всё ещё использует `id/name`, нет короткого `key`.
- Artifact delivery ограничен, но snapshot/dedupe/path принадлежность не исправлены как требовал план.

#### Задачи

1. Пройти незавершённые требования старого плана по разделам 1.3, 1.6, 1.7, 1.8, но реализовывать их уже в рамках этого нового плана.
2. Добавить model `key/cli_id/display_name/group/default_for_modes` в `config.json`.
3. Вынести timeout profiles: `chat`, `code`, `long`.
4. Исправить artifact path checks и dedupe.
5. Убрать sync git/os.walk из tracker и hot path.

### 3.10. Ввести системный слой resource isolation для фаз 2–4

Фазы 2–4 добавили фичи, но не добавили resource isolation.

Task Manager, Dashboard, Files, Diff, Run, SSH/background — это всё полезно. Но они добавлены без системного слоя лимитов:

- нет global concurrency;
- нет per-chat Telegram rate limiter;
- нет async wrappers для sync IO;
- нет lazy diff/artifact processing;
- нет fast chat path.

Итог: IDE-фичи конкурируют с базовым chat latency. Бот стал “богатым”, но отзывчивость просела. Классика: сначала построили комбайн, потом удивились, что он не едет как велосипед.

#### Задачи

1. Добавить `bot/services/resource_limits.py` с semaphore-ами и rate limiter-ами.
2. Все тяжёлые подсистемы — git, artifact scan, diff html, agy, run/test — должны использовать эти лимиты.
3. UI-команды `/project`, `/files`, `/diff`, `/search` не должны блокировать task tracker.
4. Для тяжёлых callback-ов сначала отвечать `cq.answer(...)`, потом запускать работу async/background.
5. Ввести degraded UI при timeout вместо ожидания полного результата.

### 3.11. Самые вероятные источники ваших симптомов

#### “Зависают фоновые таймеры и анимации”

Наиболее вероятно:

1. `git_manager.has_changes()` внутри `TaskTracker.render`.
2. `snapshot_workspaces()` через `os.walk` в event loop.
3. `git_manager.create_checkpoint()` до первого status.
4. `subprocess.run()` в dashboard/search/diff callbacks.
5. Частые Telegram `edit_text` без нормального rate limiting.

#### “Ответ на базовое сообщение занимает 20 секунд”

Наиболее вероятно:

1. Нет fast chat path.
2. Перед первым полезным ответом выполняются git checkpoint и snapshot.
3. `agy` запускается как полноценный project/code agent даже для обычного вопроса.
4. Prompt раздувается memory/context.
5. Web/tool policy провоцирует инструменты.

#### “В режиме чата он пытается использовать тулзы, окружение, память”

Наиболее вероятно:

1. Chat-mode не отделён от code pipeline.
2. Workspace всегда подключается через `--add-dir`.
3. Context files всегда добавляются в prompt.
4. `.agents/AGENTS.md` содержит project/IDE/tool правила.
5. Web policy может добавлять обязательный web-search rule.
6. Нет intent classifier и no-tools fast route.

### 3.12. Рекомендуемый порядок исправления

#### Срочно, даст максимальный эффект

1. Убрать `git_manager.has_changes()` из `TaskTracker.render`.
2. Убрать `snapshot_workspaces()` из pre-start hot path.
3. Перенести status message до checkpoint/snapshot.
4. Переписать git operations на async executor.
5. Отключить auto diff.html после каждой задачи.
6. Добавить fast chat path без checkpoint/snapshot/artifacts/diff.

#### Затем

7. Добавить Telegram edit rate limiter.
8. Добавить latency tracing.
9. Добавить global semaphores.
10. Нормализовать memory/context budgeting.
11. Исправить path safety/dedupe/artifact delivery.

### 3.13. Минимальная целевая архитектура после исправлений

```text
Telegram message
  ├─ intent classifier
  │   ├─ fast chat
  │   │   ├─ lightweight memory summary
  │   │   ├─ no git
  │   │   ├─ no artifact scan
  │   │   ├─ no workspace add-dir unless needed
  │   │   └─ fast status / answer
  │   │
  │   └─ code task
  │       ├─ immediate status message
  │       ├─ async checkpoint
  │       ├─ run agy under global semaphore
  │       ├─ tracker with throttled edits
  │       ├─ lazy artifact summary
  │       └─ lazy diff on button
  │
  └─ all sync IO behind asyncio.to_thread / async subprocess
```

---
## Фаза 4 — продвинутый агент, автоматизация, plugins/tools, polish и “лучше IDE”

**Цель фазы:** довести проект до уровня персонального agent workspace, который не просто выполняет команды, а умеет помнить, планировать, автоматизировать, повторять workflow и работать как “операционная система” для разработки.

### 4.1. Ввести plugin/tool architecture

Сейчас handlers/services зашиты напрямую.

#### Задачи

Создать tool registry.

Каждый tool должен иметь:

- name;
- description;
- parameters schema;
- permission level;
- mode compatibility;
- executor;
- UI renderer;
- log formatter;
- result formatter.

Группы tools:

- local files;
- git;
- tests;
- commands;
- artifacts;
- SSH;
- deploy;
- logs;
- web/research;
- memory;
- context;
- preview.

Плюсы:

- проще добавлять новые функции;
- проще показывать tool calls в UI;
- проще давать агенту capabilities;
- проще логировать и повторять действия.

### 4.2. Сделать permission presets для удобства, а не ради “защиты”

Сейчас есть primitive dangerous command detection. Для личного проекта лучше сделать presets.

#### Задачи

Presets:

- `Read Only`;
- `Coding`;
- `Full Auto`;
- `Deploy Confirm`;
- `Manual Confirm`;
- `Server Admin`.

Для каждого preset:

- file read;
- file write;
- shell command;
- git destructive;
- SSH command;
- deploy;
- delete files;
- long-running process.

UI:

```text
⚙️ Permissions

Current: Coding

[Read Only]
[Coding]
[Full Auto]
[Deploy Confirm]
[Server Admin]
```

### 4.3. Сделать saved workflows

#### Задачи

Добавить workflow entities:

- name;
- description;
- steps;
- project_id;
- mode;
- commands;
- prompts;
- server target.

Примеры workflow:

- “Проверить проект”:
  1. git status;
  2. run tests;
  3. run lint;
  4. summarize.
- “Исправить failing tests”:
  1. run tests;
  2. analyze fail;
  3. patch;
  4. rerun.
- “Deploy to VPS”:
  1. diff;
  2. tests;
  3. backup;
  4. sync;
  5. restart;
  6. healthcheck.
- “Daily project summary”.
- “Review last changes”.

Команды:

- `/workflows`;
- `/workflow run`;
- `/workflow create`;
- `/workflow edit`.

### 4.4. Реализовать auto-fix loop

#### Задачи

Сценарий:

```text
Запусти тесты и исправляй, пока они не пройдут.
```

Agent loop:

1. run tests;
2. parse failure;
3. plan fix;
4. edit files;
5. run tests again;
6. repeat до лимита;
7. summarize;
8. show diff;
9. ask accept.

Настройки:

- max iterations;
- max time;
- stop on same error;
- require approval before broad refactor;
- save logs per iteration.

### 4.5. Сделать advanced project memory

Фаза 3 добавляет базовую memory. Фаза 4 делает её умной.

#### Задачи

- Автоматически обновлять memory после задач.
- Делать summary архитектуры.
- Хранить:
  - decisions;
  - commands that work;
  - commands that failed;
  - deployment facts;
  - server facts;
  - dependency notes;
  - coding preferences.
- Добавить memory review:
  - agent предлагает, что запомнить;
  - пользователь принимает/отклоняет.
- Добавить memory search.
- Добавить memory export/import.

### 4.6. Сделать scheduled/background automations

#### Задачи

- Планировщик задач:
  - daily;
  - weekly;
  - on demand.
- Примеры:
  - ежедневный healthcheck;
  - проверка логов;
  - проверка обновлений;
  - summary активных проектов;
  - backup;
  - dependency audit.
- Команды:
  - `/schedule`;
  - `/schedule add`;
  - `/schedule remove`;
  - `/schedule run now`.

### 4.7. Добавить smart notifications

#### Задачи

Уведомления:

- задача завершена;
- тесты упали;
- deploy failed;
- сервер недоступен;
- preview упал;
- long task needs attention;
- agent asks approval;
- daily summary ready.

Настройки:

- mute project;
- quiet hours;
- notify only errors;
- notify all.

### 4.8. Улучшить voice до voice-command interface

Voice transcription уже есть.

#### Задачи

- Голосовые команды:
  - “стоп”;
  - “покажи diff”;
  - “запусти тесты”;
  - “задеплой”;
  - “посмотри логи”;
  - “продолжи задачу”.
- Режим:
  - transcribe only;
  - execute as task.
- Автоопределение языка.
- Voice summary после долгой задачи.
- Опционально TTS-ответы.

### 4.9. Сделать advanced artifact manager

Фаза 1 убирает спам. Фаза 4 делает полноценный artifact center.

#### Задачи

- `/artifacts`.
- Список артефактов по task/project.
- Типы:
  - reports;
  - generated files;
  - screenshots;
  - patches;
  - zips;
  - logs;
  - previews.
- Кнопки:
  - download;
  - delete;
  - pin;
  - send again;
  - attach to context.
- Автоматическая упаковка больших результатов.
- Artifact retention policy.

### 4.10. Добавить advanced UI polish

#### Задачи

- Единый visual language:
  - заголовки;
  - иконки;
  - separators;
  - compact/full mode;
  - breadcrumbs;
  - pagination.
- Все основные экраны:
  - Home;
  - Project;
  - Task;
  - Files;
  - Diff;
  - Context;
  - Memory;
  - Server;
  - Deploy;
  - Settings.
- Добавить “Back”/“Home” везде.
- Добавить refresh buttons.
- Добавить inline confirmations.
- Добавить empty states:
  - нет задач;
  - нет diff;
  - нет серверов;
  - нет context files.
- Добавить help per screen.

### 4.11. Добавить observability и maintenance

#### Задачи

- Structured logs.
- Log rotation.
- `/health`.
- `/debug`.
- `/version`.
- `/selfcheck`.
- DB backup.
- Config export.
- Task cleanup.
- Artifact cleanup.
- Process cleanup.
- Error reports.
- Crash recovery.

### 4.12. Документация уже под себя

README сейчас очень краткий.

#### Задачи

- Описать:
  - local setup;
  - VPS setup;
  - Telegram forum setup;
  - model config;
  - project mount;
  - tasks;
  - files;
  - SSH;
  - deploy;
  - troubleshooting.
- Добавить personal runbook:
  - как перезапустить бота;
  - где логи;
  - где DB;
  - как восстановить;
  - как обновить.
- Добавить architecture doc:
  - handlers;
  - services;
  - DB;
  - task lifecycle;
  - SSH layer;
  - plugin tools.

### Результат фазы 4

После фазы 4 проект должен стать не просто Telegram IDE, а персональной agent-платформой:

- plugin/tool architecture;
- permission presets;
- saved workflows;
- auto-fix loops;
- advanced memory;
- scheduled automations;
- smart notifications;
- voice commands;
- artifact center;
- polished UI;
- observability;
- личная документация/runbook.

---

## Сводная таблица нового плана

| Фаза | Главная цель | Что появится |
|---|---|---|
| Фаза 1 | Срочно стабилизировать event loop и hot path | async git, no hot `os.walk`, immediate status, throttled tracker, lazy diff, fixed `/search`, safer processes |
| Фаза 2 | Разделить быстрый chat и тяжёлый code-task pipeline | fast chat, intent classifier, memory/context budgeting, corrected web policy, no unnecessary tools |
| Фаза 3 | Дочистить архитектуру фаз 1–4 | atomic queue, safe artifacts, path safety, resource isolation, performance tracing, lazy dashboard |
| Фаза 4 | Довести до agent-платформы | plugins, workflows, auto-fix, advanced memory, scheduling, notifications, polish |
---

## Рекомендуемый порядок внутри всех фаз

1. Не терять ответы и задачи.
2. Сделать task manager.
3. Сделать project dashboard.
4. Добавить files/context/diff.
5. Добавить tests/run.
6. Добавить SSH/logs/deploy.
7. Добавить preview/background processes.
8. Добавить workflows/auto-fix.
9. Добавить memory/scheduling/notifications.
10. Полировать UI и документацию.
