// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

/// @title RehearsalToken
/// @notice A stand-in token for testnet rehearsals only. It behaves like the production ONE ERC-20
///         (fixed supply created once for a single holder, plain transfer / approve / transferFrom,
///         reverts instead of returning false) so a Sepolia rehearsal exercises the same code paths.
///         It is never deployed on Ethereum mainnet.
contract RehearsalToken {
    string public constant name = "Rehearsal ONE";
    string public constant symbol = "rONE";
    uint8 public constant decimals = 18;
    uint256 public immutable totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    error InsufficientBalance(address sender, uint256 balance, uint256 needed);
    error InsufficientAllowance(address spender, uint256 allowance, uint256 needed);
    error InvalidReceiver(address receiver);

    /// @param holder  receives the entire supply
    /// @param supply  total supply in the smallest unit
    constructor(address holder, uint256 supply) {
        if (holder == address(0)) revert InvalidReceiver(address(0));
        totalSupply = supply;
        balanceOf[holder] = supply;
        emit Transfer(address(0), holder, supply);
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 current = allowance[from][msg.sender];
        if (current != type(uint256).max) {
            if (current < value) revert InsufficientAllowance(msg.sender, current, value);
            allowance[from][msg.sender] = current - value;
        }
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) private {
        if (to == address(0)) revert InvalidReceiver(address(0));
        uint256 fromBalance = balanceOf[from];
        if (fromBalance < value) revert InsufficientBalance(from, fromBalance, value);
        balanceOf[from] = fromBalance - value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}
