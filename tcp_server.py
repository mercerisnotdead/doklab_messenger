# tcp_server.py
# Asyncio TCP chat server (standard library only).
# Run: python tcp_server.py --host 0.0.0.0 --port 8888
import asyncio
import argparse

clients = {}  # writer -> name

async def broadcast(msg: str, except_writer=None):
    dead = []
    for w in list(clients.keys()):
        if w is except_writer:
            continue
        try:
            w.write((msg + "\n").encode("utf-8"))
            await w.drain()
        except Exception:
            dead.append(w)
    for w in dead:
        await disconnect(w)

async def disconnect(writer: asyncio.StreamWriter):
    name = clients.pop(writer, None)
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    if name:
        await broadcast(f"🟡 {name} вышел из чата")

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    try:
        writer.write("Введите ваше имя: ".encode("utf-8"))
        await writer.drain()
        name_bytes = await reader.readline()
        if not name_bytes:
            writer.close()
            return
        name = name_bytes.decode("utf-8", errors="replace").strip() or f"{addr[0]}:{addr[1]}"
        clients[writer] = name
        await broadcast(f"🟢 {name} вошёл в чат")
        writer.write("Теперь можно писать сообщения. Для выхода — /quit\n".encode("utf-8"))
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text == "/quit":
                break
            await broadcast(f"💬 {name}: {text}")
    except Exception:
        pass
    finally:
        await disconnect(writer)

async def main(host: str, port: int):
    server = await asyncio.start_server(handle_client, host, port)
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    print(f"Сервер запущен на {sockets}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        print("\nОстановка сервера...")