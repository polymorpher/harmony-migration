#!/usr/bin/env python3
"""Send one EOA calldata file of a release plan (`1-release/<chain>-<owner>-eoa.json`) from the
owner's own wallet, one transaction at a time, with Foundry's `cast`.

    python3 send_calldata.py <file> --rpc-url <RPC of the file's chain> [--dry-run] -- <wallet options>

<wallet options> go to `cast send` unchanged, for example `--ledger`, `--trezor`, or
`--account <keystore name>`. The file's `from` address is passed as `--from`, so `cast` refuses a
wallet that signs as anyone else.

Before sending it checks that the RPC serves the file's chain and that `from` has no pending
transactions. Then, in file order, it simulates each transaction from `from` against the latest
block, sends it only if the simulation succeeds, and waits for a successful receipt before the next.
The first failure stops the run. Each confirmed transaction is appended to `<file>.sent.json`;
running the same command again resumes after the last one, and refuses to if `from` has sent
anything since (an interrupted run, or another wallet action). With `--dry-run` it lists the
transactions, simulates the next one, and sends nothing.

Standard library only, plus `cast` on PATH.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "send_calldata"})
    with urllib.request.urlopen(request, timeout=60) as response:
        reply = json.load(response)
    if "error" in reply:
        error = reply["error"]
        raise RuntimeError(f"{error.get('message')} {error.get('data') or ''}".strip())
    return reply["result"]


def load_plan(path):
    with open(path, "rb") as source:
        raw = source.read()
    plan = json.loads(raw)
    for key in ("chain", "chain_id", "from", "transactions"):
        if key not in plan:
            raise SystemExit(f"{path}: not a release calldata file (no `{key}`)")
    return plan, hashlib.sha256(raw).hexdigest()


def load_log(path, file_sha256, plan):
    """The confirmed transactions of an earlier run of this exact file, or a fresh log."""
    if not os.path.exists(path):
        return {"file_sha256": file_sha256, "chain_id": plan["chain_id"], "from": plan["from"], "sent": []}
    with open(path) as source:
        log = json.load(source)
    if log.get("file_sha256") != file_sha256:
        raise SystemExit(f"{path} records a different version of the file; keep it and ask for help")
    return log


def save_log(path, log):
    partial = path + ".partial"
    with open(partial, "w") as out:
        json.dump(log, out, indent=1)
        out.write("\n")
    os.replace(partial, path)


def simulate(url, sender, tx):
    """None if the call succeeds from `sender` at the latest block, else the node's error."""
    call = {"from": sender, "to": tx["to"], "data": tx["data"], "value": hex(int(tx["value"]))}
    try:
        rpc(url, "eth_call", [call, "latest"])
        return None
    except RuntimeError as error:
        return str(error)


def send(url, sender, tx, wallet):
    """Sign and send with `cast send`, wait for the receipt, and return it."""
    command = ["cast", "send", tx["to"], tx["data"], "--value", str(int(tx["value"])), "--from", sender,
               "--rpc-url", url, "--json", *wallet]
    result = subprocess.run(command, stdout=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise SystemExit("cast send failed (see its error above); nothing more was sent")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main():
    argv = sys.argv[1:]
    wallet = []
    if "--" in argv:
        split = argv.index("--")
        argv, wallet = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file")
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    plan, file_sha256 = load_plan(args.file)
    sender = plan["from"].lower()
    txs = plan["transactions"]
    log_path = args.file + ".sent.json"
    log = load_log(log_path, file_sha256, plan)
    start = len(log["sent"])

    chain_id = int(rpc(args.rpc_url, "eth_chainId", []), 16)
    if chain_id != int(plan["chain_id"]):
        raise SystemExit(f"the RPC serves chain {chain_id}; this file is for {plan['chain']} ({plan['chain_id']})")
    if log["sent"]:
        last = log["sent"][-1]
        receipt = rpc(args.rpc_url, "eth_getTransactionReceipt", [last["tx_hash"]])
        if not receipt or int(receipt["status"], 16) != 1:
            raise SystemExit(f"logged transaction {last['index']} ({last['tx_hash']}) is not confirmed on chain")
    mined = int(rpc(args.rpc_url, "eth_getTransactionCount", [sender, "latest"]), 16)
    pending = int(rpc(args.rpc_url, "eth_getTransactionCount", [sender, "pending"]), 16)
    if pending != mined:
        raise SystemExit(f"{sender} has {pending - mined} pending transaction(s); wait until they are mined")
    if log["sent"] and mined != log["sent"][-1]["nonce"] + 1:
        raise SystemExit(f"{sender} sent {mined - log['sent'][-1]['nonce'] - 1} transaction(s) after the last "
                         "logged one (an interrupted run, or another wallet action); check them before resuming")

    print(f"{plan['chain']} (chain {chain_id}), sender {sender}, file sha256 {file_sha256}")
    print(f"{len(txs)} transactions; {start} already confirmed (log: {log_path})")
    for i, tx in enumerate(txs):
        mark = "done" if i < start else "    "
        print(f"  [{mark}] {i:3d}  {tx['to']}  {tx.get('note', '')}")
    if start == len(txs):
        print("every transaction is confirmed")
        return

    if args.dry_run:
        error = simulate(args.rpc_url, sender, txs[start])
        print(f"simulation of transaction {start}: {'ok' if error is None else 'FAILED: ' + error}")
        print("dry run: nothing sent")
        return

    for i in range(start, len(txs)):
        tx = txs[i]
        print(f"\n[{i}/{len(txs) - 1}] {tx.get('note', '')}")
        error = simulate(args.rpc_url, sender, tx)
        if error is not None:
            raise SystemExit(f"simulation failed, nothing sent: {error}\nresume later with the same command")
        receipt = send(args.rpc_url, sender, tx, wallet)
        status = int(str(receipt.get("status", "0x0")), 16)
        tx_hash = receipt.get("transactionHash")
        if receipt.get("from", "").lower() != sender:
            raise SystemExit(f"transaction {tx_hash} was sent by {receipt.get('from')}, not {sender}; stop")
        if status != 1:
            raise SystemExit(f"transaction {i} ({tx_hash}) reverted on chain; stop and report it")
        nonce = int(rpc(args.rpc_url, "eth_getTransactionByHash", [tx_hash])["nonce"], 16)
        log["sent"].append({"index": i, "tx_hash": tx_hash, "nonce": nonce,
                            "block": int(str(receipt["blockNumber"]), 16), "note": tx.get("note", "")})
        save_log(log_path, log)
        print(f"  confirmed {tx_hash} in block {log['sent'][-1]['block']}")
    print(f"\nall {len(txs)} transactions confirmed; log: {log_path}")


if __name__ == "__main__":
    main()
