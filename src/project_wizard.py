"""
project_wizard.py — Создание нового проекта через Telegram-диалог.

Шаги:
  1. scan_drive_folder()        — сканирует папку Drive, находит Excel-файлы
  2. recognize_excel()          — определяет специализацию каждого файла
  3. create_spreadsheet()       — создаёт Google Sheet в той же папке
  4. setup_sheet_tabs()         — создаёт вкладки с заголовками и форматированием
  5. load_excel_data()          — загружает данные из Excel в соответствующие вкладки
  6. register_project()         — сохраняет проект в БД с цепочкой зависимостей
"""

import io
import json
import logging
import re
import time
import uuid
from typing import Optional
import functools
from gspread.exceptions import APIError as _GspreadAPIError

def _w(fn, *args, max_retries=3, **kwargs):
    """Вызывает fn(*args, **kwargs), при 429 ждёт 65s и повторяет (до max_retries раз)."""
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except _GspreadAPIError as e:
            if '429' in str(e) and attempt < max_retries:
                wait = 65 + attempt * 30
                log.warning(f'Sheets 429 (попытка {attempt+1}/{max_retries}), жду {wait}s...')
                time.sleep(wait)
            else:
                raise



import openpyxl
from google.oauth2.service_account import Credentials as _SACredentials
from googleapiclient.discovery import build as _gdrive_build
from googleapiclient.http import MediaIoBaseDownload
import gspread
from gspread.utils import rowcol_to_a1

from src import config, db
from src.sheets import _gc, _api_call, STATUS_COLORS, SCOPES_RW

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Правила из файла
# ──────────────────────────────────────────────────────────────────────────────

_RULES_PATH = '/root/naryady/prod/project_rules.json'

def _rules() -> dict:
    with open(_RULES_PATH, encoding='utf-8') as f:
        return json.load(f)


# ──────────────────────────────────────────────────────────────────────────────
# Drive helpers
# ──────────────────────────────────────────────────────────────────────────────

_DRIVE_TOKEN_PATH = '/root/naryady/prod/drive_token.json'

