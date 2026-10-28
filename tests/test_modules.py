from fastapi.testclient import TestClient
from src.app import app
from src.config.database import init_db
from src.config.env import settings

init_db()
client = TestClient(app)
API_PREFIX = settings.API_PREFIX


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_knowledge_api():
    # 1. Get knowledge documents
    res = client.get(f"{API_PREFIX}/knowledge")
    assert res.status_code == 200
    docs = res.json()
    assert isinstance(docs, list)

    # 2. Create a new document via JSON
    payload = {
        "title": "Test Policy.pdf",
        "file_type": "PDF",
        "chunks": 15,
        "ai_usage": "Active",
        "status": "Indexed"
    }
    create_res = client.post(f"{API_PREFIX}/knowledge", json=payload)
    assert create_res.status_code == 201
    new_doc = create_res.json()
    assert new_doc["title"] == "Test Policy.pdf"
    assert new_doc["chunks"] == 15

    # 3. Test uploading a real file
    file_content = b"AI Receptionist support guide with FAQ and pricing details."
    files = {"file": ("support_faq.txt", file_content, "text/plain")}
    upload_res = client.post(f"{API_PREFIX}/knowledge/upload", files=files)
    assert upload_res.status_code == 201
    uploaded_doc = upload_res.json()
    assert uploaded_doc["title"] == "support_faq.txt"
    assert uploaded_doc["file_type"] == "TXT"

    # 4. Search documents by query
    search_res = client.get(f"{API_PREFIX}/knowledge/search?q=support")
    assert search_res.status_code == 200
    search_docs = search_res.json()
    assert len(search_docs) >= 1

    # 5. Cleanup / Delete document
    doc_id = uploaded_doc["id"]
    del_res = client.delete(f"{API_PREFIX}/knowledge/{doc_id}")
    assert del_res.status_code == 200


def test_settings_api():
    # 1. Get app settings
    res = client.get(f"{API_PREFIX}/settings")
    assert res.status_code == 200
    settings_data = res.json()
    assert "receptionist_name" in settings_data
    assert isinstance(settings_data["receptionist_name"], str)

    # 2. Update app settings
    update_payload = {
        "receptionist_name": "Ava Pro",
        "ai_inbound_calling": True,
        "call_recording": False
    }
    put_res = client.put(f"{API_PREFIX}/settings", json=update_payload)
    assert put_res.status_code == 200
    updated = put_res.json()
    assert updated["receptionist_name"] == "Ava Pro"
    assert updated["call_recording"] is False

    # Cleanup settings back to default
    client.put(f"{API_PREFIX}/settings", json={"receptionist_name": "Ava", "call_recording": True})


def test_calls_api():
    # 1. Post a voice test call first
    test_call_payload = {
        "caller_phone": "Voice Assistant Test",
        "caller_name": "Test Runner",
        "call_type": "Inbound",
        "status": "VOICE TEST MODE",
        "duration": "00:15",
        "caller_said": "Hello AI receptionist",
        "ai_response": "Hello! How can I help you?",
        "intent": "Greeting",
        "emotion": "Happy",
        "language": "English",
        "rag_source": "company-profile.pdf"
    }
    create_res = client.post(f"{API_PREFIX}/calls", json=test_call_payload)
    assert create_res.status_code == 201
    new_call = create_res.json()
    assert new_call["caller_said"] == "Hello AI receptionist"

    # 2. Get calls list
    res = client.get(f"{API_PREFIX}/calls")
    assert res.status_code == 200
    calls = res.json()
    assert isinstance(calls, list)
    assert len(calls) >= 1

    # 3. Get latest inbound call
    latest_inbound = client.get(f"{API_PREFIX}/calls/latest?call_type=Inbound")
    assert latest_inbound.status_code == 200
    assert latest_inbound.json() is not None
    assert latest_inbound.json()["call_type"] == "Inbound"
