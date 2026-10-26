"""add message_feedback (thumbs up/down per assistant reply)

Revision ID: b7d2f4a6c308
Revises: a1c5e7b9d201
Create Date: 2026-10-26 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2f4a6c308'
down_revision: Union[str, None] = 'a1c5e7b9d201'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('message_feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('rating', sa.SmallInteger(), nullable=False),
    sa.Column('comment', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id', 'user_id', name='uq_feedback_message_user')
    )
    op.create_index(op.f('ix_message_feedback_message_id'), 'message_feedback', ['message_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_message_feedback_message_id'), table_name='message_feedback')
    op.drop_table('message_feedback')
