# Справочник API Agent Shuttle

[English version / Английская версия](../api.md)

В этом документе приведен полный справочник единого публичного Python API, эндпоинтов HTTP, команд CLI и инструментов Model Context Protocol (MCP), предоставляемых Agent Shuttle.

---

## Справочник Python API

Публичные классы и функции импортируются напрямую из пакета `agent_shuttle`:

```python
from agent_shuttle import (
    TaskManager,
    Task,
    TaskStatus,
    TaskResult,
    Session,
    SessionInfo,
    AgentInfo,
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

### `TaskManager`: задачи на уровне библиотеки

`TaskManager` запускает агента напрямую из Python, без HTTP- или MCP-сервера. Менеджер должен оставаться открытым, пока работают задачи:

```python
from agent_shuttle import TaskManager

async with TaskManager.for_workspace(project_dir) as manager:
    task = await manager.dispatch("codex", "Проверь README", tool_policy="read_only")
    status = await task.wait(timeout=30)  # Истечение ожидания не отменяет работу.
    result = await task.result()
    print(result.state, result.text, result.usage)
```

`dispatch()` сразу возвращает `Task`. У объекта есть `status()`, `wait(timeout)`, `result()`, `result_page()`, `transcript()`, асинхронный итератор журнала `events()` и `cancel()`. Задачу можно снова открыть по ID через `manager.get()`, а список получить через `list_tasks()`. Состояния: `submitted`, `working`, `completed`, `failed`, `canceled`. Результат содержит структурированную ошибку, расход токенов, предупреждения, детали и поля модели и изменённых файлов. Фактическая модель остаётся неизвестной, если бэкенд её не сообщает. Изменённые файлы собираются по Git status только при чистом рабочем каталоге до задачи и отсутствии параллельных задач в этом менеджере; иначе статус `unavailable`. Наблюдение Git само по себе не доказывает, какой процесс изменил файл.

Страница журнала ограничена 60 000 символов. Большое событие показывается сокращённым с `data_truncated=True`; полный JSON доступен частями через `task.event_page(seq, cursor, limit)`.

`create_session()` создаёт `Session` с методами `dispatch()` и `end()`. `list_sessions()` возвращает их состояние. Параметры модели, effort и политики фиксируются для всей сессии; ходы выполняются по очереди. Сессия Codex с сохранённым ID нативного треда после перезапуска получает статус `suspended` и продолжится при следующем ходе. Если во время сбоя выполнялся ход, сессия всегда становится `interrupted` и требует нового разговора. Бэкенды без проверенной возможности возобновления тоже получают `interrupted`. `reap_idle_sessions()` закрывает неактивные сессии. `list_agents()` показывает базовые возможности, `agent_info()` получает доступный каталог моделей и квоты, а `set_preference()` / `get_preference()` сохраняют настройки по умолчанию.

По умолчанию база SQLite находится в `<workspace>/.agent-shuttle/library-tasks.sqlite3`. Параметры `database=...` и `memory=True` позволяют выбрать путь или хранение в памяти. Завершённые задачи сохраняются после перезапуска, прерванные получают ошибку `worker_interrupted` без повторного выполнения. A2A показывает эти же задачи через свой протокол.

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
  - `request_id`: Необязательный UUID для защиты повторной отправки. При `--task-db` привязка переживает перезапуск сервера. Повтор с другими аргументами отклоняется.
  - Возвращает: `BridgeResult`.

- **`async def submit(..., request_id: str | None = None) -> TaskHandle`**
  Принимает те же настройки, что `ask()`, и возвращается сразу после создания задачи A2A. Прекращение ожидания вызывающим кодом не отменяет отправленную задачу.

- **`task(peer_url: str, task_id: str) -> TaskHandle`**
  Восстанавливает доступ к задаче по ID. При `--task-db` сохранённая задача доступна и после перезапуска сервера.

- **`task_status(peer_url, task_id)` / `cancel_task(peer_url, task_id)`**
  Получают текущее состояние задачи A2A или запрашивают её отмену.

- **`def session(peer_url: str, model: str | None = None, *, reasoning_effort: str | None = None, read_only: bool = False, tool_policy: str | None = None) -> BridgeSession`**  
  Создает объект контекстного менеджера `BridgeSession` с зафиксированными настройками. Рекомендуется использовать с `async with`.

- **`async def info(peer_url: str) -> dict`**  
  Возвращает полный снимок: каталог моделей, уровни рассуждений и группы квот аккаунта в реальном времени.

- **`async def identity(peer_url: str) -> dict`**  
  Легковесный запрос идентификации: `agent`, `backend`, фактический `pid` серверного процесса, `workspace`, `read_only_tools`, `supported_tool_policies`, `default_tool_policy`, `max_tool_policy`, `agy_permission_mode` и `agy_turn_timeout_seconds`. Antigravity также сообщает `tool_policy_enforcement` и `tool_policy_notes`. **Не запускает** исполняемые файлы моделей и CLI.

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

`status()` возвращает снимок `BridgeResult`. Если конечный неотрицательный срок `wait(timeout)` истёк, метод возвращает последний снимок; `result()` ждёт терминального состояния или запроса ввода. `result_page(cursor, limit)` читает до 60 000 символов ответа, `transcript(cursor, limit)` — до 100 элементов истории и артефактов с ограничением 60 000 символов на страницу. Для продолжения используйте `next_cursor`. `events()` выдаёт живые события `BridgeEvent(kind, task_id, state, text, data)`; после обрыва потока вызовите `status()`. Повторный `cancel()` уже отменённой задачи возвращает её статус. Обычный A2A-сервер хранит задачи в памяти, если не задан `--task-db`. После перезапуска незавершённые задачи получают состояние failed с ошибкой прерывания и не запускаются повторно. Отмена шага постоянной сессии закрывает её нативную сессию; для продолжения создайте новую.

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
    error: dict | None = None
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
agent-shuttle serve codex --port 8765 [--workspace <КАТАЛОГ>] [--task-db <ФАЙЛ>]

# Запуск Antigravity CLI
agent-shuttle serve antigravity --port 8766 [--workspace <КАТАЛОГ>] `
  [--agy-command <ПУТЬ>] `
  [--agy-turn-timeout-seconds 300] `
  [--agy-dangerously-skip-permissions]

