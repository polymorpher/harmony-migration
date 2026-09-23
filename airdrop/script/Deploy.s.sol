// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";
import {VmSafe} from "forge-std/Vm.sol";
import {CommittedBatchAirdrop} from "../src/CommittedBatchAirdrop.sol";
import {IERC20} from "../src/IERC20.sol";
import {RunFiles} from "./lib/RunFiles.sol";
import {Env} from "./lib/Env.sol";

/// @notice Deploys a CommittedBatchAirdrop for one run directory.
///
///   forge script script/Deploy.s.sol --sig "run(string)" runs/<name> --rpc-url $RPC_URL --broadcast
///
/// Reads the root, batch count, total and list hash from <run>/manifest.json and the role addresses
/// from .env. Writes <run>/deployment-<chainid>.json when actually broadcasting.
contract Deploy is Script {
    function run(string memory runDir) external returns (CommittedBatchAirdrop airdrop) {
        Env.requireChain();
        Env.Roles memory r = Env.roles();
        RunFiles.Manifest memory m = RunFiles.readManifest(runDir);
        require(
            RunFiles.distributionHash(runDir) == m.listHash,
            "distribution.csv does not match manifest list_sha256"
        );

        console.log("chain id        ", block.chainid);
        console.log("token           ", r.token);
        console.log("reserve         ", r.reserve);
        console.log("admin           ", r.admin);
        console.log("executor        ", r.executor);
        console.log("root            ", vm.toString(m.root));
        console.log("batch count     ", m.batchCount);
        console.log("recipient count ", m.recipientCount);
        console.log("total amount    ", m.totalAmount);
        console.log("list sha256     ", vm.toString(m.listHash));

        vm.startBroadcast();
        airdrop = new CommittedBatchAirdrop(
            IERC20(r.token), r.reserve, r.admin, r.executor, m.root, m.batchCount, m.totalAmount, m.listHash
        );
        vm.stopBroadcast();

        console.log("airdrop         ", address(airdrop));

        if (vm.isContext(VmSafe.ForgeContext.ScriptBroadcast)) {
            string memory key = "deployment";
            vm.serializeUint(key, "chain_id", block.chainid);
            vm.serializeAddress(key, "airdrop", address(airdrop));
            vm.serializeAddress(key, "token", r.token);
            vm.serializeAddress(key, "reserve", r.reserve);
            vm.serializeAddress(key, "admin", r.admin);
            vm.serializeAddress(key, "executor", r.executor);
            vm.serializeBytes32(key, "root", m.root);
            vm.serializeUint(key, "batch_count", m.batchCount);
            vm.serializeUint(key, "recipient_count", m.recipientCount);
            vm.serializeString(key, "total_amount", vm.toString(m.totalAmount));
            vm.serializeBytes32(key, "list_sha256", m.listHash);
            string memory json = vm.serializeUint(key, "deployed_at_block", block.number);
            string memory path = RunFiles.deploymentPath(runDir, block.chainid);
            vm.writeJson(json, path);
            console.log("wrote", path);
            console.log(
                "Next: set AIRDROP_ADDRESS in .env, verify the source on Etherscan, then have the reserve approve."
            );
        }
    }
}
