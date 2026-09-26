"""Shared random-sample verification core for frozen evidence bundles.

The claim formulas, schemas, totals, and cross-file rules live in rules.json;
this module and sample_core.js interpret the same file. Keep procedural code
here limited to reading files, hashing, sampling, identity checks, and
evidence comparison, and mirror every change in sample_core.js.

All amounts are exact integers in atto-ONE. Decimal ONE and USD strings are
only ever derived from integers and compared as text; they are never parsed
back into calculation inputs.
"""

import csv
import hashlib
import heapq
import importlib.util
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VERIFIER_DIR = Path(__file__).resolve().parent
RULES_PATH = VERIFIER_DIR / "rules.json"
SNAPSHOT_PATH = ROOT / "manifests" / "snapshot-2026-09-10.json"
MANIFEST_NAME = "bundle-manifest.json"

PASS = "PASS"
FAIL = "FAIL"
NOT_VERIFIED = "NOT VERIFIED"
WARNING = "WARNING"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INPUT = 2
EXIT_INCOMPLETE = 3

MAX_EXAMPLES = 20
MAX_JSON_INT = 2**53 - 1

# Claim CSVs can carry long evidence text; never let the csv module's default
# field limit turn a valid file into a parse failure.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Reuse the post-WONE verifier's exact display formulas and identity helper.
WONE = _load_module(
    "verify_wone_allocation_for_sample",
    ROOT / "toolkit" / "scripts" / "claims" / "verify-wone-allocation.py",
)
lib = WONE.lib
sys.path.insert(0, str(ROOT / "airdrop" / "tools"))
import common as airdrop_common  # noqa: E402
import verify as airdrop_verify  # noqa: E402


class BundleError(ValueError):
    """The bundle cannot be read at all (exit code 2)."""


class EvalError(ValueError):
    pass


class MissingRow(EvalError):
    pass


# ---------------------------------------------------------------------------
# strict JSON
# ---------------------------------------------------------------------------


def parse_json_strict(data, label):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label}: duplicate JSON key {key!r}")
            value[key] = item
        return value

    def reject_constant(name):
        raise ValueError(f"{label}: non-finite JSON number {name}")

    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"{label}: not UTF-8: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{label}: invalid JSON: {error}") from error


def load_rules(path=RULES_PATH):
    return parse_json_strict(Path(path).read_bytes(), "rules")


def load_snapshot(path=SNAPSHOT_PATH):
    return parse_json_strict(Path(path).read_bytes(), "snapshot")


def file_sha256(path):
    return WONE.file_sha256(Path(path))


def json_int(value):
    """A JSON integer both engines read exactly (no bool, float, or >2^53)."""
    return type(value) is int and 0 <= value <= MAX_JSON_INT


def normalized_path(text):
    """True for a relative POSIX path with no empty, '.', or '..' segments."""
    return (
        isinstance(text, str)
        and bool(text)
        and "\\" not in text
        and all(part not in ("", ".", "..") for part in text.split("/"))
    )


def strict_csv_reader(handle):
    """csv.reader in strict mode that also rejects NUL on every Python version."""
    for record in csv.reader(handle, strict=True):
        if any("\0" in value for value in record):
            raise csv.Error("line contains NUL")
        yield record


def validate_seed(seed):
    if not isinstance(seed, str) or not seed:
        raise BundleError("the seed must be non-empty text")
    if seed != seed.strip():
        raise BundleError("the seed must not start or end with whitespace")
    return seed


def hash_and_utf8_error(path):
    """SHA-256 of a file plus the first UTF-8 decoding error, if any."""
    import codecs

    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    error = None
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            if error is None:
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError as exc:
                    error = str(exc)
    if error is None:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            error = str(exc)
    return digest.hexdigest(), error


def fixed18(value):
    return WONE.fixed(value)


def usd(amount, price):
    return WONE.usd_value(amount, price, "usd")


def display_one(value):
    """Exact decimal ONE rendering of an atto integer, for people."""
    if not isinstance(value, int):
        return str(value)
    sign = "-" if value < 0 else ""
    return sign + fixed18(abs(value))


