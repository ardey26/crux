import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth import get_auth_url, get_scopes


def test_auth_url_uses_config():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")) as f:
        config = json.load(f)
    url = get_auth_url()
    assert config["auth"]["client_id"] in url
    assert config["auth"]["redirect_uri"] in url


def test_scopes_match_config():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")) as f:
        config = json.load(f)
    assert get_scopes() == config["auth"]["scopes"]
