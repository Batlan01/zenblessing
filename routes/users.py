from routes.auth import login_required, require_roles, MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES
from flask import Blueprint, render_template, session, jsonify, request, send_file
from services.db import get_db
from utils.helpers import fetch_data, format_time_difference
import pandas as pd
import io

users_bp = Blueprint('users', __name__, url_prefix='/users')

@users_bp.route('/controller')
@require_roles(MANAGER_ROLES, IT_ROLES)  # manager VAGY teamleader mehet

def users_controller():
    columns = ['ID', 'Workstation ID', 'Device ID', 'Worker Name', 'Work Order Data', 'Start Time', 'End Time', 'Status', 'Process ID', 'Next Station ID', 'QTY']
    rows = fetch_data()
    return render_template('users_controller.html', columns=columns, rows=rows, user=session.get('user'))

@users_bp.route('/api/data')
def users_data():
    rows = fetch_data()
    columns = ['ID', 'Workstation ID', 'Device ID', 'Worker Name', 'Work Order Data', 'Start Time', 'End Time', 'Status', 'Process ID', 'Next Station ID', 'QTY']
    data = [dict(zip(columns, row)) for row in rows]
    return jsonify(data)

@users_bp.route('/api/update/<int:id>', methods=['POST'])
def update_user(id):
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    query = """
        UPDATE workstationworkorder
        SET start_time = %s, end_time = %s, status = %s, process_id = %s, next_station_id = %s
        WHERE ID = %s
    """
    cursor.execute(query, (data['Start Time'], data['End Time'], data['Status'], data['Process ID'], data['Next Station ID'], id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"message": "Record updated successfully"})

@users_bp.route('/api/delete/<int:id>', methods=['DELETE'])
def delete_user(id):
    conn = get_db()
    cursor = conn.cursor()
    query = "DELETE FROM workstationworkorder WHERE ID = %s"
    cursor.execute(query, (id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"message": "Record deleted successfully"})

@users_bp.route('/api/delete_selected', methods=['POST'])
def delete_selected_users():
    data = request.json
    ids = data['ids']  # List of IDs to delete
    if not ids:
        return jsonify({"message": "No IDs provided"}), 400
    conn = get_db()
    cursor = conn.cursor()
    query = "DELETE FROM workstationworkorder WHERE ID IN (%s)" % ','.join(['%s'] * len(ids))
    cursor.execute(query, ids)
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"message": "Selected records deleted successfully"})

@users_bp.route('/api/export_to_excel', methods=['GET'])
def export_to_excel():
    conn = get_db()
    cursor = conn.cursor()
    query = """
    SELECT 
        w.ID, 
        w.workstation_id, 
        w.device_id, 
        wo.name AS worker_name, 
        CONCAT('WO: ', wo2.WO, ', PN: ', wo2.PN, '') AS work_order_data, 
        w.start_time,
        w.end_time,
        w.status,
        w.process_id,
        w.next_station_id,
        w.qty
    FROM 
        workstationworkorder w
    LEFT JOIN 
        workers wo ON w.worker_id = wo.ID
    LEFT JOIN 
        workorders wo2 ON w.work_id = wo2.id
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    columns = ['ID', 'Workstation ID', 'Device ID', 'Worker Name', 'Work Order Data', 
               'Start Time', 'End Time', 'Status', 'Process ID', 'Next Station ID', 'QTY']
    df = pd.DataFrame(rows, columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='UsersData')
        worksheet = writer.sheets['UsersData']
        worksheet.auto_filter.ref = worksheet.dimensions
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='Users_Data.xlsx', 
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