def utc_to_unix(value):
    return int(
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


# ---------------------------------------------------------------------------
# expression engine (mirrors sample_core.js)
# ---------------------------------------------------------------------------


class Engine:
    def __init__(self, rules, snapshot):
        self.rules = rules
        self.snapshot = snapshot
        self.types = {
            name: (re.compile(pattern) if pattern else None)
            for name, pattern in rules["types"].items()
        }
        self.constants = {}
        for name, literal in rules["constants"].items():
            self.constants[name] = self.literal(literal)
        for name, path in rules["snapshot_constants"].items():
            value = snapshot
            for part in path:
                value = value[part]
            self.constants[name] = value
        self.field_types = {
            role: spec["fields"] for role, spec in rules["roles"].items()
        }
        self.var_exprs = rules["row_vars"]
        self.row_checks = list(rules["row_checks"]) + self.display_checks()

    @staticmethod
    def literal(value):
        if "int" in value:
            return int(value["int"])
        if "list" in value:
            return list(value["list"])
        return value["str"]

    def display_checks(self):
        checks = []
        for role, spec in self.rules["roles"].items():
            pairs = spec.get("decimal_pairs")
            if not pairs:
                continue
            checks.append(
                {
                    "id": f"{role}.decimal_display",
                    "group": "display",
                    "needs_rows": [role],
                    "title": "Decimal ONE values are derived exactly from the atto integers",
                    "assert": {
                        "and": [
                            {
                                "eq": [
                                    {"f": f"{role}.{name}_one"},
                                    {"fixed18": {"f": f"{role}.{name}_atto"}},
                                ]
                            }
                            for name in pairs
                        ]
                    },
                    "explain": "Every *_one column is a display rendering; it must equal the atto integer divided by 10^18 with exactly 18 decimals.",
                }
            )
        return checks

    def typed(self, role, field, raw):
        if field == "_category":
            return raw
        if raw is None:
            raise EvalError(f"{role}.{field} is missing")
        kind = self.field_types.get(role, {}).get(field)
        if kind is None:
            raise EvalError(f"{role}.{field} is not a known field")
        if kind == "uint":
            if not self.types["uint"].fullmatch(raw):
                raise EvalError(f"{role}.{field} is not a canonical unsigned integer: {raw!r}")
            return int(raw)
        return raw

    def eval(self, expr, ctx, row=None, row_role=None):
        if expr is True or expr is False:
            return expr
        if not isinstance(expr, dict) or len(expr) != 1:
            raise EvalError(f"invalid expression {expr!r}")
        (op, arg), = expr.items()
        if op == "int":
            return int(arg)
        if op == "str":
            return arg
        if op == "const":
            if arg not in self.constants:
                raise EvalError(f"unknown constant {arg}")
            return self.constants[arg]
        if op == "f":
            role, field = arg.split(".", 1)
            rows = ctx.role_rows.get(role, [])
            if not rows:
                raise MissingRow(f"no {role} row for this identifier")
            if len(rows) > 1:
                raise EvalError(f"{len(rows)} {role} rows for this identifier")
            return self.typed(role, field, rows[0].get(field))
        if op == "row":
            if row is None:
                raise EvalError("row reference outside an aggregate")
            return self.typed(row_role, arg, row.get(arg))
        if op == "var":
            return ctx.var(arg, self)
        if op == "total":
            if arg not in ctx.totals:
                raise EvalError(f"total {arg} is unavailable")
            return ctx.totals[arg]
        if op == "exists":
            return len(ctx.role_rows.get(arg, [])) > 0
        if op in ("add", "max", "min"):
            values = [self.int_value(self.eval(item, ctx, row, row_role)) for item in arg]
            if op == "add":
                return sum(values)
            return max(values) if op == "max" else min(values)
        if op == "sub":
            left = self.int_value(self.eval(arg[0], ctx, row, row_role))
            right = self.int_value(self.eval(arg[1], ctx, row, row_role))
            return left - right
        if op in ("eq", "ne", "lt", "le", "gt", "ge"):
            left = self.eval(arg[0], ctx, row, row_role)
            right = self.eval(arg[1], ctx, row, row_role)
            return compare(op, left, right)
        if op == "and":
            for item in arg:
                if not self.bool_value(self.eval(item, ctx, row, row_role)):
                    return False
            return True
        if op == "or":
            for item in arg:
                if self.bool_value(self.eval(item, ctx, row, row_role)):
                    return True
            return False
        if op == "not":
            return not self.bool_value(self.eval(arg, ctx, row, row_role))
        if op == "if":
            condition = self.bool_value(self.eval(arg[0], ctx, row, row_role))
            return self.eval(arg[1] if condition else arg[2], ctx, row, row_role)
        if op == "in":
            value = self.eval(arg[0], ctx, row, row_role)
            options = arg[1] if isinstance(arg[1], list) else self.eval(arg[1], ctx, row, row_role)
            if not isinstance(options, list):
                raise EvalError("in() needs a list")
            return value in options
        if op == "lower":
            value = self.eval(arg, ctx, row, row_role)
            if not isinstance(value, str):
                raise EvalError("lower() needs text")
            return value.lower()
        if op == "fixed18":
            return fixed18(self.int_value(self.eval(arg, ctx, row, row_role)))
        if op == "usd":
            amount = self.int_value(self.eval(arg[0], ctx, row, row_role))
            price = self.eval(arg[1], ctx, row, row_role)
            try:
                return usd(amount, price)
            except ValueError as error:
                raise EvalError(str(error)) from error
        if op in ("sum", "count", "all"):
            role = arg["role"]
            where = arg.get("where")
            selected = []
            for item in ctx.role_rows.get(role, []):
                if where is None or self.bool_value(self.eval(where, ctx, item, role)):
                    selected.append(item)
            if op == "count":
                return len(selected)
            if op == "sum":
                return sum(
                    self.int_value(self.typed(role, arg["field"], item.get(arg["field"])))
                    for item in selected
                )
            for item in selected:
                if not self.bool_value(self.eval(arg["assert"], ctx, item, role)):
                    return False
            return True
        raise EvalError(f"unknown operator {op}")

    @staticmethod
    def int_value(value):
        if type(value) is not int:
            raise EvalError(f"expected an integer, found {value!r}")
        return value

    @staticmethod
    def bool_value(value):
        if type(value) is not bool:
            raise EvalError(f"expected a boolean, found {value!r}")
        return value

    def explain_failure(self, expr, ctx, row=None, row_role=None):
        """Plain description of the first false sub-condition."""
        try:
            if isinstance(expr, dict) and len(expr) == 1:
                (op, arg), = expr.items()
                if op == "eq":
                    found = self.eval(arg[0], ctx, row, row_role)
                    expected = self.eval(arg[1], ctx, row, row_role)
                    return f"found {describe(found)}, expected {describe(expected)}"
                if op in ("le", "lt", "ge", "gt", "ne"):
                    left = self.eval(arg[0], ctx, row, row_role)
                    right = self.eval(arg[1], ctx, row, row_role)
                    return f"{describe(left)} {SYMBOL[op]} {describe(right)} is false"
                if op == "and":
                    for item in arg:
                        if not self.eval(item, ctx, row, row_role):
                            return self.explain_failure(item, ctx, row, row_role)
                if op == "if":
                    condition = self.eval(arg[0], ctx, row, row_role)
                    return self.explain_failure(
                        arg[1] if condition else arg[2], ctx, row, row_role
                    )
                if op == "all":
                    role = arg["role"]
                    where = arg.get("where")
                    for index, item in enumerate(ctx.role_rows.get(role, [])):
                        if where is not None and not self.eval(where, ctx, item, role):
                            continue
                        if not self.eval(arg["assert"], ctx, item, role):
                            detail = self.explain_failure(arg["assert"], ctx, item, role)
                            return f"{role} row {item.get('_line', index)}: {detail}"
        except EvalError as error:
            return str(error)
        return "the condition is false"


SYMBOL = {"le": "<=", "lt": "<", "ge": ">=", "gt": ">", "ne": "!="}


def compare(op, left, right):
    if type(left) is not type(right):
        raise EvalError(
            f"cannot compare {type(left).__name__} with {type(right).__name__}"
        )
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if isinstance(left, bool):
        raise EvalError("cannot order booleans")
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right


def describe(value):
    if type(value) is int:
        return f"{value} atto ({display_one(value)} ONE)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return repr(value)


class RowContext:
    def __init__(self, role_rows, totals=None):
        self.role_rows = role_rows
        self.totals = totals or {}
        self._vars = {}

    def var(self, name, engine):
        if name not in self._vars:
            if name not in engine.var_exprs:
                raise EvalError(f"unknown variable {name}")
            self._vars[name] = engine.eval(engine.var_exprs[name], self)
        return self._vars[name]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


class Report:
    def __init__(self):
        self.checks = []

    def add(self, scope, check_id, subject, status, title, message="", explain="", required=True):
        self.checks.append(
            {
                "scope": scope,
                "id": check_id,
                "subject": subject,
                "status": status,
                "required": bool(required),
                "title": title,
                "message": message,
                "explain": explain,
            }
        )


def tier_status(checks, scope):
    relevant = [check for check in checks if check["scope"] == scope]
    if any(check["status"] == FAIL for check in relevant):
        return FAIL
    if any(check["status"] == NOT_VERIFIED and check["required"] for check in relevant):
        return NOT_VERIFIED
    if not relevant:
        return NOT_VERIFIED
    return PASS


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------


def sample_score(rules, seed, identifier):
    domain = rules["sample_algorithm"]["domain"]
    digest = hashlib.sha256(f"{domain}|{seed}|{identifier}".encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


class Sampler:
    """Keep the sample_size identifiers with the smallest (score, id)."""

    def __init__(self, rules, seed, size):
        self.rules = rules
        self.seed = seed
        self.size = size
        self.heap = []  # (-score, inverted id ordering via tuple) max-heap
        self.members = set()
        self.population = 0

    def offer(self, identifier, payload):
        self.population += 1
        if self.size <= 0 or identifier in self.members:
            return
        score = sample_score(self.rules, self.seed, identifier)
        entry = (-score, _Desc(identifier), identifier, payload)
        if len(self.heap) < self.size:
            heapq.heappush(self.heap, entry)
            self.members.add(identifier)
        elif (score, identifier) < (-self.heap[0][0], self.heap[0][2]):
            removed = heapq.heapreplace(self.heap, entry)
            self.members.discard(removed[2])
            self.members.add(identifier)

    def result(self):
        ordered = sorted(
            ((-entry[0], entry[2], entry[3]) for entry in self.heap),
            key=lambda item: (item[0], item[1]),
        )
        return [(identifier, payload, score) for score, identifier, payload in ordered]


class _Desc:
    """Reverse string ordering so heap ties evict the larger identifier."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return self.value == other.value


def draw_sample(rules, seed, identifiers, size):
    """Pure helper used by tests: sample from an iterable of identifiers."""
    sampler = Sampler(rules, seed, size)
    for identifier in identifiers:
        sampler.offer(identifier.lower(), None)
    return [identifier for identifier, _payload, _score in sampler.result()]


def generate_seed():
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# bundle files
# ---------------------------------------------------------------------------


class RoleData:
    def __init__(self, role):
        self.role = role
        self.files = []  # manifest entries that exist
        self.present = False
        self.errors = 0
        self.examples = []


class Verifier:
    def __init__(
        self,
        bundle_dir,
        sample_size=None,
        seed=None,
        sample_by="secure_key",
        verify_all_metadata=False,
        expected_manifest_sha256=None,
        require_chain_truth=False,
        show_row=None,
        rules=None,
        snapshot=None,
    ):
        self.bundle_dir = Path(bundle_dir)
        self.rules = rules or load_rules()
        self.snapshot = snapshot or load_snapshot()
        self.engine = Engine(self.rules, self.snapshot)
        self.sample_size = sample_size
        self.seed_generated = seed is None
        self.seed = generate_seed() if seed is None else validate_seed(seed)
        if sample_by not in ("secure_key", "address"):
            raise BundleError("sample_by must be secure_key or address")
        self.sample_by = sample_by
        self.verify_all_metadata = verify_all_metadata
        self.expected_manifest_sha256 = expected_manifest_sha256
        self.require_chain_truth = require_chain_truth
        self.show_row = show_row.lower() if show_row else None
        self.report = Report()
        self.roles = {role: RoleData(role) for role in self.rules["roles"]}
        self.json_docs = {}
        self.file_hashes = {}
        self.totals = {}
        self.tainted_roles = set()
        self.identity_state = {}
        self.utf8_errors = {}
        self.sources = {}
        self.manifest = None
        self.manifest_sha256 = None

    # -- helpers -----------------------------------------------------------

    def bundle(self, check_id, subject, status, title, message="", explain="", required=True):
        self.report.add("bundle", check_id, subject, status, title, message, explain, required)

    def chain(self, check_id, subject, status, title, message="", explain="", required=True):
        self.report.add("chain", check_id, subject, status, title, message, explain, required)

    def type_ok(self, kind, value):
        if isinstance(kind, dict):
            return value in kind["enum"]
        pattern = self.engine.types[kind]
        return pattern is None or bool(pattern.fullmatch(value))

    # -- manifest ----------------------------------------------------------

    def read_manifest(self):
        if not self.bundle_dir.is_dir():
            raise BundleError(f"bundle directory not found: {self.bundle_dir}")
        path = self.bundle_dir / MANIFEST_NAME
        if not path.is_file():
            raise BundleError(f"{path} not found; is {self.bundle_dir} an evidence bundle?")
        data = path.read_bytes()
        self.manifest_sha256 = hashlib.sha256(data).hexdigest()
        try:
            manifest = parse_json_strict(data, MANIFEST_NAME)
        except ValueError as error:
            raise BundleError(str(error)) from error
        if not isinstance(manifest, dict):
            raise BundleError(f"{MANIFEST_NAME}: top-level value must be an object")
        if not isinstance(manifest.get("files"), list):
            raise BundleError(f"{MANIFEST_NAME}: files must be a list")
        self.manifest = manifest

    def check_manifest(self):
        m = self.manifest
        rules = self.rules
        title = "Bundle manifest has the expected format"
        if m.get("format") == rules["bundle_format"]:
            self.bundle("manifest.format", MANIFEST_NAME, PASS, title, m["format"])
        else:
            self.bundle("manifest.format", MANIFEST_NAME, FAIL, title,
                        f"found {m.get('format')!r}, expected {rules['bundle_format']!r}",
                        "The manifest is not a version this verifier understands.")
        problems = []
        if not isinstance(m.get("bundle_id"), str) or not m.get("bundle_id"):
            problems.append("bundle_id must be a non-empty string")
        if not isinstance(m.get("created_utc"), str) or not self.type_ok("utc_or_empty", m["created_utc"]) or not m["created_utc"]:
            problems.append("created_utc must be YYYY-MM-DDTHH:MM:SSZ")
        if not isinstance(m.get("sources"), list):
            problems.append("sources must be a list")
        declared = m.get("declared_totals")
        if not isinstance(declared, dict) or not all(
            isinstance(value, str) and self.type_ok("uint", value) for value in declared.values()
        ):
            problems.append("declared_totals must map names to canonical unsigned integer strings")
        scope = m.get("airdrop_scope", "partial")
        if scope not in ("complete", "partial"):
            problems.append("airdrop_scope must be complete or partial")
        self.bundle("manifest.schema", MANIFEST_NAME, FAIL if problems else PASS,
                    "Bundle manifest has every required field",
                    "; ".join(problems),
                    "A malformed manifest cannot identify the frozen evidence." if problems else "")
        title = "Bundle manifest hash matches the published value"
        if self.expected_manifest_sha256:
            expected = self.expected_manifest_sha256.lower().removeprefix("0x")
            if expected == self.manifest_sha256:
                self.bundle("manifest.pinned_sha256", MANIFEST_NAME, PASS, title, self.manifest_sha256)
            else:
                self.bundle("manifest.pinned_sha256", MANIFEST_NAME, FAIL, title,
                            f"found {self.manifest_sha256}, expected {expected}",
                            "This is not the bundle whose hash was published; its contents may differ.")
        else:
            self.bundle("manifest.pinned_sha256", MANIFEST_NAME, NOT_VERIFIED, title,
                        f"manifest SHA-256 is {self.manifest_sha256}; no expected value was supplied",
                        "Compare this hash with the published release value, or pass it to the verifier.",
                        required=False)

    def check_cutoff(self):
        m = self.manifest
        pinned = self.snapshot["cutoff"]
        c = self.engine.constants
        title = "Network and chain IDs are Harmony mainnet"
        chain_ids = m.get("chain_ids")
        ok = (
            m.get("network") == c["NETWORK"]
            and isinstance(chain_ids, dict)
            and json_int(chain_ids.get("shard0")) and chain_ids["shard0"] == c["CHAIN_ID_SHARD0"]
            and json_int(chain_ids.get("shard1")) and chain_ids["shard1"] == c["CHAIN_ID_SHARD1"]
        )
        self.bundle("cutoff.chain_id", "chain", PASS if ok else FAIL, title,
                    f"network={m.get('network')!r} chain_ids={chain_ids!r}",
                    "" if ok else f"Expected network {c['NETWORK']} with shard chain IDs {c['CHAIN_ID_SHARD0']} and {c['CHAIN_ID_SHARD1']}.")
        cutoff = m.get("cutoff")
        if not isinstance(cutoff, dict):
            self.bundle("cutoff.metadata", "cutoff", FAIL, "Cutoff metadata is present",
                        "manifest has no cutoff object", "Without cutoff metadata the snapshot cannot be identified.")
            return
        title = "Requested cutoff time matches the published snapshot"
        requested = cutoff.get("requested_time_utc")
        self.bundle("cutoff.requested_time", "cutoff", PASS if requested == pinned["requested_time_utc"] else FAIL,
                    title, f"found {requested!r}, expected {pinned['requested_time_utc']!r}",
                    "" if requested == pinned["requested_time_utc"] else "The bundle claims a different cutoff time than the published snapshot.")
        for shard in ("shard0", "shard1"):
            found = cutoff.get(shard) if isinstance(cutoff.get(shard), dict) else {}
            expected = pinned[shard]
            for field in ("block", "timestamp_utc", "hash", "state_root"):
                value = found.get(field)
                wanted = expected[field]
                if isinstance(value, str) and field in ("hash", "state_root"):
                    value = value.lower()
                ok = value == wanted and (field != "block" or json_int(value))
                self.bundle(f"cutoff.{field}", shard, PASS if ok else FAIL,
                            f"{shard} cutoff {field} matches the published snapshot",
                            f"found {found.get(field)!r}, expected {wanted!r}",
                            "" if ok else "The bundle's cutoff block identity differs from the published snapshot manifest.")
            stamp = found.get("timestamp_utc")
            ok = (
                isinstance(stamp, str) and self.type_ok("utc_or_empty", stamp) and stamp
                and isinstance(requested, str) and stamp <= requested
            )
            self.bundle("cutoff.timestamp_order", shard, PASS if ok else FAIL,
                        f"{shard} cutoff block is at or before the requested cutoff time",
                        f"block time {stamp!r}, requested {requested!r}",
                        "" if ok else "A cutoff block after the requested time would include post-cutoff activity.")
        title = "Threshold is exactly 1,000 ONE in atto units"
        threshold = c["THRESHOLD_ATTO"]
        pinned_threshold = self.snapshot["eligibility_1000_one"]["threshold_atto"]
        ok = (
            threshold == 1000 * c["ATTO_PER_ONE"]
            and str(threshold) == pinned_threshold
            and m.get("threshold_atto") == str(threshold)
            and self.snapshot["eligibility_1000_one"]["equality_policy"] == "include"
        )
        self.bundle("policy.threshold", "threshold", PASS if ok else FAIL, title,
                    f"rules={threshold} snapshot={pinned_threshold} bundle={m.get('threshold_atto')!r}",
                    "" if ok else "The bundle, the published snapshot, and the verifier must all use exactly 1,000,000,000,000,000,000,000 atto-ONE with an inclusive comparison.")
        since = m.get("initial_window_since_utc")
        ok = since == c["INITIAL_SINCE_UTC"]
        self.bundle("policy.initial_window", "initial window", PASS if ok else FAIL,
                    "Initial-stage activity window starts six calendar months before cutoff",
                    f"found {since!r}, expected {c['INITIAL_SINCE_UTC']!r}",
                    "" if ok else "The six-month activity window in the bundle differs from the published policy.")

    def check_sources(self):
        kinds = self.rules["source_kinds"]
        sources = self.manifest.get("sources")
        if not isinstance(sources, list):
            return
        problems = []
        for index, source in enumerate(sources):
            where = f"sources[{index}]"
            if not isinstance(source, dict):
                problems.append(f"{where} must be an object")
                continue
            source_id = source.get("id")
            if not isinstance(source_id, str) or not source_id:
                problems.append(f"{where}.id must be a non-empty string")
                continue
            if source_id in self.sources:
                problems.append(f"duplicate source id {source_id}")
            kind = source.get("kind")
            if kind not in kinds:
                problems.append(f"{source_id}: unknown kind {kind!r}")
            elif source.get("authoritative") is True and kinds[kind]["stale_prone"]:
                problems.append(
                    f"{source_id}: {kind} sources are stale or inconsistent and cannot be authoritative"
                )
            if not isinstance(source.get("authoritative"), bool):
                problems.append(f"{source_id}: authoritative must be true or false")
            retrieved = source.get("retrieved_utc")
            if not isinstance(retrieved, str) or not retrieved or not self.type_ok("utc_or_empty", retrieved):
                problems.append(f"{source_id}: retrieved_utc must be YYYY-MM-DDTHH:MM:SSZ")
            if not isinstance(source.get("description"), str):
                problems.append(f"{source_id}: description must be text")
            self.sources[source_id] = source
        for entry in self.manifest["files"]:
            if isinstance(entry, dict) and entry.get("source") not in self.sources:
                problems.append(f"{entry.get('path')!r} names unknown source {entry.get('source')!r}")
        self.bundle("sources.metadata", "sources", FAIL if problems else PASS,
                    "Source metadata is complete and does not treat RPC or explorer data as authoritative",
                    "; ".join(problems[:MAX_EXAMPLES]),
                    "Harmony's historical RPC and explorer infrastructure is stale or inconsistent; evidence from it can only corroborate, never decide." if problems else "")

    # -- files -------------------------------------------------------------

    def check_files(self):
        role_specs = self.rules["roles"]
        json_specs = self.rules["json_roles"]
        seen_paths = set()
        declared = set()
        bundle_root = self.bundle_dir.resolve()
        for index, entry in enumerate(self.manifest["files"]):
            subject = entry.get("path") if isinstance(entry, dict) else f"files[{index}]"
            problems = []
            if not isinstance(entry, dict):
                self.bundle("file.entry", str(subject), FAIL, "Manifest file entry is well formed",
                            "entry must be an object", "Each bundle file must be declared with role, path, sha256, bytes and source.")
                continue
            role = entry.get("role")
            path_text = entry.get("path")
            if role not in role_specs and role not in json_specs:
                problems.append(f"unknown role {role!r}")
            if not normalized_path(path_text):
                problems.append("path must be a relative path inside the bundle")
            elif path_text in seen_paths:
                problems.append("path is declared twice")
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                problems.append("sha256 must be 64 lowercase hex characters")
            size = entry.get("bytes")
            if not json_int(size):
                problems.append("bytes must be a non-negative integer")
            if role in role_specs:
                rows = entry.get("rows")
                if not json_int(rows):
                    problems.append("rows must be a non-negative integer")
                categories = role_specs[role].get("categories")
                if categories and entry.get("category") not in categories:
                    problems.append(f"category must be one of {categories}")
            if problems:
                self.bundle("file.entry", str(subject), FAIL, "Manifest file entry is well formed",
                            "; ".join(problems), "The file cannot be trusted without a complete, valid declaration.")
                continue
            seen_paths.add(path_text)
            path = (self.bundle_dir / path_text)
            try:
                path.resolve().relative_to(bundle_root)
            except ValueError:
                self.bundle("file.entry", path_text, FAIL, "Manifest file entry is well formed",
                            "path leaves the bundle directory", "Bundle files must be inside the bundle.")
                continue
            declared.add(path.resolve())
            if not path.is_file():
                self.bundle("file.present", path_text, FAIL, "Declared file is present",
                            "file is missing", "A file named by the manifest is missing, so its checks cannot run.")
                continue
            self.bundle("file.present", path_text, PASS, "Declared file is present")
            actual, utf8_error = hash_and_utf8_error(path)
            if utf8_error and role in role_specs:
                self.utf8_errors[path_text] = utf8_error
            self.file_hashes[path_text] = actual
            ok = actual == sha and path.stat().st_size == size
            self.bundle("file.sha256", path_text, PASS if ok else FAIL,
                        "File SHA-256 and size match the manifest",
                        f"sha256 {actual}, {path.stat().st_size} bytes" + ("" if ok else f"; manifest says {sha}, {size} bytes"),
                        "" if ok else "The file was changed after the bundle was frozen, or the manifest is wrong.")
            if role in role_specs:
                data = self.roles[role]
                data.present = True
                data.files.append({**entry, "abs": path})
            else:
                self.json_docs.setdefault(role, []).append({**entry, "abs": path})
        missing = [
            role for role, spec in role_specs.items()
            if spec.get("required") and not self.roles[role].present
        ]
        for role, spec in role_specs.items():
            for category in spec.get("categories") or []:
                if self.roles[role].present and not any(
                    entry.get("category") == category for entry in self.roles[role].files
                ):
                    missing.append(f"{role}:{category}")
        self.bundle("files.required_roles", "roles", FAIL if missing else PASS,
                    "Every required evidence file is present",
                    ("missing: " + ", ".join(missing)) if missing else "",
                    "Required files are missing, so the matching cross-file checks cannot run." if missing else "")
        undeclared = []
        for path in sorted(self.bundle_dir.rglob("*")):
            relative = path.relative_to(self.bundle_dir).as_posix()
            if path.is_dir() or path.name.startswith(".") or relative == MANIFEST_NAME:
                continue
            if path.resolve() not in declared:
                undeclared.append(path.relative_to(self.bundle_dir).as_posix())
        self.bundle("files.undeclared", "bundle", FAIL if undeclared else PASS,
                    "Bundle contains no undeclared files",
                    ", ".join(undeclared[:MAX_EXAMPLES]),
                    "A frozen bundle must be closed: every file is declared and hashed." if undeclared else "")

    # -- CSV scanning --------------------------------------------------------

    def iter_role_rows(self, role, collector):
        """Stream every row of every file of a role, validating structure."""
        spec = self.rules["roles"][role]
        data = self.roles[role]
        fields = spec["fields"]
        unique = {name: set() for name in spec.get("unique", [])}
        total_rows = 0
        for entry in sorted(data.files, key=lambda item: (item.get("category") or "", item["path"])):
            path_text = entry["path"]
            if path_text in self.utf8_errors:
                self.bundle("file.schema", path_text, FAIL, "File has the required columns",
                            f"cannot parse: {self.utf8_errors[path_text]}",
                            "The file is not valid UTF-8 CSV, so its rows cannot be read.")
                self.tainted_roles.add(role)
                continue
            try:
                count = self.scan_file(role, spec, fields, unique, entry, collector)
            except (csv.Error, UnicodeDecodeError) as error:
                self.bundle("file.schema", path_text, FAIL, "File has the required columns",
                            f"cannot parse: {error}", "The file is not valid UTF-8 CSV, so its rows cannot be read.")
                self.tainted_roles.add(role)
                continue
            if count is None:
                continue
            declared_rows = entry.get("rows")
            ok = declared_rows == count
            self.bundle("file.rows", path_text, PASS if ok else FAIL,
                        "Row count matches the manifest",
                        f"{count} data rows" + ("" if ok else f"; manifest says {declared_rows}"),
                        "" if ok else "Rows were added or removed after the bundle was frozen.")
            total_rows += count
        if data.present:
            ok = data.errors == 0 and role not in self.tainted_roles
            self.bundle("file.rows_valid", role, PASS if ok else FAIL,
                        "Every row is well formed (types, non-negative integers, ordering, no duplicates)",
                        "" if ok else f"{data.errors} invalid row(s); " + " | ".join(data.examples),
                        "" if ok else "Invalid records are reported, never skipped; the bundle cannot pass while any record is malformed.")
            if data.errors:
                self.tainted_roles.add(role)
        return total_rows

    def scan_file(self, role, spec, fields, unique, entry, collector):
        data = self.roles[role]
        path = entry["abs"]
        path_text = entry["path"]
        count = 0
        header_problem = None
        previous = None
        with path.open(newline="", encoding="utf-8") as handle:
            reader = strict_csv_reader(handle)
            header = next(reader, None)
            if header is None:
                header_problem = "file is empty (no header)"
            elif len(set(header)) != len(header):
                header_problem = "header repeats a column name"
            elif spec.get("exact_header") and header != spec["exact_header"]:
                header_problem = f"header must be exactly {spec['exact_header']}"
            else:
                missing = [name for name in fields if name not in header]
                if missing:
                    header_problem = "missing columns: " + ", ".join(missing)
            if header_problem:
                self.bundle("file.schema", path_text, FAIL, "File has the required columns",
                            header_problem, "Rows cannot be read without the documented columns.")
                self.tainted_roles.add(role)
                return None
            line = 1
            for record in reader:
                if not record:
                    continue
                line += 1
                count += 1
                problems = []
                if len(record) != len(header):
                    problems.append(f"has {len(record)} fields, header has {len(header)}")
                    row = dict(zip(header, record))
                else:
                    row = dict(zip(header, record))
                    for name, kind in fields.items():
                        value = row[name]
                        if not self.type_ok(kind, value):
                            problems.append(self.type_problem(name, kind, value))
                    for name in spec.get("positive", []):
                        if name in row and self.type_ok("uint", row[name]) and int(row[name]) <= 0:
                            problems.append(f"{name} must be positive")
                    sort_field = spec.get("sorted_by")
                    if sort_field and not problems:
                        key = row[sort_field].lower()
                        if previous is not None and key <= previous:
                            problems.append(f"{sort_field} is not strictly increasing")
                        previous = key
                    for name, values in unique.items():
                        value = row.get(name, "").lower()
                        if value in values:
                            problems.append(f"duplicate {name} {value}")
                        values.add(value)
                row["_line"] = f"{path_text}:{line}"
                if entry.get("category"):
                    row["_category"] = entry["category"]
                if problems:
                    data.errors += 1
                    row["_problems"] = problems
                    if len(data.examples) < MAX_EXAMPLES:
                        data.examples.append(f"{path_text}:{line}: " + "; ".join(problems))
                collector(row, problems)
        return count

    @staticmethod
    def type_problem(name, kind, value):
        if isinstance(kind, dict):
            return f"{name} {value!r} is not one of {kind['enum']}"
        if kind.startswith("uint") and value.startswith("-"):
            return f"{name} is negative ({value})"
        if kind.startswith("uint"):
            return f"{name} {value!r} is not a canonical unsigned integer"
        if kind.startswith("address"):
            return f"{name} {value!r} is not a 0x-prefixed 20-byte address"
        if kind.startswith("key"):
            return f"{name} {value!r} is not a lowercase 0x-prefixed 32-byte secure key"
        return f"{name} {value!r} is not a valid {kind}"

    # -- totals --------------------------------------------------------------

    def total_specs(self, role):
        return [spec for spec in self.rules["totals"] if spec["role"] == role]

    def make_accumulator(self, role):
        specs = self.total_specs(role)
        sums = {spec["id"]: 0 for spec in specs}
        engine = self.engine
        empty = RowContext({})

        def add(row, problems):
            if problems:
                return
            for spec in specs:
                where = spec.get("where")
                try:
                    if where is not None and not engine.eval(where, empty, row, role):
                        continue
                    if spec.get("count"):
                        sums[spec["id"]] += 1
                    else:
                        sums[spec["id"]] += engine.typed(role, spec["field"], row.get(spec["field"]))
                except EvalError:
                    self.tainted_roles.add(role)

        return sums, add

    # -- main flow -----------------------------------------------------------

    def run(self):
        self.read_manifest()
        self.check_manifest()
        self.check_cutoff()
        self.check_sources()
        self.check_files()
        self.check_json_docs()
        self.check_wone_summary()

        if self.sample_size is not None and self.sample_size < 0:
            raise BundleError("sample size must be non-negative")
        size = self.sample_size or 0

        sampler = Sampler(self.rules, self.seed, size)
        show_payload = {}
        overlay_present = self.roles["wone_overlay"].present
        sums, add_total = self.make_accumulator("wone_overlay")

        def overlay_row(row, problems):
            add_total(row, problems)
            key = row.get("secure_key", "").lower()
            address = row.get("address", "").lower()
            identifier = key if self.sample_by == "secure_key" else address
            if not identifier:
                return
            sampler.offer(identifier, row)
            if self.show_row and self.show_row in (key, address):
                show_payload["row"] = row
            if self.verify_all_metadata and not problems:
                self.check_identity_inline("wone_overlay", row)

        if overlay_present:
            self.iter_role_rows("wone_overlay", overlay_row)
            self.totals.update(sums)
        population = sampler.population
        drawn = sampler.result()
        overlay_readable = overlay_present and not (
            population == 0 and "wone_overlay" in self.tainted_roles
        )
        if overlay_readable and size > population:
            raise BundleError(
                f"sample size {size} exceeds the population of {population} rows; "
                "the verifier never reduces a requested sample"
            )
        if overlay_readable and len(drawn) < size:
            raise BundleError(
                f"only {len(drawn)} distinct identifiers exist for a sample of {size}: "
                "the current-claim ledger repeats identifiers (run --verify-all-metadata "
                "to list the duplicates); the verifier never reduces a requested sample"
            )
        sample = []
        for identifier, row, score in drawn:
            sample.append({"identifier": identifier, "score": f"{score:064x}", "row": row})
        if self.show_row and "row" not in show_payload:
            raise BundleError(f"--show-row {self.show_row}: no current-claim row has that secure key or address")

        focus = {}
        for item in sample:
            focus[item["row"]["secure_key"].lower()] = item["row"]
        if self.show_row:
            row = show_payload["row"]
            focus[row["secure_key"].lower()] = row
        addresses = {}
        for item in sample + ([{"row": show_payload["row"]}] if self.show_row else []):
            row = item["row"]
            keys = addresses.setdefault(row["address"].lower(), [])
            if row["secure_key"].lower() not in keys:
                keys.append(row["secure_key"].lower())
        matched = {key: {"wone_overlay": [row]} for key, row in focus.items()}

        if self.verify_all_metadata and overlay_present:
            self.finish_identities("wone_overlay")

        self.scan_other_roles(matched, addresses)
        self.row_results = []
        for item in sample:
            key = item["row"]["secure_key"].lower()
            role_rows = {**matched[key], "wone_overlay": [item["row"]]}
            self.row_results.append(self.verify_row(item["identifier"], role_rows, "row"))
        self.show_result = None
        if self.show_row:
            row = show_payload["row"]
            key = row["secure_key"].lower()
            self.show_result = self.verify_row(self.show_row, {**matched[key], "wone_overlay": [row]}, "show")

        self.check_totals()
        self.check_summaries()
        self.check_airdrop_run()
        self.check_chain(sample, matched)
        return self.build_report(sample, population)

    def check_identity_inline(self, role, row):
        state = self.identity_state.setdefault(role, {"rows": 0, "failures": 0, "examples": []})
        state["rows"] += 1
        for address_field, key_field in self.rules["roles"][role].get("identity", []):
            address, key = row.get(address_field, ""), row.get(key_field, "")
            if not address and not key:
                continue
            try:
                lib.require_address_secure_key(address, key, row["_line"])
            except ValueError as error:
                state["failures"] += 1
                if len(state["examples"]) < MAX_EXAMPLES:
                    state["examples"].append(str(error))

    def finish_identities(self, role):
        state = self.identity_state.get(role, {"rows": 0, "failures": 0, "examples": []})
        failures = state["failures"]
        self.bundle("metadata.identity_all", role, FAIL if failures else PASS,
                    "Every row's address hashes to its secure key (keccak256(address) == secure_key)",
                    f"{failures} mismatch(es): " + " | ".join(state["examples"]) if failures else f"{state['rows']} rows checked",
                    "An address that does not hash to its secure key belongs to a different account." if failures else "")

    def scan_other_roles(self, matched, addresses):
        self.ready_by_destination = {}
        self.airdrop_amounts = {}
        airdrop_problems = []
        order = [role for role in self.rules["role_order"] if role != "wone_overlay"]
        for role in order:
            data = self.roles[role]
            if not data.present:
                continue
            spec = self.rules["roles"][role]
            match_field = spec["match"]
            sums, add_total = self.make_accumulator(role)
            by_address = match_field in ("address", "address_hex")
            native_join = None
            if role == "native_claims" and self.roles["wone_overlay"].present:
                native_join = SortedKeyCursor(self.roles["wone_overlay"].files[0]["abs"])
                self.native_missing = []

            def collect(row, problems, role=role, match_field=match_field, by_address=by_address,
                        add_total=add_total, native_join=native_join):
                add_total(row, problems)
                value = row.get(match_field, "").lower()
                keys = addresses.get(value, []) if by_address else [value]
                for key in keys:
                    if key in matched:
                        matched[key].setdefault(role, []).append(row)
                key = value
                if self.verify_all_metadata and not problems and self.rules["roles"][role].get("identity"):
                    self.check_identity_inline(role, row)
                if native_join is not None and not problems:
                    if not native_join.contains(value):
                        self.native_missing.append(row["_line"])
                if role == "wallet_allocations" and not problems and row["destination_status"] == "ready":
                    destination = row["destination_address"].lower()
                    self.ready_by_destination[destination] = (
                        self.ready_by_destination.get(destination, 0) + int(row["amount_atto"])
                    )
                    if key in matched:
                        matched[key].setdefault("_destinations", set()).add(destination)
                if role == "airdrop_distribution" and not problems:
                    address = row["address"].lower()
                    amount = int(row["amount"])
                    self.airdrop_amounts[address] = amount
                    expected = self.ready_by_destination.get(address)
                    if expected != amount:
                        airdrop_problems.append(
                            f"{row['_line']}: {address} receives {amount}, ready wallet allocations to it total {expected or 0}"
                        )

            self.iter_role_rows(role, collect)
            self.totals.update(sums)
            if self.verify_all_metadata and spec.get("identity"):
                self.finish_identities(role)
            if native_join is not None:
                native_join.close()
                self.bundle("global.native_in_overlay", "native_claims",
                            FAIL if self.native_missing else PASS,
                            "Every native claim row appears in the current claim ledger",
                            ", ".join(self.native_missing[:MAX_EXAMPLES]),
                            "A native claim missing from the current ledger would silently drop an account." if self.native_missing else "")
            if role == "airdrop_distribution":
                self.airdrop_problems = airdrop_problems
        self.check_airdrop_global()

    def check_airdrop_global(self):
        if not self.roles["airdrop_distribution"].present:
            return
        problems = list(getattr(self, "airdrop_problems", []))
        scope = self.manifest.get("airdrop_scope", "partial")
        if scope == "complete":
            for destination, amount in sorted(self.ready_by_destination.items()):
                if destination not in self.airdrop_amounts:
                    problems.append(f"ready destination {destination} ({amount}) is missing from a complete airdrop")
        self.bundle("global.airdrop_destinations", "airdrop_distribution", FAIL if problems else PASS,
                    "Every airdrop recipient is a ready wallet destination with the exact allocated amount",
                    " | ".join(problems[:MAX_EXAMPLES]),
                    "The airdrop list must be built only from ready wallet allocations, paying each destination its full ready amount (held destinations are never paid)." if problems else "")

    # -- per row -------------------------------------------------------------

    def verify_row(self, identifier, role_rows, scope):
        engine = self.engine
        ctx = RowContext(role_rows, self.totals)
        checks = []
        overlay = role_rows["wone_overlay"][0]

        def add(check_id, status, title, message="", explain="", required=True):
            checks.append({
                "scope": "row", "id": check_id, "subject": identifier, "status": status,
                "required": required, "title": title, "message": message, "explain": explain,
            })

        for role, rows in sorted(role_rows.items()):
            if role.startswith("_"):
                continue
            problems = [p for row in rows for p in row.get("_problems", [])]
            add(f"schema.{role}", FAIL if problems else PASS,
                f"{self.rules['roles'][role]['label']} record is well formed",
                "; ".join(problems),
                "The record has a malformed, negative, or missing value, so it cannot be trusted." if problems else "")
            identity_problems = []
            for row in rows:
                for address_field, key_field in self.rules["roles"][role].get("identity", []):
                    address, key = row.get(address_field, ""), row.get(key_field, "")
                    if not address and not key:
                        continue
                    try:
                        lib.require_address_secure_key(address, key, row["_line"])
                    except ValueError as error:
                        identity_problems.append(str(error))
            if self.rules["roles"][role].get("identity"):
                add(f"identity.{role}", FAIL if identity_problems else PASS,
                    "Address hashes to its secure key (keccak256(address) == secure_key)",
                    "; ".join(identity_problems),
                    "The address and secure key belong to different accounts." if identity_problems else "")
            spec = self.rules["roles"][role]
            if len(rows) > 1 and spec["match"] in spec.get("unique", []):
                add(f"duplicate.{role}", FAIL, "Record appears once",
                    f"{len(rows)} records: " + ", ".join(row["_line"] for row in rows),
                    "Duplicate records for one account could pay it twice.")

        for rule in engine.row_checks:
            needs_files = list(rule.get("needs_files", [])) + list(rule.get("needs_rows", []))
            absent = [role for role in needs_files if not self.role_present(role)]
            if absent:
                required = all(self.role_required(role) for role in absent)
                add(rule["id"], NOT_VERIFIED, rule["title"],
                    "not in bundle: " + ", ".join(sorted(set(absent))),
                    "The evidence file needed for this check is not part of the bundle.",
                    required=required)
                continue
            if any(not role_rows.get(role) for role in rule.get("needs_rows", [])):
                continue
            try:
                if "when" in rule and not engine.bool_value(engine.eval(rule["when"], ctx)):
                    continue
                if "field_equal" in rule:
                    spec = rule["field_equal"]
                    left = role_rows[spec["left"]][0]
                    right = role_rows[spec["right"]][0]
                    differ = [
                        f"{name}: {left.get(name)!r} vs {right.get(name)!r}"
                        for name in spec["fields"]
                        if (left.get(name) or "").lower() != (right.get(name) or "").lower()
                    ]
                    add(rule["id"], FAIL if differ else PASS, rule["title"],
                        "; ".join(differ), rule["explain"] if differ else "")
                    continue
                ok = engine.bool_value(engine.eval(rule["assert"], ctx))
                message = "" if ok else engine.explain_failure(rule["assert"], ctx)
                add(rule["id"], PASS if ok else FAIL, rule["title"], message, "" if ok else rule["explain"])
            except MissingRow as error:
                add(rule["id"], FAIL, rule["title"], str(error), rule["explain"])
            except EvalError as error:
                add(rule["id"], FAIL, rule["title"], f"could not evaluate: {error}", rule["explain"])

        self.verify_row_airdrop(identifier, role_rows, add)
        status = tier_status(checks, "row")
        values = {}
        for field in (
            "liquid_shard0_atto", "liquid_shard1_atto", "liquid_total_atto",
            "active_staked_or_delegated_atto", "pending_undelegation_atto",
            "unclaimed_staking_reward_atto", "pending_cross_shard_atto",
            "native_wallet_airdrop_atto", "wone_balance_atto", "wone_airdrop_atto",
            "wallet_airdrop_atto", "staked_to_vault_atto", "qualification_total_atto",
            "native_total_claim_atto", "total_claim_atto",
        ):
            raw = overlay.get(field, "")
            values[field] = {
                "atto": raw,
                "one": fixed18(int(raw)) if self.type_ok("uint", raw) else "",
            }
        stage = (role_rows.get("stage_policy") or [{}])[0]
        eligibility = (role_rows.get("eligibility") or [{}])[0]
        qualification = overlay.get("qualification_total_atto", "")
        return {
            "identifier": identifier,
            "secure_key": overlay.get("secure_key", ""),
            "address": overlay.get("address", ""),
            "status": status,
            "qualifies": (
                int(qualification) >= self.engine.constants["THRESHOLD_ATTO"]
                if self.type_ok("uint", qualification) else None
            ),
            "eligibility_category": eligibility.get("_category", ""),
            "migration_stage": stage.get("migration_stage", ""),
            "issuance_treatment": stage.get("issuance_treatment", ""),
            "routing_category": stage.get("routing_category", ""),
            "values": values,
            "records": {
                role: [row["_line"] for row in rows]
                for role, rows in sorted(role_rows.items())
                if not role.startswith("_")
            },
            "checks": checks,
        }

    def verify_row_airdrop(self, identifier, role_rows, add):
        title = "Airdrop amount and destination match the ready wallet allocation"
        if not self.roles["airdrop_distribution"].present:
            add("airdrop.row", NOT_VERIFIED, title, "no airdrop distribution in bundle",
                "No airdrop list was included, so delivery amounts were not compared.", required=False)
            return
        destinations = sorted(role_rows.get("_destinations", set()))
        address = role_rows["wone_overlay"][0]["address"].lower()
        problems = []
        paid = []
        for destination in destinations:
            expected = self.ready_by_destination.get(destination, 0)
            if destination in self.airdrop_amounts:
                amount = self.airdrop_amounts[destination]
                if amount != expected:
                    problems.append(f"{destination} receives {amount}, expected {expected}")
                else:
                    paid.append(destination)
            elif self.manifest.get("airdrop_scope") == "complete":
                problems.append(f"{destination} is missing from a complete airdrop")
        if address in self.airdrop_amounts and address not in self.ready_by_destination:
            problems.append(f"{address} is in the airdrop but has no ready wallet allocation")
        if problems:
            add("airdrop.row", FAIL, title, "; ".join(problems),
                "The airdrop pays an amount or destination that the wallet allocation does not authorize.")
        elif not destinations and address not in self.airdrop_amounts:
            add("airdrop.row", PASS, title, "row has no ready wallet destination and is not in the airdrop")
        elif len(paid) == len(destinations):
            add("airdrop.row", PASS, title, "paid: " + ", ".join(paid))
        else:
            add("airdrop.row", NOT_VERIFIED, title,
                "ready destination not in this (partial) airdrop run: "
                + ", ".join(d for d in destinations if d not in paid),
                "This airdrop run is declared partial; the destination may be paid in a later batch.",
                required=False)

    # -- totals and summaries ------------------------------------------------

    def check_totals(self):
        declared = self.manifest.get("declared_totals")
        declared = declared if isinstance(declared, dict) else {}
        known = {spec["id"]: spec for spec in self.rules["totals"]}
        for total_id, spec in known.items():
            role = spec["role"]
            if not self.roles[role].present:
                if total_id in declared:
                    self.bundle("totals.declared", total_id, FAIL, "Declared total matches the recomputed total",
                                f"declared {declared[total_id]} but the {role} file is not in the bundle",
                                "A total was published for evidence that is missing.")
                continue
            if role in self.tainted_roles:
                self.bundle("totals.declared", total_id, FAIL, "Declared total matches the recomputed total",
                            f"cannot recompute: {role} has invalid rows",
                            "Totals are not trustworthy while any record is malformed.")
                continue
            computed = self.totals[total_id]
            if total_id not in declared:
                self.bundle("totals.declared", total_id, FAIL, "Declared total matches the recomputed total",
                            f"recomputed {computed}; manifest declares no value",
                            "Every bundle total must be declared so it can be published and compared.")
                continue
            ok = declared[total_id] == str(computed)
            self.bundle("totals.declared", total_id, PASS if ok else FAIL,
                        "Declared total matches the recomputed total",
                        f"recomputed {computed}" + ("" if ok else f", manifest declares {declared[total_id]}"),
                        "" if ok else "The bundle's published total differs from its own files.")
        unknown = sorted(set(declared) - set(known))
        if unknown:
            self.bundle("totals.unknown", "declared_totals", FAIL, "Declared totals are all recognized",
                        ", ".join(unknown), "The manifest declares totals this verifier cannot recompute.")
        ctx = RowContext({}, self.totals)
        for rule in self.rules["total_checks"]:
            absent = [role for role in rule["roles"] if not self.roles[role].present]
            if absent:
                required = all(self.rules["roles"][role].get("required") for role in absent)
                self.bundle(rule["id"], "totals", NOT_VERIFIED, rule["title"],
                            "not in bundle: " + ", ".join(absent),
                            "The evidence file needed for this total is not part of the bundle.", required=required)
                continue
            tainted = [role for role in rule["roles"] if role in self.tainted_roles]
            if tainted:
                self.bundle(rule["id"], "totals", FAIL, rule["title"],
                            "cannot recompute: invalid rows in " + ", ".join(tainted),
                            "Totals are not trustworthy while any record is malformed.")
                continue
            try:
                ok = self.engine.bool_value(self.engine.eval(rule["assert"], ctx))
                message = "" if ok else self.engine.explain_failure(rule["assert"], ctx)
            except EvalError as error:
                ok, message = False, f"could not evaluate: {error}"
            self.bundle(rule["id"], "totals", PASS if ok else FAIL, rule["title"], message,
                        "" if ok else "Bundle-level totals do not close, so at least one file disagrees with another.")

    def role_present(self, role):
        if role in self.roles:
            return self.roles[role].present
        return bool(self.parsed_json.get(role))

    def role_required(self, role):
        spec = self.rules["roles"].get(role) or self.rules["json_roles"].get(role) or {}
        return bool(spec.get("required"))

    def check_wone_summary(self):
        """Take the WONE exclusion set from the overlay's own summary."""
        docs = self.parsed_json.get("wone_overlay_summary", [])
        if not docs:
            return
        entry, summary = docs[0]
        problems = []
        overlay_files = self.roles["wone_overlay"].files
        overlay_hash = self.file_hashes.get(overlay_files[0]["path"]) if overlay_files else None
        if summary.get("output_sha256") != overlay_hash:
            problems.append("output_sha256 does not identify the WONE overlay file")
        requested = summary.get("excluded_addresses_requested")
        if not isinstance(requested, list) or not all(
            isinstance(item, str) and self.type_ok("address", item) for item in requested
        ):
            problems.append("excluded_addresses_requested must be a list of addresses")
        else:
            addresses = sorted({item.lower() for item in requested})
            missing = sorted(set(self.engine.constants["WONE_REQUIRED_EXCLUSIONS"]) - set(addresses))
            if missing:
                problems.append("excluded_addresses_requested lacks required exclusions: " + ", ".join(missing))
        if problems:
            del self.parsed_json["wone_overlay_summary"]
        else:
            self.engine.constants["WONE_EXCLUDED_ADDRESSES"] = addresses
        self.bundle("summary.wone_overlay", entry["path"], FAIL if problems else PASS,
                    "WONE overlay summary identifies the overlay file and its exclusion set",
                    "; ".join(problems) if problems else f"{len(addresses)} excluded address(es)",
                    "The WONE exclusion set cannot be trusted, so WONE census checks are not run." if problems else "")

    def check_json_docs(self):
        self.parsed_json = {}
        for role, entries in self.json_docs.items():
            spec = self.rules["json_roles"][role]
            if len(entries) > 1 and not spec.get("multiple"):
                self.bundle("file.entry", role, FAIL, "JSON role appears once",
                            f"{len(entries)} files declared", "Only one file of this kind may be part of a bundle.")
            for entry in entries:
                try:
                    value = parse_json_strict(entry["abs"].read_bytes(), entry["path"])
                    if not isinstance(value, dict):
                        raise ValueError(f"{entry['path']}: top-level value must be an object")
                except ValueError as error:
                    self.bundle("file.json", entry["path"], FAIL, "JSON file parses strictly",
                                str(error), "Malformed evidence cannot be used and is never skipped silently.")
                    continue
                self.parsed_json.setdefault(role, []).append((entry, value))

    def check_summaries(self):
        docs = self.parsed_json
        stage_files = self.roles["stage_policy"].files
        stage_hash = self.file_hashes.get(stage_files[0]["path"]) if stage_files else None
        for entry, summary in docs.get("stage_summary", []):
            problems = []
            if summary.get("output_sha256") != stage_hash:
                problems.append("output_sha256 does not identify the stage policy file")
            if "stage_policy.rows" in self.totals and not (
                json_int(summary.get("qualified_rows")) and summary["qualified_rows"] == self.totals["stage_policy.rows"]
            ):
                problems.append(f"qualified_rows {summary.get('qualified_rows')!r} != {self.totals['stage_policy.rows']}")
            self.bundle("summary.stage", entry["path"], FAIL if problems else PASS,
                        "Stage summary identifies the stage policy file and row count", "; ".join(problems),
                        "The pipeline's own summary disagrees with the bundled stage policy." if problems else "")
        for entry, summary in docs.get("routing_summary", []):
            problems = []
            if summary.get("migration_stages_sha256") != stage_hash:
                problems.append("migration_stages_sha256 does not identify the stage policy file")
            routing_files = self.roles["routing_exceptions"].files
            routing_hash = self.file_hashes.get(routing_files[0]["path"]) if routing_files else None
            recorded = ((summary.get("outputs") or {}).get("routing_exceptions") or {}).get("sha256")
            if recorded != routing_hash:
                problems.append("outputs.routing_exceptions.sha256 does not identify the routing exceptions file")
            self.bundle("summary.routing", entry["path"], FAIL if problems else PASS,
                        "Routing summary identifies the stage policy and routing exceptions", "; ".join(problems),
                        "The pipeline's own summary disagrees with the bundled routing files." if problems else "")
        for entry, summary in docs.get("eligibility_summary", []):
            problems = []
            if summary.get("comparison") != "ge":
                problems.append(f"comparison is {summary.get('comparison')!r}, expected 'ge' (inclusive)")
            if summary.get("minimum_atto") != str(self.engine.constants["THRESHOLD_ATTO"]):
                problems.append(f"minimum_atto is {summary.get('minimum_atto')!r}")
            categories = summary.get("categories") if isinstance(summary.get("categories"), dict) else {}
            for file_entry in self.roles["eligibility"].files:
                record = categories.get(file_entry["category"]) or {}
                if record.get("output_sha256") != self.file_hashes.get(file_entry["path"]):
                    problems.append(f"{file_entry['category']}: output_sha256 does not identify {file_entry['path']}")
                if not (json_int(record.get("rows")) and record["rows"] == file_entry.get("rows")):
                    problems.append(f"{file_entry['category']}: rows {record.get('rows')!r} != {file_entry.get('rows')}")
            self.bundle("summary.eligibility", entry["path"], FAIL if problems else PASS,
                        "Eligibility summary uses the inclusive 1,000 ONE threshold and identifies every category file",
                        "; ".join(problems),
                        "The pipeline's eligibility summary disagrees with the bundled category files." if problems else "")

    def check_airdrop_run(self):
        manifests = self.parsed_json.get("airdrop_manifest", [])
        distribution = self.roles["airdrop_distribution"].files
        if not manifests and not distribution:
            self.bundle("airdrop.run", "airdrop", NOT_VERIFIED,
                        "Airdrop Merkle root, list hash, and totals recompute",
                        "no airdrop manifest in bundle", "No airdrop run is part of this bundle.", required=False)
            return
        if not manifests or not distribution:
            self.bundle("airdrop.run", "airdrop", FAIL, "Airdrop Merkle root, list hash, and totals recompute",
                        "an airdrop run needs both airdrop_manifest and airdrop_distribution",
                        "A partial airdrop run cannot be verified.")
            return
        entry, _manifest = manifests[0]
        run_dir = entry["abs"].parent
        problems = []
        if distribution[0]["abs"] != run_dir / "distribution.csv":
            problems.append("airdrop_distribution must be distribution.csv next to the airdrop manifest")
        batch_paths = sorted(item["abs"] for item in self.json_docs.get("airdrop_batch", []))
        try:
            result = airdrop_verify.verify_run(run_dir)
            problems.extend(result["problems"])
            expected_batches = [airdrop_common.batch_file(run_dir, index) for index in range(result["batch_count"])]
            if sorted(expected_batches) != batch_paths:
                problems.append("declared airdrop_batch files do not match the run's batch count")
            message = (
                f"root {result['root']}, list_sha256 {result['list_sha256']}, "
                f"{result['recipient_count']} recipients, total {result['total_amount']}"
            )
        except Exception as error:  # untrusted input: report every failure, never crash
            problems.append(f"{type(error).__name__}: {error}")
            message = ""
        self.bundle("airdrop.run", entry["path"], FAIL if problems else PASS,
                    "Airdrop Merkle root, list hash, and totals recompute",
                    "; ".join(problems) if problems else message,
                    "The airdrop manifest does not describe the bundled distribution list." if problems else "")

    # -- historical chain evidence ----------------------------------------------

    def check_chain(self, sample, matched):
        kinds = self.rules["source_kinds"]
        pinned = self.snapshot["cutoff"]
        docs = self.parsed_json.get("historical_evidence", [])
        malformed = [entry["path"] for entry in self.json_docs.get("historical_evidence", [])
                     if entry["path"] not in {item[0]["path"] for item in docs}]
        headers = {0: [], 1: []}
        accounts = []
        for entry, doc in docs:
            problems = self.evidence_problems(doc)
            if problems:
                self.bundle("chain.evidence_schema", entry["path"], FAIL,
                            "Archived evidence file is well formed", "; ".join(problems[:MAX_EXAMPLES]),
                            "Malformed archived evidence is rejected, never skipped silently.")
                malformed.append(entry["path"])
                continue
            self.bundle("chain.evidence_schema", entry["path"], PASS, "Archived evidence file is well formed")
            source = self.sources.get(doc["source_id"], {})
            kind = source.get("kind", "")
            for header in doc.get("block_headers", []):
                headers[header["shard"]].append((doc["source_id"], kind, header, entry["path"]))
            for account in doc.get("accounts", []):
                accounts.append((doc["source_id"], kind, account))

        if not docs and not malformed:
            self.chain("chain.headers", "cutoff", NOT_VERIFIED,
                       "Cutoff block headers are corroborated by independent archived sources",
                       "no archived historical evidence in bundle",
                       "Without archived headers, state proofs, or database-derived evidence, the bundle's agreement with the chain is asserted, not independently verified.",
                       required=self.require_chain_truth)
        corroborated = {}
        for shard in (0, 1):
            expected = pinned[f"shard{shard}"]
            expected_values = {
                "number": expected["block"],
                "hash": expected["hash"],
                "state_root": expected["state_root"],
                "timestamp": utc_to_unix(expected["timestamp_utc"]),
            }
            agreeing = []
            for source_id, kind, header, path in headers[shard]:
                differ = []
                for field, wanted in expected_values.items():
                    value = header.get(field)
                    if isinstance(value, str):
                        value = value.lower()
                    if value != wanted:
                        differ.append(f"{field}: {header.get(field)!r} vs pinned {wanted!r}")
                subject = f"{source_id} shard{shard}"
                if differ:
                    stale = kinds.get(kind, {}).get("stale_prone", True)
                    self.chain("chain.header", subject, WARNING if stale else FAIL,
                               f"Source header matches the pinned shard-{shard} cutoff block",
                               "; ".join(differ),
                               ("Stale or inconsistent RPC/explorer data: recorded as an infrastructure warning, not as verification."
                                if stale else "An independent archived source contradicts the published cutoff block."),
                               required=not stale)
                else:
                    self.chain("chain.header", subject, PASS,
                               f"Source header matches the pinned shard-{shard} cutoff block",
                               f"{kind} source agrees on number, hash, state root, and timestamp")
                    agreeing.append((source_id, kind, header))
            for field in ("parent_hash", "transactions_root"):
                values = {}
                for source_id, kind, header in agreeing:
                    values.setdefault(header[field].lower(), []).append((source_id, kind))
                if len(values) > 1:
                    independent = {
                        value for value, owners in values.items()
                        if any(kinds.get(kind, {}).get("independent") for _source, kind in owners)
                    }
                    detail = "; ".join(
                        f"{value}: " + ", ".join(source for source, _kind in owners)
                        for value, owners in sorted(values.items())
                    )
                    self.chain("chain.source_agreement", f"shard{shard} {field}",
                               FAIL if len(independent) > 1 else WARNING,
                               f"Independent sources agree on shard-{shard} {field}", detail,
                               "Sources disagree about the cutoff block; the disagreement is reported, not resolved.",
                               required=len(independent) > 1)
            independent_sources = {
                source_id for source_id, kind, _header in agreeing if kinds.get(kind, {}).get("independent")
            }
            distinct = {source_id for source_id, _kind, _header in agreeing}
            corroborated[shard] = len(distinct) >= 2 and len(independent_sources) >= 1
            if docs:
                self.chain("chain.headers", f"shard{shard}", PASS if corroborated[shard] else NOT_VERIFIED,
                           f"Shard-{shard} cutoff header is corroborated by at least two sources, one of them independent of RPC/explorer infrastructure",
                           f"{len(distinct)} agreeing source(s), {len(independent_sources)} independent",
                           "" if corroborated[shard] else "Not enough independent archived evidence to call the cutoff header verified.",
                           required=self.require_chain_truth)

        by_address = {}
        for source_id, kind, account in accounts:
            by_address.setdefault(account["address"].lower(), []).append((source_id, kind, account))
        for item in sample:
            row = item["row"]
            self.check_row_chain(item["identifier"], row, by_address.get(row["address"].lower(), []))

    def evidence_problems(self, doc):
        problems = []
        if doc.get("format") != self.rules["evidence_format"]:
            problems.append(f"format must be {self.rules['evidence_format']}")
        if doc.get("source_id") not in self.sources:
            problems.append(f"source_id {doc.get('source_id')!r} is not declared in the manifest")
        retrieved = doc.get("retrieved_utc")
        if not isinstance(retrieved, str) or not retrieved or not self.type_ok("utc_or_empty", retrieved):
            problems.append("retrieved_utc must be YYYY-MM-DDTHH:MM:SSZ")
        headers = doc.get("block_headers", [])
        accounts = doc.get("accounts", [])
        if not isinstance(headers, list) or not isinstance(accounts, list):
            return problems + ["block_headers and accounts must be lists"]
        if not headers and not accounts:
            problems.append("evidence has neither block_headers nor accounts")
        hash_re = re.compile(r"0x[0-9a-fA-F]{64}\Z")
        for index, header in enumerate(headers):
            where = f"block_headers[{index}]"
            if not isinstance(header, dict):
                problems.append(f"{where} must be an object")
                continue
            if not (json_int(header.get("shard")) and header["shard"] in (0, 1)):
                problems.append(f"{where}.shard must be 0 or 1")
            for field in ("number", "timestamp"):
                if not json_int(header.get(field)):
                    problems.append(f"{where}.{field} must be a non-negative integer")
            for field in ("hash", "parent_hash", "state_root", "transactions_root"):
                if not isinstance(header.get(field), str) or not hash_re.match(header[field]):
                    problems.append(f"{where}.{field} must be a 0x-prefixed 32-byte hash")
        for index, account in enumerate(accounts):
            where = f"accounts[{index}]"
            if not isinstance(account, dict):
                problems.append(f"{where} must be an object")
                continue
            if not (json_int(account.get("shard")) and account["shard"] in (0, 1)):
                problems.append(f"{where}.shard must be 0 or 1")
            if not json_int(account.get("block")):
                problems.append(f"{where}.block must be an integer")
            if not isinstance(account.get("address"), str) or not self.type_ok("address", account["address"]):
                problems.append(f"{where}.address is not an address")
            for field in ACCOUNT_EVIDENCE_FIELDS:
                if field in account and (not isinstance(account[field], str) or not self.type_ok("uint", account[field])):
                    problems.append(f"{where}.{field} must be a canonical unsigned integer string")
            if not any(field in account for field in ACCOUNT_EVIDENCE_FIELDS):
                problems.append(f"{where} has no balance or claim component")
        return problems

    def check_row_chain(self, identifier, row, records):
        kinds = self.rules["source_kinds"]
        pinned_blocks = {0: self.snapshot["cutoff"]["shard0"]["block"], 1: self.snapshot["cutoff"]["shard1"]["block"]}
        evidenced = {}
        problems = []
        warnings = []
        excluded = self.engine.constants.get(
            "WONE_EXCLUDED_ADDRESSES", self.engine.constants["WONE_REQUIRED_EXCLUSIONS"]
        )
        wone_excluded = row.get("address", "").lower() in excluded
        for source_id, kind, account in records:
            if account["block"] != pinned_blocks[account["shard"]]:
                warnings.append(f"{source_id}: shard{account['shard']} record is for block {account['block']}, not the cutoff")
                continue
            independent = kinds.get(kind, {}).get("independent", False)
            for field, component in account_components(account["shard"]):
                if field not in account:
                    continue
                expected = row.get(component, "")
                if component == "wone_balance_atto" and wone_excluded:
                    # The claim zeroes an excluded holder's WONE by policy; the chain
                    # balance is not expected to match, only to exist.
                    if independent:
                        evidenced[component] = f"{source_id} (excluded holder)"
                    continue
                if account[field] == expected:
                    if independent:
                        evidenced[component] = source_id
                else:
                    text = f"{source_id} ({kind}) {component}: {account[field]} vs claim {expected}"
                    (problems if independent else warnings).append(text)
        title = "Sampled claim components are corroborated by independent archived chain evidence"
        missing = [c for c in CLAIM_COMPONENTS if c not in evidenced]
        if problems:
            self.chain("chain.row", identifier, FAIL, title, "; ".join(problems),
                       "Independent archived evidence contradicts the claim row.")
        elif warnings:
            self.chain("chain.row", identifier, WARNING, title, "; ".join(warnings),
                       "Only stale or inconsistent RPC/explorer data disagrees; recorded as an infrastructure warning, not as verification.",
                       required=False)
        if not problems and missing:
            self.chain("chain.row", identifier, NOT_VERIFIED, title,
                       "no independent evidence for: " + ", ".join(missing),
                       "The claim components were checked against the bundle only; the chain values were not independently evidenced.",
                       required=self.require_chain_truth)
        elif not problems and not missing:
            self.chain("chain.row", identifier, PASS, title,
                       "all components match: " + ", ".join(f"{c}={evidenced[c]}" for c in CLAIM_COMPONENTS))

    # -- report ----------------------------------------------------------------

    def build_report(self, sample, population):
        row_checks = [check for result in self.row_results for check in result["checks"]]
        checks = self.report.checks + row_checks
        requested = bool(self.sample_size)
        sampled = tier_status(row_checks, "row") if requested else "NOT REQUESTED"
        metadata = tier_status(self.report.checks, "bundle")
        chain = self.chain_tier()
        counts = {}
        for check in checks:
            counts[check["status"]] = counts.get(check["status"], 0) + 1
        if any(check["status"] == FAIL for check in checks):
            exit_code = EXIT_FAIL
        elif (
            (requested and sampled != PASS)
            or metadata != PASS
            or (self.require_chain_truth and chain != PASS)
            or (not requested and not self.verify_all_metadata)
        ):
            exit_code = EXIT_INCOMPLETE
        else:
            exit_code = EXIT_PASS
        return {
            "format": self.rules["report_format"],
            "engine": "python",
            "rules_sha256": file_sha256(RULES_PATH),
            "bundle": {
                "path": str(self.bundle_dir),
                "bundle_id": self.manifest.get("bundle_id"),
                "manifest_sha256": self.manifest_sha256,
                "cutoff": self.manifest.get("cutoff"),
                "network": self.manifest.get("network"),
                "chain_ids": self.manifest.get("chain_ids"),
                "threshold_atto": self.manifest.get("threshold_atto"),
                "airdrop_scope": self.manifest.get("airdrop_scope", "partial"),
                "files": [
                    {
                        "role": entry.get("role"),
                        "category": entry.get("category", ""),
                        "path": entry.get("path"),
                        "declared_sha256": entry.get("sha256"),
                        "actual_sha256": self.file_hashes.get(entry.get("path"), ""),
                        "rows": entry.get("rows"),
                        "source": entry.get("source"),
                    }
                    for entry in self.manifest["files"]
                    if isinstance(entry, dict)
                ],
            },
            "sample": {
                "algorithm": self.rules["sample_algorithm"]["name"],
                "seed": self.seed,
                "seed_generated": self.seed_generated,
                "sample_by": self.sample_by,
                "requested": self.sample_size or 0,
                "population": population,
                "identifiers": [item["identifier"] for item in sample],
                "reproduce": self.reproduce_command(),
            },
            "tiers": {
                "sampled_rows": sampled,
                "bundle_metadata": metadata,
                "chain_truth": chain,
            },
            "verify_all_metadata": self.verify_all_metadata,
            "totals": {key: str(value) for key, value in sorted(self.totals.items())},
            "rows": self.row_results,
            "show_row": self.show_result,
            "checks": checks,
            "counts": counts,
            "exit_code": exit_code,
        }

    def chain_tier(self):
        chain_checks = [check for check in self.report.checks if check["scope"] == "chain"]
        if any(check["status"] == FAIL for check in chain_checks):
            return FAIL
        if not chain_checks or any(check["status"] in (NOT_VERIFIED, WARNING) for check in chain_checks):
            return NOT_VERIFIED
        return PASS

    def reproduce_command(self):
        parts = [
            "python3 toolkit/scripts/random-sample-verify.py",
            f"--bundle {self.bundle_dir}",
            f"--sample-size {self.sample_size or 0}",
            f"--seed {self.seed}",
        ]
        if self.sample_by != "secure_key":
            parts.append(f"--sample-by {self.sample_by}")
        return " ".join(parts)


CLAIM_COMPONENTS = (
    "liquid_shard0_atto",
    "liquid_shard1_atto",
    "active_staked_or_delegated_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "pending_cross_shard_atto",
    "wone_balance_atto",
)
ACCOUNT_EVIDENCE_FIELDS = (
    "balance_atto",
    "active_staked_or_delegated_atto",
    "pending_undelegation_atto",
    "unclaimed_staking_reward_atto",
    "pending_cross_shard_atto",
    "wone_balance_atto",
)


def account_components(shard):
    """Map archived account fields to claim components for a shard record."""
    return (
        ("balance_atto", f"liquid_shard{shard}_atto"),
        ("active_staked_or_delegated_atto", "active_staked_or_delegated_atto"),
        ("pending_undelegation_atto", "pending_undelegation_atto"),
        ("unclaimed_staking_reward_atto", "unclaimed_staking_reward_atto"),
        ("pending_cross_shard_atto", "pending_cross_shard_atto"),
        ("wone_balance_atto", "wone_balance_atto"),
    )


class SortedKeyCursor:
    """Walk a secure-key-sorted CSV's keys in lockstep with another sorted file."""

    def __init__(self, path):
        self.handle = Path(path).open(newline="", encoding="utf-8")
        self.reader = strict_csv_reader(self.handle)
        header = next(self.reader, None) or []
        self.index = header.index("secure_key") if "secure_key" in header else None
        self.current = None
        self.advance()

    def advance(self):
        self.current = None
        if self.index is None:
            return
        for record in self.reader:
            if record and len(record) > self.index:
                self.current = record[self.index].lower()
                return

    def contains(self, key):
        while self.current is not None and self.current < key:
            self.advance()
        return self.current == key

    def close(self):
        self.handle.close()


def verify_bundle(bundle_dir, **options):
    return Verifier(bundle_dir, **options).run()


# ---------------------------------------------------------------------------
# bundle building (shared with build-evidence-bundle.py and tests)
# ---------------------------------------------------------------------------


def count_csv_rows(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = strict_csv_reader(handle)
        next(reader, None)
        return sum(1 for record in reader if record)


def compute_declared_totals(bundle_dir, manifest, rules=None, snapshot=None):
    """Recompute every total for a staged bundle; used when freezing it."""
    verifier = Verifier(bundle_dir, sample_size=0, seed="bundle-build", rules=rules, snapshot=snapshot)
    verifier.manifest = manifest
    for entry in manifest["files"]:
        if entry["role"] in verifier.roles:
            verifier.roles[entry["role"]].present = True
            verifier.roles[entry["role"]].files.append({**entry, "abs": Path(bundle_dir) / entry["path"]})
    for role in verifier.rules["role_order"]:
        if verifier.roles[role].present:
            sums, add = verifier.make_accumulator(role)
            verifier.iter_role_rows(role, add)
            if role in verifier.tainted_roles:
                raise ValueError(f"{role} has invalid rows; fix them before freezing the bundle: "
                                 + " | ".join(verifier.roles[role].examples))
            verifier.totals.update(sums)
    return {key: str(value) for key, value in sorted(verifier.totals.items())}
