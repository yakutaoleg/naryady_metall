"""Единый диалог ввода: состав обработчиков и правила переходов (без Telegram)."""
import re
from telegram.ext import ConversationHandler
from src import bot


def _conv():
    return bot.build_input_conversation(
        bot.on_callback, bot.receive_partial_qty, bot.receive_block_comment, bot.receive_correction_qty,
        bot.receive_emp_name, bot.receive_approve_name, bot.cmd_cancel, bot._start_and_reset, bot._nav_and_reset)


def test_conversation_shape():
    c = _conv()
    assert isinstance(c, ConversationHandler)
    assert len(c.entry_points) == 5 and len(c.states) == 5
    assert c.allow_reentry and c.conversation_timeout == 600


def test_nav_reset_pattern():
    ex = bot.NAV_EXCLUDE_RE
    for keep in ('project:add', 'earnings_pick', 'wiz_cut_drawings'):
        assert not re.match(ex, keep), keep           # входы других диалогов не сбрасываем
    for reset in ('tasks', 'menu', 'projects', 'projects:archive', 'pconf:yes:1:2'):
        assert re.match(ex, reset), reset


def test_clear_pending_input():
    class Ctx:
        user_data = {'partial_task_id': 1, 'block_task_id': 2, 'keep': 3}
    bot._clear_pending_input(Ctx)
    assert Ctx.user_data == {'keep': 3}


def test_projects_keyboards_split_active_and_archive():
    act = [{'id': 1, 'project_name': 'B', 'status': 'АКТИВНЫЙ'}]
    kb = bot.projects_list_kb(act, archived_count=2).inline_keyboard
    assert any('Архив (2)' in b.text for r in kb for b in r)
    arch = [{'id': 2, 'project_name': 'А', 'status': 'АРХИВ'}]
    kb2 = bot.projects_list_kb(arch, archive=True).inline_keyboard
    assert not any('Добавить' in b.text for r in kb2 for b in r)
