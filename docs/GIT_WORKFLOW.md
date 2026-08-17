# Git Workflow — Наряды Цех Металла

## Ветки

| Ветка | Окружение | БД | Бот |
|---|---|---|---|
| develop | /root/naryady/test/ | naryady_test | Тест-бот |
| main | /root/naryady/prod/ | naryady | Прод-бот |

Прямые коммиты в main — запрещены.
Всё идёт через develop → PR → main.

---

## Цикл разработки

1. Claude пишет код в develop на VPS
2. Запускает pytest сам через SSH  
3. Если тесты зелёные → коммит → push в develop
4. Олег смотрит результат в тест-боте
5. Approve → PR develop → main → деплой в prod

---

## Формат коммитов

feat: добавить команду /план в бот
fix: исправить парсинг даты из Sheets
test: добавить тест синка для пустых строк
chore: обновить requirements.txt
docs: обновить GIT_WORKFLOW.md
refactor: вынести логику логгера в отдельный класс

---

## Деплой в prod (после approve Олега)

cd /root/naryady/prod
git pull origin main
pip install -r requirements.txt -q
systemctl restart naryady-bot
systemctl restart naryady-sync

---

## Откат в случае проблем

# Посмотреть историю
git log --oneline -10

# Откатить prod к предыдущему коммиту
git checkout <commit-hash>
systemctl restart naryady-bot

---

## Правила

- Файлы .env никогда не коммитятся (есть в .gitignore)
- Файл service_account.json никогда не коммитится
- Каждая фича — отдельный коммит с понятным сообщением
- Перед PR — все тесты должны быть зелёными
- Версии фиксируются в CHANGELOG.md
