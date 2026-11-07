"""DoseGuard local server. One process, bound to loopback, one instance of each service.

Run:  .venv/bin/python -m app.main     (or ./run.sh)"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai import AIError, AIService
from .camera import CaptureManager
from .config import ROOT, Settings
from .context import authoritative_explanation, build_facts, local_answer, select_help
from .errors import WorkflowError
from .hardware import HardwareAdapter, make_adapter
from . import telemetry
from .plans import confirm_plan
from .reminders import ReminderService
from .status import build_status, dose_detail, history, recent_events
from .store import Store, dumps
from .timeutil import Clock, fmt_local, parse
from .vision import VisionService
from .workflow import WorkflowEngine

log = logging.getLogger("doseguard")
STATIC = ROOT / "static"


@dataclass
class Services:
    settings: Settings
    store: Store
    hardware: HardwareAdapter
    camera: CaptureManager
    vision: VisionService
    reminders: ReminderService
    engine: WorkflowEngine
    ai: AIService
    token: str


# ---------------------------------------------------------------- request models
class Cmd(BaseModel):
    request_id: str = Field(min_length=6, max_length=64)


class ReminderCmd(Cmd):
    reminder_id: str = Field(max_length=32)


class DoseStart(Cmd):
    dose_id: str = Field(max_length=32)
    plan_version: int


class AsNeededRequest(Cmd):
    entry_key: str = Field(min_length=1, max_length=32)
    plan_version: int


class TxnCmd(Cmd):
    txn_id: str = Field(max_length=32)


class DoseCmd(Cmd):
    dose_id: str = Field(max_length=32)


class Inspected(Cmd):
    inspected: bool = False


class Enroll(Cmd):
    label: str = Field(min_length=1, max_length=40)
    consent: bool = False


class PlanConfirm(Cmd):
    base_plan_version_id: int | None = None
    person_id: str | None = None
    timezone: str
    timezone_confirmed: bool = False
    dst_reviewed: bool = False
    entries: list[dict] = Field(max_length=12)


class Ask(Cmd):
    question: str = Field(min_length=1, max_length=500)
    history: list[dict] = Field(default_factory=list, max_length=8)


class DemoMode(Cmd):
    enabled: bool


class DemoTrigger(Cmd):
    entry_keys: list[str] | None = None


class Calibrate(Cmd):
    class_id: str = Field(max_length=24)
    label: str | None = Field(default=None, max_length=24)


class LabelCapture(Cmd):
    label_id: str | None = Field(default=None, max_length=12)


class LabelRead(Cmd):
    label_id: str = Field(max_length=12)
    container: str = Field(default="flat", pattern="^(flat|bottle)$")


def check_label_photo(img, min_sharpness: float) -> tuple[float, str | None]:
    """Sharpness and glare. Shiny round bottles often reflect a bright stripe over the text."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if sharp < min_sharpness:
        return sharp, ("The label photo is blurry. Hold the bottle still, about 20 cm from the camera, "
                       "with the text facing it, and capture again.")
    h, w = gray.shape
    centre = gray[h // 4: 3 * h // 4, w // 4: 3 * w // 4]  # where the label should be
    if float(np.mean(centre >= 250)) > 0.06:
        return sharp, ("Too much glare on the label. Tilt the bottle slightly away from the light "
                       "or move away from the window, and capture again.")
    return sharp, None


class SimHardware(BaseModel):
    action: str
    channel: str | None = None
    mode: str | None = None
    seconds: float | None = None
    force: bool | None = None
    n: int | None = None


class SimVision(BaseModel):
    face: str | None = None
    tray: str | None = None


# ---------------------------------------------------------------- app factory
def create_app(settings: Settings | None = None, clock: Clock | None = None, testing: bool = False) -> FastAPI:
    settings = settings or Settings.from_env()
    problems = settings.validate()
    if problems:
        raise SystemExit("Configuration problems:\n  " + "\n  ".join(problems))
    clock = clock or Clock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path, clock)
        hardware = make_adapter(settings, clock)
        camera = CaptureManager(settings, clock)
        vision = VisionService(settings, store, camera, hardware, clock)
        reminders = ReminderService(store, settings)
        engine = WorkflowEngine(store, reminders, hardware, vision, settings)
        ai = AIService(settings)
        svc = Services(settings, store, hardware, camera, vision, reminders, engine, ai, secrets.token_urlsafe(24))
        app.state.svc = svc
        await hardware.start()
        await camera.start()
        engine.start()
        reminders.start()
        await ai.init()
        store.log("APP_STARTED", details={"hardware": settings.hardware_mode, "camera": settings.camera_mode,
                                          "ai": settings.genai_backend})
        try:
            yield
        finally:
            await reminders.stop()
            await engine.stop_task()
            await camera.stop()
            await hardware.close()
            store.conn.close()

    app = FastAPI(title="DoseGuard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    hosts = ["127.0.0.1", "localhost", "[::1]", "::1"] + (["testserver"] if testing else [])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    allowed_origins = {f"http://{h}:{settings.port}" for h in ("127.0.0.1", "localhost", "[::1]")}

    @app.middleware("http")
    async def guard_commands(request: Request, call_next):
        if request.method == "POST":
            svc: Services = request.app.state.svc
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return JSONResponse({"result": "rejected", "reason_codes": ["BAD_ORIGIN"], "message": "Origin not allowed."}, 403)
            if not secrets.compare_digest(request.headers.get("x-doseguard-token", ""), svc.token):
                return JSONResponse({"result": "rejected", "reason_codes": ["BAD_TOKEN"],
                                     "message": "Missing or wrong session token. Reload the page."}, 403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(WorkflowError)
    async def workflow_error(request: Request, exc: WorkflowError):
        svc: Services = request.app.state.svc
        body = {"request_id": getattr(request.state, "request_id", None), "state_version": svc.store.version,
                "result": "rejected", "reason_codes": exc.extra.pop("reason_codes", None) or [exc.code], "message": exc.msg}
        body.update(exc.extra)
        return JSONResponse(body, status_code=exc.status)

    def ok(request: Request, req_id: str | None, data: dict, result: str | None = None) -> dict:
        svc: Services = request.app.state.svc
        return {"request_id": req_id, "state_version": svc.store.version,
                "result": result or data.pop("result", "ok"), "reason_codes": data.pop("reason_codes", []), **data}

    def S(request: Request) -> Services:
        return request.app.state.svc

    def require_idle(svc: Services) -> None:
        if svc.engine.active_txn() or svc.engine.pending:
            raise WorkflowError(409, "HARDWARE_BUSY")

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        html = (STATIC / "index.html").read_text()
        return HTMLResponse(html.replace("__DOSEGUARD_TOKEN__", S(request).token))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------ reads
    @app.get("/api/status")
    async def status(request: Request):
        return build_status(S(request))

    @app.get("/api/camera/frame")
    async def camera_frame(request: Request, role: str = "face"):
        data = await asyncio.to_thread(S(request).camera.preview_jpeg, "tray" if role == "tray" else "face")
        if data is None:
            return Response(status_code=204)
        return Response(data, media_type="image/jpeg")

    @app.get("/api/camera/stream")
    async def camera_stream(request: Request, role: str = "face"):
        camera = S(request).camera
        if camera.simulated:
            return Response(status_code=204)
        return StreamingResponse(camera.preview_stream("tray" if role == "tray" else "face"),
                                 media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"X-Accel-Buffering": "no"})

    @app.get("/api/camera/face-preview")
    async def face_preview(request: Request):
        """Live positioning hint for enrollment: face count and detector confidence only."""
        return await S(request).vision.face_preview()

    @app.get("/api/history")
    async def get_history(request: Request, limit: int = 50):
        return {"doses": history(S(request), limit)}

    @app.get("/api/history/{dose_id}")
    async def get_dose(request: Request, dose_id: str):
        d = dose_detail(S(request), dose_id)
        if not d:
            raise WorkflowError(404, "NOT_FOUND", "Dose not found.")
        return d

    @app.get("/api/setup")
    async def get_setup(request: Request):
        svc = S(request)
        return {"plan": svc.store.active_plan(), "persons": [
                    {"person_id": p["person_id"], "label": p["label"], "simulated": bool(p["simulated"]),
                     "enrolled": bool(p["embedding_ref"]) or bool(p["simulated"]), "consent_at": p["consent_at"]}
                    for p in svc.store.persons()],
                "token_classes": [{k: c[k] for k in ("class_id", "label", "cartridge", "color")} for c in svc.store.token_classes()],
                "default_timezone": svc.settings.timezone, "max_quantity": svc.settings.max_tokens_per_channel,
                "camera_simulated": svc.camera.simulated, "ai": svc.ai.status(),
                "require_label_photo": svc.settings.require_label_photo}

    @app.get("/api/export/events.csv")
    async def export_events(request: Request):
        svc = S(request)
        rows = svc.store.q("SELECT event_id, ts, kind, dose_id, reminder_id, txn_id, reason_code, details_json, demo"
                           " FROM events ORDER BY event_id")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["event_id", "ts_utc", "kind", "dose_id", "reminder_id", "txn_id", "reason_code", "details", "demo"])
        for r in rows:
            w.writerow([r[k] for k in ("event_id", "ts", "kind", "dose_id", "reminder_id", "txn_id", "reason_code", "details_json", "demo")])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=doseguard-events.csv"})

    @app.get("/api/metrics")
    async def get_metrics(request: Request):
        svc = S(request)
        return telemetry.metrics(svc.store, simulated=svc.hardware.simulated)

    @app.get("/api/export/metrics.csv")
    async def export_metrics(request: Request):
        svc = S(request)
        demo = svc.store.demo
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["metric", "id", "channel", "start_utc", "end_utc", "ms", "outcome", "demo", "simulated_hardware"])
        sim = int(svc.hardware.simulated)
        for d in reversed(telemetry.reminder_delays(svc.store, demo, 5000)):
            w.writerow(["reminder_delivery_delay", d["reminder_id"], "", d["due_at_utc"], d["delivered_at_utc"], d["delay_ms"], "DUE", demo, sim])
        for c in reversed(telemetry.dispense_cycles(svc.store, demo, 5000)):
            w.writerow(["dispense_cycle_duration", c["command_id"], c["channel"], c["started_at_utc"], c["finished_at_utc"] or "",
                        "" if c["duration_ms"] is None else c["duration_ms"], c["outcome"], demo, sim])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=doseguard-metrics.csv"})

    # ------------------------------------------------------------ reminders
    @app.post("/api/reminders/snooze")
    async def snooze(request: Request, body: ReminderCmd):
        return ok(request, body.request_id, S(request).reminders.snooze(body.request_id, body.reminder_id))

    @app.post("/api/reminders/ack")
    async def ack(request: Request, body: ReminderCmd):
        return ok(request, body.request_id, S(request).reminders.acknowledge(body.request_id, body.reminder_id))

    @app.post("/api/reminders/close")
    async def close(request: Request, body: ReminderCmd):
        return ok(request, body.request_id, S(request).reminders.close_reviewed(body.request_id, body.reminder_id))

    @app.post("/api/doses/self-report")
    async def self_report(request: Request, body: DoseCmd):
        return ok(request, body.request_id, S(request).reminders.mark_taken_self_report(body.request_id, body.dose_id))

    # ------------------------------------------------------------ dose workflow
    @app.post("/api/doses/start")
    async def start(request: Request, body: DoseStart):
        return ok(request, body.request_id, await S(request).engine.start_dose(body.request_id, body.dose_id, body.plan_version))

    @app.post("/api/doses/request")
    async def request_as_needed(request: Request, body: AsNeededRequest):
        """An as-needed dose on request. Limits, arming and every workflow check are enforced by the engine."""
        return ok(request, body.request_id, await S(request).engine.request_as_needed(body.request_id, body.entry_key,
                                                                                       body.plan_version))

    @app.post("/api/doses/check")
    async def check(request: Request, body: TxnCmd):
        return ok(request, body.request_id, await S(request).engine.run_check(body.request_id, body.txn_id))

    @app.post("/api/doses/release")
    async def release(request: Request, body: TxnCmd):
        return ok(request, body.request_id, await S(request).engine.request_release(body.request_id, body.txn_id))

    @app.post("/api/doses/finish")
    async def finish(request: Request, body: TxnCmd):
        return ok(request, body.request_id, await S(request).engine.finish_without_collection(body.request_id, body.txn_id))

    @app.post("/api/doses/cancel")
    async def cancel(request: Request, body: TxnCmd):
        return ok(request, body.request_id, await S(request).engine.cancel(body.request_id, body.txn_id))

    @app.post("/api/doses/confirm")
    async def confirm(request: Request, body: DoseCmd):
        return ok(request, body.request_id, await S(request).engine.confirm_taken(body.request_id, body.dose_id))

    # ------------------------------------------------------------ device
    @app.post("/api/device/stop")
    async def stop(request: Request, body: Cmd):
        return ok(request, body.request_id, await S(request).engine.stop(body.request_id))

    @app.post("/api/device/arm")
    async def arm(request: Request, body: Inspected):
        return ok(request, body.request_id, await S(request).engine.arm(body.request_id, body.inspected))

    @app.post("/api/device/reset")
    async def reset(request: Request, body: Inspected):
        return ok(request, body.request_id, await S(request).engine.reset(body.request_id, body.inspected))

    # ------------------------------------------------------------ setup
    @app.post("/api/enroll")
    async def enroll(request: Request, body: Enroll):
        svc = S(request)
        if not body.consent:
            raise WorkflowError(422, "VALIDATION", "Consent is required before capturing reference images.")
        prev = svc.store.get_request(body.request_id)
        if prev:
            return json.loads(prev["response_json"])
        person_id = uuid.uuid4().hex[:10]
        ref, err = await svc.vision.enroll(person_id)
        if err:
            raise WorkflowError(409, "CAMERA_UNAVAILABLE", err)
        simulated = svc.camera.simulated
        with svc.store.tx() as conn:
            conn.execute("INSERT INTO persons (person_id, label, consent_at, embedding_ref, simulated, created_at) VALUES (?,?,?,?,?,?)",
                         (person_id, body.label.strip(), svc.store.now(), ref, int(simulated), svc.store.now()))
            svc.store.event(conn, "PERSON_ENROLLED", details={"person_id": person_id, "simulated": simulated})
            result = ok(request, body.request_id, {"person_id": person_id, "simulated": simulated})
            svc.store.save_request(conn, body.request_id, "enroll", 200, result)
        demo_plan = svc.store.active_plan(1)
        if demo_plan and not demo_plan["person_id"]:
            _create_demo_plan(svc)
        return result

    MAX_LABEL_PHOTOS = 4

    async def save_label(request: Request, svc: Services, request_id: str, data: bytes, mime: str,
                         source: str, sharpness: float | None, label_id: str | None = None) -> dict:
        """Store one photo locally. With label_id, add another angle of the same label
        (a round bottle needs several). Reading the fields is a separate step."""
        if label_id:
            row = svc.store.q1("SELECT label_id FROM label_scans WHERE label_id=?", (label_id,))
            if not row:
                raise WorkflowError(404, "NOT_FOUND", "Label not found. Start a new label photo.")
        n = svc.store.q1("SELECT COUNT(*) AS n FROM label_images WHERE label_id=?", (label_id or "-",))["n"]
        if n >= MAX_LABEL_PHOTOS:
            raise WorkflowError(409, "VALIDATION", f"At most {MAX_LABEL_PHOTOS} photos per label.")
        new_id = label_id or uuid.uuid4().hex[:12]
        ext = {"image/png": "png", "image/webp": "webp", "image/heic": "heic"}.get(mime, "jpg")
        rel = f"labels/{new_id}_{n}.{ext}"
        path = svc.settings.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with svc.store.tx() as conn:
            if not label_id:
                conn.execute("INSERT INTO label_scans (label_id, path, source, sharpness, fields_json, ai_status, demo, created_at)"
                             " VALUES (?,?,?,?,?,?,?,?)",
                             (new_id, rel, source, sharpness, None, "not_read", svc.store.demo_flag(conn), svc.store.now()))
            conn.execute("INSERT INTO label_images (label_id, idx, path, mime, sharpness, created_at) VALUES (?,?,?,?,?,?)",
                         (new_id, n, rel, mime, sharpness, svc.store.now()))
            svc.store.event(conn, "LABEL_PHOTO", details={"label_id": new_id, "photo": n + 1, "source": source})
            result = ok(request, request_id, {
                "label_id": new_id, "photos": n + 1, "max_photos": MAX_LABEL_PHOTOS,
                "image_urls": [f"/api/labels/{new_id}/image?i={i}" for i in range(n + 1)]})
            svc.store.save_request(conn, request_id, "labels", 200, result)
        return result

    @app.post("/api/labels/capture")
    async def capture_label(request: Request, body: LabelCapture):
        import asyncio
        import cv2
        svc = S(request)
        prev = svc.store.get_request(body.request_id)
        if prev:
            return json.loads(prev["response_json"])
        if svc.camera.simulated:
            raise WorkflowError(409, "CAMERA_UNAVAILABLE", "The camera is simulated. Upload a photo of the label instead.")
        f = await svc.camera.frame("face")
        if f is None or svc.camera.clock.monotonic() - f.capture_mono > 1.0:
            raise WorkflowError(503, "CAMERA_STALE")
        img = f.image
        sharp, problem = await asyncio.to_thread(check_label_photo, img, svc.settings.label_min_sharpness)
        if problem:
            raise WorkflowError(422, "IMAGE_BLURRY" if "blurry" in problem else "GLARE", problem)
        ok_, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return await save_label(request, svc, body.request_id, buf.tobytes(), "image/jpeg", "camera",
                                round(sharp, 1), body.label_id)

    @app.post("/api/labels/extract")
    async def upload_label(request: Request, file: UploadFile = File(...), request_id: str = Form(...),
                           label_id: str | None = Form(default=None)):
        svc = S(request)
        prev = svc.store.get_request(request_id)
        if prev:
            return json.loads(prev["response_json"])
        data = await file.read(svc.settings.max_upload_bytes + 1)
        if len(data) > svc.settings.max_upload_bytes:
            raise WorkflowError(422, "VALIDATION", "Image is too large (4 MB limit).")
        mime = file.content_type or ""
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/heic"):
            raise WorkflowError(422, "VALIDATION", "Upload a JPEG, PNG, WebP or HEIC image.")
        return await save_label(request, svc, request_id, data, mime, "upload", None, label_id or None)

    @app.post("/api/labels/read")
    async def read_label(request: Request, body: LabelRead):
        """Ask Gemini to read every photo of this label together. Suggestions only."""
        svc = S(request)
        prev = svc.store.get_request(body.request_id)
        if prev:
            return json.loads(prev["response_json"])
        imgs = svc.store.q("SELECT path, mime FROM label_images WHERE label_id=? ORDER BY idx", (body.label_id,))
        if not imgs:
            raise WorkflowError(404, "NOT_FOUND", "No photos for this label.")
        fields, ai_status, note = None, "off", None
        if svc.ai.ready:
            try:
                data = [((svc.settings.data_dir / r["path"]).read_bytes(), r["mime"]) for r in imgs]
                fields = await svc.ai.extract_label(data, svc.settings.max_tokens_per_channel, body.container)
                ai_status = "ok"
            except AIError as exc:
                ai_status, note = exc.code, exc.message + " Type the fields from the label photos yourself."
        else:
            note = "AI is off, so fields were not read automatically. Type them from the label photos."
        with svc.store.tx() as conn:
            conn.execute("UPDATE label_scans SET fields_json=?, ai_status=? WHERE label_id=?",
                         (dumps(fields) if fields else None, ai_status, body.label_id))
            svc.store.event(conn, "LABEL_READ", details={"label_id": body.label_id, "photos": len(imgs),
                                                         "container": body.container, "ai": ai_status, "requires_review": True})
            result = ok(request, body.request_id, {"label_id": body.label_id, "photos": len(imgs), "fields": fields,
                                                   "ai_status": ai_status, "note": note, "confirmed": False})
            svc.store.save_request(conn, body.request_id, "labels/read", 200, result)
        return result

    @app.get("/api/labels/{label_id}/image")
    async def label_image(request: Request, label_id: str, i: int = 0):
        import re
        from fastapi.responses import FileResponse
        svc = S(request)
        if not re.fullmatch(r"[0-9a-f]{12}", label_id):
            raise WorkflowError(404, "NOT_FOUND", "Label not found.")
        row = svc.store.q1("SELECT path FROM label_images WHERE label_id=? AND idx=?", (label_id, i)) or \
            svc.store.q1("SELECT path FROM label_scans WHERE label_id=?", (label_id,))
        if not row or not (svc.settings.data_dir / row["path"]).exists():
            raise WorkflowError(404, "NOT_FOUND", "Label not found.")
        return FileResponse(svc.settings.data_dir / row["path"])

    @app.post("/api/plans/confirm")
    async def plans_confirm(request: Request, body: PlanConfirm):
        svc = S(request)
        prev = svc.store.get_request(body.request_id)
        if prev:
            return json.loads(prev["response_json"])
        result = ok(request, body.request_id, confirm_plan(svc.store, svc.reminders, request=body.model_dump(),
                                                           max_per_channel=svc.settings.max_tokens_per_channel,
                                                           require_label=svc.settings.require_label_photo))
        with svc.store.tx() as conn:
            svc.store.save_request(conn, body.request_id, "plans/confirm", 200, result)
        return result

    # ------------------------------------------------------------ assistant (read only)
    @app.post("/api/assistant")
    async def assistant(request: Request, body: Ask):
        svc = S(request)
        snap = build_status(svc)
        facts = build_facts(snap, recent_events(svc))
        snippets = select_help(body.question)
        auth = authoritative_explanation(snap)
        source, note = "local", None
        history_clean = [{"role": h.get("role") if h.get("role") in ("user", "assistant") else "user",
                          "text": str(h.get("text", ""))[:300]} for h in body.history[-4:]]
        try:
            answer = await svc.ai.ask(body.question, snippets, facts, history_clean)
            source = "gemini"
        except AIError as exc:
            answer = local_answer(body.question, snap, facts)
            note = exc.message
        used = [f for f in facts if f["id"] in answer["supporting_fact_ids"]] + \
               [{"id": h["id"], "text": h["title"]} for h in snippets if h["id"] in answer["supporting_fact_ids"]]
        with svc.store.tx() as conn:
            conn.execute("INSERT INTO assistant_exchanges (request_id, snapshot_version, fact_ids_json, result, backend, model, created_at)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (body.request_id, snap["state_version"], dumps(answer["supporting_fact_ids"]), source,
                          svc.ai.backend, svc.ai.model, svc.store.now()))
        return ok(request, body.request_id, {
            "answer": answer, "authoritative": auth, "supporting_facts": used, "source": source, "note": note,
            "snapshot_time": snap["server_time"], "snapshot_time_local": fmt_local(parse(snap["server_time"]), _tz(svc)),
            "snapshot_version": svc.store.version,  # after logging this exchange
        })

    # ------------------------------------------------------------ demo mode
    @app.post("/api/demo/mode")
    async def demo_mode(request: Request, body: DemoMode):
        svc = S(request)
        require_idle(svc)
        with svc.store.tx() as conn:
            svc.store.meta_set(conn, "demo_mode", "1" if body.enabled else "0")
            svc.store.event(conn, "DEMO_MODE", details={"enabled": body.enabled}, demo=1 if body.enabled else 0)
        if body.enabled and not svc.store.active_plan(1):
            _create_demo_plan(svc)
        return ok(request, body.request_id, {"demo_mode": body.enabled})

    @app.post("/api/demo/trigger")
    async def demo_trigger(request: Request, body: DemoTrigger):
        svc = S(request)
        return ok(request, body.request_id, svc.reminders.trigger_demo(body.request_id, body.entry_keys, 30))

    @app.post("/api/demo/reset")
    async def demo_reset(request: Request, body: Cmd):
        svc = S(request)
        require_idle(svc)
        if not svc.store.demo:
            raise WorkflowError(409, "DEMO_MISMATCH", "Demo reset is available only in demo mode.")
        with svc.store.tx() as conn:
            txns = [r["txn_id"] for r in conn.execute("SELECT txn_id FROM transactions WHERE demo=1").fetchall()]
            for t in txns:
                conn.execute("DELETE FROM commands WHERE txn_id=?", (t,))
            for table in ("reminders", "doses", "transactions", "observations", "events", "plan_versions"):
                conn.execute(f"DELETE FROM {table} WHERE demo=1")
            svc.store.event(conn, "DEMO_RESET", demo=1)
        _create_demo_plan(svc)
        return ok(request, body.request_id, {"reset": True})

    # ------------------------------------------------------------ tray calibration
    @app.post("/api/tray/calibrate")
    async def tray_calibrate(request: Request, body: Calibrate):
        """Learn one pill type's colour and shape from a single sample in the tray."""
        svc = S(request)
        require_idle(svc)
        classes = svc.store.class_by_id()
        if body.class_id not in classes:
            raise WorkflowError(404, "NOT_FOUND", "Unknown pill type.")
        async with svc.engine.lock:  # nothing else uses the camera for checks meanwhile
            prof, err = await svc.vision.calibrate_class()
        if err:
            raise WorkflowError(409, "CALIBRATION_FAILED", err)
        label = (body.label or "").strip() or classes[body.class_id]["label"]
        with svc.store.tx() as conn:
            conn.execute("UPDATE token_classes SET hsv_json=?, color=?, label=? WHERE class_id=?",
                         (dumps({"ranges": prof["ranges"], "shape": prof["shape"]}), prof["color"], label, body.class_id))
            svc.store.event(conn, "TRAY_CLASS_CALIBRATED", details={"class_id": body.class_id, "label": label, **prof["shape"]})
        return ok(request, body.request_id, {"class_id": body.class_id, "label": label, **prof})

    @app.post("/api/tray/rename")
    async def tray_rename(request: Request, body: Calibrate):
        svc = S(request)
        if body.class_id not in svc.store.class_by_id() or not (body.label or "").strip():
            raise WorkflowError(422, "VALIDATION", "Choose a pill type and a name.")
        with svc.store.tx() as conn:
            conn.execute("UPDATE token_classes SET label=? WHERE class_id=?", (body.label.strip(), body.class_id))
        return ok(request, body.request_id, {"class_id": body.class_id, "label": body.label.strip()})

    # ------------------------------------------------------------ simulators (fake modes only)
    @app.post("/api/sim/hardware")
    async def sim_hardware(request: Request, body: SimHardware):
        svc = S(request)
        if not svc.hardware.simulated:
            raise WorkflowError(409, "INVALID_TRANSITION", "The hardware simulator is off (HARDWARE_MODE=serial).")
        err = svc.hardware.sim(body.action, **body.model_dump(exclude_none=True, exclude={"action"}))
        svc.store.log("SIMULATOR_ACTION", details={"simulated": True, **body.model_dump(exclude_none=True)})
        if err:
            raise WorkflowError(409, "INVALID_TRANSITION", err)
        return ok(request, None, {"simulator": svc.hardware.sim_state()})

    @app.post("/api/sim/vision")
    async def sim_vision(request: Request, body: SimVision):
        svc = S(request)
        if not svc.vision.simulated:
            raise WorkflowError(409, "INVALID_TRANSITION", "Vision simulation is off (CAMERA_MODE is not simulated).")
        svc.vision.set_sim(body.face, body.tray)
        return ok(request, None, {"sim": svc.vision.sim})

    return app


