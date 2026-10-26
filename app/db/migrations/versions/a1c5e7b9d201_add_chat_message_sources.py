"""store the sources shown under each assistant reply

Additive and nullable: existing rows keep NULL, which the API reads as "no sources
recorded" (an empty list), so nothing has to be backfilled.

Revision ID: a1c5e7b9d201
Revises: 13c704704310
Create Date: 2026-10-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c5e7b9d201'
down_revision: Union[str, None] = '13c704704310'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('chat_messages') as batch:
        batch.add_column(sa.Column('sources', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('chat_messages') as batch:
        batch.drop_column('sources')
