from fastapi import FastAPI

app = FastAPI(title="Context Graph MCP Server", version="0.1.0")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    # Placeholder. Later: implement MCP endpoints + tool routing.
    return {"ok": True, "message": "MCP server is running"}

