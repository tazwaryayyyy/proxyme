import os
import json
import asyncio
import datetime
from datetime import timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from urllib.parse import urlparse
import uvicorn

from .permission_engine import PermissionEngine
from .groq_agent import GroqAgent
from .auth0_client import Auth0Client
from .models import PermissionRule, ApprovalRequest, SessionConfig

app = FastAPI(title="Proxy Me", description="AI meeting assistant with Auth0 authorization")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")
templates = Jinja2Templates(directory="frontend/templates")

permission_engine = PermissionEngine()
groq_agent = GroqAgent()
auth0_client = Auth0Client()

active_sessions: dict[str, dict] = {}
pending_approvals: dict[str, asyncio.Event] = {}
approval_results: dict[str, bool] = {}
audit_logs: dict[str, list] = {}


# ─── Pages ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/overlay", response_class=HTMLResponse)
async def overlay(request: Request):
    return templates.TemplateResponse("overlay.html", {"request": request})

@app.get("/summary", response_class=HTMLResponse)
async def summary_page(request: Request):
    return templates.TemplateResponse("summary.html", {"request": request})


# ─── Session ────────────────────────────────────────────────────────────────

@app.post("/api/session/start")
async def start_session(config: SessionConfig):
    session_id = os.urandom(8).hex()
    active_sessions[session_id] = {
        "config": config.model_dump(),
        "transcript": [],
        "responses": [],
        "pending": None,
        "ws_lock": asyncio.Lock(),
        "last_topic": None,
    }
    audit_logs[session_id] = []
    return {"session_id": session_id, "status": "active"}


@app.post("/api/session/{session_id}/rules")
async def update_rules(session_id: str, rules: list[PermissionRule]):
    if session_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    permission_engine.load_rules(session_id, rules)
    return {"status": "rules updated", "count": len(rules)}


@app.post("/api/session/{session_id}/role")
async def set_role(session_id: str, request: Request):
    body = await request.json()
    role = body.get("role", "custom")
    if session_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    permission_engine.set_role(session_id, role)
    fga_result = auth0_client.get_fga_roles().get(role, {})
    return {
        "status": "role set",
        "role": role,
        "label": fga_result.get("label", role),
        "allowed_topics": fga_result.get("allowed_topics", [])
    }


@app.post("/api/session/{session_id}/confidence")
async def set_confidence_threshold(session_id: str, request: Request):
    body = await request.json()
    threshold = float(body.get("threshold", 0.7))
    if session_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    permission_engine.set_confidence_threshold(session_id, threshold)
    return {"status": "threshold set", "threshold": threshold}


@app.post("/api/session/{session_id}/natural-language-rule")
async def add_nl_rule(session_id: str, request: Request):
    body = await request.json()
    text = body.get("text", "")
    if session_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    parsed = await permission_engine.parse_natural_language_rule(text, session_id)
    return {"parsed": parsed, "status": "added"}


@app.get("/api/fga/roles")
async def get_fga_roles():
    return auth0_client.get_fga_roles()


# ─── Audit ──────────────────────────────────────────────────────────────────

@app.get("/audit/{session_id}", response_class=HTMLResponse)
async def audit_page(request: Request, session_id: str):
    log = audit_logs.get(session_id, [])
    return templates.TemplateResponse("summary.html", {"request": request, "audit_log": log})


@app.get("/api/audit/{session_id}")
async def get_audit_log(session_id: str):
    log = audit_logs.get(session_id, [])
    return {"session_id": session_id, "total_events": len(log), "log": log}


# ─── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    if session_id not in active_sessions:
        await websocket.send_json({"type": "error", "message": "Invalid session"})
        await websocket.close()
        return

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "transcript":
                transcript_chunk = data.get("text", "").strip()
                if not transcript_chunk:
                    continue
                active_sessions[session_id]["transcript"].append(transcript_chunk)
                asyncio.create_task(
                    process_transcript(websocket, session_id, transcript_chunk)
                )

            elif msg_type == "approval_response":
                approval_id = data.get("approval_id")
                approved = data.get("approved", False)
                # Resolve the pending event immediately — this is the overlay button path
                if approval_id in pending_approvals:
                    approval_results[approval_id] = approved
                    pending_approvals[approval_id].set()

    except WebSocketDisconnect:
        # Cancel all pending approvals for this session on disconnect
        for approval_id, event in list(pending_approvals.items()):
            approval_results[approval_id] = False
            event.set()


