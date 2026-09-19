"""Forms for explicit immutable portfolio snapshots."""

from flask_wtf import FlaskForm
from wtforms import BooleanField, DateField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional


NOTE_MAX_LENGTH = 1000


class SnapshotForm(FlaskForm):
    as_of_date = DateField(
        "Snapshot date", validators=[DataRequired()], format="%Y-%m-%d"
    )
    note = TextAreaField(
        "Note", validators=[Optional(), Length(max=NOTE_MAX_LENGTH)]
    )
    confirm_snapshot = BooleanField(
        "Save this frozen snapshot",
        validators=[DataRequired(message="Confirm before saving the snapshot.")],
    )
    save_snapshot = SubmitField("Save snapshot")
