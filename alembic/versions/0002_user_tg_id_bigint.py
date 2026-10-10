"""users.tg_id: Integer -> BigInteger (Telegram ID может быть > 2^31)

Revision ID: 0002_user_tg_id_bigint
Revises: 0001_initial
Create Date: 2026-10-10 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0002_user_tg_id_bigint'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'tg_id', existing_type=sa.Integer(), type_=sa.BigInteger())


def downgrade() -> None:
    op.alter_column('users', 'tg_id', existing_type=sa.BigInteger(), type_=sa.Integer())
