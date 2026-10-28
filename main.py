import os
import uvicorn
from src.app import app
from src.config.env import settings

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or getattr(settings, "PORT", None) or settings.APP_PORT or 8000)
    is_dev = getattr(settings, "APP_ENV", "production") == "development"
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=is_dev,
        reload_dirs=["src"] if is_dev else None,
        reload_excludes=["scratch/*", "logs/*", "audio_checkpoints/*", "uploads/*", "*.log"] if is_dev else None,
    )
