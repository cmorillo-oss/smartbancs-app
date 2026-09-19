"""Genera etl/data/raw/raw_transactions.csv con BASURA REALISTA y reproducible (semilla fija).

POR QUÉ generarlo así: los datos limpios no demuestran nada. Este archivo trae, a propósito, cada
tipo de suciedad que aparece en extracciones reales de sistemas heredados, para que el pipeline
tenga que resolverlas (o poner en cuarentena lo irrecuperable). El resumen final imprime cuánta
basura de cada tipo se inyectó, así el reporte del ETL se puede contrastar contra lo sembrado.
"""
import csv
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

RAW_PATH = Path(__file__).parent / "data" / "raw" / "raw_transactions.csv"
SEED = 20240115
HEADER = ["transaction_id", "customer_id", "account_number", "date", "amount", "currency", "merchant", "description", "category"]

MERCHANTS = {  # comercio -> (categoría "verdadera", descripción típica)
    "Supermercado Central": ("alimentacion", "compra semanal supermercado"),
    "Restaurante El Fogon": ("alimentacion", "almuerzo restaurante"),
    "Uber": ("transporte", "viaje uber"),
    "Estacion Gasolina Sol": ("transporte", "gasolina"),
    "Netflix": ("suscripciones", "suscripcion mensual netflix"),
    "Spotify": ("suscripciones", "suscripcion spotify"),
    "Inmobiliaria Norte": ("vivienda", "renta apartamento"),
    "Empresa de Luz": ("servicios", "factura luz"),
    "Internet Hogar": ("servicios", "factura internet"),
    "Farmacia Salud": ("salud", "medicinas"),
    "Tienda Online XYZ": ("compras", "compra online"),
}
CURRENCY_VARIANTS = {
    "USD": ["USD", "usd", "Dólares", "US$", " usd ", "Usd"],
    "EUR": ["EUR", "eur", "Euros"],
    "COP": ["COP", "cop", "Pesos"],
}


def fmt_date(d: date, rng: random.Random) -> str:
    """Tres formatos distintos, como llegan de tres sistemas de origen."""
    kind = rng.choice(["iso", "dmy", "mdy_text"])
    if kind == "iso":
        return d.strftime("%Y-%m-%d")            # 2024-01-15
    if kind == "dmy":
        return d.strftime("%d/%m/%Y")            # 15/01/2024
    return d.strftime("%b %d, %Y")               # Jan 15, 2024


def fmt_amount(a: float, rng: random.Random) -> str:
    kind = rng.choices(["plain", "comma", "dollar", "dollar_comma"], weights=[40, 25, 20, 15])[0]
    if kind == "plain":
        return f"{a:.2f}"
    if kind == "comma":
        return f"{a:,.2f}"                        # 1,234.56  (la coma obliga a entrecomillar en el CSV)
    if kind == "dollar":
        return f"${a:.2f}"                        # $500.00
    return f"${a:,.2f}"


def messy(text: str, rng: random.Random) -> str:
    """Espacios y mayúsculas inconsistentes."""
    r = rng.random()
    if r < 0.08:
        return f"  {text} "
    if r < 0.14:
        return text.upper()
    if r < 0.20:
        return text.lower()
    return text


