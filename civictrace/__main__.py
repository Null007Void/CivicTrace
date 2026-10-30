"""Run with python -m civictrace (local only, no inference unless explicit opt-in)."""
import uvicorn

uvicorn.run('civictrace.app:app', host='127.0.0.1', port=8765, reload=False)
