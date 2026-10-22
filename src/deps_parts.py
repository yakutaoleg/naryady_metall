"""Зависимости с количеством деталей на сборку.

Лист ЗАВИСИМОСТИ: строки с заполненными F «ДЕТАЛЬ» и G «КОЛ-ВО НА 1 СБОРКУ» задают точную потребность
(например: СБОРКА | Б4в | ПЛАЗМА | Б4в | кп1 | 2). Для таких сборок блокировка считается по фактическому
расходу деталей, для остальных работает прежняя пропорциональная логика (sync._compute_block, bot._deps_*).

Расчёт для сборки A (лист-ожидатель S) и детали P (лист-источник R, нужно q штук на сборку):
    свободно(P)  = выполнено(P в R) − Σ q_B(P) × выполнено(сборка B в S)      (расход уже готовых сборок, включая A)
    можно ещё    = min по всем деталям A:  свободно(P) // q_A(P)
    заблокировано, если «можно ещё» < количество в строке.
"""
import re
import time

from src import db

PART_COLS = ('ДЕТАЛЬ', 'КОЛ-ВО НА 1 СБОРКУ')      # заголовки колонок F и G листа ЗАВИСИМОСТИ
SHEET_ORDER = ['ПИЛА', 'ПЛАЗМА', 'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА']
_FOOTER = ('итог', 'добавление массы', 'общая масса', 'общий вес')
_RE_COUNT = re.compile(r'(?i)количество\s*=\s*(\d+(?:[.,]\d+)?)')
_RE_POZ = re.compile(r'(?i)поз[.\s]*=\s*(\S+)')


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(',', '.').replace('\xa0', '').strip())
    except ValueError:
        return None


# ───────────────────────── разбор исходного файла сборок ─────────────────────────

def parse_assembly_components(rows):
    """rows — строки листа (кортежи значений). Блок сборки начинается строкой «Поз.=Б3 Количество=2 …»
    или «ИМЯ Количество=N», дальше идут детали: позиция в первой колонке, количество на ОДНУ сборку — в колонке «Кол-во».
    → (asm_qty {сборка: сколько штук}, need_total {сборка: {деталь: штук на все такие сборки}})."""
    qty_col = None
    for r in rows[:6]:
        for i, c in enumerate(r):
            if c is not None and str(c).strip().lower().startswith('кол'):
                qty_col = i
                break
        if qty_col is not None:
            break
    if qty_col is None:
        qty_col = 2

    asm_qty, need_total = {}, {}
    cur, cur_qty = None, 0.0
    for r in rows:
        c0 = str(r[0]).strip() if r and r[0] is not None else ''
        if not c0:
            continue
        low = c0.lower()
        m_cnt = _RE_COUNT.search(c0)
        if low.startswith('поз.=') or low.startswith('поз =') or (m_cnt and not low.startswith(_FOOTER)):
            m_name = _RE_POZ.match(c0)
            if m_name:
                name = m_name.group(1).strip()
            else:
                name = re.match(r'^(.*?)\s+[Кк]оличество\s*=', c0)
                name = (name.group(1) if name else c0).rstrip('. ').strip()
            cur = name
            cur_qty = _num(m_cnt.group(1)) if m_cnt else 1.0
            asm_qty[cur] = asm_qty.get(cur, 0.0) + cur_qty
            need_total.setdefault(cur, {})
            continue
        if low.startswith(_FOOTER) or cur is None:
            continue
        q = _num(r[qty_col]) if qty_col < len(r) else None
        if q is None or q <= 0:
            continue
        d = need_total[cur]
        d[c0] = d.get(c0, 0.0) + q * cur_qty
    return asm_qty, need_total


def per_assembly(asm_qty, need_total):
    """→ {сборка: {деталь: штук на 1 сборку}}."""
    out = {}
    for a, parts in need_total.items():
        n = asm_qty.get(a) or 1.0
        out[a] = {p: round(v / n, 6) for p, v in parts.items()}
    return out


# ───────────────────────── расчёт доступности ─────────────────────────

class Ctx:
    """Данные проекта для расчёта: потребности, готовые сборки, готовые детали."""
    def __init__(self, parts_rows, done_asm, stats):
        self.parts_rows = parts_rows      # [(element, requires_sheet, part_lower, qty_per)]
        self.done_asm = done_asm          # {element: выполнено штук в листе-ожидателе}
        self.stats = stats                # {(requires_sheet, part_lower): (всего, выполнено)}
        self.by_element = {}
        self.consumers = {}               # (requires_sheet, part_lower) -> [(element, q)]
        for e, rs, pl, q in parts_rows:
            self.by_element.setdefault(e, []).append((rs, pl, q))
            self.consumers.setdefault((rs, pl), []).append((e, q))


