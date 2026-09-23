// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";
import {VmSafe} from "forge-std/Vm.sol";
import {CommittedBatchAirdrop} from "../src/CommittedBatchAirdrop.sol";
import {IERC20} from "../src/IERC20.sol";
import {RunFiles} from "./lib/RunFiles.sol";
import {Env} from "./lib/Env.sol";

/// @notice Full dress rehearsal against a fork of the target chain, with no transactions sent.
///
///   forge script script/Simulate.s.sol --sig "run(string)" runs/<name> --rpc-url $RPC_URL
///
/// The script impersonates the reserve to grant the approval (if it has not been granted yet),
/// impersonates the executor to send every batch, and then checks that every recipient in a sample
/// of batches received exactly the published amount and that the reserve paid exactly the total.
/// If AIRDROP_ADDRESS is not set yet, a contract is deployed inside the simulation.
contract Simulate is Script {
    function run(string memory runDir) external {
        require(!vm.isContext(VmSafe.ForgeContext.ScriptBroadcast), "Simulate must not be broadcast");
        Env.requireChain();
        Env.Roles memory r = Env.roles();
        RunFiles.Manifest memory m = RunFiles.readManifest(runDir);
        require(RunFiles.distributionHash(runDir) == m.listHash, "distribution.csv does not match manifest");

        CommittedBatchAirdrop airdrop;
        address existing = Env.airdrop();
        if (existing == address(0)) {
            airdrop = new CommittedBatchAirdrop(
                IERC20(r.token),
                r.reserve,
                r.admin,
                r.executor,
                m.root,
                m.batchCount,
                m.totalAmount,
                m.listHash
            );
            console.log("simulated deployment at", address(airdrop));
        } else {
            airdrop = CommittedBatchAirdrop(existing);
            require(airdrop.root() == m.root, "AIRDROP_ADDRESS root does not match this run");
        }

        IERC20 token = airdrop.token();
        address reserve = airdrop.reserve();
        uint256 reserveBefore = token.balanceOf(reserve);
        require(reserveBefore >= m.totalAmount, "reserve balance is below the total to distribute");

        if (token.allowance(reserve, address(airdrop)) < m.totalAmount) {
            vm.prank(reserve);
            token.approve(address(airdrop), m.totalAmount);
            console.log("simulated approval by reserve for", m.totalAmount);
        }

        // Remember pre-balances for the sampled batches so the check below is exact even if a
        // recipient already held tokens.
        uint256[] memory sample = _sampleBatches(m.batchCount);
        RunFiles.Batch[] memory sampled = new RunFiles.Batch[](sample.length);
        uint256[][] memory before = new uint256[][](sample.length);
        for (uint256 s = 0; s < sample.length; s++) {
            sampled[s] = RunFiles.readBatch(runDir, sample[s]);
            before[s] = new uint256[](sampled[s].recipients.length);
            for (uint256 j = 0; j < before[s].length; j++) {
                before[s][j] = token.balanceOf(sampled[s].recipients[j]);
            }
        }

        uint256 executed;
        vm.startPrank(airdrop.executor());
        for (uint256 i = 0; i < m.batchCount; i++) {
            if (airdrop.isExecuted(i)) continue;
            RunFiles.Batch memory b = RunFiles.readBatch(runDir, i);
            airdrop.execute(i, b.recipients, b.amounts, b.proof);
            executed++;
        }
        vm.stopPrank();

        require(airdrop.executedBatches() == m.batchCount, "not every batch executed");
        require(airdrop.distributedAmount() == m.totalAmount, "distributed amount differs from total");
        require(airdrop.remainingAmount() == 0, "remaining amount is not zero");
        require(token.allowance(reserve, address(airdrop)) == 0, "allowance not fully consumed");

        for (uint256 s = 0; s < sample.length; s++) {
            RunFiles.Batch memory b = sampled[s];
            for (uint256 j = 0; j < b.recipients.length; j++) {
                uint256 got = token.balanceOf(b.recipients[j]) - before[s][j];
                require(got == b.amounts[j], "a sampled recipient did not receive the published amount");
            }
        }

        console.log("batches executed in this simulation", executed);
        console.log("reserve paid", reserveBefore - token.balanceOf(reserve));
        console.log("total per manifest", m.totalAmount);
        console.log("sampled batches checked recipient by recipient", sample.length);
        console.log("SIMULATION OK");
    }

    /// @dev First, middle and last batch (deduplicated for tiny runs).
    function _sampleBatches(uint256 count) private pure returns (uint256[] memory out) {
        uint256[3] memory picks = [uint256(0), count / 2, count - 1];
        uint256 n;
        uint256[] memory tmp = new uint256[](3);
        for (uint256 i = 0; i < 3; i++) {
            bool dup;
            for (uint256 j = 0; j < n; j++) {
                if (tmp[j] == picks[i]) dup = true;
            }
            if (!dup) tmp[n++] = picks[i];
        }
        out = new uint256[](n);
        for (uint256 i = 0; i < n; i++) {
            out[i] = tmp[i];
        }
    }
}
