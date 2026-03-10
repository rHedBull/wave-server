"""Entry point for running the server directly: python main.py"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("wave_server.main:app", host="0.0.0.0", port=9718, reload=True)
