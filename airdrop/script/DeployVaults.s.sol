// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";

/// @notice Sends the GovernorDelegator vault deployments planned by `vault-batch deploy`, from a private key.
///
///   python3 tools/vault_batch.py deploy --out-dir runs/<name> --rpc-url $RPC_URL [--broadcast]
///
/// The Python tool writes the plan (one call to the standard CREATE2 deployer per vault, and the address
/// each call must create) and runs this script with the sender's key in VAULT_SENDER_PRIVATE_KEY. The
/// script only replays those bytes, one transaction per vault, and stops unless each call creates the
/// planned address. Never run it with --json: the raw traces it prints include the key.
contract DeployVaults is Script {
    address internal constant FACTORY = 0x4e59b44847b379578588920cA78FbF26c0B4956C;
    bytes32 internal constant FACTORY_CODE_HASH = 0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989;

    function sender() external view returns (address) {
        return vm.addr(vm.envUint("VAULT_SENDER_PRIVATE_KEY"));
    }

    function run(string memory planPath) external {
        uint256 key = vm.envUint("VAULT_SENDER_PRIVATE_KEY");
        string memory plan = vm.readFile(planPath);
        require(block.chainid == vm.parseJsonUint(plan, ".chain_id"), "the RPC is on another chain than the plan");
        require(vm.addr(key) == vm.parseJsonAddress(plan, ".sender"), "the key does not belong to the plan's sender");
        require(FACTORY.codehash == FACTORY_CODE_HASH, "no standard CREATE2 deployer on this chain");
        address[] memory vaults = vm.parseJsonAddressArray(plan, ".vaults");
        bytes[] memory data = vm.parseJsonBytesArray(plan, ".data");
        // From the node's eth_estimateGas: forge's local EVM can price state creation lower than the chain does.
        uint256[] memory gasLimits = vm.parseJsonUintArray(plan, ".gas");
        require(
            vaults.length > 0 && vaults.length == data.length && vaults.length == gasLimits.length,
            "the plan's vaults, call data and gas limits do not pair up"
        );

        for (uint256 i; i < vaults.length; ++i) {
            require(vaults[i].code.length == 0, string.concat("already deployed: ", vm.toString(vaults[i])));
            vm.broadcast(key);
            (bool ok, bytes memory created) = FACTORY.call{gas: gasLimits[i]}(data[i]);
            require(
                ok && keccak256(created) == keccak256(abi.encodePacked(vaults[i])),
                string.concat("the deployment did not create ", vm.toString(vaults[i]))
            );
            console.log("vault", i + 1, vaults[i]);
        }
    }
}
