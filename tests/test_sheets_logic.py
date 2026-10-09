"""Потокобезопасность кешей клиентов Google (без обращения к Google)."""
import threading
from src import sheets


def test_tldict_isolated_per_thread():
    d = sheets._TLDict()
    d['k'] = 1
    seen = {}

    def worker():
        seen['has'] = 'k' in d
        d['k'] = 2
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen['has'] is False and d['k'] == 1
