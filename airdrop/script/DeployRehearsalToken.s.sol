// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Script, console} from "forge-std/Script.sol";
import {RehearsalToken} from "../src/testnet/RehearsalToken.sol";
import {Env} from "./lib/Env.sol";

/// @notice Testnet only. Deploys a stand-in token whose whole supply goes to `holder` (use the
///         address you will put in RESERVE_ADDRESS for the rehearsal).
///
///   forge script script/DeployRehearsalToken.s.sol --sig "run(address,uint256)" <holder> <supply> \
///       --rpc-url $RPC_URL --broadcast
contract DeployRehearsalToken is Script {
    function run(address holder, uint256 supply) external returns (RehearsalToken token) {
        require(block.chainid != 1, "never deploy the rehearsal token on Ethereum mainnet");
        Env.requireChain();
        vm.startBroadcast();
        token = new RehearsalToken(holder, supply);
        vm.stopBroadcast();
        console.log("rehearsal token", address(token));
        console.log("holder         ", holder);
        console.log("supply         ", supply);
        console.log("Next: set TOKEN_ADDRESS in .env to this address.");
    }
}
