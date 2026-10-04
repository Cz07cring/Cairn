from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import export, hints, intents, projects, settings
from cairn.server.integration.identity import product_gate, router as auth_router, product_mode
from cairn.server.integration.ring_client import RingConfig

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if product_mode():
        RingConfig.load()
    db.configure(db.DEFAULT_DB)
    if not product_mode():
        with db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM ring_bindings LIMIT 1").fetchone():
                raise RuntimeError("Ring bindings exist; standalone mode would expose project data")
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(settings.router)
app.include_router(auth_router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)
app.middleware("http")(product_gate)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
