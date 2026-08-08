# План развития Antigravity Telegram IDE

Этот документ фиксирует поэтапный план развития проекта из текущего Telegram-раннера для Antigravity CLI в персональную Telegram IDE / agent-control center.

План разделён на 5 фаз:

- **Фаза 1** — стабилизация текущего MVP и подготовка базы.
- **Фаза 2** — Task Manager, Project Dashboard и нормальный Telegram UI.
- **Фаза 3** — файловая IDE, Git/Diff workflow и контекст.
- **Фаза 4** — SSH, серверы, deploy, preview и background processes.
- **Фаза 5** — продвинутый агент, автоматизация, plugins/tools, polish и “лучше IDE”.

---

## Фаза 1 — стабилизация текущего MVP и подготовка базы

**Цель фазы:** сделать текущий проект предсказуемым, стабильным и пригодным для ежедневного использования в текущей архитектуре: Telegram → Topic → `agy` → ответ/diff/artifacts.

Эта фаза не добавляет “большую IDE”, а исправляет всё, что уже сейчас мешает пользоваться ботом.

### 1.1. Исправить потерю длинных ответов

Сейчас `TaskTracker.render()` делит ответ на chunks, но использует только `chunks[0]`, из-за чего длинные ответы фактически обрезаются.

#### Задачи

- Исправить отправку финального ответа:
  - первый chunk редактирует status message;
  - остальные chunks отправляются отдельными сообщениями.
- Добавить нумерацию частей:
  - `Часть 1/3`;
  - `Часть 2/3`;
  - `Часть 3/3`.
- Если ответ очень большой — отправлять полный ответ `.md` или `.txt` файлом.
- Если в ответе длинный code block — отправлять его файлом, а в Telegram давать summary.
- Добавить fallback:
  - если Telegram HTML ломается;
  - если сообщение слишком длинное;
  - если edit failed.
- Добавить тесты для chunking:
  - короткий ответ;
  - ответ > 4000 символов;
  - длинная строка без переносов;
  - кодовый блок;
  - HTML entities.

### 1.2. Исправить поведение при активной задаче

Сейчас если в ветке уже есть активная задача, новое сообщение silently dropped.

#### Задачи

- Не игнорировать новое сообщение.
- Показывать ответ:
  - “Задача уже выполняется”;
  - “Добавить в очередь / Отменить текущую / Посмотреть статус”.
- Добавить минимальную очередь сообщений на thread.
- Добавить `/cancel`.
- Добавить `/status`.
- Добавить кнопку `⏹ Стоп` в tracker.
- Добавить кнопку `📌 Статус`.
- Сделать поведение:
  - если задача активна — новое сообщение попадает в очередь;
  - если пользователь нажал cancel — текущий `agy_task` отменяется;
  - после завершения текущей задачи автоматически стартует следующая.
- Сохранять хотя бы минимальное состояние очереди в памяти на этой фазе.
- Подготовить интерфейсы так, чтобы во 2 фазе перенести queue/tasks в SQLite.

### 1.3. Сделать timeout настраиваемым и согласованным

Сейчас CLI получает `--print-timeout 10m0s`, но Python-код обрывает задачу через 300 секунд.

#### Задачи

- Вынести таймауты в settings:
  - `task_timeout_seconds`;
  - `agy_print_timeout`;
  - `idle_timeout_seconds`;
  - `command_timeout_seconds`.
- Синхронизировать Python timeout и CLI timeout.
- Добавить режимы таймаутов:
  - quick/chat;
  - code task;
  - long/server/deploy.
- В tracker показывать elapsed time.
- При timeout отправлять понятное сообщение:
  - сколько выполнялось;
  - какой timeout сработал;
  - что можно сделать дальше.
- Добавить кнопку:
  - `🔁 Повторить`;
  - `▶️ Продолжить`;
  - `⏱ Запустить как долгую задачу`.
- Убедиться, что `proc.kill()` корректно завершает процесс и не оставляет zombie.
- Добавить тесты на timeout/cancel с fake process.

