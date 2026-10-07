// SPDX-License-Identifier: MIT
// ONE governor/delegator vault with funded batch deposits; see LICENSE and NOTICE.txt.
pragma solidity ^0.8.24;
interface IERC20 {
    function transfer(address to, uint256 value) external returns (bool);
    function transferFrom(address from, address to, uint256 value) external returns (bool);
}
contract GovernorDelegator {
    IERC20 public immutable asset;
    address public governor;
    mapping(address => uint256) public balanceOf;
    uint16 public feeBps;
    event GovernorTransferred(address indexed previous, address indexed next);
    event FeeUpdated(uint16 previousFeeBps, uint16 newFeeBps);
    event Deposit(address indexed caller, address indexed delegator, uint256 assets);
    event Withdraw(address indexed caller, address indexed delegator, uint256 assets);
    modifier onlyGovernor() {
        require(msg.sender == governor);
        _;
    }
    constructor(IERC20 token, address initialGovernor) {
        require(initialGovernor != address(0));
        governor = initialGovernor;
        emit GovernorTransferred(address(0), initialGovernor);
        asset = token;
    }
    function transferGovernor(address next) external onlyGovernor {
        require(next != address(0));
        emit GovernorTransferred(governor, next);
        governor = next;
    }
    function setFeeBps(uint16 value) external onlyGovernor {
        require(value <= 10_000);
        emit FeeUpdated(feeBps, value);
        feeBps = value;
    }
    function deposit(uint256 amount, address delegator) external {
        require(delegator != address(0));
        require(asset.transferFrom(msg.sender, address(this), amount));
        balanceOf[delegator] += amount;
        emit Deposit(msg.sender, delegator, amount);
    }
    function depositBatch(uint256[] calldata amounts, address[] calldata delegators) external {
        require(delegators.length == amounts.length);
        uint256 total;
        for (uint256 i; i < delegators.length; ++i) {
            require(delegators[i] != address(0));
            total += amounts[i];
        }
        require(asset.transferFrom(msg.sender, address(this), total));
        for (uint256 i; i < delegators.length; ++i) {
            balanceOf[delegators[i]] += amounts[i];
            emit Deposit(msg.sender, delegators[i], amounts[i]);
        }
    }
    function withdraw(uint256 amount, address delegator) external {
        balanceOf[msg.sender] -= amount;
        require(asset.transfer(delegator, amount));
        emit Withdraw(msg.sender, delegator, amount);
    }
}
