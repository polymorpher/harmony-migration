// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Test} from "forge-std/Test.sol";
import {BridgeClaims} from "../src/BridgeClaims.sol";
import {MockERC20, MockWETH} from "./mocks/Mocks.sol";

/// @notice The Python tree (`toolkit/scripts/bridge/claim_tree.py`) and the contract agree: every row
///         of a list built in Python verifies and pays, and the CREATE2 address computed in Python is
///         the one the bytecode produces. Regenerate the fixture with `test/fixtures/build_fixture.py`.
contract TreeFixtureTest is Test {
    string internal json;

    function setUp() public {
        json = vm.readFile("test/fixtures/claim-tree.json");
    }

    function _deploy() internal returns (BridgeClaims claims) {
        vm.chainId(vm.parseJsonUint(json, ".chain_id"));
        claims = new BridgeClaims(
            vm.parseJsonAddress(json, ".funder"),
            vm.parseJsonAddress(json, ".wrapped_native"),
            vm.parseJsonUint(json, ".chain_id"),
            vm.parseJsonBytes32(json, ".root"),
            vm.parseJsonUint(json, ".claim_count"),
            vm.parseJsonBytes32(json, ".list_hash")
        );
    }

    function test_PythonLeavesAndProofsVerify() public {
        BridgeClaims claims = _deploy();
        address[] memory accounts = vm.parseJsonAddressArray(json, ".accounts");
        address[] memory tokens = vm.parseJsonAddressArray(json, ".row_tokens");
        uint256[] memory amounts = vm.parseJsonUintArray(json, ".amounts");
        bytes32[] memory leaves = vm.parseJsonBytes32Array(json, ".leaves");
        for (uint256 i = 0; i < accounts.length; i++) {
            bytes32[] memory proof = vm.parseJsonBytes32Array(json, string.concat(".proofs[", vm.toString(i), "]"));
            assertEq(claims.leaf(i, accounts[i], tokens[i], amounts[i]), leaves[i]);
            assertTrue(claims.verify(i, accounts[i], tokens[i], amounts[i], proof));
        }
    }

    function test_EveryPythonRowPays() public {
        BridgeClaims claims = _deploy();
        address funder = claims.funder();
        address wrapped = claims.wrappedNative();
        vm.etch(wrapped, address(new MockWETH()).code);
        address[] memory erc20s = vm.parseJsonAddressArray(json, ".tokens");
        for (uint256 t = 0; t < erc20s.length; t++) {
            vm.etch(erc20s[t], address(new MockERC20()).code);
        }

        address[] memory accounts = vm.parseJsonAddressArray(json, ".accounts");
        address[] memory tokens = vm.parseJsonAddressArray(json, ".row_tokens");
        uint256[] memory amounts = vm.parseJsonUintArray(json, ".amounts");
        uint256 nativeTotal;
        for (uint256 i = 0; i < accounts.length; i++) {
            if (tokens[i] == address(0)) {
                nativeTotal += amounts[i];
            } else {
                MockERC20(tokens[i]).mint(funder, amounts[i]);
            }
        }
        vm.deal(funder, nativeTotal);
        vm.startPrank(funder);
        MockWETH(payable(wrapped)).deposit{value: nativeTotal}();
        MockWETH(payable(wrapped)).approve(address(claims), nativeTotal);
        for (uint256 t = 0; t < erc20s.length; t++) {
            MockERC20(erc20s[t]).approve(address(claims), type(uint256).max);
        }
        vm.stopPrank();

        for (uint256 i = 0; i < accounts.length; i++) {
            bytes32[] memory proof = vm.parseJsonBytes32Array(json, string.concat(".proofs[", vm.toString(i), "]"));
            claims.claim(i, accounts[i], tokens[i], amounts[i], proof);
            assertTrue(claims.isClaimed(i));
        }
        assertEq(MockWETH(payable(wrapped)).allowance(funder, address(claims)), 0);
        assertEq(address(claims).balance, 0);
    }

    function test_PythonCreate2AddressMatchesBytecode() public view {
        bytes memory initCode = abi.encodePacked(
            type(BridgeClaims).creationCode,
            abi.encode(
                vm.parseJsonAddress(json, ".funder"),
                vm.parseJsonAddress(json, ".wrapped_native"),
                vm.parseJsonUint(json, ".chain_id"),
                vm.parseJsonBytes32(json, ".root"),
                vm.parseJsonUint(json, ".claim_count"),
                vm.parseJsonBytes32(json, ".list_hash")
            )
        );
        assertEq(keccak256(initCode), vm.parseJsonBytes32(json, ".init_code_keccak256"), "rebuild the fixture");
        assertEq(
            vm.computeCreate2Address(
                vm.parseJsonBytes32(json, ".salt"), keccak256(initCode), vm.parseJsonAddress(json, ".create2_deployer")
            ),
            vm.parseJsonAddress(json, ".address")
        );
    }
}
