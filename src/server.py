import os
import uvicorn

from src.app import app
from src.config.env import settings

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or getattr(settings, "PORT", None) or settings.APP_PORT or 8000)
    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=port,
        reload=settings.APP_ENV == "development",
    )
