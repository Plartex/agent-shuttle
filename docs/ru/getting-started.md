# Начало работы с Agent Shuttle

[English version / Английская версия](../getting-started.md)

Это руководство описывает процесс первоначальной настройки Agent Shuttle, проверку доступности локальных агентных сред (харнессов), запуск A2A- и MCP-серверов, а также отправку первых задач через Python, CLI и MCP.

---

## Требования

Agent Shuttle взаимодействует с локальными агентными средами разработки на вашем компьютере. Убедитесь в наличии:

- **Python 3.11 или новее**, установленный и доступный в `PATH`.
- Для **Codex**:
  - Установленное и авторизованное приложение Codex (десктоп или CLI).
  - Зависимость Python SDK (`openai-codex`) устанавливается автоматически вместе с Agent Shuttle.
- Для **Antigravity**:
  - Установленный и авторизованный CLI `agy` (интерактивно войдите через `agy`, затем проверьте командой `agy models`).
  - Проверьте авторизацию командой `agy models` в терминале.
- Для **OpenCode** (необязательно):
  - Установленный `opencode` (например, через npm: `npm i -g opencode-ai`).
  - Для локальных моделей: запущенная [Ollama](https://ollama.ai/) на `http://127.0.0.1:11434` с загруженной моделью (например, `ollama pull qwen3.5:9b`).
- Для **Claude Code** (необязательно):
  - Установленный CLI `claude` (`npm install -g @anthropic-ai/claude-code`).
  - Авторизованный аккаунт Anthropic либо настроенный локальный endpoint Ollama.

---

## Настройка из чекаута репозитория (Windows)

Для автономной разработки или тестирования непосредственно из репозитория:

```powershell
# 1. Клонирование репозитория
git clone https://github.com/Plartex/agent-shuttle.git
Set-Location agent-shuttle

# 2. Установка Python-пакета в отдельное окружение
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .

# 3. Укажите MCP-клиенту абсолютный путь к
#    .venv\Scripts\agent-shuttle-mcp.exe

# 4. Откройте MCP-клиент и вызовите ask_codex или ask_antigravity.
# При необходимости A2A-сервер запустится автоматически на время запроса.
```

Antigravity Desktop может читать `%USERPROFILE%\.gemini\config\mcp_config.json` вместо локальной конфигурации MCP в чекауте. Если нужен постоянный A2A-сервер, запустите команду `agent-shuttle serve` ниже и остановите её сочетанием Ctrl+C.

---

## Использование Agent Shuttle как библиотеки в другом проекте

Вы можете установить Agent Shuttle в любой другой Python-проект без сохранения исходного чекаута репозитория:

```powershell
# В каталоге вашего проекта:
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Установка напрямую из каталога чекаута или готового wheel-файла
pip install C:\path\to\agent-shuttle
```

После установки в `.venv` проекта:
- Команда `agent-shuttle` доступна по пути `.venv\Scripts\agent-shuttle.exe`.
- Сервер MCP `agent-shuttle-mcp` доступен по пути `.venv\Scripts\agent-shuttle-mcp.exe`.
- Модули можно импортировать в Python: `from agent_shuttle import ShuttleClient, connect_harness, HarnessLaunch`.

---

## Проверка обнаружения харнессов (Discovery)

Перед запуском серверов проверьте, какие харнессы обнаружены в системе:

```powershell
agent-shuttle discover
```

Пример вывода:
```json
{
  "codex": "agent-shuttle",
  "antigravity": "C:\\Users\\username\\AppData\\Local\\agy\\bin\\agy.exe",
  "opencode": "C:\\Users\\username\\AppData\\Roaming\\npm\\node_modules\\opencode-ai\\bin\\opencode.exe",
  "claude_code": "C:\\Users\\username\\.local\\bin\\claude.exe"
}
```

Если исполняемый файл расположен вне стандартных путей поиска, укажите его явно:
```powershell
agent-shuttle discover --agy-command 'D:\tools\agy.exe'
# Или через переменную окружения:
$env:BRIDGE_AGY_COMMAND = 'D:\tools\agy.exe'
```

---

## Запуск серверов

### Сервер Codex
Запускает бэкенд Codex, привязанный к текущему каталогу проекта, на порту 8765:
```powershell
agent-shuttle serve codex --workspace . --port 8765
```

### Сервер Antigravity
Запускает бэкенд Antigravity CLI в headless-режиме на порту 8766:
```powershell
agent-shuttle serve antigravity --workspace . --port 8766
```

### OpenCode или Claude Code (профиль Ollama)
OpenCode и Claude Code работают на основе JSON-профилей. Готовые примеры профилей находятся в каталоге `examples/`:

```powershell
# OpenCode с локальной Ollama
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Claude Code с локальной Ollama
agent-shuttle serve profile --profile .\examples\claude-code-ollama.json --workspace . --port 8768
```

Параметр `--workspace` задает рабочий каталог агента и перекрывает относительное значение `workspace` из JSON-файла.

---

## Проверка статуса сервера

Каждый запущенный сервер предоставляет read-only эндпоинты HTTP на интерфейсе loopback:

- **Проверка идентификации** (мгновенная, без вызова языковых моделей):
  ```powershell
  Invoke-RestMethod http://127.0.0.1:8765/bridge/identity
  ```
  Возвращает `{"agent": "codex", "backend": "codex_app_server", "workspace": "C:\\path\\to\\project", ...}`.

- **Возможности и квоты** (опрашивает метаданные харнесса):
  ```powershell
  agent-shuttle info http://127.0.0.1:8765
  ```

---

## Отправка первых задач

### Через CLI
```powershell
agent-shuttle ask http://127.0.0.1:8765 "Проверь тесты и перечисли упавшие."
```

С выбором модели и усилия рассуждений:
```powershell
agent-shuttle ask http://127.0.0.1:8766 "Объясни архитектуру репозитория." `
  --model gemini-3.8-flash-medium --reasoning-effort medium
```

### Через Python API
```python
import asyncio
from agent_shuttle import ShuttleClient

async def main():
    client = ShuttleClient()
    result = await client.ask(
        "http://127.0.0.1:8765",
        "Перечисли точки входа Python из pyproject.toml",
    )
    print("ID задачи:", result.task_id)
    print("Статус:", result.state)
    print("Ответ:\n", result.text)

asyncio.run(main())
```

### Через Model Context Protocol (MCP)
Добавьте Agent Shuttle в конфигурацию MCP-клиента. Для Antigravity Desktop проверьте `%USERPROFILE%\.gemini\config\mcp_config.json`: файл `.agents/mcp_config.json` в чекауте может не быть активной конфигурацией.

Пример конфигурации:
```toml
[mcp_servers.agent_shuttle]
command = "C:/path/to/agent-shuttle/.venv/Scripts/agent-shuttle-mcp.exe"
tool_timeout_sec = 1800

[mcp_servers.agent_shuttle.env]
BRIDGE_WORKSPACE = "C:/path/to/project"
```

Пример TOML подходит клиенту, который читает секции `mcp_servers`. Для клиента с JSON используйте те же значения `command` и `env` в его формате. Для MCP-запросов отдельно запускать `agent-shuttle serve` не нужно: при отсутствии подходящего сервера Agent Shuttle временно запускает его на время вызова. `BRIDGE_WORKSPACE` задаёт проверяемый проект.

После этого инструменты можно вызывать прямо в диалоге с агентом:
```text
ask_antigravity(prompt="Проверь тесты в tests/test_backends.py", model="gemini-3.8-flash-medium")
ask_codex(prompt="Выполни рефакторинг функции в agent_bridge/discovery.py", model="gpt-5.6-terra")
```

---

## Дальнейшие шаги

- Изучите полный [Справочник API](api.md) для программного управления серверами и сессиями.
- Ознакомьтесь с [Руководством по безопасности и разрешениям](permissions.md) перед включением `full_access`.
- Узнайте подробнее о внутреннем устройстве и протоколе в разделе [Архитектура](architecture.md).
- В случае затруднений обратитесь к разделу [Диагностика и устранение неполадок](troubleshooting.md).
