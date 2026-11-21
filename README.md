# lab 5 by vilimetov t.s. 241-334 — Распределённая трассировка (OpenTelemetry + Grafana Stack)

Учебный проект по распределённой трассировке, структурированному логированию и метрикам на Python (`FastAPI` + `httpx` + `OpenTelemetry`) для предметной области **«Распределение заказов между водителями такси»**.

## 1. Проверка конфигурации OTel Collector

Перед запуском убедитесь, какой OTLP-протокол открыт в `Lecture10.Tracing/infrastructure/otel-collector/` (обычно включены оба: `grpc` на `4317` и `http` на `4318`):

- По умолчанию сервисы отправляют телеметрию по **OTLP/gRPC (`http://localhost:4317`)**.
- Если в конфиге коллектора используется только HTTP (`4318`), задайте перед запуском сервисов переменные окружения:
  - **Bash:** `export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
  - **PowerShell:** `$env:OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"`

## 2. Запуск инфраструктуры наблюдаемости

Из корня `lab5/`:

```bash
cd Lecture10.Tracing
docker compose up -d
docker compose ps
```

Убедитесь, что все контейнеры (`otel-collector`, `tempo`, `loki`, `prometheus`, `grafana`) находятся в состоянии `Up`.
Интерфейс Grafana доступен по адресу: **http://localhost:3000**

## 3. Установка зависимостей и запуск сервисов

Вернитесь в корень `lab5/` и создайте виртуальное окружение.

### Linux / macOS (Bash)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Запуск обоих приложений (в двух отдельных терминалах из корня `lab5/`)

**Терминал 1 — `dispatch-service` (порт 8002):**
```bash
uvicorn dispatch_service.main:app --host 0.0.0.0 --port 8002
```

**Терминал 2 — `order-service` (порт 8001):**
```bash
uvicorn order_service.main:app --host 0.0.0.0 --port 8001
```

Проверка готовности:
- http://localhost:8001/health
- http://localhost:8002/health

---

## 4. Проверка 4 обязательных сценариев

### Сценарий 1 — Успешный заказ и назначение водителя (HTTP 201)

**Bash:**
```bash
curl -i -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "passenger_id": "pax-101",
    "pickup_zone": "center",
    "tariff": "comfort"
  }'
```

**PowerShell:**
```powershell
curl.exe -i -X POST http://localhost:8001/api/v1/orders `
  -H "Content-Type: application/json" `
  -d '{\"passenger_id\":\"pax-101\",\"pickup_zone\":\"center\",\"tariff\":\"comfort\"}'
```

### Сценарий 2 — Медленная операция в `dispatch-service` (задержка 1.5–2.5 сек)

**Bash:**
```bash
curl -i -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "passenger_id": "pax-202",
    "pickup_zone": "airport",
    "tariff": "business",
    "simulate_slow": true
  }'
```

**PowerShell:**
```powershell
curl.exe -i -X POST http://localhost:8001/api/v1/orders `
  -H "Content-Type: application/json" `
  -d '{\"passenger_id\":\"pax-202\",\"pickup_zone\":\"airport\",\"tariff\":\"business\",\"simulate_slow\":true}'
```

### Сценарий 3 — Ошибка внутри `dispatch-service` (`NoDriversAvailableError`, ответ 502 от `order-service`)

**Bash:**
```bash
curl -i -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "passenger_id": "pax-303",
    "pickup_zone": "north",
    "tariff": "economy",
    "simulate_error": true
  }'
```

**PowerShell:**
```powershell
curl.exe -i -X POST http://localhost:8001/api/v1/orders `
  -H "Content-Type: application/json" `
  -d '{\"passenger_id\":\"pax-303\",\"pickup_zone\":\"north\",\"tariff\":\"economy\",\"simulate_error\":true}'
```

### Сценарий 4 — Остановленный `dispatch-service` (HTTP 503 + Client-спан с ошибкой)

1. Перейдите в **Терминал 1** (`dispatch-service`) и остановите его нажатием `Ctrl + C`.
2. Выполните запрос к `order-service`:

**Bash:**
```bash
curl -i -X POST http://localhost:8001/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "passenger_id": "pax-404",
    "pickup_zone": "south",
    "tariff": "comfort"
  }'
```

**PowerShell:**
```powershell
curl.exe -i -X POST http://localhost:8001/api/v1/orders `
  -H "Content-Type: application/json" `
  -d '{\"passenger_id\":\"pax-404\",\"pickup_zone\":\"south\",\"tariff\":\"comfort\"}'
```