### 1.4. Привести Markdown/HTML rendering к единой системе

Сейчас есть два разных конвертера Markdown/HTML: `md_to_html()` и `clean_telegram_markdown()`.

#### Задачи

- Оставить один единый renderer:
  - `telegram_renderer.py`;
  - или объединить в существующий `formatting.py`.
- Разделить функции:
  - sanitize plain text;
  - markdown → Telegram HTML;
  - safe HTML escaping;
  - chunking;
  - fallback plain text.
- Экранировать:
  - пути;
  - имена файлов;
  - model names;
  - stderr/stdout;
  - ошибки CLI;
  - tool labels.
- Исправить вставку ошибок CLI без escaping.
- Исправить fallback `_safe_edit()`, чтобы он не просто грубо вырезал HTML-теги regex-ом.
- Покрыть тестами:
  - bold;
  - italic;
  - inline code;
  - fenced code;
  - links;
  - bullets;
  - blockquotes;
  - HTML special chars;
  - broken markdown;
  - длинные code blocks.

### 1.5. Улучшить текущий tracker без большой переделки архитектуры

`TaskTracker` сейчас показывает плоский список шагов и spinner.

#### Задачи

- Сделать финальное состояние всегда явным:
  - `✅ Готово`;
  - `❌ Ошибка`;
  - `⏹ Отменено`;
  - `⏱ Timeout`;
  - `⚠️ Ответ пустой`.
- Если `clean_text` пустой, не оставлять старое “Агент работает…”.
- Добавить elapsed time.
- Добавить количество выполненных steps.
- Добавить последние tool actions.
- Добавить кнопку `📄 Логи`, даже если пока лог будет коротким.
- Добавить кнопку `🔁 Повторить`.
- Улучшить текст статуса:
  - не только “Оформление ответа…”;
  - а “Формирую финальный ответ”, “Проверяю изменения”, “Готовлю diff”.
- Уменьшить flicker:
  - не редактировать сообщение слишком часто;
  - учитывать Telegram rate limits;
  - правильно обрабатывать `retry after`.

### 1.6. Исправить model selection

Модели сейчас берутся из `config.json`, callback строится напрямую из model id, а парсинг идёт через `split(":", 2)`.

#### Задачи

- Ввести короткий `key` для каждой модели.
- Разделить:
  - `key`;
  - `cli_id`;
  - `display_name`;
  - `group`;
  - `default_for_modes`.
- Callback сделать коротким:
  - `model:<key>:<thread_id>`.
- Добавить галочку текущей модели.
- Добавить кнопку “по умолчанию”.
- Добавить fallback, если модель пропала из config.
- Добавить валидацию config.
- Добавить тесты callback parsing.

### 1.7. Исправить artifact delivery, чтобы не было спама

Сейчас все изменённые файлы подходящих расширений отправляются документами.

#### Задачи

- Отключить автоматическую отправку всех исходников.
- Оставить автоотправку только для явных пользовательских артефактов:
  - PDF;
  - ZIP;
  - картинки;
  - отчёты;
  - готовые HTML-документы;
  - презентации/таблицы/документы.
- Для code changes показывать summary:
  - сколько файлов изменено;
  - список файлов;
  - кнопки:
    - `👀 Diff`;
    - `📦 Скачать zip`;
    - `✅ Принять`;
    - `↩️ Откатить`.
- Добавить настройку:
  - `auto_send_artifacts`;
  - `auto_send_code_files`;
  - `artifact_max_files`;
  - `artifact_max_size_mb`.

### 1.8. Исправить определение workspace/scratchpad

Сейчас логика проверяет `settings.workspaces_dir not in fpath`, что ломко для mounted-проектов.

#### Задачи

- Использовать `Path.resolve()`.
- Проверять принадлежность через `is_relative_to()`.
- Deduplicate делать по relative path, а не basename.
- Разделить:
  - workspace files;
  - scratchpad files;
  - generated artifacts;
  - uploaded files.
