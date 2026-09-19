"""Pruebas del circuit breaker con un reloj simulado (sin dormir 30 segundos reales)."""
from app.services.circuit_breaker import CircuitBreaker, State


def make(threshold=5, recovery=30.0):
    now = [0.0]
    changes = []
    cb = CircuitBreaker(threshold, recovery, on_state_change=lambda s: changes.append(s), clock=lambda: now[0])
    return cb, now, changes


def fail(cb, n):
    for _ in range(n):
        assert cb.allow_request()
        cb.record_failure()


def test_cuatro_fallos_no_abren():
    cb, _, _ = make()
    fail(cb, 4)
    assert cb.state is State.CLOSED


def test_un_exito_reinicia_la_cuenta_los_fallos_deben_ser_consecutivos():
    cb, _, _ = make()
    fail(cb, 4)
    cb.record_success()
    fail(cb, 4)
    assert cb.state is State.CLOSED


def test_cinco_fallos_consecutivos_abren_y_rechazan():
    cb, _, _ = make()
    fail(cb, 5)
    assert cb.state is State.OPEN
    assert not cb.allow_request() and not cb.would_allow()


def test_pasa_a_half_open_a_los_30s_exactos():
    cb, now, _ = make()
    fail(cb, 5)
    now[0] = 29.9
    assert cb.state is State.OPEN
    now[0] = 30.0
    assert cb.state is State.HALF_OPEN


def test_half_open_permite_una_sola_prueba():
    cb, now, _ = make()
    fail(cb, 5)
    now[0] = 30.0
    assert cb.allow_request()
    assert not cb.allow_request()  # la segunda simultánea se rechaza


def test_prueba_fallida_reabre_y_espera_otro_ciclo_completo():
    cb, now, _ = make()
    fail(cb, 5)
    now[0] = 30.0
    cb.allow_request()
    cb.record_failure()
    assert cb.state is State.OPEN
    now[0] = 59.0  # solo 29s desde la reapertura
    assert cb.state is State.OPEN
    now[0] = 60.0
    assert cb.state is State.HALF_OPEN


def test_prueba_exitosa_cierra():
    cb, now, changes = make()
    fail(cb, 5)
    now[0] = 30.0
    cb.allow_request()
    cb.record_success()
    assert cb.state is State.CLOSED
    assert changes == [State.OPEN, State.HALF_OPEN, State.CLOSED]  # cada transición notifica (alimenta la métrica)


def test_release_devuelve_el_permiso_sin_contar_fallo():
    cb, now, _ = make()
    fail(cb, 5)
    now[0] = 30.0
    assert cb.allow_request()
    cb.release()
    assert cb.would_allow() and cb.state is State.HALF_OPEN