async def safe_send(websocket: WebSocket, lock: asyncio.Lock, payload: dict):
    """Send a JSON message safely under the ws_lock."""
    try:
        async with lock:
            await websocket.send_json(payload)
    except Exception:
        pass


async def poll_ciba_until_resolved(
    auth_req_id: str,
    approval_id: str,
    interval: int,
    timeout: int,
) -> None:
    """
    Polls Auth0 /oauth/token for CIBA result (Guardian phone approval).
    When the user taps Allow/Deny on their phone, this resolves the event
    so the background task doesn't have to wait for the overlay button.
    """
    if auth_req_id.startswith("demo_ciba_"):
        return  # demo mode — nothing to poll

    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval

        if approval_id not in pending_approvals:
            return  # already resolved by overlay button

        result = await auth0_client.poll_ciba(auth_req_id)
        status = result.get("status")

        if status == "approved":
            approval_results[approval_id] = True
            pending_approvals[approval_id].set()
            return
        elif status == "denied":
            approval_results[approval_id] = False
            pending_approvals[approval_id].set()
            return
        elif status == "error":
            return  # give up, let overlay button or timeout handle it


async def process_transcript(websocket: WebSocket, session_id: str, transcript: str):
    """Process a transcript chunk — classify, maybe generate, maybe ask for approval."""
    if session_id not in active_sessions:
        return

    ws_lock = active_sessions[session_id]["ws_lock"]

    try:
        await safe_send(websocket, ws_lock, {
            "type": "flow_ticker",
            "message": "[Token Vault] Fetching read:users..."
        })

        classify_token = await auth0_client.get_scoped_token(
            action="classify_transcript",
            scope="read:users"
        )

        await safe_send(websocket, ws_lock, {
            "type": "flow_ticker",
            "message": "[Permission Engine] Checking FGA / Rules..."
        })

        check = await permission_engine.check(session_id, transcript, auth0_client)

        topic = check.get("topic", "general")
        last_topic = active_sessions[session_id].get("last_topic")
        topic_change = (last_topic is not None and last_topic != topic)
        active_sessions[session_id]["last_topic"] = topic

        analysis_task = asyncio.create_task(groq_agent.analyze_transcript_chunk(transcript))
        
        # Ensure analysis task is cleaned up even if errors occur
        try:
            audit_event = {
                "transcript": transcript,
                "topic": check.get("topic"),
                "allowed": check.get("allowed"),
                "layer": check.get("layer"),
                "fga_role": check.get("fga_role"),
                "confidence": check.get("confidence"),
                "token_action": classify_token.get("action"),
                "token_scope": classify_token.get("scope"),
                "vault_sourced": classify_token.get("vault_sourced", False),
            }

            # ── Auto-approved path ───────────────────────────────────────────────
            if check["allowed"]:
                await safe_send(websocket, ws_lock, {
                    "type": "flow_ticker",
                    "message": f"[FGA Check] Allowed ({check.get('layer')})"
                })
                await safe_send(websocket, ws_lock, {
                    "type": "flow_ticker",
                    "message": "[Token Vault] Fetching update:users..."
                })

                gen_token = await auth0_client.get_scoped_token(
                    action="generate_response",
                    scope="update:users"
                )

                await safe_send(websocket, ws_lock, {
                    "type": "flow_ticker",
                    "message": "[Groq AI] Generating response..."
                })

                response = await groq_agent.generate_response(
                    transcript,
                    active_sessions[session_id].get("config", {}),
                    check.get("matched_rule"),
                    active_sessions[session_id]["transcript"],
                    topic_change=topic_change
                )

                audit_event.update({"response_generated": True, "gen_token_scope": gen_token.get("scope")})
                audit_logs[session_id].append(audit_event)

                analysis_result = await analysis_task
                audit_logs[session_id].append({
                    "type": "analysis",
                    "transcript": transcript,
                    "analysis": analysis_result,
                    "timestamp": datetime.datetime.now(timezone.utc).isoformat()
                })

                await safe_send(websocket, ws_lock, {
                    "type": "suggestion",
                    "text": response,
                    "topic": check.get("topic", "general"),
                    "confidence": check.get("confidence", 0.9),
                    "matched_rule": check.get("matched_rule"),
                    "fga_role": check.get("fga_role"),
                    "fga_label": check.get("fga_label"),
                    "layer": check.get("layer"),
                    "approved": True,
                })

            # ── Approval required path ───────────────────────────────────────────
            else:
                await safe_send(websocket, ws_lock, {
                    "type": "flow_ticker",
                    "message": f"[FGA Check] Denied ({check.get('layer')})"
                })
                await safe_send(websocket, ws_lock, {
                    "type": "flow_ticker",
                    "message": "[Groq AI] Generating preview..."
                })

                approval_id = os.urandom(6).hex()
                pending_approvals[approval_id] = asyncio.Event()

                preview = await groq_agent.generate_response(
                    transcript,
                    active_sessions[session_id].get("config", {}),
                    None,
                    active_sessions[session_id]["transcript"],
                    topic_change=topic_change
                )

                await safe_send(websocket, ws_lock, {
                    "type": "flow_ticker",
                    "message": "[CIBA] Initiating Step-up Auth..."
                })

                user_id = os.getenv("AUTH0_USER_ID", "demo_user")
                login_hint = json.dumps({
                    "format": "iss_sub",
                    "iss": f"https://{auth0_client.domain}/",
                    "sub": user_id
                })

                ciba_result = await auth0_client.initiate_ciba_standard(
                    login_hint=login_hint,
                    topic=check.get("topic", "sensitive"),
                    proposed_response=preview
                )

                audit_event.update({
                    "ciba_initiated": True,
                    "ciba_demo_mode": ciba_result.get("demo_mode", True),
                    "rar_topic": check.get("topic", "sensitive"),
                    "approval_id": approval_id,
                })
                audit_logs[session_id].append(audit_event)

                analysis_result = await analysis_task
                audit_logs[session_id].append({
                    "type": "analysis",
                    "transcript": transcript,
                    "analysis": analysis_result,
                    "timestamp": datetime.datetime.now(timezone.utc).isoformat()
                })

                await safe_send(websocket, ws_lock, {
                    "type": "approval_required",
                    "approval_id": approval_id,
                    "topic": check.get("topic", "sensitive"),
                    "question": transcript,
                    "suggested_response": preview,
                    "reason": check.get("reason", "This topic requires your approval"),
                    "fga_role": check.get("fga_role"),
                    "fga_label": check.get("fga_label"),
                    "layer": check.get("layer"),
                    "ciba_mode": "guardian" if not ciba_result.get("demo_mode") else "overlay",
                    "rar_details": {},
                })

                # Start CIBA polling in background so phone approval also resolves the event
                auth_req_id = ciba_result.get("auth_req_id", "demo_ciba_")
                ciba_poll_task = asyncio.create_task(
                    poll_ciba_until_resolved(
                        auth_req_id=auth_req_id,
                        approval_id=approval_id,
                        interval=ciba_result.get("interval", 5),
                        timeout=ciba_result.get("expires_in", 300),
                    )
                )

                # Wait for EITHER the overlay button OR the Guardian phone tap
                try:
                    await asyncio.wait_for(
                        pending_approvals[approval_id].wait(),
                        timeout=60
                    )
                    approved = approval_results.get(approval_id, False)

                    audit_logs[session_id].append({
                        "type": "approval_decision",
                        "approval_id": approval_id,
                        "approved": approved,
                        "topic": check.get("topic"),
                    })

                    if approved:
                        await safe_send(websocket, ws_lock, {
                            "type": "suggestion",
                            "text": preview,
                            "topic": check.get("topic"),
                            "approved": True,
                            "approval_id": approval_id,
                            "confidence": check.get("confidence"),
                            "matched_rule": "user approved via CIBA",
                            "layer": "ciba_approved",
                        })
                    else:
                        await safe_send(websocket, ws_lock, {
                            "type": "denied",
                            "approval_id": approval_id,
                            "message": "You chose to handle this yourself.",
                        })

                except asyncio.TimeoutError:
                    await safe_send(websocket, ws_lock, {
                        "type": "timeout",
                        "approval_id": approval_id,
                        "message": "Approval timed out — handle this one yourself.",
                    })
                finally:
                    try:
                        ciba_poll_task.cancel()
                        await ciba_poll_task
                    except asyncio.CancelledError:
                        pass
                    pending_approvals.pop(approval_id, None)
                    approval_results.pop(approval_id, None)

        except Exception as e:
            # Ensure analysis task is cancelled on error
            if not analysis_task.done():
                analysis_task.cancel()
            try:
                await analysis_task
            except asyncio.CancelledError:
                pass
            raise

    except Exception as e:
        try:
            await safe_send(websocket, ws_lock, {"type": "error", "message": str(e)})
        except Exception:
            pass