- Добавить ignore dirs:
  - `.git`;
  - `node_modules`;
  - `.venv`;
  - `venv`;
  - `dist`;
  - `build`;
  - `.cache`;
  - `__pycache__`.
- Для workspace использовать git status/diff вместо полного `os.walk`, где возможно.
- Добавить тесты на:
  - mounted workspace;
  - tmp workspace;
  - duplicate basenames;
  - scratchpad file;
  - ignored dirs.

### 1.9. Нормализовать работу с загруженными файлами

Документ сейчас сохраняется по имени из Telegram напрямую через `os.path.join(ws, filename)`. Фото и файлы передаются агенту через абсолютные пути.

#### Задачи

- Создать папку `uploads/` внутри workspace.
- Нормализовать filename:
  - basename;
  - убрать странные символы;
  - ограничить длину;
  - добавить unique suffix при конфликте.
- Передавать агенту относительные пути:
  - `uploads/file.pdf`;
  - `uploads/photo.jpg`.
- Не передавать в prompt абсолютные `/tmp/...` или `/root/...`.
- Добавить отображение прикреплённых файлов в tracker.
- Добавить тесты filename normalization.

### 1.10. Привести config/startup к стабильному состоянию

Config сейчас жёстко смотрит `/opt/antigravity-bot/.env`, лог пишется в `/opt/antigravity-bot/logs/bot.log`, DB path — `/opt/antigravity-bot/data/bot.db`.

#### Задачи

- Поддержать local `.env`:
  - repo `.env`;
  - `antigravity-bot/.env`;
  - `/opt/antigravity-bot/.env`.
- Создавать директории:
  - logs;
  - data;
  - workspaces.
- Вынести `log_path` в settings.
- Добавить startup self-check:
  - bot token есть;
  - allowed ids распарсились;
  - agy exists;
  - ffmpeg exists;
  - db dir writable;
  - workspaces dir writable;
  - config.json валиден.
- Сделать понятный startup report в логах.
- Валидировать `allowed_user_ids`.
- Добавить `.env.example`, если его нет/он неполный.

### 1.11. Привести DB к будущему расширению

Сейчас есть только `thread_sessions`. Также DB создаёт `uuid`, но реальный conversation id вычисляется через UUIDv5 от `thread_id`.

#### Задачи

- Решить, какой id является главным:
  - использовать `session["uuid"]`;
  - или хранить `agy_conversation_id` отдельно;
  - или убрать лишний `uuid`.
- Подготовить DB migrations:
  - `schema_version`;
  - migration runner;
  - backup перед миграцией.
- Пока не добавлять все будущие таблицы, но подготовить механизм.
- Добавить базовые тесты DB CRUD.

### 1.12. Добавить базовый test suite

Сейчас зависимости минимальные, тестового слоя нет.

#### Задачи

- Добавить dev dependencies:
  - pytest;
  - pytest-asyncio;
  - ruff;
  - mypy/pyright по желанию.
- Добавить тесты:
  - renderer;
  - chunking;
  - model callback;
  - artifact diff;
  - path normalization;
  - DB sessions;
  - git manager на temp repo;
  - fake agy stream parser;
  - timeout/cancel.
- Добавить команды:
  - `pytest`;
  - `python -m compileall`;
  - `ruff check`.
- Обновить README с dev/test commands.

### Результат фазы 1

После фазы 1 проект должен стать:

- стабильным;
- не теряющим длинные ответы;
- с нормальным cancel/status;
- с базовой очередью;
- с адекватным tracker;
- с нормальным rendering;
- без артефактного спама;
- с нормальными uploads;
- с валидируемым config;
- с подготовкой к DB migrations;
- с базовыми тестами.

---

## Фаза 2 — Task Manager, Project Dashboard и нормальный Telegram UI

**Цель фазы:** превратить текущий чатовый MVP в управляемый персональный центр задач.

Здесь появляется полноценная модель задач, красивый dashboard, queue/history/status и нормальная карточная навигация.

### 2.1. Добавить полноценную таблицу tasks

