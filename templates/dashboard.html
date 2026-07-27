from __future__ import annotations

import logging

from flask import Blueprint, jsonify, make_response, render_template, request

try:
    from .database import dashboard_data, init_db
    from .security import verify_dashboard_token
except ImportError:
    from database import dashboard_data, init_db
    from security import verify_dashboard_token

logger = logging.getLogger("bank-dashboard")
dashboard_bp = Blueprint("bank_dashboard", __name__, template_folder="templates")


@dashboard_bp.get("/dashboard/<token>")
def dashboard_page(token: str):
    try:
        verify_dashboard_token(token)
    except ValueError as exc:
        return render_template("dashboard.html", token="", initial_error=str(exc)), 401
    response = make_response(render_template("dashboard.html", token=token, initial_error=""))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@dashboard_bp.get("/api/dashboard/<token>")
def dashboard_api(token: str):
    try:
        identity = verify_dashboard_token(token)
        period = request.args.get("period", "30")
        payload = dashboard_data(identity.chat_id, period)
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception:
        logger.exception("Falha ao carregar dashboard")
        return jsonify({"error": "Não foi possível carregar o dashboard agora."}), 500


def initialize_dashboard_database() -> None:
    init_db()
