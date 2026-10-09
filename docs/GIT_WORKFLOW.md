# Git — правила работы (Наряды, Цех Металла)

Принято 10.10.2026. Заменяет прежнюю схему test → prod с PR: тестового стенда больше нет.

## Источник истины

- Код бота живёт в репозитории на VPS: `/root/naryady/prod`, ветка **`develop`**. Это то, что работает в проде.
- Remote `origin` = `github.com/yakutaoleg/naryady_metall`. Ветка `main` там — **замороженный архив** старой истории (у неё нет общего предка с `develop`). В `main` не пишем, PR в неё не открываем.
- Папка `Automatization/Metall/src` на Windows — архивная копия. Не править. Изменения делаются только на VPS патчами через scp.
- Репозиторий `C:\Users\MSI\Projects` на Windows не связан с GitHub-репозиторием бота (remote `metall` удалён).

## Цикл изменения

1. Сделать патч (scp + python-скрипт), перед правкой сохранить бэкап файла.
2. `cd /root/naryady/prod && /root/naryady/test/venv/bin/python -m pytest tests -q` — все тесты зелёные.
3. Перезапуск сервиса, проверка: `systemctl is-active naryady-bot-prod` = `active`, в логах нет ошибок.
4. Коммит и `git push origin develop` **в тот же день**. Рабочие изменения без коммита дольше суток не держим.
5. После выкатывания, меняющего поведение бота или синка, поставить тег `prod-ГГГГ-ММ-ДД` и запушить его: `git tag prod-2026-10-10 && git push origin prod-2026-10-10`.

## Коммиты

- Формат: `feat | fix | test | docs | chore | refactor: описание`, по-русски. Один коммит — одно логическое изменение.
- Документацию (`docs/project_status.md`, `CHANGELOG.md`) обновляем в том же коммите.
- **Не коммитим:** секреты (`*token*.json`, `service_account.json`, `.env`), `state/`, `logs/`, `backup/`, `*.bak*`, `project_rules.json`.

## Откат

Через git, а не через файлы `.bak`:

```bash
git log --oneline -10
git checkout <тег-или-коммит> -- src/файл.py
systemctl restart naryady-bot-prod
```

или `git revert <коммит>`. Старые файлы `.bak*` в `src/` не копим: после коммита бэкап не нужен.

## Токены GitHub

- Fine-grained токен, только на репозиторий `naryady_metall`, права: Contents и Pull requests — Read and write.
- Срок 90 дней. Хранится только в `mcp.json`, не в чате и не в git.
- Продлить за 7 дней до окончания срока.

## Пульс и проверки (для справки)

- Ночная проверка данных: 07:30, `src/consistency_check.py`.
- Пульс: каждые 10 минут, `src/heartbeat.py`.
- Unit-файлы лежат в `deploy/systemd/`; после изменения в `/etc/systemd/system/` копируем их сюда.
