from routes.auth import login_required, require_roles, MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES
from flask import Blueprint, render_template, session, abort
from utils.helpers import role_required, manager_required


main_bp = Blueprint('main', __name__)

@main_bp.route('/<lang>/')
@require_roles(IT_ROLES)  # manager VAGY teamleader mehet
def index(lang):
    user = session.get('user')
    return render_template(f'{lang}/index.html', user=user)

@main_bp.route('/<lang>/programs', methods=['GET'])
@require_roles(IT_ROLES)  # manager VAGY teamleader mehet
def programs_page(lang):
    if lang not in ('hu', 'sk'):
        abort(404)
    return render_template(f'{lang}/programs.html', user=session.get('user'))