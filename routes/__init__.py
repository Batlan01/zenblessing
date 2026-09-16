from .users import users_bp
from .errors import errors_bp
from .api import api_bp
from .teamleaders import teamleaders_bp
from .auth import auth_bp
from .main import main_bp
from .progress import progress_bp
from .bartender import bartender_bp
from .qr_injector import qr_bp
from .qr_logs import qr_log
from .scan_injector import bp_scan
from .human_resources_injector import bp_human_resources
from .database_viewer_injector import dbv_bp
from .tables_injector import bp_tables
from .notification_injector import bp_notify
from .bartender_project_creator_report_injector import bp_bartender_project_report
from .vnc import vnc_bp
from .search_injector import bp_search as bp_search_files
from .ad_users_injector import bp_ad_users
from .tl_settings_injector import bp_tl_settings

from services.qc_bartender_core import qc_bartender_bp
from .qc_bartender_injector import bt_injector_bp
from services.email_core import email_module_bp




def register_blueprints(app):
    app.register_blueprint(users_bp)
    app.register_blueprint(teamleaders_bp)
    app.register_blueprint(errors_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(progress_bp)
    app.register_blueprint(bartender_bp)
    app.register_blueprint(qc_bartender_bp)
    app.register_blueprint(bt_injector_bp)
    app.register_blueprint(qr_bp)
    app.register_blueprint(qr_log)
    app.register_blueprint(bp_scan)
    app.register_blueprint(bp_human_resources)
    app.register_blueprint(dbv_bp)
    app.register_blueprint(bp_tables)
    app.register_blueprint(bp_notify)
    app.register_blueprint(bp_bartender_project_report)
    app.register_blueprint(vnc_bp)
    app.register_blueprint(bp_search_files)
    app.register_blueprint(email_module_bp)
    app.register_blueprint(bp_ad_users)
    app.register_blueprint(bp_tl_settings)