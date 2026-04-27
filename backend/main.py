"""
main.py — FastAPI entry point
Khởi động: uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio

from database import init_db
from closed_loop import ClosedLoopController
from decision_engine import DecisionEngine
from routers import stats, anomalies, recommendations, internal, control


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    engine = DecisionEngine()
    controller = ClosedLoopController()
    app.state.closed_loop_controller = controller

    decision_task = asyncio.create_task(engine.loop())
    control_task = asyncio.create_task(controller.loop())
    yield
    decision_task.cancel()
    control_task.cancel()


app = FastAPI(title="SDN Traffic Monitor API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stats.router,            prefix="/api")
app.include_router(anomalies.router,        prefix="/api")
app.include_router(recommendations.router,  prefix="/api")
app.include_router(control.router,          prefix="/api")
app.include_router(internal.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
