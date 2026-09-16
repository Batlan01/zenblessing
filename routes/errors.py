from routes.auth import login_required, require_roles, MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES
from flask import Blueprint, render_template, session, jsonify, request
from services.db import get_db
from datetime import datetime

errors_bp = Blueprint('errors', __name__, url_prefix='/errors')

@errors_bp.route('/controller')
@require_roles(IT_ROLES)
def errors_controller():
    # nyelv a session-ből (a nyelvi base sablonhoz)
    lang = session.get('lang') if session.get('lang') in ('hu', 'sk') else 'hu'
    return render_template('errors_controller.html',
                           user=session.get('user'), active='errors', lang=lang)

@errors_bp.route('/api/data')
@require_roles(IT_ROLES)
def errors_data():
    conn = get_db()
    cursor = conn.cursor()
    query = ("SELECT ID, `Device IP`, `Device Name`, Error, `Error Raised Date`, "
             "`Error Solved Date`, `Error Status` FROM errors "
             "ORDER BY `Error Raised Date` DESC, ID DESC LIMIT 1000")
    cursor.execute(query)
    rows = cursor.fetchall()
    columns = ['ID', 'Device IP', 'Device Name', 'Error', 'Error Raised Date', 'Error Solved Date', 'Error Status']
    data = [dict(zip(columns, row)) for row in rows]
    cursor.close()
    return jsonify(data)

@errors_bp.route('/api/open_count')
@require_roles(IT_ROLES)
def errors_open_count():
    """Nyitott hibák száma a menü-badge-hez."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM errors WHERE `Error Status` IN ('Open','In Progress')"
        )
        row = cursor.fetchone()
        cursor.close()
        return jsonify({"ok": True, "count": int(row[0] if row else 0)})
    except Exception as e:
        return jsonify({"ok": False, "count": 0, "error": str(e)})

@errors_bp.route('/api/resolve', methods=['POST'])
@require_roles(IT_ROLES)
def resolve_error():
    error_id = request.json['id']
    current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    # FONTOS: az oszlop ENUM('Open','In Progress','Closed') – a korábbi
    # 'Completed' érték nem érvényes ENUM-tag, ezért nem működött a lezárás.
    query = """
        UPDATE errors
        SET `Error Solved Date` = %s, `Error Status` = 'Closed'
        WHERE ID = %s
    """
    cursor.execute(query, (current_date, error_id))
    conn.commit()
    cursor.close()
    return jsonify({"result": "Error resolved successfully."})

@errors_bp.route('/api/delete', methods=['POST'])
@require_roles(IT_ROLES)
def delete_error():
    error_id = request.json['id']
    conn = get_db()
    cursor = conn.cursor()
    query = "DELETE FROM errors WHERE ID = %s"
    cursor.execute(query, (error_id,))
    conn.commit()
    cursor.close()
    return jsonify({"result": "Error deleted successfully."})
