"""Models that depend on base_schema."""

from base_schema import User


def create_user(id: int, name: str, email: str) -> User:
    return User(id=id, name=name, email=email)


def user_to_dict(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email}
