"""Triage Agent - understands the customer message (intent, sentiment, entities, priority)."""
import re

from common.a2a import agent_card, build_agent_app, emit
from common.config import PORTS

NODE = "triage"

CARD = agent_card(
    name="Fashionocracy Triage Agent",
    description="Classifies inbound customer messages: intent, sentiment, entities and routing priority.",
    port=PORTS["triage"], icon="🧠",
    skills=[{
        "id": "classify_message",
        "name": "Classify customer message",
        "description": "Returns intent, sentiment, order ID, customer tier hints and a recommended priority.",
        "tags": ["nlu", "triage", "routing"],
        "examples": ["I received the wrong colour dress, order FSH-101736"],
    }],
)

INTENTS = [
    ("address_change", "Order status", ["change the delivery address", "change my address", "change the address",
                                         "change delivery address", "new address", "deliver it to", "wrong address",
                                         "update my address", "update the address"]),
    ("refund", "Refund", ["refund", "money back"]),
    ("wrong_item", "Wrong item received", ["wrong colour", "wrong color", "wrong item", "different colour",
                                            "different color", "instead", "wrong shade", "not what i ordered", "wrong product"]),
    ("damaged_item", "Damaged or faulty item", ["stain", "damaged", "torn", "broken", "defect", "hole", "ripped", "faulty"]),
    ("size_exchange", "Exchanges", ["too small", "too big", "too tight", "too loose", "size", "doesn't fit", "does not fit", "exchange"]),
    ("delayed_order", "Order status", ["where is", "not arrived", "hasn't arrived", "late", "delayed", "still waiting",
                                       "not received", "tracking", "delivery"]),
    ("return_refund", "Returns", ["return"]),
]
NEGATIVE = ["disappointed", "unacceptable", "angry", "frustrat", "??", "terrible", "worst", "upset", "ridiculous",
            "again", "seriously", "special occasion", "wedding", "event", "urgent", "!!"]


async def handle(skill: str, data: dict, text: str) -> dict:
    msg = (data.get("message") or text or "").strip()
    low = msg.lower()
    await emit({"node": NODE, "state": "working", "detail": "Reading message…"})

    intent_key, intent_label, hits = "general", "General enquiry", []
    for key, label, kws in INTENTS:
        found = [k for k in kws if k in low]
        if found:
            intent_key, intent_label, hits = key, label, found
            break
    order = re.search(r"\bFSH-?\d{5,6}\b", msg, re.I)
    order_id = order.group(0).upper().replace("FSH", "FSH-").replace("--", "-") if order else None
    neg = [w for w in NEGATIVE if w in low]
    score = max(-1.0, round(-0.25 - 0.18 * len(neg), 2)) if neg or intent_key != "general" else 0.1
    sentiment = "negative" if score < -0.5 else "mildly negative" if score < 0 else "neutral"
    urgency = "high" if any(w in low for w in ["wedding", "event", "tomorrow", "urgent", "special occasion"]) else "normal"
    priority = "high" if intent_key in ("wrong_item", "damaged_item") or sentiment == "negative" or urgency == "high" else "normal"
    if intent_key == "delayed_order" and sentiment == "negative":
        priority = "urgent"

    await emit({"node": NODE, "state": "working",
                "detail": f"Intent: {intent_label} · sentiment {score:+.2f}"})
    result = {
        "intent": intent_key, "intent_label": intent_label, "matched_keywords": hits,
        "sentiment": sentiment, "sentiment_score": score, "urgency": urgency,
        "order_id": order_id, "language": "en", "recommended_priority": priority,
        "confidence": 0.94 if hits else 0.55,
    }
    await emit({"node": NODE, "state": "done", "detail": f"{intent_label} · {priority.upper()}"}, pace=0.1)
    return result


app = build_agent_app(CARD, handle)
