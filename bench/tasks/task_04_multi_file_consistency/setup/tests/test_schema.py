import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from base_schema import User
from models import create_user, user_to_dict
from api import handle_create_user


def test_user_has_age():
    u = User(id=1, name="Alice", email="a@b.com", age=30)
    assert u.age == 30


def test_create_user_with_age():
    u = create_user(id=1, name="Alice", email="a@b.com", age=30)
    assert u.age == 30


def test_user_to_dict_includes_age():
    u = User(id=1, name="Alice", email="a@b.com", age=30)
    d = user_to_dict(u)
    assert d["age"] == 30


def test_api_includes_age():
    result = handle_create_user({"id": 1, "name": "Alice", "email": "a@b.com", "age": 30})
    assert result["age"] == 30
