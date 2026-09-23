// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Vm} from "forge-std/Vm.sol";

/// @notice Reads the files that `tools/build.py` writes into a run directory:
///         <run>/manifest.json, <run>/distribution.csv and <run>/batches/batch-NNNNN.json.
library RunFiles {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    struct Manifest {
        bytes32 root;
        uint256 batchCount;
        uint256 batchSize;
        uint256 recipientCount;
        uint256 totalAmount;
        bytes32 listHash;
    }

    struct Batch {
        uint256 index;
        bytes32 leaf;
        address[] recipients;
        uint256[] amounts;
        bytes32[] proof;
    }

    function manifestPath(string memory runDir) internal pure returns (string memory) {
        return string.concat(runDir, "/manifest.json");
    }

    function distributionPath(string memory runDir) internal pure returns (string memory) {
        return string.concat(runDir, "/distribution.csv");
    }

    function batchPath(string memory runDir, uint256 index) internal pure returns (string memory) {
        return string.concat(runDir, "/batches/batch-", _pad5(index), ".json");
    }

    function deploymentPath(string memory runDir, uint256 chainId) internal pure returns (string memory) {
        return string.concat(runDir, "/deployment-", vm.toString(chainId), ".json");
    }

    function readManifest(string memory runDir) internal view returns (Manifest memory m) {
        string memory json = vm.readFile(manifestPath(runDir));
        m.root = vm.parseJsonBytes32(json, ".root");
        m.batchCount = vm.parseJsonUint(json, ".batch_count");
        m.batchSize = vm.parseJsonUint(json, ".batch_size");
        m.recipientCount = vm.parseJsonUint(json, ".recipient_count");
        m.totalAmount = vm.parseJsonUint(json, ".total_amount");
        m.listHash = vm.parseJsonBytes32(json, ".list_sha256");
    }

    function readBatch(string memory runDir, uint256 index) internal view returns (Batch memory b) {
        string memory json = vm.readFile(batchPath(runDir, index));
        b.index = vm.parseJsonUint(json, ".batch_index");
        require(b.index == index, "batch file index mismatch");
        b.leaf = vm.parseJsonBytes32(json, ".leaf");
        b.recipients = vm.parseJsonAddressArray(json, ".recipients");
        b.amounts = vm.parseJsonUintArray(json, ".amounts");
        b.proof = vm.parseJsonBytes32Array(json, ".proof");
        require(b.recipients.length == b.amounts.length, "batch file length mismatch");
    }

    /// @notice SHA-256 of the distribution file, as recorded in the manifest and the contract.
    function distributionHash(string memory runDir) internal view returns (bytes32) {
        return sha256(vm.readFileBinary(distributionPath(runDir)));
    }

    function _pad5(uint256 n) private pure returns (string memory) {
        bytes memory s = bytes(vm.toString(n));
        require(s.length <= 5, "batch index too large");
        bytes memory out = new bytes(5);
        for (uint256 i = 0; i < 5; i++) {
            out[i] = "0";
        }
        for (uint256 i = 0; i < s.length; i++) {
            out[5 - s.length + i] = s[i];
        }
        return string(out);
    }
}
