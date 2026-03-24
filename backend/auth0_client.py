import os
import httpx
import json
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
        if self._management_token:
            return self._management_token
        if not self.domain or not self.client_id:
            return ""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/oauth/token",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "audience": f"https://{self.domain}/api/v2/",
                    },
                    timeout=10
                )
                data = response.json()
                self._management_token = data.get("access_token", "")
                return self._management_token
        except Exception:
            return ""

    async def get_scoped_token(self, action: str, scope: str) -> dict:
        mgmt_token = await self.get_management_token()
        if not mgmt_token:
            return {
                "access_token": f"demo_{action}_{os.urandom(4).hex()}",
                "scope": scope,
                "action": action,
                "vault_sourced": False,
                "note": "Demo mode — configure AUTH0_DOMAIN for live Token Vault"
            }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/oauth/token",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "audience": self.audience or f"https://{self.domain}/api/v2/",
                        "scope": scope,
                    },
                    timeout=10
                )
                data = response.json()
                return {
                    "access_token": data.get("access_token", ""),
                    "scope": scope,
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

    async def initiate_ciba_with_rar(self, user_id: str, topic: str, proposed_response: str, binding_message: str) -> dict:
        """
        CIBA with Rich Authorization Requests (RAR).
        - login_hint uses iss_sub JSON format required by Auth0
        - request parameter removed (not supported on standard plans)
        - authorization_details carries the RAR payload
        """
        short_binding = f"ProxyMe:{topic[:12]}"

        authorization_details = json.dumps([{
            "type": "proxy_me_approval",
            "topic": topic,
            "proposed_response": proposed_response[:80],
            "action": "speak_on_behalf"
        }])

        login_hint = json.dumps({
            "format": "iss_sub",
            "iss": f"https://{self.domain}/",
            "sub": user_id
        })

        if not self.domain or not self.client_id:
            return {
                "auth_req_id": f"demo_ciba_{os.urandom(6).hex()}",
                "expires_in": 300,
                "interval": 5,
                "demo_mode": True,
                "rar_details": {"topic": topic, "proposed_response": proposed_response[:100]}
            }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://{self.domain}/bc-authorize",
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "login_hint": login_hint,
                        "scope": "openid",
                        "binding_message": short_binding,
                        "authorization_details": authorization_details,
                    },
                    timeout=15
                )
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "auth_req_id": data.get("auth_req_id"),
                        "expires_in": data.get("expires_in", 300),
                        "interval": data.get("interval", 5),
                        "demo_mode": False,
                        "rar_details": {"topic": topic, "proposed_response": proposed_response[:100]}
                    }
                return {
                    "auth_req_id": f"demo_ciba_{os.urandom(6).hex()}",
                    "expires_in": 300,
                    "interval": 5,
                    "demo_mode": True,
                    "error": response.text,
                    "rar_details": {"topic": topic, "proposed_response": proposed_response[:100]}
                }
        except Exception as e:
            return {
                "auth_req_id": f"demo_ciba_{os.urandom(6).hex()}",
                "expires_in": 300,
                "interval": 5,
                "demo_mode": True,
                "error": str(e),
                "rar_details": {"topic": topic, "proposed_response": proposed_response[:100]}
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
