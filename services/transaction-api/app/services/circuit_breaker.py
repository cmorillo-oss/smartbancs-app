"""Circuit breaker propio (sin librerías) con tres estados.

    CLOSED  --(N fallos CONSECUTIVOS)-->  OPEN  --(pasa recovery_s)-->  HALF_OPEN
       ^                                   ^                                |
       |                                   +-------(la prueba falla)--------+
       +--------------(la prueba tiene éxito)-------------------------------+

POR QUÉ existe: si la IA está caída o lenta, seguir llamándola desperdicia una conexión y una
tarea por cada transferencia esperando un timeout que ya sabemos que llegará. El breaker "falla
rápido": con el circuito abierto ni se intenta la llamada, se usa el fallback y se protege
tanto a nuestro servicio como a la IA (que así puede recuperarse sin recibir carga).

Sin locks a propósito: todo el código corre en un único event loop de asyncio y ninguna de estas
operaciones hace `await`, por lo que no puede interrumpirse a la mitad (son atómicas entre awaits).
"""
import time
from collections.abc import Callable
from enum import IntEnum


class State(IntEnum):
    # Los valores son los que expone la métrica smartbancs_ai_circuit_breaker_state.
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_s: float = 30.0,
        on_state_change: Callable[[State], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        # `clock` inyectable: las pruebas avanzan el tiempo sin dormir 30 segundos de verdad.
        # monotonic (no time.time): no salta hacia atrás si cambia la hora del sistema.
        self._clock = clock
        self._on_state_change = on_state_change
        self._state = State.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = False

    @property
    def state(self) -> State:
        # OPEN -> HALF_OPEN es "perezoso": ocurre al consultar, sin temporizadores en segundo plano.
        if self._state is State.OPEN and self._clock() - self._opened_at >= self.recovery_s:
            self._set(State.HALF_OPEN)
            self._half_open_in_flight = False
        return self._state

    def would_allow(self) -> bool:
        """Consulta SIN efectos: ¿dejaría pasar una llamada ahora? Sirve para no hacer trabajo previo inútil."""
        state = self.state
        if state is State.CLOSED:
            return True
        if state is State.HALF_OPEN:
            return not self._half_open_in_flight
        return False

    def allow_request(self) -> bool:
        """Pide permiso para llamar. En HALF_OPEN solo deja pasar UNA llamada de prueba a la vez:
        si dejara pasar todas, una IA que apenas se recupera recibiría de golpe todo el tráfico acumulado."""
        state = self.state
        if state is State.CLOSED:
            return True
        if state is State.HALF_OPEN and not self._half_open_in_flight:
            self._half_open_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._half_open_in_flight = False
        if self._state is not State.CLOSED:
            self._set(State.CLOSED)

    def record_failure(self) -> None:
        self._half_open_in_flight = False
        if self._state is State.HALF_OPEN:
            # La prueba falló: la IA sigue mal. Otra ronda completa de espera.
            self._open()
            return
        self._consecutive_failures += 1
        if self._state is State.CLOSED and self._consecutive_failures >= self.failure_threshold:
            self._open()

    def release(self) -> None:
        """Devuelve el permiso de HALF_OPEN sin contar éxito ni fallo (llamada que no llegó a ejecutarse)."""
        self._half_open_in_flight = False

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._set(State.OPEN)

    def _set(self, new: State) -> None:
        if new is not self._state:
            self._state = new
            if self._on_state_change:
                self._on_state_change(new)
