from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.subject import Subject
    from app.models.user import User


class University(Base):
    __tablename__ = "universities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    country: Mapped[str] = mapped_column(String(100), nullable=False)

    subjects: Mapped[list["Subject"]] = relationship(
        back_populates="university", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(back_populates="university")

    def __repr__(self) -> str:
        return f"<University id={self.id} name={self.name!r}>"
