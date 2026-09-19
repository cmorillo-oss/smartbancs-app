"""Motor de reglas: recomendaciones reales a partir de patrones de gasto.

No es un modelo entrenado (el reto permite "un mock avanzado y funcional"), pero sí produce
salidas calculadas a partir de los datos recibidos, no texto fijo. Son funciones PURAS (sin
E/S ni reloj): misma entrada => misma salida, lo que las hace fáciles de probar y de defender.
"""
from collections import Counter, defaultdict
from decimal import Decimal
from statistics import mean, median, pstdev

# Umbrales en un solo sitio, con nombre: cada número es una decisión de negocio defendible.
MIN_HISTORY_FOR_ANOMALY = 5       # con menos datos una desviación estándar no significa nada
ANOMALY_ZSCORE = 2.5              # > 2.5 desviaciones sobre la media => atípico
ANOMALY_VS_MEDIAN = Decimal("3")  # o más de 3x la mediana (robusto ante outliers previos)
RECURRING_MIN_COUNT = 3           # >= 3 pagos al mismo destino => pago recurrente
CONCENTRATION_SHARE = Decimal("0.5")
SAVING_RATE = Decimal("0.10")     # sugerencia prudente: apartar el 10% del gasto medio

KEYWORDS = {
    "alimentacion": ("super", "market", "grocery", "restaurante", "comida"),
    "transporte": ("uber", "taxi", "gasolina", "fuel", "metro"),
    "suscripciones": ("netflix", "spotify", "suscrip", "subscription"),
    "vivienda": ("renta", "alquiler", "rent", "hipoteca"),
    "servicios": ("luz", "agua", "internet", "telefono", "electric"),
}


def categorize(item: dict, dest_counts: Counter) -> str:
    """Categoría explícita > palabras clave en la descripción > recurrencia por destino > 'transferencia'."""
    if item.get("category"):
        return item["category"]
    text = (item.get("description") or "").lower()
    for category, words in KEYWORDS.items():
        if any(w in text for w in words):
            return category
    if item.get("dest_account") and dest_counts[item["dest_account"]] >= RECURRING_MIN_COUNT:
        return "pago_recurrente"
    return "transferencia"


def generate(customer_id: str, history: list[dict]) -> dict:
    """`history` viene ordenado del más reciente al más antiguo; el primero es la transacción que disparó el análisis."""
    amounts = [Decimal(str(h["amount"])) for h in history]
    dest_counts = Counter(h.get("dest_account") for h in history if h.get("dest_account"))
    categories = [categorize(h, dest_counts) for h in history]

    recommendations: list[dict] = []

    # --- 1. Detección de gasto atípico ------------------------------------------------------
    if len(amounts) > MIN_HISTORY_FOR_ANOMALY:
        latest, previous = amounts[0], [float(a) for a in amounts[1:]]
        mu, sigma, med = mean(previous), pstdev(previous), median(previous)
        z = (float(latest) - mu) / sigma if sigma > 0 else 0.0
        if z > ANOMALY_ZSCORE or (med > 0 and latest > ANOMALY_VS_MEDIAN * Decimal(str(med))):
            recommendations.append({
                "type": "atypical_spending", "severity": "warning",
                "title": "Gasto atípico detectado",
                "message": f"La última transferencia ({latest}) supera de forma notable tu patrón habitual "
                           f"(media {mu:.2f}, mediana {med:.2f}). Si no la reconoces, contacta al banco.",
                "evidence": {"latest": str(latest), "mean": round(mu, 2), "median": round(med, 2), "zscore": round(z, 2)},
            })

    # --- 2. Concentración del gasto en un destino ---------------------------------------------
    outflow = sum(amounts, Decimal("0"))
    if outflow > 0 and dest_counts:
        by_dest: dict[str, Decimal] = defaultdict(Decimal)
        for h, a in zip(history, amounts):
            if h.get("dest_account"):
                by_dest[h["dest_account"]] += a
        top_dest, top_amount = max(by_dest.items(), key=lambda kv: kv[1])
        if len(history) >= RECURRING_MIN_COUNT and top_amount / outflow >= CONCENTRATION_SHARE:
            recommendations.append({
                "type": "spending_concentration", "severity": "info",
                "title": "Gasto concentrado en un solo destino",
                "message": f"El {top_amount / outflow:.0%} de tu gasto reciente va a {top_dest}. "
                           "Considera programar este pago para no olvidarlo.",
                "evidence": {"destination": top_dest, "share": round(float(top_amount / outflow), 2)},
            })

    # --- 3. Sugerencia de ahorro --------------------------------------------------------------
    if len(amounts) >= RECURRING_MIN_COUNT:
        avg = (outflow / len(amounts)).quantize(Decimal("0.01"))
        suggested = (avg * SAVING_RATE).quantize(Decimal("0.01"))
        recommendations.append({
            "type": "saving_suggestion", "severity": "info",
            "title": "Sugerencia de ahorro",
            "message": f"Tu transferencia promedio es {avg}. Apartar {suggested} en cada operación (10%) "
                       "te ayudaría a construir un fondo de ahorro.",
            "evidence": {"average_amount": str(avg), "suggested_per_transfer": str(suggested)},
        })

    if not recommendations:
        recommendations.append({
            "type": "insufficient_history", "severity": "info", "title": "Aún estamos conociéndote",
            "message": "Con más movimientos podremos darte recomendaciones personalizadas.", "evidence": {},
        })

    return {
        "customer_id": customer_id,
        "recommendations": recommendations,
        "summary": {
            "transactions_analyzed": len(history),
            "total_outflow": str(outflow),
            "category_breakdown": dict(Counter(categories)),
        },
    }
