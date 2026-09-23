// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";
import {CommittedBatchAirdrop} from "../src/CommittedBatchAirdrop.sol";
import {RunFiles} from "./lib/RunFiles.sol";
import {BatchTree} from "./lib/BatchTree.sol";
import {Env} from "./lib/Env.sol";

/// @notice Independent check of a run directory, written in Solidity so it does not share code with
///         the Python builder. Recomputes every batch fingerprint and the root from the batch files,
///         checks the totals and the list hash against the manifest, and, when AIRDROP_ADDRESS is
///         set, compares everything with the deployed contract.
///
///   forge script script/VerifyRoot.s.sol --sig "run(string)" runs/<name> [--rpc-url $RPC_URL]
contract VerifyRoot is Script {
    function run(string memory runDir) external view {
        RunFiles.Manifest memory m = RunFiles.readManifest(runDir);
        require(m.batchCount > 0, "manifest has no batches");

        bytes32[] memory leaves = new bytes32[](m.batchCount);
        uint256 total;
        uint256 recipients;
        for (uint256 i = 0; i < m.batchCount; i++) {
            RunFiles.Batch memory b = RunFiles.readBatch(runDir, i);
            bytes32 leaf = BatchTree.leaf(i, b.recipients, b.amounts);
            require(leaf == b.leaf, "batch file leaf differs from recomputed leaf");
            require(BatchTree.verify(b.proof, m.root, leaf), "batch file proof does not reach manifest root");
            leaves[i] = leaf;
            recipients += b.recipients.length;
            for (uint256 j = 0; j < b.amounts.length; j++) {
                require(b.recipients[j] != address(0), "zero recipient");
                require(b.amounts[j] != 0, "zero amount");
                total += b.amounts[j];
            }
        }
        require(BatchTree.root(leaves) == m.root, "recomputed root differs from manifest root");
        require(total == m.totalAmount, "sum of amounts differs from manifest total_amount");
        require(recipients == m.recipientCount, "recipient count differs from manifest recipient_count");
        require(
            RunFiles.distributionHash(runDir) == m.listHash, "distribution.csv sha256 differs from manifest"
        );

        console.log("root             ", vm.toString(m.root));
        console.log("batches          ", m.batchCount);
        console.log("recipients       ", recipients);
        console.log("total amount     ", total);
        console.log("list sha256      ", vm.toString(m.listHash));
        console.log("run directory OK: batch files, proofs, totals and list hash are consistent");

        address addr = Env.airdrop();
        if (addr != address(0)) {
            CommittedBatchAirdrop a = CommittedBatchAirdrop(addr);
            require(a.root() == m.root, "on-chain root differs");
            require(a.batchCount() == m.batchCount, "on-chain batchCount differs");
            require(a.totalAmount() == m.totalAmount, "on-chain totalAmount differs");
            require(a.listHash() == m.listHash, "on-chain listHash differs");
            console.log("contract         ", addr);
            console.log("token            ", address(a.token()));
            console.log("reserve          ", a.reserve());
            console.log("admin            ", a.admin());
            console.log("executor         ", a.executor());
            console.log("executed batches ", a.executedBatches());
            console.log("contract OK: root, batch count, total and list hash match this run");
        } else {
            console.log("AIRDROP_ADDRESS not set; skipped on-chain comparison");
        }
    }
}
