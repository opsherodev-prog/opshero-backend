"""
start.py — Lance uvicorn en passant une socket pré-bindée.
Contourne le bug Windows [Errno 10048] de uvicorn 0.38 où la socket
est bindée APRÈS le lifespan startup.

Usage:
    python start.py
"""
import socket
import asyncio
import uvicorn

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 8000))
sock.set_inheritable(True)

print("[start.py] Socket bound to 127.0.0.1:8000")

config = uvicorn.Config("main:app", host="127.0.0.1", port=8000, log_level="info")
server = uvicorn.Server(config)

asyncio.run(server.serve(sockets=[sock]))
