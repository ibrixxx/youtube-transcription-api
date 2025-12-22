from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import health, metadata, transcribe
from app.services.transcription import init_assemblyai


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for startup/shutdown events.
    """
    # Startup: Initialize AssemblyAI
    init_assemblyai()
    yield
    # Shutdown: Nothing to clean up


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
    settings = get_settings()

    app = FastAPI(
        title="YouTube Transcription API",
        description="Minimal microservice for transcribing YouTube videos using yt-dlp and AssemblyAI",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(health.router, tags=["Health"])
    app.include_router(metadata.router, tags=["Metadata"])
    app.include_router(transcribe.router, tags=["Transcription"])

    return app


# Create the app instance
app = create_app()