Сейчас `_active` хранит задачи только в памяти.

#### Задачи

- Добавить таблицу `tasks`:
  - id;
  - chat_id;
  - thread_id;
  - project_id;
  - prompt;
  - status;
  - mode;
  - model;
  - created_at;
  - started_at;
  - finished_at;
  - error;
  - result_summary;
  - full_response_path;
  - parent_task_id;
  - retry_of_task_id.
- Статусы:
  - queued;
  - running;
  - waiting_approval;
  - done;
  - failed;
  - cancelled;
  - timeout.
- Добавить таблицу `task_logs`.
- Добавить таблицу `task_artifacts`.
- Добавить migration для новых таблиц.
- Перенести `_active` в task service:
  - in-memory runtime state;
  - persisted DB state.
- После рестарта показывать незавершённые задачи как interrupted.

### 2.2. Реализовать очередь задач

#### Задачи

- Очередь на thread/project.
- Новые сообщения во время running task:
  - добавить в очередь;
  - или добавить как уточнение;
  - или отменить текущую.
- Команды:
  - `/queue`;
  - `/tasks`;
  - `/task <id>`;
  - `/cancel`;
  - `/retry`;
  - `/continue`.
- Inline-кнопки:
  - `➕ В очередь`;
  - `⏹ Отменить текущую`;
  - `💬 Добавить уточнение`;
  - `📋 Очередь`;
  - `📌 Статус`.
- Автоматический запуск следующей задачи после завершения текущей.
- Хранить task logs/result.
- Показывать task id в tracker.

### 2.3. Сделать красивую карточку задачи

#### Задачи

Создать единый task card renderer:

```text
🧠 Задача #42

Проект: AG-agent-in-telegram
Режим: Code
Модель: Claude Sonnet
Статус: running
Время: 04:12

Шаги:
✅ Прочитал проект
✅ Нашёл проблему
⏳ Исправляю tracker
⬜ Запущу тесты

Последний вывод:
...
```

Кнопки:

- `⏹ Стоп`;
- `📄 Логи`;
- `👀 Diff`;
- `📁 Файлы`;
- `🔁 Retry`;
- `▶️ Continue`;
- `✅ Accept`;
- `↩️ Rollback`.

#### Технические задачи

- Разделить rendering task card и rendering agent response.
- Хранить timeline steps.
- Добавить timestamps per step.
- Сохранять tool events в task logs.
- Показывать последние N строк output.
- Добавить compact/full view.

### 2.4. Сделать project dashboard

Сейчас `/settings` показывает только модель, web, директорию и session id.

#### Задачи

Создать команду `/project` или расширить `/settings`.

Dashboard должен показывать:

- project name;
- path;
- git branch;
- dirty files count;
- active task;
- queued tasks;
- last task;
- last error;
- selected model;
- selected mode;
- web status;
- timeout profile;
- artifact settings;
- server profile, если подключён;
- test command;
- run command;
- deploy target.

Кнопки:

- `💬 Chat`;
- `🛠 Code Task`;
- `📁 Files`;
- `👀 Diff`;
- `🧪 Tests`;
- `🚀 Deploy`;
- `🖥 Server`;
- `🧠 Context`;
- `⚙️ Settings`.

### 2.5. Добавить project entity

Сейчас сессия хранит только `thread_id`, `uuid`, `workdir`, настройки.

#### Задачи

- Добавить таблицу `projects`:
  - id;
  - name;
  - path;
  - type;
  - created_at;
  - last_used_at;
  - default_model;
  - default_mode;
  - test_command;
  - run_command;
  - deploy_command;
  - server_profile_id.
- Связать thread session с project.
- Добавить project aliases.
- Добавить `/projects`.
- Добавить выбор текущего проекта.
- Добавить rename project.
- Добавить archive project.
- Добавить project settings.

### 2.6. Улучшить master panel

Текущий master panel — текстовый список сессий.

#### Задачи

Сделать главный экран:

