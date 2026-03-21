"""Main app that uses auth module."""

from auth import get_auth_url, get_scopes


def start():
    url = get_auth_url()
    scopes = get_scopes()
    return {"auth_url": url, "scopes": scopes}
