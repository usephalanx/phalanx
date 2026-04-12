"""FastAPI application entry point.

Provides a single GET / endpoint returning a greeting and version info.
"""

from fastapi import FastAPI

app = FastAPI(title="FORGE API", version="1.0.0")


@app.get("/")
async def root() -> dict[str, str]:
    """Return a greeting message and the current API version."""
    return {"message": "Hello, World!", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
