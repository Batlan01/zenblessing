from routes.auth import login_required, require_roles, MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES, QUALITY_TEAMLEADER_ROLES
from flask import Blueprint, render_template, session, request
from utils.helpers import role_required

progress_bp = Blueprint('progress', __name__)

@progress_bp.route('/<lang>/wo_progress')
@require_roles(MANAGER_ROLES, IT_ROLES, QUALITY_TEAMLEADER_ROLES)  # manager / IT / Quality Team Leader
def wo_progress(lang):
    user = session.get('user')
    return render_template(f'{lang}/wo_progress.html', user=user)



@progress_bp.route('/<lang>/users_progress')
@require_roles(MANAGER_ROLES, IT_ROLES)  # manager VAGY teamleader mehet
def users_progress(lang):
    return render_template(f'{lang}/users_progress.html', user=session.get('user'))