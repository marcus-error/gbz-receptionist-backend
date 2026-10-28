from typing import List
from fastapi import APIRouter

from src.controllers import knowledge_controller
from src.validators.knowledge_validator import KnowledgeDocOut

router = APIRouter(prefix="/knowledge", tags=["Knowledge Base"])

router.add_api_route("", knowledge_controller.list_docs, methods=["GET"], response_model=List[KnowledgeDocOut])
router.add_api_route("", knowledge_controller.create_doc, methods=["POST"], response_model=KnowledgeDocOut, status_code=201)
router.add_api_route("/upload", knowledge_controller.upload_doc, methods=["POST"], response_model=KnowledgeDocOut, status_code=201)
router.add_api_route("/search", knowledge_controller.search_docs, methods=["GET"], response_model=List[KnowledgeDocOut])
router.add_api_route("/query", knowledge_controller.query_rag, methods=["POST"])
router.add_api_route("/reindex", knowledge_controller.reindex_all, methods=["POST"])
router.add_api_route("/{doc_id}", knowledge_controller.get_doc, methods=["GET"], response_model=KnowledgeDocOut)
router.add_api_route("/{doc_id}", knowledge_controller.update_doc, methods=["PUT"], response_model=KnowledgeDocOut)
router.add_api_route("/{doc_id}", knowledge_controller.delete_doc, methods=["DELETE"])
router.add_api_route("/{doc_id}/file", knowledge_controller.get_doc_file, methods=["GET"])
