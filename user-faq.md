## What will happen to my tokens?

We took a cutoff snapshot of the blockchain and calculated each allocation.
Same-address delivery applies only when the wallet policy and destination
checks allow it; active stake is represented separately through validator-vault
shares.

The cutoff time (asset migration snapshot time) is Thursday, September 10, 2026, at 7 a.m. Pacific Time (14:00 UTC).

Wallets with at least 1,000 ONE in combined snapshot qualification value and
indexed activity in the six calendar months before cutoff are in the initial
stage. Liquid ONE, active delegation or validator stake, pending undelegation,
unclaimed staking rewards, supported pending cross-shard transfers, and WONE
held on Harmony are summed before deductions for threshold membership.

Wallets with below 1,000 ONE may claim from a claim portal at a later time, or wait for another batch of airdrop when it makes economic sense to do so - there is no scheduled time for that at this time.

If you own ONE through an exchange, there is normally nothing you need to do.
We are coordinating directly with participating exchanges. Exchange-controlled
wallets that requested consolidation will be excluded from the ordinary
same-address automatic airdrop and transferred manually to the exchange's
confirmed Ethereum destination. Gate did not request consolidation, so its
wallets continue through the ordinary at-least-1,000-ONE wallet policy,
including the six-month initial-stage rule.
Your exchange remains responsible for crediting your account; contact it for
its customer timeline.

There are more complex scenarios, such as:
1. you delegated some ONE to a validator
2. you own bridged assets on Harmony (originated from another chain)
3. you own WONE on other chains (originated from Harmony)
4. your assets are in a smart-contract wallet, such as Harmony Multisig or 1wallet
5. you deployed your ONE or bridged assets in dApps, such as liquidity pools or lending pools

In these scenarios, the process or outcome would be slightly different. See below.

## What will happen to dApps and smart contracts?

Contract code will not be migrated. Reviewed multisig, LayerZero collateral,
and 1wallet allocations are reserved for a later stage. SmartVault and other
reviewed contract allocations are not issued and remain in the 2050 premint
reserve.

## Do I need to do anything?

Normally, no. Please just wait for the airdrop. If you are in one of the more complex scenarios described above, see below.

### I delegated some ONE to a validator

Your validator will become a governor of an ERC-4626 vault on Ethereum. All delegated ONE will be deposited into the vault. Your delegation will become shares in the vault. You are free to withdraw (subject to a delay, similar to undelegation) or deposit into the vault, just like you do with staking and delegation on Harmony. You still earn rewards from the governor's reward pool, proportional to your delegation (subject to the governor's fee), as described in the token migration transition announcement.

### I own bridged assets on Harmony

A claim contract will be made available on the chain where the asset originated, and you will be able to claim the original asset 1:1 to the same wallet address where you own the bridged asset.

### I own WONE on other chains

A claim contract on Ethereum will be made available for you to claim the new ERC-20 ONE at a 1:1 ratio. It will be funded from the separately reconciled Harmony-side LayerZero NativeOFT reserve for that route, not from the Harmony WONE contract reserve; claims transfer that existing reserve and do not create additional ONE. We will most likely use the pre-hack snapshot on those chains to determine your WONE balance, since there are still 2.412 billion forged WONE remaining on BSC and 12.9 million forged WONE on Ethereum following the 8/11/2026 forge-mint incident. We are still looking into whether we can use a more up-to-date snapshot and surgically remove the hacker's balances and transfers. If we can, we will use that instead.

### My assets are in a smart-contract wallet

Reviewed multisig allocations are reserved for the next stage regardless of
activity. Create a Safe on Ethereum with the same owners and threshold and
retain evidence linking it to the cutoff owner set. Do not assume that the old
Harmony contract address is a valid Ethereum destination.

Reviewed 1wallet allocations will use next-stage recovery-multisig handling.
The implementation will preserve cutoff recovery/ownership evidence and will
not substitute an unverified destination. SmartVault is a separate family and
is not issued under the selected policy.

### I deployed ONE or bridged assets in dApps, such as liquidity pools or lending pools

The cutoff has passed. Reviewed pool, application, token, and unidentified
contract allocations outside the approved next-stage set are not issued. This
policy does not assert that every contract lacked underlying holders; it means
the migration does not create replacement tokens or vault shares for those
contract allocations.
