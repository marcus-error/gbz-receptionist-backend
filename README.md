# AI Receptionist - Backend (Python / FastAPI)

Layered architecture: `routes -> controllers -> services -> repositories -> models`.

## Stack
- FastAPI
- SQLAlchemy (MySQL via PyMySQL)
- Pydantic v2 (request/response validation, in `validators/`)
- python-jose (JWT)
- passlib (password hashing)

## Setup & RAG + Mistral Configuration

### 1. Prerequisites & Virtual Environment

Create and activate a fresh virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate          # On Windows: .venv\Scripts\activate
```

### 2. Dependency Installation

Install all required dependencies (PyTorch, Hugging Face SentenceTransformers, ChromaDB, Mistral/OpenAI API, pypdf, python-dotenv):

```bash
pip install -r requirements.txt
```

### 3. Environment Variables Configuration

Copy `.env.example` to `.env` and fill in your API keys and configuration parameters:

```bash
cp .env.example .env
```

Configure key RAG + LLM settings in `.env`:

```ini
# Mistral LLM Primary / Answer Generator
LLM_PROVIDER=mistral
MISTRAL_API_KEY=your_mistral_api_key
MISTRAL_MODEL=mistral-tiny

# Optional Gemini (Query rewriting & Intent Normalization ONLY)
GEMINI_API_KEY=your_optional_gemini_api_key

# RAG & Hugging Face Embedding Settings
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
TOP_K=5
SIMILARITY_THRESHOLD=0.35
HOSPITAL_ID=mgm_healthcare_chennai
```

### 4. Sentence Transformer Model Caching

Upon first launch or document ingestion, Hugging Face `sentence-transformers/all-MiniLM-L6-v2` is downloaded automatically and cached locally. Subsequent runs will reuse the locally cached model weights.

### 5. Running Verification Tests

Run the complete backend test suite including Section 18 RAG + Mistral verification questions:

```bash
pytest tests/
```

### 6. Run Server

```bash
python -m src.server
# or
uvicorn src.server:app --reload --port 8000
```

API docs: http://localhost:8000/docs
Health check: http://localhost:8000/health

## Project layout

```
src/
  app.py            # FastAPI app factory (CORS, middleware, routers)
  server.py         # uvicorn entry point
  config/           # env, database, jwt, redis
  routes/           # APIRouter definitions, one file per resource
  controllers/      # thin request handlers, call into services
  services/         # business logic
  repositories/     # DB access (SQLAlchemy queries)
  models/           # SQLAlchemy ORM models
  validators/       # Pydantic schemas (request/response contracts)
  middleware/       # error handling, logging, auth, upload guards
  utils/            # shared helpers
  public/, uploads/ # static + uploaded files
  sockets/, jobs/   # reserved for websockets / background jobs
  docs/             # additional API documentation
tests/
```

## Adding a new resource

1. `models/<name>.py` - SQLAlchemy model, import it in `config/database.py::init_db`.
2. `validators/<name>_validator.py` - Pydantic Create/Update/Out schemas.
3. `repositories/<name>_repository.py` - DB queries.
4. `services/<name>_service.py` - business logic, uses the repository.
5. `controllers/<name>_controller.py` - request handlers, uses the service.
6. `routes/<name>_routes.py` - APIRouter wiring controller functions to paths.
7. Register the new router in `routes/index.py`.

## Currently implemented resources
- `auth` - register / login (JWT)
- `dashboard` - stats / recent calls / status (used by the Dashboard page)
- `appointments` - full CRUD (used by the Appointments page)
- `staff` - full CRUD (used by the Staff page)

Other sidebar pages (Knowledge Base, Conversations, Voice History,
Insights, Telephony, Calendar Integration, Notifications, Email,
WhatsApp, Gemini Accounts, Settings) are currently frontend-only demo
data, matching the original static mockup. Follow the "Adding a new
resource" steps above to back any of them with real data.
