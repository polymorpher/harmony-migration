// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

import {IERC20} from "./IERC20.sol";

/// @title CommittedBatchAirdrop
/// @notice Sends a fixed, pre-published list of ERC-20 payments, in batches, and nothing else.
///
/// How it works, in plain terms:
///
/// 1. Off-chain, the full recipient list is split into numbered batches. Each batch is reduced to a
///    32-byte fingerprint (`batchLeaf`), and all batch fingerprints are combined into one 32-byte
///    `root`. The list, the batches and the root are published so anyone can recompute them.
/// 2. This contract is deployed with that `root` baked in. It can never be changed. If the list ever
///    needs to change, a new contract is deployed with a new root and this one is abandoned.
/// 3. The account holding the tokens (`reserve`) approves this contract to spend exactly
///    `totalAmount`. Tokens stay in the reserve until each batch is actually sent.
/// 4. An operator wallet (`executor`) submits batches one transaction at a time. The contract
///    recomputes the batch fingerprint from the submitted recipients and amounts and checks that it
///    belongs to the root. A batch that was not in the published list, or that was already sent, is
///    rejected. The executor therefore cannot change a recipient, change an amount, add a payment,
///    or pay anyone twice. The worst a leaked executor key can do is send the published batches
///    earlier than planned.
/// 5. `admin` (normally the same multisig as the reserve) can pause the contract and replace the
///    executor. It cannot change what is paid or to whom. Revoking the token approval is the
///    ultimate stop button and needs nothing from this contract.
///
/// The contract never holds tokens itself under normal operation.
contract CommittedBatchAirdrop {
    // ------------------------------------------------------------------ fixed at deployment

    /// @notice The ERC-20 token being distributed.
    IERC20 public immutable token;
    /// @notice The account that holds the tokens and approved this contract for `totalAmount`.
    address public immutable reserve;
    /// @notice The account that may pause the contract and replace the executor.
    address public immutable admin;
    /// @notice Fingerprint of the complete published batch list. Never changes.
    bytes32 public immutable root;
    /// @notice Number of batches in the published list. Batch indexes run from 0 to batchCount - 1.
    uint256 public immutable batchCount;
    /// @notice Sum of every amount in the published list; the reserve approves exactly this much.
    uint256 public immutable totalAmount;
    /// @notice SHA-256 of the published recipient list file, so the contract can be tied to the exact
    ///         file people download. Informational only; it is not used in any check.
    bytes32 public immutable listHash;

    // ------------------------------------------------------------------ operational state

    /// @notice The wallet allowed to submit batches.
    address public executor;
    /// @notice When true, `execute` is disabled.
    bool public paused;
    /// @notice How many batches have been sent so far.
    uint256 public executedBatches;
    /// @notice How many tokens have been sent so far (should reach `totalAmount`).
    uint256 public distributedAmount;
    /// @dev Bit i of word i/256 is set once batch i has been sent.
    mapping(uint256 => uint256) private _executedBitmap;

    // ------------------------------------------------------------------ events and errors

    event BatchExecuted(uint256 indexed batchIndex, bytes32 indexed leaf, uint256 recipients, uint256 amount);
    event ExecutorChanged(address indexed previousExecutor, address indexed newExecutor);
    event PausedSet(bool paused);
    event Rescued(address indexed asset, address indexed to, uint256 amount);

    error ZeroAddress();
    error ZeroValue();
    error NotExecutor(address caller);
    error NotAdmin(address caller);
    error IsPaused();
    error LengthMismatch();
    error EmptyBatch();
    error BatchIndexOutOfRange(uint256 batchIndex, uint256 batchCount);
    error BatchNotInList(uint256 batchIndex, bytes32 leaf);
    error BatchAlreadyExecuted(uint256 batchIndex);
    error TransferFailed(address to, uint256 amount);

    // ------------------------------------------------------------------ construction

    /// @param token_       the ERC-20 token to distribute
    /// @param reserve_     the account holding the tokens (approves this contract for `totalAmount_`)
    /// @param admin_       the account allowed to pause and to replace the executor
    /// @param executor_    the wallet that will submit batches
    /// @param root_        fingerprint of the published batch list (see `batchLeaf`)
    /// @param batchCount_  number of batches in that list
    /// @param totalAmount_ sum of all amounts in that list
    /// @param listHash_    SHA-256 of the published recipient list file
    constructor(
        IERC20 token_,
        address reserve_,
        address admin_,
        address executor_,
        bytes32 root_,
        uint256 batchCount_,
        uint256 totalAmount_,
        bytes32 listHash_
    ) {
        if (
            address(token_) == address(0) || reserve_ == address(0) || admin_ == address(0)
                || executor_ == address(0)
        ) {
            revert ZeroAddress();
        }
        if (root_ == bytes32(0) || batchCount_ == 0 || totalAmount_ == 0) revert ZeroValue();
        token = token_;
        reserve = reserve_;
        admin = admin_;
        executor = executor_;
        root = root_;
        batchCount = batchCount_;
        totalAmount = totalAmount_;
        listHash = listHash_;
        emit ExecutorChanged(address(0), executor_);
    }

    // ------------------------------------------------------------------ the one thing this contract does

    /// @notice Send one published batch. Anyone can read the batch file, but only `executor` may call.
    /// @param batchIndex position of the batch in the published list
    /// @param recipients the batch's recipient addresses, in published order
    /// @param amounts    the batch's amounts (smallest token unit), in published order
    /// @param proof      the sibling hashes that connect this batch's fingerprint to `root`
    function execute(
        uint256 batchIndex,
        address[] calldata recipients,
        uint256[] calldata amounts,
        bytes32[] calldata proof
    ) external {
        if (msg.sender != executor) revert NotExecutor(msg.sender);
        if (paused) revert IsPaused();
        uint256 n = recipients.length;
        if (n == 0) revert EmptyBatch();
        if (amounts.length != n) revert LengthMismatch();
        if (batchIndex >= batchCount) revert BatchIndexOutOfRange(batchIndex, batchCount);

        bytes32 leaf = batchLeaf(batchIndex, recipients, amounts);
        if (!_verify(proof, leaf)) revert BatchNotInList(batchIndex, leaf);

        uint256 word = batchIndex >> 8;
        uint256 bit = 1 << (batchIndex & 0xff);
        uint256 bits = _executedBitmap[word];
        if (bits & bit != 0) revert BatchAlreadyExecuted(batchIndex);
        _executedBitmap[word] = bits | bit;

        uint256 sum;
        for (uint256 i = 0; i < n; ++i) {
            address to = recipients[i];
            uint256 amount = amounts[i];
            if (!token.transferFrom(reserve, to, amount)) revert TransferFailed(to, amount);
            sum += amount;
        }

        executedBatches += 1;
        distributedAmount += sum;
        emit BatchExecuted(batchIndex, leaf, n, sum);
    }

    // ------------------------------------------------------------------ read-only helpers

    /// @notice The fingerprint of a batch: keccak256 of keccak256 of the ABI encoding of
    ///         (batchIndex, recipients, amounts). The double hash keeps batch fingerprints distinct
    ///         from the internal nodes of the tree, following the OpenZeppelin convention.
    function batchLeaf(uint256 batchIndex, address[] calldata recipients, uint256[] calldata amounts)
        public
        pure
        returns (bytes32)
    {
        return keccak256(bytes.concat(keccak256(abi.encode(batchIndex, recipients, amounts))));
    }

    /// @notice True if a batch belongs to the published list. Lets anyone check a batch file against
    ///         the contract without sending a transaction.
    function verifyBatch(
        uint256 batchIndex,
        address[] calldata recipients,
        uint256[] calldata amounts,
        bytes32[] calldata proof
    ) external view returns (bool) {
        if (batchIndex >= batchCount || recipients.length == 0 || recipients.length != amounts.length) return false;
        return _verify(proof, batchLeaf(batchIndex, recipients, amounts));
    }

    /// @notice True once batch `batchIndex` has been sent.
    function isExecuted(uint256 batchIndex) external view returns (bool) {
        return _executedBitmap[batchIndex >> 8] & (1 << (batchIndex & 0xff)) != 0;
    }

    /// @notice Raw bitmap word; bit i of word w corresponds to batch w * 256 + i.
    function executedBitmap(uint256 word) external view returns (uint256) {
        return _executedBitmap[word];
    }

    /// @notice Tokens still to be sent.
    function remainingAmount() external view returns (uint256) {
        return totalAmount - distributedAmount;
    }

    // ------------------------------------------------------------------ admin

    /// @notice Replace the operational wallet. Does not affect what can be sent.
    function setExecutor(address newExecutor) external {
        if (msg.sender != admin) revert NotAdmin(msg.sender);
        if (newExecutor == address(0)) revert ZeroAddress();
        emit ExecutorChanged(executor, newExecutor);
        executor = newExecutor;
    }

    /// @notice Stop or resume `execute`. Pausing does not move any tokens.
    function setPaused(bool paused_) external {
        if (msg.sender != admin) revert NotAdmin(msg.sender);
        paused = paused_;
        emit PausedSet(paused_);
    }

    /// @notice Recover tokens sent to this contract by mistake. The contract never needs to hold
    ///         tokens, so under normal operation there is nothing here to move.
    function rescue(IERC20 asset, address to, uint256 amount) external {
        if (msg.sender != admin) revert NotAdmin(msg.sender);
        if (to == address(0)) revert ZeroAddress();
        if (!asset.transfer(to, amount)) revert TransferFailed(to, amount);
        emit Rescued(address(asset), to, amount);
    }

    // ------------------------------------------------------------------ internal

    /// @dev Standard sorted-pair Merkle proof check: hash the leaf with each sibling in turn,
    ///      smaller value first, and compare the result with `root`.
    function _verify(bytes32[] calldata proof, bytes32 leaf) private view returns (bool) {
        bytes32 h = leaf;
        for (uint256 i = 0; i < proof.length; ++i) {
            bytes32 p = proof[i];
            h = h < p ? _hashPair(h, p) : _hashPair(p, h);
        }
        return h == root;
    }

    function _hashPair(bytes32 a, bytes32 b) private pure returns (bytes32 out) {
        assembly ("memory-safe") {
            mstore(0x00, a)
            mstore(0x20, b)
            out := keccak256(0x00, 0x40)
        }
    }
}
