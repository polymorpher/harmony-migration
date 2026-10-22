// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Test, console} from "forge-std/Test.sol";
import {BridgeClaims} from "../src/BridgeClaims.sol";

interface IToken {
    function balanceOf(address account) external view returns (uint256);
}

interface IWrapped {
    function deposit() external payable;
}

/// @notice One run's contract against the real chain: deploys it through the CREATE2 deployer on a
///         fork, gives the funder Safe each asset's total (as moving the collateral will), approves
///         exactly the manifest's approvals from the Safe, and claims the first row of every asset
///         with its real token. Skipped unless both variables are set:
///
///     CLAIMS_DIR=runs/<run>/<label> FORK_RPC_URL=<archive RPC> forge test --match-contract Fork -vv
contract ForkTest is Test {
    address internal constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;
    address internal constant NATIVE = address(0);

    string internal manifest;
    string internal samples;
    BridgeClaims internal claims;
    address internal funder;
    address internal wrapped;

    function setUp() public {
        string memory dir = vm.envOr("CLAIMS_DIR", string(""));
        string memory rpc = vm.envOr("FORK_RPC_URL", string(""));
        if (bytes(dir).length == 0 || bytes(rpc).length == 0) {
            vm.skip(true);
            return;
        }
        manifest = vm.readFile(string.concat(dir, "/manifest.json"));
        samples = vm.readFile(string.concat(dir, "/samples.json"));
        vm.createSelectFork(rpc);
        assertEq(block.chainid, vm.parseJsonUint(manifest, ".chain_id"), "fork is another chain");
        funder = vm.parseJsonAddress(manifest, ".deployment.constructor.funder");
        wrapped = vm.parseJsonAddress(manifest, ".deployment.constructor.wrappedNative");
        claims = _deploy();
        _fundAndApprove();
    }

    function _deploy() internal returns (BridgeClaims) {
        bytes memory initCode = abi.encodePacked(
            type(BridgeClaims).creationCode,
            abi.encode(
                funder,
                wrapped,
                vm.parseJsonUint(manifest, ".deployment.constructor.chainId"),
                vm.parseJsonBytes32(manifest, ".deployment.constructor.root"),
                vm.parseJsonUint(manifest, ".deployment.constructor.claimCount"),
                vm.parseJsonBytes32(manifest, ".deployment.constructor.listHash")
            )
        );
        address expected = vm.parseJsonAddress(manifest, ".deployment.address");
        assertEq(expected.code.length, 0, "already deployed on this chain");
        bytes32 salt = vm.parseJsonBytes32(manifest, ".deployment.salt");
        (bool ok, bytes memory ret) = CREATE2_DEPLOYER.call(abi.encodePacked(salt, initCode));
        assertTrue(ok, "CREATE2 deployment failed");
        // The deployer returns exactly the 20-byte address of the new contract.
        // forge-lint: disable-next-line(unsafe-typecast)
        assertEq(address(bytes20(ret)), expected, "deployed address differs from the manifest");
        return BridgeClaims(payable(expected));
    }

    function _count(string memory json, string memory array) internal view returns (uint256 n) {
        while (vm.keyExistsJson(json, string.concat(array, "[", vm.toString(n), "]"))) n++;
    }

    /// @dev What the Safe ends up with after the collateral move, the wrap, and the approve batch.
    ///      ERC-20 collateral is moved for real, from each escrow (or the reserve Safe for ONE) listed
    ///      in the manifest's `sources`, so tokens with transfer hooks (FXS vote checkpoints, PAXG
    ///      fees) behave as they will on chain. The native coin is credited and wrapped.
    function _fundAndApprove() internal {
        uint256 n = _count(manifest, ".approvals");
        for (uint256 i = 0; i < n; i++) {
            string memory key = string.concat(".approvals[", vm.toString(i), "]");
            address token = vm.parseJsonAddress(manifest, string.concat(key, ".token"));
            uint256 amount = vm.parseJsonUint(manifest, string.concat(key, ".amount_raw"));
            if (token == wrapped) {
                vm.deal(funder, funder.balance + amount);
                vm.prank(funder);
                IWrapped(wrapped).deposit{value: amount}();
            } else {
                _moveCollateral(token, string.concat(key, ".sources"));
                assertGe(IToken(token).balanceOf(funder), amount, "collateral below the approval");
            }
            vm.prank(funder);
            (bool ok, bytes memory ret) =
                token.call(abi.encodeWithSignature("approve(address,uint256)", address(claims), amount));
            assertTrue(ok && (ret.length == 0 || abi.decode(ret, (bool))), "approve failed");
        }
    }

    function _moveCollateral(address token, string memory sources) internal {
        uint256 n = _count(manifest, sources);
        assertGt(n, 0, "approval without a collateral source");
        for (uint256 j = 0; j < n; j++) {
            string memory key = string.concat(sources, "[", vm.toString(j), "]");
            address from = vm.parseJsonAddress(manifest, string.concat(key, ".from"));
            uint256 amount = vm.parseJsonUint(manifest, string.concat(key, ".amount_raw"));
            uint256 held = IToken(token).balanceOf(from);
            if (held < amount) amount = held;
            vm.prank(from);
            (bool ok, bytes memory ret) =
                token.call(abi.encodeWithSignature("transfer(address,uint256)", funder, amount));
            assertTrue(ok && (ret.length == 0 || abi.decode(ret, (bool))), "collateral transfer failed");
        }
    }

    function _received(address account, address token, uint256 nativeBefore, uint256 wrappedBefore, uint256 before)
        internal
        view
        returns (uint256)
    {
        if (token == NATIVE) {
            return (account.balance - nativeBefore) + (IToken(wrapped).balanceOf(account) - wrappedBefore);
        }
        return IToken(token).balanceOf(account) - before;
    }

    function test_SampleRowOfEveryAssetPays() public {
        uint256 n = _count(samples, ".rows");
        assertGt(n, 0, "no sample rows");
        uint256 failures;
        for (uint256 i = 0; i < n; i++) {
            string memory key = string.concat(".rows[", vm.toString(i), "]");
            uint256 index = vm.parseJsonUint(samples, string.concat(key, ".index"));
            address account = vm.parseJsonAddress(samples, string.concat(key, ".account"));
            address token = vm.parseJsonAddress(samples, string.concat(key, ".token"));
            uint256 amount = vm.parseJsonUint(samples, string.concat(key, ".amount_raw"));
            bytes32[] memory proof = vm.parseJsonBytes32Array(samples, string.concat(key, ".proof"));
            string memory symbol = vm.parseJsonString(samples, string.concat(key, ".symbol"));

            uint256 nativeBefore = account.balance;
            uint256 wrappedBefore = wrapped == address(0) ? 0 : IToken(wrapped).balanceOf(account);
            uint256 before = token == NATIVE ? 0 : IToken(token).balanceOf(account);
            try claims.claim(index, account, token, amount, proof) {
                uint256 got = _received(account, token, nativeBefore, wrappedBefore, before);
                // Fee-on-transfer tokens (PAXG) deliver slightly less than the row.
                bool ok = got <= amount && got >= amount - amount / 1000 && claims.isClaimed(index);
                console.log(ok ? "ok  " : "BAD ", symbol, index, got);
                if (!ok) failures++;
            } catch (bytes memory reason) {
                console.log("FAIL", symbol, index);
                console.logBytes(reason);
                failures++;
            }
        }
        assertEq(failures, 0, "some sample rows did not pay");
    }
}