# ─── Misc endpoints ──────────────────────────────────────────────────────────

@app.post("/api/approve/{approval_id}")
async def approve_action(approval_id: str, request: Request):
    body = await request.json()
    approved = body.get("approved", False)
    if approval_id in pending_approvals:
        approval_results[approval_id] = approved
        pending_approvals[approval_id].set()
        return {"status": "processed"}
    raise HTTPException(404, "Approval request not found or expired")


@app.post("/api/session/{session_id}/summary")
async def get_summary(session_id: str, request: Request):
    body = await request.json()
    log = body.get("log", [])
    if not log:
        return {"summary": "No meeting data recorded."}

    log_text = "\n".join([
        f"[{e.get('time','?')}] {e.get('type','?').upper()} — topic: {e.get('topic','?')} — {e.get('text','')}"
        for e in log
    ])
    used = len([e for e in log if e.get('type') == 'use'])
    approved = len([e for e in log if e.get('approved')])
    denied = len([e for e in log if e.get('type') == 'deny'])
    topics = list(set([e.get('topic') for e in log if e.get('topic') and e.get('topic') != '—']))

    from groq import AsyncGroq
    groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
    response = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=400,
        messages=[
            {"role": "system", "content": "Write concise post-meeting summaries in bullet points. Focus on what was discussed, decisions made, and what required human approval."},
            {"role": "user", "content": f"Summarize:\n{log_text}\n\nStats: {used} suggestions used, {approved} approvals, {denied} manual\nTopics: {', '.join(topics) if topics else 'general'}"}
        ]
    )
    return {"summary": response.choices[0].message.content.strip()}