def _drive_alert(msg: str):
    """Шлёт алерт администратору в Telegram при проблемах с Drive-токеном."""
    import requests as _req
    try:
        from src import config as _cfg
    except Exception:
        import config as _cfg
    ADMIN_CHAT_ID = 340620064
    text = f'⚠️ <b>Drive OAuth Alert</b>\n{msg}\n\nЗапусти /root/naryady/prod/reauth_drive.py для повторной авторизации.'
    try:
        _req.post(
            f'https://api.telegram.org/bot{_cfg.TG_TOKEN}/sendMessage',
            json={'chat_id': ADMIN_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
    except Exception as _e:
        import logging
        logging.getLogger(__name__).error('Drive alert send failed: %s', _e)


def _drive_oauth():
    """Drive API от имени пользователя (yakuta.yulia@gmail.com) — для создания файлов."""
    from google.oauth2.credentials import Credentials as _OAuthCreds
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    creds = _OAuthCreds.from_authorized_user_file(_DRIVE_TOKEN_PATH)
    if creds.expired:
        if not creds.refresh_token:
            _drive_alert('refresh_token отсутствует в drive_token.json — нужна повторная авторизация')
            raise RuntimeError('Drive: refresh_token отсутствует, требуется повторная авторизация')
        try:
            creds.refresh(Request())
            with open(_DRIVE_TOKEN_PATH, 'w') as f:
                f.write(creds.to_json())
        except RefreshError as e:
            _drive_alert(f'Ошибка обновления токена (<code>invalid_grant</code>):\n<code>{e}</code>')
            raise RuntimeError(f'Drive: токен истёк и не может быть обновлён ({e})') from e
    return _gdrive_build('drive', 'v3', credentials=creds)

def _drive_sa():
    """Drive API от service account — для чтения папок/файлов."""
    creds = _SACredentials.from_service_account_file(
        config.GOOGLE_SA_KEY,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return _gdrive_build('drive', 'v3', credentials=creds)


def scan_drive_folder(folder_id: str) -> dict:
    """
    Сканирует папку Drive.
    Возвращает:
      {
        'ok': True,
        'folder_name': str,
        'excel_files': [{'id': ..., 'name': ...}, ...],
        'existing_sheet': {'id': ..., 'name': ...} | None,
        'drawings_folder_id': str | None,
      }
    или {'ok': False, 'error': str}
    """
    drive = _drive_sa()

    # Метаданные самой папки
    try:
        meta = drive.files().get(
            fileId=folder_id, fields='name', supportsAllDrives=True
        ).execute()
        folder_name = meta.get('name', '')
    except Exception as e:
        return {'ok': False, 'error': f'Папка не найдена или нет доступа: {e}'}

    # Содержимое папки
    q = f"trashed=false and '{folder_id}' in parents"
    res = drive.files().list(
        q=q,
        fields='files(id,name,mimeType)',
        pageSize=50,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get('files', [])

    excel_files = []
    existing_sheet = None
    drawings_folder_id = None

    EXCEL_MIME = (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel',
    )
    SHEETS_MIME = 'application/vnd.google-apps.spreadsheet'

    def _is_excel_name(name):
        return name.lower().endswith(('.xlsx', '.xls'))

    def _scan_folder_files(fid, depth=0):
        """Рекурсивно сканирует папку и подпапки (до 3 уровней)."""
        nonlocal drawings_folder_id, existing_sheet
        try:
            res = drive.files().list(
                q=f"trashed=false and '{fid}' in parents",
                fields='files(id,name,mimeType)',
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
        except Exception as e:
            log.warning(f'scan_drive_folder: ошибка сканирования {fid}: {e}')
            return
        for f in res.get('files', []):
            mime = f['mimeType']
            name = f['name']
            if mime in EXCEL_MIME or (mime != SHEETS_MIME and _is_excel_name(name)):
                excel_files.append({'id': f['id'], 'name': name, 'mime': mime})
            elif mime == SHEETS_MIME:
                if _is_excel_name(name):
                    excel_files.append({'id': f['id'], 'name': name, 'mime': mime})
                elif depth == 0:
                    existing_sheet = {'id': f['id'], 'name': name}
                else:
                    # Google Sheets в подпапке — потенциальный источник данных (не готовый наряд)
                    excel_files.append({'id': f['id'], 'name': name, 'mime': mime})
            elif mime == 'application/vnd.google-apps.folder' and depth < 3:
                if 'черт' in name.lower() or 'drawing' in name.lower():
                    if drawings_folder_id is None:
                        drawings_folder_id = f['id']
                else:
                    _scan_folder_files(f['id'], depth + 1)

    for f in files:
        mime = f['mimeType']
        name = f['name']
        if mime in EXCEL_MIME or (mime != SHEETS_MIME and _is_excel_name(name)):
            excel_files.append({'id': f['id'], 'name': name, 'mime': mime})
        elif mime == SHEETS_MIME:
            if _is_excel_name(name):
                excel_files.append({'id': f['id'], 'name': name, 'mime': mime})
            else:
                # Если имя похоже на источник данных — добавляем как источник
                _data_kw = ('элемент', 'деталь', 'покраска', 'грунт', 'спецификац', 'ведомост', 'кмд')
                if any(kw in name.lower() for kw in _data_kw):
                    excel_files.append({'id': f['id'], 'name': name, 'mime': mime})
                else:
                    existing_sheet = {'id': f['id'], 'name': name}
        elif mime == 'application/vnd.google-apps.folder':
            if 'черт' in name.lower() or 'drawing' in name.lower():
                drawings_folder_id = f['id']
            else:
                _scan_folder_files(f['id'], depth=1)

    return {
        'ok': True,
        'folder_name': folder_name,
        'excel_files': excel_files,
        'existing_sheet': existing_sheet,
        'drawings_folder_id': drawings_folder_id,
    }


def _download_excel(file_id: str, mime: str = '') -> bytes:
    drive = _drive_sa()
    SHEETS_MIME = 'application/vnd.google-apps.spreadsheet'
    XLSX_MIME   = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if mime == SHEETS_MIME:
        # Файл хранится как Google Sheet — экспортируем в xlsx
        request = drive.files().export_media(fileId=file_id, mimeType=XLSX_MIME)
    else:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Распознавание специализации Excel
# ──────────────────────────────────────────────────────────────────────────────

def recognize_excel(file_id: str, filename: str, mime: str = '') -> list[str]:
    """
    Определяет список специализаций для Excel-файла.
    Возвращает например ['ПЛАЗМА'] или ['СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА'].
    Пустой список = не удалось распознать.
    """
    rules = _rules()['excel_recognition_rules']['rules']
    fname_lower = filename.lower()

    # Загружаем только заголовки и первые 30 строк данных (быстро)
    try:
        data = _download_excel(file_id, mime)
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(min_row=1, max_row=31, values_only=True))
        wb.close()
    except Exception as e:
        log.warning(f'recognize_excel: не удалось открыть {filename}: {e}')
        return []

    # Заголовки — ищем строку с наибольшим кол-вом ТЕКСТОВЫХ (не числовых) ячеек
    # Защита от выбора строки данных вместо заголовка
    import re as _re_hdr
    def _text_cell_count(row):
        return sum(1 for c in row
                   if c is not None and str(c).strip()
                   and not _re_hdr.match(r'^-?\d+[.,]?\d*$', str(c).strip()))
    header_row = []
    max_filled = -1
    for row in rows[:5]:
        score = _text_cell_count(row)
        if score > max_filled:
            max_filled = score
            header_row = [str(c).strip() if c is not None else '' for c in row]

    # Значения колонки "Сечение" из данных
    section_values = []
    if header_row:
        sec_idx = next((i for i, h in enumerate(header_row) if 'Сечение' in h or 'сечени' in h.lower()), None)
        surf_idx = next((i for i, h in enumerate(header_row) if 'Поверхн' in h), None)
        len_idx  = next((i for i, h in enumerate(header_row) if 'Длина' in h or 'длин' in h.lower()), None)
        if sec_idx is not None:
            for row in rows[1:]:
                if sec_idx < len(row) and row[sec_idx]:
                    section_values.append(str(row[sec_idx]).strip())
    else:
        sec_idx = surf_idx = len_idx = None

    def _has_col(keyword: str) -> bool:
        return any(keyword.lower() in h.lower() for h in header_row)

    def _section_matches(pattern: str) -> bool:
        import re as _re
        return any(_re.search(pattern, v, _re.IGNORECASE) for v in section_values[:20])

    for rule in rules:
        matched = True
        for cond in rule.get('conditions', []):
            if 'filename_contains' in cond:
                if cond['filename_contains'] not in fname_lower:
                    matched = False; break
            elif 'filename_contains_any' in cond:
                if not any(k in fname_lower for k in cond['filename_contains_any']):
                    matched = False; break
            elif 'column_contains' in cond and 'value_contains' in cond:
                col_kw = cond['column_contains']
                val_kw = cond['value_contains']
                if col_kw == 'Сечение':
                    if not any(val_kw.lower() in v.lower() for v in section_values):
                        matched = False; break
                else:
                    matched = False; break
            elif 'column_contains' in cond and 'value_regex' in cond:
                col_kw = cond['column_contains']
                if col_kw == 'Сечение':
                    if not _section_matches(cond['value_regex']):
                        matched = False; break
                else:
                    matched = False; break
            elif 'has_column_contains' in cond:
                if not _has_col(cond['has_column_contains']):
                    matched = False; break
            elif 'not_has_column_contains' in cond:
                if _has_col(cond['not_has_column_contains']):
                    matched = False; break

        if matched:
            specs = rule['specialization']
            result_specs = specs if isinstance(specs, list) else [specs]
            # Для ПЛАЗМА: если в файле есть и профили — добавить ПИЛА (смешанный КМД файл)
            if result_specs == ['ПЛАЗМА'] and section_values:
                import re as _re_mix3
                _PILA_RE3 = _re_mix3.compile(
                    r'^(L |T |ШП |ПВ|Пр |Двутавр|ДБ |ДШ |ДК |Швеллер|Уголок|Труба|Тг|Tг|T_г)',
                    _re_mix3.IGNORECASE
                )
                if any(_PILA_RE3.match(v) for v in section_values[:40]):
                    result_specs = ['ПЛАЗМА', 'ПИЛА']
            # Если файл ПИЛА/ПЛАЗМА и есть колонка отверстий с ненулевыми значениями
            # Сканируем ВСЕ строки заголовка (до 5) — может быть 3-строчный заголовок
            if any(s in result_specs for s in ('ПИЛА', 'ПЛАЗМА')):
                _holes_idx = None
                for _ri in range(min(5, len(rows))):
                    for _ci, _c in enumerate(rows[_ri]):
                        if _c is not None and 'отверст' in str(_c).lower():
                            _holes_idx = _ci
                            break
                    if _holes_idx is not None:
                        break
                if _holes_idx is not None:
                    _holes_has_val = any(
                        rows[r][_holes_idx] is not None
                        and str(rows[r][_holes_idx]).strip() not in ('', '0')
                        and not any(w in str(rows[r][_holes_idx]).lower() for w in ('отверст', 'кол', 'шт'))
                        for r in range(len(rows))
                        if _holes_idx < len(rows[r])
                    )
                    if _holes_has_val and 'СВЕРЛЕНИЕ' not in result_specs:
                        result_specs = result_specs + ['СВЕРЛЕНИЕ']
            return result_specs

    # Смешанные КМД «элементы»: если есть и пластины (ПЛАЗМА) и профили (ПИЛА) — вернуть оба
    import re as _re_mix2
    _PILA_RE2 = _re_mix2.compile(
        r'^(L |T |ШП |ПВ|Пр |Двутавр|ДБ |ДШ |ДК |Швеллер|Уголок|Труба|Тг|Tг|T_г)',
        _re_mix2.IGNORECASE
    )
    _has_plate2   = any('пластина' in v.lower() or 'лист' in v.lower() for v in section_values[:40])
    _has_profile2 = any(_PILA_RE2.match(v) for v in section_values[:40])
    if _has_plate2 and _has_profile2 and _has_col('Сечение') and not _has_col('Поверхн'):
        _mix_specs = ['ПЛАЗМА', 'ПИЛА']
        if _has_col('отверст'):
            _hix = next((i for i, h in enumerate(header_row) if 'отверст' in h.lower()), None)
            if _hix is not None and any(
                rows[r][_hix] is not None and str(rows[r][_hix]).strip() not in ('', '0')
                for r in range(1, len(rows)) if _hix < len(rows[r])
            ):
                _mix_specs.append('СВЕРЛЕНИЕ')
        return _mix_specs

    return []


def scan_and_recognize(folder_id: str) -> dict:
    """
    Сканирует папку и распознаёт специализации всех Excel-файлов.
    Возвращает:
      {
        'ok': True,
        'folder_name': str,
        'existing_sheet': ... | None,
        'drawings_folder_id': str | None,
        'recognized': [
            {'file': {'id':..,'name':..}, 'specs': ['ПЛАЗМА']},
            ...
        ],
        'unrecognized': [{'id':..,'name':..}, ...],
      }
    """
    scan = scan_drive_folder(folder_id)
    if not scan['ok']:
        return scan

    recognized = []
    unrecognized = []
    for xf in scan['excel_files']:
        specs = recognize_excel(xf['id'], xf['name'], xf.get('mime', ''))
        if specs:
            recognized.append({'file': xf, 'specs': specs})
        else:
            unrecognized.append(xf)

    return {
        'ok': True,
        'folder_name': scan['folder_name'],
        'existing_sheet': scan['existing_sheet'],
        'drawings_folder_id': scan['drawings_folder_id'],
        'recognized': recognized,
        'unrecognized': unrecognized,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Пересчёт цепочки зависимостей
# ──────────────────────────────────────────────────────────────────────────────

def compute_dependencies(active_specs: list[str]) -> list[tuple[str, str]]:
    """
    Вычисляет минимальный набор зависимостей с учётом исключённых специализаций.
    Транзитивно пробрасывает через пропущенные звенья.

    Пример: нет СВЕРЛЕНИЯ → ПЛАЗМА→СБОРКА, ПИЛА→СБОРКА вместо *→СВЕРЛЕНИЕ→СБОРКА
    """
    full_chain = _rules()['dependency_chain']  # [[A, B], ...]
    active = set(active_specs)

    # Граф: spec → список прямых потомков (из full_chain)
    children: dict[str, list[str]] = {}
    parents:  dict[str, list[str]] = {}
    for a, b in full_chain:
        children.setdefault(a, []).append(b)
        parents.setdefault(b, []).append(a)

    # Для каждой активной пары (A, B) добавляем зависимость только если:
    # — A активен, B активен
    # — нет другого активного узла X такого что A→...→X→...→B (X — промежуточный)
    #   иначе связь A→B будет избыточной (она покрыта цепочкой A→X→B)
    #
    # Алгоритм: для каждого активного B найти ближайших активных предшественников
    # обходом графа вверх, останавливаясь на первом встреченном активном узле.

    def nearest_active_ancestors(spec: str) -> list[str]:
        """BFS вверх по full_chain, останавливаемся на первом активном узле."""
        result = []
        queue = list(parents.get(spec, []))
        visited = set()
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            if node in active:
                result.append(node)
            else:
                # Узел исключён — поднимаемся выше
                queue.extend(parents.get(node, []))
        return result

    deps = []
    for spec in active_specs:
        for ancestor in nearest_active_ancestors(spec):
            pair = (ancestor, spec)
            if pair not in deps:
                deps.append(pair)

    return deps


# ──────────────────────────────────────────────────────────────────────────────
# Публичные хелперы
# ──────────────────────────────────────────────────────────────────────────────

def get_all_specs() -> list[str]:
    """Все специализации в порядке из project_rules.json."""
    return _rules()['specializations']


def build_source_map(active_specs: list[str], recognized: list[dict]) -> list[dict]:
    """
    Для каждой активной специализации определяет источники (все файлы).
    Если несколько файлов → одна спец — показываем все, каждый можно отключить.
    Fallback: спец из assembly_group берут файлы СБОРКА если своих нет.

    Формат: [{'spec':..., 'entries':[{'file':{...},'active':True,'source_type':'direct'|'fallback'}]}, ...]
    Пустая специализация: entries = []
    """
    rules = _rules()
    assembly_group = rules.get('assembly_group', ['СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА', 'ОТГРУЗКА'])
    fallback_spec  = rules.get('assembly_fallback', 'СБОРКА')

    spec_files: dict[str, list] = {}
    for item in recognized:
        for s in item['specs']:
            if s not in spec_files:
                spec_files[s] = []
            spec_files[s].append(item['file'])

    fallback_files = spec_files.get(fallback_spec, [])
    result = []
    for spec in active_specs:
        if spec == 'СВЕРЛЕНИЕ':
            # СВЕРЛЕНИЕ всегда авто из ПИЛА/ПЛАЗМА — никогда не грузим напрямую
            _hole_src_files = [
                f for item in recognized
                for f in [item['file']]
                if 'СВЕРЛЕНИЕ' in item['specs']
            ]
            entries = [{'file': f, 'active': True, 'source_type': 'auto_from_holes'} for f in _hole_src_files]
        elif spec in spec_files:
            entries = [{'file': f, 'active': True, 'source_type': 'direct'} for f in spec_files[spec]]
        elif spec in assembly_group and fallback_files:
            entries = [{'file': f, 'active': True, 'source_type': 'fallback'} for f in fallback_files]
        else:
            entries = []
        result.append({'spec': spec, 'entries': entries})
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Создание Google Sheet
# ──────────────────────────────────────────────────────────────────────────────


def detect_unknown_sections(source_map: list) -> dict:
    """
    Сканирует Excel файлы (ПЛАЗМА/ПИЛА) и возвращает секции,
    не распознанные ни как ПЛАЗМА (лист/пластина), ни как ПИЛА (профиль).
    Возвращает {section_value: [позиция1, ...]} — до 5 позиций на секцию.
    """
    import re as _re_unk
    PLATE_RE   = _re_unk.compile(r'пластина|лист', _re_unk.IGNORECASE)
    PROFILE_RE = _re_unk.compile(
        r'^(L |T |ШП |ПВ|Пр |Двутавр|ДБ |ДШ |ДК |Швеллер|Уголок|Труба|Тг|Tг|T_г)', _re_unk.IGNORECASE
    )

    unknown: dict = {}
    seen_files: set = set()

    for entry in source_map:
        spec = entry.get('spec', '')
        if spec not in ('ПЛАЗМА', 'ПИЛА'):
            continue
        for e in entry.get('entries', []):
            if not e.get('active'):
                continue
            xf = e['file']
            fid = xf['id']
            if fid in seen_files:
                continue
            seen_files.add(fid)

            try:
                data = _download_excel(fid, xf.get('mime', ''))
                wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(min_row=1, values_only=True))
                wb.close()
            except Exception as exc:
                log.warning(f'detect_unknown_sections: не удалось открыть {xf.get("name")}: {exc}')
                continue

            header_row = []
            for row in rows[:5]:
                non_empty = sum(1 for c in row if c is not None and str(c).strip())
                if non_empty > len(header_row):
                    header_row = [str(c).strip() if c is not None else '' for c in row]

            sec_idx = next((i for i, h in enumerate(header_row) if 'сечени' in h.lower()), None)
            pos_idx = next(
                (i for i, h in enumerate(header_row) if 'поз' in h.lower() and 'чертеж' in h.lower()),
                None
            )
            if pos_idx is None:
                pos_idx = next((i for i, h in enumerate(header_row) if h.lower().startswith('поз')), None)
            if sec_idx is None:
                continue

            for row in rows[1:]:
                if sec_idx >= len(row) or not row[sec_idx]:
                    continue
                sec_val = str(row[sec_idx]).strip()
                if not sec_val:
                    continue
                if PLATE_RE.search(sec_val) or PROFILE_RE.match(sec_val):
                    continue
                pos_val = ''
                if pos_idx is not None and pos_idx < len(row) and row[pos_idx]:
                    pos_val = str(row[pos_idx]).strip()
                if sec_val not in unknown:
                    unknown[sec_val] = []
                if pos_val and pos_val not in unknown[sec_val]:
                    unknown[sec_val].append(pos_val)

    return {k: v[:5] for k, v in unknown.items()}

def create_spreadsheet(project_name: str, folder_id: str) -> str:
    """
    Создаёт новый Google Spreadsheet в указанной папке Drive.
    Использует OAuth2 (пользователь) чтобы файл создался в его Drive.
    Возвращает sheet_id.
    """
    drive = _drive_oauth()
    body = {
        'name': project_name,
        'mimeType': 'application/vnd.google-apps.spreadsheet',
        'parents': [folder_id],
    }
    f = drive.files().create(body=body, fields='id').execute()
    sheet_id = f['id']

    # Даём service account права редактора — чтобы он мог писать данные
    sa_email = _SACredentials.from_service_account_file(
        config.GOOGLE_SA_KEY, scopes=[]
    ).service_account_email
    drive.permissions().create(
        fileId=sheet_id,
        body={'type': 'user', 'role': 'writer', 'emailAddress': sa_email},
        sendNotificationEmail=False,
    ).execute()

    return sheet_id


# ──────────────────────────────────────────────────────────────────────────────
# Настройка вкладок: заголовки + форматирование
# ──────────────────────────────────────────────────────────────────────────────

# Высота строки заголовка в пикселях → в "points" для Sheets API (1px ≈ 0.75pt, API принимает в пикселях*100/72?)
# gspread format принимает pixelSize напрямую
_HEADER_BG   = {'red': 0.937, 'green': 0.937, 'blue': 0.937}  # #EFEFEF
_HEADER_TEXT = {'red': 0.0,   'green': 0.0,   'blue': 0.0}


def setup_sheet_tabs(sheet_id: str, active_specs: list[str]) -> None:
    """
    Создаёт вкладки специализаций, форматирование, дропдауны
    СТАТУС / ОБЯЗАТЕЛЬНАЯ / ИСПОЛНИТЕЛЬ.
    """
    rules      = _rules()
    col_defs   = rules['columns']
    col_widths = rules['column_widths_px']
    ALL_SPECS  = rules['specializations']

    # Порядок активных спец — он же порядок колонок в СОТРУДНИКИ начиная с G
    active_ordered = [s for s in ALL_SPECS if s in active_specs]

    gc = _gc(SCOPES_RW)
    ss = gc.open_by_key(sheet_id)

    existing_titles = {ws.title for ws in ss.worksheets()}
    default_sheet = None
    if 'Sheet1' in existing_titles or 'Лист1' in existing_titles:
        title = 'Sheet1' if 'Sheet1' in existing_titles else 'Лист1'
        default_sheet = ss.worksheet(title)

    created_sheets = []
    for spec in active_ordered:
        if spec not in col_defs:
            log.warning(f'setup_sheet_tabs: нет колонок для {spec}')
            continue
        cols = col_defs[spec]
        ws = ss.add_worksheet(title=spec, rows=1000, cols=len(cols))
        created_sheets.append((ws, cols))
        _w(ws.update, [[spec]], range_name='B1')
        _w(ws.update, [cols],   range_name='A2')
        time.sleep(1)

    if default_sheet and created_sheets:
        time.sleep(2)
        ss.del_worksheet(default_sheet)

    time.sleep(3)
    service  = _sheets_service()
    requests = []

    for spec_idx, (ws, cols) in enumerate(created_sheets):
        sid    = ws.id
        n_cols = len(cols)

        # Заморозить строки 1-2
        requests.append({'updateSheetProperties': {
            'properties': {'sheetId': sid, 'gridProperties': {'frozenRowCount': 2}},
            'fields': 'gridProperties.frozenRowCount',
        }})
        # Высота строки заголовка
        requests.append({'updateDimensionProperties': {
            'range': {'sheetId': sid, 'dimension': 'ROWS', 'startIndex': 1, 'endIndex': 2},
            'properties': {'pixelSize': 60},
            'fields': 'pixelSize',
        }})
        # Форматирование строки заголовков
        requests.append({'repeatCell': {
            'range': {'sheetId': sid, 'startRowIndex': 1, 'endRowIndex': 2,
                      'startColumnIndex': 0, 'endColumnIndex': n_cols},
            'cell': {'userEnteredFormat': {
                'backgroundColor': _HEADER_BG,
                'textFormat': {'bold': True, 'fontSize': 9, 'fontFamily': 'Arial', 'foregroundColor': _HEADER_TEXT},
                'horizontalAlignment': 'CENTER',
                'verticalAlignment': 'MIDDLE',
                'wrapStrategy': 'WRAP',
            }},
            'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)',
        }})
        # Скрыть ROW_ID (col A)
        requests.append({'updateDimensionProperties': {
            'range': {'sheetId': sid, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 1},
            'properties': {'pixelSize': 1, 'hiddenByUser': True},
            'fields': 'pixelSize,hiddenByUser',
        }})
        # Ширины колонок
        for ci, col_name in enumerate(cols[1:], start=1):
            width = col_widths.get(col_name, 100)
            requests.append({'updateDimensionProperties': {
                'range': {'sheetId': sid, 'dimension': 'COLUMNS',
                          'startIndex': ci, 'endIndex': ci + 1},
                'properties': {'pixelSize': width},
                'fields': 'pixelSize',
            }})

        stat_col_idx  = next((i for i, c in enumerate(cols) if c == 'СТАТУС'),       None)
        ob_col_idx    = next((i for i, c in enumerate(cols) if c == 'ОБЯЗАТЕЛЬНАЯ'), None)
        exec_col_idx  = next((i for i, c in enumerate(cols) if c == 'ИСПОЛНИТЕЛЬ'),  None)
        dplan_col_idx = next((i for i, c in enumerate(cols) if c == 'ДАТА ПЛАН'),    None)
        dfact_col_idx = next((i for i, c in enumerate(cols) if c == 'ДАТА ФАКТ'),    None)

        # Условное форматирование СТАТУС
        if stat_col_idx is not None:
            for status, style in STATUS_COLORS.items():
                requests.append({'addConditionalFormatRule': {
                    'rule': {
                        'ranges': [{'sheetId': sid, 'startRowIndex': 2, 'endRowIndex': 1000,
                                    'startColumnIndex': stat_col_idx, 'endColumnIndex': stat_col_idx + 1}],
                        'booleanRule': {
                            'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': status}]},
                            'format': {'backgroundColor': style['bg']},
                        },
                    },
                    'index': 0,
                }})
            # Дропдауны СТАТУС/ОБЯЗАТЕЛЬНАЯ/ИСПОЛНИТЕЛЬ добавляются позже (add_data_validations)

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={'requests': requests}
        ).execute()


