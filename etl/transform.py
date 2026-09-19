"""Pipeline ETL de 4 etapas: EXTRACT -> CLEAN -> TRANSFORM -> LOAD.

  EXTRACT   lee el CSV crudo; las filas con número de columnas incorrecto van a cuarentena.
  CLEAN     normaliza fechas/montos/divisas/texto, imputa nulos (documentado), deduplica y pone en
            cuarentena TODO lo irrecuperable (nada se descarta en silencio).
  TRANSFORM enriquece: categoría, agregados por cliente y día, banderas de comportamiento atípico.
  LOAD      escribe Parquet particionado y carga las tablas analíticas en PostgreSQL.

Cada etapa registra en JSON (con trace_id = id de la corrida) cuántos registros entran y salen, y al
final se imprime y guarda un reporte con la duración de cada etapa.

Uso:  python transform.py        (o: make etl)
"""
import csv
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import structlog

ETL_DIR = Path(__file__).parent
RAW_PATH = ETL_DIR / "data" / "raw" / "raw_transactions.csv"
QUARANTINE_DIR = ETL_DIR / "data" / "quarantine"
PROCESSED_DIR = ETL_DIR / "data" / "processed"
EVIDENCE_DIR = Path(os.environ.get("ETL_EVIDENCE_DIR", "/evidence/etl"))
DDL_PATH = Path(os.environ.get("ANALYTICS_DDL", "/database/ddl/04_analytics.sql"))
DSN = os.environ.get("DATABASE_DSN", "postgresql://smartbancs:smartbancs@localhost:5432/smartbancs")

RUN_ID = str(uuid.uuid4())

# ---- Reglas de negocio del CLEAN (cada umbral con nombre y razón) --------------------------------
AMOUNT_CAP = Decimal("1000000")  # por encima => se trata como error de captura, no como transacción real
MIN_DATE = date(2020, 1, 1)
NULL_MARKERS = {"", "n/a", "na", "null", "none", "-", "?", "??"}
CURRENCY_MAP = {  # texto normalizado (minúsculas, sin acentos ni símbolos) -> ISO-4217
    "usd": "USD", "us$": "USD", "dolares": "USD", "dollars": "USD", "dolar": "USD",
    "eur": "EUR", "euros": "EUR", "euro": "EUR",
    "cop": "COP", "pesos": "COP", "peso": "COP",
}
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y"]  # 2024-01-15 | 15/01/2024 (día/mes) | Jan 15, 2024
CATEGORY_KEYWORDS = {
    "alimentacion": ("super", "market", "restaurante", "comida", "grocery"),
    "transporte": ("uber", "taxi", "gasolina", "fuel"),
    "suscripciones": ("netflix", "spotify", "suscrip"),
    "vivienda": ("renta", "alquiler", "inmobiliaria"),
    "servicios": ("luz", "internet", "agua", "factura"),
    "salud": ("farmacia", "medicina", "clinica"),
    "compras": ("tienda", "online", "compra"),
}
HIGH_FREQUENCY_DAY = 4      # >= 4 transacciones de un cliente en un día => bandera (con ~1.2/día de media, 4 ya es inusual)
MAD_THRESHOLD = 3.5         # umbral estándar de Iglewicz-Hoaglin para el z-score modificado
MIN_HISTORY_FOR_ATYPICAL = 5

structlog.configure(processors=[
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    structlog.processors.JSONRenderer(),
])
_log = structlog.get_logger()


def log(event: str, **kw) -> None:
    # trace_id = id de la corrida: correlaciona todas las líneas de esta ejecución (regla de oro 4).
    _log.info(event, service="etl", trace_id=RUN_ID, **kw)


# ------------------------------------------------------------------------------------------------
# Utilidades de limpieza
# ------------------------------------------------------------------------------------------------
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(v: str | None) -> str | None:
    """Recorta, colapsa espacios y convierte marcadores de nulo (N/A, null, -) en None."""
    if v is None:
        return None
    v = re.sub(r"\s+", " ", v).strip()
    return None if v.lower() in NULL_MARKERS else v


