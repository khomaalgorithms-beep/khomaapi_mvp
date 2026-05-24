import os
import urllib.parse

def build_tradovate_login():

    client_id = os.getenv("TRADOVATE_CLIENT_ID")

    redirect_uri = os.getenv(
        "TRADOVATE_REDIRECT_URI"
    )

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri
    })

    return f"https://trader.tradovate.com/oauth/authorize?{params}"