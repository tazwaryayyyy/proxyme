import os
import json
import asyncio
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
    """Set FGA role for this session."""
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
    """Set confidence threshold for auto-approval."""
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
    """Return all available FGA roles."""
    return auth0_client.get_fga_roles()


# ─── Audit ──────────────────────────────────────────────────────────────────

@app.get("/audit/{session_id}", response_class=HTMLResponse)
async def audit_page(request: Request, session_id: str):
    """Shareable audit log URL for a session."""
    log = audit_logs.get(session_id, [])
    return templates.TemplateResponse("summary.html", {"request": request, "audit_log": log})


@app.get("/api/audit/{session_id}")
async def get_audit_log(session_id: str):
    """Return full audit log for a session as JSON."""
    log = audit_logs.get(session_id, [])
    return {
        "session_id": session_id,
        "total_events": len(log),
        "log": log
    }


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
                if approval_id in pending_approvals:
                    approval_results[approval_id] = approved
                    pending_approvals[approval_id].set()

    except WebSocketDisconnect:
        pass


async def process_transcript(websocket: WebSocket, session_id: str, transcript: str):
    """Process a transcript chunk in a background task."""
    if session_id not in active_sessions:
        return
    try:
        # Fetch a per-action scoped token from Token Vault for classification
        classify_token = await auth0_client.get_scoped_token(
            action="classify_transcript",
            scope="read:transcripts"
        )

        check = await permission_engine.check(session_id, transcript, auth0_client)

        # Log the classification event
        audit_event = {
            "type": "classify",
            "transcript": transcript[:100],
            "topic": check.get("topic"),
            "allowed": check.get("allowed"),
            "layer": check.get("layer"),
            "fga_role": check.get("fga_role"),
            "confidence": check.get("confidence"),
            "token_action": classify_token.get("action"),
            "token_scope": classify_token.get("scope"),
            "vault_sourced": classify_token.get("vault_sourced", False),
        }

        if check["allowed"]:
            # Fetch per-action token for response generation
            gen_token = await auth0_client.get_scoped_token(
                action="generate_response",
                scope="write:suggestions"
            )

            response = await groq_agent.generate_response(
                transcript,
                active_sessions[session_id].get("config", {}),
                check.get("matched_rule")
            )

            audit_event.update({"response_generated": True, "gen_token_scope": gen_token.get("scope")})
            audit_logs[session_id].append(audit_event)

            async with active_sessions[session_id]["ws_lock"]:
                await websocket.send_json({
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

        else:
            approval_id = os.urandom(6).hex()
            pending_approvals[approval_id] = asyncio.Event()

            preview = await groq_agent.generate_response(
                transcript,
                active_sessions[session_id].get("config", {}),
                None
            )

            # Initiate CIBA with RAR
            ciba_result = await auth0_client.initiate_ciba_with_rar(
                user_id=active_sessions[session_id].get("config", {}).get("user_id", "demo_user"),
                topic=check.get("topic", "sensitive"),
                proposed_response=preview,
                binding_message=f"ProxyMe approval needed"
            )

            audit_event.update({
                "ciba_initiated": True,
                "ciba_demo_mode": ciba_result.get("demo_mode", True),
                "rar_topic": ciba_result.get("rar_details", {}).get("topic"),
                "approval_id": approval_id,
            })
            audit_logs[session_id].append(audit_event)

            async with active_sessions[session_id]["ws_lock"]:
                await websocket.send_json({
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
                "rar_details": ciba_result.get("rar_details", {}),
            })

            # Wait for approval
            try:
                await asyncio.wait_for(pending_approvals[approval_id].wait(), timeout=30)
                approved = approval_results.get(approval_id, False)

                audit_logs[session_id].append({
                    "type": "approval_decision",
                    "approval_id": approval_id,
                    "approved": approved,
                    "topic": check.get("topic"),
                })

                if approved:
                    async with active_sessions[session_id]["ws_lock"]:
                        await websocket.send_json({
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
                    async with active_sessions[session_id]["ws_lock"]:
                        await websocket.send_json({
                            "type": "denied",
                        "approval_id": approval_id,
                        "message": "You chose to handle this yourself.",
                    })
            except asyncio.TimeoutError:
                async with active_sessions[session_id]["ws_lock"]:
                    await websocket.send_json({
                        "type": "timeout",
                    "approval_id": approval_id,
                    "message": "Approval timed out — handle this one yourself.",
                })
            finally:
                pending_approvals.pop(approval_id, None)
                approval_results.pop(approval_id, None)

    except Exception as e:
        try:
            async with active_sessions[session_id]["ws_lock"]:
                await websocket.send_json({"type": "error", "message": str(e)})
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
    approved = len([e for e in log if e.get('type') == 'approve'])
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


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
