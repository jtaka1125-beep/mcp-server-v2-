"""Smoke test for classify_screen via MCP V2 JSON-RPC.
Runs 3 cases (ax_only / image_only / ax_priority) and checks acceptance gates.
"""
import json
import time
import urllib.request

MCP_URL = "http://127.0.0.1:3001/"
# AX summary string mirroring tonight's _test_api3.py TEXT_USER (proven baseline).
AX_SUMMARY = (
    'AX tree: FrameLayout root > FrameLayout ia_clickable_close_button clickable=true > '
    'TextView ad_label text="AD" > Button install_button text="インストール" > '
    'Activity=TTFullScreenVideoActivity pkg=com.kurashiru.box.merge. Classify.'
)
IMG_PATH = r"C:\MirageWork\bonsai\x1_now_test.png"

GATES = {
    "ax_only":      {"max_ms": 4000,  "min_conf": 0.90, "expect_screen": "ad", "expect_source": "ax_only"},
    "image_only":   {"max_ms": 12000, "min_conf": 0.90, "expect_screen": "ad", "expect_source": "image_only"},
    "ax_priority":  {"max_ms": 4000,  "min_conf": 0.90, "expect_screen": "ad", "expect_source": "ax_priority"},
}


def call_mcp(args: dict, http_timeout: int = 60) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "classify_screen", "arguments": args},
    }
    t0 = time.monotonic()
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=http_timeout) as r:
        body = r.read()
    wall_ms = int((time.monotonic() - t0) * 1000)
    rpc = json.loads(body)
    text = rpc["result"]["content"][0]["text"]
    return {"wall_ms": wall_ms, "is_error": rpc["result"].get("isError", False), "data": json.loads(text)}


def check(case: str, res: dict) -> tuple[bool, list]:
    g = GATES[case]
    fails = []
    if res["is_error"]:
        return False, [f"isError=True: {res['data']}"]
    d = res["data"]
    if d.get("screen") != g["expect_screen"]:
        fails.append(f"screen={d.get('screen')!r} != {g['expect_screen']!r}")
    if (d.get("confidence") or 0) < g["min_conf"]:
        fails.append(f"confidence={d.get('confidence')} < {g['min_conf']}")
    if d.get("source") != g["expect_source"]:
        fails.append(f"source={d.get('source')!r} != {g['expect_source']!r}")
    if (d.get("elapsed_ms") or 0) >= g["max_ms"]:
        fails.append(f"elapsed_ms={d.get('elapsed_ms')} >= {g['max_ms']}")
    return len(fails) == 0, fails


def main():
    cases = [
        ("ax_only",     {"ax_dump": AX_SUMMARY}),
        ("image_only",  {"image_path": IMG_PATH}),
        ("ax_priority", {"ax_dump": AX_SUMMARY, "image_path": IMG_PATH}),
    ]
    print(f"AX summary bytes: {len(AX_SUMMARY)}")
    print("=" * 70)
    all_ok = True
    for name, args in cases:
        try:
            res = call_mcp(args)
        except Exception as e:
            print(f"[{name}] EXCEPTION: {e}")
            all_ok = False
            continue
        ok, fails = check(name, res)
        all_ok &= ok
        verdict = "PASS" if ok else "FAIL"
        d = res["data"]
        print(f"[{name}] {verdict}  wall={res['wall_ms']}ms  internal={d.get('elapsed_ms')}ms")
        print(f"  data: screen={d.get('screen')!r} conf={d.get('confidence')} source={d.get('source')!r}")
        if d.get("raw"):
            print(f"  raw: {d.get('raw')!r}")
        if fails:
            for f_ in fails:
                print(f"  - FAIL: {f_}")
        print("-" * 70)
    print("OVERALL:", "PASS" if all_ok else "FAIL")


if __name__ == "__main__":
    main()
