"""LookThrough Flask application factory."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from flask import Flask

from app.extensions import csrf, db, migrate
from app.health import health_blueprint
from app.local_host import LocalHostOnly
from app import models as models
from app.activity import activity_blueprint
from app.accounts import accounts_blueprint
from app.current_positions import positions_blueprint
from app.dividends import dividends_blueprint
from app.fixed_deposits import fixed_deposits_blueprint
from app.instruments import instruments_blueprint
from app.institutions import institutions_blueprint
from app.maintenance import maintenance_blueprint
from app.fx_settings import fx_settings_blueprint
from app.overview import overview_blueprint
from app.planning import planning_blueprint
from app.retirement import retirement_blueprint
from app.monte_carlo import monte_carlo_blueprint
from app.relationships import relationships_blueprint
from app.shell import shell_blueprint
from app.setup import setup_blueprint
from app.snapshots import snapshots_blueprint


def create_app(test_config: Mapping[str, Any] | None = None) -> Flask:
    """Launch the chooser; explicit database configurations serve one dataset."""

    if not test_config or "SQLALCHEMY_DATABASE_URI" not in test_config:
        from app.databases import create_launcher

        return create_launcher(test_config)
    return create_portfolio_app(test_config)


def create_portfolio_app(test_config: Mapping[str, Any]) -> Flask:
    """Create an isolated application bound permanently to one database."""

    app = Flask(__name__, instance_relative_config=True)
    default_database_path = Path(app.instance_path) / "lookthrough.sqlite3"

    app.config.from_mapping(
        SECRET_KEY=os.environ.get(
            "LOOKTHROUGH_SECRET_KEY", "lookthrough-local-development-only"
        ),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{default_database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CURRENT_DATE_PROVIDER=date.today,
        RESTORE_STAGING_DIRECTORY=str(Path(app.instance_path) / "restore-staging"),
    )

    if test_config is not None:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True, render_as_batch=True)
    csrf.init_app(app)

    app.register_blueprint(health_blueprint)
    app.register_blueprint(shell_blueprint)
    app.register_blueprint(setup_blueprint)
    app.register_blueprint(positions_blueprint)
    app.register_blueprint(overview_blueprint)
    app.register_blueprint(planning_blueprint)
    app.register_blueprint(retirement_blueprint)
    app.register_blueprint(monte_carlo_blueprint)
    app.register_blueprint(activity_blueprint)
    app.register_blueprint(dividends_blueprint)
    app.register_blueprint(fixed_deposits_blueprint)
    app.register_blueprint(accounts_blueprint)
    app.register_blueprint(relationships_blueprint)
    app.register_blueprint(instruments_blueprint)
    app.register_blueprint(institutions_blueprint)
    app.register_blueprint(maintenance_blueprint)
    app.register_blueprint(fx_settings_blueprint)
    app.register_blueprint(snapshots_blueprint)

    app.wsgi_app = LocalHostOnly(app.wsgi_app)
    return app
