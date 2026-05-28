"""
快速诊断脚本：测试 Binance WebSocket 流
"""
import json
import websocket

URL = "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker"

count = {"aggTrade": 0, "bookTicker": 0, "other": 0}

def on_message(ws, msg):
    try:
        data = json.loads(msg)
        stream = data.get("stream", "unknown")
        inner = data.get("data", {})
        etype = inner.get("e", "?")
        if etype in count:
            count[etype] += 1
        else:
            count["other"] += 1
            print(f"\n[UNKNOWN] {msg[:200]}")
        if sum(count.values()) % 100 == 0:
            print(f"\raggTrade={count['aggTrade']}  bookTicker={count['bookTicker']}  other={count['other']}", end="", flush=True)
    except Exception as e:
        print(f"\n[PARSE ERROR] {e}: {msg[:200]}")

def on_error(ws, err):
    print(f"\n[ERROR] {err}")

def on_open(ws):
    print(f"Connected to {URL}")

def on_close(ws, code, msg):
    print(f"\n[CLOSED] {code} {msg}")

print(f"Testing: {URL}")
ws = websocket.WebSocketApp(URL, on_message=on_message, on_error=on_error,
                            on_open=on_open, on_close=on_close)
try:
    ws.run_forever(ping_interval=30, ping_timeout=10)
except KeyboardInterrupt:
    print(f"\n\nFinal: aggTrade={count['aggTrade']}  bookTicker={count['bookTicker']}  other={count['other']}")
    ws.close()
