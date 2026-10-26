# Контракты и API

Канонические схемы — `src/assr/models.py`. Сгенерировать их: `uv run assr schemas`. Не редактировать JSON описание отдельно от моделей.

| Объект | Назначение |
|---|---|
| ProjectSpec | Цель, mode, профиль, seeds, delta, baseline tolerance, бюджет |
| ExperimentSpec | Гипотеза, фиксируемые условия, treatment, revision |
| CodeProposal | Полное содержимое только изменяемых allowlisted файлов |
| ReviewDecision | accept/revise/escalate, target_digest, проверки, evidence refs |
| RunSpec | Код, commit, hashes, точная команда argv, config, seed, walltime, ключ |
| ResearchConclusion | Вердикт, claim, experiment_refs, ограничения, следующий шаг |
| Envelope | UUID сообщения, sender/recipient, correlation/causation, revision, payload |

`extra=forbid`, NaN/Infinity запрещены. Полный код хранится в RunSpec, чтобы восстановление не зависело от изменяемой ветки. API никогда не принимает от браузера произвольную команду runner: команды задаёт администратор профиля.

## HTTP

Все данные защищены `Authorization: Bearer <ASSR_TOKEN>`, если токен задан. Health и статические ресурсы общедоступны. Для LIVE токен обязателен. Swagger: `/docs`.

| Метод | Путь | Смысл |
|---|---|---|
| POST | /campaigns | Создать из ProjectSpec |
| GET | /campaigns | Список кампаний |
| GET | /campaigns/{id} | Полный snapshot |
| GET | /campaigns/{id}/events | Последовательный журнал |
| POST | /campaigns/{id}/decisions | Решение человека с revision |
| POST | /campaigns/{id}/pause или resume или cancel | То же решение с соответствующим action |
| GET | /experiments/{id} | Спецификация, включая прошлые итерации |
| GET | /runs/{id} | RunSpec, результат и MLflow ID |
| GET | /campaigns/{id}/report | Markdown отчёт |

Изменяющие запросы требуют `Idempotency-Key` длиной 8–100 символов. Повтор с тем же payload возвращает исходный ответ; другой payload даёт 409. Устаревший revision тоже даёт 409. Некорректная схема — 422; отсутствие объекта — 404; ошибка токена — 401.

```json
{"revision": 4, "action": "approve", "comment": "Принимаю план и бюджет"}
```

Повторное согласование каждого запуска не требуется. request_changes возможно в AWAITING_APPROVAL либо после pause без активной задачи, требует комментария и расходует лимит исследовательских итераций. Увеличение общего бюджета требует новой кампании — агентам такой операции не предоставлено.

## Runner RPC

`ssh -o BatchMode=yes -o StrictHostKeyChecking=yes HOST 'python3 -m assr.runner_rpc'`

На stdin передаётся JSON `{op, profile, spec}`, op равен submit/status/cancel. Ответ содержит status, job_id, spec_digest, исходный spec и при завершении exit_code, logs, metrics, artifact_manifest. Profile выбирается только из локального каталога оператора, не из пути пользователя. SSH не получает команды, созданные LLM.
