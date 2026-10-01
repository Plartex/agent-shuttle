# Проверка Agent Shuttle из Antigravity

Проверка выполняется на локальной версии Agent Shuttle. В конфигурации MCP Antigravity укажите Python из окружения Agent Shuttle и модуль `agent_shuttle.mcp_server`. Для каталога проекта задайте `BRIDGE_WORKSPACE`; можно также передать `workspace` в самом вызове. Перезапустите MCP-подключение после изменения конфигурации или исходников. Для `gpt-6-sol` требуется `openai-codex>=0.155.1`: с установленной версией `0.154.0` воспроизводится ошибка 400 `model is not supported when using Codex with a ChatGPT account`.

## 1. Автозапуск A2A-сервера через MCP

Не запускайте `Start-Shuttle.ps1` или `agent-shuttle serve`. В чате Antigravity попросите вызвать инструмент MCP `ask_codex` сервера `agent-shuttle`:

```text
prompt: Прочитай pyproject.toml в этом проекте. Верни только название пакета и версию. Файлы не меняй.
workspace: C:/путь/к/agent-bridge
```

Параметр `model` для первого прогона можно опустить. Для проверки именно GPT-6 Sol передайте `model="gpt-6-sol"`. Если уже работающий A2A-сервер сообщает старый каталог без этой модели, обновлённый MCP запустит отдельный временный сервер вместо повторного использования старого. Перезапустите MCP-подключение Antigravity, чтобы оно загрузило эту правку. Название модели самого Antigravity не следует автоматически копировать в параметр модели Codex.

Ожидается `TASK_STATE_COMPLETED` и ответ `agent-shuttle 0.5.0` (для текущей версии исходников). MCP сам запустит временный локальный A2A-сервер, если подходящий сервер не работает, и завершит его после ответа. Если сервер уже работает, MCP сверит харнесс и рабочий каталог и повторно использует его.

У Antigravity Desktop активная пользовательская конфигурация может находиться в `%USERPROFILE%/.gemini/config/mcp_config.json`, даже если в проекте есть `.agents/mcp_config.json`. Если ошибка сохраняется после перезапуска, проверьте глобальный файл и путь `mcpServers.agent-bridge.command`: он должен вести в `agent-bridge/.venv/Scripts/python.exe` с `openai-codex>=0.155.1`. В проверенном случае Antigravity продолжал запускать старое корневое окружение именно из глобального файла. Имя сервера `agent-bridge` и модуль `agent_bridge.mcp_server` совместимы с Agent Shuttle. Передайте `workspace` явно, если в конфигурации нет `BRIDGE_WORKSPACE`.

## 2. Жизненный цикл задач

Из терминала Antigravity в каталоге Agent Shuttle запустите:

```powershell
& .\.venv\Scripts\python.exe examples\lifecycle_smoke.py
```

Скрипт создаёт временный сервер и реальную задачу Codex с политикой `read_only`. Он проверяет `submit`, начальный `status`, поток `events`, `wait`, повторное открытие по ID и `cancel` второй задачи. Ожидаются `TASK_STATE_WORKING`, событие `TASK_STATE_COMPLETED`, совпадающие финальный и повторно прочитанный статусы, затем `TASK_STATE_CANCELED` для второй задачи. Для запуска Codex нужен доступ к его локальному состоянию в профиле пользователя.

У MCP-инструмента `ask_codex` сейчас однократный интерфейс: он возвращает финальный результат. Поэтапный API `submit/status/events/cancel` проверяется Python-скриптом выше.
