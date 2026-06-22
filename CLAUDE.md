# Claude Code Workflow Instructions

This file contains instructions for Claude Code when working on this project.

---

## Workflow Rules

1. **ALWAYS start by asking clarifying questions** if the request could be interpreted as either business-level or technical-level work.

2. **ALWAYS use TodoWrite to create a numbered action plan** before making ANY file changes. Maximum 2-3 items per phase.

3. **NEVER proceed past the plan phase without explicit user confirmation** — say 'Plan ready. Approve to proceed?' and wait.

4. **Default to the simplest solution** — no architectural changes, no new dependencies, no refactors unless specifically requested.

5. **Surgical Changes** — touch only what was asked. Do not improve, reformat, or refactor adjacent code. If unrelated dead code is noticed — mention it, don't delete it. Every changed line must trace directly to the user's request.

6. **Plan format with verification** — for multi-step tasks, state the plan as:
   ```
   1. [Step] → verify: [what to check]
   2. [Step] → verify: [what to check]
   ```
   Do not proceed to next step until current step is verified.

7. **AI Council — Вариант A (сложные решения):** Когда задача требует выбора между подходами или есть неочевидные риски — явно пройти через три угла зрения перед выводом:
   - 🔴 Скептик: что может пойти не так?
   - 🟡 Прагматик: что реально сделать прямо сейчас?
   - 🟢 Архитектор: как это впишется в долгосрочную картину?

8. **AI Council — Вариант B (ресёрч и рассуждения):** Если пользователь явно просит "сделай ресёрч", "порассуждай как лучше", "сравни варианты" — запускать независимые субагенты с разными задачами (исследование, критика, синтез), затем сводить результаты в итоговый вывод.

9. **If a tool/approach fails twice, STOP and present alternatives** instead of retrying the same approach.

6. **For documentation tasks**: Ask upfront — business audience or technical audience? What format (MD, DOCX, Notion)? No conversion attempts without confirming available tools first.

---

## Project Context

- **Primary Projects**: Nedvex Bot (n8n workflows), Nedvex Portal, Personal Assistant, Design Site
- **Stack**: n8n workflows, PostgreSQL, MySQL, Telegram bots, React, JavaScript, Python
- **Environment**: Windows 11, Git repository

## N8N Workflows

- Always verify API connectivity before attempting bulk changes
- If SSH or remote server operations fail, immediately switch to providing manual step-by-step instructions instead of retrying automated approaches
- Do not attempt more than 2 automated retries before switching

## Writing & Documentation

- Default to business-level language unless explicitly asked for technical details
- Never include API parameters, database schemas, or code snippets in business documents

## Git & Commits

Before committing:
1. Save/update documentation first
2. Delete temp/scratch files
3. Run a final check
4. Then commit

Always follow this order unless told otherwise.

## Environment Constraints

- Running on Windows 11
- Do not attempt to install system-level dependencies (like wkhtmltopdf, psql, etc.) without asking
- For file format conversions (MD→PDF, MD→DOCX), ask what tools are available before trying
