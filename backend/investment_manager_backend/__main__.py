from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "investment_manager_backend.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("UVICORN_RELOAD") == "1",
    )


if __name__ == "__main__":
    main()