def setup_service_tabs(sheet_id: str, active_specs: list[str]) -> None:
    """
    Создаёт служебные вкладки: СОТРУДНИКИ, ТАРИФЫ, ЗАВИСИМОСТИ, _HELPER.
    Форматирование точно совпадает с эталоном (Щучин Кули - наряды).
    Вызывать после setup_sheet_tabs().
    """
    ALL_SPECS = _rules()['specializations']
    spec_headers = [s for s in ALL_SPECS if s in active_specs]

    # Ждём сброса лимита после setup_sheet_tabs (60 write req/min)
    time.sleep(65)

    gc = _gc(SCOPES_RW)
    ss  = gc.open_by_key(sheet_id)
    svc = _sheets_service()

    # Цвет ярлыка служебных вкладок (тёмно-серый)
    TAB_COLOR = {'red': 0.216, 'green': 0.278, 'blue': 0.310}

    # Цвета фона заголовков (RGB float)
    BG_TITLE  = {'red': 0.91, 'green': 0.91, 'blue': 0.91}  # #E8E8E8 — строка-заголовок секции
    BG_HEADER = {'red': 0.87, 'green': 0.87, 'blue': 0.87}  # #DEDEDE — строка с именами колонок
    BG_SEP    = {'red': 0.96, 'green': 0.96, 'blue': 0.96}  # #F5F5F5 — разделитель
    BG_DARK   = {'red': 0.74, 'green': 0.74, 'blue': 0.74}  # #BCBCBC — заголовок ЗАВИСИМОСТИ

    def _fmt(bold=False, size=9, bg=None, align='LEFT'):
        f = {'textFormat': {'bold': bold, 'fontSize': size}, 'horizontalAlignment': align}
        if bg:
            f['backgroundColor'] = bg
        return f

    def _repeat(sid, r1, r2, c1, c2, fmt):
        return {'repeatCell': {
            'range': {'sheetId': sid, 'startRowIndex': r1, 'endRowIndex': r2,
                      'startColumnIndex': c1, 'endColumnIndex': c2},
            'cell': {'userEnteredFormat': fmt},
            'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)',
        }}

    def _tab_color_req(sid, color):
        return {'updateSheetProperties': {
            'properties': {'sheetId': sid, 'tabColor': color},
            'fields': 'tabColor',
        }}

    def _freeze(sid, rows):
        return {'updateSheetProperties': {
            'properties': {'sheetId': sid, 'gridProperties': {'frozenRowCount': rows}},
            'fields': 'gridProperties.frozenRowCount',
        }}

    requests = []

    # ── СОТРУДНИКИ ──────────────────────────────────────────────────────────
    ws_emp = _w(ss.add_worksheet, title='СОТРУДНИКИ', rows=200, cols=20)
    sid_emp = ws_emp.id

    employees = db.fetchall(
        "SELECT full_name, specialization, telegram_id, is_active "
        "FROM employees WHERE is_active=true ORDER BY full_name",
        []
    )

    # Группировка по специализации (для правой части листа)
    spec_workers: dict[str, list[str]] = {s: [] for s in spec_headers}
    emp_rows = []
    for emp in employees:
        specs = emp['specialization'] or []
        tg    = str(emp['telegram_id']) if emp['telegram_id'] else ''
        emp_rows.append(['', emp['full_name'], ', '.join(specs), tg, 'ДА', ''])
        for s in specs:
            if s in spec_workers:
                spec_workers[s].append(emp['full_name'])

    # Строки формул правой части (начиная с G4)
    # FILTER+SEARCH: работает и для "ПИЛА" (точное) и для "ПИЛА, СБОРКА" (перечисление)
    col_letters = [chr(ord('G') + i) for i in range(len(spec_headers))]
    filter_formulas = [
        f'=IFERROR(FILTER($B$4:$B$300;ISNUMBER(SEARCH({col}3;$C$4:$C$300)));"")'
        for col in col_letters
    ]

    # Пишем данные
    _w(ws_emp.update, [
        [''] * 20,                                                         # строка 1 — пустая
        ['', 'ОСНОВНОЙ СПРАВОЧНИК СОТРУДНИКОВ', '', '', '', '',            # строка 2
         'СПИСОК СОТРУДНИКОВ ПО СПЕЦИАЛИЗАЦИИ (для выпадающих списков)'],
        ['', 'ФИО', 'СПЕЦИАЛИЗАЦИЯ', 'TELEGRAM USERNAME', 'АКТИВЕН', ''] + spec_headers,  # строка 3
    ], range_name='A1')
    time.sleep(1)
    if emp_rows:
        _w(ws_emp.update, emp_rows, range_name='A4')
        time.sleep(1)
    _w(ws_emp.update, [filter_formulas], range_name='G4', value_input_option='USER_ENTERED')

    n_spec = len(spec_headers)
    def _col_width(sid, col_idx, px):
        return {'updateDimensionProperties': {
            'range': {'sheetId': sid, 'dimension': 'COLUMNS',
                      'startIndex': col_idx, 'endIndex': col_idx + 1},
            'properties': {'pixelSize': px},
            'fields': 'pixelSize',
        }}

    requests += [
        _tab_color_req(sid_emp, TAB_COLOR),
        # Строка 2: заголовки секций
        _repeat(sid_emp, 1, 2, 1, 5,          _fmt(bold=True,  size=10, bg=BG_TITLE,  align='CENTER')),
        _repeat(sid_emp, 1, 2, 5, 6,          _fmt(bold=False, size=9,  bg=BG_SEP,    align='LEFT')),
        _repeat(sid_emp, 1, 2, 6, 6+n_spec,   _fmt(bold=True,  size=10, bg=BG_TITLE,  align='CENTER')),
        # Строка 3: имена колонок
        _repeat(sid_emp, 2, 3, 1, 5,          _fmt(bold=True,  size=9,  bg=BG_HEADER, align='CENTER')),
        _repeat(sid_emp, 2, 3, 5, 6,          _fmt(bold=False, size=9,  bg=BG_SEP,    align='LEFT')),
        _repeat(sid_emp, 2, 3, 6, 6+n_spec,   _fmt(bold=True,  size=9,  bg=BG_HEADER, align='CENTER')),
        # Колонка F — разделитель на всю высоту
        _repeat(sid_emp, 0, 200, 5, 6,        _fmt(bold=False, size=9,  bg=BG_SEP)),
        # Ширины колонок: A=18, B=155, C=115, D=140, E=99, F=18, G+=115
        _col_width(sid_emp, 0, 18),   # A
        _col_width(sid_emp, 1, 155),  # B — ФИО
        _col_width(sid_emp, 2, 115),  # C — СПЕЦИАЛИЗАЦИЯ
        _col_width(sid_emp, 3, 140),  # D — TELEGRAM USERNAME
        _col_width(sid_emp, 4, 99),   # E — АКТИВЕН
        _col_width(sid_emp, 5, 18),   # F — разделитель
    ] + [_col_width(sid_emp, 6+i, 115) for i in range(n_spec)]  # G+ — специализации

    # ── ТАРИФЫ ──────────────────────────────────────────────────────────────
    time.sleep(2)
    ws_tar = _w(ss.add_worksheet, title='ТАРИФЫ', rows=50, cols=10)
    sid_tar = ws_tar.id
    _w(ws_tar.update, [
        [''] * 6,
        ['', 'ТАРИФЫ НА ВИДЫ РАБОТ', '', '', '', ''],
        ['', 'ТИП РАБОТЫ', 'УСЛОВИЕ ОТ', 'УСЛОВИЕ ДО', 'СТАВКА', 'ЕДИНИЦА'],
    ], range_name='A1')
    requests += [
        _tab_color_req(sid_tar, TAB_COLOR),
        _repeat(sid_tar, 1, 2, 1, 6, _fmt(bold=True,  size=10, bg=BG_TITLE,  align='CENTER')),
        _repeat(sid_tar, 2, 3, 1, 6, _fmt(bold=True,  size=9,  bg=BG_HEADER, align='CENTER')),
        # Ширины: A=18, B=130, C=100, D=100, E=80, F=100
        _col_width(sid_tar, 0, 18),
        _col_width(sid_tar, 1, 130),
        _col_width(sid_tar, 2, 100),
        _col_width(sid_tar, 3, 100),
        _col_width(sid_tar, 4, 80),
        _col_width(sid_tar, 5, 100),
    ]

    # ── ЗАВИСИМОСТИ ─────────────────────────────────────────────────────────
    time.sleep(2)
    ws_dep = _w(ss.add_worksheet, title='ЗАВИСИМОСТИ', rows=500, cols=6)
    sid_dep = ws_dep.id
    _w(ws_dep.update, [
        ['', 'КТО ЖДЁТ (специализация)', 'ЭЛЕМЕНТ (который ждёт)',
         'ЗАВИСИТ ОТ (специализация)', 'ПОЗИЦИЯ (должна быть выполнена)', ''],
    ], range_name='A1')
    requests += [
        _freeze(sid_dep, 1),
        _repeat(sid_dep, 0, 1, 0, 6, _fmt(bold=True, bg=BG_DARK, align='CENTER')),
        # Ширины: A=30, B=180, C=169, D=192, E=163
        _col_width(sid_dep, 0, 30),
        _col_width(sid_dep, 1, 180),
        _col_width(sid_dep, 2, 169),
        _col_width(sid_dep, 3, 192),
        _col_width(sid_dep, 4, 163),
    ]

    # ── _HELPER ─────────────────────────────────────────────────────────────
    time.sleep(2)
    _w(ss.add_worksheet, title='_HELPER', rows=200, cols=5)

    # Применяем всё форматирование одним батчем
    time.sleep(8)
    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={'requests': requests}
        ).execute()


