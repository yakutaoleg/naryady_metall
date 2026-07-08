# Deploy — Naryady Metall Bot

## Структура

| Место | Что | Ветка |
|-------|-----|-------|
| Локально (`C:/Users/MSI/Projects`) | `Automatization/Metall/bot_remote.py` — локальная копия для разработки | `main` |
| GitHub | `yakutaoleg/naryady_metall` — удалённый репозиторий | `main` |
| VPS (`2.56.212.56`) | `/root/naryady/test/src/bot.py` — реально работающий файл | `develop` |

**Важно:** VPS запускает `src/bot.py`, а не `bot_remote.py`. Это разные файлы.
Сервис: `naryady-bot-test` (systemd).

---

## Процесс деплоя

### Стандартный деплой (изменения в `bot_remote.py`)

1. **Закоммить локально:**
   ```bash
   cd C:/Users/MSI/Projects
   git add Automatization/Metall/bot_remote.py
   git commit -m "feat: ..."
   git push metall main
   ```

2. **Применить изменения на VPS через VPS SSH Executor (n8n workflow `qljQU2Ptd8OxvCcw`):**
   - Патч-скриптом: написать Python-скрипт который вносит нужные изменения в `src/bot.py`
   - Или вручную через SSH

3. **Перезапустить бот на VPS:**
   ```bash
   systemctl restart naryady-bot-test
   systemctl is-active naryady-bot-test
   journalctl -u naryady-bot-test -n 20 --no-pager
   ```

### Откат (если что-то сломалось)

```bash
# Бэкап создаётся перед каждым деплоем автоматически
ls /root/naryady/test/src/bot.py.bak*
cp /root/naryady/test/src/bot.py.bak.YYYYMMDD_HHMMSS /root/naryady/test/src/bot.py
systemctl restart naryady-bot-test
```

---

## Прямой SSH доступ

```bash
ssh root@2.56.212.56
```

SSH ключ уже настроен (`~/.ssh/id_ed25519`), пароль не нужен.
Claude использует Bash tool напрямую: `ssh root@2.56.212.56 "команда"`.

> ~~n8n VPS SSH Executor~~ (`qljQU2Ptd8OxvCcw`) — устаревший костыль, больше не использовать.

---

## Полезные команды для VPS

```bash
# Статус бота
systemctl is-active naryady-bot-test

# Логи (последние 30 строк)
journalctl -u naryady-bot-test -n 30 --no-pager

# Перезапуск
systemctl restart naryady-bot-test

# Бэкап перед правкой
cp /root/naryady/test/src/bot.py /root/naryady/test/src/bot.py.bak.$(date +%Y%m%d_%H%M%S)

# Путь к файлу бота
/root/naryady/test/src/bot.py

# Путь к venv
/root/naryady/test/venv/bin/python

# Синхронизация данных (запуск вручную)
cd /root/naryady/test && venv/bin/python src/sync.py
```

---

## Ситуация с git (июль 2026)

VPS ветка `develop` опережает GitHub `main` на ~10 коммитов (правки были сделаны
прямо на VPS). GitHub `main` опережает VPS на ~10 коммитов (правки через Claude локально).

**Пока деплой работает так:** изменения применяются Python-патчем напрямую на VPS,
затем коммитятся в VPS ветку `develop`.

**TODO:** при удобном случае слить ветки — вытащить VPS commits на GitHub и сделать
единый `main`, чтобы деплой стал простым `git pull`.
