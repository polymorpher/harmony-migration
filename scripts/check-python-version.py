#!/usr/bin/env python3

import sys


MINIMUM = (3, 12)


def main():
    current = sys.version_info[:3]
    if current < MINIMUM:
        required = ".".join(str(value) for value in MINIMUM)
        actual = ".".join(str(value) for value in current)
        raise SystemExit(
            f"Python {required} or newer is required; found {actual}"
        )
    print(
        "PASS Python version: "
        + ".".join(str(value) for value in current)
    )


if __name__ == "__main__":
    main()
