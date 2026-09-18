"""Contratos de entrada/salida de la API (Pydantic v2)."""
import re
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings

# Formato de Idempotency-Key: uuid o cualquier id corto y seguro (columna VARCHAR(64)).
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TransferRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_account: str = Field(min_length=1, max_length=20)
    dest_account: str = Field(min_length=1, max_length=20)
    # Decimal, NUNCA float: 0.1 + 0.2 != 0.3 en binario. max_digits/decimal_places replican
    # NUMERIC(18,2) para rechazar en la frontera lo que la BD truncaría o rechazaría.
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("amount")
    @classmethod
    def _two_decimals(cls, v: Decimal) -> Decimal:
        # Normaliza 50.1 -> 50.10 para que la respuesta original y el replay sean idénticas
        # y la comparación de idempotencia (mismo cuerpo) no falle por formato.
        return v.quantize(Decimal("0.01"))

    @field_validator("currency")
    @classmethod
    def _currency_supported(cls, v: str) -> str:
        v = v.upper()
        if v not in settings.currencies:
            raise ValueError(f"divisa no soportada: {v}")
        return v

    @model_validator(mode="after")
    def _accounts_differ(self):
        # Se valida aquí para responder 422 claro; la BD también lo impide (chk_different_accounts).
        if self.source_account == self.dest_account:
            raise ValueError("la cuenta origen y destino deben ser distintas")
        return self


class TransferResponse(BaseModel):
    transaction_id: uuid.UUID
    idempotency_key: str
    status: str
    source_account: str
    dest_account: str
    amount: Decimal
    currency: str
    trace_id: str | None
    created_at: datetime
    completed_at: datetime | None


class AccountResponse(BaseModel):
    account_number: str
    customer_id: str
    balance: Decimal
    currency: str
    status: str
    version: int
    updated_at: datetime


class TransactionListItem(TransferResponse):
    # Desde el punto de vista de la cuenta consultada: DEBIT = salió dinero, CREDIT = entró.
    direction: str


class TransactionPage(BaseModel):
    items: list[TransactionListItem]
    limit: int
    offset: int
    has_more: bool


class LedgerEntryResponse(BaseModel):
    account_number: str
    entry_type: str
    amount: Decimal
    balance_after: Decimal


class TransactionDetail(TransferResponse):
    error_code: str | None
    ledger_entries: list[LedgerEntryResponse]
