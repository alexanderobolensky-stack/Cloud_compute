# Портал локальных ИИ-агентов

Веб-портал для управления и мониторинга ИИ-агентов, работающих на вашем компьютере:
запуск и остановка, автоперезапуск при сбоях, живые логи, загрузка CPU/памяти/видеопамяти,
проверки здоровья, отправка задач агентам и обзор локальных серверов моделей (Ollama, LM Studio,
llama.cpp, vLLM).

Работает полностью локально и офлайн: Python-бэкенд (FastAPI + psutil) и интерфейс без сборки
и внешних CDN.

## Быстрый старт

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m agent_portal
```

Откройте http://localhost:8700. В `agents.yaml` уже есть два демо-агента
(`examples/demo_agent.py`): «Исследователь» стартует сам, «Нестабильный кодер» падает через
минуту работы — на нём видно автоперезапуск. Третья запись наблюдает за `ollama serve`, если он запущен.

## Что умеет

| Раздел | Возможности |
|---|---|
| **Обзор** | ЦП, ОЗУ, GPU (через `nvidia-smi`), диск и сеть; у каждого агента — статус, живая дорожка загрузки ЦП, память, аптайм, кнопки запуска/остановки; лента событий |
| **Агент** | графики ЦП, памяти и видеопамяти за 5 мин / 15 мин / 1 час с линией лимита; живые логи с фильтром, скачиванием и очисткой; консоль задач; журнал событий агента; конфигурация |
| **Модели** | доступность серверов, установленные модели, модели в памяти (Ollama `/api/ps`), проверочный запрос со скоростью ток/с |
| **Обнаружение** | поиск запущенных процессов, похожих на ИИ-рантаймы и фреймворки агентов (Ollama, llama.cpp, vLLM, LM Studio, CrewAI, AutoGen, LangGraph и др.), добавление в мониторинг одной кнопкой |
| **Журнал** | запуски, падения, превышения лимита памяти, смена статуса проверки здоровья; фильтры по важности и агенту; вебхук для оповещений |

## Два вида агентов

- **Управляемый** (`kind: process`) — портал запускает команду в отдельной группе процессов,
  пишет stdout/stderr в `data/logs/<id>.log`, перезапускает по политике
  (`never`, `on-failure`, `always`) с экспоненциальной задержкой до 30 с и лимитом попыток
  подряд. Метрики считаются по всему дереву процессов (агент + дочерние).
- **Внешний** (`kind: external`) — агент запускается без портала (systemd, Docker, IDE);
  портал находит его процессы по регулярному выражению `process_match` и/или опрашивает `health_url`.

## Конфигурация: `agents.yaml`

```yaml
backends:
- name: Ollama
  type: ollama                      # или openai — для LM Studio, llama.cpp, vLLM, LocalAI
  url: http://127.0.0.1:11434
agents:
- id: my-agent                      # латиница, цифры, - и _
  name: Мой агент
  kind: process
  command: '{python} agent.py --port 8000'   # {python} — интерпретатор портала
  cwd: /path/to/project
  env:
    OPENAI_BASE_URL: http://127.0.0.1:11434/v1
  autostart: true
  restart_policy: on-failure
  max_restarts: 5
  max_memory_mb: 2048               # при превышении — перезапуск
  health_url: http://127.0.0.1:8000/health   # GET, код < 400 = здоров
  task_url: http://127.0.0.1:8000/task       # POST {"input": "..."} — консоль задач
  tags: [rag]
```

Изменения из интерфейса сохраняются в этот же файл (комментарии в нём при этом не сохраняются).
Правки вручную подхватываются при перезапуске портала.

### Подключение своего агента к консоли задач

Достаточно HTTP-эндпоинта, принимающего `POST` с JSON `{"input": "текст"}` и возвращающего
JSON или текст. Пример — `examples/demo_agent.py` (только стандартная библиотека).

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PORTAL_HOST` / `PORTAL_PORT` | `127.0.0.1` / `8700` | адрес сервера (также `--host`, `--port`) |
| `PORTAL_CONFIG` | `agents.yaml` | файл конфигурации (`--config`) |
| `PORTAL_DATA` | `data` | логи и база событий (`--data`) |
| `PORTAL_TOKEN` | — | токен доступа; обязателен при привязке не к localhost |
| `PORTAL_ALLOWED_HOSTS` | — | доп. имена хоста через запятую (например, `myhost.local`) |
| `PORTAL_SAMPLE_INTERVAL` | `2` | период сбора метрик, с |
| `PORTAL_HEALTH_INTERVAL` | `10` | период проверок здоровья, с |
| `PORTAL_HISTORY_SECONDS` | `3600` | глубина истории метрик в памяти, с |
| `PORTAL_WEBHOOK_URL` | — | POST с JSON для каждого предупреждения и ошибки (Slack/Telegram-бот/ntfy и т. п.) |

## Безопасность

Портал умеет запускать произвольные команды, поэтому:

- по умолчанию слушает только `127.0.0.1`; для доступа из сети нужно задать `PORTAL_TOKEN`
  (без него портал откажется стартовать на внешнем адресе);
- проверяет заголовок `Host` (защита от DNS rebinding) и `Origin` у WebSocket;
- все изменяющие запросы требуют заголовок `X-Portal-Request: 1`, который посторонняя
  страница в браузере не может отправить без CORS — так что сайты в соседних вкладках
  не смогут запустить что-либо через ваш портал.

## API

REST-документация доступна на http://localhost:8700/docs. Основное:

```
GET    /api/overview                      система + статусы всех агентов
GET    /api/agents | /api/agents/{id}
POST   /api/agents                        создать (тело — как в agents.yaml)
PUT    /api/agents/{id}  DELETE /api/agents/{id}
POST   /api/agents/{id}/start|stop|restart
GET    /api/agents/{id}/metrics | /logs?tail=500
POST   /api/agents/{id}/task              {"input": "..."}
GET    /api/events?agent_id=&level=
GET    /api/discovery    GET|PUT /api/backends    POST /api/backends/{i}/prompt
WS     /ws/live  (снимки каждые 2 с + события)   WS /ws/agents/{id}/logs
```

Пример из скрипта: `curl -X POST -H 'X-Portal-Request: 1' localhost:8700/api/agents/researcher/restart`.

## Разработка

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Структура: `agent_portal/supervisor.py` — жизненный цикл процессов, `monitor.py` — метрики и
проверки здоровья, `discovery.py` — обнаружение, `backends.py` — серверы моделей,
`security.py` — защита запросов, `app.py` — API, `static/` — интерфейс.
