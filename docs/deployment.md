# Развёртывание и эксплуатация

## Локальная разработка

Python 3.12+, Git, uv. `uv sync --frozen --extra dev`, затем `uv run assr demo`. SQLite создаётся в `.assr/assr.db`, профили — в `.assr/profiles`. CLI и API должны указывать на один ASSR_ROOT. `.assr` и `.env` никогда не коммитятся.

API по умолчанию слушает localhost. Для удалённого доступа задайте ASSR_TOKEN и используйте TLS reverse proxy. Токен в UI вводится в поле и хранится только в памяти страницы. Открывать порт без TLS в недоверенную сеть нельзя.

## PostgreSQL и MLflow

```bash
cp .env.example .env
# Задайте разные случайные ASSR_DB_PASSWORD и ASSR_TOKEN.
# Пароль БД должен быть URL-safe, либо закодирован в ASSR_DATABASE_URL.
docker compose build
docker compose up -d postgres mlflow
docker compose run --rm api assr init-demo
docker compose up -d api
```

UI: localhost:8000, MLflow: localhost:5000. Docker socket не монтируется в API. Для SSH контейнеру API отдельно смонтируйте ключ и known_hosts только для service account, read-only, с владельцем UID 10001. Секреты не передаются в training контейнер. Если проще, запускайте API в venv на доверенном control host, а PostgreSQL/MLflow — в Compose.

Compose здесь — одноузловая конфигурация. Не увеличивайте replicas/uvicorn workers: текущая эксплуатационная модель рассчитана на один worker. Таблицы создаются при первом старте; последующие несовместимые изменения схемы требуют явной миграции и backup.

## LLM

Задайте ASSR_LLM_URL (HTTPS URL совместимого API, заканчивающийся `/v1`), ASSR_LLM_KEY, ASSR_LLM_MODEL и положительные цены ASSR_INPUT_USD_PER_MILLION / ASSR_OUTPUT_USD_PER_MILLION. Провайдер должен поддерживать JSON object responses и возвращать token usage. Ключи выбираются оператором, не исследователем.

Запросы ограничены 60 секундами. До вызовов резервируется консервативный бюджет. Уточните цены и кредитный предел в аккаунте провайдера; локальная оценка не контролирует сторонние расходы вне ASSR. Файлы agent-cache содержат результаты и usage, но не Authorization header.

## GPU сервер

1. Создайте отдельного непривилегированного пользователя. Установите Python 3.12+, проект ASSR, rootless Docker и совместимый NVIDIA Container Toolkit. Проверьте ручной GPU запуск.
2. В образе обучения должны быть `python`, `timeout`, необходимые библиотеки и CUDA runtime. Образ evaluator должен содержать установленный ASSR и `timeout`. Получите immutable image digests. У образов не должно быть переопределяющего ENTRYPOINT.
3. Подготовьте `.json` данные: `train.json` с `{id,x,label}`, `validation.json` с `{id,x}`; labels отдельно с `{id,label}`. Для текущего evaluator — бинарные labels. Нельзя положить labels внутрь data_dir. Задайте уникальные IDs без пересечения splits.
4. Настройте **реальную квоту файловой системы** на каталог jobs. Bind mount сам по себе не ограничивает диск; без `output_quota_verified=true` runner отказывается работать. Эта настройка — подтверждение оператора, не автоматическая проверка quota.
5. Создайте профиль из закреплённого Git commit. Для примера используйте `examples/pytorch/train.py` в отдельном исследовательском репозитории как `train.py`.

```bash
python -m assr.profile \
  --repo /srv/research/project --ref FULL_40_CHARACTER_COMMIT \
  --file train.py --data /srv/research/data --labels /srv/research/labels.json \
  --image 'registry/train@sha256:ACTUAL_DIGEST' \
  --evaluator-image 'registry/evaluator@sha256:ACTUAL_DIGEST' \
  --out ~/.assr-runner/profiles/toy.json
```

Замените placeholders реальными значениями. По умолчанию smoke использует ту же программу с `config.phase=smoke`; PyTorch пример ограничивает его двумя шагами и проверяет формы, loss и gradients. Для другого проекта определите собственный smoke argv в профиле. Команды должны писать `/output/predictions.json`, в том числе после smoke.

6. Проверьте max_walltime, max_gpus, memory, cpus; подтвердите quota только после её настройки. Скопируйте тот же профиль в `ASSR_ROOT/profiles` control host. Пути data_dir и labels_path внутри профиля относятся к runner host. Передайте исходники профиля только если их разрешено отправлять выбранному LLM.
7. Закрепите SSH host key в known_hosts. Задайте ASSR_SSH_HOST, токен, LLM настройки и ASSR_MLFLOW_URL. Создайте LIVE кампанию с измеренным baseline_expected, tolerance и достаточными лимитами.
8. Пройдите ручные пункты в acceptance.md до использования результатов в исследованиях.

Не устанавливайте restart=always для training контейнеров. `timeout` внутри контейнера ограничивает длительность независимо от control host. Worker не должен работать под root; тренировочный код не получает root, socket Docker, labels или ключи.

## Сбой и отмена

- Перезапуск API: snapshot восстанавливается; незавершённый lease истекает через 300 секунд. Не удаляйте record.json и контейнеры до reconciliation.
- Ошибка SSH: остаётся RUNNING либо CANCEL_PENDING; сначала восстановите доступ и запросите статус.
- PAUSED: причина отображается в UI. Исправьте инфраструктуру и используйте resume. Для изменения гипотезы — request_changes без активных задач.
- Лимит бюджета: создайте новую кампанию с новым планом. Скрытого повышения лимита нет.
- Отмена доступного job: runner останавливает training и evaluator. При недоступности хоста действует walltime; API ждёт подтверждения.

## Хранение и резервное копирование

Сохраняйте PostgreSQL/SQLite, ASSR_ROOT (profiles, agent-cache, workspaces, demo-jobs), каталоги runner и MLflow. Backup БД и артефактов должен быть согласован по времени. Не удаляйте данные незавершённых кампаний. После завершения оператор может архивировать каталог и удалить остановленные контейнеры; автоматическая garbage collection в MVP не включена.

GPU затраты в интерфейсе — зарезервированный потолок, не измеренный биллинг. Артефакты находятся на runner по artifacts_uri; MLflow хранит ссылку, а не автоматически загруженную копию. Для физического доступа к ним используется разрешённый SSH/SFTP доступ оператора.
