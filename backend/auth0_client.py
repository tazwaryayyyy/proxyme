import os
import httpx
import json
import re
from typing import Optional


FGA_ROLE_PERMISSIONS = {
    "sales_engineer": {
        "allowed_topics": ["general", "technical", "pricing", "timeline"],
        "label": "Sales Engineer",
        "description": "Can auto-approve technical and pricing discussions"
    },
    "junior_ae": {
        "allowed_topics": ["general"],
        "label": "Junior AE",
        "description": "Can only auto-approve general conversation"
    },
    "executive": {
        "allowed_topics": ["general", "technical", "pricing", "timeline", "commitments", "personal"],
        "label": "Executive",
        "description": "Full auto-approval authority"
    },
    "legal": {
        "allowed_topics": ["general", "technical"],
        "label": "Legal / Compliance",
        "description": "Auto-approves general and technical only"
    },
    "custom": {
        "allowed_topics": [],
        "label": "Custom",
        "description": "Defined by your category toggles"
    }
}


class Auth0Client:
    def __init__(self):
        self.domain = os.getenv("AUTH0_DOMAIN", "")
        self.client_id = os.getenv("AUTH0_CLIENT_ID", "")
        self.client_secret = os.getenv("AUTH0_CLIENT_SECRET", "")
        self.audience = os.getenv("AUTH0_AUDIENCE", "")
        self._management_token: Optional[str] = None

    def fga_check(self, role: str, topic: str) -> dict:
        role_config = FGA_ROLE_PERMISSIONS.get(role, FGA_ROLE_PERMISSIONS["custom"])
        allowed = topic.lower() in [t.lower() for t in role_config["allowed_topics"]]
        return {
            "allowed": allowed,
            "role": role,
            "role_label": role_config["label"],
            "topic": topic,
            "allowed_topics": role_config["allowed_topics"],
            "reason": f"Role '{role_config['label']}' {'permits' if allowed else 'does not permit'} auto-approval of '{topic}' topics"
        }

    def get_fga_roles(self) -> dict:
        return FGA_ROLE_PERMISSIONS

    async def get_management_token(self) -> str:
        """Always targets the Auth0 Management API."""
        if self._management_token:
            return self._management_token
        
        # Use the hardcoded Management API audience
        mgmt_audience = f"https://{self.domain}/api/v2/" 
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/oauth/token",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "audience": mgmt_audience,
                    },
                    timeout=10
                )
                data = response.json()
                self._management_token = data.get("access_token", "")
                return self._management_token
        except Exception:
            return ""

    async def get_scoped_token(self, action: str, scope: str) -> dict:
        """Targets either Management API or Custom API based on scope."""
        # Determine which API to talk to
        is_mgmt = "users" in scope or "roles" in scope
        target_audience = f"https://{self.domain}/api/v2/" if is_mgmt else self.audience

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/oauth/token",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "audience": target_audience,
                        "scope": scope,
                    },
                    timeout=10
                )
                data = response.json()
                return {
                    "access_token": data.get("access_token", ""),
                    "scope": data.get("scope", scope),
                    "action": action,
                    "vault_sourced": True,
                    "expires_in": data.get("expires_in", 86400)
                }
        except Exception as e:
            return {
                "access_token": "",
                "scope": scope,
                "action": action,
                "vault_sourced": False,
                "error": str(e)
            }

    async def initiate_ciba_standard(self, login_hint: str, topic: str, proposed_response: str) -> dict:
        """
        CIBA with Rich Authorization Requests (RAR).
        Sends full context via authorization_details, truncating long responses to 255 chars.
        """
        # Truncate proposed_response to 250 chars (to be safe with 255 limit)
        truncated_response = proposed_response[:250] + ("..." if len(proposed_response) > 250 else "")
        
        # Build RAR payload with truncated response
        rar_payload = {
            "type": "proxyme_approval",
            "actions": ["approve", "deny"],
            "locations": ["https://proxyme.app"],
            "data": {
                "topic": topic[:50],  # also truncate topic if needed (max 50)
                "suggested_response": truncated_response
            }
        }
        auth_details = json.dumps([rar_payload])
        binding_message = f"Proxy Me: {topic[:30]}"  # short for notification

        if not self.domain or not self.client_id:
            return {
                "auth_req_id": f"demo_ciba_{os.urandom(6).hex()}",
                "expires_in": 300,
                "interval": 5,
                "demo_mode": True,
            }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/bc-authorize",
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "login_hint": login_hint,
                        "scope": "openid profile email",
                        "audience": self.audience,
                        "authorization_details": auth_details,
                        "binding_message": binding_message,  # <-- added
                    },
                    timeout=15
                )
                if response.status_code == 200:
                    data = response.json()
                    data["demo_mode"] = False
                    return data
                return {
                    "auth_req_id": f"demo_ciba_{os.urandom(6).hex()}",
                    "expires_in": 300,
                    "interval": 5,
                    "demo_mode": True,
                    "error": response.text,
                }
        except Exception as e:
            return {
                "auth_req_id": f"demo_ciba_{os.urandom(6).hex()}",
                "expires_in": 300,
                "interval": 5,
                "demo_mode": True,
                "error": str(e),
            }

    async def poll_ciba(self, auth_req_id: str) -> dict:
        if not self.domain or auth_req_id.startswith("demo_ciba_"):
            return {"status": "pending", "demo_mode": True}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/oauth/token",
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "urn:openid:params:grant-type:ciba",
                        "auth_req_id": auth_req_id,
                    },
                    timeout=10
                )
                data = response.json()
                if "access_token" in data:
                    return {"status": "approved", "token": data["access_token"]}
                elif data.get("error") == "authorization_pending":
                    return {"status": "pending"}
                elif data.get("error") == "access_denied":
                    return {"status": "denied"}
                return {"status": "error", "details": data}
        except Exception as e:
            return {"status": "error", "details": str(e)}