@app.get("/api/session/{session_id}/history")
async def get_history(session_id: str):
    if session_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    return {
        "transcript": active_sessions[session_id].get("transcript", []),
        "audit_log": audit_logs.get(session_id, []),
    }


@app.get("/api/session/{session_id}/role")
async def get_session_role(session_id: str):
    if session_id not in active_sessions:
        raise HTTPException(404, "Session not found")
    role = permission_engine.session_roles.get(session_id, "custom")
    fga_result = auth0_client.get_fga_roles().get(role, {})
    return {
        "role": role,
        "label": fga_result.get("label", role),
        "allowed_topics": fga_result.get("allowed_topics", [])
    }


@app.post("/api/export/{session_id}")
async def export_audit(session_id: str, webhook_url: str = Query(...)):
    log = audit_logs.get(session_id, [])
    if not log:
        raise HTTPException(404, "No data to export")
    
    # Validate URL to prevent SSRF
    parsed = urlparse(webhook_url)
    if parsed.scheme not in ('https',) or not parsed.netloc:
        raise HTTPException(400, "Invalid webhook URL - must be HTTPS")
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(webhook_url, json={"session_id": session_id, "log": log}, timeout=10)
            resp.raise_for_status()
            return {"status": "exported"}
        except Exception as e:
            raise HTTPException(500, f"Export failed: {str(e)}")


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
