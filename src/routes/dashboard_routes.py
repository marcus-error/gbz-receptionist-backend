from fastapi import APIRouter

from src.controllers import dashboard_controller

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

router.add_api_route("/overview", dashboard_controller.get_overview, methods=["GET"])
router.add_api_route("/stats", dashboard_controller.get_stats, methods=["GET"])
router.add_api_route("/recent-calls", dashboard_controller.get_recent_calls, methods=["GET"])
router.add_api_route("/schedule", dashboard_controller.get_upcoming_schedule, methods=["GET"])
router.add_api_route("/leads", dashboard_controller.get_leads_summary, methods=["GET"])
router.add_api_route("/staff", dashboard_controller.get_staff_performance, methods=["GET"])
router.add_api_route("/status", dashboard_controller.get_status, methods=["GET"])
router.add_api_route("/insights", dashboard_controller.get_insights, methods=["GET"])

