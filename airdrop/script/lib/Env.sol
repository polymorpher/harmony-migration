// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Vm} from "forge-std/Vm.sol";

/// @notice Reads the role addresses from `.env` and refuses to run against the wrong chain.
library Env {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    struct Roles {
        address token;
        address reserve;
        address admin;
        address executor;
    }

    function roles() internal view returns (Roles memory r) {
        r.token = vm.envAddress("TOKEN_ADDRESS");
        r.reserve = vm.envAddress("RESERVE_ADDRESS");
        r.admin = vm.envAddress("ADMIN_ADDRESS");
        r.executor = vm.envAddress("EXECUTOR_ADDRESS");
    }

    /// @notice AIRDROP_ADDRESS, or address(0) when it is not set yet.
    function airdrop() internal view returns (address) {
        return vm.envOr("AIRDROP_ADDRESS", address(0));
    }

    /// @notice Aborts if CHAIN_ID is set in .env and the RPC reports a different chain.
    function requireChain() internal view {
        uint256 want = vm.envOr("CHAIN_ID", uint256(0));
        require(want == 0 || block.chainid == want, "RPC chain id does not match CHAIN_ID in .env");
    }
}
