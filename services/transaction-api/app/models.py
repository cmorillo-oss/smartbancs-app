"""Modelos de dominio internos.

DECISIÓN: no usamos ORM declarativo. El camino crítico es SQL explícito (repositories/),
porque el algoritmo de la transferencia depende de sentencias exactas
(`ORDER BY id ... FOR UPDATE`, `UPDATE ... RETURNING`) y el ORM añade un mapa de identidad
y refrescos implícitos que dificultan razonar (y defender) qué bloqueo se toma y cuándo.
Aquí solo viven las estructuras inmutables que viajan entre capas.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LockedAccount:
    """Cuenta leída BAJO bloqueo (FOR UPDATE): sus datos no pueden cambiar hasta el commit."""

    id: int
    account_number: str
    customer_id: str
    balance: Decimal
    currency: str
    status: str
