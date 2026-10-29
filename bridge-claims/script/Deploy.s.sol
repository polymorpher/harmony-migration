// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";
import {BridgeClaims} from "../src/BridgeClaims.sol";

/// @notice Deploys one BridgeClaims contract from its run manifest through the standard CREATE2
///         deployer, so it lands at the address in the manifest (the address the funder approves).
///
///     MANIFEST=runs/<run>/<label>/manifest.json forge script script/Deploy.s.sol \
///         --rpc-url "$RPC" --broadcast --account <keystore>
///
/// Without `--broadcast` it only simulates. The address is recomputed from the compiled init code,
/// never read from the manifest alone, and the deployment is a plain call to the deployer, so it does
/// not depend on how a Forge version routes `new{salt}`. It refuses to deploy when the chain, the
/// deployer's code, the bytecode, or the address differ from the manifest; when the contract already
/// exists it checks that the code there is this contract's; and it prints the runtime code hash to
/// compare with the signing sheet. Anyone can deploy: the address commits to the bytecode and every
/// constructor argument, so the deploying account has no say over the result.
contract Deploy is Script {
    address internal constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;
    /// @dev keccak256 of the deterministic-deployment proxy's 69-byte runtime code, the same on every chain.
    bytes32 internal constant CREATE2_DEPLOYER_CODEHASH =
        0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989;

    function run() external returns (BridgeClaims claims) {
        string memory json = vm.readFile(vm.envString("MANIFEST"));
        require(
            keccak256(bytes(vm.parseJsonString(json, ".deployment.status"))) == keccak256("ready"),
            "manifest deployment is not ready (no funder or no bytecode)"
        );
        require(vm.parseJsonAddress(json, ".deployment.create2_deployer") == CREATE2_DEPLOYER, "deployer");
        require(CREATE2_DEPLOYER.codehash == CREATE2_DEPLOYER_CODEHASH, "no canonical CREATE2 deployer on this chain");

        address funder = vm.parseJsonAddress(json, ".deployment.constructor.funder");
        address wrapped = vm.parseJsonAddress(json, ".deployment.constructor.wrappedNative");
        uint256 chainId = vm.parseJsonUint(json, ".deployment.constructor.chainId");
        bytes32 root = vm.parseJsonBytes32(json, ".deployment.constructor.root");
        uint256 count = vm.parseJsonUint(json, ".deployment.constructor.claimCount");
        bytes32 listHash = vm.parseJsonBytes32(json, ".deployment.constructor.listHash");
        bytes32 salt = vm.parseJsonBytes32(json, ".deployment.salt");
        require(block.chainid == chainId && vm.parseJsonUint(json, ".chain_id") == chainId, "wrong chain");
        require(funder == vm.parseJsonAddress(json, ".funder"), "constructor funder is not the manifest funder");
        require(root == vm.parseJsonBytes32(json, ".root"), "constructor root is not the manifest root");
        require(count == vm.parseJsonUint(json, ".claim_count"), "constructor claimCount is not the row count");

        require(
            keccak256(type(BridgeClaims).creationCode) == vm.parseJsonBytes32(json, ".deployment.bytecode_keccak256"),
            "compiled bytecode differs from the manifest; rebuild with the pinned settings"
        );
        bytes memory initCode = abi.encodePacked(
            type(BridgeClaims).creationCode, abi.encode(funder, wrapped, chainId, root, count, listHash)
        );
        require(keccak256(initCode) == vm.parseJsonBytes32(json, ".deployment.init_code_keccak256"), "init code");
        address expected = vm.computeCreate2Address(salt, keccak256(initCode), CREATE2_DEPLOYER);
        require(
            expected == vm.parseJsonAddress(json, ".deployment.address"),
            "manifest address is not the CREATE2 address of this init code"
        );
        if (expected.code.length == 0) {
            vm.startBroadcast();
            (bool ok, bytes memory ret) = CREATE2_DEPLOYER.call(abi.encodePacked(salt, initCode));
            vm.stopBroadcast();
            // The deployer returns exactly the 20-byte address of the new contract.
            // forge-lint: disable-next-line(unsafe-typecast)
            require(ok && ret.length == 20 && address(bytes20(ret)) == expected, "CREATE2 deployment failed");
            console.log("deployed:", expected);
        } else {
            // A local, never broadcast, copy through the same deployer under another salt has the exact
            // runtime code (immutables included) this init code leaves. Forge 1.8.1 fails every creation
            // outside a broadcast on Base; there, `approve_claims.py --rpc` makes the same comparison.
            (bool copied, bytes memory copy) = CREATE2_DEPLOYER.call(abi.encodePacked(~salt, initCode));
            require(copied && copy.length == 20, "no local copy to compare with; check with approve_claims.py --rpc");
            // forge-lint: disable-next-line(unsafe-typecast)
            require(expected.codehash == address(bytes20(copy)).codehash, "code at the address is not this BridgeClaims");
            console.log("already deployed:", expected);
        }
        console.log("runtime code keccak256:");
        console.logBytes32(expected.codehash);

        claims = BridgeClaims(payable(expected));
        require(claims.funder() == funder, "funder");
        require(claims.wrappedNative() == wrapped, "wrappedNative");
        require(claims.chainId() == chainId, "chainId");
        require(claims.root() == root, "root");
        require(claims.claimCount() == count, "claimCount");
        require(claims.listHash() == listHash, "listHash");
    }
}
