## What will happen to my tokens?

We will take a snapshot of the blockchain, find out how much ONE is in your wallet (including staking rewards and pending undelegations), and airdrop the new Harmony ERC-20 token on Ethereum to you using the same wallet address.

The cutoff time (asset migration snapshot time) is Thursday, September 10, 2026, at 7 a.m. Pacific Time (14:00 UTC).

Wallets with at least 1,000 ONE will be prioritized. Your liquid ONE, pending undelegation, unclaimed staking rewards, and pending cross-shard transfers will summed together for this calculation. 

Wallets with below 1,000 ONE may claim from a claim portal at a later time, or wait for another batch of airdrop when it makes economic sense to do so - there is no scheduled time for that at this time.

If you own some ONE at an exchange, there is nothing you need to do. Your exchange will receive the airdrop automatically. Some exchanges also work with us directly to ensure the process go smoothly.

There are more complex scenarios, such as:
1. you delegated some ONE to a validator
2. you own bridged assets on Harmony (originated from another chain)
3. you own WONE on other chains (originated from Harmony)
4. your assets are in a smart-contract wallet, such as Harmony Multisig or 1wallet
5. you deployed your ONE or bridged assets in dApps, such as liquidity pools or lending pools

In these scenarios, the process or outcome would be slightly different. See below.

## What will happen to dApps and smart contracts?

They will not be migrated. If you are a dApp developer, please deploy your dApp on Ethereum.

## Do I need to do anything?

Normally, no. Please just wait for the airdrop. If you are in one of the more complex scenarios described above, see below.

### I delegated some ONE to a validator

Your validator will become a governor of an ERC-4626 vault on Ethereum. All delegated ONE will be deposited into the vault. Your delegation will become shares in the vault. You are free to withdraw (subject to a delay, similar to undelegation) or deposit into the vault, just like you do with staking and delegation on Harmony. You still earn rewards from the governor's reward pool, proportional to your delegation (subject to the governor's fee), as described in the token migration transition announcement.

### I own bridged assets on Harmony

A claim contract will be made available on the chain where the asset originated, and you will be able to claim the original asset 1:1 to the same wallet address where you own the bridged asset.

### I own WONE on other chains

A claim contract on Ethereum will be made available for you to claim the new ERC-20 ONE at a 1:1 ratio. We will most likely use the pre-hack snapshot on those chains to determine your WONE balance, since there are still 2.412 billion forged WONE remaining on BSC and 12.9 million forged WONE on Ethereum following the 8/11/2026 forge-mint incident. We are still looking into whether we can use a more up-to-date snapshot and surgically remove the hacker's balances and transfers. If we can, we will use that instead.

### My assets are in a smart-contract wallet

We urge you to withdraw your assets to a simple EOA wallet (Ledger, MetaMask, command line wallet...) before the cutoff time. Smart contracts will not be migrated, so automatic migration of those balances is not guaranteed. If you own a multisig and want to continue using a multisig to hold the new ERC-20 ONE or other assets on Ethereum, please:
1. create a Gnosis Safe wallet on Ethereum, and re-create the owners and configurations
2. move the assets from your Harmony multisig to a simple EOA wallet before the cutoff time
3. wait for the ERC-20 ONE airdrop. If you own other bridged assets, use that EOA wallet to complete the claim process
4. after migration, move the airdropped ERC-20 ONE and other assets to the Gnosis Safe you created

For those who were unable to do that before cutoff time, please:

1. Create a Safe on Ethereum with exactly the same owners
2. If your Safe has at least 1,000,000 ONE: you may contact us with your new Safe address so we can verify the ownership and transfer the allocation to your new Safe address; or,
3. Wait for an automated claim portal to do that. It might take a while

### I deployed ONE or bridged assets in dApps, such as liquidity pools or lending pools

Please withdraw your ONE or bridged assets to a simple EOA wallet (Ledger, MetaMask, command line wallet...) as soon as possible. DApps and pools will not be migrated, so your funds may be lost if you do not act before the cutoff time.
