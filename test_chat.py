"""
Chat with your assistant from the terminal, like a client would.
    python test_chat.py https://your-service.onrender.com YOUR_TEST_KEY
Type messages, press Enter. Ctrl+C to quit.
"""
import sys
import uuid

import requests

if len(sys.argv) < 3:
    sys.exit("Usage: python test_chat.py <server-url> <TEST_KEY>")
url, key = sys.argv[1].rstrip("/") + "/test", sys.argv[2]
user = "test-" + uuid.uuid4().hex[:6]   # new fake client each run
print("You are a client writing to the specialist. Ctrl+C to quit.\n")
while True:
    text = input("Client: ").strip()
    if not text:
        continue
    r = requests.post(url, json={"text": text, "user": user}, headers={"X-Test-Key": key}, timeout=60)
    if not r.ok:
        print("Error", r.status_code, r.text[:200])
        continue
    d = r.json()
    flag = "   [→ sent to master in Telegram]" if d.get("handed_to_master") else ""
    print(f"Assistant: {d['reply']}{flag}\n")
