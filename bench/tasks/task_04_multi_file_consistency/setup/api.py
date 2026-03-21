"""API layer that depends on models."""

from models import create_user, user_to_dict


def handle_create_user(data: dict) -> dict:
    user = create_user(
        id=data["id"],
        name=data["name"],
        email=data["email"],
    )
    return user_to_dict(user)