def _tz(svc: Services):
    from .status import tz_for
    return tz_for(svc)


def _create_demo_plan(svc: Services) -> None:
    persons = svc.store.persons()
    person_id = persons[-1]["person_id"] if persons else None
    entries = [
        {"entry_key": "demoA", "label": "Demo token A (fictional)", "strength_text": "blue", "quantity": 1,
         "delivery_mode": "DEVICE", "class_id": "blue", "cartridge": "A", "times": []},
        {"entry_key": "demoB", "label": "Demo token B (fictional)", "strength_text": "orange", "quantity": 1,
         "delivery_mode": "DEVICE", "class_id": "orange", "cartridge": "B", "times": []},
        {"entry_key": "demoR", "label": "Vitamin reminder (fictional)", "strength_text": None, "quantity": 1,
         "delivery_mode": "REMINDER_ONLY", "class_id": None, "cartridge": None, "times": []},
        {"entry_key": "demoP", "label": "Headache relief (fictional)", "strength_text": "orange", "quantity": 1,
         "delivery_mode": "DEVICE", "class_id": "orange", "cartridge": "B", "times": [],
         "schedule": "AS_NEEDED", "min_interval_hours": 4, "max_per_day": 3},
    ]
    with svc.store.tx() as conn:
        conn.execute("UPDATE plan_versions SET active=0 WHERE demo=1")
        n = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM plan_versions WHERE plan_id='demo'").fetchone()["v"]
        conn.execute("INSERT INTO plan_versions (plan_id, version, person_id, timezone, entries_json, confirmed_at, active, demo)"
                     " VALUES ('demo',?,?,?,?,?,1,1)", (n + 1, person_id, svc.settings.timezone, dumps(entries), svc.store.now()))
        svc.store.event(conn, "DEMO_PLAN_CREATED", demo=1, details={"person_id": person_id})


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    app = create_app(settings)
    print(f"\n  DoseGuard  ->  http://{settings.host}:{settings.port}\n"
          f"  hardware={settings.hardware_mode}  camera={settings.camera_mode}  ai={settings.genai_backend}\n"
          "  Keep this laptop awake and the page open for reminders.\n")
    # One process, no workers, no auto-reload: exactly one owner of the serial port and cameras.
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, reload=False, log_level="warning")


if __name__ == "__main__":
    main()
