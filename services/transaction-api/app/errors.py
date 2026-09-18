"""Errores de dominio con código estable.

POR QUÉ códigos (error_code) además del mensaje: los clientes y las métricas
(smartbancs_transaction_errors_total{error_code}) dependen de un identificador estable;
el texto humano puede cambiar sin romper a nadie.
"""


class DomainError(Exception):
    status_code: int = 400
    error_code: str = "DOMAIN_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AccountNotFound(DomainError):
    status_code = 404
    error_code = "ACCOUNT_NOT_FOUND"


class AccountNotActive(DomainError):
    status_code = 422
    error_code = "ACCOUNT_NOT_ACTIVE"


class CurrencyMismatch(DomainError):
    status_code = 422
    error_code = "CURRENCY_MISMATCH"


class InsufficientFunds(DomainError):
    status_code = 422
    error_code = "INSUFFICIENT_FUNDS"


class IdempotencyKeyConflict(DomainError):
    # 409: la misma clave se reutilizó con un cuerpo DISTINTO; devolver el resultado viejo
    # engañaría al cliente haciéndole creer que su nueva orden se ejecutó.
    status_code = 409
    error_code = "IDEMPOTENCY_KEY_CONFLICT"


class LockTimeout(DomainError):
    # 503 (no 500): el fallo es transitorio y reintentar (con la misma Idempotency-Key) es seguro.
    status_code = 503
    error_code = "LOCK_TIMEOUT"


class DeadlockRetryExhausted(DomainError):
    status_code = 503
    error_code = "DEADLOCK_RETRY_EXHAUSTED"


class PoolTimeout(DomainError):
    status_code = 503
    error_code = "POOL_TIMEOUT"
