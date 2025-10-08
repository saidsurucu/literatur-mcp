"""
Modal deployment for DergiPark API
Run: modal deploy modal_app.py
"""
import modal
import os

# Create Modal app
app = modal.App("dergipark-api")

# Create persistent volume for cookies
volume = modal.Volume.from_name("dergipark-cookies", create_if_missing=True)

# Define the Docker image with all dependencies
# Use Microsoft's official Playwright Python image as base (same as Dockerfile)
image = (
    modal.Image.from_registry("mcr.microsoft.com/playwright/python:v1.51.0-jammy", add_python="3.11")
    .pip_install_from_requirements("requirements.txt")
    .add_local_file("main.py", remote_path="/root/main.py", copy=True)
    .add_local_dir("gizlilik", remote_path="/root/gizlilik")
)

# Mount volume at /data for cookie persistence
@app.function(
    image=image,
    cpu=1.0,
    memory=2048,
    timeout=900,  # 15 minutes max per request
    volumes={"/data": volume},
    secrets=[
        modal.Secret.from_name("dergipark-secrets")  # Will contain CAPSOLVER_API_KEY, etc.
    ]
)
@modal.asgi_app()
def fastapi_app():
    """FastAPI app wrapped for Modal"""
    import sys
    sys.path.insert(0, "/root")

    from main import app as fastapi_app

    # Override cookie path to use Modal volume
    import main
    main.COOKIES_FILE_PATH = "/data/cookies_persistent.pkl"

    return fastapi_app


@app.local_entrypoint()
def main():
    """Local testing entrypoint"""
    print("DergiPark API deployed to Modal!")
    print("Run 'modal serve modal_app.py' for local testing")
    print("Run 'modal deploy modal_app.py' for production deployment")
