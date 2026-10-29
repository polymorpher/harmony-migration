// SPDX-License-Identifier: MIT
pragma solidity 0.8.36;

/// @notice Standard ERC-20 that returns `true`.
contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external virtual returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external virtual returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external virtual returns (bool) {
        _spend(from, amount);
        _move(from, to, amount);
        return true;
    }

    function _spend(address from, uint256 amount) internal {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
    }

    function _move(address from, address to, uint256 amount) internal virtual {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }
}

/// @notice Ethereum-USDT-like token: no return values, and a nonzero approval can only replace zero.
contract NoReturnToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external {
        require(amount == 0 || allowance[msg.sender][spender] == 0, "reset approval first");
        allowance[msg.sender][spender] = amount;
    }

    function transfer(address to, uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
    }

    function transferFrom(address from, address to, uint256 amount) external {
        require(allowance[from][msg.sender] >= amount, "allowance");
        require(balanceOf[from] >= amount, "balance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }
}

/// @notice Token whose transfers report failure without reverting.
contract FalseToken is MockERC20 {
    function transferFrom(address, address, uint256) external pure override returns (bool) {
        return false;
    }
}

/// @notice Token that keeps a 1% fee on every transfer, the behaviour of a fee-on-transfer token.
contract FeeToken is MockERC20 {
    address public constant FEE_SINK = address(0xFEE);

    function _move(address from, address to, uint256 amount) internal override {
        require(balanceOf[from] >= amount, "balance");
        uint256 fee = amount / 100;
        balanceOf[from] -= amount;
        balanceOf[to] += amount - fee;
        balanceOf[FEE_SINK] += fee;
    }
}

/// @notice WETH9 semantics: `withdraw` sends the coin with a 2,300 gas stipend.
contract MockWETH is MockERC20 {
    receive() external payable {
        balanceOf[msg.sender] += msg.value;
    }

    function deposit() external payable {
        balanceOf[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        payable(msg.sender).transfer(amount);
    }
}

/// @notice An account that refuses the native coin.
contract RejectNative {
    receive() external payable {
        revert("no coin");
    }
}

/// @notice An account that answers a payment with a large return payload.
contract ReturnBomb {
    receive() external payable {
        assembly {
            return(0, 200000)
        }
    }
}

interface IClaims {
    function claim(uint256 index, address account, address token, uint256 amount, bytes32[] calldata proof)
        external;
    function sweep(address token) external;
}

/// @notice An account that, when paid in coin, tries to claim the same row again and to sweep.
contract Reenterer {
    IClaims public target;
    uint256 public index;
    address public token;
    uint256 public amount;
    bytes32[] public proof;
    bool public reclaimReverted;
    bool public entered;

    function arm(IClaims target_, uint256 index_, address token_, uint256 amount_, bytes32[] calldata proof_)
        external
    {
        target = target_;
        index = index_;
        token = token_;
        amount = amount_;
        proof = proof_;
    }

    receive() external payable {
        if (entered) return;
        entered = true;
        try target.claim(index, address(this), token, amount, proof) {
            reclaimReverted = false;
        } catch {
            reclaimReverted = true;
        }
        target.sweep(address(0));
    }
}
