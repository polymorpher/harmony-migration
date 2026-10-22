// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

interface IERC20Minimal {
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
}

interface IWrappedNative {
    function deposit() external payable;
    function withdraw(uint256 amount) external;
}

/// @title BridgeClaims
/// @notice Pays a fixed, pre-published list of bridge-collateral claims on one chain, every asset of
///         that chain in one list. Tokens move straight from the custody multisig to each payee.
///
/// How it works, in plain terms:
///
/// 1. Off-chain, every claim on this chain (one row per payee and asset) gets a number (`index`)
///    and is reduced to a 32-byte fingerprint (`leaf`). All fingerprints are combined into one
///    32-byte `root`. The list and the root are published so anyone can recompute them.
/// 2. This contract is deployed with that `root`, the custody multisig (`funder`), the wrapped
///    native coin and the chain id baked in. Nothing can be changed afterwards: there is no owner,
///    no admin, no pause and no upgrade. If the list ever needs to change, a new contract is
///    deployed with a new root.
/// 3. The funder approves this contract, per asset, for exactly the total of that asset's rows.
///    Tokens stay in the funder until claimed. The funder can stop the claims of any asset at any
///    time by lowering or revoking its approval; that needs nothing from this contract.
/// 4. A claim submits a row (index, account, token, amount) and the sibling hashes that connect
///    its fingerprint to `root`. The contract checks them, marks the row paid, and moves `amount`
///    of `token` from the funder to `account`. Anyone may submit a claim, but the tokens always go
///    to the row's `account`, and each row pays once.
/// 5. Native coin rows (ETH, BNB) use token address 0. The funder holds and approves the wrapped
///    coin (WETH, WBNB); the contract unwraps it and sends the coin. An account that does not
///    accept the coin receives the wrapped token instead.
///
/// The contract holds no tokens between transactions. Anything sent to it by mistake can be
/// returned to the funder by anyone with `sweep`.
contract BridgeClaims {
    // ------------------------------------------------------------------ fixed at deployment

    /// @notice Token address used in rows that pay the chain's native coin.
    address public constant NATIVE = address(0);

    /// @notice The custody multisig that holds the assets and approves this contract.
    address public immutable funder;
    /// @notice The wrapped native coin (WETH, WBNB) the funder approves for native rows; zero on a
    ///         chain without native rows.
    address public immutable wrappedNative;
    /// @notice The chain this list belongs to; deployment on any other chain reverts.
    uint256 public immutable chainId;
    /// @notice Fingerprint of the complete published list. Never changes.
    bytes32 public immutable root;
    /// @notice Number of rows in the published list. Indexes run from 0 to claimCount - 1.
    uint256 public immutable claimCount;
    /// @notice SHA-256 of the published list file, so the contract can be tied to the exact file
    ///         people download. Informational only; it is not used in any check.
    bytes32 public immutable listHash;

    // ------------------------------------------------------------------ state

    /// @dev Bit i of word i/256 is set once row i has been paid.
    mapping(uint256 => uint256) private _claimedBitmap;

    // ------------------------------------------------------------------ types, events and errors

    /// @notice One row of the published list together with its proof.
    struct Claim {
        uint256 index;
        address account;
        address token;
        uint256 amount;
        bytes32[] proof;
    }

    event Claimed(uint256 indexed index, address indexed account, address indexed token, uint256 amount);
    /// @notice A native-coin row paid in the wrapped coin because the account refused the coin.
    event PaidWrapped(uint256 indexed index, address indexed account, uint256 amount);
    event Swept(address indexed token, uint256 amount);

    error ZeroAddress();
    error ZeroValue();
    error WrongChain(uint256 expected, uint256 actual);
    error IndexOutOfRange(uint256 index, uint256 claimCount);
    error AlreadyClaimed(uint256 index);
    error InvalidProof(uint256 index);
    error NativeNotSupported();
    error TransferFailed(address token, address to, uint256 amount);
    error UnexpectedNativeSender(address sender);

    // ------------------------------------------------------------------ construction

    /// @param funder_        the custody multisig holding the assets (approves this contract)
    /// @param wrappedNative_ the wrapped native coin, or zero if the list has no native rows
    /// @param chainId_       the chain the list belongs to
    /// @param root_          fingerprint of the published list (see `leaf`)
    /// @param claimCount_    number of rows in that list
    /// @param listHash_      SHA-256 of the published list file
    constructor(
        address funder_,
        address wrappedNative_,
        uint256 chainId_,
        bytes32 root_,
        uint256 claimCount_,
        bytes32 listHash_
    ) {
        if (funder_ == address(0)) revert ZeroAddress();
        if (root_ == bytes32(0) || claimCount_ == 0) revert ZeroValue();
        if (chainId_ != block.chainid) revert WrongChain(chainId_, block.chainid);
        funder = funder_;
        wrappedNative = wrappedNative_;
        chainId = chainId_;
        root = root_;
        claimCount = claimCount_;
        listHash = listHash_;
    }

    // ------------------------------------------------------------------ claiming

    /// @notice Pay one row of the published list to its account.
    /// @param index   position of the row in the published list
    /// @param account the payee
    /// @param token   the asset (an ERC-20 address, or `NATIVE` for the native coin)
    /// @param amount  the amount in the asset's smallest unit
    /// @param proof   the sibling hashes that connect this row's fingerprint to `root`
    function claim(uint256 index, address account, address token, uint256 amount, bytes32[] calldata proof)
        external
    {
        _claim(index, account, token, amount, proof);
    }

    /// @notice Pay several rows in one transaction, for example every asset of one payee. Rows that
    ///         were already paid are skipped, so a batch is not blocked by someone claiming one of its
    ///         rows first; any other failure reverts the whole batch.
    /// @return paid the number of rows paid by this call
    function claimMany(Claim[] calldata claims) external returns (uint256 paid) {
        for (uint256 i = 0; i < claims.length; ++i) {
            Claim calldata c = claims[i];
            if (c.index < claimCount && isClaimed(c.index)) continue;
            _claim(c.index, c.account, c.token, c.amount, c.proof);
            ++paid;
        }
    }

    // ------------------------------------------------------------------ read-only helpers

    /// @notice The fingerprint of a row: keccak256 of keccak256 of the ABI encoding of
    ///         (index, account, token, amount). The double hash keeps row fingerprints distinct from
    ///         the internal nodes of the tree, following the OpenZeppelin convention.
    function leaf(uint256 index, address account, address token, uint256 amount) public pure returns (bytes32) {
        return keccak256(bytes.concat(keccak256(abi.encode(index, account, token, amount))));
    }

    /// @notice True if a row belongs to the published list. Lets anyone check a row and its proof
    ///         without sending a transaction.
    function verify(uint256 index, address account, address token, uint256 amount, bytes32[] calldata proof)
        external
        view
        returns (bool)
    {
        return index < claimCount && _verify(proof, leaf(index, account, token, amount));
    }

    /// @notice True once row `index` has been paid.
    function isClaimed(uint256 index) public view returns (bool) {
        return _claimedBitmap[index >> 8] & (1 << (index & 0xff)) != 0;
    }

    /// @notice Raw bitmap word; bit i of word w corresponds to row w * 256 + i.
    function claimedBitmap(uint256 word) external view returns (uint256) {
        return _claimedBitmap[word];
    }

    /// @notice How much of an asset can be claimed right now: the smaller of the funder's approval to
    ///         this contract and the funder's balance. For `NATIVE` this is the wrapped coin.
    function available(address token) external view returns (uint256) {
        address asset = token == NATIVE ? wrappedNative : token;
        if (asset == address(0)) return 0;
        uint256 approved = IERC20Minimal(asset).allowance(funder, address(this));
        uint256 balance = IERC20Minimal(asset).balanceOf(funder);
        return approved < balance ? approved : balance;
    }

    // ------------------------------------------------------------------ mistakes

    /// @notice Return this contract's whole balance of `token` (or of the native coin, for `NATIVE`)
    ///         to the funder. The contract never needs to hold anything, so under normal operation
    ///         there is nothing to move. Anyone may call it; the destination is always the funder.
    function sweep(address token) external {
        uint256 amount;
        if (token == NATIVE) {
            amount = address(this).balance;
            if (!_sendNative(funder, amount)) revert TransferFailed(NATIVE, funder, amount);
        } else {
            amount = IERC20Minimal(token).balanceOf(address(this));
            _safeTransfer(token, funder, amount);
        }
        emit Swept(token, amount);
    }

    /// @dev Only the wrapped coin sends the native coin here, while unwrapping a claim.
    receive() external payable {
        if (msg.sender != wrappedNative) revert UnexpectedNativeSender(msg.sender);
    }

    // ------------------------------------------------------------------ internal

    function _claim(uint256 index, address account, address token, uint256 amount, bytes32[] calldata proof)
        private
    {
        if (index >= claimCount) revert IndexOutOfRange(index, claimCount);
        uint256 word = index >> 8;
        uint256 bit = 1 << (index & 0xff);
        uint256 bits = _claimedBitmap[word];
        if (bits & bit != 0) revert AlreadyClaimed(index);
        if (!_verify(proof, leaf(index, account, token, amount))) revert InvalidProof(index);
        _claimedBitmap[word] = bits | bit;
        emit Claimed(index, account, token, amount);

        if (token == NATIVE) {
            _payNative(index, account, amount);
        } else {
            _safeTransferFrom(token, funder, account, amount);
        }
    }

    function _payNative(uint256 index, address account, uint256 amount) private {
        address wrapped = wrappedNative;
        if (wrapped == address(0)) revert NativeNotSupported();
        _safeTransferFrom(wrapped, funder, address(this), amount);
        IWrappedNative(wrapped).withdraw(amount);
        if (!_sendNative(account, amount)) {
            IWrappedNative(wrapped).deposit{value: amount}();
            _safeTransfer(wrapped, account, amount);
            emit PaidWrapped(index, account, amount);
        }
    }

    /// @dev Sends the coin without copying any return data, so a recipient cannot make the call
    ///      expensive by returning a large payload.
    function _sendNative(address to, uint256 amount) private returns (bool sent) {
        assembly ("memory-safe") {
            sent := call(gas(), to, amount, 0, 0, 0, 0)
        }
    }

    function _safeTransferFrom(address token, address from, address to, uint256 amount) private {
        if (!_tokenCall(token, abi.encodeWithSelector(0x23b872dd, from, to, amount))) {
            revert TransferFailed(token, to, amount);
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) private {
        if (!_tokenCall(token, abi.encodeWithSelector(0xa9059cbb, to, amount))) {
            revert TransferFailed(token, to, amount);
        }
    }

    /// @dev Accepts tokens that return `true` and tokens that return nothing (such as Ethereum USDT);
    ///      rejects a `false` return, a revert, and an address without code.
    function _tokenCall(address token, bytes memory data) private returns (bool) {
        (bool ok, bytes memory ret) = token.call(data);
        if (!ok) return false;
        if (ret.length == 0) return token.code.length != 0;
        return ret.length >= 32 && abi.decode(ret, (uint256)) == 1;
    }

    /// @dev Standard sorted-pair Merkle proof check: hash the leaf with each sibling in turn,
    ///      smaller value first, and compare the result with `root`.
    function _verify(bytes32[] calldata proof, bytes32 leaf_) private view returns (bool) {
        bytes32 h = leaf_;
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