```text
🚀 Antigravity Personal IDE

📂 Current project: ...
🌿 Branch: ...
📝 Changes: ...
🧠 Model: ...
🌐 Web: ...
⚙️ Mode: ...

Active task:
#42 ...

[Projects] [Tasks] [Files]
[Diff] [Run] [Settings]
```

Добавить:

- pagination проектов;
- active/queued task count;
- быстрые действия;
- красивые иконки;
- единый стиль сообщений.

### 2.7. Добавить режимы агента

Сейчас есть только model/web.

#### Задачи

Добавить `/mode`.

Режимы:

- `Chat`;
- `Plan`;
- `Code`;
- `Review`;
- `Debug`;
- `Test`;
- `Research`;
- `Artifact`;
- `Deploy`;
- `Server`.

Для каждого режима:

- default model;
- default timeout;
- permissions preset;
- prompt prefix/rules;
- UI buttons;
- artifact behavior.

Примеры:

- Plan — не менять файлы.
- Code — можно менять файлы.
- Review — смотреть diff/code.
- Test — запускать тесты и чинить.
- Research — web required.
- Deploy — серверный workflow.

### 2.8. Улучшить web-search как режим

Сейчас web-search — это просто добавление правила в AGENTS.md.

#### Задачи

- Сделать web mode:
  - off;
  - auto;
  - required.
- В tracker показывать web actions.
- Сохранять источники в task logs.
- В финальном ответе показывать “Источники”.
- Если web не использовался в required mode — предупреждать.
- Добавить кнопку `🌐 Web: Auto/Required/Off`.

### 2.9. Сделать настройки не просто callback-кнопками, а полноценными экранами

Сейчас settings keyboard содержит только web и model.

#### Задачи

Разделить settings:

- model settings;
- mode settings;
- web settings;
- timeout settings;
- artifact settings;
- git settings;
- project settings;
- UI settings.

Добавить общий settings router:

```text
⚙️ Settings

[🤖 Model]
[🧠 Mode]
[🌐 Web]
[⏱ Timeouts]
[📦 Artifacts]
[🌿 Git]
[🖥 Server]
```

### Результат фазы 2

После фазы 2 проект должен стать не просто чатом, а **центром управления задачами**:

- task queue;
- persistent task history;
- project dashboard;
- красивый task tracker;
- modes;
- settings screens;
- улучшенный web mode;
- basic project entities.

---

## Фаза 3 — файловая IDE, Git/Diff workflow и контекст

**Цель фазы:** добавить ключевую IDE-логику: файлы, поиск, контекст, diff review, git workflow, тесты.

После этой фазы проект уже должен ощущаться как Telegram IDE.

### 3.1. Реализовать файловый браузер

Сейчас проект умеет принимать файлы, но не умеет просматривать файлы workspace.

#### Задачи

Добавить `/files`.

Функции:

- показать текущую директорию;
- список папок;
- список файлов;
- pagination;
- breadcrumbs;
- назад/вперёд;
- сортировка:
  - folders first;
  - modified first;
  - by name;
- фильтр скрытых файлов;
- ignore:
  - `.git`;
  - `node_modules`;
  - `.venv`;
  - build/cache dirs.
- кнопки:
  - открыть;
  - скачать;
  - добавить в контекст;
  - удалить;
  - переименовать;
  - создать файл;
  - создать папку.

### 3.2. Реализовать просмотр файла

#### Задачи

- `/open path`.
- Inline open из `/files`.
- Для маленьких файлов — показать в Telegram.
- Для больших файлов — отправить документ.
- Для code files — подсветка через code block или HTML file.
- Для binary files — metadata + download.
- Кнопки:
  - `🧠 В контекст`;
  - `✏️ Редактировать`;
  - `📥 Скачать`;
  - `🔍 Найти в файле`;
  - `↩️ Назад`.

### 3.3. Реализовать поиск по проекту

#### Задачи

- `/search query`.
- Поиск по filenames.
- Поиск по content.
- Использовать ripgrep.
- Показывать результаты:
  - path;
  - line number;
  - snippet.