def _sheets_service():
    creds = _SACredentials.from_service_account_file(
        config.GOOGLE_SA_KEY,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    from googleapiclient.discovery import build
    return build('sheets', 'v4', credentials=creds)


# ──────────────────────────────────────────────────────────────────────────────
# Загрузка данных из Excel в Sheet
# ──────────────────────────────────────────────────────────────────────────────

def _add_itogo_rows(sheet_id: str, tasks: list[dict]) -> None:
    """Добавляет строку ИТОГО после данных в каждую вкладку."""
    _ITOGO_BG = {'red': 0.91, 'green': 0.91, 'blue': 0.91}
    _SUM_COLS = {'КОЛ-ВО', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ',
                 'Поверхность\nЭлемент (м²)', 'КОЛ-ВО ОТВЕРСТИЙ'}
    service  = _sheets_service()
    requests = []

    for task in tasks:
        sid   = task['ws_id']
        count = task['data_count']
        cols  = task['cols']
        if count == 0:
            continue

        itogo_idx = count + 2   # 0-based: строки данных с index 2..count+1, итого на count+2
        can_merge = len(cols) > 2 and cols[2] not in _SUM_COLS

        cell_values = []
        for i, col_name in enumerate(cols):
            fmt = {'backgroundColor': _ITOGO_BG}
            if i == 0:
                cell_values.append({'userEnteredFormat': fmt})
            elif i == 1:
                cell_values.append({
                    'userEnteredValue': {'stringValue': 'ИТОГО'},
                    'userEnteredFormat': {**fmt, 'textFormat': {'bold': True},
                                         'horizontalAlignment': 'CENTER'},
                })
            elif i == 2 and can_merge:
                cell_values.append({'userEnteredFormat': fmt})
            elif col_name in _SUM_COLS:
                col_letter = chr(ord('A') + i)
                last_row   = 2 + count
                cell_values.append({
                    'userEnteredValue': {'formulaValue': '=SUM(' + col_letter + '3:' + col_letter + str(last_row) + ')'},
                    'userEnteredFormat': {**fmt, 'textFormat': {'bold': True},
                                         'horizontalAlignment': 'CENTER'},
                })
            else:
                cell_values.append({'userEnteredFormat': fmt})

        requests.append({'updateCells': {
            'rows': [{'values': cell_values}],
            'range': {'sheetId': sid, 'startRowIndex': itogo_idx, 'endRowIndex': itogo_idx + 1,
                      'startColumnIndex': 0, 'endColumnIndex': len(cols)},
            'fields': 'userEnteredValue,userEnteredFormat',
        }})
        if can_merge:
            requests.append({'mergeCells': {
                'range': {'sheetId': sid, 'startRowIndex': itogo_idx, 'endRowIndex': itogo_idx + 1,
                          'startColumnIndex': 1, 'endColumnIndex': 3},
                'mergeType': 'MERGE_ALL',
            }})

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={'requests': requests}
        ).execute()


