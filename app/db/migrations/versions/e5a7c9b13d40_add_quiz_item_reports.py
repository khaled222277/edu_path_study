"""add quiz_item_reports (a student flags a bad quiz question)

Revision ID: e5a7c9b13d40
Revises: d4f9b2c7e613
Create Date: 2026-10-31 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a7c9b13d40'
down_revision: Union[str, None] = 'd4f9b2c7e613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('quiz_item_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('attempt_id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['attempt_id'], ['quiz_attempts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['item_id'], ['quiz_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('item_id')
    )
    op.create_index(op.f('ix_quiz_item_reports_attempt_id'), 'quiz_item_reports', ['attempt_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_quiz_item_reports_attempt_id'), table_name='quiz_item_reports')
    op.drop_table('quiz_item_reports')