Ожидаемый результат: `HTTP/1.1 503 Service Unavailable`. После снятия скриншота перезапустите `dispatch-service`.

*(Рекомендация: выполните сценарии 1–3 по 3–5 раз с разными тарифами, чтобы на графиках метрик в Prometheus появились выразительные точки.)*

---

## 5. Анализ телеметрии в Grafana (http://localhost:3000)

### A. Просмотр и анализ трейсов в Tempo

1. Откройте **Explore** (иконка компаса слева) -> выберите источник данных **Tempo**.
2. Вкладка **Search**: выберите `Service Name = order-service` и нажмите **Run query** (или переключитесь на **TraceQL** и выполните `{ resource.service.name = "order-service" }`).
3. Проверьте все 4 сценария, открывая соответствующие трейсы:
   - **Успешный трейс:** видны спаны обоих сервисов в одном дереве:
     `POST /api/v1/orders` (Server) -> `calculate_trip_estimate` (Internal) + `POST` (Client) -> `POST /api/v1/dispatch` (Server) -> `search_available_drivers` (Internal) -> `assign_best_driver` (Internal). Раскройте каждый бизнес-спан и проверьте секцию **Span Attributes**.
   - **Медленный трейс:** на временной диаграмме (Waterfall) основную ширину занимает спан `search_available_drivers` (1.5–2.5 с), внутри него присутствует событие `slow_driver_search_simulated` (секция **Events**).
   - **Ошибка в `dispatch-service`:** спан `search_available_drivers` отмечен красным (`Status: Error`), в разделе **Events** записано исключение `NoDriversAvailableError` со стектрейсом.
   - **Недоступный `dispatch-service`:** в трейсе присутствуют только спаны `order-service`, при этом спан `POST` вида `Client` помечен красным (`Status: Error`) и содержит событие исключения `ConnectError`.

### B. Поиск связанных логов по `TraceId` в Loki

**Способ 1 (быстрый переход из Tempo):**
1. Откройте любой трейс в Tempo и скопируйте его `Trace ID` в шапке справа (либо нажмите кнопку **Logs for this span** на спане, если включён Trace-to-Logs).

**Способ 2 (прямой запрос LogQL в Loki):**
1. Перейдите в **Explore** -> выберите источник данных **Loki**.
2. Введите один из запросов (подставьте реальный `TraceId` без угловых скобок):

```logql
{service_name=~"order-service|dispatch-service"} | trace_id="ВАШ_TRACE_ID"
```

Если в конфигурации коллектора OTLP-логи приходят в JSON или с меткой `job`/`exporter`, используйте универсальный поиск по `TraceId`:

```logql
{service_name=~".+"} |= "ВАШ_TRACE_ID"
```

Либо просмотрите все структурированные логи обоих сервисов:

```logql
{service_name=~"order-service|dispatch-service"}
```

Раскройте любую запись лога — в секции **Detected fields / Structured metadata** отобразятся `trace_id`, `span_id`, `order.id`, `taxi.tariff`, `pickup.zone` и остальные поля из `extra={...}`.

### C. Построение графиков кастомных метрик в Prometheus

1. Перейдите в **Explore** -> выберите источник данных **Prometheus** (подождите 10–15 секунд после отправки запросов, чтобы сработал цикл экспорта и scrape).
2. Начните вводить `taxi_orders` или `driver_dispatch` в поле **Metrics browser**, чтобы увидеть точное имя с учётом суффиксов OTel Collector.
3. Примеры **PromQL-запросов**:

- **Общее число заказов по тарифам и статусам (`Counter`):**
  ```promql
  sum by (tariff, status) (taxi_orders_total)
  ```
- **Скорость поступления заказов в секунду за последние 5 минут:**
  ```promql
  sum by (status) (rate(taxi_orders_total[5m]))
  ```
- **95-й перцентиль времени подбора водителя в мс (`Histogram`):**
  ```promql
  histogram_quantile(0.95, sum by (le, tariff) (rate(driver_dispatch_duration_ms_milliseconds_bucket[5m])))
  ```
  *(Примечание: в зависимости от настройки `add_metric_suffixes` в вашем `otel-collector`, имя бакетов может быть `driver_dispatch_duration_ms_bucket` без `_milliseconds` — проверьте автодополнением в Grafana.)*
- **Среднее время подбора водителя (мс):**
  ```promql
  sum(rate(driver_dispatch_duration_ms_milliseconds_sum[5m])) / sum(rate(driver_dispatch_duration_ms_milliseconds_count[5m]))
  ```

