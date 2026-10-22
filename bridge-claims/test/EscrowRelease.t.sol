// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Test, console} from "forge-std/Test.sol";

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}

interface IWrapped {
    function deposit() external payable;
}

interface IEscrow {
    function token() external view returns (address);
    function lzEndpoint() external view returns (address);
    function trustedRemoteLookup(uint16) external view returns (bytes memory);
}

interface IEndpointV1 {
    function uaConfigLookup(address)
        external
        view
        returns (uint16 sendVersion, uint16 receiveVersion, address receiveLibrary, address sendLibrary);
    function getReceiveLibraryAddress(address) external view returns (address);
    function getInboundNonce(uint16, bytes calldata) external view returns (uint64);
    function hasStoredPayload(uint16, bytes calldata) external view returns (bool);
}

interface ISafe {
    function nonce() external view returns (uint256);
    function getThreshold() external view returns (uint256);
    function getOwners() external view returns (address[] memory);
    function getTransactionHash(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address refundReceiver,
        uint256 nonce
    ) external view returns (bytes32);
    function execTransaction(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address payable refundReceiver,
        bytes memory signatures
    ) external payable returns (bool);
}

/// @notice Replays a run's release files (`tools/release_plan.py`) on a fork of one chain and proves
///         that they release every escrow on that chain to the custody Safe and fund every approval:
///
///         - each Safe batch is rebuilt from its Transaction Builder file, must hash to the Safe
///           transaction hash on the signing sheet at the Safe's live nonce, and is executed through
///           the real Safe (threshold lowered to 1 and an approved-hash signature from one owner
///           standing in for the real signatures);
///         - each EOA file is sent transaction by transaction from its owner;
///         - every escrow must end empty, the Safe must receive its locked balance (less PAXG's
///           transfer fee), the local path must advance by exactly one nonce, and every setting the
///           release touches (trusted remotes, receive version, verifier and executor configs for
///           the local and the Harmony path) must read the same as before;
///         - every batch must stay under the 16,777,216 per-transaction gas cap (EIP-7825);
///         - after wrapping the native coin, the Safe must hold every approval in the manifest.
///
///     CLAIMS_DIR=runs/<run>/<label> RELEASE_DIR=runs/<run>/release FORK_RPC_URL=<archive RPC> \
///       forge test --match-contract EscrowRelease -vv
///
/// No LayerZero verifier, executor, or cross-chain message is involved, so the fork covers the whole
/// path.
contract EscrowReleaseTest is Test {
    uint16 internal constant HARMONY = 116;
    uint256 internal constant TX_GAS_CAP = 16_777_216;
    /// Safe storage slot of `threshold` (Safe 1.3.0 and 1.4.1).
    bytes32 internal constant THRESHOLD_SLOT = bytes32(uint256(4));

    struct Escrow {
        address escrow;
        bool native;
        address asset;
        uint256 locked;
        uint256 safeBefore;
        uint64 pathNonce;
        bytes32 settings;
    }

    string internal manifest;
    string internal plan;
    string internal releaseDir;
    string internal chain;
    address internal safe;
    address internal wrapped;
    uint16 internal localEid;
    uint256 internal failures;

    function setUp() public {
        string memory dir = vm.envOr("CLAIMS_DIR", string(""));
        releaseDir = vm.envOr("RELEASE_DIR", string(""));
        string memory rpc = vm.envOr("FORK_RPC_URL", string(""));
        if (bytes(dir).length == 0 || bytes(releaseDir).length == 0 || bytes(rpc).length == 0) {
            vm.skip(true);
            return;
        }
        manifest = vm.readFile(string.concat(dir, "/manifest.json"));
        plan = vm.readFile(string.concat(releaseDir, "/release-plan.json"));
        vm.createSelectFork(rpc);
        assertEq(block.chainid, vm.parseJsonUint(manifest, ".chain_id"), "fork is another chain");
        chain = vm.parseJsonString(manifest, ".chain");
        safe = vm.parseJsonAddress(manifest, ".funder");
        assertEq(vm.parseJsonAddress(plan, ".safe"), safe, "release plan pays another Safe");
        wrapped = vm.parseJsonAddress(manifest, ".deployment.constructor.wrappedNative");
        localEid = _lzChainId(block.chainid);
    }

    function _lzChainId(uint256 chainId) internal pure returns (uint16) {
        if (chainId == 1) return 101;
        if (chainId == 56) return 102;
        if (chainId == 42161) return 110;
        if (chainId == 8453) return 184;
        if (chainId == 59144) return 183;
        revert("no LayerZero v1 chain id");
    }

    function _count(string memory json, string memory array) internal view returns (uint256 n) {
        while (vm.keyExistsJson(json, string.concat(array, "[", vm.toString(n), "]"))) n++;
    }

    function _same(string memory a, string memory b) internal pure returns (bool) {
        return keccak256(bytes(a)) == keccak256(bytes(b));
    }

    function _raw(address target, bytes memory data) internal view returns (bytes memory ret) {
        bool ok;
        (ok, ret) = target.staticcall(data);
        require(ok, "settings read failed");
    }

    /// @dev Every setting the release writes and must write back: both trusted remotes, the stored
    ///      receive version and library, and the verifier and executor configs of both paths in the
    ///      receive library in effect.
    function _settings(address escrow) internal view returns (bytes32) {
        IEndpointV1 endpoint = IEndpointV1(IEscrow(escrow).lzEndpoint());
        (uint16 sendVersion, uint16 receiveVersion, address receiveLibrary, address sendLibrary) =
            endpoint.uaConfigLookup(escrow);
        address lib = endpoint.getReceiveLibraryAddress(escrow);
        bytes memory paths;
        for (uint256 i = 0; i < 2; i++) {
            uint16 eid = i == 0 ? localEid : HARMONY;
            paths = abi.encode(
                paths,
                IEscrow(escrow).trustedRemoteLookup(eid),
                _raw(lib, abi.encodeWithSignature("getAppUlnConfig(address,uint32)", escrow, uint32(eid))),
                _raw(lib, abi.encodeWithSignature("executors(address,uint32)", escrow, uint32(eid)))
            );
        }
        bytes memory harmonyPath = IEscrow(escrow).trustedRemoteLookup(HARMONY);
        return keccak256(
            abi.encode(
                paths,
                sendVersion,
                receiveVersion,
                receiveLibrary,
                sendLibrary,
                lib,
                harmonyPath.length > 0 ? endpoint.getInboundNonce(HARMONY, harmonyPath) : 0
            )
        );
    }

    function _locked(address escrow, bool native, address asset) internal view returns (uint256) {
        return native ? IERC20(escrow).balanceOf(escrow) : IERC20(asset).balanceOf(escrow);
    }

    function _heldBy(address holder, bool native, address asset) internal view returns (uint256) {
        return native ? holder.balance : IERC20(asset).balanceOf(holder);
    }

    function _path(address escrow) internal view returns (bytes memory) {
        return abi.encodePacked(safe, escrow);
    }

    function _snapshot(string memory key) internal view returns (Escrow memory e) {
        e.escrow = vm.parseJsonAddress(plan, string.concat(key, ".escrow"));
        e.native = _same(vm.parseJsonString(plan, string.concat(key, ".kind")), "native");
        e.asset = e.native ? address(0) : IEscrow(e.escrow).token();
        e.locked = _locked(e.escrow, e.native, e.asset);
        e.safeBefore = _heldBy(safe, e.native, e.asset);
        e.pathNonce = IEndpointV1(IEscrow(e.escrow).lzEndpoint()).getInboundNonce(localEid, _path(e.escrow));
        e.settings = _settings(e.escrow);
    }

    function _check(Escrow memory e) internal {
        IEndpointV1 endpoint = IEndpointV1(IEscrow(e.escrow).lzEndpoint());
        bool ok = e.locked > 0 && _locked(e.escrow, e.native, e.asset) == 0
            && endpoint.getInboundNonce(localEid, _path(e.escrow)) == e.pathNonce + 1
            && !endpoint.hasStoredPayload(localEid, _path(e.escrow)) && _settings(e.escrow) == e.settings;
        console.log(ok ? "  escrow ok   " : "  escrow FAIL ", e.escrow, e.locked);
        if (!ok) failures++;
    }

    /// @dev What the Safe received of each asset against the sum locked in that asset's escrows
    ///      (Ethereum has two ETH escrows). Fee-on-transfer tokens (PAXG) deliver slightly less.
    function _checkCredits(Escrow[] memory escrows, uint256 m) internal {
        for (uint256 j = 0; j < m; j++) {
            bool first = true;
            uint256 locked;
            for (uint256 k = 0; k < m; k++) {
                if (escrows[k].native != escrows[j].native || escrows[k].asset != escrows[j].asset) continue;
                if (k < j) first = false;
                locked += escrows[k].locked;
            }
            if (!first) continue;
            uint256 got = _heldBy(safe, escrows[j].native, escrows[j].asset) - escrows[j].safeBefore;
            bool ok = got >= locked - locked / 1000 && got <= locked;
            console.log(
                ok ? "  credit ok   " : "  credit FAIL ", escrows[j].native ? address(0) : escrows[j].asset
            );
            console.log("      locked / credited", locked, got);
            if (!ok) failures++;
        }
    }

    /// @dev A Transaction Builder entry carries either raw `data` or a `contractMethod` with
    ///      `contractInputsValues`; the planner only emits methods shaped `(address, uint256)`
    ///      (`transfer`, `addOwnerWithThreshold`), which are encoded here the way the Safe app does.
    function _parseCall(string memory file, string memory key)
        internal
        view
        returns (address to, uint256 value, bytes memory data)
    {
        to = vm.parseJsonAddress(file, string.concat(key, ".to"));
        value = vm.parseJsonUint(file, string.concat(key, ".value"));
        string memory method = string.concat(key, ".contractMethod");
        if (!vm.keyExistsJson(file, string.concat(method, ".name"))) {
            data = vm.parseJsonBytes(file, string.concat(key, ".data"));
            return (to, value, data);
        }
        assertEq(_count(file, string.concat(method, ".inputs")), 2, "unexpected contract method");
        assertEq(vm.parseJsonString(file, string.concat(method, ".inputs[0].type")), "address");
        assertEq(vm.parseJsonString(file, string.concat(method, ".inputs[1].type")), "uint256");
        string memory signature =
            string.concat(vm.parseJsonString(file, string.concat(method, ".name")), "(address,uint256)");
        string memory values = string.concat(key, ".contractInputsValues.");
        string memory first = vm.parseJsonString(file, string.concat(method, ".inputs[0].name"));
        string memory second = vm.parseJsonString(file, string.concat(method, ".inputs[1].name"));
        data = abi.encodeWithSignature(
            signature,
            vm.parseJsonAddress(file, string.concat(values, first)),
            vm.parseJsonUint(file, string.concat(values, second))
        );
    }

    function _execSafeBatch(string memory key, string memory file, address sender)
        internal
        returns (uint256 gas)
    {
        assertEq(
            vm.parseJsonAddress(file, ".meta.createdFromSafeAddress"), sender, "batch is for another Safe"
        );
        uint256 n = _count(file, ".transactions");
        address to;
        uint256 value;
        bytes memory data;
        uint8 operation;
        if (n == 1) {
            (to, value, data) = _parseCall(file, ".transactions[0]");
        } else {
            bytes memory calls;
            for (uint256 i = 0; i < n; i++) {
                (address t, uint256 v, bytes memory d) =
                    _parseCall(file, string.concat(".transactions[", vm.toString(i), "]"));
                calls = abi.encodePacked(calls, uint8(0), t, v, d.length, d);
            }
            to = vm.parseJsonAddress(plan, string.concat(key, ".to"));
            data = abi.encodeWithSignature("multiSend(bytes)", calls);
            operation = 1;
        }
        assertEq(operation, vm.parseJsonUint(plan, string.concat(key, ".operation")), "operation");
        uint256 nonce = vm.parseJsonUint(plan, string.concat(key, ".planned_nonce"));
        assertEq(ISafe(sender).nonce(), nonce, "the Safe's nonce moved since the plan was built");
        bytes32 hash = ISafe(sender)
            .getTransactionHash(to, value, data, operation, 0, 0, 0, address(0), address(0), nonce);
        assertEq(hash, vm.parseJsonBytes32(plan, string.concat(key, ".safe_tx_hash")), "Safe tx hash");

        uint256 threshold = ISafe(sender).getThreshold();
        if (threshold > 1) vm.store(sender, THRESHOLD_SLOT, bytes32(uint256(1)));
        address signer = ISafe(sender).getOwners()[0];
        bytes memory signature = abi.encodePacked(uint256(uint160(signer)), uint256(0), uint8(1));
        vm.prank(signer);
        gas = gasleft();
        bool ok = ISafe(sender)
            .execTransaction(to, value, data, operation, 0, 0, 0, address(0), payable(0), signature);
        gas -= gasleft();
        if (threshold > 1) vm.store(sender, THRESHOLD_SLOT, bytes32(threshold));
        assertTrue(ok, "Safe batch failed");
    }

    function _sendEoaFile(string memory file, address sender) internal returns (uint256 gas) {
        assertEq(vm.parseJsonAddress(file, ".from"), sender, "calldata is for another sender");
        uint256 n = _count(file, ".transactions");
        for (uint256 i = 0; i < n; i++) {
            (address to, uint256 value, bytes memory data) =
                _parseCall(file, string.concat(".transactions[", vm.toString(i), "]"));
            vm.prank(sender);
            uint256 before = gasleft();
            (bool ok, bytes memory ret) = to.call{value: value}(data);
            uint256 used = before - gasleft();
            if (used > gas) gas = used;
            if (!ok) {
                failures++;
                console.log("FAIL transaction", i);
                console.logBytes(ret);
            }
        }
    }

    function test_ReleaseFilesFundEveryApproval() public {
        uint256 total = _count(plan, ".escrows");
        Escrow[] memory escrows = new Escrow[](total);
        uint256 m;
        for (uint256 i = 0; i < total; i++) {
            string memory key = string.concat(".escrows[", vm.toString(i), "]");
            if (_same(vm.parseJsonString(plan, string.concat(key, ".chain")), chain)) {
                escrows[m++] = _snapshot(key);
            }
        }
        assertGt(m, 0, "no escrows on this chain");

        uint256 batches = _count(plan, ".batches");
        for (uint256 i = 0; i < batches; i++) {
            string memory key = string.concat(".batches[", vm.toString(i), "]");
            if (!_same(vm.parseJsonString(plan, string.concat(key, ".chain")), chain)) continue;
            string memory name = vm.parseJsonString(plan, string.concat(key, ".file"));
            string memory file = vm.readFile(string.concat(releaseDir, "/", name));
            address sender = vm.parseJsonAddress(plan, string.concat(key, ".sender"));
            uint256 gas = vm.parseJsonBool(plan, string.concat(key, ".sender_is_safe"))
                ? _execSafeBatch(key, file, sender)
                : _sendEoaFile(file, sender);
            console.log(name, gas < TX_GAS_CAP ? "gas" : "OVER GAS CAP", gas);
            if (gas >= TX_GAS_CAP) failures++;
        }

        for (uint256 j = 0; j < m; j++) {
            _check(escrows[j]);
        }
        _checkCredits(escrows, m);

        uint256 approvals = _count(manifest, ".approvals");
        for (uint256 i = 0; i < approvals; i++) {
            string memory key = string.concat(".approvals[", vm.toString(i), "]");
            address token = vm.parseJsonAddress(manifest, string.concat(key, ".token"));
            uint256 need = vm.parseJsonUint(manifest, string.concat(key, ".amount_raw"));
            string memory symbol = vm.parseJsonString(manifest, string.concat(key, ".symbol"));
            if (token == wrapped && safe.balance >= need) {
                vm.prank(safe);
                IWrapped(wrapped).deposit{value: need}();
            }
            uint256 have = IERC20(token).balanceOf(safe);
            bool funded = have >= need;
            console.log(funded ? "funded      " : "UNDERFUNDED ", symbol, have, need);
            if (!funded) failures++;
        }
        assertEq(failures, 0, "some escrows did not release cleanly or some approvals are underfunded");
    }
}
