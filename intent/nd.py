"""Minimal client for the pi nodriver-browser worker socket (stdlib only)."""
import json, os, socket, itertools

SOCKET = os.environ.get("PI_NODRIVER_SOCKET", os.path.expanduser("~/.pi/agent/nodriver-browser.sock"))
MARKER = "__PI_NODRIVER__"
_ids = itertools.count(int.from_bytes(os.urandom(3), "big"))


def request(command: str, session_id: str, timeout: float = 90.0) -> dict:
    rid = next(_ids)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(SOCKET)
        s.sendall((json.dumps({"id": rid, "command": command, "sessionId": session_id}) + "\n").encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("browser daemon closed the connection")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace")
                if not text.startswith(MARKER):
                    continue
                resp = json.loads(text[len(MARKER):])
                if resp.get("id") == rid:
                    return resp


if __name__ == "__main__":
    import sys
    r = request(sys.argv[2], sys.argv[1])
    print(json.dumps({k: v for k, v in r.items() if k != "text"}, ensure_ascii=False))
    print(r.get("text") or r.get("error"))
