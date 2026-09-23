// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {Test} from "forge-std/Test.sol";
import {CommittedBatchAirdrop} from "../src/CommittedBatchAirdrop.sol";
import {IERC20} from "../src/IERC20.sol";
import {RehearsalToken} from "../src/testnet/RehearsalToken.sol";
import {BatchTree} from "../script/lib/BatchTree.sol";

contract CommittedBatchAirdropTest is Test {
    RehearsalToken internal token;
    CommittedBatchAirdrop internal airdrop;

    address internal reserve = makeAddr("reserve");
    address internal admin = makeAddr("admin");
    address internal executor = makeAddr("executor");
    address internal stranger = makeAddr("stranger");

    uint256 internal constant SUPPLY = 1_000_000_000 ether;

    // A small distribution built in setUp: N recipients, K per batch.
    uint256 internal constant N = 1_000;
    uint256 internal constant K = 100;
    address[][] internal recipients;
    uint256[][] internal amounts;
    bytes32[][] internal proofs;
    bytes32[] internal leaves;
    bytes32 internal root;
    uint256 internal total;

    function setUp() public {
        token = new RehearsalToken(reserve, SUPPLY);
        _buildDistribution(N, K, 1);
        airdrop = new CommittedBatchAirdrop(
            IERC20(address(token)), reserve, admin, executor, root, leaves.length, total, keccak256("list")
        );
        vm.prank(reserve);
        token.approve(address(airdrop), total);
    }

    // ------------------------------------------------------------------ helpers

    function _buildDistribution(uint256 n, uint256 k, uint256 salt) internal {
        delete recipients;
        delete amounts;
        delete proofs;
        delete leaves;
        total = 0;
        uint256 batches = (n + k - 1) / k;
        for (uint256 b = 0; b < batches; b++) {
            uint256 len = b + 1 == batches ? n - b * k : k;
            address[] memory r = new address[](len);
            uint256[] memory a = new uint256[](len);
            for (uint256 i = 0; i < len; i++) {
                uint256 idx = b * k + i;
                r[i] = address(uint160(uint256(keccak256(abi.encode("recipient", salt, idx)))));
                a[i] = (1 + (uint256(keccak256(abi.encode("amount", salt, idx))) % 100_000)) * 1e18;
                total += a[i];
            }
            recipients.push(r);
            amounts.push(a);
            leaves.push(BatchTree.leaf(b, r, a));
        }
        bytes32[][] memory lv = BatchTree.levels(leaves);
        root = lv[lv.length - 1][0];
        for (uint256 b = 0; b < batches; b++) {
            proofs.push(BatchTree.proof(lv, b));
        }
    }

    function _execute(uint256 b) internal {
        vm.prank(executor);
        airdrop.execute(b, recipients[b], amounts[b], proofs[b]);
    }

    function _sum(uint256[] memory a) internal pure returns (uint256 s) {
        for (uint256 i = 0; i < a.length; i++) {
            s += a[i];
        }
    }

    // ------------------------------------------------------------------ happy path

    function test_ExecutesEveryBatchExactly() public {
        uint256 reserveBefore = token.balanceOf(reserve);
        for (uint256 b = 0; b < leaves.length; b++) {
            vm.expectEmit(true, true, false, true, address(airdrop));
            emit CommittedBatchAirdrop.BatchExecuted(b, leaves[b], recipients[b].length, _sum(amounts[b]));
            _execute(b);
            assertTrue(airdrop.isExecuted(b));
        }
        assertEq(airdrop.executedBatches(), leaves.length);
        assertEq(airdrop.distributedAmount(), total);
        assertEq(airdrop.remainingAmount(), 0);
        assertEq(token.allowance(reserve, address(airdrop)), 0);
        assertEq(reserveBefore - token.balanceOf(reserve), total);
        for (uint256 b = 0; b < leaves.length; b++) {
            for (uint256 i = 0; i < recipients[b].length; i++) {
                assertEq(token.balanceOf(recipients[b][i]), amounts[b][i]);
            }
        }
        assertEq(token.balanceOf(address(airdrop)), 0);
    }

    function test_BatchesCanRunInAnyOrderAndResume() public {
        _execute(7);
        _execute(2);
        _execute(9);
        assertEq(airdrop.executedBatches(), 3);
        assertFalse(airdrop.isExecuted(0));
        assertTrue(airdrop.isExecuted(7) && airdrop.isExecuted(2) && airdrop.isExecuted(9));
        for (uint256 b = 0; b < leaves.length; b++) {
            if (!airdrop.isExecuted(b)) _execute(b);
        }
        assertEq(airdrop.distributedAmount(), total);
    }

    function test_VerifyBatchView() public view {
        assertTrue(airdrop.verifyBatch(3, recipients[3], amounts[3], proofs[3]));
        assertFalse(airdrop.verifyBatch(4, recipients[3], amounts[3], proofs[3]));
        assertFalse(airdrop.verifyBatch(3, recipients[3], amounts[3], proofs[4]));
        assertFalse(airdrop.verifyBatch(leaves.length, recipients[0], amounts[0], proofs[0]));
        uint256[] memory shortAmounts = new uint256[](1);
        assertFalse(airdrop.verifyBatch(3, recipients[3], shortAmounts, proofs[3]));
    }

    function test_ImmutableParametersAreExposed() public view {
        assertEq(address(airdrop.token()), address(token));
        assertEq(airdrop.reserve(), reserve);
        assertEq(airdrop.admin(), admin);
        assertEq(airdrop.executor(), executor);
        assertEq(airdrop.root(), root);
        assertEq(airdrop.batchCount(), leaves.length);
        assertEq(airdrop.totalAmount(), total);
        assertEq(airdrop.listHash(), keccak256("list"));
        assertEq(airdrop.executedBitmap(0), 0);
    }

    // ------------------------------------------------------------------ what the executor cannot do

    function test_RevertsReplay() public {
        _execute(1);
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.BatchAlreadyExecuted.selector, 1));
        airdrop.execute(1, recipients[1], amounts[1], proofs[1]);
    }

    function test_RevertsTamperedAmount() public {
        uint256[] memory a = amounts[0];
        a[5] += 1;
        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(
                CommittedBatchAirdrop.BatchNotInList.selector, 0, BatchTree.leaf(0, recipients[0], a)
            )
        );
        airdrop.execute(0, recipients[0], a, proofs[0]);
    }

    function test_RevertsTamperedRecipient() public {
        address[] memory r = recipients[0];
        r[0] = stranger;
        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(
                CommittedBatchAirdrop.BatchNotInList.selector, 0, BatchTree.leaf(0, r, amounts[0])
            )
        );
        airdrop.execute(0, r, amounts[0], proofs[0]);
    }

    function test_RevertsExtraRecipient() public {
        address[] memory r = new address[](recipients[0].length + 1);
        uint256[] memory a = new uint256[](amounts[0].length + 1);
        for (uint256 i = 0; i < recipients[0].length; i++) {
            r[i] = recipients[0][i];
            a[i] = amounts[0][i];
        }
        r[r.length - 1] = stranger;
        a[a.length - 1] = 1;
        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(CommittedBatchAirdrop.BatchNotInList.selector, 0, BatchTree.leaf(0, r, a))
        );
        airdrop.execute(0, r, a, proofs[0]);
    }

    function test_RevertsWrongIndexForValidBatch() public {
        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(
                CommittedBatchAirdrop.BatchNotInList.selector, 2, BatchTree.leaf(2, recipients[1], amounts[1])
            )
        );
        airdrop.execute(2, recipients[1], amounts[1], proofs[1]);
    }

    function test_RevertsIndexOutOfRange() public {
        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(
                CommittedBatchAirdrop.BatchIndexOutOfRange.selector, leaves.length, leaves.length
            )
        );
        airdrop.execute(leaves.length, recipients[0], amounts[0], proofs[0]);
    }

    function test_RevertsLengthMismatchAndEmpty() public {
        uint256[] memory a = new uint256[](recipients[0].length - 1);
        vm.prank(executor);
        vm.expectRevert(CommittedBatchAirdrop.LengthMismatch.selector);
        airdrop.execute(0, recipients[0], a, proofs[0]);

        address[] memory none = new address[](0);
        uint256[] memory noAmounts = new uint256[](0);
        vm.prank(executor);
        vm.expectRevert(CommittedBatchAirdrop.EmptyBatch.selector);
        airdrop.execute(0, none, noAmounts, proofs[0]);
    }

    function test_RevertsNotExecutor() public {
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.NotExecutor.selector, stranger));
        airdrop.execute(0, recipients[0], amounts[0], proofs[0]);
        vm.prank(admin);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.NotExecutor.selector, admin));
        airdrop.execute(0, recipients[0], amounts[0], proofs[0]);
    }

    function test_RevertsWhenPaused() public {
        vm.prank(admin);
        airdrop.setPaused(true);
        vm.prank(executor);
        vm.expectRevert(CommittedBatchAirdrop.IsPaused.selector);
        airdrop.execute(0, recipients[0], amounts[0], proofs[0]);
        vm.prank(admin);
        airdrop.setPaused(false);
        _execute(0);
        assertTrue(airdrop.isExecuted(0));
    }

    function test_RevertsWhenAllowanceRevoked() public {
        vm.prank(reserve);
        token.approve(address(airdrop), 0);
        vm.prank(executor);
        vm.expectRevert(); // token-level allowance error; the batch stays unexecuted
        airdrop.execute(0, recipients[0], amounts[0], proofs[0]);
        assertFalse(airdrop.isExecuted(0));
        assertEq(airdrop.executedBatches(), 0);
    }

    function testFuzz_TamperedAmountRejected(uint8 position, uint256 delta) public {
        uint256 i = position % amounts[0].length;
        delta = bound(delta, 1, type(uint128).max);
        uint256[] memory a = amounts[0];
        a[i] = a[i] + delta;
        vm.prank(executor);
        vm.expectRevert(
            abi.encodeWithSelector(
                CommittedBatchAirdrop.BatchNotInList.selector, 0, BatchTree.leaf(0, recipients[0], a)
            )
        );
        airdrop.execute(0, recipients[0], a, proofs[0]);
    }

    // ------------------------------------------------------------------ admin

    function test_AdminFunctionsAreGated() public {
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.NotAdmin.selector, stranger));
        airdrop.setPaused(true);
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.NotAdmin.selector, executor));
        airdrop.setExecutor(stranger);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.NotAdmin.selector, stranger));
        airdrop.rescue(IERC20(address(token)), stranger, 1);
    }

    function test_SetExecutor() public {
        vm.prank(admin);
        vm.expectEmit(true, true, false, false, address(airdrop));
        emit CommittedBatchAirdrop.ExecutorChanged(executor, stranger);
        airdrop.setExecutor(stranger);
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.NotExecutor.selector, executor));
        airdrop.execute(0, recipients[0], amounts[0], proofs[0]);
        vm.prank(stranger);
        airdrop.execute(0, recipients[0], amounts[0], proofs[0]);
        assertTrue(airdrop.isExecuted(0));

        vm.prank(admin);
        vm.expectRevert(CommittedBatchAirdrop.ZeroAddress.selector);
        airdrop.setExecutor(address(0));
    }

    function test_RescueReturnsStrayTokens() public {
        vm.prank(reserve);
        assertTrue(token.transfer(address(airdrop), 5 ether));
        vm.prank(admin);
        airdrop.rescue(IERC20(address(token)), reserve, 5 ether);
        assertEq(token.balanceOf(address(airdrop)), 0);
    }

    function test_ConstructorRejectsZeroValues() public {
        IERC20 t = IERC20(address(token));
        vm.expectRevert(CommittedBatchAirdrop.ZeroAddress.selector);
        new CommittedBatchAirdrop(IERC20(address(0)), reserve, admin, executor, root, 1, 1, 0);
        vm.expectRevert(CommittedBatchAirdrop.ZeroAddress.selector);
        new CommittedBatchAirdrop(t, address(0), admin, executor, root, 1, 1, 0);
        vm.expectRevert(CommittedBatchAirdrop.ZeroAddress.selector);
        new CommittedBatchAirdrop(t, reserve, address(0), executor, root, 1, 1, 0);
        vm.expectRevert(CommittedBatchAirdrop.ZeroAddress.selector);
        new CommittedBatchAirdrop(t, reserve, admin, address(0), root, 1, 1, 0);
        vm.expectRevert(CommittedBatchAirdrop.ZeroValue.selector);
        new CommittedBatchAirdrop(t, reserve, admin, executor, bytes32(0), 1, 1, 0);
        vm.expectRevert(CommittedBatchAirdrop.ZeroValue.selector);
        new CommittedBatchAirdrop(t, reserve, admin, executor, root, 0, 1, 0);
        vm.expectRevert(CommittedBatchAirdrop.ZeroValue.selector);
        new CommittedBatchAirdrop(t, reserve, admin, executor, root, 1, 0, 0);
    }

    // ------------------------------------------------------------------ bitmap across words

    function test_BitmapWorksBeyond256Batches() public {
        _buildDistribution(300, 1, 2); // 300 batches of one recipient
        CommittedBatchAirdrop big = new CommittedBatchAirdrop(
            IERC20(address(token)), reserve, admin, executor, root, leaves.length, total, keccak256("big")
        );
        vm.prank(reserve);
        token.approve(address(big), total);
        for (uint256 b = 250; b < 300; b++) {
            vm.prank(executor);
            big.execute(b, recipients[b], amounts[b], proofs[b]);
        }
        assertTrue(big.isExecuted(255) && big.isExecuted(256) && big.isExecuted(299));
        assertFalse(big.isExecuted(249));
        assertEq(big.executedBitmap(0) >> 250, (1 << 6) - 1);
        assertEq(big.executedBitmap(1), (1 << 44) - 1);
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(CommittedBatchAirdrop.BatchAlreadyExecuted.selector, 256));
        big.execute(256, recipients[256], amounts[256], proofs[256]);
    }

    function test_SingleBatchHasEmptyProof() public {
        _buildDistribution(5, 10, 3);
        assertEq(leaves.length, 1);
        assertEq(proofs[0].length, 0);
        assertEq(root, leaves[0]);
        CommittedBatchAirdrop one = new CommittedBatchAirdrop(
            IERC20(address(token)), reserve, admin, executor, root, 1, total, keccak256("one")
        );
        vm.prank(reserve);
        token.approve(address(one), total);
        vm.prank(executor);
        one.execute(0, recipients[0], amounts[0], proofs[0]);
        assertEq(one.distributedAmount(), total);
    }

    // ------------------------------------------------------------------ cross-implementation vector
    //
    // Produced by `python3 tools/build.py --input examples/recipients-example.csv --batch-size 3`.
    // If the Python tools and the Solidity code ever disagree on hashing or encoding, this fails.

    function test_MatchesPythonBuilderVector() public pure {
        address[] memory r = new address[](3);
        r[0] = 0x4444444444444444444444444444444444444444;
        r[1] = 0x5555555555555555555555555555555555555555;
        r[2] = 0x6666666666666666666666666666666666666666;
        uint256[] memory a = new uint256[](3);
        a[0] = 1000 ether;
        a[1] = 42 ether;
        a[2] = 7.125 ether;
        bytes32 leaf1 = BatchTree.leaf(1, r, a);
        assertEq(leaf1, 0x6cd516e53f31e84c522246f625a470bddc81f92d928be0b9f6d8364038147880);

        bytes32[] memory lv0 = new bytes32[](3);
        lv0[0] = 0xb899b44f0d9fd8e1efc94a32230b4dea88e80fa1fde046b703c5c4c0af8e70a7;
        lv0[1] = leaf1;
        lv0[2] = 0x31ff0c6cdd64732a5bc1ac8a6c12e4989d1b14473105318c2e0680e190a095ff;
        assertEq(BatchTree.root(lv0), 0xeb572a4ceb7e436de5d9fd48add8ffd54d06754b5eea731bf275b4196dbcf6a8);

        bytes32[] memory proof = new bytes32[](2);
        proof[0] = lv0[0];
        proof[1] = lv0[2];
        assertTrue(BatchTree.verify(proof, BatchTree.root(lv0), leaf1));
    }

    // ------------------------------------------------------------------ gas reference

    function test_GasReferenceBatchOf400() public {
        _buildDistribution(400, 400, 4);
        CommittedBatchAirdrop d = new CommittedBatchAirdrop(
            IERC20(address(token)), reserve, admin, executor, root, 1, total, keccak256("gas")
        );
        vm.prank(reserve);
        token.approve(address(d), total);
        vm.prank(executor);
        uint256 g = gasleft();
        d.execute(0, recipients[0], amounts[0], proofs[0]);
        g -= gasleft();
        // well under the 16,777,216 per-transaction cap even after calldata and base cost
        assertLt(g + 21_000 + 400 * 700, 16_777_216);
        emit log_named_uint("execution gas for a 400-recipient batch", g);
    }
}
