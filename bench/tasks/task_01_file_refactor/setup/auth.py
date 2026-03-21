"""Auth module — uses hardcoded values that should come from config."""


def get_auth_url():
    provider = "oauth2"
    client_id = "HARDCODED_ID"
    redirect = "http://localhost:8080/callback"
    return f"https://auth.example.com/{provider}?client_id={client_id}&redirect_uri={redirect}"


def get_scopes():
    return ["read", "write"]