def evaluate(ctx, element, own_qty):
    """Чистая функция. → None, если для элемента нет строк с количеством, иначе словарь:
    {'avail': сколько ещё сборок можно сделать, 'blocked': bool, 'limiting': [(лист, деталь, свободно, нужно)]}."""
    reqs = ctx.by_element.get(element)
    if not reqs:
        return None
    avail, limiting, known, covered = None, [], False, set()
    for rs, pl, q in reqs:
        tot, done = ctx.stats.get((rs, pl), (0.0, 0.0))
        if not tot:
            continue                      # детали нет в листе-источнике: не блокирует (ловит ночная проверка)
        known = True
        covered.add(rs)
        used = sum(qb * ctx.done_asm.get(eb, 0.0) for eb, qb in ctx.consumers.get((rs, pl), []))
        free = max(0.0, done - used)
        can = int(free // q + 1e-9)
        if avail is None or can < avail:
            avail = can
        limiting.append((rs, pl, free, q, can))
    if not known:
        return None
    need = own_qty if own_qty and own_qty > 0 else 1
    blocked = avail < need
    lim = [(rs, pl, free, q) for rs, pl, free, q, can in limiting if can < need]
    return {'avail': avail, 'blocked': blocked, 'limiting': lim, 'need': need, 'covered': covered}


def format_block(res, own_qty=None):
    """Текст для колонки БЛОК, как в sync._compute_block."""
    if res is None:
        return None
    if not res['blocked']:
        return '✅'
    sheets_ = []
    for rs, *_ in res['limiting']:
        if rs not in sheets_:
            sheets_.append(rs)
    sheets_.sort(key=lambda s: SHEET_ORDER.index(s) if s in SHEET_ORDER else 99)
    tail = '\n'.join(f'⛔ {s}' for s in sheets_)
    n = res['avail']
    total = int(own_qty) if own_qty else res['need']
    if n > 0:
        return f'✅ {n} из {total}\n{tail}'
    return tail


# ───────────────────────── доступ к БД ─────────────────────────

_cache = {}
_TTL = 5.0


def has_parts(project_name, waiting_sheet, element):
    ctx = load_ctx(project_name, waiting_sheet)
    return bool(ctx and element in ctx.by_element)


def load_ctx(project_name, waiting_sheet, fresh=False):
    key = (project_name, waiting_sheet)
    hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < _TTL:
        return hit[1]
    rows = db.fetchall(
        "SELECT element, requires_sheet, part, qty_per FROM element_dependencies "
        "WHERE project_name=%s AND waiting_sheet=%s AND part IS NOT NULL AND qty_per > 0",
        [project_name, waiting_sheet])
    if not rows:
        _cache[key] = (time.time(), None)
        return None
    parts_rows = [(r['element'], r['requires_sheet'], str(r['part']).strip().lower(), float(r['qty_per'])) for r in rows]
    done_asm = {r['element']: float(r['d'] or 0) for r in db.fetchall(
        "SELECT element, SUM(quantity) AS d FROM work_orders "
        "WHERE project_name=%s AND sheet_name=%s AND status='ВЫПОЛНЕНО' AND row_num > 0 GROUP BY element",
        [project_name, waiting_sheet])}
    stats = {}
    for rs in {r[1] for r in parts_rows}:
        for r in db.fetchall(
                "SELECT lower(trim(position)) AS p, SUM(quantity) AS t, "
                "SUM(quantity) FILTER (WHERE status='ВЫПОЛНЕНО') AS d FROM work_orders "
                "WHERE project_name=%s AND sheet_name=%s AND position IS NOT NULL GROUP BY 1",
                [project_name, rs]):
            stats[(rs, r['p'])] = (float(r['t'] or 0), float(r['d'] or 0))
    ctx = Ctx(parts_rows, done_asm, stats)
    _cache[key] = (time.time(), ctx)
    return ctx


def compute(project_name, waiting_sheet, element, own_qty=None):
    """→ результат evaluate или None (для сборки нет данных о количествах: работает прежняя логика)."""
    ctx = load_ctx(project_name, waiting_sheet)
    if not ctx:
        return None
    return evaluate(ctx, element, own_qty)


def covered_sheets(project_name, waiting_sheet, element):
    """Листы-источники, для которых блокировка считается по количеству деталей (для остальных работает прежняя логика)."""
    res = compute(project_name, waiting_sheet, element)
    return set(res['covered']) if res else set()


def blocking_lines(res):
    return [f'{rs}: {pl} (свободно {free:g}, нужно {q:g} на сборку)' for rs, pl, free, q in res['limiting']]
