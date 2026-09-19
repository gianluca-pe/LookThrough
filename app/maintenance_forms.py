"""Settings, backup, and restore forms."""

from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import BooleanField, HiddenField, SubmitField
from wtforms.validators import DataRequired


class RestoreUploadForm(FlaskForm):
    backup_file = FileField("LookThrough JSON backup", validators=[DataRequired()])
    submit = SubmitField("Validate backup")


class RestoreConfirmForm(FlaskForm):
    restore_token = HiddenField(validators=[DataRequired()])
    confirm_restore = BooleanField(
        "I understand this will replace the current local data",
        validators=[DataRequired(message="Confirm before restoring the backup.")],
    )
    submit = SubmitField("Restore this backup")
