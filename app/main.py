import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import camera
from .models import CameraDevice, ExportRequest, FrameSelection, PreviewRequest, Settings
from .service import Recorder
from .projects import Projects


def create_app(data_dir=None, demo=None):
    projects = Projects(Path(data_dir or os.environ.get("DATA_DIR", "data")).resolve(),
                        os.environ.get("DEMO_MODE") == "1" if demo is None else demo)

    @asynccontextmanager
    async def lifespan(app):
        await projects.start()
        try:
            yield
        finally:
            await projects.stop()

    app = FastAPI(title="Grow · Plant time-lapse", lifespan=lifespan)
    app.state.projects = projects
    app.state.recorder = projects.get("default")  # Compatibility with the original single-project API.

    def get_recorder(project_id: str = "default"):
        recorder = projects.get(project_id)
        if recorder is None:
            raise HTTPException(404, "Project not found")
        return recorder

    router = APIRouter()

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        # JSON writes + same-origin checks prevent another website operating a local camera.
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/health")
    async def health():
        if not projects.active or any(rec.scheduler_task.done() for rec in projects.recorders.values()):
            raise HTTPException(503, "Recorder is not running")
        return {"status": "ok"}

    @app.get("/api/projects")
    async def list_projects():
        return {"projects": projects.list(), "demo": projects.demo}

    @app.post("/api/projects", status_code=201)
    async def create_project(settings: Settings):
        return await projects.create(settings)

    @app.get("/api/defaults")
    async def defaults():
        return Settings()

    @router.get("/status")
    async def status(recorder: Recorder = Depends(get_recorder)):
        return recorder.status()

    @app.get("/api/cameras")
    def cameras():
        return {"demo": projects.demo, "cameras": camera.list_devices(projects.demo)}

    @app.get("/api/cameras/capabilities")
    def capabilities(device: CameraDevice):
        return camera.capabilities(device, projects.demo)

    @app.get("/api/cameras/focus")
    def focus(device: CameraDevice):
        return camera.focus_capabilities(device, projects.demo)

    @router.put("/settings")
    async def settings(settings: Settings, recorder: Recorder = Depends(get_recorder)):
        await recorder.save_settings(settings)
        return recorder.settings()

    @router.post("/start")
    async def start(recorder: Recorder = Depends(get_recorder)):
        await recorder.set_running(True)
        return recorder.status()

    @router.post("/pause")
    async def pause(recorder: Recorder = Depends(get_recorder)):
        await recorder.set_running(False)
        return recorder.status()

    @router.post("/capture")
    async def capture(recorder: Recorder = Depends(get_recorder)):
        return await recorder.take_photo()

    @router.post("/preview")
    async def preview(request: PreviewRequest = PreviewRequest(), recorder: Recorder = Depends(get_recorder)):
        return Response(await recorder.take_photo(save=False, preview_settings=request.settings), media_type="image/jpeg")

    @router.get("/frames")
    async def frames(limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0), recorder: Recorder = Depends(get_recorder)):
        return recorder.store.rows("SELECT * FROM frames ORDER BY captured_at DESC LIMIT ? OFFSET ?", (limit, offset))

    @router.get("/timeline")
    def timeline(cutoff: float | None = Query(None, ge=0, allow_inf_nan=False), limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0), included_only: bool = False, recorder: Recorder = Depends(get_recorder)):
        return recorder.timeline(cutoff, offset, limit, included_only)

    @router.patch("/frames/selection")
    def selection(request: FrameSelection, recorder: Recorder = Depends(get_recorder)):
        recorder.select_frames(request.frame_ids, request.excluded)
        return {"updated": len(set(request.frame_ids)), "excluded": request.excluded}

    @router.get("/events")
    async def events(recorder: Recorder = Depends(get_recorder)):
        return recorder.store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 20")

    @router.get("/exports")
    async def exports(recorder: Recorder = Depends(get_recorder)):
        return recorder.store.rows("SELECT id,created_at,status,frames,fps,error FROM exports ORDER BY created_at DESC LIMIT 20")

    @router.post("/exports", status_code=202)
    async def export(request: ExportRequest = ExportRequest(), recorder: Recorder = Depends(get_recorder)):
        return await recorder.create_export(request.cutoff)

    app.include_router(router, prefix="/api/projects/{project_id}")
    app.include_router(router, prefix="/api", include_in_schema=False)

    @app.get("/media/projects/{project_id}/{kind}/{filename}")
    @app.get("/media/{kind}/{filename}", include_in_schema=False)
    async def media(kind: str, filename: str, recorder: Recorder = Depends(get_recorder)):
        extension = "mp4" if kind == "exports" else "jpg"
        if kind not in {"frames", "thumbs", "exports"} or not re.fullmatch(r"[0-9a-f]{32}\." + extension, filename):
            raise HTTPException(404)
        path = recorder.root / kind / filename
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4" if extension == "mp4" else "image/jpeg",
                            filename=filename if kind == "exports" else None)

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app


app = create_app()
