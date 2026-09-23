"""uv run --env-file .env python scripts/ping_jev.py -- one tiny paid Jev call to check the route."""

import json
import os

from jev_ultrafast.model import post_json

base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
result = post_json(
    base + "/v1/systemone",
    os.environ["TYPESAFE_API_KEY"],
    {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": "I was charged twice for my order. Please refund me ASAP.",
        "questions": {
            "urgent": {"type": "noul", "instructions": "The message conveys urgency"},
            "topic": {
                "type": "choice",
                "instructions": "What is this message about?",
                "criteria": {"billing": "Payments or refunds", "technical": "Bugs", "other": "Anything else"},
            },
        },
    },
)
print(json.dumps({k: result.get(k) for k in ("model", "answers", "usage")}, indent=2))
