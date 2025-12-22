from fastapi import APIRouter

from app.config import get_settings
from app.schemas.models import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Health check endpoint for monitoring and Railway healthchecks.
    """
    settings = get_settings()

    return HealthResponse(
        status="ok",
        version="1.0.0",
        environment=settings.environment,
    )