- Кнопки:
  - открыть файл;
  - добавить файл в контекст;
  - спросить агента про найденное.
- Ограничить max results.
- Учитывать ignore dirs.

### 3.4. Реализовать context manager

Сейчас контекст implicit: prompt + workspace + conversation.

#### Задачи

Добавить `/context`.

Функции:

- показать текущие файлы в контексте;
- добавить файл;
- добавить папку;
- удалить файл;
- очистить контекст;
- pin file;
- pin note;
- режим:
  - selected files only;
  - whole project allowed;
  - recent files;
  - git changed files.
- Хранить в DB:
  - context_files;
  - context_notes;
  - context_mode.
- При запуске `agy` добавлять context summary в prompt/rules.
- Добавить кнопку `🧠 В контекст` в file browser/search/diff.

### 3.5. Реализовать project memory

AGENTS.md сейчас создаётся автоматически и содержит базовые правила.

#### Задачи

Добавить `/memory`.

Хранить:

- project architecture;
- how to run;
- how to test;
- how to deploy;
- coding style;
- common issues;
- server notes;
- user preferences;
- important commands.

Функции:

- `Запомни: ...`;
- показать memory;
- редактировать memory;
- удалить note;
- pin note;
- auto-summarize после крупных задач.

Интеграция:

- включать memory в prompt;
- не перезаписывать пользовательский AGENTS.md;
- разделить bot rules и project memory.

### 3.6. Улучшить AGENTS/rules handling

Сейчас `_ensure_agents_md()` пишет `.agents/AGENTS.md` в workspace.

#### Задачи

- Не перезаписывать существующие пользовательские правила без необходимости.
- Добавить bot-managed блок с маркерами.
- Или использовать отдельный файл, если CLI поддерживает.
- Показывать активные правила в `/settings` или `/memory`.
- Добавить режимы правил:
  - concise;
  - detailed;
  - code-only;
  - plan-only;
  - Russian;
  - no-files-without-request.
- Связать правила с mode.

### 3.7. Реализовать полноценный diff workflow

Сейчас есть diff.html, accept all, rollback all.

#### Задачи

Добавить `/diff`.

Функции:

- список изменённых файлов;
- статус каждого файла:
  - added;
  - modified;
  - deleted;
  - renamed;
- diff по одному файлу;
- diff all;
- скачать diff.patch;
- summary изменений;
- accept all;
- rollback all;
- accept file;
- rollback file;
- ask agent to revise file;
- ask agent to explain diff;
- run tests before accept.

UI:

```text
👀 Diff

Изменено 5 файлов:
M src/app.py
A src/tasks.py
M README.md

[Открыть src/app.py]
[✅ Accept all] [↩️ Rollback all]
[🧪 Run tests] [📦 Patch]
```

### 3.8. Улучшить Git workflow

`GitManager` сейчас умеет init, checkpoint, has_changes, diff, rollback, accept.

#### Задачи

Добавить:

- git status;
- current branch;
- create branch;
- checkout branch;
- commit with custom message;
- stash;
- restore file;
- clean selected files;
- show commit history;
- show last checkpoint;
- compare with checkpoint;
- tag accepted task.
- Перед accept:
  - предложить commit message;
  - связать commit с task id.

### 3.9. Реализовать test workflow

#### Задачи

Добавить `/test`.

Функции:

- auto-detect test command:
  - pytest;
  - npm test;
  - pnpm test;
  - cargo test;
  - go test;
  - etc.
- Хранить test command в project settings.
- Запускать тесты.
- Стримить output.
- Сохранять result.
- Показывать summary.
- Кнопки:
  - `🧪 Run tests`;
  - `🐞 Fix failures`;
  - `📄 Full log`;
  - `🔁 Rerun`.
- Перед accept diff предлагать run tests.
- После fail запускать Debug mode.

### 3.10. Реализовать `/run` как managed command

Сейчас агент может вызвать команды через CLI, но пользовательского command runner нет.

#### Задачи