def load_excel_data(sheet_id: str, source_map: list[dict], reference_files: list = None, section_mapping: dict = None) -> dict:
    """
    Загружает данные из source_map в соответствующие вкладки + добавляет ИТОГО.
    source_map = [{'spec':'ПЛАЗМА', 'file':{'id':...,'name':...,'mime':...}, 'source_type':'direct'|'fallback'|'empty'}, ...]
    Возвращает {'ПЛАЗМА': 42, ...} — кол-во строк.
    """
    rules       = _rules()
    col_mapping = rules['excel_recognition_rules']['column_mapping']
    col_defs    = rules['columns']

    gc = _gc(SCOPES_RW)
    ss = gc.open_by_key(sheet_id)

    # Скачиваем каждый уникальный файл один раз
    _file_cache: dict[str, tuple] = {}

    def _parse(xf: dict) -> tuple:
        fid = xf['id']
        if fid in _file_cache:
            return _file_cache[fid]
        raw   = _download_excel(fid, xf.get('mime', ''))
        wb    = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws_xl = wb.active
        rows  = list(ws_xl.iter_rows(values_only=True))
        wb.close()

        hdr_idx = 0
        for i, row in enumerate(rows[:5]):
            if any(c is not None and str(c).strip() for c in row):
                hdr_idx = i
                break
        r1 = rows[hdr_idx]
        r2 = rows[hdr_idx + 1] if hdr_idx + 1 < len(rows) else []

        # Определяем: r2 — продолжение заголовков (2-строчный шапки) или уже данные?
        # Если r2 содержит числа — это строка данных, у файла однострочный заголовок
        import re as _re_num_hdr
        _r2_is_data = any(
            c is not None and str(c).strip()
            and _re_num_hdr.match(r'^-?\d+([.,]\d+)?$', str(c).strip())
            for c in r2
        )

        if not _r2_is_data and r2:
            # Проверяем нужна ли 3-я строка заголовка
            r3 = rows[hdr_idx + 2] if hdr_idx + 2 < len(rows) else []
            _r3_is_data = any(
                c is not None and str(c).strip()
                and _re_num_hdr.match(r'^-?\d+([.,]\d+)?$', str(c).strip())
                for c in r3
            )
            # r3 — продолжение заголовка если: не числа И хотя бы r1/r2 имеют None в той же колонке
            _r3_is_hdr = (not _r3_is_data and r3 and
                any(c is not None and str(c).strip() for c in r3) and
                any(
                    (ci >= len(r1) or r1[ci] is None or str(r1[ci]).strip() == '') and
                    (ci >= len(r2) or r2[ci] is None or str(r2[ci]).strip() == '')
                    for ci, c in enumerate(r3)
                    if c is not None and str(c).strip()
                )
            )
            hdrs = []
            n_cols = max(len(r1), len(r2), len(r3) if _r3_is_hdr else 0)
            for ci in range(n_cols):
                c1 = str(r1[ci]).strip() if ci < len(r1) and r1[ci] is not None else ''
                c2 = str(r2[ci]).strip() if ci < len(r2) and r2[ci] is not None else ''
                c3 = str(r3[ci]).strip() if _r3_is_hdr and ci < len(r3) and r3[ci] is not None else ''
                parts = [p for p in (c1, c2, c3) if p]
                hdrs.append(' '.join(parts))
            ds = hdr_idx + 3 if _r3_is_hdr else hdr_idx + 2
        else:
            hdrs = [str(c).strip() if c is not None else '' for c in r1]
            ds = hdr_idx + 1

        if ds < len(rows) and all(c is None or str(c).strip() == '' for c in rows[ds]):
            ds += 1
        data_rows = rows[ds:]

        xl_to_sheet = {}
        for xi, xh in enumerate(hdrs):
            if not xh or xh.startswith('_'):
                continue
            mapped = col_mapping.get(xh)
            if xh in col_mapping and mapped is None:
                continue
            xl_to_sheet[xi] = mapped if mapped else xh

        result = (data_rows, xl_to_sheet)
        _file_cache[fid] = result
        return result

    stats       = {}
    itogo_tasks = []

    # Строки для СВЕРЛЕНИЕ, собранные из ПИЛА/ПЛАЗМА по колонке КОЛ-ВО ОТВЕРСТИЙ
    _sverlenie_pending: list = []
    # Простые профили из отправочных файлов (без подэлементов) -> в ПИЛА
    _pila_from_otpr: list = []  # list of (xl_row, xl_to_sheet, xf)
    _pila_otpr_seen: set  = set()

    for item in source_map:
        spec    = item['spec']
        entries = item.get('entries', [])
        active_entries = [e for e in entries if e.get('active', True)]
        if not active_entries or spec not in col_defs:
            continue

        sheet_cols = col_defs[spec]
        try:
            ws_gs = ss.worksheet(spec)
        except Exception:
            log.warning(f'load_excel_data: лист {spec} не найден')
            continue

        # Дедупликация единая для всех файлов одной специализации
        _seen_dedup: set = set()

        for entry in active_entries:
            xf = entry['file']
            try:
                data_rows, xl_to_sheet = _parse(xf)
            except Exception as e:
                log.error(f'load_excel_data: ошибка {xf["name"]}: {e}')
                continue

            # СВЕРЛЕНИЕ: прямая загрузка разрешена если файл содержит колонку КОЛ-ВО ОТВЕРСТИЙ
            # иначе — данные придут через _sverlenie_pending из ПИЛА/ПЛАЗМА файлов
            if spec == 'СВЕРЛЕНИЕ':
                _has_holes_direct = any(n == 'КОЛ-ВО ОТВЕРСТИЙ' for n in xl_to_sheet.values())
                if not _has_holes_direct:
                    continue

            # Индекс колонки Сечение в Excel (для фильтрации ПЛАЗМА/ПИЛА)
            import re as _re_sec
            _sec_xi = next((xi for xi, n in xl_to_sheet.items()
                            if n in ('СЕЧЕНИЕ',) or 'сечени' in n.lower()), None)
            _dedup_pk_xi = next((xi for xi, n in xl_to_sheet.items()
                                 if n in ('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка', 'ЭЛЕМЕНТ')), None)

            current_elem_grouper = None
            current_grouper_qty  = 1.0

            rows_to_write = []
            for xl_row in data_rows:
                if all(c is None or str(c).strip() == '' for c in xl_row):
                    continue
                # Пропускаем строки-итоги из Excel (Итого (кг), ИТОГО и т.п.)
                first_vals = [str(c).strip().lower() for c in xl_row if c is not None and str(c).strip()]
                if first_vals and first_vals[0].startswith('итог'):
                    continue
                # Grouper-строка: «Поз.=К1д Количество=4 ...»
                if first_vals and first_vals[0].startswith('поз.='):
                    import re as _re_grp
                    _c0 = str(xl_row[0]).strip() if xl_row[0] is not None else ''
                    _gm = _re_grp.match(r'(?i)поз[.\s]*=\s*(\S+)', _c0)
                    if _gm:
                        current_elem_grouper = _gm.group(1).strip()
                    _qm = _re_grp.search(r'(?i)количество\s*=\s*(\d+(?:[.,]\d+)?)', _c0)
                    current_grouper_qty = float(_qm.group(1).replace(',', '.')) if _qm else 1.0
                    continue
                # Пропускаем строки-разделители где PRIMARY KEY пуст
                _PK_COL = ('ПОЗ. СОГЛАСНО ЧЕРТЕЖА' if spec in ('ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ')
                           else 'ЭЛЕМЕНТ')
                _pk_xi = next((xi for xi, n in xl_to_sheet.items() if n == _PK_COL), None)
                if _pk_xi is not None:
                    _pk_raw = xl_row[_pk_xi] if _pk_xi < len(xl_row) else None
                    _pk_val = str(_pk_raw).strip() if _pk_raw is not None else ''
                    if not _pk_val:
                        continue
                    try:
                        if float(_pk_val.replace(',', '.')) == 0:
                            continue
                    except ValueError:
                        pass
                # Для ПЛАЗМА/ПИЛА: пропускаем строки с пустым КОЛ-ВО
                if spec in ('ПЛАЗМА', 'ПИЛА'):
                    _kvo_xi = next((xi for xi, n in xl_to_sheet.items() if n == 'КОЛ-ВО'), None)
                    if _kvo_xi is not None:
                        _kvo_raw = xl_row[_kvo_xi] if _kvo_xi < len(xl_row) else None
                        _kvo_val = str(_kvo_raw).strip() if _kvo_raw is not None else ''
                        if not _kvo_val:
                            continue
                        try:
                            if float(_kvo_val.replace(',', '.')) == 0:
                                continue
                        except ValueError:
                            pass
                # Фильтрация по типу сечения для смешанных файлов ПЛАЗМА/ПИЛА
                if spec in ('ПЛАЗМА', 'ПИЛА') and _sec_xi is not None:
                    _sec_val = str(xl_row[_sec_xi]).strip() if _sec_xi < len(xl_row) and xl_row[_sec_xi] else ''
                    if _sec_val:
                        _is_plate   = 'пластина' in _sec_val.lower() or 'лист' in _sec_val.lower()
                        _is_profile = bool(_re_sec.match(
                            r'^(L |T |ШП |ПВ|Пр |Двутавр|ДБ |ДШ |ДК |Швеллер|Уголок|Труба|Тг|Tг|T_г)',
                            _sec_val, _re_sec.IGNORECASE
                        ))
                        _sec_mapped = (section_mapping or {}).get(_sec_val)
                        if not _is_plate and not _is_profile and _sec_mapped:
                            if _sec_mapped == 'ПИЛА':
                                _is_profile = True
                            elif _sec_mapped == 'ПЛАЗМА':
                                _is_plate = True
                        if spec == 'ПЛАЗМА' and not _is_plate:
                            continue
                        if spec == 'ПИЛА' and not _is_profile:
                            continue
                # Dedup: для ПИЛА/ПЛАЗМА только по позиции (grouper не учитываем)
                _grp_elem = current_elem_grouper if spec not in ('ПЛАЗМА', 'ПИЛА') else ''
                if _dedup_pk_xi is not None:
                    _pk_raw2 = xl_row[_dedup_pk_xi] if _dedup_pk_xi < len(xl_row) and xl_row[_dedup_pk_xi] else ''
                    _dedup_key = (spec, str(_pk_raw2).strip(), _grp_elem)
                    if _dedup_key in _seen_dedup:
                        continue
                    _seen_dedup.add(_dedup_key)

                _DEFAULTS = {'СТАТУС': 'ПЛАН', 'ОБЯЗАТЕЛЬНАЯ': 'НЕТ'}
                sheet_row = [str(uuid.uuid4())]
                for col_name in sheet_cols[1:]:
                    val = None
                    for xi, mapped_name in xl_to_sheet.items():
                        if mapped_name == col_name and xi < len(xl_row):
                            raw = xl_row[xi]
                            _NUM_COLS = {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'Поверхность\nЭлемент (м²)'}
                            if col_name in _NUM_COLS and raw is not None:
                                try:
                                    _fval = float(str(raw).strip().replace(',', '.'))
                                    if col_name == 'КОЛ-ВО' and current_grouper_qty != 1.0:
                                        _fval = _fval * current_grouper_qty
                                    val = _fval
                                except (ValueError, TypeError):
                                    val = str(raw).strip() if raw is not None else ''
                            else:
                                val = str(raw).strip() if raw is not None else ''
                            break
                    if not val:
                        val = _DEFAULTS.get(col_name, '')
                    sheet_row.append(val)
                # Inline ЭЛЕМЕНТ из grouper
                if spec in ('ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ') and current_elem_grouper:
                    _elem_ci = next((i for i, c in enumerate(sheet_cols) if c == 'ЭЛЕМЕНТ'), None)
                    if _elem_ci is not None:
                        while len(sheet_row) <= _elem_ci:
                            sheet_row.append('')
                        if not sheet_row[_elem_ci]:
                            sheet_row[_elem_ci] = current_elem_grouper
                # Для отправочных файлов: если у элемента нет подэлементов -> ПИЛА вместо СБОРКА/СВАРКА
                if spec in ('СБОРКА', 'СВАРКА') and 'отправочн' in xf.get('name', '').lower():
                    import re as _re_otpr
                    _elem_raw_xi = next((xi for xi, n in xl_to_sheet.items()
                                        if 'элемент' in n.lower() and n != 'ЭЛЕМЕНТ'), None)
                    _marka_xi    = next((xi for xi, n in xl_to_sheet.items() if n == 'ЭЛЕМЕНТ'), None)
                    if _elem_raw_xi is not None and _marka_xi is not None:
                        _elem_cell  = str(xl_row[_elem_raw_xi]).strip() if _elem_raw_xi < len(xl_row) and xl_row[_elem_raw_xi] else ''
                        _marka_cell = str(xl_row[_marka_xi]).strip()    if _marka_xi  < len(xl_row) and xl_row[_marka_xi]    else ''
                        _subparts   = [s.strip() for s in _re_otpr.findall(r'\d+\.\s*(\S+)', _elem_cell)]
                        _is_simple  = bool(_subparts and len(_subparts) == 1
                                           and _subparts[0].lower() == _marka_cell.lower())
                        if _is_simple:
                            if spec == 'СБОРКА' and _marka_cell not in _pila_otpr_seen:
                                _pila_from_otpr.append((xl_row, xl_to_sheet, xf))
                                _pila_otpr_seen.add(_marka_cell)
                            continue  # пропускаем для СБОРКА и СВАРКА
                rows_to_write.append(sheet_row)

                # Если у строки есть отверстия — запоминаем для СВЕРЛЕНИЕ
                if spec in ('ПИЛА', 'ПЛАЗМА'):
                    _holes_xi = next((xi for xi, n in xl_to_sheet.items() if n == 'КОЛ-ВО ОТВЕРСТИЙ'), None)
                    if _holes_xi is not None:
                        _holes_raw = xl_row[_holes_xi] if _holes_xi < len(xl_row) else None
                        try:
                            _holes_val = float(str(_holes_raw).strip().replace(',', '.')) if _holes_raw else 0
                        except (ValueError, TypeError):
                            _holes_val = 0
                        if _holes_val > 0:
                            _sverlenie_pending.append({
                                'xl_row': xl_row,
                                'xl_to_sheet': xl_to_sheet,
                                'holes_qty': _holes_val,
                                'elem_grouper': current_elem_grouper,
                            })

            # Сортировка по ЭЛЕМЕНТ → ПОЗ.
            if rows_to_write:
                _elem_si = next((i for i, c in enumerate(sheet_cols) if c == 'ЭЛЕМЕНТ'), None)
                _pos_si  = next((i for i, c in enumerate(sheet_cols) if c in ('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка')), None)
                def _sort_key(row):
                    e = str(row[_elem_si]).strip().lower() if _elem_si is not None and _elem_si < len(row) else ''
                    p = str(row[_pos_si]).strip().lower() if _pos_si is not None and _pos_si < len(row) else ''
                    return (e, p)
                rows_to_write.sort(key=_sort_key)
            if rows_to_write:
                existing = ws_gs.get_all_values()
                existing_count = sum(1 for r in existing[2:] if any(c for c in r))
                # Вставляем формулу МАССА ВСЕХ = КОЛ-ВО × МАССА ЕД.
                kol_idx     = next((i for i, c in enumerate(sheet_cols) if c == 'КОЛ-ВО'), None)
                mass_ed_idx = next((i for i, c in enumerate(sheet_cols) if 'МАССА ЕД' in c), None)
                mass_all_idx= next((i for i, c in enumerate(sheet_cols) if 'МАССА ВСЕХ' in c), None)
                if kol_idx and mass_ed_idx and mass_all_idx:
                    start_r = 3 + existing_count
                    for ri, srow in enumerate(rows_to_write):
                        rn = start_r + ri
                        kol_col = chr(ord('A') + kol_idx)
                        med_col = chr(ord('A') + mass_ed_idx)
                        srow[mass_all_idx] = f'={kol_col}{rn}*{med_col}{rn}'
                # Формула СУММА К ОПЛАТЕ = КОЛ-ВО × Поверхность × Тариф (ГРУНТОВКА/ПОКРАСКА)
                if spec in ('ГРУНТОВКА', 'ПОКРАСКА'):
                    _kol_i   = next((i for i, c in enumerate(sheet_cols) if c == 'КОЛ-ВО'), None)
                    _surf_i  = next((i for i, c in enumerate(sheet_cols) if 'Поверхность' in c), None)
                    _tar_i   = next((i for i, c in enumerate(sheet_cols) if 'за (м²)' in c), None)
                    _sum_i   = next((i for i, c in enumerate(sheet_cols) if c == 'СУММА К ОПЛАТЕ'), None)
                    if None not in (_kol_i, _surf_i, _tar_i, _sum_i):
                        _start_r2 = 3 + existing_count
                        for ri, srow in enumerate(rows_to_write):
                            rn = _start_r2 + ri
                            c_col = chr(ord('A') + _kol_i)
                            d_col = chr(ord('A') + _surf_i)
                            e_col = chr(ord('A') + _tar_i)
                            while len(srow) <= _sum_i:
                                srow.append('')
                            srow[_sum_i] = f'={c_col}{rn}*{d_col}{rn}*{e_col}{rn}'
                ws_gs.update(rows_to_write, range_name=f'A{3 + existing_count}',
                             value_input_option='USER_ENTERED')

            count = len(rows_to_write)
            stats[spec] = stats.get(spec, 0) + count
            log.info(f'load_excel_data: {spec} <- {xf["name"]}: {count} строк')

        if spec in stats:
            itogo_tasks.append({'ws_id': ws_gs.id, 'data_count': stats[spec], 'cols': sheet_cols})

    # Простые профили из отправочных файлов -> ПИЛА
    if _pila_from_otpr:
        try:
            ws_pila_otp = ss.worksheet('ПИЛА')
            pila_cols_otp = col_defs.get('ПИЛА', [])
            existing_pila_otp = ws_pila_otp.get_all_values()
            _ex_pila  = sum(1 for r in existing_pila_otp[2:] if any(c for c in r))
            _kvo_ci   = next((i for i, c in enumerate(pila_cols_otp) if c == 'КОЛ-ВО'), None)
            _med_ci   = next((i for i, c in enumerate(pila_cols_otp) if 'МАССА ЕД' in c), None)
            _mall_ci  = next((i for i, c in enumerate(pila_cols_otp) if 'МАССА ВСЕХ' in c), None)
            _pos_ci   = next((i for i, c in enumerate(pila_cols_otp) if c == 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА'), 1)
            _ob_ci    = next((i for i, c in enumerate(pila_cols_otp) if c == 'ОБЯЗАТЕЛЬНАЯ'), None)
            _st_ci    = next((i for i, c in enumerate(pila_cols_otp) if c == 'СТАТУС'), None)
            _lnk_ci   = next((i for i, c in enumerate(pila_cols_otp) if 'ССЫЛКА' in c.upper() or chr(1025)+'Ж' in c.upper()), None)
            pila_rows_otp: list = []
            for _xl_r, _xl_m, _xf_o in _pila_from_otpr:
                _mk_xi = next((xi for xi, n in _xl_m.items() if n == 'ЭЛЕМЕНТ'), None)
                _kv_xi = next((xi for xi, n in _xl_m.items() if n == 'КОЛ-ВО'), None)
                _me_xi = next((xi for xi, n in _xl_m.items() if 'МАССА ЕД' in n), None)
                _lk_xi = next((xi for xi, n in _xl_m.items() if 'ССЫЛКА' in n.upper()), None)
                _mk = str(_xl_r[_mk_xi]).strip() if _mk_xi is not None and _mk_xi < len(_xl_r) and _xl_r[_mk_xi] else ''
                _kv = _xl_r[_kv_xi] if _kv_xi is not None and _kv_xi < len(_xl_r) else None
                _me = _xl_r[_me_xi] if _me_xi is not None and _me_xi < len(_xl_r) else None
                _lk = str(_xl_r[_lk_xi]).strip() if _lk_xi is not None and _lk_xi < len(_xl_r) and _xl_r[_lk_xi] else ''
                if not _mk: continue
                _srow = [''] * len(pila_cols_otp)
                _srow[0] = str(uuid.uuid4())
                _srow[_pos_ci] = _mk
                if _kvo_ci is not None and _kv is not None:
                    try: _srow[_kvo_ci] = float(str(_kv).replace(',', '.'))
                    except: _srow[_kvo_ci] = str(_kv)
                if _med_ci is not None and _me is not None:
                    try: _srow[_med_ci] = float(str(_me).replace(',', '.'))
                    except: _srow[_med_ci] = str(_me)
                if _ob_ci  is not None: _srow[_ob_ci]  = 'НЕТ'
                if _st_ci  is not None: _srow[_st_ci]  = 'ПЛАН'
                if _lnk_ci is not None: _srow[_lnk_ci] = _lk
                pila_rows_otp.append(_srow)
            if pila_rows_otp and _kvo_ci is not None and _med_ci is not None and _mall_ci is not None:
                _start_po = 3 + _ex_pila
                for ri, srow in enumerate(pila_rows_otp):
                    rn = _start_po + ri
                    kc = chr(ord('A') + _kvo_ci); mc = chr(ord('A') + _med_ci)
                    srow[_mall_ci] = f'={kc}{rn}*{mc}{rn}'
                ws_pila_otp.update(pila_rows_otp, range_name=f'A{3+_ex_pila}',
                                   value_input_option='USER_ENTERED')
                stats['ПИЛА'] = stats.get('ПИЛА', 0) + len(pila_rows_otp)
                # Update itogo_tasks so ИТОГО does not overwrite Пр1
                for _it in itogo_tasks:
                    if _it["ws_id"] == ws_pila_otp.id:
                        _it["data_count"] += len(pila_rows_otp)
                        break
                log.info(f'load_excel_data: ПИЛА <- простые профили из отправочных: {len(pila_rows_otp)} строк')
        except Exception as _e_otp:
            log.warning(f'load_excel_data: ошибка записи профилей из отправочных в ПИЛА: {_e_otp}')

    if itogo_tasks:
        _add_itogo_rows(sheet_id, itogo_tasks)

    # Записываем строки СВЕРЛЕНИЕ
    if _sverlenie_pending:
        try:
            ws_sv = ss.worksheet('СВЕРЛЕНИЕ')
            sv_cols = col_defs.get('СВЕРЛЕНИЕ', [])
            sv_rows_to_write = []
            _seen_sv: set = set()
            for _p in _sverlenie_pending:
                _xrow = _p['xl_row']
                _xmap = _p['xl_to_sheet']
                _holes_qty = _p['holes_qty']
                _grp = _p['elem_grouper']
                _pos_xi = next((xi for xi, n in _xmap.items() if n == 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА'), None)
                _pos_val = str(_xrow[_pos_xi]).strip() if _pos_xi is not None and _pos_xi < len(_xrow) and _xrow[_pos_xi] else ''
                _dedup_sv = (_pos_val, _grp or '')
                if _dedup_sv in _seen_sv:
                    continue
                _seen_sv.add(_dedup_sv)
                _DEFAULTS_SV = {'СТАТУС': 'ПЛАН', 'ОБЯЗАТЕЛЬНАЯ': 'НЕТ'}
                sv_row = [str(uuid.uuid4())]
                for col_name in sv_cols[1:]:
                    val = None
                    if col_name == 'КОЛ-ВО ОТВЕРСТИЙ':
                        val = _holes_qty
                    else:
                        for xi, mapped_name in _xmap.items():
                            if mapped_name == col_name and xi < len(_xrow):
                                raw = _xrow[xi]
                                if col_name in {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)'} and raw is not None:
                                    try:
                                        val = float(str(raw).strip().replace(',', '.'))
                                    except (ValueError, TypeError):
                                        val = str(raw).strip() if raw is not None else ''
                                else:
                                    val = str(raw).strip() if raw is not None else ''
                                break
                    if not val and val != 0:
                        val = _DEFAULTS_SV.get(col_name, '')
                    sv_row.append(val)
                if _grp:
                    _elem_ci_sv = next((i for i, c in enumerate(sv_cols) if c == 'ЭЛЕМЕНТ'), None)
                    if _elem_ci_sv is not None:
                        while len(sv_row) <= _elem_ci_sv:
                            sv_row.append('')
                        if not sv_row[_elem_ci_sv]:
                            sv_row[_elem_ci_sv] = _grp
                sv_rows_to_write.append(sv_row)
            if sv_rows_to_write:
                existing_sv = ws_sv.get_all_values()
                existing_sv_count = sum(1 for r in existing_sv[2:] if any(c for c in r))
                ws_sv.update(
                    [[str(c) if c is not None else '' for c in r] for r in sv_rows_to_write],
                    range_name=f'A{3 + existing_sv_count}',
                    value_input_option='USER_ENTERED'
                )
                stats['СВЕРЛЕНИЕ'] = stats.get('СВЕРЛЕНИЕ', 0) + len(sv_rows_to_write)
                _add_itogo_rows(sheet_id, [{'ws_id': ws_sv.id, 'data_count': stats['СВЕРЛЕНИЕ'], 'cols': sv_cols}])
                log.info(f'load_excel_data: СВЕРЛЕНИЕ <- {len(sv_rows_to_write)} строк из отверстий')
        except Exception as _sv_e:
            log.warning(f'load_excel_data: СВЕРЛЕНИЕ из отверстий не записано: {_sv_e}')

    return stats

# ──────────────────────────────────────────────────────────────────────────────
# Сохранение проекта в БД
# ──────────────────────────────────────────────────────────────────────────────

def add_data_validations(sheet_id: str, active_specs: list[str], row_counts: dict) -> None:
    """
    Добавляет дропдауны СТАТУС / ОБЯЗАТЕЛЬНАЯ / ИСПОЛНИТЕЛЬ строго для строк с данными.
    row_counts: {spec: n} — количество строк данных.
    Вызывать ПОСЛЕ load_excel_data и ПОСЛЕ setup_service_tabs (нужна СОТРУДНИКИ).
    """
    rules = _rules()
    col_defs  = rules['columns']
    ALL_SPECS = rules['specializations']
    active_ordered = [s for s in ALL_SPECS if s in active_specs]

    gc = _gc(SCOPES_RW)
    ss = gc.open_by_key(sheet_id)
    service = _sheets_service()
    requests = []

    for spec_idx, spec in enumerate(active_ordered):
        cols = col_defs.get(spec)
        if not cols:
            continue
        n_rows = row_counts.get(spec, 0)
        if n_rows == 0:
            continue
        # Данные: строки 3..(2+n_rows), индексы 2..(2+n_rows-1)
        row_end = 2 + n_rows  # exclusive

        stat_col_idx  = next((i for i, c in enumerate(cols) if c == 'СТАТУС'),       None)
        ob_col_idx    = next((i for i, c in enumerate(cols) if c == 'ОБЯЗАТЕЛЬНАЯ'), None)
        exec_col_idx  = next((i for i, c in enumerate(cols) if c == 'ИСПОЛНИТЕЛЬ'),  None)
        dplan_col_idx = next((i for i, c in enumerate(cols) if c == 'ДАТА ПЛАН'),    None)
        dfact_col_idx = next((i for i, c in enumerate(cols) if c == 'ДАТА ФАКТ'),    None)

        try:
            ws = ss.worksheet(spec)
        except Exception:
            continue
        sid = ws.id

        if stat_col_idx is not None:
            requests.append({'setDataValidation': {
                'range': {'sheetId': sid, 'startRowIndex': 2, 'endRowIndex': row_end,
                          'startColumnIndex': stat_col_idx, 'endColumnIndex': stat_col_idx + 1},
                'rule': {
                    'condition': {'type': 'ONE_OF_LIST', 'values': [
                        {'userEnteredValue': v} for v in ['ПЛАН', 'ВЫПОЛНЕНО', 'ЧАСТИЧНО', 'БЛОК']
                    ]},
                    'showCustomUi': True, 'strict': True,
                },
            }})

        if ob_col_idx is not None:
            requests.append({'setDataValidation': {
                'range': {'sheetId': sid, 'startRowIndex': 2, 'endRowIndex': row_end,
                          'startColumnIndex': ob_col_idx, 'endColumnIndex': ob_col_idx + 1},
                'rule': {
                    'condition': {'type': 'ONE_OF_LIST', 'values': [
                        {'userEnteredValue': 'ДА'}, {'userEnteredValue': 'НЕТ'}
                    ]},
                    'showCustomUi': True, 'strict': False,
                },
            }})

        if exec_col_idx is not None:
            emp_col_letter = chr(ord('G') + spec_idx)
            requests.append({'setDataValidation': {
                'range': {'sheetId': sid, 'startRowIndex': 2, 'endRowIndex': row_end,
                          'startColumnIndex': exec_col_idx, 'endColumnIndex': exec_col_idx + 1},
                'rule': {
                    'condition': {
                        'type': 'ONE_OF_RANGE',
                        'values': [{'userEnteredValue':
                                    '=СОТРУДНИКИ!$' + emp_col_letter + '$4:$' + emp_col_letter + '$103'}],
                    },
                    'showCustomUi': True, 'strict': False,
                },
            }})

        for _date_ci in [dplan_col_idx, dfact_col_idx]:
            if _date_ci is not None:
                requests.append({'setDataValidation': {
                    'range': {'sheetId': sid, 'startRowIndex': 2, 'endRowIndex': row_end,
                              'startColumnIndex': _date_ci, 'endColumnIndex': _date_ci + 1},
                    'rule': {
                        'condition': {'type': 'DATE_IS_VALID'},
                        'showCustomUi': True, 'strict': False,
                    },
                }})

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={'requests': requests}
        ).execute()
        log.info(f'add_data_validations: {len(requests)} правил добавлено')


def fill_deps_sheet(sheet_id: str, active_specs: list[str]) -> None:
    """Записывает пары зависимостей в лист ЗАВИСИМОСТИ."""
    deps = compute_dependencies(active_specs)
    if not deps:
        return
    gc  = _gc(SCOPES_RW)
    ss  = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet('ЗАВИСИМОСТИ')
    except Exception:
        log.warning('fill_deps_sheet: лист ЗАВИСИМОСТИ не найден')
        return
    rows = [[spec_from, '', spec_to, ''] for spec_from, spec_to in deps]
    ws.update(rows, range_name='B2', value_input_option='RAW')
    log.info(f'fill_deps_sheet: {len(deps)} зависимостей записано')




def fill_element_column(sheet_id: str, bom: dict, active_specs: list[str]) -> int:
    """
    Zapolnyaet kolonku ELEMENT v detail-spetakh (PLAZMA, PILA, SVERLENIE)
    na osnove BOM-dannykh iz chertezhey.

    bom: {element: [pozitsii]}  naprimer {'K1a': ['pk9', 'pk11', 'ps5']}

    Stroim obratnyy map {pozitsiya_lower: element}. Esli pozitsiya
    vstrechaetsya v neskolkikh elementakh — pishetsya pervyy po alfavitu.
    Vozvraschaet kolichestvo zapolnennykh yacheek.
    """
    if not bom:
        return 0

    rules    = _rules()
    col_defs = rules['columns']
    ALL_SPECS = rules['specializations']

    # Detail-spety: imeyut kolonku s 'POZ'
    detail_specs = [s for s in ALL_SPECS if s in active_specs
                    and any('ПОЗ' in c.upper() for c in col_defs.get(s, []))]
    if not detail_specs:
        return 0

    # Obratnyy map: pozitsiya_lower -> element (prioritet — pervyy po alfavitu)
    pos_to_elem: dict[str, str] = {}
    for element in sorted(bom.keys()):
        for pos in bom[element]:
            pk = pos.lower()
            if pk not in pos_to_elem:
                pos_to_elem[pk] = element

    log.info(f'fill_element_column: {len(pos_to_elem)} pozitsiy -> elementov')

    gc      = _gc(SCOPES_RW)
    ss      = gc.open_by_key(sheet_id)
    service = _sheets_service()
    total   = 0

    for spec in detail_specs:
        cols = col_defs.get(spec, [])
        pos_col_idx  = next((i for i, c in enumerate(cols) if 'ПОЗ' in c.upper()), None)
        elem_col_idx = next((i for i, c in enumerate(cols) if c == 'ЭЛЕМЕНТ'), None)
        if pos_col_idx is None or elem_col_idx is None:
            continue
        try:
            ws   = ss.worksheet(spec)
            vals = ws.get_all_values()
        except Exception as e:
            log.warning(f'fill_element_column: ne mogu prochitat {spec}: {e}')
            continue

        updates = []
        for row_i, row in enumerate(vals[2:], start=3):  # dannyye s 3 stroki
            if pos_col_idx >= len(row):
                continue
            pos_val = row[pos_col_idx].strip()
            if not pos_val:
                continue
            element = pos_to_elem.get(pos_val.lower())
            if not element:
                continue
            # Yacheyka v kolonke ELEMENT
            col_letter = chr(ord('A') + elem_col_idx)
            updates.append({
                'range': f"'{spec}'!{col_letter}{row_i}",
                'values': [[element]],
            })
            total += 1

        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={'valueInputOption': 'RAW', 'data': updates},
            ).execute()
            log.info(f'fill_element_column: {spec} — zapolneno {len(updates)} yacheek ELEMENT')

    return total


def fill_element_deps(sheet_id: str, bom: dict, active_specs: list[str]) -> int:
    """
    Dopolnyaet list ZAVISIMOSTI strok-ami urovnya element->pozitsiya.

    Algoritmm:
      1. Chitaet detail-listy (PLAZMA, PILA) — stroit {pozitsiya_lower: spec}
      2. Dlya kazhdogo (element, pozitsii) iz bom pishet:
           assembly_spec | element | detail_spec | pozitsiya
      3. Pishet tsepochku assembly->assembly (SVARKA|el|SBORKA|el, etc.)
    """
    if not bom:
        return 0

    rules     = _rules()
    col_defs  = rules['columns']
    ALL_SPECS = rules['specializations']

    detail_specs   = [s for s in ALL_SPECS if s in active_specs
                      and any('ПОЗ' in c.upper() for c in col_defs.get(s, []))]
    assembly_specs = [s for s in ALL_SPECS if s in active_specs and s not in detail_specs]

    gc = _gc(SCOPES_RW)
    ss = gc.open_by_key(sheet_id)

    # {pozitsiya_lower: spec}
    pos_to_spec: dict[str, str] = {}
    for spec in detail_specs:
        cols = col_defs.get(spec, [])
        pos_col_idx = next((i for i, c in enumerate(cols) if 'ПОЗ' in c.upper()), None)
        if pos_col_idx is None:
            continue
        try:
            ws   = ss.worksheet(spec)
            vals = ws.get_all_values()
            for row in vals[2:]:
                if pos_col_idx < len(row) and row[pos_col_idx].strip():
                    pos_to_spec[row[pos_col_idx].strip().lower()] = spec
        except Exception as e:
            log.warning(f'fill_element_deps: ne mogu prochitat {spec}: {e}')

    # {element_lower: [assembly_specs]}
    elem_to_assembly: dict[str, list] = {}
    for spec in assembly_specs:
        cols = col_defs.get(spec, [])
        el_col_idx = next((i for i, c in enumerate(cols) if c == 'ЭЛЕМЕНТ'), None)
        if el_col_idx is None:
            continue
        try:
            ws   = ss.worksheet(spec)
            vals = ws.get_all_values()
            for row in vals[2:]:
                if el_col_idx < len(row) and row[el_col_idx].strip():
                    el = row[el_col_idx].strip()
                    el_lo = el.lower()
                    if el_lo not in elem_to_assembly:
                        elem_to_assembly[el_lo] = []
                    if spec not in elem_to_assembly[el_lo]:
                        elem_to_assembly[el_lo].append(spec)
        except Exception as e:
            log.warning(f'fill_element_deps: ne mogu prochitat {spec}: {e}')

    assembly_chain = [(a, b) for a, b in compute_dependencies(active_specs)
                      if a in assembly_specs and b in assembly_specs]

    new_rows: list[list] = []
    seen: set = set()

    def _add(row):
        key = tuple(row)
        if key not in seen:
            seen.add(key)
            new_rows.append(row)

    for element, positions in bom.items():
        el_lo = element.lower()
        a_specs = elem_to_assembly.get(el_lo, assembly_specs)

        # Detail -> assembly
        for pos in positions:
            d_spec = pos_to_spec.get(pos.lower())
            if not d_spec:
                continue
            for a_spec in a_specs:
                _add([a_spec, element, d_spec, pos])

        # Assembly chain
        for (chain_from, chain_to) in assembly_chain:
            if chain_from in a_specs and chain_to in a_specs:
                _add([chain_from, element, chain_to, element])

    if not new_rows:
        log.info('fill_element_deps: net novykh strok')
        return 0

    try:
        ws_z = ss.worksheet('ЗАВИСИМОСТИ')
        existing = ws_z.get_all_values()
        next_row = max(2, len(existing)) + 1
        ws_z.update(new_rows, range_name=f'B{next_row}', value_input_option='RAW')
        log.info(f'fill_element_deps: dobavleno {len(new_rows)} strok s {next_row}')
    except Exception as e:
        log.error(f'fill_element_deps: oshibka zapisi: {e}')
        return 0

    return len(new_rows)


def cut_pdf_drawings(drawings_folder_id: str) -> dict:
    """
    Narezaet PDF na stranitsy, opredelyaet marku cherezha iz uglovogo shtampa
    i izvlekaet BOM-tablitsu s pozitsiyami elementov.

    Vozvraschaet:
      {
        'files': {marka: drive_file_id, ...},
        'bom':   {element: [pozitsiya1, pozitsiya2, ...], ...}
      }
    """
    import io as _io
    import re as _re
    from pypdf import PdfReader, PdfWriter
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
    from pdf2image import convert_from_bytes
    import pytesseract

    PDF_MIME = 'application/pdf'
    # Marka v uglovom shtampe: 1-3 zaglavnykh + tsifry + opcionalno strochnaya
    MARK_RE = _re.compile(r'\b([А-ЯЁ]{1,3}\d+[а-яё]?)\b')
    # Pozitsii v BOM: strochnye bukvy + tsifry (pk1, gk1, ul1...)
    POS_RE  = _re.compile(r'\b([а-яё]{1,4}\d+[а-яё]?)\b')
    # Zaglolovok elementa v BOM: "Pod=K1" ili "K1" kak pervoe slovo stroki
    ELEM_RE = _re.compile(r'[Пп]од\s*[=~\-]\s*([А-ЯЁ]{1,3}\d+[а-яё]?)')

    drive_sa    = _drive_sa()
    drive_oauth = _drive_oauth()

    res = drive_sa.files().list(
        q=f"trashed=false and '{drawings_folder_id}' in parents and mimeType='{PDF_MIME}'",
        fields='files(id,name)',
        pageSize=50,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    pdf_files = res.get('files', [])
    if not pdf_files:
        log.info('cut_pdf_drawings: PDF ne naydeny v papke')
        return {'files': {}, 'bom': {}}

    log.info(f'cut_pdf_drawings: naydeno {len(pdf_files)} PDF faylov')

    files_result: dict[str, str] = {}
    bom_result:   dict[str, list] = {}
    used_marks:   set[str] = set()

    # ── Файловый лог для отладки нарезки ──────────────────────────────────
    import os as _os, datetime as _dt
    _log_dir = '/root/naryady/prod/logs'
    _os.makedirs(_log_dir, exist_ok=True)
    _log_path = _os.path.join(_log_dir, 'cut_pdf_' + _dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '.log')
    _lf = open(_log_path, 'w', encoding='utf-8')
    def _clog(msg):
        _lf.write(msg + '\n')
        _lf.flush()
        log.info(msg)
    _clog(f'=== cut_pdf_drawings START | folder={drawings_folder_id} | pdfs={len(pdf_files)} ===')

    for xf in pdf_files:
        pdf_id   = xf['id']
        pdf_name = xf['name']
        _clog(f'\n--- PDF: {pdf_name} ---')

        req = drive_sa.files().get_media(fileId=pdf_id, supportsAllDrives=True)
        buf = _io.BytesIO()
        dl  = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        raw = buf.getvalue()

        reader = PdfReader(_io.BytesIO(raw))
        total  = len(reader.pages)
        uploaded_this_file = 0

        for page_idx in range(total):
            try:
                images = convert_from_bytes(
                    raw, first_page=page_idx+1, last_page=page_idx+1, dpi=150
                )
                img  = images[0]
                h, w = img.height, img.width

                # ── Угловой штамп: правые 45%, нижние 20% ──
                stamp = img.crop((int(w * 0.55), int(h * 0.80), w, h))
                stamp_text = pytesseract.image_to_string(stamp, lang='rus').strip()

                # ── BOM-таблица: нижние 55%, левые 65% ──
                bom_area = img.crop((0, int(h * 0.45), int(w * 0.65), h))
                bom_text = pytesseract.image_to_string(bom_area, lang='rus').strip()

            except Exception as e:
                log.warning(f'  str {page_idx+1}: OCR oshibka — {e}')
                continue

            # ── Марки из штампа: ищем строку "Отправочная марка …" ──
            # Триггеры: "Отправочная марка" ИЛИ "Детали"
            # Марка стоит на строке НИЖЕ триггера в таблице штампа ГОСТ,
            # поэтому захватываем остаток строки триггера + следующую строку.
            # Если триггер не найден — страница пропускается (не чертёж).
            # Триггер 1: "Отправочная марка" / "Детали" — марка на СЛЕДУЮЩЕЙ строке
            OTPR_BLOCK_RE = _re.compile(
                r'(?:отправочн[\w\s]*марк[аи]?|детали)([^\n]*\n[^\n]*|[^\n]+)',
                _re.IGNORECASE
            )
            # Триггер 2: "Пластины" — марки на ТОЙ ЖЕ строке (пластины пл1-пл4; пс1-пс4)
            PLASTINY_RE = _re.compile(
                r'(?:пластин[ыа]?)([^\n]+)',
                _re.IGNORECASE
            )
            # Триггер 3: "Лист просечно" — марки на СЛЕДУЮЩЕЙ строке (нв1-нв2)
            PROSECHNO_RE = _re.compile(
                r'(?:лист\s+просечно[^\n]*)([^\n]*\n[^\n]*)',
                _re.IGNORECASE
            )
            RANGE_RE2  = _re.compile(r'([А-ЯЁа-яё]{1,4})(\d+)\s*[-–]\s*(?:[А-ЯЁа-яё]{1,4})?(\d+)')
            MARK_ANY   = _re.compile(r'[А-ЯЁа-яё]{1,4}\d+[а-яё]?')

            def _expand_marks(raw):
                # Предобработка: "!" → "1" (OCR ошибка), убрать пробел внутри марки
                raw = raw.replace('!', '1')
                raw = _re.sub(r'([А-ЯЁа-яё]{1,4})\s+(\d)', r'\1\2', raw)

                # OCR фикс только для КОНЦА числового диапазона:
                # "ум1-умб" → "ум1-ум6", но "К1б" (суффикс марки) НЕ трогаем
                _RANGE_OCR = {'б': '6', 'З': '3', 'з': '3', 'О': '0', 'о': '0'}
                def _fix_range_end(m):
                    # Заменяем OCR-буквы на цифры только в числовой части конца диапазона
                    dash_and_prefix = m.group(1)  # "- ум" или "-"
                    num_part = m.group(2)          # "б" или "б3" или "6" итд
                    fixed = ''.join(_RANGE_OCR.get(c, c) for c in num_part)
                    return dash_and_prefix + fixed

                # Применяем к части ПОСЛЕ тире: [-–]\s*([А-Яа-я]{0,4})([бЗзОо\d]+)
                raw = _re.sub(
                    r'([-–]\s*[А-ЯЁа-яё]{0,4})([бЗзОо\d]+)',
                    _fix_range_end,
                    raw
                )

                result = []
                for part in _re.split(r'[,;]\s*', raw):
                    rm = RANGE_RE2.search(part)
                    if rm:
                        prefix, n1, n2 = rm.group(1), int(rm.group(2)), int(rm.group(3))
                        for n in range(n1, n2 + 1):
                            mk = f'{prefix}{n}'
                            if mk not in result:
                                result.append(mk)
                    else:
                        for mk in MARK_ANY.findall(part):
                            if mk not in result:
                                result.append(mk)
                return [m for m in result if 1 < len(m) <= 6]

            # Собираем марки из всех возможных триггеров
            marks = []
            raw_parts = []

            otpr_m = OTPR_BLOCK_RE.search(stamp_text)
            if otpr_m:
                raw_parts.append(('OTPR', otpr_m.group(1).strip()))

            plas_m = PLASTINY_RE.search(stamp_text)
            if plas_m:
                raw_parts.append(('PLAS', plas_m.group(1).strip()))

            pros_m = PROSECHNO_RE.search(stamp_text)
            if pros_m:
                raw_parts.append(('PROS', pros_m.group(1).strip()))

            if not raw_parts:
                _clog(f'  стр {page_idx+1}/{total}: триггер не найден — пропуск')
                continue

            for trig, raw_part in raw_parts:
                new_marks = _expand_marks(raw_part)
                _clog(f'  стр {page_idx+1}/{total} [{trig}]: raw={raw_part!r} → марки={new_marks}')
                for m in new_marks:
                    if m not in marks:
                        marks.append(m)

            if not marks:
                _clog(f'  стр {page_idx+1}/{total}: марки не найдены — пропуск')
                continue

            # ── Загружаем страницу один раз ──
            writer  = PdfWriter()
            writer.add_page(reader.pages[page_idx])
            out_buf = _io.BytesIO()
            writer.write(out_buf)
            out_buf.seek(0)
            page_bytes = out_buf.getvalue()

            # ── Каждую марку регистрируем (один файл, разные имена через symlink в dict) ──
            uploaded_fid = None
            for mark in marks:
                final_mark = mark
                if final_mark in used_marks:
                    suffix = 2
                    while f'{mark}_{suffix}' in used_marks:
                        suffix += 1
                    final_mark = f'{mark}_{suffix}'
                used_marks.add(final_mark)
                final_name = final_mark + '.pdf'

                if uploaded_fid is None:
                    # Загружаем файл только первый раз
                    media = MediaIoBaseUpload(
                        _io.BytesIO(page_bytes), mimetype=PDF_MIME, resumable=False
                    )
                    uploaded = drive_oauth.files().create(
                        body={'name': final_name, 'parents': [drawings_folder_id]},
                        media_body=media,
                        fields='id',
                    ).execute()
                    uploaded_fid = uploaded['id']
                    _clog(f'  ✓ СОЗДАН {final_name} (id={uploaded_fid})')
                    uploaded_this_file += 1
                else:
                    # Дополнительные марки той же страницы — копируем файл в Drive
                    copied = drive_oauth.files().copy(
                        fileId=uploaded_fid,
                        body={'name': final_name, 'parents': [drawings_folder_id]},
                    ).execute()
                    _clog(f'  ✓ КОПИЯ  {final_name} (id={copied["id"]})')

                files_result[final_mark] = uploaded_fid if uploaded_fid else uploaded_fid

            # ── BOM: только если первая марка заглавная → это сборочный элемент ──
            # Структурированный BOM ("Поз=Р1 ...") → per-element positions
            # Плоский BOM (заглавная марка) → все позиции к ней
            # Строчные марки → только файлы, BOM пропускаем
            SECTION_RE2 = _re.compile(r'[Пп]оз[=.]\s*([А-ЯЁа-яё]{1,4}\d+[а-яё]?)')
            struct_bom = {}
            cur_el = None
            for _line in bom_text.split('\n'):
                sec_m = SECTION_RE2.search(_line)
                if sec_m:
                    cur_el = sec_m.group(1)
                    struct_bom.setdefault(cur_el, [])
                elif cur_el:
                    for _p in POS_RE.findall(_line):
                        if len(_p) <= 6 and _p not in struct_bom[cur_el]:
                            struct_bom[cur_el].append(_p)

            if struct_bom:
                for _el, _pos in struct_bom.items():
                    if _pos:
                        bom_result.setdefault(_el, [])
                        for _p in _pos:
                            if _p not in bom_result[_el]:
                                bom_result[_el].append(_p)
                _bkeys = list(struct_bom.keys())
                log.info(f'  BOM structured: elements={_bkeys}')
            else:
                primary = marks[0] if marks else ''
                if primary and primary[0].isupper():
                    positions = [p for p in POS_RE.findall(bom_text)
                                 if len(p) <= 6 and p.lower() != primary.lower()]
                    if positions:
                        bom_result.setdefault(primary, [])
                        for _p in positions:
                            if _p not in bom_result[primary]:
                                bom_result[primary].append(_p)
                        log.info(f'  BOM flat: element={primary} positions={positions}')
                    else:
                        log.info(f'  BOM flat: element={primary} no positions')
                else:
                    log.info(f'  Detail marks {marks} — files only no BOM')

        _clog(f'  Итого по {pdf_name}: {uploaded_this_file}/{total} страниц обработано')
        time.sleep(1)

    _clog(f'\n=== ИТОГ: файлов={len(files_result)}, BOM-элементов={len(bom_result)} ===')
    for _el, _pos in sorted(bom_result.items()):
        _clog(f'  BOM {_el} → {_pos}')
    _clog(f'Файлы: {sorted(files_result.keys())}')
    _lf.close()
    log.info(f'cut_pdf_drawings: files={len(files_result)}, bom_elements={len(bom_result)} | log={_log_path}')
    return {'files': files_result, 'bom': bom_result}

def link_drawings(sheet_id: str, drawings_folder_id: str, active_specs: list[str]) -> int:
    """
    Сканирует папку чертежей, сопоставляет имена файлов с колонкой
    «ПОЗ. СОГЛАСНО ЧЕРТЕЖА» и заполняет «ССЫЛКА НА ЧЕРТЁЖ» в каждом листе.
    Возвращает количество заполненных ссылок.
    """
    if not drawings_folder_id:
        log.warning('link_drawings: drawings_folder_id не задан')
        return 0

    rules      = _rules()
    col_defs   = rules['columns']
    ALL_SPECS  = rules['specializations']
    active_ordered = [s for s in ALL_SPECS if s in active_specs]

    # ─── Список файлов в папке чертежей ──────────────────────────────────────
    drive = _drive_sa()
    try:
        def _collect_pdfs(fid, depth=0):
            if depth > 3:
                return []
            r = drive.files().list(
                q=f"trashed=false and '{fid}' in parents",
                fields='files(id,name,mimeType)',
                pageSize=500,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            result = []
            for f in r.get('files', []):
                if 'folder' in f['mimeType']:
                    result.extend(_collect_pdfs(f['id'], depth + 1))
                elif 'pdf' in f['mimeType'].lower() or f['name'].lower().endswith('.pdf'):
                    result.append(f)
            return result
        drawing_files = _collect_pdfs(drawings_folder_id)
    except Exception as e:
        log.error(f'link_drawings: ошибка получения файлов: {e}')
        return 0

    if not drawing_files:
        log.info('link_drawings: папка чертежей пуста')
        return 0

    import os as _os
    import re as _re

    # Извлекаем позиционные коды из имени файла: буквы+цифры+необязательные_буквы
    # Примеры: К1, пб1, Св1а, тс10, П2г, шп14
    _POS_RE = _re.compile(r'[а-яёa-zА-ЯЁA-Z]+\d+[а-яёA-ZА-ЯЁa-z]*', _re.IGNORECASE)

    def _extract_positions(name: str) -> list:
        base = _os.path.splitext(name)[0]
        tokens = _POS_RE.findall(base)
        return [t.strip().lower() for t in tokens if len(t) >= 2]

    # drawing_index: позиционный_код -> url
    # Один PDF может содержать несколько позиций (первый файл выигрывает при дублях)
    drawing_index: dict[str, str] = {}
    for f in drawing_files:
        url = f'https://drive.google.com/file/d/{f["id"]}/view'
        for pos in _extract_positions(f['name']):
            if pos not in drawing_index:
                drawing_index[pos] = url

    log.info(f'link_drawings: папка {drawings_folder_id}, файлов={len(drawing_files)}, '
             f'кодов в индексе={len(drawing_index)}, примеры={list(drawing_index.keys())[:10]}')

    # ─── Заполнение в каждом листе ───────────────────────────────────────────
    gc = _gc(SCOPES_RW)
    ss = gc.open_by_key(sheet_id)
    service = _sheets_service()
    total_linked = 0

    for spec in active_ordered:
        cols = col_defs.get(spec)
        if not cols:
            continue

        pos_col_idx  = next((i for i, c in enumerate(cols) if 'ПОЗ' in c.upper()), None)
        elem_col_idx = next((i for i, c in enumerate(cols) if c == 'ЭЛЕМЕНТ'), None)
        link_col_idx = next((i for i, c in enumerate(cols) if 'ССЫЛКА' in c.upper()), None)
        # Используем либо ПОЗ (детальные листы), либо ЭЛЕМЕНТ (сборочные)
        match_col_idx = pos_col_idx if pos_col_idx is not None else elem_col_idx
        if match_col_idx is None or link_col_idx is None:
            continue

        try:
            ws = ss.worksheet(spec)
        except Exception:
            continue

        all_vals = ws.get_all_values()
        if len(all_vals) < 3:   # нет данных
            continue

        updates = []
        for row_idx, row in enumerate(all_vals[2:], start=3):  # данные с 3-й строки (1-based)
            if not row or row_idx - 3 >= len(all_vals) - 2:
                continue
            pos_raw = row[match_col_idx].strip() if match_col_idx < len(row) else ''
            if not pos_raw:
                continue
            pos_val = _re.sub(r'[\s_-]+', '', pos_raw.lower())

            # Точное совпадение (после нормализации)
            url = drawing_index.get(pos_val)
            if url is None:
                # Частичное: ключ содержит позицию или наоборот
                for key, u in drawing_index.items():
                    if pos_val and key.startswith(pos_val) and (len(key) == len(pos_val) or not key[len(pos_val)].isdigit()):
                        url = u
                        break

            if url:
                col_letter = chr(ord('A') + link_col_idx)
                updates.append({
                    'range': f'{col_letter}{row_idx}',
                    'values': [[url]],
                })
                total_linked += 1

        if updates:
            ws.batch_update(updates, value_input_option='USER_ENTERED')
            log.info(f'link_drawings: {spec} — заполнено {len(updates)} ссылок')
        time.sleep(1)  # rate limit

    return total_linked


def register_project(
    project_name: str,
    folder_id: str,
    sheet_id: str,
    drawings_folder_id: Optional[str],
    active_specs: list[str],
    created_by: int,
    status: str = 'СОЗДАНИЕ',
) -> dict:
    """
    Сохраняет проект в БД. Возвращает {'ok': True, 'id': int} или {'ok': False, 'error': str}.
    """
    existing = db.fetchone("SELECT id, project_name FROM projects WHERE folder_id=%s", [folder_id])
    if existing:
        return {'ok': False, 'error': f"Проект «{existing['project_name']}» уже существует."}

    db.execute(
        """INSERT INTO projects (project_name, folder_id, sheet_id, drawings_folder_id, status, created_by, active_specs)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        [project_name, folder_id, sheet_id, drawings_folder_id, status, created_by, active_specs]
    )
    project = db.fetchone("SELECT id FROM projects WHERE folder_id=%s", [folder_id])
    project_id = project['id']

    # Сохраняем активные специализации и зависимости
    # (таблица project_specs если есть, иначе просто логируем)
    deps = compute_dependencies(active_specs)
    log.info(f'register_project: {project_name} id={project_id}, specs={active_specs}, deps={deps}')

    # project_deps удалена — таблица не используется (убрана 2026-08-17)

    return {'ok': True, 'id': project_id}

def activate_project(project_id: int) -> None:
    """Переводит проект в статус АКТИВНЫЙ — финальный шаг создания."""
    db.execute(
        "UPDATE projects SET status='АКТИВНЫЙ' WHERE id=%s",
        [project_id]
    )
    log.info(f'activate_project: project_id={project_id} -> АКТИВНЫЙ')

