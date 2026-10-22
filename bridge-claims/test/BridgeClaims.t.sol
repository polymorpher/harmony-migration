// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Test} from "forge-std/Test.sol";
import {BridgeClaims} from "../src/BridgeClaims.sol";
import {ClaimTree} from "./lib/ClaimTree.sol";
import {
    MockERC20,
    NoReturnToken,
    FalseToken,
    FeeToken,
    MockWETH,
    RejectNative,
    ReturnBomb,
    Reenterer,
    IClaims
} from "./mocks/Mocks.sol";

contract BridgeClaimsTest is Test {
    struct Row {
        address account;
        address token;
        uint256 amount;
    }

    address internal constant NATIVE = address(0);

    BridgeClaims internal claims;
    MockERC20 internal std;
    NoReturnToken internal noReturn;
    FeeToken internal fee;
    FalseToken internal falseToken;
    MockWETH internal weth;
    RejectNative internal rejector;
    ReturnBomb internal bomb;
    Reenterer internal reenterer;

    address internal funder = makeAddr("funder");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");
    address internal relayer = makeAddr("relayer");

    Row[] internal rows;
    bytes32[] internal leaves;
    bytes32[][] internal tree;
    bytes32 internal root;

    // Row indexes, fixed by the order rows are added in setUp.
    uint256 internal constant ALICE_STD = 0;
    uint256 internal constant ALICE_NATIVE = 1;
    uint256 internal constant BOB_NORETURN = 2;
    uint256 internal constant BOB_FEE = 3;
    uint256 internal constant CAROL_STD = 4;
    uint256 internal constant REJECTOR_NATIVE = 5;
    uint256 internal constant BOMB_NATIVE = 6;
    uint256 internal constant REENTER_NATIVE = 7;
    uint256 internal constant FALSE_ROW = 8;
    uint256 internal constant NO_CODE_ROW = 9;
    uint256 internal constant ALICE_WETH = 10;

    address internal constant NO_CODE_TOKEN = address(0xC0DE);

    function setUp() public {
        std = new MockERC20();
        noReturn = new NoReturnToken();
        fee = new FeeToken();
        falseToken = new FalseToken();
        weth = new MockWETH();
        rejector = new RejectNative();
        bomb = new ReturnBomb();
        reenterer = new Reenterer();

        _add(alice, address(std), 100e18);
        _add(alice, NATIVE, 1.5 ether);
        _add(bob, address(noReturn), 250e6);
        _add(bob, address(fee), 1_000e18);
        _add(carol, address(std), 7e18);
        _add(address(rejector), NATIVE, 0.25 ether);
        _add(address(bomb), NATIVE, 0.125 ether);
        _add(address(reenterer), NATIVE, 0.5 ether);
        _add(alice, address(falseToken), 1e18);
        _add(alice, NO_CODE_TOKEN, 1e18);
        _add(alice, address(weth), 2 ether);
        _buildTree();

        claims = new BridgeClaims(funder, address(weth), block.chainid, root, rows.length, keccak256("list"));
        _fundAndApprove();
        reenterer.arm(IClaims(address(claims)), REENTER_NATIVE, NATIVE, 0.5 ether, _proof(REENTER_NATIVE));
    }

    // ------------------------------------------------------------------ helpers

    function _add(address account, address token, uint256 amount) internal {
        rows.push(Row(account, token, amount));
    }

    function _buildTree() internal {
        delete leaves;
        for (uint256 i = 0; i < rows.length; i++) {
            leaves.push(ClaimTree.leaf(i, rows[i].account, rows[i].token, rows[i].amount));
        }
        bytes32[][] memory lv = ClaimTree.levels(leaves);
        delete tree;
        for (uint256 d = 0; d < lv.length; d++) {
            tree.push(lv[d]);
        }
        root = lv[lv.length - 1][0];
    }

    function _proof(uint256 index) internal view returns (bytes32[] memory) {
        bytes32[][] memory lv = new bytes32[][](tree.length);
        for (uint256 d = 0; d < tree.length; d++) {
            lv[d] = tree[d];
        }
        return ClaimTree.proof(lv, index);
    }

    function _total(address token) internal view returns (uint256 sum) {
        for (uint256 i = 0; i < rows.length; i++) {
            if (rows[i].token == token) sum += rows[i].amount;
        }
    }

    /// @dev What the funder's Safe batch does: wrap the native total, approve every asset's total.
    function _fundAndApprove() internal {
        std.mint(funder, 1_000e18);
        noReturn.mint(funder, 1_000e6);
        fee.mint(funder, 10_000e18);
        falseToken.mint(funder, 10e18);
        uint256 wrappedTotal = _total(NATIVE) + _total(address(weth));
        vm.deal(funder, wrappedTotal);
        vm.startPrank(funder);
        weth.deposit{value: wrappedTotal}();
        std.approve(address(claims), _total(address(std)));
        noReturn.approve(address(claims), _total(address(noReturn)));
        fee.approve(address(claims), _total(address(fee)));
        falseToken.approve(address(claims), _total(address(falseToken)));
        weth.approve(address(claims), wrappedTotal);
        vm.stopPrank();
    }

    function _claim(uint256 index) internal {
        Row memory r = rows[index];
        claims.claim(index, r.account, r.token, r.amount, _proof(index));
    }

    function _claimData(uint256 index) internal view returns (BridgeClaims.Claim memory) {
        Row memory r = rows[index];
        return BridgeClaims.Claim(index, r.account, r.token, r.amount, _proof(index));
    }

    // ------------------------------------------------------------------ deployment

    function test_ConstructorStoresEverything() public view {
        assertEq(claims.funder(), funder);
        assertEq(claims.wrappedNative(), address(weth));
        assertEq(claims.chainId(), block.chainid);
        assertEq(claims.root(), root);
        assertEq(claims.claimCount(), rows.length);
        assertEq(claims.listHash(), keccak256("list"));
        assertEq(claims.NATIVE(), address(0));
    }

    function test_ConstructorRejectsBadArguments() public {
        vm.expectRevert(BridgeClaims.ZeroAddress.selector);
        new BridgeClaims(address(0), address(weth), block.chainid, root, 1, bytes32(0));
        vm.expectRevert(BridgeClaims.ZeroValue.selector);
        new BridgeClaims(funder, address(weth), block.chainid, bytes32(0), 1, bytes32(0));
        vm.expectRevert(BridgeClaims.ZeroValue.selector);
        new BridgeClaims(funder, address(weth), block.chainid, root, 0, bytes32(0));
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.WrongChain.selector, 1, block.chainid));
        new BridgeClaims(funder, address(weth), 1, root, 1, bytes32(0));
    }

    // ------------------------------------------------------------------ ERC-20 rows

    function test_ClaimPaysFromFunder() public {
        uint256 before = std.balanceOf(funder);
        vm.expectEmit(address(claims));
        emit BridgeClaims.Claimed(ALICE_STD, alice, address(std), 100e18);
        _claim(ALICE_STD);
        assertEq(std.balanceOf(alice), 100e18);
        assertEq(std.balanceOf(funder), before - 100e18);
        assertEq(std.balanceOf(address(claims)), 0);
        assertEq(std.allowance(funder, address(claims)), 7e18);
        assertTrue(claims.isClaimed(ALICE_STD));
        assertFalse(claims.isClaimed(CAROL_STD));
    }

    function test_AnyoneCanClaimButAccountIsPaid() public {
        vm.prank(relayer);
        _claim(CAROL_STD);
        assertEq(std.balanceOf(carol), 7e18);
        assertEq(std.balanceOf(relayer), 0);
    }

    function test_RowPaysOnce() public {
        _claim(ALICE_STD);
        bytes32[] memory p = _proof(ALICE_STD);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.AlreadyClaimed.selector, ALICE_STD));
        claims.claim(ALICE_STD, alice, address(std), 100e18, p);
    }

    function test_AlteredRowsAreRejected() public {
        bytes32[] memory p = _proof(ALICE_STD);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.InvalidProof.selector, ALICE_STD));
        claims.claim(ALICE_STD, alice, address(std), 100e18 + 1, p);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.InvalidProof.selector, ALICE_STD));
        claims.claim(ALICE_STD, bob, address(std), 100e18, p);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.InvalidProof.selector, ALICE_STD));
        claims.claim(ALICE_STD, alice, address(fee), 100e18, p);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.InvalidProof.selector, CAROL_STD));
        claims.claim(CAROL_STD, alice, address(std), 100e18, p);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.IndexOutOfRange.selector, rows.length, rows.length));
        claims.claim(rows.length, alice, address(std), 100e18, p);
        assertFalse(claims.isClaimed(ALICE_STD));
    }

    function test_VerifyMatchesClaimability() public view {
        for (uint256 i = 0; i < rows.length; i++) {
            Row memory r = rows[i];
            assertTrue(claims.verify(i, r.account, r.token, r.amount, _proof(i)));
            assertFalse(claims.verify(i, r.account, r.token, r.amount + 1, _proof(i)));
            assertEq(claims.leaf(i, r.account, r.token, r.amount), leaves[i]);
        }
        assertFalse(claims.verify(rows.length, alice, address(std), 1, new bytes32[](0)));
    }

    function test_TokenWithoutReturnValue() public {
        _claim(BOB_NORETURN);
        assertEq(noReturn.balanceOf(bob), 250e6);
    }

    function test_FalseReturnReverts() public {
        bytes32[] memory p = _proof(FALSE_ROW);
        vm.expectRevert(
            abi.encodeWithSelector(BridgeClaims.TransferFailed.selector, address(falseToken), alice, 1e18)
        );
        claims.claim(FALSE_ROW, alice, address(falseToken), 1e18, p);
        assertFalse(claims.isClaimed(FALSE_ROW));
    }

    function test_TokenWithoutCodeReverts() public {
        bytes32[] memory p = _proof(NO_CODE_ROW);
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.TransferFailed.selector, NO_CODE_TOKEN, alice, 1e18));
        claims.claim(NO_CODE_ROW, alice, NO_CODE_TOKEN, 1e18, p);
    }

    function test_FeeTokenPaysAmountLessFee() public {
        _claim(BOB_FEE);
        assertEq(fee.balanceOf(bob), 990e18);
        assertEq(fee.allowance(funder, address(claims)), 0);
    }

    function test_RevokedApprovalStopsClaimsUntilRestored() public {
        vm.prank(funder);
        std.approve(address(claims), 0);
        assertEq(claims.available(address(std)), 0);
        bytes32[] memory p = _proof(ALICE_STD);
        vm.expectRevert();
        claims.claim(ALICE_STD, alice, address(std), 100e18, p);
        assertFalse(claims.isClaimed(ALICE_STD));

        vm.prank(funder);
        std.approve(address(claims), 107e18);
        _claim(ALICE_STD);
        assertEq(std.balanceOf(alice), 100e18);
    }

    function test_AvailableIsMinOfApprovalAndBalance() public {
        assertEq(claims.available(address(std)), 107e18);
        vm.prank(funder);
        assertTrue(std.transfer(bob, 1_000e18 - 50e18));
        assertEq(claims.available(address(std)), 50e18);
        assertEq(claims.available(NATIVE), _total(NATIVE) + _total(address(weth)));
    }

    // ------------------------------------------------------------------ native rows

    function test_NativeRowPaysCoin() public {
        uint256 wrappedBefore = weth.balanceOf(funder);
        _claim(ALICE_NATIVE);
        assertEq(alice.balance, 1.5 ether);
        assertEq(weth.balanceOf(funder), wrappedBefore - 1.5 ether);
        assertEq(address(claims).balance, 0);
        assertEq(weth.balanceOf(address(claims)), 0);
    }

    function test_NativeRowFallsBackToWrapped() public {
        vm.expectEmit(address(claims));
        emit BridgeClaims.PaidWrapped(REJECTOR_NATIVE, address(rejector), 0.25 ether);
        _claim(REJECTOR_NATIVE);
        assertEq(address(rejector).balance, 0);
        assertEq(weth.balanceOf(address(rejector)), 0.25 ether);
        assertEq(address(claims).balance, 0);
        assertEq(weth.balanceOf(address(claims)), 0);
    }

    function test_NativeRowIgnoresReturnPayload() public {
        _claim(BOMB_NATIVE);
        assertEq(address(bomb).balance, 0.125 ether);
    }

    function test_ReentryCannotPayTwice() public {
        _claim(REENTER_NATIVE);
        assertTrue(reenterer.reclaimReverted());
        assertEq(address(reenterer).balance, 0.5 ether);
        assertEq(weth.balanceOf(address(reenterer)), 0);
    }

    function test_WrappedTokenRowIsPlainErc20() public {
        _claim(ALICE_WETH);
        assertEq(weth.balanceOf(alice), 2 ether);
        assertEq(alice.balance, 0);
    }

    function test_NativeRowWithoutWrappedCoinReverts() public {
        BridgeClaims noNative = new BridgeClaims(funder, address(0), block.chainid, root, rows.length, bytes32(0));
        bytes32[] memory p = _proof(ALICE_NATIVE);
        vm.expectRevert(BridgeClaims.NativeNotSupported.selector);
        noNative.claim(ALICE_NATIVE, alice, NATIVE, 1.5 ether, p);
        assertEq(noNative.available(NATIVE), 0);
    }

    function test_RejectsCoinFromAnyoneButWrapped() public {
        vm.deal(alice, 1 ether);
        vm.prank(alice);
        (bool ok,) = address(claims).call{value: 1 ether}("");
        assertFalse(ok);
    }

    // ------------------------------------------------------------------ batches

    function test_ClaimManyPaysAllAssetsOfAPayee() public {
        BridgeClaims.Claim[] memory batch = new BridgeClaims.Claim[](3);
        batch[0] = _claimData(ALICE_STD);
        batch[1] = _claimData(ALICE_NATIVE);
        batch[2] = _claimData(ALICE_WETH);
        vm.prank(relayer);
        uint256 paid = claims.claimMany(batch);
        assertEq(paid, 3);
        assertEq(std.balanceOf(alice), 100e18);
        assertEq(alice.balance, 1.5 ether);
        assertEq(weth.balanceOf(alice), 2 ether);
    }

    function test_ClaimManySkipsPaidRows() public {
        _claim(ALICE_STD);
        BridgeClaims.Claim[] memory batch = new BridgeClaims.Claim[](2);
        batch[0] = _claimData(ALICE_STD);
        batch[1] = _claimData(CAROL_STD);
        uint256 paid = claims.claimMany(batch);
        assertEq(paid, 1);
        assertEq(std.balanceOf(alice), 100e18);
        assertEq(std.balanceOf(carol), 7e18);
    }

    function test_ClaimManyRevertsOnABadRow() public {
        BridgeClaims.Claim[] memory batch = new BridgeClaims.Claim[](2);
        batch[0] = _claimData(ALICE_STD);
        batch[1] = _claimData(CAROL_STD);
        batch[1].amount += 1;
        vm.expectRevert(abi.encodeWithSelector(BridgeClaims.InvalidProof.selector, CAROL_STD));
        claims.claimMany(batch);
        assertFalse(claims.isClaimed(ALICE_STD));
    }

    // ------------------------------------------------------------------ sweep

    function test_SweepReturnsMistakesToFunder() public {
        std.mint(address(claims), 5e18);
        vm.prank(relayer);
        claims.sweep(address(std));
        assertEq(std.balanceOf(address(claims)), 0);
        assertEq(std.balanceOf(funder), 1_000e18 + 5e18);

        vm.deal(address(claims), 3 ether);
        uint256 before = funder.balance;
        claims.sweep(NATIVE);
        assertEq(address(claims).balance, 0);
        assertEq(funder.balance, before + 3 ether);
    }

    // ------------------------------------------------------------------ whole list

    function test_EveryPayableRowExactlyOnceExhaustsApprovals() public {
        for (uint256 i = 0; i < rows.length; i++) {
            if (i == FALSE_ROW || i == NO_CODE_ROW) continue;
            _claim(i);
        }
        assertEq(std.allowance(funder, address(claims)), 0);
        assertEq(noReturn.allowance(funder, address(claims)), 0);
        assertEq(fee.allowance(funder, address(claims)), 0);
        assertEq(weth.allowance(funder, address(claims)), 0);
        assertEq(address(claims).balance, 0);
        assertEq(weth.balanceOf(address(claims)), 0);
    }

    function testFuzz_RandomListClaimsOnceInAnyOrder(uint256 seed, uint8 sizeSeed) public {
        uint256 n = 1 + (uint256(sizeSeed) % 70);
        delete rows;
        for (uint256 i = 0; i < n; i++) {
            address account = address(uint160(uint256(keccak256(abi.encode("account", seed, i % 23)))));
            uint256 amount = 1 + (uint256(keccak256(abi.encode("amount", seed, i))) % 1e24);
            rows.push(Row(account, address(std), amount));
        }
        _buildTree();
        BridgeClaims list = new BridgeClaims(funder, address(weth), block.chainid, root, n, bytes32(0));
        uint256 total = _total(address(std));
        std.mint(funder, total);
        vm.prank(funder);
        std.approve(address(list), total);

        for (uint256 k = 0; k < n; k++) {
            uint256 i = (k * 7 + seed % n) % n;
            if (list.isClaimed(i)) {
                i = 0;
                while (list.isClaimed(i)) i++;
            }
            Row memory r = rows[i];
            list.claim(i, r.account, r.token, r.amount, _proof(i));
            vm.expectRevert(abi.encodeWithSelector(BridgeClaims.AlreadyClaimed.selector, i));
            list.claim(i, r.account, r.token, r.amount, _proof(i));
        }
        assertEq(std.allowance(funder, address(list)), 0);
    }
}