# Запуск OpenCode или Claude Code из профиля
agent-shuttle serve profile --profile .\profile.json --port 8767 [--workspace <КАТАЛОГ>]
```

`--task-db` включает хранение задач и `request_id` в SQLite. `--execution-timeout-seconds` (по умолчанию 1800) ограничивает весь шаг, `--stall-timeout-seconds` (по умолчанию 1800) — паузу между событиями прогресса. Для задач через MCP управляемый A2A-сервер использует SQLite автоматически.

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

- `BRIDGE_WORKSPACE`: Каталог проекта для временных серверов. Если не задан, используется рабочий каталог MCP-процесса; инструментам также можно передать `workspace`.
- `BRIDGE_CODEX_URL` и `BRIDGE_ANTIGRAVITY_URL`: Необязательные локальные адреса. Если подходящий сервер не запущен, MCP запускает его на время запроса.
- `BRIDGE_TASK_REGISTRY`: Необязательный путь к реестру задач MCP (по умолчанию `<BRIDGE_WORKSPACE>/.agent-shuttle/mcp-tasks.json`). Базы A2A-серверов находятся в `.agent-shuttle` целевого рабочего каталога.
- `BRIDGE_AGENTS_JSON`: Необязательные параметры запуска для произвольных идентификаторов агентов. Для встроенного идентификатора по-прежнему допустима строка URL:
  ```json
  {"opencode-local": {"harness": "opencode", "profile": "C:/profiles/opencode.json", "url": "http://127.0.0.1:8767"}}
  ```
  URL можно опустить: тогда выбирается свободный локальный порт. Для запуска пользовательских конфигураций OpenCode и Claude Code нужен путь к профилю. К прежней записи с произвольным ID и одним URL добавьте `harness` и `profile`.

### Доступные инструменты MCP

1. **`ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?, workspace?)`**
   Использует подходящий A2A-сервер или запускает его. Встроенным идентификаторам запись в `BRIDGE_AGENTS_JSON` не нужна.

2. **`get_agent_info(agent_id, workspace?)`**
   Возвращает модели, усилия и квоты, при необходимости запуская временный сервер.

3. **`ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`**  
   Делегирует задачу Antigravity, при необходимости запуская временный сервер.

4. **`ask_codex(prompt, model?, reasoning_effort?, workspace?)`**
   Делегирует задачу Codex с возможностью переопределения модели и усилия рассуждений, при необходимости запуская временный сервер.

5. **`get_antigravity_info(workspace?)`**  
   Возвращает возможности и квоты Antigravity `/usage` без расхода лимитов моделей.

6. **`get_codex_info()`**  
   Возвращает каталог моделей и лимиты квот Codex через `account/rateLimits/read`.

7. **`submit_task(agent_id, prompt, model?, reasoning_effort?, tool_policy?, workspace?, request_id?)`**
   Сразу возвращает `task_id` и оставляет управляемый A2A-сервер запущенным между вызовами. При неясном исходе отправки повторите её с тем же UUID `request_id`.

8. **`check_task(task_id)` / `wait_task(task_id, timeout_seconds=180)` / `cancel_task(task_id)`**
   Проверяют состояние, ждут с отдельным лимитом времени или явно отменяют задачу. Истечение времени ожидания не останавливает исполнение.

9. **`get_result(task_id, cursor=0, limit=60000)` / `get_transcript(task_id, cursor=0, limit=100)`**
   Читают ответ и историю страницами; продолжайте по `next_cursor` до `null`. Сохранённые результаты доступны после перезапуска MCP; прерванная работа получает состояние failed.