- `/run <command>`.
- Запуск команды в workspace.
- Стрим output.
- Таймаут.
- Kill/stop button.
- История команд.
- Сохранение result в task logs.
- Presets:
  - install;
  - test;
  - build;
  - lint;
  - dev server;
  - deploy.
- Для долгих команд — background process.

### Результат фазы 3

После фазы 3 проект должен стать уже похож на Telegram IDE:

- файловый браузер;
- просмотр файлов;
- поиск;
- context manager;
- project memory;
- полноценный diff review;
- git workflow;
- test workflow;
- managed command runner.

---

## Фаза 4 — SSH, серверы, deploy, preview и background processes

**Цель фазы:** добавить то, чего сейчас полностью нет: полноценную работу с удалёнными серверами, SSH, логами, деплоем и preview.

Это фаза превращает проект из local Telegram IDE в personal DevOps/IDE agent.

### 4.1. Добавить SSH profiles

Сейчас SSH/server entities отсутствуют.

#### Задачи

Добавить таблицу `servers`:

- id;
- name;
- host;
- port;
- user;
- auth_type;
- key_path;
- default_project_path;
- created_at;
- last_used_at.

Команды:

- `/servers`;
- `/server add`;
- `/server select`;
- `/server test`;
- `/server remove`;
- `/server edit`.

UI:

```text
🖥 Servers

1. prod-api
2. staging
3. personal-vps

[➕ Add] [🔑 Test] [⚙️ Edit]
```

### 4.2. Реализовать remote command runner

#### Задачи

- Выполнение команд по SSH.
- Стрим output в Telegram.
- Timeout.
- Cancel.
- Сохранение logs.
- Working directory на сервере.
- Команды:
  - `/ssh <server> <command>`;
  - `/server run`.
- Presets:
  - `systemctl status`;
  - `docker ps`;
  - `docker compose ps`;
  - `journalctl`;
  - `df -h`;
  - `free -m`;
  - `git status`;
  - `tail logs`.

### 4.3. Реализовать remote logs

#### Задачи

- `/logs`.
- Источники логов:
  - local command logs;
  - SSH command logs;
  - systemd logs;
  - docker logs;
  - app log file;
  - deploy logs.
- Кнопки:
  - refresh;
  - tail -f;
  - stop;
  - last 100;
  - last 500;
  - search in logs.
- Agent action:
  - “проанализируй эти логи”;
  - “найди причину ошибки”;
  - “исправь и задеплой”.

### 4.4. Реализовать remote file browser

#### Задачи

- Просмотр файлов на сервере.
- Открытие файла.
- Скачивание файла.
- Поиск по remote project.
- Загрузка локального файла на сервер.
- Сравнение local/remote файла.
- Добавление remote file в контекст.
- Ограничение по project root для удобства навигации.

### 4.5. Реализовать deploy workflow

Deploy scripts уже есть в репозитории, но не интегрированы в Telegram UX.

#### Задачи

Добавить `/deploy`.

Workflow:

1. выбрать project;
2. выбрать server;
3. показать diff;
4. run tests;
5. build;
6. backup;
7. upload/sync;
8. restart service;
9. healthcheck;
10. show logs;
11. mark deploy success/fail.

Кнопки:

- `🚀 Deploy`;
- `🧪 Test first`;
- `📦 Backup`;
- `🔁 Retry`;
- `↩️ Rollback deploy`;
- `📄 Logs`.

Хранить deploy history:

- task_id;
- server_id;
- commit;
- started_at;
- finished_at;
- status;
- logs;
- rollback info.

### 4.6. Реализовать deploy rollback

#### Задачи

- Перед deploy делать backup.
- Хранить backup metadata.
- Откат:
  - previous release;
  - previous archive;
  - previous docker image;
  - previous git commit.
- Команда:
  - `/deploy rollback`.
- UI:
  - список последних deploy;
  - выбрать;
  - подтвердить;
  - показать logs;
  - healthcheck после rollback.

### 4.7. Реализовать managed background processes

Сейчас blocking web server commands hard-deny, чтобы не зависать. Для IDE нужно не запрещать, а управлять ими.