def main() -> None:
    rng = random.Random(SEED)
    injected: Counter = Counter()
    customers = []
    for i in range(1, 41):
        n_acc = 2 if i % 5 == 0 else 1
        accs = [f"ACC-E{i:04d}{chr(65 + k)}" for k in range(n_acc)]
        # cada cliente tiene su escala de gasto: hace que "atípico" tenga sentido POR CLIENTE
        customers.append({"id": f"CUST-E{i:04d}", "accounts": accs, "scale": rng.choice([20, 40, 80, 150]),
                          "currency": rng.choices(["USD", "EUR", "COP"], weights=[80, 12, 8])[0]})

    start = date(2024, 1, 1)
    lines: list[list[str]] = []
    raw_text_rows: list[str] = []  # filas corruptas, escritas a mano
    tx_counter = 0
    business_rows: list[list[str]] = []

    for _ in range(4200):
        c = rng.choice(customers)
        acc = rng.choice(c["accounts"])
        merchant = rng.choice(list(MERCHANTS))
        cat, desc = MERCHANTS[merchant]
        d = start + timedelta(days=rng.randint(0, 89))
        amount = max(1.0, round(rng.lognormvariate(0, 0.6) * c["scale"], 2))
        if rng.random() < 0.012:
            amount = round(amount * rng.uniform(15, 40), 2); injected["gasto atípico (legítimo, se conserva)"] += 1
        tx_counter += 1
        row = {
            "transaction_id": f"TX-{tx_counter:06d}", "customer_id": c["id"], "account_number": acc,
            "date": fmt_date(d, rng), "amount": fmt_amount(amount, rng),
            "currency": rng.choice(CURRENCY_VARIANTS[c["currency"]]),
            "merchant": merchant, "description": desc, "category": cat,
        }

        # --- suciedad de formato ---
        for f in ("customer_id", "account_number", "merchant"):
            row[f] = messy(row[f], rng)

        # --- nulos ---
        if rng.random() < 0.010: row["customer_id"] = ""; injected["nulo en campo crítico: customer_id"] += 1
        elif rng.random() < 0.010: row["amount"] = ""; injected["nulo en campo crítico: amount"] += 1
        elif rng.random() < 0.010: row["date"] = ""; injected["nulo en campo crítico: date"] += 1
        if rng.random() < 0.030: row["currency"] = ""; injected["nulo: currency (imputable)"] += 1
        if rng.random() < 0.040: row["merchant"] = ""; injected["nulo no crítico: merchant"] += 1
        if rng.random() < 0.200: row["category"] = ""; injected["nulo no crítico: category"] += 1
        if rng.random() < 0.100: row["description"] = ""; injected["nulo no crítico: description"] += 1
        if rng.random() < 0.010: row["merchant"] = "N/A"; injected["marcador de nulo textual (N/A)"] += 1

        # --- valores irrecuperables o sospechosos ---
        r = rng.random()
        if r < 0.015:
            row["amount"] = f"-{amount:.2f}"; injected["monto negativo"] += 1
        elif r < 0.020:
            row["amount"] = rng.choice(["$9,999,999.99", "5,000,000.00", "8000000"]); injected["outlier extremo"] += 1
        elif r < 0.025:
            row["amount"] = rng.choice(["abc", "12..5", "1,2,3.4", "USD"]); injected["monto ilegible"] += 1
        r = rng.random()
        if r < 0.006:
            row["date"] = rng.choice(["31/02/2024", "2024-13-45", "N/A", "15-01-24"]); injected["fecha ilegible"] += 1
        if rng.random() < 0.006:
            row["currency"] = rng.choice(["XYZ", "??", "bitcoins"]); injected["divisa desconocida"] += 1

        vals = [row[h] for h in HEADER]
        lines.append(vals)
        if rng.random() < 0.5:
            business_rows.append(vals)

    # --- duplicados exactos (mismo id y mismos campos) ---
    for vals in rng.sample(lines, 90):
        lines.append(list(vals)); injected["duplicado exacto"] += 1
    # --- duplicados por clave de negocio (mismos datos, OTRO transaction_id) ---
    for vals in rng.sample(business_rows, 80):
        tx_counter += 1
        dup = list(vals); dup[0] = f"TX-{tx_counter:06d}"
        lines.append(dup); injected["duplicado por clave de negocio"] += 1

    rng.shuffle(lines)

    # --- filas corruptas: número de columnas incorrecto ---
    corrupt = []
    for _ in range(45):
        base = rng.choice(lines)
        if rng.random() < 0.5:
            corrupt.append(",".join(base[: rng.randint(2, 6)])); injected["fila corrupta: columnas de menos"] += 1
        else:
            corrupt.append(",".join(base) + ",extra,columnas"); injected["fila corrupta: columnas de más"] += 1

    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RAW_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(lines)
    # Las corruptas se insertan como texto crudo en posiciones aleatorias (un csv.writer las "arreglaría").
    content = RAW_PATH.read_text(encoding="utf-8").splitlines()
    for c_line in corrupt:
        content.insert(rng.randint(1, len(content)), c_line)
    RAW_PATH.write_text("\n".join(content) + "\n", encoding="utf-8")

    print(f"Generado {RAW_PATH} con {len(lines) + len(corrupt)} filas de datos (semilla {SEED}).")
    print("Basura sembrada:")
    for k, v in sorted(injected.items()):
        print(f"  {v:>5}  {k}")


if __name__ == "__main__":
    main()
