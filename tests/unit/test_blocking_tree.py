"""El árbol de bloqueos debe ser LINEAL aunque la cola sea densa (regresión del incidente: 2.5 GB de RAM)."""
import time

from app.services.blocking_tree import build_tree


def s(pid, blocked_by=(), app="api"):
    return {"pid": pid, "blocked_by": list(blocked_by), "application_name": app, "state": "active",
            "transaction_seconds": 1.0, "query": f"q{pid}"}


def count(node):
    return 1 + sum(count(c) for c in node["blocks"])


def test_sin_bloqueos():
    assert build_tree([])["summary"] == "sin bloqueos"


def test_cadena_a_bloquea_b_bloquea_c():
    t = build_tree([s(1), s(2, [1]), s(3, [2])])
    assert t["roots"] == 1
    root = t["tree"][0]
    assert root["pid"] == 1 and root["blocked_sessions"] == 2
    assert root["blocks"][0]["pid"] == 2 and root["blocks"][0]["blocks"][0]["pid"] == 3


def test_cola_densa_de_200_sesiones_es_lineal_y_rapida():
    # Caso real: el culpable (pid 0) y una cola donde CADA sesión está bloqueada por el culpable Y por todas las anteriores.
    n = 200
    sessions = [s(0, app="incident-simulator")] + [s(i, [0, *range(1, i)]) for i in range(1, n + 1)]
    started = time.perf_counter()
    t = build_tree(sessions)
    assert time.perf_counter() - started < 1.0      # la versión recursiva ingenua no terminaba nunca (~2^200 caminos)
    assert t["roots"] == 1 and t["tree"][0]["pid"] == 0
    assert count(t["tree"][0]) == n + 1             # cada sesión aparece EXACTAMENTE una vez
    assert t["tree"][0]["blocked_sessions"] == n
    assert "pid 0 (incident-simulator" in t["summary"]


def test_deadlock_en_curso_se_reporta_como_ciclo_sin_colgarse():
    t = build_tree([s(1, [2]), s(2, [1])])
    assert t["roots"] == 0 and t["cycle_sessions"] == [1, 2]
    assert "deadlock" in t["summary"]