#### Задачи

- Таблица `processes`.
- Запуск:
  - dev server;
  - test watcher;
  - tail logs;
  - preview server.
- Статусы:
  - running;
  - stopped;
  - crashed.
- Команды:
  - `/processes`;
  - `/process stop`;
  - `/process logs`.
- Кнопки:
  - stop;
  - restart;
  - logs;
  - open preview.
- Автоматическое завершение старых процессов.

### 4.8. Реализовать preview для web-приложений

Сейчас артефакты можно отправить файлами, но preview-сервера нет.

#### Задачи

- Запуск локального preview server.
- Поддержка:
  - static HTML;
  - Vite;
  - Next;
  - React;
  - simple Python http server.
- Генерация preview URL.
- Опционально tunnel.
- Screenshot preview.
- Кнопки:
  - refresh;
  - stop;
  - logs;
  - open.
- Интеграция с task result:
  - если агент создал web page — предложить preview.

### 4.9. Добавить server/project linking

#### Задачи

- Связать project с server.
- У project может быть:
  - local path;
  - remote path;
  - deploy target;
  - test command;
  - build command;
  - restart command;
  - healthcheck URL.
- В project dashboard показывать server status.
- В task mode Deploy автоматически использовать эти настройки.

### 4.10. Добавить agent-driven server workflows

#### Задачи

Сценарии:

- “Подключись к серверу и посмотри почему сайт не работает”.
- “Проверь docker logs”.
- “Исправь ошибку локально, задеплой и проверь healthcheck”.
- “Собери проект и перезапусти сервис”.
- “Сравни локальную и серверную версию”.
- “Найди почему deploy падает”.

Для этого нужно:

- tools layer для SSH;
- logs ingestion;
- remote file context;
- deploy task mode;
- server memory.

### Результат фазы 4

После фазы 4 проект станет персональным Telegram DevOps/IDE agent:

- SSH profiles;
- remote commands;
- logs;
- remote files;
- deploy;
- rollback deploy;
- managed background processes;
- web preview;
- server-aware tasks.

---

## Фаза 5 — продвинутый агент, автоматизация, plugins/tools, polish и “лучше IDE”

**Цель фазы:** довести проект до уровня персонального agent workspace, который не просто выполняет команды, а умеет помнить, планировать, автоматизировать, повторять workflow и работать как “операционная система” для разработки.

### 5.1. Ввести plugin/tool architecture

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

### 5.2. Сделать permission presets для удобства, а не ради “защиты”

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

### 5.3. Сделать saved workflows

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

### 5.4. Реализовать auto-fix loop

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

### 5.5. Сделать advanced project memory

Фаза 3 добавляет базовую memory. Фаза 5 делает её умной.

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

### 5.6. Сделать scheduled/background automations

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

### 5.7. Добавить smart notifications

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

### 5.8. Улучшить voice до voice-command interface

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

### 5.9. Сделать advanced artifact manager

Фаза 1 убирает спам. Фаза 5 делает полноценный artifact center.

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

### 5.10. Добавить advanced UI polish

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

### 5.11. Добавить observability и maintenance

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

### 5.12. Документация уже под себя

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

### Результат фазы 5

После фазы 5 проект должен стать не просто Telegram IDE, а персональной agent-платформой:

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

## Сводная таблица фаз

| Фаза | Главная цель | Что появится |
|---|---|---|
| Фаза 1 | Стабилизировать текущий MVP | длинные ответы, cancel/status, rendering, timeout config, artifact cleanup, uploads, tests |
| Фаза 2 | Сделать task/project control center | task DB, queue, dashboard, modes, settings screens |
| Фаза 3 | Добавить IDE-функции | files, open, search, context, memory, diff, git, tests, run |
| Фаза 4 | Добавить server/devops | SSH, remote commands, logs, deploy, rollback, preview, processes |
| Фаза 5 | Довести до agent-платформы | plugins, workflows, auto-fix, advanced memory, scheduling, notifications, polish |

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
