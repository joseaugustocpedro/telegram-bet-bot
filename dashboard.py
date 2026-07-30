from __future__ import annotations

import logging

from flask import Blueprint, jsonify, make_response, render_template, request

try:
    from .dashboard_analytics import dashboard_snapshot, initialize_dashboard_analytics
    from .database import init_db
    from .security import verify_dashboard_token
except ImportError:
    from dashboard_analytics import dashboard_snapshot, initialize_dashboard_analytics
    from database import init_db
    from security import verify_dashboard_token

logger = logging.getLogger("bank-dashboard-v7")
dashboard_bp = Blueprint("bank_dashboard", __name__, template_folder="templates")


@dashboard_bp.get("/dashboard/<token>")
def dashboard_page(token: str):
    try:
        verify_dashboard_token(token)
    except ValueError as exc:
        return render_template("dashboard.html", token="", initial_error=str(exc)), 401

    response = make_response(
        render_template("dashboard.html", token=token, initial_error="")
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@dashboard_bp.get("/api/dashboard/<token>")
def dashboard_api(token: str):
    try:
        identity = verify_dashboard_token(token)
        period = request.args.get("period", "30")
        payload = dashboard_snapshot(identity.chat_id, period)
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception:
        logger.exception("Falha ao carregar dashboard v7")
        return jsonify(
            {
                "error": "Não foi possível carregar o dashboard agora.",
                "hint": "Confira os logs do Render e tente atualizar a página.",
            }
        ), 500


def initialize_dashboard_database() -> None:
    init_db()
    initialize_dashboard_analytics()
