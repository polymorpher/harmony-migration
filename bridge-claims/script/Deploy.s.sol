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
/// Without `--broadcast` it only simulates. It refuses to deploy when the chain, the deployer, or the
/// resulting address differ from the manifest, and does nothing when the contract already exists.
/// Anyone can deploy: the address commits to the bytecode and every constructor argument, so the
/// deploying account has no say over the result.
contract Deploy is Script {
    address internal constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;

    function run() external returns (BridgeClaims claims) {
        string memory json = vm.readFile(vm.envString("MANIFEST"));
        require(
            keccak256(bytes(vm.parseJsonString(json, ".deployment.status"))) == keccak256("ready"),
            "manifest deployment is not ready (no funder or no bytecode)"
        );
        require(vm.parseJsonAddress(json, ".deployment.create2_deployer") == CREATE2_DEPLOYER, "deployer");
        require(CREATE2_DEPLOYER.code.length > 0, "no CREATE2 deployer on this chain");

        address funder = vm.parseJsonAddress(json, ".deployment.constructor.funder");
        address wrapped = vm.parseJsonAddress(json, ".deployment.constructor.wrappedNative");
        uint256 chainId = vm.parseJsonUint(json, ".deployment.constructor.chainId");
        bytes32 root = vm.parseJsonBytes32(json, ".deployment.constructor.root");
        uint256 count = vm.parseJsonUint(json, ".deployment.constructor.claimCount");
        bytes32 listHash = vm.parseJsonBytes32(json, ".deployment.constructor.listHash");
        bytes32 salt = vm.parseJsonBytes32(json, ".deployment.salt");
        address expected = vm.parseJsonAddress(json, ".deployment.address");
        require(block.chainid == chainId, "wrong chain");

        bytes memory initCode = abi.encodePacked(
            type(BridgeClaims).creationCode, abi.encode(funder, wrapped, chainId, root, count, listHash)
        );
        require(
            keccak256(initCode) == vm.parseJsonBytes32(json, ".deployment.init_code_keccak256"),
            "compiled bytecode differs from the manifest; rebuild with the pinned settings"
        );

        if (expected.code.length > 0) {
            console.log("already deployed:", expected);
            claims = BridgeClaims(payable(expected));
        } else {
            vm.startBroadcast();
            claims = new BridgeClaims{salt: salt}(funder, wrapped, chainId, root, count, listHash);
            vm.stopBroadcast();
            require(address(claims) == expected, "deployed address differs from the manifest");
            console.log("deployed:", address(claims));
        }

        require(claims.funder() == funder, "funder");
        require(claims.wrappedNative() == wrapped, "wrappedNative");
        require(claims.chainId() == chainId, "chainId");
        require(claims.root() == root, "root");
        require(claims.claimCount() == count, "claimCount");
        require(claims.listHash() == listHash, "listHash");
    }
}
