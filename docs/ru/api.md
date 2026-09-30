# Справочник API Agent Shuttle

[English version / Английская версия](../api.md)

В этом документе приведен полный справочник единого публичного Python API, эндпоинтов HTTP, команд CLI и инструментов Model Context Protocol (MCP), предоставляемых Agent Shuttle.

---

## Справочник Python API

Публичные классы и функции импортируются напрямую из пакета `agent_shuttle`:

```python
from agent_shuttle import (
    ShuttleClient,
    BridgeResult,
    BridgeEvent,
    BridgeSession,
    TaskHandle,
    HarnessLaunch,
    BridgeConnection,
    connect_harness,
    AgentProfile,
    ToolPolicy,
    build_profile,
    discover_harnesses,
    AntigravityPermissionDenied,
    AntigravityAuthenticationError,
)
```

### `ShuttleClient`

Основной клиент Agent Shuttle и других серверов A2A 1.x, поддерживающих задачи. Для `submit()` и `ask()` сервер должен возвращать A2A Task, а не только сообщение.

```python
client = ShuttleClient(timeout_seconds: float = 1800)
```

#### Методы

- **`async def ask(peer_url: str, prompt: str, model: str | None = None, *, reasoning_effort: str | None = None, read_only: bool = False, tool_policy: str | None = None, session_id: str | None = None, request_id: str | None = None) -> BridgeResult`**
  Отправляет задачу и ждёт результат. При отмене вызывающего кода или истечении общего тайм-аута запрашивает удалённую отмену.
  - `prompt`: Непустая строка с задачей.
  - `model`: Идентификатор целевой модели агента.
  - `reasoning_effort`: Уровень рассуждений (например, `low`, `medium`, `high`, `none`).
  - `read_only`: Устаревший булев флаг только для чтения (сохранен для совместимости).
  - `tool_policy`: Политика инструментов: `"no_tools"`, `"read_only"`, `"workspace_write"` или `"full_access"`.
  - `session_id`: Идентификатор UUID для продолжения существующей сессии.
  - `request_id`: Необязательный UUID для защиты повторной отправки в течение жизни сервера. Повтор с другими аргументами отклоняется.
  - Возвращает: `BridgeResult`.

- **`async def submit(..., request_id: str | None = None) -> TaskHandle`**
  Принимает те же настройки, что `ask()`, и возвращается сразу после создания задачи A2A. Прекращение ожидания вызывающим кодом не отменяет отправленную задачу.

- **`task(peer_url: str, task_id: str) -> TaskHandle`**
  Восстанавливает доступ к задаче по ID, пока работает тот же экземпляр сервера.

- **`task_status(peer_url, task_id)` / `cancel_task(peer_url, task_id)`**
  Получают текущее состояние задачи A2A или запрашивают её отмену.

- **`def session(peer_url: str, model: str | None = None, *, reasoning_effort: str | None = None, read_only: bool = False, tool_policy: str | None = None) -> BridgeSession`**  
  Создает объект контекстного менеджера `BridgeSession` с зафиксированными настройками. Рекомендуется использовать с `async with`.

- **`async def info(peer_url: str) -> dict`**  
  Возвращает полный снимок: каталог моделей, уровни рассуждений и группы квот аккаунта в реальном времени.

- **`async def identity(peer_url: str) -> dict`**  
  Легковесный запрос идентификации: `agent`, `backend`, фактический `pid` серверного процесса, `workspace`, `read_only_tools`, `max_tool_policy`, `agy_permission_mode` и `agy_turn_timeout_seconds`. **Не запускает** исполняемые файлы моделей и CLI.

- **`async def capabilities(peer_url: str) -> dict`**  
  Возвращает список моделей, текущую выбранную модель, параметры рассуждений и предел политик инструментов.

- **`async def usage(peer_url: str) -> dict`**  
  Возвращает группы квот аккаунта, процент использованных/оставшихся ресурсов и время сброса окна.

- **`async def close_session(peer_url: str, session_id: str) -> bool`**  
  Явно закрывает сессию на сервере и освобождает ресурсы.

---

### `TaskHandle` и `BridgeEvent`

```python
handle = await client.submit(url, "Проверь проект", request_id=my_uuid)
print(handle.task_id)
snapshot = await handle.wait(timeout=30)  # Задача может ещё работать; это не отмена.
if snapshot.state == "TASK_STATE_WORKING":
    handle = client.task(url, handle.task_id)
    result = await handle.result()

# Явная остановка активной задачи:
# cancelled = await handle.cancel()
```

