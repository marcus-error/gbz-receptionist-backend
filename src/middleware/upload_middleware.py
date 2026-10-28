"""
Placeholder for upload validation (used by the future Knowledge Base
document upload endpoint - PDF/TXT/Markdown). Kept as a stub so the
folder structure matches the required layout; wire this into
routes/knowledge_routes.py once that endpoint is built.
"""
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
MAX_UPLOAD_SIZE_MB = 20


def is_allowed_file(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS)
