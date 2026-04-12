import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from message_bus import get_full_history


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_PATH = BASE_DIR / "frontend" / "index.html"
FRONTEND_DIR = BASE_DIR / "frontend"
AGENT_CHANNELS = ["ceo", "product", "engineer", "marketing", "qa"]

app = FastAPI(title="PitchBot Live Dashboard")
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

_connected_clients: set[WebSocket] = set()
_clients_lock = asyncio.Lock()
_redis_task: asyncio.Task[Any] | None = None
_run_lock = asyncio.Lock()
_main_process: asyncio.subprocess.Process | None = None


async def _broadcast(payload: Dict[str, Any]) -> None:
    async with _clients_lock:
        clients = list(_connected_clients)

    stale_clients: list[WebSocket] = []
    for client in clients:
        try:
            await client.send_json(payload)
        except Exception:
            stale_clients.append(client)

    if stale_clients:
        async with _clients_lock:
            for client in stale_clients:
                _connected_clients.discard(client)


async def _forward_main_output(process: asyncio.subprocess.Process) -> None:
    if process.stdout is None:
        return

    while True:
        line = await process.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            await _broadcast({"kind": "system", "event": "main_output", "line": text})

    code = await process.wait()
    await _broadcast({"kind": "system", "event": "run_finished", "exit_code": code})


async def _start_main_run(startup_idea: str) -> tuple[bool, str]:
    global _main_process

    idea = startup_idea.strip()
    if not idea:
        return False, "Startup idea cannot be empty."

    async with _run_lock:
        if _main_process and _main_process.returncode is None:
            return False, "A run is already in progress."

        _main_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "main.py",
            cwd=str(BASE_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        if _main_process.stdin is not None:
            _main_process.stdin.write((idea + "\n").encode("utf-8"))
            await _main_process.stdin.drain()
            _main_process.stdin.close()

        asyncio.create_task(_forward_main_output(_main_process))

    await _broadcast({"kind": "system", "event": "run_started", "idea": idea})
    return True, "Run started successfully."


async def _redis_subscriber_loop() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    while True:
        pubsub = None
        client = None
        try:
            client = redis.from_url(redis_url, decode_responses=True)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(*AGENT_CHANNELS)

            while True:
                message = await pubsub.get_message(timeout=1.0)
                if not message:
                    await asyncio.sleep(0.05)
                    continue

                if message.get("type") != "message":
                    continue

                raw = message.get("data")
                if not raw:
                    continue

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                await _broadcast({"kind": "message", "message": event})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[server] Redis subscriber error: {exc}; reconnecting...")
            await asyncio.sleep(2)
        finally:
            if pubsub is not None:
                await pubsub.close()
            if client is not None:
                await client.aclose()


@app.on_event("startup")
async def _on_startup() -> None:
    global _redis_task
    _redis_task = asyncio.create_task(_redis_subscriber_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    global _redis_task
    if _redis_task is not None:
        _redis_task.cancel()
        try:
            await _redis_task
        except asyncio.CancelledError:
            pass
        _redis_task = None


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return FRONTEND_PATH.read_text(encoding="utf-8")


@app.websocket("/ws")
async def ws_dashboard(websocket: WebSocket) -> None:
    await websocket.accept()

    async with _clients_lock:
        _connected_clients.add(websocket)

    try:
        history = get_full_history()[-120:]
        await websocket.send_json({"kind": "history", "messages": history})

        run_active = bool(_main_process and _main_process.returncode is None)
        await websocket.send_json({"kind": "system", "event": "run_state", "active": run_active})

        while True:
            incoming = await websocket.receive_text()
            try:
                packet = json.loads(incoming)
            except json.JSONDecodeError:
                continue

            if not isinstance(packet, dict):
                continue

            action = str(packet.get("action", "")).strip().lower()
            if action == "start_run":
                idea = str(packet.get("startup_idea", ""))
                ok, message = await _start_main_run(idea)
                await websocket.send_json(
                    {
                        "kind": "system",
                        "event": "start_run_ack",
                        "ok": ok,
                        "message": message,
                    }
                )
    except WebSocketDisconnect:
        pass
    finally:
        async with _clients_lock:
            _connected_clients.discard(websocket)
