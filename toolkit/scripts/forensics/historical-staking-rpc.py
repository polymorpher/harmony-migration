#!/usr/bin/env python3

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict


CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_polymod(values):
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def hrp_expand(hrp):
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def convert_bits(values, from_bits, to_bits):
    accumulator = 0
    bits = 0
    output = []
    maximum = (1 << to_bits) - 1
    for value in values:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            output.append((accumulator >> bits) & maximum)
    if bits and ((accumulator << (to_bits - bits)) & maximum):
        raise ValueError("nonzero address padding")
    return bytes(output)


def decode_address(value):
    if value != value.lower() or not value.startswith("one1"):
        raise ValueError(f"invalid Harmony address {value!r}")
    separator = value.rfind("1")
    hrp = value[:separator]
    data = [CHARSET.index(char) for char in value[separator + 1 :]]
    if len(data) < 6 or bech32_polymod(hrp_expand(hrp) + data) != 1:
        raise ValueError(f"invalid Harmony address checksum {value!r}")
    decoded = convert_bits(data[:-6], 5, 8)
    if len(decoded) != 20:
        raise ValueError(f"invalid Harmony address length {value!r}")
    return "0x" + decoded.hex()


def rpc(url, method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    for attempt in range(5):
        try:
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result["result"]
        except (OSError, RuntimeError, urllib.error.HTTPError):
            if attempt == 4:
                raise
            time.sleep(2**attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    claims = defaultdict(lambda: [0, 0, 0])
    validators = 0
    page = 0
    while True:
        batch = rpc(
            args.rpc,
            "hmyv2_getAllValidatorInformationByBlockNumber",
            [page, args.block],
        )
        if not batch:
            break
        validators += len(batch)
        for information in batch:
            for delegation in information["validator"]["delegations"]:
                address = decode_address(delegation["delegator-address"])
                claims[address][0] += int(delegation["amount"])
                claims[address][1] += sum(
                    int(item["amount"]) for item in delegation["undelegations"]
                )
                claims[address][2] += int(delegation["reward"])
        print(
            f"block={args.block} page={page} validators={validators}",
            flush=True,
        )
        page += 1

    with open(args.output, "x", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(
            (
                "address",
                "active_delegation_atto",
                "pending_undelegation_atto",
                "unclaimed_reward_atto",
            )
        )
        for address, values in sorted(claims.items()):
            writer.writerow((address, *values))

    totals = [sum(values[index] for values in claims.values()) for index in range(3)]
    print(
        json.dumps(
            {
                "block": args.block,
                "validators": validators,
                "delegators": len(claims),
                "active_delegation_atto": str(totals[0]),
                "pending_undelegation_atto": str(totals[1]),
                "unclaimed_reward_atto": str(totals[2]),
                "output": args.output,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