`status()` возвращает снимок `BridgeResult`. Если срок `wait(timeout)` истёк, метод возвращает последний снимок; `result()` ждёт терминального состояния или запроса ввода. `events()` выдаёт живые события `BridgeEvent(kind, task_id, state, text)`; после обрыва потока вызовите `status()` для получения достоверного состояния. Повторный `cancel()` уже отменённой задачи возвращает её статус. Сейчас задачи хранятся **в памяти**: ID и привязки `request_id` не переживают перезапуск сервера. Отмена шага постоянной сессии закрывает её нативную сессию; для продолжения создайте новую.

---

### `BridgeResult`

Датакласс с результатом выполнения задачи:

```python
@dataclass(frozen=True)
class BridgeResult:
    peer: str
    task_id: str | None
    context_id: str | None
    state: str
    text: str
    usage: dict[str, int] | None = None
    details: dict | None = None
```

- `state`: Имя состояния задачи (например, `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `message`).
- `text`: Извлеченный текстовый ответ агента.
- `usage`: Словарь нормализованных счетчиков токенов (`input_tokens`, `output_tokens`, `total_tokens`, `thinking_tokens`, `cache_read_tokens`, `cache_write_tokens`).
- `details`: Метаданные поставщика (например, статус кеша или число повторных попыток handshake).

---

### `BridgeSession`

Управляет состоянием многошаговой беседы с фиксированными параметрами:

```python
async with client.session(url, model="gpt-5.6-terra") as session:
    res1 = await session.ask("Какие файлы изменились в коммите abc?")
    res2 = await session.ask("Покажи диффы для первого файла.")
```

- Настройки (`model`, `reasoning_effort`, `tool_policy`) фиксируются при создании сессии и не могут быть изменены между вызовами.
- Вызовы сериализуются через внутренний `asyncio.Lock` — одновременно в сессии выполняется не более одного шага.
- При выходе из блока `async with` автоматически вызывается `close()`.

---

### `HarnessLaunch` и `connect_harness`

Программное управление жизненным циклом локальных серверов Bridge. Автоматически переиспользует уже запущенный сервер (если совпадают бэкенд, канонический рабочий каталог и разрешения) или поднимает временный изолированный экземпляр.

```python
@dataclass(frozen=True)
class HarnessLaunch:
    name: str                           # "codex", "antigravity", "opencode", "claude_code"
    url: str                            # URL на loopback, например "http://127.0.0.1:8765"
    workspace: Path                     # Канонический путь к каталогу проекта
    model: str | None = None
    command: str | None = None          # Путь к исполняемому файлу
    profile_path: Path | None = None    # Путь к файлу профиля (OpenCode/Claude Code)
    ollama_url: str = "http://127.0.0.1:11434"
    log_path: Path | None = None        # Путь к файлу логов
    start_if_missing: bool = True       # Если False, требует готовый сервер
    tool_policy: str | None = None      # "no_tools", "read_only", "workspace_write", "full_access"
    agy_dangerously_skip_permissions: bool = False
    agy_turn_timeout_seconds: float = 300
```

Пример:
```python
launch = HarnessLaunch(name="antigravity", url="http://127.0.0.1:8766", workspace=Path.cwd())
async with connect_harness(launch) as connection:
    # connection.url готов к работе
    # connection.started равен True, если сервер был запущен временно
    result = await ShuttleClient().ask(connection.url, "Проверь тесты")
```

При завершении контекста временные процессы и их дочернее дерево гарантированно завершаются (с помощью `taskkill /PID ... /T /F` на Windows).

---

### `AgentProfile` и `ToolPolicy`

Серверные профили для OpenCode и Claude Code:

```python
class ToolPolicy(str, Enum):
    NO_TOOLS = "no_tools"
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"
```

- **`AgentProfile.from_file(path: Path, *, workspace_override: Path | None = None) -> AgentProfile`**: Загружает профиль из JSON-файла.
- **`AgentProfile.from_mapping(data: dict) -> AgentProfile`**: Проверяет структуру словаря.
- **`profile.resolve(model, reasoning_effort, tool_policy) -> ProfileSelection`**: Проверяет, что запрошенные клиентом модель и политика инструментов не выходят за рамки `allowed_models` и `max_tool_policy`. Предотвращает несанкционированное повышение привилегий.

---

### `discover_harnesses`

```python
def discover_harnesses(commands: dict[str, str] | None = None) -> dict[str, str]
```

Сканирует `PATH` и стандартные системные каталоги на наличие установленных исполняемых файлов (`codex`, `antigravity`, `opencode`, `claude_code`). Возвращает словарь «харнесс → путь к бинарнику». Не запускает процессы и не опрашивает модели.

---

### `AntigravityPermissionDenied`

Исключение, выбрасываемое в случае, если Antigravity CLI возвращает `status: SUCCESS`, но запрошенные инструменты были заблокированы политикой разрешений (soft denial).

### `AntigravityAuthenticationError`

Исключение означает, что Antigravity CLI не может получить доступ к своей учётной записи из процесса Bridge. Если CLI также сообщает об отказе доступа к своей конфигурации, запустите Bridge от имени вошедшего пользователя вне песочницы вызывающего процесса.

---

## Справочник эндпоинтов HTTP

Каждый сервер Agent Shuttle A2A предоставляет следующие HTTP-эндпоинты на интерфейсе loopback (`127.0.0.1`):

| Эндпоинт | Метод | Описание |
|---|---|---|
| `/.well-known/agent-card.json` | `GET` | Визитная карточка агента A2A 1.0 JSON (описание возможностей и навыков). |
| `/` | `POST` | Точка входа JSON-RPC по протоколу A2A 1.0. |
| `/bridge/identity` | `GET` | Быстрая проверка идентификатора (бэкенд, канонический workspace, режим разрешений, тайм-аут хода). Быстрая проверка готовности. |
| `/bridge/info` | `GET` | Объединенный снимок возможностей, каталога моделей и квот аккаунта. |
| `/bridge/capabilities` | `GET` | Список моделей, текущая модель, уровни рассуждений и предел политик инструментов. |
| `/bridge/usage` | `GET` | Квоты аккаунта, лимиты окон и время сброса. |
| `/bridge/sessions/{session_id}` | `DELETE` | Закрывает сессию и освобождает ресурсы бэкенда. |

---

## Интерфейс командной строки (CLI)

Точка входа — `agent-shuttle` (или `python -m agent_shuttle`). Старые имена `BridgeClient`, `agent_bridge` и `agent-bridge` сохранены.

### `agent-shuttle serve`

Запускает A2A-сервер:

```powershell
# Запуск Codex
agent-shuttle serve codex --port 8765 [--workspace <КАТАЛОГ>]

# Запуск Antigravity CLI
agent-shuttle serve antigravity --port 8766 [--workspace <КАТАЛОГ>] `
  [--agy-command <ПУТЬ>] `
  [--agy-turn-timeout-seconds 300] `
  [--agy-dangerously-skip-permissions]

# Запуск OpenCode или Claude Code из профиля
agent-shuttle serve profile --profile .\profile.json --port 8767 [--workspace <КАТАЛОГ>]
```

### `agent-shuttle ask`

Отправляет задачу:
```powershell
agent-shuttle ask <URL> "<ПРОМПТ>" `
  [--model <МОДЕЛЬ>] `
  [--reasoning-effort <УСИЛИЕ>] `
  [--tool-policy <no_tools|read_only|workspace_write|full_access>]
```

### `agent-shuttle info`

Запрашивает и выводит JSON возможностей и квот по указанному `<URL>`:
```powershell
agent-shuttle info http://127.0.0.1:8765
```

### `agent-shuttle discover`

Выводит список найденных в системе харнессов:
```powershell
agent-shuttle discover `
  [--agy-command <ПУТЬ>] `
  [--opencode-command <ПУТЬ>] `
  [--claude-command <ПУТЬ>]
```

---

## Model Context Protocol (MCP)

Сервер MCP работает через стандартные потоки ввода-вывода (stdio):
```powershell
agent-shuttle-mcp
# или: python -m agent_shuttle.mcp_server
```

### Переменные окружения

- `BRIDGE_CODEX_URL`: URL сервера Codex (например, `http://127.0.0.1:8765`).
- `BRIDGE_ANTIGRAVITY_URL`: URL сервера Antigravity (например, `http://127.0.0.1:8766`).
- `BRIDGE_ANTIGRAVITY_WORKSPACE`: Рабочий каталог по умолчанию для временных серверов Antigravity.
- `BRIDGE_AGENTS_JSON`: JSON-словарь соответствия идентификаторов профилей и локальных адресов:
  ```json
  {"opencode-local": "http://127.0.0.1:8767", "claude-local": "http://127.0.0.1:8768"}
  ```

### Доступные инструменты MCP

1. **`ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?)`**  
   Передает задачу любому профилю, заданному в `BRIDGE_AGENTS_JSON`.

2. **`get_agent_info(agent_id)`**  
   Возвращает модели, усилия и квоты профильного агента.

3. **`ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`**  
   Делегирует задачу Antigravity. Если передан параметр `workspace`, динамически создает временный изолированный сервер Bridge.

4. **`ask_codex(prompt, model?, reasoning_effort?)`**  
   Делегирует задачу Codex с возможностью переопределения модели и усилия рассуждений.

5. **`get_antigravity_info(workspace?)`**  
   Возвращает возможности и квоты Antigravity `/usage` без расхода лимитов моделей.

6. **`get_codex_info()`**  
   Возвращает каталог моделей и лимиты квот Codex через `account/rateLimits/read`.