def parse_date(v: str) -> date | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None  # p. ej. 31/02/2024 (día inexistente), 2024-13-45, 15-01-24


def parse_amount(v: str) -> Decimal | None:
    """'1,234.56' | '$500.00' | 'US$ 5.00' | '(30.00)' -> Decimal. Devuelve None si no es un número válido."""
    s = v.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[()$\s]|us|usd", "", s, flags=re.I)
    if s.startswith("-"):
        negative, s = True, s[1:]
    # La coma solo es válida como separador de MILES en grupos de 3 (1,234.56). "1,2,3.4" no es un
    # número: aceptarlo como 123.4 sería inventar un dato financiero.
    if not re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?", s):
        return None
    s = s.replace(",", "")
    try:
        d = Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return -d if negative else d


def infer_category(merchant: str, description: str) -> str:
    text = strip_accents(f"{merchant} {description}").lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in text for w in words):
            return cat
    return "otros"


# ------------------------------------------------------------------------------------------------
# Etapas
# ------------------------------------------------------------------------------------------------
def extract() -> tuple[list[dict], list[dict], dict]:
    rows, quarantine = [], []
    with open(RAW_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        total = 0
        for raw in reader:
            total += 1
            if len(raw) != len(header):
                # Fila corrupta: no se puede saber qué valor va en qué columna. Se guarda ÍNTEGRA.
                quarantine.append({"line": reader.line_num, "stage": "EXTRACT", "reason": "wrong_column_count",
                                   "raw": ",".join(raw)})
                continue
            rows.append({**dict(zip(header, raw)), "_line": reader.line_num})
    return rows, quarantine, {"read": total, "passed": len(rows), "quarantined": len(quarantine)}


def clean(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    quarantine, cleaned = [], []
    imputed = Counter()

    def reject(r: dict, reason: str) -> None:
        quarantine.append({"line": r["_line"], "stage": "CLEAN", "reason": reason,
                           "raw": ",".join(str(r.get(k, "")) for k in
                                           ("transaction_id", "customer_id", "account_number", "date", "amount", "currency",
                                            "merchant", "description", "category"))})

    # ---- 1) fila por fila: normalizar, validar, imputar ----
    for r in rows:
        v = {k: norm_text(val) for k, val in r.items() if not k.startswith("_")}
        # Campos CRÍTICOS: sin ellos la fila no es una transacción utilizable => cuarentena.
        # (No se inventa un cliente, un monto ni una fecha: imputar aquí sería falsear datos financieros.)
        missing = next((f for f in ("transaction_id", "customer_id", "account_number", "date", "amount") if not v.get(f)), None)
        if missing:
            reject(r, f"missing_critical:{missing}")
            continue

        d = parse_date(v["date"])
        if d is None:
            reject(r, "invalid_date"); continue
        if not (MIN_DATE <= d <= date.today()):
            reject(r, "date_out_of_range"); continue

        amount = parse_amount(v["amount"])
        if amount is None:
            reject(r, "invalid_amount"); continue
        # Negativos: en este origen no se distingue un reverso de un error de captura; sin esa
        # información, contabilizarlos como gasto (o como ingreso) sería adivinar => cuarentena.
        if amount < 0:
            reject(r, "negative_amount"); continue
        if amount == 0:
            reject(r, "zero_amount"); continue
        if amount > AMOUNT_CAP:
            reject(r, "amount_exceeds_cap"); continue

        currency = None
        if v.get("currency"):
            currency = CURRENCY_MAP.get(strip_accents(v["currency"]).lower())
            if currency is None:
                reject(r, "unknown_currency"); continue

        # Campos NO críticos: se imputan con un valor neutro y explícito (documentado en el reporte).
        merchant = v.get("merchant")
        if merchant is None:
            merchant = "DESCONOCIDO"; imputed["merchant -> 'DESCONOCIDO'"] += 1
        else:
            merchant = merchant.title() if merchant.isupper() or merchant.islower() else merchant
        description = v.get("description")
        if description is None:
            description = ""; imputed["description -> ''"] += 1

        cleaned.append({
            "transaction_id": v["transaction_id"].upper(), "customer_id": v["customer_id"].upper(),
            "account_number": v["account_number"].upper(), "date": d, "amount": amount, "currency": currency,
            "merchant": merchant, "description": description,
            "category": (v.get("category") or "").lower() or None, "_line": r["_line"],
        })

    # ---- 2) imputación de la divisa: la moda de su propia cuenta ----
    # Una cuenta opera casi siempre en una sola divisa; si la divisa falta, la más frecuente de la cuenta
    # es una estimación razonable Y auditable. Si la cuenta no tiene ninguna otra divisa conocida => cuarentena.
    modal: dict[str, str] = {}
    by_acc: dict[str, Counter] = defaultdict(Counter)
    for c in cleaned:
        if c["currency"]:
            by_acc[c["account_number"]][c["currency"]] += 1
    for acc, cnt in by_acc.items():
        modal[acc] = cnt.most_common(1)[0][0]
    final = []
    for c in cleaned:
        if c["currency"] is None:
            if c["account_number"] in modal:
                c["currency"] = modal[c["account_number"]]; imputed["currency -> moda de la cuenta"] += 1
            else:
                reject({"_line": c["_line"], **{k: c.get(k) for k in ("transaction_id", "customer_id", "account_number", "amount")},
                        "date": c["date"]}, "currency_not_imputable")
                continue
        final.append(c)

    # ---- 3) deduplicación ----
    # Duplicado EXACTO = mismo transaction_id. Duplicado por CLAVE DE NEGOCIO = mismo cliente, cuenta, día,
    # monto, divisa y comercio con OTRO id. Se conserva la primera aparición; las demás van a cuarentena
    # (no se borran: se puede auditar qué se descartó y por qué).
    # LIMITACIÓN CONOCIDA: dos compras legítimas idénticas el mismo día en el mismo comercio se tratarían
    # como duplicado; es el precio de la regla pedida y se declara en el reporte.
    seen_ids: set[str] = set()
    seen_keys: set[tuple] = set()
    deduped = []
    for c in sorted(final, key=lambda x: x["_line"]):
        key = (c["customer_id"], c["account_number"], c["date"], c["amount"], c["currency"], c["merchant"].lower())
        if c["transaction_id"] in seen_ids:
            reject_row = "exact_duplicate"
        elif key in seen_keys:
            reject_row = "duplicate_business_key"
        else:
            seen_ids.add(c["transaction_id"]); seen_keys.add(key)
            deduped.append(c)
            continue
        quarantine.append({"line": c["_line"], "stage": "CLEAN", "reason": reject_row,
                           "raw": f"{c['transaction_id']},{c['customer_id']},{c['account_number']},{c['date']},{c['amount']},{c['currency']},{c['merchant']}"})

    reasons = Counter(q["reason"] for q in quarantine)
    return deduped, quarantine, {"in": len(rows), "out": len(deduped), "quarantined": len(quarantine),
                                 "quarantine_by_reason": dict(reasons), "imputations": dict(imputed)}


def transform(rows: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    df = pd.DataFrame(rows).drop(columns=["_line"])
    # Fecha (sin hora en el origen) => medianoche UTC. ISO-8601 con zona: sin ambigüedad de huso horario.
    df["occurred_at"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC")
    df["day"] = df["date"]

    # Categoría: la explícita del origen; si falta, se infiere por palabras clave del comercio/descripción.
    inferred = df["category"].isna()
    df["category"] = df.apply(lambda r: r["category"] or infer_category(r["merchant"], r["description"]), axis=1)

    # Bandera de monto atípico POR CLIENTE con z-score modificado (mediana y MAD). POR QUÉ MAD y no la
    # desviación estándar: un solo outlier infla la media y la desviación estándar y se "esconde" a sí
    # mismo; la mediana y la MAD son robustas. 3.5 es el umbral estándar (Iglewicz-Hoaglin).
    # Se aplica sobre el LOGARITMO del monto: los montos de gasto siguen una distribución muy sesgada
    # (log-normal); sobre el monto crudo el MAD marcaría como "atípica" la cola natural (medido: 4.5%
    # de las filas); en escala logarítmica la distribución es simétrica y solo se marca lo realmente raro.
    df["_a"] = np.log(df["amount"].astype(float))
    grp = df.groupby("customer_id")["_a"]
    med = grp.transform("median")
    mad = (df["_a"] - med).abs().groupby(df["customer_id"]).transform("median")
    n = grp.transform("count")
    modz = 0.6745 * (df["_a"] - med) / mad.where(mad > 0)
    df["flag_atypical_amount"] = ((modz > MAD_THRESHOLD) & (n >= MIN_HISTORY_FOR_ATYPICAL)).fillna(False)

    # Agregados por cliente y día: total, promedio, conteo, desviación (lo que consume la IA y el análisis).
    df["_amount_f"] = df["amount"].astype(float)
    agg = (df.groupby(["customer_id", "day"])["_amount_f"]
             .agg(tx_count="count", total_amount="sum", avg_amount="mean", std_amount="std", max_amount="max")
             .reset_index())
    agg["std_amount"] = agg["std_amount"].fillna(0.0)  # un solo dato no tiene dispersión (no "desconocida")
    df = df.merge(agg[["customer_id", "day", "tx_count"]], on=["customer_id", "day"])
    df["flag_high_frequency_day"] = df["tx_count"] >= HIGH_FREQUENCY_DAY
    df["year_month"] = df["occurred_at"].dt.strftime("%Y-%m")
    for c in ("total_amount", "avg_amount", "std_amount", "max_amount"):
        agg[c] = agg[c].round(2)
    df = df.drop(columns=["_a", "_amount_f", "tx_count", "date"])

    info = {"rows": len(df), "customer_days": len(agg), "category_inferred": int(inferred.sum()),
            "flag_atypical_amount": int(df["flag_atypical_amount"].sum()),
            "flag_high_frequency_day": int(df["flag_high_frequency_day"].sum()),
            "categories": df["category"].value_counts().to_dict()}
    return df, agg, info


def load(df: pd.DataFrame, agg: pd.DataFrame) -> dict:
    # ---- Parquet ----
    # POR QUÉ Parquet y no CSV: (1) COLUMNAR: una consulta analítica ("gasto por categoría") lee solo las
    # columnas que necesita, no todas; (2) esquema con TIPOS (decimal exacto, timestamp con zona), sin
    # reinterpretar texto; (3) compresión por columna (zstd), típicamente 5-10x menor que CSV;
    # (4) particionado por mes: una consulta de un mes lee solo esa carpeta; (5) lo leen directamente
    # pandas, Spark, DuckDB y los pipelines de entrenamiento del modelo de IA.
    if PROCESSED_DIR.exists():
        shutil.rmtree(PROCESSED_DIR)  # corrida idempotente: mismo resultado si se repite
    schema = pa.schema([
        ("transaction_id", pa.string()), ("customer_id", pa.string()), ("account_number", pa.string()),
        ("occurred_at", pa.timestamp("us", tz="UTC")), ("amount", pa.decimal128(18, 2)), ("currency", pa.string()),
        ("merchant", pa.string()), ("description", pa.string()), ("category", pa.string()),
        ("flag_atypical_amount", pa.bool_()), ("flag_high_frequency_day", pa.bool_()), ("year_month", pa.string()),
    ])
    table = pa.Table.from_pandas(df[[f.name for f in schema]], schema=schema, preserve_index=False)
    pq.write_to_dataset(table, root_path=str(PROCESSED_DIR / "transactions"), partition_cols=["year_month"], compression="zstd")
    agg_table = pa.Table.from_pandas(agg, preserve_index=False)
    pq.write_table(agg_table, PROCESSED_DIR / "customer_daily.parquet", compression="zstd")
    parquet_bytes = sum(p.stat().st_size for p in PROCESSED_DIR.rglob("*.parquet"))

    # ---- PostgreSQL (tablas analíticas) ----
    # TRUNCATE + COPY en UNA transacción: o queda la carga completa o queda la anterior; nunca a medias.
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL_PATH.read_text(encoding="utf-8"))
            cur.execute("TRUNCATE analytics_transactions, analytics_customer_daily")
            with cur.copy("COPY analytics_transactions (transaction_id, customer_id, account_number, occurred_at, amount, currency, "
                          "merchant, description, category, flag_atypical_amount, flag_high_frequency_day, etl_run_id) FROM STDIN") as cp:
                for r in df.itertuples(index=False):
                    cp.write_row((r.transaction_id, r.customer_id, r.account_number, r.occurred_at.to_pydatetime(), r.amount,
                                  r.currency, r.merchant, r.description, r.category, bool(r.flag_atypical_amount),
                                  bool(r.flag_high_frequency_day), RUN_ID))
            with cur.copy("COPY analytics_customer_daily (customer_id, day, tx_count, total_amount, avg_amount, std_amount, max_amount, etl_run_id) FROM STDIN") as cp:
                for r in agg.itertuples(index=False):
                    cp.write_row((r.customer_id, r.day, int(r.tx_count), r.total_amount, r.avg_amount, r.std_amount, r.max_amount, RUN_ID))
            cur.execute("SELECT (SELECT count(*) FROM analytics_transactions), (SELECT count(*) FROM analytics_customer_daily)")
            n_tx, n_daily = cur.fetchone()
    raw_bytes = RAW_PATH.stat().st_size
    return {"parquet_files": len(list(PROCESSED_DIR.rglob("*.parquet"))), "parquet_bytes": parquet_bytes, "raw_csv_bytes": raw_bytes,
            "db_analytics_transactions": n_tx, "db_analytics_customer_daily": n_daily}


# ------------------------------------------------------------------------------------------------
def write_quarantine(quarantine: list[dict]) -> Path:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    path = QUARANTINE_DIR / "quarantine.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["line", "stage", "reason", "raw"])
        w.writeheader()
        w.writerows(sorted(quarantine, key=lambda q: q["line"]))
    return path


def render_report(rep: dict) -> str:
    s, c, t, l = rep["stages"]["extract"], rep["stages"]["clean"], rep["stages"]["transform"], rep["stages"]["load"]
    lines = [
        "=" * 72, f"REPORTE ETL  (corrida {RUN_ID})", "=" * 72,
        f"Registros leídos del CSV crudo ........ {rep['read']}",
        f"Registros limpios (tras CLEAN) ........ {c['out']}",
        f"Registros en cuarentena (total) ....... {rep['quarantined_total']}   ({rep['quarantined_total'] / rep['read']:.1%})",
        f"Registros cargados a la BD ............ {l['db_analytics_transactions']}",
        f"Control: leídos = limpios + cuarentena  {rep['read']} = {c['out']} + {rep['quarantined_total']}  ->  {'OK' if rep['balanced'] else 'DESCUADRADO'}",
        "", "Cuarentena por motivo:",
    ]
    lines += [f"  {n:>5}  {reason}" for reason, n in sorted(rep["quarantine_by_reason"].items(), key=lambda x: -x[1])]
    lines += ["", "Imputaciones aplicadas:"] + [f"  {n:>5}  {k}" for k, n in c["imputations"].items()]
    lines += ["", f"Enriquecimiento: {t['customer_days']} agregados cliente-día | {t['category_inferred']} categorías inferidas | "
                  f"{t['flag_atypical_amount']} montos atípicos | {t['flag_high_frequency_day']} filas en días de alta frecuencia",
              f"Parquet: {l['parquet_files']} archivos, {l['parquet_bytes']:,} bytes  (CSV crudo: {l['raw_csv_bytes']:,} bytes)",
              f"Tablas analíticas: analytics_transactions={l['db_analytics_transactions']}  analytics_customer_daily={l['db_analytics_customer_daily']}",
              "", "Duración por etapa:"]
    lines += [f"  {name:<10} {rep['durations_s'][name]:>7.3f} s" for name in ("extract", "clean", "transform", "load")]
    lines += [f"  {'TOTAL':<10} {rep['durations_s']['total']:>7.3f} s", "=" * 72]
    return "\n".join(lines)


def main() -> int:
    if not RAW_PATH.exists():
        log("raw_file_missing_generating", path=str(RAW_PATH))
        import generate_raw_data
        generate_raw_data.main()

    durations: dict[str, float] = {}
    stages: dict[str, dict] = {}
    t_total = time.perf_counter()

    t = time.perf_counter()
    log("stage_started", stage="EXTRACT", source=str(RAW_PATH))
    rows, q_extract, stages["extract"] = extract()
    durations["extract"] = time.perf_counter() - t
    log("stage_finished", stage="EXTRACT", **stages["extract"], seconds=round(durations["extract"], 3))

    t = time.perf_counter()
    log("stage_started", stage="CLEAN", records_in=len(rows))
    cleaned, q_clean, stages["clean"] = clean(rows)
    durations["clean"] = time.perf_counter() - t
    log("stage_finished", stage="CLEAN", records_in=stages["clean"]["in"], records_out=stages["clean"]["out"],
        quarantined=stages["clean"]["quarantined"], by_reason=stages["clean"]["quarantine_by_reason"], seconds=round(durations["clean"], 3))

    t = time.perf_counter()
    log("stage_started", stage="TRANSFORM", records_in=len(cleaned))
    df, agg, stages["transform"] = transform(cleaned)
    durations["transform"] = time.perf_counter() - t
    log("stage_finished", stage="TRANSFORM", records_out=len(df), customer_days=len(agg), seconds=round(durations["transform"], 3))

    t = time.perf_counter()
    log("stage_started", stage="LOAD", records_in=len(df))
    stages["load"] = load(df, agg)
    durations["load"] = time.perf_counter() - t
    log("stage_finished", stage="LOAD", **stages["load"], seconds=round(durations["load"], 3))
    durations["total"] = time.perf_counter() - t_total

    quarantine = q_extract + q_clean
    qpath = write_quarantine(quarantine)
    by_reason = Counter(q["reason"] for q in quarantine)
    report = {
        "run_id": RUN_ID, "finished_at": datetime.now(timezone.utc).isoformat(), "read": stages["extract"]["read"],
        "quarantined_total": len(quarantine), "quarantine_by_reason": dict(by_reason),
        "balanced": stages["extract"]["read"] == len(cleaned) + len(quarantine),
        "stages": stages, "durations_s": {k: round(v, 3) for k, v in durations.items()},
    }
    text = render_report(report)
    print("\n" + text)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "etl_report.txt").write_text(text + "\n", encoding="utf-8")
    (EVIDENCE_DIR / "etl_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    shutil.copy(qpath, EVIDENCE_DIR / "quarantine.csv")
    log("etl_finished", read=report["read"], clean=len(cleaned), quarantined=len(quarantine), balanced=report["balanced"])
    return 0 if report["balanced"] else 1


if __name__ == "__main__":
    sys.exit(main())
