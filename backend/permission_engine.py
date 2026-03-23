import os
import json
from groq import AsyncGroq
from .models import PermissionRule

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

DEFAULT_CATEGORIES = [
    {"id": "pricing", "label": "Pricing & costs"},
    {"id": "timeline", "label": "Timelines & deadlines"},
    {"id": "technical", "label": "Technical details"},
    {"id": "personal", "label": "Personal information"},
    {"id": "commitments", "label": "Commitments & agreements"},
    {"id": "general", "label": "General conversation"},
]


class PermissionEngine:
    def __init__(self):
        self.session_rules: dict[str, list] = {}
        self.session_categories: dict[str, dict] = {}
        self.session_roles: dict[str, str] = {}
        self.session_confidence_thresholds: dict[str, float] = {}

    def load_rules(self, session_id: str, rules: list[PermissionRule]):
        self.session_rules[session_id] = [r.model_dump() for r in rules]

    def set_role(self, session_id: str, role: str):
        self.session_roles[session_id] = role

    def set_confidence_threshold(self, session_id: str, threshold: float):
        self.session_confidence_thresholds[session_id] = threshold

    def get_category_config(self, session_id: str) -> dict:
        return self.session_categories.get(session_id, {
            cat["id"]: cat["id"] == "general" for cat in DEFAULT_CATEGORIES
        })

    async def parse_natural_language_rule(self, text: str, session_id: str) -> dict:
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": """Parse natural language permission rules. Output ONLY valid JSON, no markdown:
{"allowed": true/false, "topics": ["topic"], "description": "short description", "original": "original text"}
Topics: pricing, timeline, technical, personal, commitments, general"""
                },
                {"role": "user", "content": f"Parse: {text}"}
            ]
        )
        try:
            raw = response.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
            parsed = json.loads(raw)
            if session_id not in self.session_rules:
                self.session_rules[session_id] = []
            self.session_rules[session_id].append(parsed)
            return parsed
        except Exception:
            return {"allowed": False, "topics": [], "description": text, "original": text}

    async def check(self, session_id: str, transcript: str, auth0_client=None) -> dict:
        """
        Multi-layer permission check:
        Layer 1: Custom NL rules (highest priority)
        Layer 2: FGA role-based permissions
        Layer 3: Category toggles
        + Confidence threshold applied at every layer
        """
        rules = self.session_rules.get(session_id, [])
        categories = self.get_category_config(session_id)
        role = self.session_roles.get(session_id, "custom")
        confidence_threshold = self.session_confidence_thresholds.get(session_id, 0.7)

        # Classify topic
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=150,
            messages=[
                {
                    "role": "system",
                    "content": """Classify meeting transcript. Output ONLY valid JSON, no markdown:
{"topic": "one of: pricing/timeline/technical/personal/commitments/general", "confidence": 0.0-1.0, "reason": "brief"}"""
                },
                {"role": "user", "content": f'Classify: "{transcript}"'}
            ]
        )

        try:
            raw = response.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
            classification = json.loads(raw)
        except Exception:
            classification = {"allowed": False, "topic": "unknown", "confidence": 0.0, "reason": "Classification error — requiring approval", "matched_rule": None, "fga_role": role, "layer": "error", "confidence_threshold": confidence_threshold, "threshold_passed": False}
            return classification

        topic = classification.get("topic", "general")
        confidence = float(classification.get("confidence", 0.5))
        threshold_passed = confidence >= confidence_threshold

        # Layer 1: Custom NL rules
        for rule in rules:
            rule_topics = [t.lower() for t in rule.get("topics", [])]
            if topic.lower() in rule_topics or "all" in rule_topics:
                matched_rule = rule.get("description", "custom rule")
                rule_allowed = rule.get("allowed", False)
                return {
                    "allowed": rule_allowed and threshold_passed,
                    "topic": topic, "confidence": confidence,
                    "reason": f"Custom rule: {matched_rule}",
                    "matched_rule": matched_rule,
                    "fga_role": role, "layer": "custom_rule",
                    "confidence_threshold": confidence_threshold,
                    "threshold_passed": threshold_passed
                }

        # Layer 2: FGA role
        if role != "custom" and auth0_client:
            fga_result = auth0_client.fga_check(role, topic)
            return {
                "allowed": fga_result["allowed"] and threshold_passed,
                "topic": topic, "confidence": confidence,
                "reason": fga_result["reason"],
                "matched_rule": f"FGA:{role}" if fga_result["allowed"] else None,
                "fga_role": role, "fga_label": fga_result.get("role_label", role),
                "layer": "fga",
                "confidence_threshold": confidence_threshold,
                "threshold_passed": threshold_passed
            }

        # Layer 3: Category toggles
        category_allowed = categories.get(topic.lower(), False)
        return {
            "allowed": category_allowed and threshold_passed,
            "topic": topic, "confidence": confidence,
            "reason": f"Category '{topic}' is {'enabled' if category_allowed else 'disabled'}",
            "matched_rule": None, "fga_role": role, "layer": "category",
            "confidence_threshold": confidence_threshold,
            "threshold_passed": threshold_passed
        }
