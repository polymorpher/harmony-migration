// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";
import {CommittedBatchAirdrop} from "../src/CommittedBatchAirdrop.sol";
import {IERC20} from "../src/IERC20.sol";
import {RunFiles} from "./lib/RunFiles.sol";
import {Env} from "./lib/Env.sol";

/// @notice Sends the published batches of a run, one transaction per batch.
///
///   forge script script/Execute.s.sol --sig "run(string,uint256,uint256)" runs/<name> 0 20 \
///       --rpc-url $RPC_URL --broadcast --slow
///
/// Batches that are already marked executed on-chain are skipped, so the command can be re-run
/// after an interruption. Without --broadcast the script only simulates.
contract Execute is Script {
    /// @param runDir  run directory produced by tools/build.py
    /// @param from    first batch index to send (inclusive)
    /// @param to      last batch index to send (exclusive); pass 0 for "through the last batch"
    function run(string memory runDir, uint256 from, uint256 to) external {
        _run(runDir, from, to);
    }

    /// @notice Convenience: every batch not yet executed.
    function runAll(string memory runDir) external {
        _run(runDir, 0, 0);
    }

    function _run(string memory runDir, uint256 from, uint256 to) private {
        Env.requireChain();
        CommittedBatchAirdrop airdrop = _airdropFor(runDir);
        if (to == 0 || to > airdrop.batchCount()) to = airdrop.batchCount();
        require(from < to, "empty batch range");

        IERC20 token = airdrop.token();
        address reserve = airdrop.reserve();
        require(!airdrop.paused(), "airdrop is paused");
        require(
            airdrop.executor() == msg.sender,
            "sender is not the executor (pass --sender EXECUTOR_ADDRESS or the executor's key)"
        );

        uint256 sent;
        uint256 skipped;
        for (uint256 i = from; i < to; i++) {
            if (airdrop.isExecuted(i)) {
                skipped++;
                continue;
            }
            RunFiles.Batch memory b = RunFiles.readBatch(runDir, i);
            require(
                airdrop.verifyBatch(i, b.recipients, b.amounts, b.proof),
                "batch file does not match the contract root"
            );

            uint256 sum;
            for (uint256 j = 0; j < b.amounts.length; j++) {
                sum += b.amounts[j];
            }
            require(
                token.allowance(reserve, address(airdrop)) >= sum, "reserve allowance too low for this batch"
            );
            require(token.balanceOf(reserve) >= sum, "reserve balance too low for this batch");

            vm.broadcast();
            airdrop.execute(i, b.recipients, b.amounts, b.proof);
            sent++;
            console.log("batch", i, "recipients", b.recipients.length);
        }
        console.log("sent", sent, "skipped (already executed)", skipped);
        console.log("executed batches", airdrop.executedBatches(), "of", airdrop.batchCount());
        console.log("distributed", airdrop.distributedAmount(), "of", airdrop.totalAmount());
    }

    function _airdropFor(string memory runDir) private view returns (CommittedBatchAirdrop airdrop) {
        address addr = Env.airdrop();
        require(addr != address(0), "set AIRDROP_ADDRESS in .env");
        airdrop = CommittedBatchAirdrop(addr);
        RunFiles.Manifest memory m = RunFiles.readManifest(runDir);
        require(airdrop.root() == m.root, "AIRDROP_ADDRESS was deployed for a different run (root mismatch)");
        require(airdrop.batchCount() == m.batchCount, "batch count mismatch");
        require(airdrop.totalAmount() == m.totalAmount, "total amount mismatch");
    }
}
