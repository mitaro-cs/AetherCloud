import os

from aethercloud import create_app


def normalized_public_url(raw_url):
    value = (raw_url or "").strip().rstrip("/")
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value


if __name__ == "__main__":
    app = create_app()
    host = app.config["APP_HOST"]
    port = app.config["APP_PORT"]
    debug_flag = os.getenv("AETHER_DEBUG", "1").strip().lower()
    debug = debug_flag not in {"0", "false", "no", "off"}
    local_url = f"http://{host}:{port}"
    public_url = normalized_public_url(app.config["TUNA_PUBLIC_URL"])

    print(f"[AetherCloud] Local URL: {local_url}")
    if public_url:
        print(f"[AetherCloud] Public URL: {public_url}")

    app.run(debug=debug, host=host, port=port)
