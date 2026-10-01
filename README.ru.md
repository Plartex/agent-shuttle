# Agent Shuttle

[![Тесты](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml/badge.svg)](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml)
[![Лицензия MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![A2A Protocol 1.0](https://img.shields.io/badge/A2A-1.0_JSON--RPC-blue)](https://a2a-protocol.org/latest/)
[![MCP](https://img.shields.io/badge/MCP-tools-green)](https://modelcontextprotocol.io/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

[English version / Английская версия](README.md)

Agent Shuttle даёт Python-приложениям единый способ работать с **Codex**, **Antigravity**, **OpenCode** и **Claude Code**. Библиотека запускает локальные задачи агентов, сохраняет многошаговые сессии и предоставляет доступ через [A2A 1.0 JSON-RPC](https://a2a-protocol.org/latest/) и [MCP](https://modelcontextprotocol.io/).

Библиотека позволяет агентам и внешним программам делегировать задачи агентам-партнерам, повторно использовать контекст многошаговых бесед, запрашивать актуальные каталоги моделей и квоты аккаунтов, а также разграничивать политики доступа к инструментам — исключительно на локальном интерфейсе (`127.0.0.1`) без передачи облачных API-ключей.

---

## Поддерживаемые агентные среды (харнессы)

| Харнесс | Основной механизм интеграции | Авторизация и доступ к моделям | Ключевые возможности |
|---|---|---|---|
| **Codex** | Официальный Python SDK `openai-codex` | Авторизация локального Codex App Server | Песочницы (`workspace_write`, `read_only`, `full_access`), каталог моделей и усилий рассуждения, чтение квот через `account/rateLimits/read`. |
| **Antigravity** | Официальный CLI `agy` в headless-режиме (`-p` / `stream-json`) | Авторизованный аккаунт Antigravity | Считывание моделей, усилий и квот `/usage` в реальном времени. Режим стандартных настроек либо полный доступ (`--dangerously-skip-permissions`). Опциональный устаревший SDK backend. |
| **OpenCode** | Управляемый локальный HTTP-сервер (`--pure serve`) | Локальная Ollama или сторонние провайдеры | Настройка через JSON-профиль, гранулярные политики инструментов (`no_tools`, `read_only`, `workspace_write`, `full_access`), варианты моделей. |
| **Claude Code** | Управляемый CLI в print-режиме (`claude -p`) | Локальный endpoint Ollama или Anthropic | Настройка через JSON-профиль, изолированные временные конфигурации сессий, возобновление диалога, безопасный режим против полного доступа. |

---

## Установка

Agent Shuttle требует **Python 3.11+** и работает на Windows, Linux и macOS.

### Установка с GitHub

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "git+https://github.com/Plartex/agent-shuttle.git"
```

Пакет пока не опубликован в PyPI. Для разработки клонируйте репозиторий и установите его в редактируемом режиме.

### Установка из локального чекаута

Библиотеку можно установить напрямую из каталога репозитория в виртуальное окружение вашего проекта:

```powershell
# Создание и активация виртуального окружения
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Установка в стандартном или редактируемом режиме
pip install C:\path\to\agent-shuttle
# или редактируемый режим для разработки:
# pip install -e C:\path\to\agent-shuttle
```

После установки консольные утилиты (`agent-shuttle`, `agent-shuttle-mcp`) и Python API (`agent_shuttle`) становятся доступны в этом окружении. Исходный каталог репозитория для последующей работы клиентских проектов не требуется. Старые импорты `agent_bridge` и команды `agent-bridge` сохранены как совместимые алиасы; ключи метаданных A2A `agent_bridge.*` не меняются.

---

## Быстрый старт (Windows PowerShell)

Для автономной разработки и тестирования внутри этого репозитория:

1. **Инициализация окружения:**
   ```powershell
   & .\Install-Shuttle.ps1
   ```
   *(Если Python 3.11+ не добавлен в `PATH`, предварительно задайте `$env:BRIDGE_BOOTSTRAP_PYTHON = 'C:\path\to\python.exe'`).*

2. **Генерация конфигураций MCP:**
   ```powershell
   & .\Configure-Shuttle-Mcp.ps1
   ```
   Скрипт создаст файлы `.codex/config.toml` и `.agents/mcp_config.json` с абсолютными путями к Python-окружению.

3. **Запуск стандартных серверов Codex и Antigravity:**
   ```powershell
   & .\Start-Shuttle.ps1
   ```
   Скрипт запустит фоновые серверы на портах loopback:
   - Codex: `http://127.0.0.1:8765` (agent card: `http://127.0.0.1:8765/.well-known/agent-card.json`)
   - Antigravity: `http://127.0.0.1:8766` (agent card: `http://127.0.0.1:8766/.well-known/agent-card.json`)

4. **Остановка фоновых серверов:**
   ```powershell
   & .\Stop-Shuttle.ps1
   ```

Логи выполнения и PID-файлы сохраняются в каталоге `.runtime/` и исключены из Git.

---

## Минимальные примеры

### 1. Python API

```python
import asyncio
from pathlib import Path
from agent_shuttle import ShuttleClient, HarnessLaunch, connect_harness

async def main():
    client = ShuttleClient()

    # Запрос каталога моделей и информации о квотах в реальном времени
    info = await client.info("http://127.0.0.1:8766")
    print("Выбранная модель:", info["capabilities"]["selected_model"])

    # Одиночный вызов агента
    result = await client.ask(
        "http://127.0.0.1:8766",
        "Объясни структуру проекта и перечисли точки входа.",
        model="gemini-3.8-flash-medium",
        reasoning_effort="medium",
    )
    print(f"[{result.state}] Задача {result.task_id}:\n{result.text}")

    # Повторно используемая сессия (сохраняет контекст диалога)
    async with client.session(
        "http://127.0.0.1:8765",
        model="gpt-5.6-terra",
        reasoning_effort="high",
    ) as session:
        step1 = await session.ask("Какие миграции базы данных еще не применены?")
        step2 = await session.ask("Сгенерируй SQL для применения первой из них.")
        print("Ответ второго шага:", step2.text)
        print("Расход токенов второго шага:", step2.usage)

    # Управление жизненным циклом: переиспользование существующего или запуск временного сервера
    launch = HarnessLaunch(
        name="antigravity",
        url="http://127.0.0.1:8766",
        workspace=Path.cwd(),
    )
    async with connect_harness(launch) as conn:
        print("Подключено к:", conn.url, "(запущен временный сервер:", conn.started, ")")

asyncio.run(main())
```

### 2. Интерфейс командной строки (CLI)

```powershell
# Поиск локально установленных харнессов без запуска моделей
agent-shuttle discover

# Запуск A2A-сервера для Codex
agent-shuttle serve codex --workspace . --port 8765

# Запуск A2A-сервера для Antigravity (в режиме CLI)
agent-shuttle serve antigravity --workspace . --port 8766

# Запуск A2A-сервера из профиля (OpenCode или Claude Code)
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Запрос возможностей сервера, каталога моделей и лимитов квот
agent-shuttle info http://127.0.0.1:8765

# Отправка задачи из командной строки
agent-shuttle ask http://127.0.0.1:8765 "Сделай краткое резюме последних изменений" --model gpt-5.6-terra
```

### 3. Model Context Protocol (MCP)

Запуск stdio-сервера MCP:
```powershell
agent-shuttle-mcp
# или: python -m agent_shuttle.mcp_server
```

Доступные инструменты MCP:
- `ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?, workspace?)`: Использует подходящий локальный A2A-сервер или временно запускает его. Встроенные идентификаторы: `codex`, `antigravity`, `opencode`, `claude_code`; последним двум нужен профиль или модель Ollama.
- `get_agent_info(agent_id, workspace?)`: Возвращает модели и квоты, при необходимости запуская временный сервер.
- `ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`: При необходимости запускает сервер Antigravity.
- `ask_codex(prompt, model?, reasoning_effort?, workspace?)`: При необходимости запускает сервер Codex.
- `get_antigravity_info(workspace?)` и `get_codex_info()`: Читают возможности и квоты без расхода модельных запросов.

---

## Ключевые концепции

### Обнаружение харнессов (Discovery)
Команда `agent-shuttle discover` (или вызов `discover_harnesses()` в Python) проверяет установленные исполняемые файлы без старта процессов и загрузки весов. Проверяется переменная `PATH` и стандартные каталоги установки операционной системы (`%LOCALAPPDATA%\agy\bin`, глобальные пути npm и т.д.). Пути можно переопределить переменными окружения (`BRIDGE_AGY_COMMAND`) или флагами CLI (`--agy-command`, `--opencode-command`, `--claude-command`).

### Выбор модели и уровня рассуждений (Reasoning Effort)
Параметры передаются через метаданные A2A (`agent_bridge.model`, `agent_bridge.reasoning_effort`):
- **Antigravity:** Уровень рассуждений встроен в идентификаторы моделей (например, `gemini-3.8-flash-medium`). При одновременной передаче `--model` и `--effort` значения обязаны совпадать.
- **Codex:** Модель и усилие рассуждения настраиваются независимо согласно каталогу, возвращаемому `get_codex_info`.
- **OpenCode и Claude Code:** Профили определяют списки `allowed_models` и допустимые `reasoning_efforts` (например, варианты модели для Ollama или флаги CLI).

### Рабочие каталоги (Workspaces) и изоляция сессий
- Каждый сервер привязывается к строго проверенному каноническому каталогу проекта.
- `connect_harness()` сверяет рабочий каталог существующего сервера с запрошенным перед его повторным использованием.
- **Сессии:** Объект `BridgeSession` поддерживает непрерывную беседу на протяжении нескольких вызовов `ask()`. Параметры сессии (модель, reasoning effort, политика инструментов) фиксируются при открытии и не могут изменяться между шагами. Неактивные сессии автоматически закрываются через 30 минут бездействия.
- **Жизненный цикл задач:** `ShuttleClient.submit()` сразу возвращает `TaskHandle`. Доступны `status()`, ограниченное по времени `wait(timeout)`, `events()`, `result()` и `cancel()`; к работающей задаче можно вернуться по ID через `client.task(url, task_id)`. Истечение времени ожидания не останавливает агента. Необязательный UUID `request_id` защищает от повторного запуска при повторной отправке в течение жизни сервера. Задачи пока хранятся в памяти и не переживают перезапуск сервера. Подробнее — в [справочнике API](docs/ru/api.md#taskhandle-и-bridgeevent).

### Безопасность и политики инструментов (Tool Policies)
Agent Shuttle определяет четыре стандартные политики инструментов:
- `no_tools`: Полный запрет вызова инструментов.
- `read_only`: Разрешены операции поиска и чтения файлов без модификации.
- `workspace_write`: Разрешена модификация файлов внутри рабочего каталога.
- `full_access`: Явное снятие всех ограничений инструментов и запросов подтверждения.

> [!WARNING]
> Режим `full_access` (или флаг `--agy-dangerously-skip-permissions` для Antigravity) снимает все ограничения на подтверждение вызовов инструментов на уровне всего сервера для всех входящих запросов. Никогда не включайте этот режим для недоверенных задач и не выставляйте порты наружу. Antigravity CLI в headless-режиме не обеспечивает ограничение `read_only` и отклоняет такие запросы.

---

## Тестирование

Agent Shuttle содержит полный набор офлайн-тестов на базе имитационных (fake) бэкендов, не требующих доступа к сети, учетных записей или расхода квот:

```powershell
python -m unittest discover -s tests -v
```

Интеграционные тесты с реальными моделями запускаются выборочно при наличии соответствующего окружения (например, `BRIDGE_LIVE_OLLAMA_MODEL=qwen3.5:9b` или `BRIDGE_LIVE_AGY_FULL_ACCESS=1`). Подробные инструкции приведены в [CONTRIBUTING.ru.md](CONTRIBUTING.ru.md).

---

## Разделы документации

- [Руководство по началу работы](docs/ru/getting-started.md)
- [Справочник API](docs/ru/api.md)
- [Разрешения и безопасность](docs/ru/permissions.md)
- [Архитектура и протокол](docs/ru/architecture.md)
- [Диагностика и устранение неполадок](docs/ru/troubleshooting.md)
- [Участие в разработке](CONTRIBUTING.ru.md)
- [Политика безопасности](SECURITY.ru.md)
