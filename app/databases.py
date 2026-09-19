"""Local entry page and isolated, dataset-prefixed Flask applications."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
from hashlib import sha256
import hmac
from pathlib import Path
from secrets import token_hex
from threading import RLock

from flask import Blueprint, Flask, flash, redirect, render_template, request, url_for
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFError
from itsdangerous import BadData, URLSafeSerializer
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool
from werkzeug.wsgi import ClosingIterator
from wtforms import HiddenField, StringField, SubmitField
from wtforms.validators import DataRequired

from app.extensions import csrf, db
from app.health import health_blueprint
from app.local_host import LocalHostOnly
from app.services.local_databases import (
    DatabaseError, checked_path, create_database, discover, identity,
    inspect_database, new_filename, upgrade_database,
)
from app.template_filters import register_filters


class ChooseForm(FlaskForm):
    database = HiddenField(validators=[DataRequired()])
    submit = SubmitField('Open')


class CreateForm(FlaskForm):
    name = StringField('Database filename', validators=[DataRequired(message='Enter a name for the new database.')])
    submit = SubmitField('Create database')


class UpgradeForm(FlaskForm):
    database = HiddenField(validators=[DataRequired()])
    submit = SubmitField('Back up and upgrade')


class DatasetDispatcher:
    """Pin requests to fixed app/engine pairs; evict only idle applications."""

    def __init__(self, launcher, folder, secret, config):
        self.launcher = launcher
        self.folder = folder
        self.secret = secret
        self.serializer = URLSafeSerializer(secret, salt='local-dataset')
        self.config = config
        self.apps = OrderedDict()
        self.lock = RLock()
        self.capacity = 8

    def token(self, candidate):
        return self.serializer.dumps({'filename': candidate.filename, 'identity': candidate.identity})

    def trim(self):
        for token in list(self.apps):
            if len(self.apps) <= self.capacity:
                break
            application, active = self.apps[token]
            if active == 0:
                with application.app_context():
                    db.engine.dispose()
                del self.apps[token]

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if not path.startswith('/d/'):
            return self.launcher(environ, start_response)
        parts = path.split('/', 3)
        token = parts[2]
        try:
            payload = self.serializer.loads(token)
            filename = payload['filename']
            file_path = checked_path(self.folder, filename)
            if identity(file_path) != payload['identity']:
                raise DatabaseError('The database file was replaced.')
            with self.lock:
                if token not in self.apps:
                    candidate = inspect_database(self.folder, filename)
                    if candidate.status not in {'ready', 'empty'}:
                        raise DatabaseError(candidate.reason or 'Review this database in the chooser.')
                    from app import create_portfolio_app
                    dataset_secret = hmac.new(self.secret.encode(), token.encode(), sha256).hexdigest()
                    prefix = '/d/' + token
                    application = create_portfolio_app({
                        **self.config,
                        'SQLALCHEMY_DATABASE_URI': URL.create('sqlite', database=file_path.as_uri(), query={'mode': 'rw', 'uri': 'true'}),
                        'SQLALCHEMY_ENGINE_OPTIONS': {'poolclass': NullPool},
                        'SECRET_KEY': dataset_secret,
                        'SESSION_COOKIE_NAME': 'lt_' + sha256(token.encode()).hexdigest()[:16],
                        'SESSION_COOKIE_PATH': prefix,
                        'RESTORE_STAGING_DIRECTORY': str(self.folder / 'restore-staging' / sha256(token.encode()).hexdigest()),
                        'DATASET_FILENAME': filename,
                        'DATASET_PATH': str(file_path),
                    })
                    @application.context_processor
                    def dataset_context():
                        return {'dataset_filename': filename, 'dataset_path': str(file_path), 'database_chooser_url': '/'}

                    @application.errorhandler(CSRFError)
                    def form_expired(error):
                        return render_template('databases/form_expired.html'), 400

                    @application.after_request
                    def private_response(response):
                        response.headers['Cache-Control'] = 'no-store'
                        response.headers['Referrer-Policy'] = 'no-referrer'
                        return response

                    self.apps[token] = [application, 0]
                application, _ = self.apps[token]
                self.apps[token][1] += 1
                self.apps.move_to_end(token)
                self.trim()
        except (BadData, KeyError, TypeError, OSError, DatabaseError):
            fallback = dict(environ)
            fallback['PATH_INFO'] = '/selection-expired'
            return self.launcher(fallback, start_response)

        child_environ = dict(environ)
        child_environ['SCRIPT_NAME'] = environ.get('SCRIPT_NAME', '') + '/d/' + token
        child_environ['PATH_INFO'] = '/' + parts[3] if len(parts) == 4 else '/'

        def release():
            with self.lock:
                self.apps[token][1] -= 1
                self.trim()

        try:
            response = application(child_environ, start_response)
        except BaseException:
            release()
            raise
        return ClosingIterator(response, release)


def create_launcher(config=None):
    application = Flask(__name__, instance_relative_config=True)
    # A fresh process key makes old dataset links require intentional re-selection.
    application.config.from_mapping(SECRET_KEY=token_hex(32), SESSION_COOKIE_NAME='lt_chooser')
    application.config.update(config or {})
    folder = Path(application.config.get('LOCAL_DATABASE_DIRECTORY', application.instance_path)).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    csrf.init_app(application)
    application.register_blueprint(health_blueprint)
    presentation = Blueprint('database_presentation', __name__)
    register_filters(presentation)
    application.register_blueprint(presentation)
    application.jinja_env.globals['static_url'] = lambda filename: url_for('static', filename=filename)
    dispatcher = DatasetDispatcher(application.wsgi_app, folder, token_hex(32), dict(config or {}))
    application.wsgi_app = LocalHostOnly(dispatcher)
    application.extensions['datasets'] = dispatcher
    preview_signer = URLSafeSerializer(application.secret_key, salt='database-preview')

    def candidate_from_form(form):
        if not form.validate_on_submit():
            raise DatabaseError('This selection expired. Refresh the list and choose again.')
        try:
            selected = preview_signer.loads(form.database.data)
            candidate = inspect_database(folder, selected['filename'])
            if candidate.identity != selected['identity'] or candidate.revision != selected['revision']:
                raise DatabaseError('This file changed since it was listed. Refresh and choose again.')
            return candidate
        except (BadData, KeyError, TypeError) as error:
            raise DatabaseError('This selection expired. Refresh the list and choose again.') from error

    def enter(candidate):
        return redirect('/d/' + dispatcher.token(candidate) + '/', code=303)

    @application.after_request
    def no_store(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @application.get('/')
    def chooser():
        entries = []
        folder_error = None
        try:
            for candidate in discover(folder):
                entry = asdict(candidate)
                entry['form'] = ChooseForm(database=preview_signer.dumps({
                    'filename': candidate.filename, 'identity': candidate.identity, 'revision': candidate.revision,
                }))
                entries.append(entry)
        except DatabaseError as error:
            folder_error = str(error)
        return render_template('databases/index.html', entries=entries, folder=str(folder), folder_error=folder_error)

    @application.post('/databases/open')
    def open_database():
        try:
            candidate = candidate_from_form(ChooseForm())
            if candidate.status == 'upgrade':
                form = UpgradeForm(database=ChooseForm().database.data)
                return render_template('databases/upgrade.html', candidate=candidate, form=form, folder=str(folder))
            if candidate.status not in {'ready', 'empty'}:
                raise DatabaseError(candidate.reason)
            return enter(candidate)
        except DatabaseError as error:
            flash(str(error), 'error')
            return redirect(url_for('chooser'), code=303)

    @application.route('/databases/new', methods=['GET', 'POST'])
    def new_database():
        form = CreateForm()
        if form.validate_on_submit():
            try:
                # A preview step makes the resulting filename reviewable without JS.
                filename = new_filename(form.name.data)
                if request.form.get('confirm') != 'yes' or request.form.get('preview_filename') != filename:
                    return render_template('databases/create.html', form=form, filename=filename, folder=str(folder))
                filename = create_database(folder, form.name.data)
                return enter(inspect_database(folder, filename))
            except DatabaseError as error:
                form.name.errors.append(str(error))
        return render_template('databases/create.html', form=form, folder=str(folder)), (400 if form.errors else 200)

    @application.post('/databases/upgrade')
    def upgrade_selected():
        try:
            candidate = candidate_from_form(UpgradeForm())
            backup = upgrade_database(folder, candidate.filename, candidate.identity, candidate.revision)
            flash(f'{candidate.filename} is ready. Your recovery copy is saved at {backup}. Open the database to continue.', 'success')
        except DatabaseError as error:
            flash(str(error), 'error')
        return redirect(url_for('chooser'), code=303)

    @application.route('/selection-expired', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    def selection_expired():
        return render_template('databases/expired.html'), (409 if request.method != 'GET' else 200)

    @application.errorhandler(CSRFError)
    def expired_form(error):
        return render_template('databases/expired.html'), 400

    @application.errorhandler(404)
    def no_dataset(error):
        if request.method == 'GET':
            return redirect(url_for('chooser'))
        return render_template('databases/expired.html'), 409

    return application
