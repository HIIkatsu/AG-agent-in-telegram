from bot.services.execution_profiles import (
    CHAT_MEMORY_CHAR_BUDGET,
    classify_execution_profile,
    effective_mode,
    effective_web_policy,
    select_relevant_notes,
)


def test_chat_memory_budget_keeps_substantial_context() -> None:
    assert CHAT_MEMORY_CHAR_BUDGET >= 8_000


def test_plain_question_uses_fast_chat() -> None:
    assert classify_execution_profile("Почему небо синее?") == "chat"


def test_project_mutation_uses_code_profile() -> None:
    assert classify_execution_profile("Исправь файл bot/main.py и запусти тесты") == "code"
    assert classify_execution_profile("Посмотри вложение", has_attachments=True) == "code"


def test_code_request_overrides_chat_mode() -> None:
    assert effective_mode("chat", "code") == "code"
    assert effective_mode("plan", "code") == "plan"
    assert effective_mode("code", "chat") == "chat"


def test_effective_web_policy_honours_explicit_setting_and_default() -> None:
    assert effective_web_policy("required", "off") == "required"
    assert effective_web_policy("", "auto") == "auto"
    assert effective_web_policy(None, "invalid") == "off"


def test_relevant_memory_is_ranked_and_bounded() -> None:
    notes = [
        {"note": "Пользователь любит короткие ответы"},
        {"note": "Проект использует Telegram и aiogram"},
        {"note": "Telegram webhook развёрнут на сервере"},
    ]
    selected = select_relevant_notes(notes, "Как устроен Telegram проект?", char_budget=55)

    assert selected[0] == "Проект использует Telegram и aiogram"
    assert sum(map(len, selected)) + max(0, len(selected) - 1) * 2 <= 55
