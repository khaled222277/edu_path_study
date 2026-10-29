"""add allowed_emails (pilot sign-in allowlist)

Revision ID: d4f9b2c7e613
Revises: c3e8a1d94f52
Create Date: 2026-10-29 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f9b2c7e613'
down_revision: Union[str, None] = 'c3e8a1d94f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('allowed_emails',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('email')
    )


def downgrade() -> None:
    op.drop_table('allowed_emails')
