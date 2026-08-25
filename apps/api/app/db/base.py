"""Declarative base. Every model module must import this Base and be
imported from app/models/__init__.py so Alembic's autogenerate (env.py)
can see it via Base.metadata.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
